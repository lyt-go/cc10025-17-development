"""结构发布批次评估：判定每张表能否进入发布，并给出阻断原因。

输入是 ``normalize_submission`` 归一化后的提交、已注册版本目录和已发布下游
usage；输出是逐表结论。这里不写仓库、不改状态，发布动作由 SchemaRegistryService
在拿到结论后执行。
"""
from __future__ import annotations

from typing import Any

_TYPE_ALIASES = {
    "int": "int", "integer": "int", "bigint": "int", "smallint": "int", "tinyint": "int",
    "int32": "int", "int64": "int", "long": "int", "short": "int",
    "decimal": "decimal", "numeric": "decimal", "number": "decimal", "float": "decimal",
    "double": "decimal", "real": "decimal", "money": "decimal",
    "string": "string", "varchar": "string", "char": "string", "text": "string",
    "nvarchar": "string", "clob": "string", "uuid": "string",
    "bool": "bool", "boolean": "bool",
    "date": "date", "datetime": "datetime", "timestamp": "datetime", "time": "time",
    "json": "json", "jsonb": "json", "array": "array", "list": "array",
    "binary": "binary", "blob": "binary", "bytes": "binary",
}


def normalize_type(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().casefold()
    # 去掉精度/长度，例如 decimal(18,2) / varchar(64)
    base = text.split("(", 1)[0].strip()
    return _TYPE_ALIASES.get(base, base) or None


def fields_by_name(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(field.get("name", "")).casefold(): field for field in fields
            if field.get("name")}


def _field_names(fields: list[dict[str, Any]]) -> list[str]:
    return [str(field.get("name", "")) for field in fields if field.get("name")]


def _reason(code: str, message: str, related: list[str] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "related": related or []}


def _add_reason(reasons: dict[str, list[dict[str, Any]]], key: str,
                code: str, message: str, related: list[str] | None = None) -> None:
    bucket = reasons.setdefault(key, [])
    if any(item["code"] == code and item["message"] == message for item in bucket):
        return
    bucket.append(_reason(code, message, related))


def _upstream_lookup(batch: dict[str, dict[str, Any]], asset: str) -> dict[str, Any] | None:
    for table in batch.values():
        if str(table.get("asset", "")).casefold() == str(asset).casefold():
            return table
    return None


def _find_cycles(batch: dict[str, dict[str, Any]]) -> list[list[str]]:
    """在本次提交内部找出所有依赖环，返回每个环上的表名列表。"""
    by_folded = {str(asset).casefold(): asset for asset in batch}
    edges: dict[str, list[str]] = {}
    for asset, table in batch.items():
        targets: list[str] = []
        for dependency in table.get("dependencies") or []:
            upstream = str(dependency.get("upstream", "")).strip()
            resolved = by_folded.get(upstream.casefold())
            if resolved and resolved != asset:
                targets.append(resolved)
        edges[asset] = targets

    cycles: dict[tuple[str, ...], list[str]] = {}

    def walk(start: str, node: str, path: list[str], visiting: set[str]) -> None:
        for nxt in edges.get(node, []):
            if nxt == start:
                cycle = path[path.index(start):] if start in path else [start]
                key = tuple(sorted(cycle))
                cycles.setdefault(key, cycle)
            elif nxt not in visiting and len(path) < len(edges):
                walk(start, nxt, path + [nxt], visiting | {nxt})

    for asset in edges:
        walk(asset, asset, [asset], {asset})
    return list(cycles.values())


def evaluate_submission(
    normalized: dict[str, Any],
    *,
    catalog: dict[str, dict[str, Any]],
    published_usage: dict[str, list[dict[str, Any]]] | None = None,
    confirmations: set[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    """评估一次结构发布提交。

    参数：
        catalog: 已注册版本目录 ``{asset: {version, fields, entry_id}}``。
        published_usage: 已发布下游声明 ``{downstream: [{upstream, version, required}]}``。
        confirmations: 已确认的上游字段声明 ``{(downstream, upstream, version)}``。
    """
    published_usage = published_usage or {}
    confirmations = confirmations or set()

    tables = normalized.get("tables", [])
    reasons: dict[str, list[dict[str, Any]]] = {}
    outcome: dict[str, dict[str, Any]] = {}
    batch_by_asset: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()

    for table in tables:
        asset = str(table.get("asset", "")).strip()
        key = asset or f"#{table.get('index', 0)}"
        if not asset:
            table["_key"] = key
            _add_reason(reasons, key, "invalid_asset", "缺少数据表标识（关联资产）")
            continue
        if asset in batch_by_asset:
            duplicates.add(asset)
            table["_key"] = f"{asset}#{table.get('index', 0)}"
            _add_reason(reasons, table["_key"], "duplicate_asset",
                        f"本次提交重复包含数据表 {asset}，只能保留一条发布记录")
            continue
        table["_key"] = key
        batch_by_asset[asset] = table

    catalog_by_asset = {str(asset).casefold(): record for asset, record in catalog.items()}

    def catalog_for(asset: str) -> dict[str, Any] | None:
        record = catalog_by_asset.get(str(asset).casefold())
        if record is None:
            return None
        return record

    # 第一轮：解析每张表自身的字段、版本和来源。
    for table in tables:
        key = table.get("_key") or f"#{table.get('index', 0)}"
        asset = str(table.get("asset", "")).strip()
        record = catalog_for(asset) if asset else None
        submitted_fields = table.get("fields")
        if table.get("fields_provided") and submitted_fields is not None:
            fields = list(submitted_fields)
            source_kind = "本次提交字段清单"
        elif record and record.get("fields"):
            fields = list(record["fields"])
            source_kind = "已注册版本"
        else:
            fields = []
            source_kind = "缺失"

        version = table.get("version")
        if not version and record:
            version = record.get("version")
        entry_id = table.get("entry_id")
        if entry_id is None and record:
            entry_id = record.get("entry_id")

        if not version:
            _add_reason(reasons, key, "missing_version",
                        f"{asset or key} 未提供版本编号，且没有可回查的已注册版本")
        if not fields:
            _add_reason(reasons, key, "missing_fields",
                        f"{asset or key} 没有字段清单，无法生成字段依据")
        if version and record and record.get("version") and str(version) != str(record["version"]):
            if table.get("fields_provided") and source_kind == "本次提交字段清单":
                source_kind = "本次提交字段清单（新版本）"
            elif not table.get("fields_provided"):
                _add_reason(reasons, key, "version_not_found",
                            f"{asset} 版本 {version} 不是已注册版本，且未随提交提供字段清单")
        table["_fields"] = fields
        table["_version"] = str(version) if version else None
        table["_entry_id"] = entry_id
        table["_source_kind"] = source_kind
        table["_source_version"] = (str(record["version"]) if record else None)

    def fields_for(asset: str) -> tuple[list[dict[str, Any]], str | None, str | None]:
        table = _upstream_lookup(batch_by_asset, asset)
        if table is not None:
            return (list(table.get("_fields") or []),
                    table.get("_version"), table.get("_source_kind"))
        record = catalog_for(asset)
        if record is not None:
            return list(record.get("fields") or []), record.get("version"), "已注册版本"
        return [], None, None

    # 第二轮：依赖版本、字段冲突和确认门槛。
    for table in tables:
        asset = str(table.get("asset", "")).strip()
        key = table.get("_key") or f"#{table.get('index', 0)}"
        if not asset:
            continue
        for dependency in table.get("dependencies") or []:
            upstream = str(dependency.get("upstream", "")).strip()
            if not upstream:
                continue
            upstream_table = _upstream_lookup(batch_by_asset, upstream)
            upstream_fields, upstream_version, upstream_source = fields_for(upstream)
            declared_version = (str(dependency["version"])
                                if dependency.get("version") not in (None, "") else None)
            confirmed = dependency.get("confirmed")
            required = list(dependency.get("required") or [])

            if not upstream_fields:
                if upstream_table is not None:
                    # 上游就在本次提交里，但自身没有可用的字段依据；
                    # 该分支留待第四轮随上游一起阻断，这里不再重复下结论。
                    continue
                _add_reason(reasons, key, "upstream_unknown",
                            f"{asset} 依赖的上游表 {upstream} 不在本次提交中，也没有已注册版本",
                            [upstream])
                continue

            effective_version = declared_version or upstream_version
            if declared_version and upstream_version and declared_version != upstream_version:
                _add_reason(reasons, key, "dependency_version_mismatch",
                            f"{asset} 声明的上游版本 {declared_version} 与可发布版本 "
                            f"{upstream_version} 不一致", [upstream])
                continue

            upstream_index = fields_by_name(upstream_fields)
            for requirement in required:
                needed = str(requirement.get("name", ""))
                upstream_field = upstream_index.get(needed.casefold())
                if upstream_field is None:
                    message = (f"{upstream} 的字段清单缺少 {asset} 所需的字段 {needed}")
                    _add_reason(reasons, key, "required_field_missing", message, [upstream])
                    _add_reason(reasons, str(upstream), "downstream_requires_missing_field",
                                f"字段 {needed} 与下游 {asset} 的使用方式冲突：上游未提供该字段",
                                [asset])
                    continue
                required_type = normalize_type(requirement.get("type"))
                upstream_type = normalize_type(upstream_field.get("type"))
                if required_type and upstream_type and required_type != upstream_type:
                    message = (f"{upstream}.{needed} 类型为 {upstream_type}，"
                               f"下游 {asset} 需要 {required_type}")
                    _add_reason(reasons, key, "required_field_type_conflict", message, [upstream])
                    _add_reason(reasons, str(upstream), "downstream_usage_conflict", message, [asset])

            if required and confirmed is not True:
                confirmation_key = (asset, upstream, effective_version or "")
                if confirmation_key not in confirmations:
                    _add_reason(
                        reasons, key, "upstream_unconfirmed",
                        f"{asset} 依赖 {upstream} 的字段 "
                        f"{','.join(_field_names(required))} 尚未确认，不得激活该分支",
                        [upstream])

    # 第三轮：已发布下游的字段可用性不能再被破坏。
    for downstream, usages in published_usage.items():
        for usage in usages:
            upstream = str(usage.get("upstream", "")).strip()
            upstream_table = _upstream_lookup(batch_by_asset, upstream)
            if upstream_table is None:
                continue
            upstream_fields, upstream_version, _ = fields_for(upstream)
            if not upstream_fields:
                continue
            upstream_index = fields_by_name(upstream_fields)
            for requirement in usage.get("required") or []:
                needed = str(requirement.get("name", ""))
                upstream_field = upstream_index.get(needed.casefold())
                if upstream_field is None:
                    _add_reason(
                        reasons, str(upstream), "published_usage_conflict",
                        f"已发布下游 {downstream} 依赖字段 {needed}，"
                        f"本次 {upstream} 字段清单不再提供该字段", [downstream])
                    continue
                required_type = normalize_type(requirement.get("type"))
                upstream_type = normalize_type(upstream_field.get("type"))
                if required_type and upstream_type and required_type != upstream_type:
                    _add_reason(
                        reasons, str(upstream), "published_usage_conflict",
                        f"已发布下游 {downstream} 需要 {needed}:{required_type}，"
                        f"本次 {upstream} 为 {needed}:{upstream_type}", [downstream])

    # 第四轮：上游分支未进入发布时，同批下游不得单独激活。
    changed = True
    while changed:
        changed = False
        for table in tables:
            key = table.get("_key") or f"#{table.get('index', 0)}"
            asset = str(table.get("asset", "")).strip()
            if not asset:
                continue
            for dependency in table.get("dependencies") or []:
                upstream = str(dependency.get("upstream", "")).strip()
                upstream_table = _upstream_lookup(batch_by_asset, upstream)
                if upstream_table is None:
                    continue
                upstream_key = upstream_table.get("_key", upstream)
                if reasons.get(upstream_key) and not reasons.get(key):
                    _add_reason(
                        reasons, key, "upstream_not_published",
                        f"{asset} 依赖的上游表 {upstream} 本次未能进入发布，分支不得单独激活",
                        [upstream])
                    changed = True

    # 第五轮：同批内部的依赖环无法确定发布顺序，环上所有表都不得发布。
    for cycle in _find_cycles(batch_by_asset):
        for asset in cycle:
            _add_reason(
                reasons, asset, "dependency_cycle",
                f"{asset} 处于依赖环 {'→'.join(cycle + [cycle[0]])} 中，"
                f"无法确定发布顺序", cycle)

    for table in tables:
        key = table.get("_key") or f"#{table.get('index', 0)}"
        asset = str(table.get("asset", "")).strip()
        table_reasons = reasons.get(key, [])
        outcome[key] = {
            "asset": asset,
            "entry_id": table.get("_entry_id"),
            "version": table.get("_version"),
            "fields": list(table.get("_fields") or []),
            "source_kind": table.get("_source_kind"),
            "source_version": table.get("_source_version"),
            "dependencies": [
                {
                    "upstream": dep.get("upstream"),
                    "version": dep.get("version"),
                    "required": list(dep.get("required") or []),
                    "confirmed": dep.get("confirmed"),
                }
                for dep in (table.get("dependencies") or [])
            ],
            "eligible": not table_reasons,
            "reasons": table_reasons,
        }

    order = [table.get("_key") or f"#{table.get('index', 0)}" for table in tables]
    for table in tables:
        table.pop("_key", None)
    return {
        "tables": outcome,
        "order": order,
        "eligible": [key for key in order if outcome[key]["eligible"]],
        "blocked": [key for key in order if not outcome[key]["eligible"]],
    }
