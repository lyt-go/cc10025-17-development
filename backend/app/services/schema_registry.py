"""schema_registry 领域服务：Schema注册管理，接收 asset_catalog 上游事件。

跨模块数据流：
    asset_catalog → schema_registry: 资产发布触发Schema注册
    schema_registry → version_snapshot: Schema注册触发版本快照创建

除逐条注册外，本服务支持多选后的结构发布批次：
    1. 一次提交多张互相引用的表，按字段清单和依赖版本评估；
    2. 下游声明所需的上游字段未确认时，该分支不得激活；
    3. 同一次提交重复发布按批次键/内容指纹幂等，不会重复写快照；
    4. 每张已发布表都记录真实来源版本、字段依据和依赖关系，
       未进入发布的表连同原因写入批次记录。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.schema_publication import (
    format_fields,
    normalize_submission,
    parse_fields,
)
from app.services.schema_publication_engine import evaluate_submission
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")
PUBLICATION_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "publications")
SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "snapshots")

BATCH_TABLE = "schema_publication_batch"
MANIFEST_TABLE = "schema_version_manifest"
USAGE_TABLE = "schema_published_usage"
CONFIRMATION_TABLE = "schema_field_confirmation"

SNAPSHOT_TYPE = "结构发布快照"
_TABLE_KEYS = ("tables", "items", "entries", "records", "schemas",
               "表清单", "数据表清单", "表列表")


class SchemaRegistryService(BaseModuleService):
    MODULE = "schema_registry"
    ENTITY = "Schema版本"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["版本编号", "关联资产", "字段清单", "前向兼容", "后向兼容", "Schema摘要", "注册时间", "版本状态"]
    REQUIRED_FIELDS = ["版本编号", "关联资产", "字段清单"]
    STATUS_ORDER = ["草稿", "待校验", "已注册", "已废止"]
    ACTION_RULES = {"提交校验": "待校验", "确认注册": "已注册", "标记废止": "已废止"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        for path in (DATA_DIR, PUBLICATION_DIR, SNAPSHOT_DIR):
            os.makedirs(path, exist_ok=True)

    # ================= 逐条注册（保留原有入口） =================

    def register_schema(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))
        fields = str(entry.get("字段清单", ""))

        entry["status"] = "已注册"
        entry["pending"] = False
        entry["注册时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["版本状态"] = "已注册"

        self._write_schema_record(asset_id, entry)
        self._upsert_manifest(
            asset=asset_id,
            version=str(entry.get("版本编号", "")),
            fields=parse_fields(entry.get("字段清单", "")),
            entry_id=entry.get("id"),
            source="逐条注册",
        )
        self._create_version_snapshot(asset_id, entry)
        return entry, f"{self.ENTITY} {entry_id} 已注册: {fields}"

    def get_schema_for_asset(self, asset_id: str) -> dict[str, Any] | None:
        filename = f"schema_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认注册":
            return self.register_schema(entry_id)
        return super().run_action(entry_id, action)

    # ================= 结构发布批次 =================

    def preview_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        """只评估不落库，返回每张表的发布资格、字段范围和阻断原因。"""
        normalized = self._normalize_publication(payload)
        evaluation = self._evaluate(normalized)
        existing = self._find_identical_batch(normalized, evaluation)
        return {
            "ok": True,
            "persisted": False,
            "replayed": existing is not None,
            "batch": existing,
            "提交": self._submission_digest(normalized),
            "表结论": self._table_verdicts(evaluation),
            "可发布表": [self._verdict_asset(evaluation, key) for key in evaluation["eligible"]],
            "不可发布表": [self._verdict_asset(evaluation, key) for key in evaluation["blocked"]],
            "字段可用范围": self.field_availability(),
            "告警": normalized.get("warnings", []),
        }

    def publish_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        """写入结构发布批次：发布合格表、生成批次快照并记录排除表。"""
        normalized = self._normalize_publication(payload)
        # 评估只做一次：重复提交识别和后续发布必须基于同一份结论。
        evaluation = self._evaluate(normalized)
        existing = self._find_identical_batch(normalized, evaluation)
        if existing is not None:
            if (normalized.get("batch_key")
                    and existing.get("内容指纹") != normalized.get("fingerprint")):
                return {
                    "ok": False,
                    "replayed": False,
                    "message": f"批次键 {normalized['batch_key']} 已被另一份提交占用"
                               f"（{existing.get('批次编号')}），请换一个批次键",
                    "batch": existing,
                }
            return {
                "ok": True,
                "replayed": True,
                "message": f"该次发布已存在批次 {existing.get('批次编号')}，未重复生成快照",
                "batch": existing,
            }

        before = self.field_availability()
        batch_no = len(store.rows(BATCH_TABLE)) + 1
        batch_id = f"PUBL-{batch_no:04d}"
        now = datetime.now(timezone.utc)

        published_tables: list[dict[str, Any]] = []
        for key in evaluation["order"]:
            table = evaluation["tables"][key]
            if not table["eligible"]:
                continue
            published = self._publish_table(table, batch_id, now)
            published_tables.append(published)
            self._record_published_usage(table, batch_id)

        snapshot_set = self._write_batch_snapshot(
            batch_id, batch_no, normalized, evaluation, published_tables, now)
        after = self.field_availability()
        excluded = [
            {
                "关联资产": evaluation["tables"][key]["asset"],
                "来源记录": evaluation["tables"][key]["entry_id"],
                "来源版本": evaluation["tables"][key]["version"],
                "原因": evaluation["tables"][key]["reasons"],
            }
            for key in evaluation["order"]
            if not evaluation["tables"][key]["eligible"]
        ]

        record = {
            "批次编号": batch_id,
            "批次键": normalized.get("batch_key"),
            "内容指纹": normalized.get("fingerprint"),
            "发布状态": "已完成" if published_tables else "未发布",
            "提交表数": len(evaluation["order"]),
            "已发布表数": len(published_tables),
            "排除表数": len(excluded),
            "快照集合": snapshot_set,
            "已发布表": published_tables,
            "未发布表": excluded,
            "提交快照": self._submission_digest(normalized),
            "字段可用范围": {"发布前": before, "发布后": after},
            "提交时间": now.isoformat(),
            "操作人": "数据工程组",
        }
        store.rows(BATCH_TABLE).append({
            "id": batch_no,
            "批次编号": batch_id,
            "批次键": normalized.get("batch_key") or "",
            "内容指纹": normalized.get("fingerprint"),
            "发布状态": record["发布状态"],
            "提交表数": record["提交表数"],
            "已发布表数": record["已发布表数"],
            "排除表数": record["排除表数"],
            "提交时间": record["提交时间"],
            "记录": record,
        })

        self._write_publication_file(batch_id, record)
        self._write_audit_entry(
            entity="Schema注册",
            action="结构发布",
            target_id=batch_id,
            detail=f"批次 {batch_id} 发布 {len(published_tables)} 张表，"
                   f"排除 {len(excluded)} 张表",
        )

        return {
            "ok": True,
            "replayed": False,
            "message": f"结构发布批次 {batch_id} 已写入：发布 {len(published_tables)} 张表，"
                       f"排除 {len(excluded)} 张表",
            "batch": record,
        }

    def list_publications(self) -> list[dict[str, Any]]:
        return [row.get("记录", row) for row in store.rows(BATCH_TABLE)]

    def get_publication(self, batch_ref: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if str(row.get("批次编号")) == batch_ref or str(row.get("批次键")) == batch_ref:
                return row.get("记录", row)
        return None

    def confirm_upstream_fields(self, payload: dict[str, Any]) -> dict[str, Any]:
        """确认下游对上游字段的依赖，解除对应分支的确认门槛。"""
        downstream = str(payload.get("downstream") or payload.get("下游资产") or "").strip()
        upstream = str(payload.get("upstream") or payload.get("上游资产") or "").strip()
        version = payload.get("version") or payload.get("版本编号")
        fields_value = payload.get("fields") or payload.get("字段清单") or []
        if not downstream or not upstream:
            return {"ok": False, "message": "确认字段依赖需要同时提供下游资产和上游资产"}
        parsed = parse_fields(fields_value)
        version_text = str(version).strip() if version not in (None, "") else None
        self._add_confirmation(
            downstream, upstream, version_text, [field["name"] for field in parsed])
        return {
            "ok": True,
            "message": f"已确认 {downstream} 对 {upstream} 字段依赖"
                       + (f"（{format_fields(parsed)}）" if parsed else ""),
            "confirmation": {
                "downstream": downstream,
                "upstream": upstream,
                "version": version_text,
                "fields": [field["name"] for field in parsed],
            },
        }

    def version_catalog(self) -> dict[str, dict[str, Any]]:
        """当前可回查的已注册版本目录。"""
        return self._catalog()

    def field_availability(self) -> dict[str, list[str]]:
        """字段可用范围：每张已发布/已注册表当前可消费的字段。"""
        return {
            asset: [field["name"] for field in record.get("fields", [])]
            for asset, record in self._catalog().items()
        }

    # ---------- 提交归一化 ----------

    def _normalize_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        values = payload.get("values")
        if isinstance(values, dict) and not any(payload.get(key) for key in _TABLE_KEYS):
            payload = {**values, **{k: v for k, v in payload.items() if k != "values"}}
        if not any(payload.get(key) for key in _TABLE_KEYS):
            entry_ids = payload.get("entry_ids") or payload.get("entryIds")
            if entry_ids:
                payload = {
                    **payload,
                    "tables": self._tables_from_entries(entry_ids),
                    "dependencies": self._dependencies_from_lineage(entry_ids),
                }
        return normalize_submission(payload)

    def _tables_from_entries(self, entry_ids: Any) -> list[dict[str, Any]]:
        tables: list[dict[str, Any]] = []
        for raw_id in self._as_list(entry_ids):
            try:
                entry = store.find(self.MODULE, int(raw_id))
            except (TypeError, ValueError):
                entry = None
            if entry is None:
                tables.append({"entry_id": raw_id})
                continue
            tables.append({
                "entry_id": entry.get("id"),
                "asset": entry.get("关联资产"),
                "version": entry.get("版本编号"),
                "fields": entry.get("字段清单"),
            })
        return tables

    def _dependencies_from_lineage(self, entry_ids: Any) -> list[dict[str, Any]]:
        """用血缘记录补全本次提交内部的上下游依赖，不引入提交外的表。"""
        selected: set[str] = set()
        for raw_id in self._as_list(entry_ids):
            try:
                entry = store.find(self.MODULE, int(raw_id))
            except (TypeError, ValueError):
                entry = None
            if entry is not None and entry.get("关联资产"):
                selected.add(str(entry["关联资产"]))
        edges: list[dict[str, Any]] = []
        for row in store.rows("lineage_graph"):
            upstream = str(row.get("上游资产", "")).strip()
            downstream = str(row.get("下游资产", "")).strip()
            if upstream in selected and downstream in selected:
                edges.append({"upstream": upstream, "downstream": downstream})
        return edges

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    # ---------- 评估 ----------

    def _evaluate(self, normalized: dict[str, Any]) -> dict[str, Any]:
        evaluation = evaluate_submission(
            normalized,
            catalog=self._catalog(),
            published_usage=self._published_usage(),
            confirmations=self._confirmations(),
        )
        self._apply_entry_guards(normalized, evaluation)
        return evaluation

    def _apply_entry_guards(self, normalized: dict[str, Any],
                            evaluation: dict[str, Any]) -> None:
        """记录级约束：已废止版本不可再发布，记录与资产必须对得上。"""
        used_entries: dict[int, str] = {}
        for table, key in zip(normalized.get("tables", []), evaluation["order"]):
            entry_id = table.get("entry_id")
            if entry_id is None:
                continue
            try:
                entry = store.find(self.MODULE, int(entry_id))
            except (TypeError, ValueError):
                entry = None
            if entry is None:
                continue
            verdict = evaluation["tables"][key]
            asset = str(table.get("asset", "")).strip()
            entry_asset = str(entry.get("关联资产", "")).strip()
            status = str(entry.get("版本状态") or entry.get("status") or "")
            if entry_asset and asset and entry_asset != asset:
                self._guard_reason(
                    verdict, "entry_asset_mismatch",
                    f"记录 {entry_id} 属于 {entry_asset}，与提交的 {asset} 不一致")
            if status == "已废止":
                self._guard_reason(
                    verdict, "entry_deprecated",
                    f"记录 {entry_id}（{entry.get('版本编号')}）已废止，不得再次发布")
            if int(entry_id) in used_entries:
                self._guard_reason(
                    verdict, "entry_reused",
                    f"记录 {entry_id} 已被 {used_entries[int(entry_id)]} 使用，"
                    f"一张表只能对应一条来源记录")
            else:
                used_entries[int(entry_id)] = asset or key

        # 记录级阻断同样会传导给同批下游分支。
        changed = True
        while changed:
            changed = False
            for table, key in zip(normalized.get("tables", []), evaluation["order"]):
                verdict = evaluation["tables"][key]
                if not verdict["eligible"]:
                    continue
                for dependency in table.get("dependencies") or []:
                    upstream = str(dependency.get("upstream", "")).strip()
                    upstream_verdict = next(
                        (item for item in evaluation["tables"].values()
                         if str(item["asset"]).casefold() == upstream.casefold()), None)
                    if upstream_verdict is None or upstream_verdict["eligible"]:
                        continue
                    self._guard_reason(
                        verdict, "upstream_not_published",
                        f"{verdict['asset']} 依赖的上游表 {upstream} 本次未能进入发布，"
                        f"分支不得单独激活")
                    changed = True
                    break
        evaluation["eligible"] = [
            key for key in evaluation["order"] if evaluation["tables"][key]["eligible"]]
        evaluation["blocked"] = [
            key for key in evaluation["order"] if not evaluation["tables"][key]["eligible"]]

    @staticmethod
    def _guard_reason(verdict: dict[str, Any], code: str, message: str) -> None:
        verdict["eligible"] = False
        if any(reason.get("code") == code for reason in verdict["reasons"]):
            return
        verdict["reasons"].append({"code": code, "message": message, "related": []})

    def _catalog(self) -> dict[str, dict[str, Any]]:
        catalog: dict[str, dict[str, Any]] = {}
        for row in store.rows(self.MODULE):
            if str(row.get("版本状态", "")) != "已注册":
                continue
            asset = str(row.get("关联资产", "")).strip()
            version = str(row.get("版本编号", "")).strip()
            fields = parse_fields(row.get("字段清单", ""))
            if not asset or not version or not fields:
                continue
            catalog[asset] = {
                "asset": asset,
                "version": version,
                "fields": fields,
                "entry_id": row.get("id"),
                "source": "schema_registry",
            }
        for row in store.rows(MANIFEST_TABLE):
            asset = str(row.get("asset", "")).strip()
            version = str(row.get("version", "")).strip()
            fields = row.get("fields") or []
            if not asset or not version or not fields:
                continue
            catalog[asset] = {
                "asset": asset,
                "version": version,
                "fields": list(fields),
                "entry_id": row.get("entry_id"),
                "source": row.get("source", "结构发布批次"),
            }
        return catalog

    def _published_usage(self) -> dict[str, list[dict[str, Any]]]:
        usage: dict[str, list[dict[str, Any]]] = {}
        for row in store.rows(USAGE_TABLE):
            downstream = str(row.get("downstream", "")).strip()
            upstream = str(row.get("upstream", "")).strip()
            required = row.get("required") or []
            if not downstream or not upstream or not required:
                continue
            usage.setdefault(downstream, []).append({
                "upstream": upstream,
                "version": row.get("version"),
                "required": list(required),
            })
        return usage

    def _confirmations(self) -> set[tuple[str, str, str]]:
        confirmations: set[tuple[str, str, str]] = set()
        for row in store.rows(CONFIRMATION_TABLE):
            downstream = str(row.get("downstream", ""))
            upstream = str(row.get("upstream", ""))
            version = str(row.get("version") or "")
            confirmations.add((downstream, upstream, version))
            confirmations.add((downstream, upstream, ""))
        return confirmations

    def _table_verdicts(self, evaluation: dict[str, Any]) -> list[dict[str, Any]]:
        return [evaluation["tables"][key] for key in evaluation["order"]]

    @staticmethod
    def _verdict_asset(evaluation: dict[str, Any], key: str) -> str:
        return str(evaluation["tables"][key]["asset"])

    # ---------- 发布与快照 ----------

    def _publish_table(self, table: dict[str, Any], batch_id: str,
                       now: datetime) -> dict[str, Any]:
        asset = table["asset"]
        version = table["version"]
        fields = list(table["fields"])

        self._upsert_manifest(asset, version, fields, table.get("entry_id"), "结构发布批次")
        self._write_schema_record(asset, {
            "版本编号": version,
            "字段清单": format_fields(fields),
            "前向兼容": "是",
            "后向兼容": "是",
            "Schema摘要": f"{asset}@{version} 结构发布",
        })
        if table.get("entry_id") is not None:
            entry = store.find(self.MODULE, int(table["entry_id"]))
            if entry is not None:
                entry["版本状态"] = "已注册"
                entry["status"] = "已注册"
                entry["pending"] = False
                entry["注册时间"] = now.strftime("%Y-%m-%d")

        snap_rows = store.rows("version_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snap_rows), default=0) + 1
        snapshot_row = {
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"VERS-{next_id:04d}",
            "关联资产": asset,
            "快照时间": now.strftime("%Y-%m-%d"),
            "数据量": "0MB",
            "快照类型": SNAPSHOT_TYPE,
            "保留天数": "90",
            "依赖快照": batch_id,
            "快照状态": "已完成",
            "来源版本": version,
            "字段依据": format_fields(fields),
            "依赖关系": ",".join(
                dep.get("upstream", "") + (f"@{dep.get('version')}" if dep.get("version") else "")
                for dep in (table.get("dependencies") or [])),
            "结构发布批次": batch_id,
        }
        snap_rows.append(snapshot_row)

        record = {
            "快照编号": snapshot_row["快照编号"],
            "关联资产": asset,
            "来源版本": version,
            "来源类型": table.get("source_kind"),
            "来源记录": table.get("entry_id"),
            "字段依据": fields,
            "依赖关系": table.get("dependencies") or [],
            "结构发布批次": batch_id,
        }
        self._write_snapshot_record(record, now)
        return record

    def _record_published_usage(self, table: dict[str, Any], batch_id: str) -> None:
        rows = store.rows(USAGE_TABLE)
        for dependency in table.get("dependencies") or []:
            required = dependency.get("required") or []
            if not required:
                continue
            signature = self._usage_signature(
                table["asset"], dependency.get("upstream"),
                dependency.get("version"), required)
            if any(row.get("signature") == signature for row in rows):
                continue
            rows.append({
                "id": len(rows) + 1,
                "signature": signature,
                "downstream": table["asset"],
                "upstream": dependency.get("upstream"),
                "version": dependency.get("version"),
                "required": list(required),
                "batch": batch_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

    @staticmethod
    def _usage_signature(downstream: str, upstream: Any, version: Any,
                         required: list[dict[str, Any]]) -> str:
        return json.dumps(
            [downstream, upstream, version,
             sorted((str(field.get("name")), str(field.get("type") or "")) for field in required)],
            ensure_ascii=False, default=str)

    def _add_confirmation(self, downstream: str, upstream: str,
                          version: str | None, fields: list[str]) -> None:
        rows = store.rows(CONFIRMATION_TABLE)
        for row in rows:
            if (row.get("downstream"), row.get("upstream"),
                    row.get("version") or "") == (downstream, upstream, version or ""):
                row["fields"] = sorted(set(list(row.get("fields") or []) + fields))
                row["confirmed_at"] = datetime.now(timezone.utc).isoformat()
                return
        rows.append({
            "id": len(rows) + 1,
            "downstream": downstream,
            "upstream": upstream,
            "version": version or "",
            "fields": sorted(set(fields)),
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        })

    def _upsert_manifest(self, asset: str, version: str,
                         fields: list[dict[str, Any]], entry_id: Any, source: str) -> None:
        rows = store.rows(MANIFEST_TABLE)
        for row in rows:
            if str(row.get("asset")) == asset:
                row.update({
                    "version": version,
                    "fields": list(fields),
                    "entry_id": entry_id,
                    "source": source,
                    "registered_at": datetime.now(timezone.utc).isoformat(),
                })
                break
        else:
            rows.append({
                "id": len(rows) + 1,
                "asset": asset,
                "version": version,
                "fields": list(fields),
                "entry_id": entry_id,
                "source": source,
                "registered_at": datetime.now(timezone.utc).isoformat(),
            })
        self._write_manifest_file(rows)

    # ---------- 幂等 ----------

    def _find_identical_batch(self, normalized: dict[str, Any],
                              evaluation: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """重复提交识别。

        - 显式批次键：同一批次键只允许一条发布记录；
        - 未给批次键：提交内容指纹相同，且已有批次中没有被阻断的表，
          才视为同一次发布的重放。已经排除过的表再次提交是有意义的
          （例如补齐上游字段后），必须生成新批次。
        """
        explicit = normalized.get("batch_key")
        if explicit:
            for row in store.rows(BATCH_TABLE):
                if str(row.get("批次键")) == str(explicit):
                    return row.get("记录", row)
            return None
        has_blocked = evaluation is not None and bool(evaluation.get("blocked"))
        for row in store.rows(BATCH_TABLE):
            if row.get("内容指纹") != normalized.get("fingerprint"):
                continue
            if has_blocked:
                continue
            record = row.get("记录", row)
            if record.get("排除表数"):
                continue
            return record
        return None

    # ---------- 批次产物 ----------

    def _submission_digest(self, normalized: dict[str, Any]) -> list[dict[str, Any]]:
        digest: list[dict[str, Any]] = []
        for index, table in enumerate(normalized.get("tables", [])):
            digest.append({
                "index": index,
                "asset": table.get("asset"),
                "entry_id": table.get("entry_id"),
                "version": table.get("version"),
                "fields": table.get("fields") if table.get("fields_provided") else None,
                "dependencies": table.get("dependencies") or [],
            })
        return digest

    def _write_batch_snapshot(self, batch_id: str, batch_no: int,
                              normalized: dict[str, Any], evaluation: dict[str, Any],
                              published: list[dict[str, Any]],
                              now: datetime) -> dict[str, Any]:
        snapshots = [
            {
                "批次编号": batch_id,
                "快照编号": record["快照编号"],
                "关联资产": record["关联资产"],
                "来源版本": record["来源版本"],
                "字段依据": record["字段依据"],
                "依赖关系": record["依赖关系"],
                "快照时间": now.isoformat(),
            }
            for record in published
        ]
        excluded = [
            {
                "关联资产": evaluation["tables"][key]["asset"],
                "来源记录": evaluation["tables"][key]["entry_id"],
                "来源版本": evaluation["tables"][key]["version"],
                "原因": evaluation["tables"][key]["reasons"],
            }
            for key in evaluation["order"]
            if not evaluation["tables"][key]["eligible"]
        ]
        artifact = {
            "batch_id": batch_id,
            "batch_no": batch_no,
            "submitted_at": now.isoformat(),
            "tables": snapshots,
            "excluded_tables": excluded,
            "submission": self._submission_digest(normalized),
        }
        path = os.path.join(PUBLICATION_DIR, f"batch_snapshot_{batch_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False, indent=2, default=str)
        return {
            "批次编号": batch_id,
            "快照文件": f"batch_snapshot_{batch_id}.json",
            "快照": snapshots,
            "条目数": len(snapshots),
        }

    def _write_publication_file(self, batch_id: str, record: dict[str, Any]) -> None:
        path = os.path.join(PUBLICATION_DIR, f"publication_{batch_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _write_manifest_file(self, rows: list[dict[str, Any]]) -> None:
        path = os.path.join(PUBLICATION_DIR, "schema_versions.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2, default=str)

    def _write_snapshot_record(self, record: dict[str, Any], now: datetime) -> None:
        filename = f"snapshot_{record['关联资产']}_{record['快照编号']}.json"
        path = os.path.join(SNAPSHOT_DIR, filename)
        payload = {
            "snapshot_id": record["快照编号"],
            "asset_id": record["关联资产"],
            "snapshot_time": now.strftime("%Y-%m-%d"),
            "data_volume": "0MB",
            "snapshot_type": SNAPSHOT_TYPE,
            "retention_days": "90",
            "dependent_snapshot": record["结构发布批次"],
            "status": "已完成",
            "source_version": record["来源版本"],
            "source_kind": record["来源类型"],
            "source_entry_id": record["来源记录"],
            "fields": record["字段依据"],
            "dependencies": record["依赖关系"],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    # ---------- 既有单条产物 ----------

    def _write_schema_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"schema_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "schema_id": str(entry.get("版本编号", "")),
            "asset_id": asset_id,
            "fields": str(entry.get("字段清单", "")),
            "forward_compatible": str(entry.get("前向兼容", "")),
            "backward_compatible": str(entry.get("后向兼容", "")),
            "summary": str(entry.get("Schema摘要", "")),
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "version_status": "已注册",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _create_version_snapshot(self, asset_id: str, schema_entry: dict[str, Any]) -> None:
        snap_rows = store.rows("version_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snap_rows), default=0) + 1
        snap = {
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"VERS-{next_id:04d}",
            "关联资产": asset_id,
            "快照时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "数据量": "0MB",
            "快照类型": "Schema快照",
            "保留天数": "90",
            "依赖快照": "",
            "快照状态": "已完成",
        }
        snap_rows.append(snap)

    def _write_audit_entry(self, entity: str, action: str, target_id: str,
                           detail: str) -> None:
        audit_rows = store.rows("audit_log")
        next_id = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        audit_rows.append({
            "id": next_id,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "记录编号": f"AUDI-{next_id:04d}",
            "操作主体": entity,
            "操作类型": action,
            "操作对象": target_id,
            "操作时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "影响范围": detail,
            "审计摘要": detail,
            "归档状态": "已记录",
        })
