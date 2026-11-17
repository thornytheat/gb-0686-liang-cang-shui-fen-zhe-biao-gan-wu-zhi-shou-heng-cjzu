import json
from decimal import Decimal as D
import pytest
from app.pipeline import run
from app import store
from app.config import SAMPLES, RULE_VERSION
from app.errors import DomainError
from app.modules.m8_adjustment import service, store as adjust_store
from app.modules.m8_adjustment.rules import (
    adjustment_id, new_standard, restandardize, verify_dry_matter,
)


@pytest.fixture
def approved(isolate):
    result = run((SAMPLES / "dirty.csv").read_bytes())
    store.review(result["manifest"]["run_id"], 1, "通过")
    return store.fetch(result["manifest"]["run_id"])


# ---------- 规则层 ----------

def test_new_standard_validation(approved):
    old = approved["rules"]["standard_moisture"]
    cap = approved["rules"]["max_moisture"]
    assert new_standard("13", old, cap) == D("13")
    with pytest.raises(DomainError) as exc:
        new_standard("14", old, cap)
    assert exc.value.code == "RANGE"
    with pytest.raises(DomainError) as exc:
        new_standard("28", old, cap)
    assert exc.value.code == "RANGE"
    with pytest.raises(DomainError) as exc:
        new_standard("-", old, cap)
    assert exc.value.code == "MISSING"


def test_restandardize_keeps_dry_matter():
    record = {"id": "01", "bin": "仓1", "_line": 7, "moisture": D("16"),
              "clean_kg": D("990"), "dry_kg": D("831.6"),
              "standard_kg": D("967.9069767441860465116279070"),
              "amount": D("2516.56")}
    entry = restandardize(record, D("13"))
    assert entry["dry_kg"] == D("831.6")
    assert entry["new_standard_kg"] == D("831.6") / D("0.87")
    assert entry["delta_kg"] == entry["new_standard_kg"] - entry["old_standard_kg"]


def test_dry_matter_mismatch_detected():
    record = {"id": "01", "clean_kg": D("990"), "moisture": D("16"),
              "dry_kg": D("1.0")}
    with pytest.raises(DomainError) as exc:
        verify_dry_matter(record)
    assert exc.value.code == "DRY_MISMATCH"


def test_adjustment_id_is_deterministic():
    a = adjustment_id("run1", D("14"), D("13"), RULE_VERSION)
    b = adjustment_id("run1", D("14"), D("13"), RULE_VERSION)
    c = adjustment_id("run1", D("14"), D("12"), RULE_VERSION)
    assert a == b and a != c and a.startswith("ADJ")


# ---------- 服务层：原单保留、两版干物质、分币守恒 ----------

def test_open_requires_reviewed_settlement(isolate):
    pending = run((SAMPLES / "dirty.csv").read_bytes())
    with pytest.raises(DomainError) as exc:
        service.open_or_get(pending["manifest"]["run_id"], "13", RULE_VERSION)
    assert exc.value.code == "STATE" and exc.value.status == 409
    assert adjust_store.list_adjustments() == []


def test_adjustment_creates_new_version_without_touching_original(approved):
    before_total = approved["total_amount"]
    doc = service.open_or_get(approved["manifest"]["run_id"], "13", RULE_VERSION)
    assert doc["status"] == "已确认"
    assert doc["run_id"] == approved["manifest"]["run_id"]
    assert doc["id"] != approved["manifest"]["run_id"]
    assert doc["confirmed_trucks"] == doc["total_trucks"] == len(approved["records"])
    # 原单原标准、原分币结果原样保留
    original = store.fetch(approved["manifest"]["run_id"])
    assert original["total_amount"] == before_total
    assert original["review"] == "通过"
    assert str(original["rules"]["standard_moisture"]) == doc["old_standard"]
    # 每条分录两版干物质一致
    by_id = {r["id"]: r for r in original["records"]}
    for entry in doc["entries"]:
        assert D(entry["dry_kg"]) == D(by_id[entry["id"]]["dry_kg"])
    # 新折标按新标准重算，干物质不动
    for entry in doc["entries"]:
        assert D(entry["new_standard_kg"]) == D(entry["dry_kg"]) / D("0.87")
    # 末端分币守恒与差额守恒
    assert sum(D(e["new_amount"]) for e in doc["entries"]) == D(doc["new_total"])
    assert sum(D(e["amount_delta"]) for e in doc["entries"]) == D(doc["total_delta"])
    assert D(doc["total_delta"]) == D(doc["new_total"]) - D(doc["old_total"])
    # 收购上限与单价沿用原单
    assert doc["max_moisture"] == str(approved["rules"]["max_moisture"])
    assert doc["price"] == str(approved["rules"]["price"])


