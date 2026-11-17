from datetime import datetime, timezone
from decimal import Decimal as D
from app import store as run_store
from app.errors import DomainError
from app.modules.m6_settlement.rules import settle
from . import store
from .rules import adjustment_id, new_standard as parse_standard, restandardize, verify_dry_matter


def _now():
    return datetime.now(timezone.utc).isoformat()


def _original(run_id):
    run = run_store.fetch(run_id)
    if run.get("review") != "通过":
        raise DomainError("STATE", "原结算单未复核通过，不能调整", 409)
    if not run.get("records"):
        raise DomainError("ADJUST_EMPTY", "原批次没有可调整的采信车次")
    return run


def _trucks(run):
    # id 升序处理：与 M6 分币裁决的稳定次序一致，续跑次序也确定
    return sorted(run["records"], key=lambda r: str(r["id"]))


def process_truck(record, target_standard):
    """单车确认：核对两版干物质后按新标准重算折标。可被测试替身中断。"""
    return restandardize(record, target_standard)


def open_or_get(run_id, raw_new_standard, rule_version):
    """开调整单；同一调整重跑复用原单，绝不另起一账。"""
    run = _original(run_id)
    rules = run["rules"]
    old_standard = D(str(rules["standard_moisture"]))
    target = parse_standard(raw_new_standard, old_standard,
                            D(str(rules["max_moisture"])))
    adjust_id = adjustment_id(run_id, old_standard, target, rule_version)
    existing = store.list_adjustments(run_id)
    for header in existing:
        if header["id"] == adjust_id:
            return resume(adjust_id)
    old_total = sum((D(r["amount"]) for r in run["records"]), D(0))
    if old_total != D(str(run["total_amount"])):
        raise DomainError("INCOMPLETE", "原结算单分币合计与总额不符，禁止调整")
    header = {
        "id": adjust_id, "run_id": run_id, "status": "处理中",
        "old_standard": str(old_standard), "new_standard": str(target),
        "max_moisture": str(rules["max_moisture"]), "price": str(rules["price"]),
        "old_total": str(old_total), "new_total": None, "total_delta": None,
        "total_trucks": len(run["records"]),
        "created": _now(), "updated": _now(),
    }
    store.save_header(header)
    return resume(adjust_id)


def resume(adjust_id):
    """从已确认车次后续跑；已确认的车次与分录不重复处理。"""
    header = store.get_header(adjust_id)
    if header["status"] == "已确认":
        return store.load(adjust_id)
    run = _original(header["run_id"])
    target = D(header["new_standard"])
    if header["status"] == "失败":
        # 失败单允许整单重跑：先清掉半成品分录，原结算单始终未动
        store.clear_entries(adjust_id)
        store.mark_status(adjust_id, "处理中")
    trucks = _trucks(run)
    try:
        for record in trucks:
            truck_id = str(record["id"])
            if store.entry_exists(adjust_id, truck_id):
                continue
            entry = process_truck(record, target)
            store.add_entry(adjust_id, entry)
    except DomainError as exc:
        store.mark_status(adjust_id, "失败",
                          failure={"code": exc.code, "message": exc.message})
        raise
    if store.count_entries(adjust_id) == header["total_trucks"]:
        _finalize(adjust_id, run)
    return store.load(adjust_id)


def _finalize(adjust_id, run):
    """全部车次确认后，末端一次性分币；核对两版干物质与金额守恒。"""
    header = store.get_header(adjust_id)
    entries = store.list_entries(adjust_id)
    trucks = _trucks(run)
    if len(entries) != len(trucks):
        raise DomainError("INCOMPLETE", "调整分录车次不完整，不能结算")
    by_id = {str(r["id"]): r for r in trucks}
    if {e["id"] for e in entries} != set(by_id):
        raise DomainError("INCOMPLETE", "调整分录与原单车次不一致")
    target = D(header["new_standard"])
    for entry in entries:
        # 终态再核一遍两版干物质，防止中途数据被改
        verify_dry_matter(by_id[entry["id"]])
    rows, new_total = settle(
        [{"id": e["id"], "standard_kg": D(e["new_standard_kg"])} for e in entries],
        D(header["price"]),
    )
    amounts = {r["id"]: r["amount"] for r in rows}
    old_total = D(header["old_total"])
    if old_total != sum((D(r["amount"]) for r in trucks), D(0)):
        raise DomainError("INCOMPLETE", "原单分币合计发生变化，禁止定稿")
    total_delta = new_total - old_total
    store.finalize(adjust_id, amounts, new_total, total_delta)
    check = store.load(adjust_id)
    if sum((D(e["amount_delta"]) for e in check["entries"]), D(0)) != total_delta:
        raise DomainError("INCOMPLETE", "分币差额合计与总差额不符")
    return check
