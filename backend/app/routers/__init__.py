"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, schema_publication

# 固定路径的结构发布接口排在通用资源路由之前。
ROUTERS = [schema_publication, resource]