def test_same_adjustment_rerun_is_idempotent(approved):
    first = service.open_or_get(approved["manifest"]["run_id"], "13", RULE_VERSION)
    second = service.open_or_get(approved["manifest"]["run_id"], "13", RULE_VERSION)
    assert first["id"] == second["id"]
    assert len(second["entries"]) == len(approved["records"])
    assert len({e["id"] for e in second["entries"]}) == len(second["entries"])
    third = service.resume(first["id"])
    assert third["id"] == first["id"] and len(third["entries"]) == len(first["entries"])


def test_interrupted_run_resumes_from_confirmed_trucks(approved, monkeypatch):
    calls = []
    real = service.process_truck

    def flaky(record, target):
        calls.append(record["id"])
        if len(calls) == 3:
            raise RuntimeError("simulated crash")
        return real(record, target)

    monkeypatch.setattr(service, "process_truck", flaky)
    with pytest.raises(RuntimeError):
        service.open_or_get(approved["manifest"]["run_id"], "13", RULE_VERSION)
    adjust_id = adjustment_id(approved["manifest"]["run_id"],
                               D("14"), D("13"), RULE_VERSION)
    header = adjust_store.get_header(adjust_id)
    assert header["status"] == "处理中"
    assert adjust_store.count_entries(adjust_id) == 2

    # 注意：不能用 monkeypatch.undo()，它会连带回滚 isolate fixture 设置的 DATA_DIR
    processed_after_resume = []
    def counting(record, target):
        processed_after_resume.append(record["id"])
        return real(record, target)
    monkeypatch.setattr(service, "process_truck", counting)
    doc = service.resume(adjust_id)
    assert doc["status"] == "已确认"
    assert doc["confirmed_trucks"] == len(approved["records"])
    # 已确认的两车次续跑时不再处理；本次只补跑剩余车次
    assert len(processed_after_resume) == len(approved["records"]) - 2
    assert not set(processed_after_resume) & set(calls[:2])
    # 再续跑不重复任何车次或分录
    again = service.resume(adjust_id)
    assert len(again["entries"]) == len(approved["records"])
    assert len({e["id"] for e in again["entries"]}) == len(again["entries"])


def test_dry_matter_mismatch_fails_open_and_keeps_original_ledger(approved):
    rid = approved["manifest"]["run_id"]
    with store.connection() as db:
        payload = json.loads(
            db.execute("SELECT payload FROM runs WHERE id=?", (rid,)).fetchone()["payload"])
        payload["records"][0]["dry_kg"] = "999999"
        db.execute("UPDATE runs SET payload=? WHERE id=?",
                   (json.dumps(payload, ensure_ascii=False), rid))
    with pytest.raises(DomainError) as exc:
        service.open_or_get(rid, "13", RULE_VERSION)
    assert exc.value.code == "DRY_MISMATCH"
    adjust_id = adjustment_id(rid, D("14"), D("13"), RULE_VERSION)
    header = adjust_store.get_header(adjust_id)
    assert header["status"] == "失败" and header["failure_code"] == "DRY_MISMATCH"
    assert adjust_store.count_entries(adjust_id) == 0
    # 原账保留：金额、复核状态、标准水分均不变
    original = store.fetch(rid)
    assert original["total_amount"] == approved["total_amount"]
    assert original["review"] == "通过"
    assert str(original["rules"]["standard_moisture"]) == "14"


def test_failed_adjustment_rerun_does_not_duplicate(approved):
    rid = approved["manifest"]["run_id"]
    adjust_id = adjustment_id(rid, D("14"), D("13"), RULE_VERSION)
    adjust_store.save_header({
        "id": adjust_id, "run_id": rid, "status": "失败",
        "old_standard": "14", "new_standard": "13", "max_moisture": "28",
        "price": "2.6", "old_total": approved["total_amount"],
        "new_total": None, "total_delta": None,
        "total_trucks": len(approved["records"]),
        "created": "2026-11-17T00:00:00+00:00", "updated": "2026-11-17T00:00:00+00:00",
        "failure_code": "DRY_MISMATCH", "failure_message": "x",
    })
    adjust_store.add_entry(adjust_id, {"id": "stale", "dry_kg": "1"})
    doc = service.open_or_get(rid, "13", RULE_VERSION)
    assert doc["id"] == adjust_id and doc["status"] == "已确认"
    ids = [e["id"] for e in doc["entries"]]
    assert "stale" not in ids and len(ids) == len(set(ids))
    assert len(ids) == len(approved["records"])


