"""结构发布批次接口：多选数据表的预览、发布、批次回查和上游字段确认。

固定路径必须排在通用资源路由 ``/api/{module}`` 之前注册，避免被动态路径抢占。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.services import service_for

router = APIRouter(prefix="/api", tags=["Schema结构发布"])


def _service():
    service = service_for("schema_registry")
    if service is None:
        raise HTTPException(status_code=404, detail="Schema注册服务未加载")
    return service


@router.get("/schema-publications/catalog")
def version_catalog() -> dict[str, Any]:
    service = _service()
    return {
        "catalog": service.version_catalog(),
        "field_availability": service.field_availability(),
    }


@router.get("/schema-publications")
def list_publications() -> dict[str, Any]:
    items = _service().list_publications()
    return {"items": items, "total": len(items)}


@router.get("/schema-publications/{batch_ref}")
def get_publication(batch_ref: str) -> dict[str, Any]:
    record = _service().get_publication(batch_ref)
    if record is None:
        raise HTTPException(status_code=404, detail=f"结构发布批次 {batch_ref} 不存在")
    return record


@router.post("/schema-publications/preview")
def preview_publication(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return _service().preview_publication(payload)


@router.post("/schema-publications/publish")
def publish_publication(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return _service().publish_publication(payload)


@router.post("/schema-publications/confirm")
def confirm_upstream_fields(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return _service().confirm_upstream_fields(payload)
