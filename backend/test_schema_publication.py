"""结构发布批次验收测试：多选提交、依赖冲突、确认门槛、幂等与快照回查。

运行：python3 test_schema_publication.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.services import service_for
from app.store import store

DATA_ROOT = os.path.join(os.path.dirname(__file__), "app", "data")
PUBLICATION_DIR = os.path.join(DATA_ROOT, "publications")
SNAPSHOT_DIR = os.path.join(DATA_ROOT, "snapshots")

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
        return True
    print(f"  {RED}FAIL{RESET} {desc} {detail}")
    failed += 1
    return False


def snapshots_for_batch(batch_id):
    return [row for row in store.rows("version_snapshot")
            if row.get("结构发布批次") == batch_id]


def reason_codes(verdict):
    return {reason["code"] for reason in verdict["reasons"]}


def test_batch_publish_with_shared_dependencies():
    """一组有共同依赖的表 + 一张独立表整组提交：发布前后字段范围、快照集合、逐条对应。"""
    print("\n=== 场景1: 共同依赖组 + 独立表整组发布 ===")
    svc = service_for("schema_registry")

    common_upstream = "T_DWD_COMMON"
    left_downstream = "T_DWS_LEFT"
    right_downstream = "T_DWS_RIGHT"
    independent = "T_ADS_SOLO"

    payload = {
        "batch_key": "ACC-BATCH-SHARED",
        "tables": [
            {
                "entry_id": 1001,
                "asset": common_upstream,
                "version": "v4",
                "fields": ["order_id:bigint", "user_id:bigint", "amount:decimal"],
            },
            {
                "entry_id": 1002,
                "asset": left_downstream,
                "version": "v2",
                "fields": ["order_id:bigint", "day_amount:decimal"],
                "depends_on": {
                    common_upstream: {
                        "version": "v4",
                        "requires": ["order_id:bigint"],
                        "confirmed": True,
                    }
                },
            },
            {
                "entry_id": 1003,
                "asset": right_downstream,
                "version": "v3",
                "fields": ["user_id:bigint", "user_amount:decimal"],
                "depends_on": {
                    common_upstream: {
                        "version": "v4",
                        "requires": ["user_id:bigint"],
                        "confirmed": True,
                    }
                },
            },
            {
                "entry_id": 1004,
                "asset": independent,
                "version": "v1",
                "fields": ["stat_date:date", "total:decimal"],
            },
        ],
    }

    before = svc.field_availability()
    preview = svc.preview_publication(payload)
    check("预览不落库", preview["persisted"] is False)
    check("预览不写批次记录",
          not any(row.get("批次键") == "ACC-BATCH-SHARED"
                  for row in store.rows("schema_publication_batch")))
    check("四张表全部具备发布资格", len(preview["不可发布表"]) == 0,
          f"不可发布: {preview['不可发布表']}")

    result = svc.publish_publication(payload)
    check("批次写入成功", result["ok"] and not result["replayed"], result.get("message", ""))
    batch = result["batch"]
    check("批次编号按发布顺序生成", batch["批次编号"].startswith("PUBL-"))
    check("已发布表数为 4", batch["已发布表数"] == 4, str(batch["已发布表数"]))
    check("没有排除表", batch["排除表数"] == 0)

    after = svc.field_availability()
    check("发布前共享上游字段不可用",
          common_upstream not in before.get("字段可用范围", before))
    check("发布后共享上游字段可用",
          after.get(common_upstream) == ["order_id", "user_id", "amount"],
          str(after.get(common_upstream)))
    check("发布后独立表字段可用",
          after.get(independent) == ["stat_date", "total"], str(after.get(independent)))
    check("批次记录保存发布前后字段范围",
          batch["字段可用范围"]["发布前"] == before
          and batch["字段可用范围"]["发布后"] == after)

    snapshots = batch["快照集合"]["快照"]
    check("快照集合与已发布表一一对应", len(snapshots) == 4, str(len(snapshots)))
    check("快照集合适配提交顺序",
          [item["关联资产"] for item in snapshots] ==
          [common_upstream, left_downstream, right_downstream, independent])
    snapshot_rows = snapshots_for_batch(batch["批次编号"])
    check("版本快照表写入同一批次", len(snapshot_rows) == 4, str(len(snapshot_rows)))
    check("每张表都能回查真实来源版本",
          {row["关联资产"]: row.get("来源版本") for row in snapshot_rows} == {
              common_upstream: "v4",
              left_downstream: "v2",
              right_downstream: "v3",
              independent: "v1",
          }, str({row["关联资产"]: row.get("来源版本") for row in snapshot_rows}))
    check("快照保存字段依据与依赖关系",
          all(row.get("字段依据") and row.get("依赖快照") == batch["批次编号"]
              for row in snapshot_rows))
    shared_row = next(row for row in snapshot_rows if row["关联资产"] == left_downstream)
    check("共享依赖表快照记录上游及版本",
          common_upstream in str(shared_row.get("依赖关系"))
          and "v4" in str(shared_row.get("依赖关系")),
          str(shared_row.get("依赖关系")))

    artifact = os.path.join(PUBLICATION_DIR,
                            f"batch_snapshot_{batch['批次编号']}.json")
    check("批次快照文件已写入", os.path.exists(artifact), artifact)
    if os.path.exists(artifact):
        with open(artifact, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        check("批次快照文件含已发布表与排除原因字段",
              len(saved.get("tables", [])) == 4 and "excluded_tables" in saved)
        check("批次快照文件与提交逐条对应",
              [item["asset"] for item in saved["submission"]] ==
              [common_upstream, left_downstream, right_downstream, independent])

    single_row = next(row for row in snapshot_rows if row["关联资产"] == independent)
    snapshot_file = os.path.join(
        SNAPSHOT_DIR, f"snapshot_{independent}_{single_row['快照编号']}.json")
    check("单表快照文件可回查来源", os.path.exists(snapshot_file), snapshot_file)
    if os.path.exists(snapshot_file):
        with open(snapshot_file, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        check("单表快照文件记录 source_version",
              saved.get("source_version") == "v1", str(saved.get("source_version")))


def test_conflict_blocks_only_affected_branch():
    """同一次提交里上游字段与下游使用方式冲突，独立分支仍具备发布资格。"""
    print("\n=== 场景2: 字段冲突阻断分支，独立表仍可发布 ===")
    svc = service_for("schema_registry")

    upstream = "T_DWD_PAY"
    downstream = "T_DWS_PAY"
    independent = "T_ADS_PAY_SOLO"

    payload = {
        "tables": [
            {
                "entry_id": 1101,
                "asset": upstream,
                "version": "v5",
                "fields": ["pay_id:bigint", "biz_no:string"],
            },
            {
                "entry_id": 1102,
                "asset": downstream,
                "version": "v1",
                "fields": ["biz_no:string", "pay_cnt:bigint"],
                "depends_on": {
                    upstream: {
                        "version": "v5",
                        # channel 字段上游从未提供，属于字段与下游使用方式冲突
                        "requires": ["pay_id:bigint", "channel:string"],
                        "confirmed": True,
                    }
                },
            },
            {
                "entry_id": 1103,
                "asset": independent,
                "version": "v1",
                "fields": ["id:bigint", "metric:decimal"],
            },
        ],
    }

    preview = svc.preview_publication(payload)
    verdicts = {item["asset"]: item for item in preview["表结论"]}
    check("下游分支被字段冲突阻断",
          verdicts[downstream]["eligible"] is False
          and "required_field_missing" in reason_codes(verdicts[downstream]))
    check("冲突记录在上游表结论中",
          "downstream_requires_missing_field" in reason_codes(verdicts[upstream]))
    check("独立分支仍具备发布资格", verdicts[independent]["eligible"] is True)
    check("可发布表只包含独立分支",
          preview["可发布表"] == [independent], str(preview["可发布表"]))

    result = svc.publish_publication(payload)
    batch = result["batch"]
    check("发布批次只发布独立分支", batch["已发布表数"] == 1, str(batch["已发布表数"]))
    check("被阻断的上游与下游都记录为未发布",
          {item["关联资产"] for item in batch["未发布表"]} == {upstream, downstream},
          str([item["关联资产"] for item in batch["未发布表"]]))
    check("未发布表带原因",
          all(item["原因"] for item in batch["未发布表"]))
    check("字段可用范围未包含被阻断表",
          downstream not in svc.field_availability()
          and upstream not in svc.field_availability())
    check("快照集合只含独立分支",
          [item["关联资产"] for item in batch["快照集合"]["快照"]] == [independent])


def test_unconfirmed_branch_not_activated():
    """下游声明所需的上游字段未确认前不得激活；确认后可发布。"""
    print("\n=== 场景3: 上游字段未确认分支不得激活 ===")
    svc = service_for("schema_registry")

    upstream = "T_DWD_STOCK"
    downstream = "T_DWS_STOCK"
    payload = {
        "tables": [
            {
                "entry_id": 1201,
                "asset": upstream,
                "version": "v2",
                "fields": ["sku_id:bigint", "qty:int"],
            },
            {
                "entry_id": 1202,
                "asset": downstream,
                "version": "v1",
                "fields": ["sku_id:bigint", "stock_days:int"],
                "depends_on": {
                    upstream: {
                        "version": "v2",
                        "requires": {"sku_id": "bigint"},
                        "confirmed": False,
                    }
                },
            },
        ],
    }

    preview = svc.preview_publication(payload)
    verdict = next(item for item in preview["表结论"] if item["asset"] == downstream)
    check("未确认时下游分支不可发布", verdict["eligible"] is False)
    check("阻断原因是上游字段未确认",
          "upstream_unconfirmed" in reason_codes(verdict))

    result = svc.publish_publication(payload)
    batch = result["batch"]
    check("未确认分支未进入发布",
          [item["关联资产"] for item in batch["未发布表"]] == [downstream],
          str(batch["未发布表"]))
    check("未确认分支未生成快照",
          not any(item["关联资产"] == downstream
                  for item in batch["快照集合"]["快照"]))

    confirm = svc.confirm_upstream_fields({
        "downstream": downstream,
        "upstream": upstream,
        "version": "v2",
        "fields": ["sku_id:bigint"],
    })
    check("字段依赖确认成功", confirm["ok"] is True, confirm.get("message", ""))

    preview_after = svc.preview_publication(payload)
    verdict_after = next(item for item in preview_after["表结论"]
                         if item["asset"] == downstream)
    check("确认后下游分支可发布", verdict_after["eligible"] is True,
          str(verdict_after["reasons"]))

    # 补齐确认后重新提交同一份清单，生成新批次而不是复用被阻断的批次。
    result_after = svc.publish_publication(payload)
    check("确认后重新提交生成新批次",
          result_after.get("replayed") is False
          and result_after["batch"]["批次编号"] != batch["批次编号"],
          str(result_after["batch"]["批次编号"]))
    check("确认后整组发布成功", result_after["batch"]["已发布表数"] == 2,
          str(result_after["batch"]["已发布表数"]))
    check("新批次包含两张表的快照",
          {item["关联资产"] for item in result_after["batch"]["快照集合"]["快照"]}
          == {upstream, downstream})

    replay = svc.publish_publication(payload)
    check("新批次完全成功后再提交仍幂等",
          replay.get("replayed") is True
          and replay["batch"]["批次编号"] == result_after["batch"]["批次编号"])


def test_duplicate_submission_is_idempotent():
    """同一次发布重复提交不生成重复快照。"""
    print("\n=== 场景4: 重复提交幂等 ===")
    svc = service_for("schema_registry")

    payload = {
        "batch_key": "ACC-BATCH-REPLAY",
        "tables": [
            {
                "entry_id": 1301,
                "asset": "T_DWD_REPLAY",
                "version": "v1",
                "fields": ["id:bigint", "v:string"],
            },
            {
                "entry_id": 1302,
                "asset": "T_DWS_REPLAY",
                "version": "v1",
                "fields": ["id:bigint", "cnt:bigint"],
                "depends_on": {
                    "T_DWD_REPLAY": {
                        "version": "v1",
                        "requires": ["id:bigint"],
                        "confirmed": True,
                    }
                },
            },
        ],
    }

    first = svc.publish_publication(payload)
    batch_id = first["batch"]["批次编号"]
    snapshot_count = len(store.rows("version_snapshot"))
    batch_count = len(store.rows("schema_publication_batch"))
    artifact_path = os.path.join(PUBLICATION_DIR, f"batch_snapshot_{batch_id}.json")

    second = svc.publish_publication(payload)
    check("重复提交返回已存在批次", second.get("replayed") is True)
    check("重复提交返回同一个批次编号",
          second["batch"]["批次编号"] == batch_id)
    check("重复提交不新增版本快照",
          len(store.rows("version_snapshot")) == snapshot_count,
          f"{snapshot_count} -> {len(store.rows('version_snapshot'))}")
    check("重复提交不新增批次记录",
          len(store.rows("schema_publication_batch")) == batch_count)
    with open(artifact_path, "r", encoding="utf-8") as handle:
        artifact = json.load(handle)
    check("批次快照文件未被重复覆盖出新条目",
          len(artifact["tables"]) == 2, str(len(artifact["tables"])))

    third = svc.publish_publication({**payload, "batch_key": "ACC-BATCH-REPLAY-COPY"})
    check("同内容不同批次键可重新发布（新批次）",
          third.get("replayed") is False and third["batch"]["批次编号"] != batch_id)
    check("重新发布生成独立快照集合",
          len(third["batch"]["快照集合"]["快照"]) == 2)

    conflicting = svc.publish_publication({
        **payload,
        "tables": [{"entry_id": 1399, "asset": "T_OTHER_REPLAY",
                    "version": "v1", "fields": ["id:bigint"]}],
    })
    check("同一批次键携带不同内容被拒绝",
          conflicting["ok"] is False and conflicting["batch"]["批次编号"] == batch_id,
          str(conflicting.get("message")))


def test_missing_required_field_recorded():
    """下游所需的字段上游完全没有时，上游与下游都不得进入快照。"""
    print("\n=== 场景5: 缺失所需字段时两张表都记录原因 ===")
    svc = service_for("schema_registry")

    payload = {
        "tables": [
            {"entry_id": 1401, "asset": "T_DWD_EDGE", "version": "v1",
             "fields": ["a:bigint"]},
            {"entry_id": 1402, "asset": "T_DWS_EDGE", "version": "v1",
             "fields": ["b:string"],
             "depends_on": {"T_DWD_EDGE": {"version": "v1",
                                           "requires": ["c:string"],
                                           "confirmed": True}}},
        ],
    }
    result = svc.publish_publication(payload)
    batch = result["batch"]
    check("没有任何表进入发布", batch["已发布表数"] == 0, str(batch["已发布表数"]))
    check("快照集合为空", batch["快照集合"]["条目数"] == 0)
    check("两张未发布表都有原因",
          len(batch["未发布表"]) == 2
          and all(item["原因"] for item in batch["未发布表"]))
    check("批次状态为未发布", batch["发布状态"] == "未发布")
    check("批次仍可回查",
          svc.get_publication(batch["批次编号"]) is not None)


def test_selected_entries_with_lineage_dependencies():
    """只给记录 id 的提交也能按已注册字段和血缘补全依赖。"""
    print("\n=== 场景6: 按记录 id 提交并回查已注册版本 ===")
    svc = service_for("schema_registry")

    upstream_entry = {
        "id": 1501, "status": "已注册", "pending": False, "abnormal": False,
        "版本编号": "SCHE-1501", "关联资产": "T_DWD_SELECTED",
        "字段清单": "id:bigint,name:string", "前向兼容": "是",
        "后向兼容": "是", "Schema摘要": "已注册", "注册时间": "2026-01-01",
        "版本状态": "已注册",
    }
    downstream_entry = {
        "id": 1502, "status": "已注册", "pending": False, "abnormal": False,
        "版本编号": "SCHE-1502", "关联资产": "T_DWS_SELECTED",
        "字段清单": "id:bigint,total:decimal", "前向兼容": "是",
        "后向兼容": "是", "Schema摘要": "已注册", "注册时间": "2026-01-01",
        "版本状态": "已注册",
    }
    store.rows("schema_registry").extend([upstream_entry, downstream_entry])
    store.rows("lineage_graph").append({
        "id": 1501, "status": "已生效", "pending": False, "abnormal": False,
        "记录编号": "LINE-1501", "上游资产": "T_DWD_SELECTED",
        "下游资产": "T_DWS_SELECTED", "依赖类型": "数仓加工",
        "加工逻辑": "测试", "生效时间": "2026-01-01", "变更SHA": "x",
        "血缘状态": "已生效",
    })

    preview = svc.preview_publication({"entry_ids": [1501, 1502]})
    verdicts = {item["asset"]: item for item in preview["表结论"]}
    check("按记录 id 提交能识别两张表", set(verdicts) ==
          {"T_DWD_SELECTED", "T_DWS_SELECTED"}, str(set(verdicts)))
    dependency = verdicts["T_DWS_SELECTED"]["dependencies"]
    check("血缘补全了上游依赖",
          any(dep["upstream"] == "T_DWD_SELECTED" for dep in dependency), str(dependency))
    check("来源版本取自已注册记录",
          verdicts["T_DWD_SELECTED"]["source_version"] == "SCHE-1501",
          str(verdicts["T_DWD_SELECTED"]["source_version"]))

    check("缺少声明字段不阻断（未声明 required）",
          verdicts["T_DWS_SELECTED"]["eligible"] is True,
          str(verdicts["T_DWS_SELECTED"]["reasons"]))

    result = svc.publish_publication({"entry_ids": [1501, 1502]})
    check("按记录 id 的整组提交发布成功",
          result["batch"]["已发布表数"] == 2, str(result["batch"]["已发布表数"]))
    check("来源版本写入快照",
          {item["关联资产"]: item["来源版本"]
           for item in result["batch"]["快照集合"]["快照"]} == {
              "T_DWD_SELECTED": "SCHE-1501",
              "T_DWS_SELECTED": "SCHE-1502",
          })


def test_route_registration_order():
    """结构发布固定路径必须排在 /api/{module} 之前。"""
    print("\n=== 场景7: 路由注册顺序 ===")
    from app.main import app

    paths = list(app.openapi()["paths"])
    publication_index = paths.index("/api/schema-publications")
    module_index = paths.index("/api/{module}")
    check("固定路径先于动态模块路径注册", publication_index < module_index,
          f"{publication_index} vs {module_index}")
    check("发布接口已注册", "/api/schema-publications/publish" in paths)
    check("确认接口已注册", "/api/schema-publications/confirm" in paths)
    check("批次快照目录存在", os.path.isdir(PUBLICATION_DIR))


def test_entry_guards_and_downstream_propagation():
    """记录级约束：已废止版本、来源记录错配都会阻断，并传导给同批下游。"""
    print("\n=== 场景8: 记录级约束与下游传导 ===")
    svc = service_for("schema_registry")

    store.rows("schema_registry").extend([
        {
            "id": 1601, "status": "已注册", "pending": False, "abnormal": False,
            "版本编号": "SCHE-1601", "关联资产": "T_DWD_DEPRECATED",
            "字段清单": "id:bigint", "前向兼容": "是", "后向兼容": "是",
            "Schema摘要": "已废止", "注册时间": "2026-01-01", "版本状态": "已废止",
        },
        {
            "id": 1602, "status": "已注册", "pending": False, "abnormal": False,
            "版本编号": "SCHE-1602", "关联资产": "T_DWS_GUARDED",
            "字段清单": "id:bigint", "前向兼容": "是", "后向兼容": "是",
            "Schema摘要": "已注册", "注册时间": "2026-01-01", "版本状态": "已注册",
        },
    ])

    payload = {
        "tables": [
            {"entry_id": 1601, "asset": "T_DWD_DEPRECATED", "version": "SCHE-1601"},
            {"entry_id": 1602, "asset": "T_DWS_GUARDED", "version": "SCHE-1602",
             "depends_on": {"T_DWD_DEPRECATED": {"version": "SCHE-1601"}}},
            {"entry_id": 1602, "asset": "T_OTHER_TABLE", "version": "v1",
             "fields": ["x:int"]},
        ],
    }

    preview = svc.preview_publication(payload)
    verdicts = {item["asset"]: item for item in preview["表结论"]}
    check("已废止版本不可发布",
          verdicts["T_DWD_DEPRECATED"]["eligible"] is False
          and "entry_deprecated" in reason_codes(verdicts["T_DWD_DEPRECATED"]))
    check("下游因上游不可发布而阻断",
          verdicts["T_DWS_GUARDED"]["eligible"] is False
          and "upstream_not_published" in reason_codes(verdicts["T_DWS_GUARDED"]))
    check("来源记录错配被阻断",
          verdicts["T_OTHER_TABLE"]["eligible"] is False
          and "entry_asset_mismatch" in reason_codes(verdicts["T_OTHER_TABLE"]))
    check("重复使用同一来源记录被阻断",
          "entry_reused" in reason_codes(verdicts["T_OTHER_TABLE"]))
    check("预览没有任何可发布表", preview["可发布表"] == [], str(preview["可发布表"]))

    result = svc.publish_publication(payload)
    check("批次记录全部排除", result["batch"]["排除表数"] == 3,
          str(result["batch"]["排除表数"]))
    check("所有排除表都带原因",
          all(item["原因"] for item in result["batch"]["未发布表"]))


def test_cycle_and_version_mismatch():
    """同批依赖环、依赖版本不一致都不能进入发布，独立表不受影响。"""
    print("\n=== 场景9: 依赖环与版本不一致 ===")
    svc = service_for("schema_registry")

    payload = {
        "tables": [
            {"entry_id": 1701, "asset": "T_CYCLE_A", "version": "v1",
             "fields": ["a:int"],
             "depends_on": {"T_CYCLE_B": {"version": "v1", "requires": ["a:int"],
                                          "confirmed": True}}},
            {"entry_id": 1702, "asset": "T_CYCLE_B", "version": "v1",
             "fields": ["a:int"],
             "depends_on": {"T_CYCLE_A": {"version": "v1", "requires": ["a:int"],
                                          "confirmed": True}}},
            {"entry_id": 1703, "asset": "T_VERSION_UP", "version": "v2",
             "fields": ["id:bigint"]},
            {"entry_id": 1704, "asset": "T_VERSION_DOWN", "version": "v1",
             "fields": ["id:bigint"],
             "depends_on": {"T_VERSION_UP": {"version": "v1", "requires": ["id:bigint"],
                                             "confirmed": True}}},
            {"entry_id": 1705, "asset": "T_VERSION_FREE", "version": "v1",
             "fields": ["id:bigint"]},
        ],
    }

    preview = svc.preview_publication(payload)
    verdicts = {item["asset"]: item for item in preview["表结论"]}
    check("依赖环上的表被阻断",
          "dependency_cycle" in reason_codes(verdicts["T_CYCLE_A"])
          and "dependency_cycle" in reason_codes(verdicts["T_CYCLE_B"]))
    check("依赖版本不一致被阻断",
          "dependency_version_mismatch" in reason_codes(verdicts["T_VERSION_DOWN"]))
    check("版本不一致不牵连上游独立发布",
          verdicts["T_VERSION_UP"]["eligible"] is True)
    check("独立表仍可发布", verdicts["T_VERSION_FREE"]["eligible"] is True)

    result = svc.publish_publication(payload)
    published = {item["关联资产"] for item in result["batch"]["快照集合"]["快照"]}
    check("只有独立表与被依赖的上游进入快照",
          published == {"T_VERSION_UP", "T_VERSION_FREE"}, str(published))


if __name__ == "__main__":
    print("=" * 60)
    print("结构发布批次验收测试")
    print("=" * 60)

    test_batch_publish_with_shared_dependencies()
    test_conflict_blocks_only_affected_branch()
    test_unconfirmed_branch_not_activated()
    test_duplicate_submission_is_idempotent()
    test_missing_required_field_recorded()
    test_selected_entries_with_lineage_dependencies()
    test_route_registration_order()
    test_entry_guards_and_downstream_propagation()
    test_cycle_and_version_mismatch()

    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)

    if failed:
        sys.exit(1)
