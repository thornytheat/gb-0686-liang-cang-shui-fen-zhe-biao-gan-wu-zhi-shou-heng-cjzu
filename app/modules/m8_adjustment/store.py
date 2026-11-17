import json
from app.errors import DomainError
from app.normalize import serial
from app.store import connection as _base_connection


def connection():
    db = _base_connection()
    db.execute(
        "CREATE TABLE IF NOT EXISTS adjustments ("
        "id TEXT PRIMARY KEY,run_id TEXT NOT NULL,status TEXT NOT NULL,"
        "old_standard TEXT NOT NULL,new_standard TEXT NOT NULL,"
        "max_moisture TEXT NOT NULL,price TEXT NOT NULL,"
        "old_total TEXT,new_total TEXT,total_delta TEXT,"
        "total_trucks INTEGER NOT NULL,created TEXT NOT NULL,"
        "updated TEXT NOT NULL,failure_code TEXT,failure_message TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS adjustment_entries ("
        "adjustment_id TEXT NOT NULL,truck_id TEXT NOT NULL,"
        "payload TEXT NOT NULL,PRIMARY KEY(adjustment_id,truck_id)) WITHOUT ROWID"
    )
    return db


def save_header(header):
    # INSERT OR IGNORE：重跑同一调整复用同一单，不产生第二条头记录
    with connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO adjustments(id,run_id,status,old_standard,new_standard,"
            "max_moisture,price,old_total,new_total,total_delta,total_trucks,created,updated,"
            "failure_code,failure_message) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (header["id"], header["run_id"], header["status"],
             header["old_standard"], header["new_standard"], header["max_moisture"],
             header["price"], header.get("old_total"), header.get("new_total"),
             header.get("total_delta"), header["total_trucks"],
             header["created"], header["updated"],
             header.get("failure_code"), header.get("failure_message")),
        )


def mark_status(adjustment_id, status, failure=None):
    from datetime import datetime, timezone
    updated = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        if failure is None:
            cur = db.execute(
                "UPDATE adjustments SET status=?,updated=?,failure_code=NULL,"
                "failure_message=NULL WHERE id=?", (status, updated, adjustment_id))
        else:
            cur = db.execute(
                "UPDATE adjustments SET status=?,updated=?,failure_code=?,"
                "failure_message=? WHERE id=?",
                (status, updated, failure["code"], failure["message"], adjustment_id))
        if cur.rowcount != 1:
            raise DomainError("NOT_FOUND", "调整单不存在", 404)


def get_header(adjustment_id):
    with connection() as db:
        row = db.execute(
            "SELECT * FROM adjustments WHERE id=?", (adjustment_id,)).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "调整单不存在", 404)
    return dict(row)


def entry_exists(adjustment_id, truck_id):
    with connection() as db:
        row = db.execute(
            "SELECT 1 FROM adjustment_entries WHERE adjustment_id=? AND truck_id=?",
            (adjustment_id, truck_id)).fetchone()
    return row is not None


def add_entry(adjustment_id, entry):
    # 主键冲突即忽略：续跑时已确认车次不会重复落分录
    with connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO adjustment_entries(adjustment_id,truck_id,payload)"
            " VALUES(?,?,?)",
            (adjustment_id, entry["id"],
             json.dumps(serial(entry), ensure_ascii=False)))


def count_entries(adjustment_id):
    with connection() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM adjustment_entries WHERE adjustment_id=?",
            (adjustment_id,)).fetchone()
    return row["n"]


def list_entries(adjustment_id):
    with connection() as db:
        rows = db.execute(
            "SELECT payload FROM adjustment_entries WHERE adjustment_id=? "
            "ORDER BY truck_id", (adjustment_id,)).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def clear_entries(adjustment_id):
    with connection() as db:
        db.execute(
            "DELETE FROM adjustment_entries WHERE adjustment_id=?", (adjustment_id,))


def finalize(adjustment_id, amounts, new_total, total_delta):
    from decimal import Decimal as D
    from datetime import datetime, timezone
    updated = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        entries = db.execute(
            "SELECT truck_id,payload FROM adjustment_entries WHERE adjustment_id=?",
            (adjustment_id,)).fetchall()
        for row in entries:
            payload = json.loads(row["payload"])
            payload["new_amount"] = str(amounts[row["truck_id"]])
            payload["amount_delta"] = str(
                amounts[row["truck_id"]] - D(payload["old_amount"]))
            db.execute(
                "UPDATE adjustment_entries SET payload=? WHERE adjustment_id=? "
                "AND truck_id=?",
                (json.dumps(payload, ensure_ascii=False),
                 adjustment_id, row["truck_id"]))
        cur = db.execute(
            "UPDATE adjustments SET status='已确认',new_total=?,total_delta=?,"
            "updated=?,failure_code=NULL,failure_message=NULL WHERE id=?",
            (str(new_total), str(total_delta), updated, adjustment_id))
        if cur.rowcount != 1:
            raise DomainError("NOT_FOUND", "调整单不存在", 404)


def list_adjustments(run_id=None):
    with connection() as db:
        if run_id:
            rows = db.execute(
                "SELECT * FROM adjustments WHERE run_id=? ORDER BY rowid DESC",
                (run_id,)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM adjustments ORDER BY rowid DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]


def load(adjustment_id):
    header = get_header(adjustment_id)
    entries = list_entries(adjustment_id)
    header["entries"] = entries
    header["confirmed_trucks"] = len(entries)
    return header
