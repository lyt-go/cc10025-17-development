# 数据资产与生命周期治理平台

面向数据资产编目、冷热分层、生命周期管理、血缘追踪与合规治理的统一数据治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 资产编目 | `asset_catalog` | 数据资产 | 资产编号、资产名称、所属域 |
| 分层策略 | `tier_policy` | 分层策略 | 策略编号、目标资产、当前层级 |
| 血缘图谱 | `lineage_graph` | 血缘记录 | 记录编号、上游资产、下游资产 |
| 保留规则 | `retention_rule` | 保留规则 | 规则编号、目标资产、保留天数 |
| 合规检查 | `compliance_check` | 合规规则 | 规则编号、检查维度、合规标准 |
| 冷存储管理 | `cold_storage` | 冷存任务 | 任务编号、源资产、目标存储 |
| 预热回迁 | `hot_prefetch` | 回迁任务 | 任务编号、冷存位置、目标分区 |
| 版本快照 | `version_snapshot` | 数据快照 | 快照编号、关联资产、快照时间 |
| 标签体系 | `label_tag` | 数据标签 | 标签编号、标签名称、标签类型 |
| 审计日志 | `audit_log` | 审计记录 | 记录编号、操作主体、操作类型 |
| 访问网关 | `access_gate` | 访问策略 | 策略编号、目标资产、授权主体 |
| 成本追踪 | `cost_tracker` | 成本记录 | 记录编号、关联资产、存储成本 |
| Schema注册 | `schema_registry` | Schema版本 | 版本编号、关联资产、字段清单 |
| 分区规划 | `partition_plan` | 分区方案 | 方案编号、关联资产、分区键 |
| 补数任务 | `backfill_job` | 补数作业 | 作业编号、目标资产、补数区间 |
| 质量报告 | `quality_report` | 质量报告 | 报告编号、关联资产、检核时间 |
| 回收清理 | `reclaim_job` | 回收任务 | 任务编号、目标资产、回收范围 |
| 变更通知 | `change_notice` | 变更通知 | 通知编号、关联资产、变更类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## Schema 结构发布批次

Schema 注册除逐条「确认注册」外，支持多选后整组发布，接口在
`backend/app/routers/schema_publication.py`（固定路径，注册在 `/api/{module}` 之前）：

| 接口 | 说明 |
| --- | --- |
| `POST /api/schema-publications/preview` | 只评估不落库：逐表返回发布资格、字段依据、依赖和阻断原因 |
| `POST /api/schema-publications/publish` | 写入结构发布批次：发布合格表、生成批次快照、记录排除表及原因 |
| `GET /api/schema-publications` | 批次列表 |
| `GET /api/schema-publications/{批次编号或批次键}` | 批次回查（快照集合、字段可用范围、未发布表原因） |
| `POST /api/schema-publications/confirm` | 确认下游对上游字段的依赖，解除该分支的确认门槛 |
| `GET /api/schema-publications/catalog` | 已注册版本目录与当前字段可用范围 |

提交体（`tables` 也接受 `entry_ids`，按已注册记录和血缘补全依赖）：

```json
{
  "batch_key": "PUB-20261122-01",
  "tables": [
    {"entry_id": 1, "asset": "T_DWD_ORDER", "version": "v4",
     "fields": ["order_id:bigint", "user_id:bigint"]},
    {"entry_id": 2, "asset": "T_DWS_USER", "version": "v2",
     "fields": ["user_id:bigint", "cnt:bigint"],
     "depends_on": {"T_DWD_ORDER": {"version": "v4",
                                     "requires": ["user_id:bigint"],
                                     "confirmed": true}}}
  ]
}
```

规则：

- 字段清单与依赖版本是快照依据；每张已发布表在 `version_snapshot` 和
  `data/snapshots/` 里都能回查到真实来源版本（`来源版本` / `source_version`）。
- 上游字段缺失或类型与下游要求不一致时，只有受影响的分支被阻断，
  独立分支仍可发布；同批下游依赖了未进入发布的上游时也不得单独激活。
- 下游声明了 `requires` 却没有 `confirmed: true`（或未经
  `/confirm` 确认）时，该分支不得激活。
- 同一次发布重复提交按批次键幂等；未给批次键时按提交内容指纹幂等，
  已排除过表的批次不会复用，补齐后重新提交会生成新批次。
- 批次记录写在 `data/publications/publication_<批次编号>.json`，批次快照写在
  `data/publications/batch_snapshot_<批次编号>.json`，同时汇总到模块 `schema_publication_batch`。

前端在「Schema注册」页面底部提供多选、预览、批量发布、批次结果与字段确认操作。

验收：`cd backend && .venv/bin/python test_schema_publication.py`（结构发布批次）
和 `.venv/bin/python test_cross_module.py`（既有跨模块链路）。

领域：数据工程
子方向：数仓与数据湖
