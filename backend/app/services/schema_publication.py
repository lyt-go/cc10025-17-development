"""结构发布批次的提交解析：把多选提交的表、字段清单和依赖版本归一化。

这里的函数保持纯计算，不读写仓库和文件；真正的发布、快照和审计留在
``SchemaRegistryService`` 里，避免路由层出现业务判断。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

TABLE_KEYS = ("tables", "items", "entries", "records", "schemas", "表清单", "数据表清单", "表列表")
BATCH_KEY_KEYS = ("batch_key", "batchKey", "submission_id", "submissionId", "idempotency_key",
                  "幂等键", "提交编号", "批次键")
EDGE_KEYS = ("dependencies", "edges", "依赖关系", "依赖列表", "depends_on")

ASSET_KEYS = ("asset", "asset_id", "assetId", "table", "table_id", "tableId",
              "关联资产", "数据表", "表名")
ENTRY_ID_KEYS = ("entry_id", "entryId", "记录id", "记录ID")
VERSION_KEYS = ("version", "version_id", "versionId", "schema_version", "版本编号",
                "来源版本", "source_version", "base_version", "依赖版本")
FIELDS_KEYS = ("fields", "columns", "field_list", "字段清单", "字段列表")
DEPENDS_KEYS = ("depends_on", "dependsOn", "upstreams", "upstream_assets", "upstream",
                "依赖", "上游依赖", "上游数据表")
REQUIRES_KEYS = ("requires", "required_fields", "requiredFields", "upstream_fields",
                 "所需上游字段", "依赖字段")
CONFIRMED_KEYS = ("confirmed", "is_confirmed", "确认", "确认状态")
UPSTREAM_KEYS = ("upstream", "upstream_asset", "from", "source", "上游", "上游资产", "上游数据表")
DOWNSTREAM_KEYS = ("downstream", "downstream_asset", "to", "target", "下游", "下游资产",
                   "下游数据表")

_FIELD_NAME_KEYS = ("name", "field", "field_name", "column", "column_name", "字段", "字段名")
_FIELD_TYPE_KEYS = ("type", "data_type", "dtype", "类型", "字段类型")

_SPLIT_RE = re.compile(r"[,，;；、\n\r|]+")
_ANNOTATION_RE = re.compile(r"^\s*([^:：()（）]+?)\s*[:：]\s*([^()（）]+?)\s*$")
_PAREN_TYPE_RE = re.compile(r"^\s*([^()（）]+?)\s*[（(]\s*([^()（）]+?)\s*[)）]\s*$")


def _first(mapping: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, "", [], {}):
            return mapping[key]
    return default


def _as_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in ("true", "1", "yes", "y", "已确认", "确认", "confirmed"):
        return True
    if text in ("false", "0", "no", "n", "未确认", "待确认", "unconfirmed"):
        return False
    return None


def _split_assets(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_split_assets(item))
        return result
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in _SPLIT_RE.split(text) if part.strip()]


def _field_entry(name: Any, field_type: Any = None) -> dict[str, Any] | None:
    text = str(name).strip() if name is not None else ""
    if not text:
        return None
    annotation = str(field_type).strip() if field_type not in (None, "") else ""
    match = _ANNOTATION_RE.match(text)
    if match and not annotation:
        text, annotation = match.group(1).strip(), match.group(2).strip()
    else:
        match = _PAREN_TYPE_RE.match(text)
        if match and not annotation:
            text, annotation = match.group(1).strip(), match.group(2).strip()
    if not text:
        return None
    return {"name": text, "type": annotation or None}


def parse_fields(value: Any) -> list[dict[str, Any]]:
    """把字段清单解析成 ``[{name, type}]``，兼容字符串、列表和键值映射。"""
    if value is None:
        return []
    if isinstance(value, dict):
        if _first(value, _FIELD_NAME_KEYS) is not None:
            entry = _field_entry(_first(value, _FIELD_NAME_KEYS), _first(value, _FIELD_TYPE_KEYS))
            return [entry] if entry else []
        result: list[dict[str, Any]] = []
        for name, field_type in value.items():
            entry = _field_entry(name, field_type if isinstance(field_type, str) else None)
            if entry:
                result.append(entry)
        return _dedupe_fields(result)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        parts = [part.strip() for part in _SPLIT_RE.split(text) if part.strip()]
        if len(parts) == 1:
            parts = [part for part in text.split() if part]
        return _dedupe_fields([entry for part in parts if (entry := _field_entry(part))])
    if isinstance(value, (list, tuple, set)):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(parse_fields(item))
        return _dedupe_fields(result)
    entry = _field_entry(value)
    return [entry] if entry else []


def _dedupe_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for field in fields:
        key = str(field.get("name", "")).casefold()
        if not key:
            continue
        if key in index:
            existing = result[index[key]]
            if not existing.get("type") and field.get("type"):
                existing["type"] = field["type"]
            continue
        index[key] = len(result)
        result.append({"name": field["name"], "type": field.get("type")})
    return result


def parse_requirements(value: Any) -> list[dict[str, Any]]:
    """解析下游声明的上游字段需求，支持 ``name`` / ``name:type`` / 映射。"""
    if value is None:
        return []
    if isinstance(value, dict):
        if _first(value, _FIELD_NAME_KEYS) is not None:
            name = _first(value, _FIELD_NAME_KEYS)
            entry = _field_entry(name, _first(value, _FIELD_TYPE_KEYS))
            if not entry:
                return []
            entry["confirmed"] = _as_bool(_first(value, CONFIRMED_KEYS))
            return [entry]
        result: list[dict[str, Any]] = []
        for name, spec in value.items():
            if isinstance(spec, dict):
                entry = _field_entry(name, _first(spec, _FIELD_TYPE_KEYS))
                if entry:
                    entry["confirmed"] = _as_bool(_first(spec, CONFIRMED_KEYS))
                    result.append(entry)
            else:
                entry = _field_entry(name, spec if isinstance(spec, str) else None)
                if entry:
                    entry["confirmed"] = None
                    result.append(entry)
        return _dedupe_requirements(result)
    if isinstance(value, (list, tuple, set)):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(parse_requirements(item))
        return _dedupe_requirements(result)
    text = str(value).strip()
    if not text:
        return []
    parts = [part.strip() for part in _SPLIT_RE.split(text) if part.strip()]
    if len(parts) == 1:
        parts = [part for part in text.split() if part]
    return _dedupe_requirements([entry for part in parts if (entry := _field_entry(part))])


def _dedupe_requirements(requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for requirement in requirements:
        key = str(requirement.get("name", "")).casefold()
        if not key:
            continue
        if key in index:
            existing = result[index[key]]
            if not existing.get("type") and requirement.get("type"):
                existing["type"] = requirement["type"]
            if existing.get("confirmed") is None and requirement.get("confirmed") is not None:
                existing["confirmed"] = requirement["confirmed"]
            continue
        index[key] = len(result)
        result.append(requirement)
    return result


def parse_requires(value: Any) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], bool]:
    """返回 (按上游分组的字段需求, 无上游归属的需求, 是否按上游分组)。"""
    if value is None:
        return {}, [], False
    if isinstance(value, dict):
        if _first(value, _FIELD_NAME_KEYS) is not None:
            return {}, parse_requirements(value), False
        mapping: dict[str, list[dict[str, Any]]] = {}
        for upstream, spec in value.items():
            if isinstance(spec, (list, tuple, set, str)) or spec is None:
                mapping[str(upstream).strip()] = parse_requirements(spec)
            elif isinstance(spec, dict):
                mapping[str(upstream).strip()] = parse_requirements(
                    _first(spec, REQUIRES_KEYS, _first(spec, FIELDS_KEYS, spec)))
        return mapping, [], True
    if isinstance(value, (list, tuple, set)):
        grouped: dict[str, list[dict[str, Any]]] = {}
        loose: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict) and _first(item, UPSTREAM_KEYS) is not None:
                upstream = str(_first(item, UPSTREAM_KEYS)).strip()
                grouped.setdefault(upstream, []).extend(
                    parse_requirements(_first(item, REQUIRES_KEYS, _first(item, FIELDS_KEYS))))
                continue
            if isinstance(item, str) and "." in item:
                upstream, _, field_text = item.partition(".")
                if upstream.strip() and field_text.strip():
                    grouped.setdefault(upstream.strip(), []).extend(parse_requirements(field_text))
                    continue
            loose.extend(parse_requirements(item))
        return grouped, loose, bool(grouped)
    return {}, parse_requirements(value), False


def _merge_dependency(dependencies: dict[str, dict[str, Any]], upstream: str,
                      version: Any = None, required: Any = None,
                      confirmed: Any = None) -> None:
    upstream = str(upstream).strip()
    if not upstream:
        return
    current = dependencies.setdefault(upstream, {
        "upstream": upstream, "version": None, "required": [], "confirmed": None})
    _merge_into_dependency(current, version, required, confirmed)


def _merge_into_dependency(current: dict[str, Any], version: Any = None,
                           required: Any = None, confirmed: Any = None) -> None:
    if version not in (None, ""):
        current["version"] = str(version)
    if required:
        current["required"] = _dedupe_requirements(
            list(current.get("required") or []) + parse_requirements(required))
    parsed_confirmed = _as_bool(confirmed)
    if parsed_confirmed is not None:
        current["confirmed"] = parsed_confirmed


def _single_dependency(raw: Any) -> tuple[str, Any, Any, Any] | None:
    if isinstance(raw, str):
        return raw, None, None, None
    if not isinstance(raw, dict):
        return None
    upstream = _first(raw, UPSTREAM_KEYS, _first(raw, ASSET_KEYS))
    if upstream in (None, ""):
        return None
    return (str(upstream).strip(), _first(raw, VERSION_KEYS),
            _first(raw, REQUIRES_KEYS, _first(raw, FIELDS_KEYS)),
            _first(raw, CONFIRMED_KEYS))


def parse_depends_on(value: Any) -> dict[str, dict[str, Any]]:
    """解析单表声明依赖，返回 ``{upstream: {version, required, confirmed}}``。"""
    dependencies: dict[str, dict[str, Any]] = {}
    if value is None:
        return dependencies
    if isinstance(value, str):
        for upstream in _split_assets(value):
            _merge_dependency(dependencies, upstream)
        return dependencies
    if isinstance(value, dict):
        single = _single_dependency(value)
        if single is not None:
            _merge_dependency(dependencies, *single)
            return dependencies
        for upstream, spec in value.items():
            if isinstance(spec, dict):
                _merge_dependency(dependencies, upstream, _first(spec, VERSION_KEYS),
                                  _first(spec, REQUIRES_KEYS, _first(spec, FIELDS_KEYS)),
                                  _first(spec, CONFIRMED_KEYS))
            else:
                _merge_dependency(dependencies, upstream, None, spec, None)
        return dependencies
    if isinstance(value, (list, tuple, set)):
        for item in value:
            single = _single_dependency(item)
            if single is not None:
                _merge_dependency(dependencies, *single)
            elif isinstance(item, str):
                for upstream in _split_assets(item):
                    _merge_dependency(dependencies, upstream)
        return dependencies
    return dependencies


def _table_entry_id(raw: dict[str, Any]) -> int | None:
    explicit = _first(raw, ENTRY_ID_KEYS)
    if explicit is None and "id" in raw:
        value = raw["id"]
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            explicit = value
    if explicit is None:
        return None
    try:
        return int(explicit)
    except (TypeError, ValueError):
        return None


def _normalize_table(raw: Any, index: int) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = {"asset": raw}
    if not isinstance(raw, dict):
        raw = {}
    asset = str(_first(raw, ASSET_KEYS, "")).strip()
    fields_value = _first(raw, FIELDS_KEYS)
    fields_provided = fields_value is not None
    version = _first(raw, VERSION_KEYS)
    dependencies = parse_depends_on(_first(raw, DEPENDS_KEYS))
    grouped, loose, _ = parse_requires(_first(raw, REQUIRES_KEYS))
    for upstream, required in grouped.items():
        _merge_dependency(dependencies, upstream, None, required, None)
    if loose:
        if len(dependencies) == 1:
            only = next(iter(dependencies))
            _merge_dependency(dependencies, only, None, loose, None)
        else:
            for dependency in dependencies.values():
                dependency["required"] = _dedupe_requirements(
                    list(dependency["required"]) + loose)
                break
    return {
        "index": index,
        "asset": asset,
        "entry_id": _table_entry_id(raw),
        "version": str(version).strip() if version not in (None, "") else None,
        "fields": parse_fields(fields_value) if fields_provided else None,
        "fields_provided": fields_provided,
        "dependencies": list(dependencies.values()),
    }


def _parse_edges(value: Any) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    if value is None:
        return edges
    if isinstance(value, dict):
        for downstream, upstreams in value.items():
            if isinstance(upstreams, dict) and _first(upstreams, UPSTREAM_KEYS) is not None:
                upstreams = [upstreams]
            if isinstance(upstreams, (list, tuple, set)):
                for item in upstreams:
                    if isinstance(item, dict):
                        merged = dict(item)
                        merged.setdefault("upstream", _first(item, UPSTREAM_KEYS))
                        merged["downstream"] = downstream
                        edges.append(merged)
                    else:
                        edges.append({"upstream": str(item), "downstream": downstream})
            else:
                edges.append({"upstream": str(upstreams), "downstream": downstream})
        return edges
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, dict):
                edges.append(item)
    return edges


def normalize_submission(payload: dict[str, Any]) -> dict[str, Any]:
    """把外部提交归一化为引擎可消费的结构。"""
    if not isinstance(payload, dict):
        payload = {}
    tables_value = _first(payload, TABLE_KEYS, [])
    if isinstance(tables_value, dict):
        tables_value = list(tables_value.values())
    if not isinstance(tables_value, (list, tuple)):
        tables_value = []
    tables = [_normalize_table(raw, index) for index, raw in enumerate(tables_value)]

    warnings: list[str] = []
    by_asset = {table["asset"]: table for table in tables if table["asset"]}
    for edge in _parse_edges(_first(payload, EDGE_KEYS)):
        upstream = _first(edge, UPSTREAM_KEYS)
        downstream = _first(edge, DOWNSTREAM_KEYS)
        if upstream in (None, "") or downstream in (None, ""):
            continue
        downstream = str(downstream).strip()
        target = by_asset.get(downstream)
        if target is None:
            warnings.append(f"依赖关系 {upstream}→{downstream} 的下游不在本次提交中，已忽略")
            continue
        version = _first(edge, VERSION_KEYS)
        required = _first(edge, REQUIRES_KEYS, _first(edge, FIELDS_KEYS))
        confirmed = _first(edge, CONFIRMED_KEYS)
        existing = next((dep for dep in target["dependencies"]
                         if dep["upstream"] == str(upstream).strip()), None)
        if existing is None:
            existing = {"upstream": str(upstream).strip(), "version": None,
                        "required": [], "confirmed": None}
            target["dependencies"].append(existing)
        _merge_into_dependency(existing, version, required, confirmed)

    batch_key = _first(payload, BATCH_KEY_KEYS)
    normalized = {
        "batch_key": str(batch_key).strip() if batch_key not in (None, "") else None,
        "tables": tables,
        "warnings": warnings,
    }
    normalized["fingerprint"] = submission_fingerprint(normalized)
    return normalized


def submission_fingerprint(normalized: dict[str, Any]) -> str:
    """对提交内容做稳定指纹，用于同一次发布的幂等去重。"""
    canonical = {
        "tables": normalized.get("tables", []),
        "warnings": normalized.get("warnings", []),
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def field_names(value: Any) -> list[str]:
    return [field["name"] for field in parse_fields(value)]


def format_fields(fields: list[dict[str, Any]]) -> str:
    return ",".join(
        f"{field['name']}:{field['type']}" if field.get("type") else str(field["name"])
        for field in fields)