def test_different_targets_are_distinct_adjustments(approved):
    rid = approved["manifest"]["run_id"]
    a = service.open_or_get(rid, "13", RULE_VERSION)
    b = service.open_or_get(rid, "12", RULE_VERSION)
    assert a["id"] != b["id"]
    assert {h["id"] for h in adjust_store.list_adjustments(rid)} == {a["id"], b["id"]}


# ---------- HTTP / 页面 / 证据 ----------

def test_http_adjustment_flow_and_validation(client, approved):
    rid = approved["manifest"]["run_id"]
    assert client.post("/api/adjustments", json={}).status_code == 422
    assert client.post("/api/adjustments",
                       json={"run_id": "missing", "new_standard_moisture": "13"}).status_code == 404
    assert client.post("/api/adjustments",
                       json={"run_id": rid, "new_standard_moisture": "14"}).status_code == 422
    reply = client.post("/api/adjustments",
                        json={"run_id": rid, "new_standard_moisture": "13",
                              "max_moisture": "99", "price": "0.01"})
    assert reply.status_code == 200
    doc = reply.json
    assert doc["max_moisture"] == "28" and doc["price"] == "2.6"
    # 重跑同一调整不重复
    again = client.post("/api/adjustments",
                        json={"run_id": rid, "new_standard_moisture": "13"})
    assert again.json["id"] == doc["id"] and len(again.json["entries"]) == len(doc["entries"])
    assert client.get(f"/api/adjustments?run_id={rid}").json[0]["id"] == doc["id"]
    resumed = client.post(f"/api/adjustments/{doc['id']}/resume", json={})
    assert resumed.status_code == 200 and resumed.json["status"] == "已确认"
    assert client.get("/api/adjustments/" + doc["id"]).json["total_delta"]


def test_adjustment_evidence_csv(client, approved):
    rid = approved["manifest"]["run_id"]
    doc = client.post("/api/adjustments",
                      json={"run_id": rid, "new_standard_moisture": "13"}).json
    response = client.get(f"/api/adjustments/{doc['id']}/evidence.csv")
    assert response.status_code == 200
    text = response.data.decode("utf-8-sig")
    header = text.splitlines()[0].split(",")
    for column in ["原标准水分 %", "新标准水分 %", "原金额 CNY", "新金额 CNY", "差额 CNY"]:
        assert column in header
    lines = text.strip().splitlines()
    # 每车一行 + 合计行
    assert len(lines) == len(doc["entries"]) + 2
    assert "不调收购上限；不做付款" in lines[-1]


def test_m6_page_shows_original_and_adjustment(client, approved):
    rid = approved["manifest"]["run_id"]
    doc = client.post("/api/adjustments",
                      json={"run_id": rid, "new_standard_moisture": "13"}).json
    page = client.get(f"/m/6?run={rid}&adj={doc['id']}").get_data(as_text=True)
    for needle in ["分币结算表（原单保留）", "水分标准分批调整", "新差额 CNY",
                   doc["id"], "不调"]:
        assert needle in page


def test_pending_run_page_hides_adjustment_form(client, isolate):
    pending = run((SAMPLES / "dirty.csv").read_bytes())
    page = client.get(f"/m/6?run={pending['manifest']['run_id']}").get_data(as_text=True)
    assert "adjust-form" not in page
    assert "复核通过后才能发起标准调整" in page


def test_no_payment_capability_exposed(client, approved):
    rid = approved["manifest"]["run_id"]
    doc = client.post("/api/adjustments",
                      json={"run_id": rid, "new_standard_moisture": "13"}).json
    # 调整单与分录只承载折标/结算字段，没有任何支付状态或账户字段
    forbidden = {"paid", "payment", "pay_status", "account", "account_no"}
    blob = json.dumps(doc, ensure_ascii=False)
    assert not any(key in blob for key in forbidden)
    for entry in doc["entries"]:
        assert forbidden.isdisjoint(entry)
    # 路由表中没有付款类端点
    rules = {rule.rule for rule in client.application.url_map.iter_rules()}
    assert not any("pay" in rule for rule in rules)
