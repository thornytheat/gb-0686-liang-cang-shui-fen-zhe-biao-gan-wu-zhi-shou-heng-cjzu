import csv
import io
import json
from flask import Blueprint, request, jsonify, Response
from app.config import RULE_VERSION
from app.errors import DomainError
from app.normalize import serial
from . import store
from .service import open_or_get, resume

bp = Blueprint("m8", __name__)


def _body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise DomainError("BODY", "请求必须是对象")
    return payload


@bp.route("/api/adjustments", methods=["POST"])
def start_adjustment():
    payload = _body()
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise DomainError("MISSING", "请提供原批次 run_id")
    result = open_or_get(run_id, payload.get("new_standard_moisture"),
                         RULE_VERSION)
    return jsonify(serial(result))


@bp.route("/api/adjustments/<adjustment_id>/resume", methods=["POST"])
def resume_adjustment(adjustment_id):
    return jsonify(serial(resume(adjustment_id)))


@bp.route("/api/adjustments/<adjustment_id>")
def get_adjustment(adjustment_id):
    return jsonify(serial(store.load(adjustment_id)))


@bp.route("/api/adjustments")
def list_adjustments():
    run_id = request.args.get("run_id")
    return jsonify(serial(store.list_adjustments(run_id)))


@bp.route("/api/adjustments/<adjustment_id>/evidence.csv")
def adjustment_evidence(adjustment_id):
    doc = store.load(adjustment_id)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["调整单", "原批次", "状态", "行号", "车次", "仓号",
                     "实测水分 %", "干物质 kg", "原标准水分 %", "新标准水分 %",
                     "原折标 kg", "新折标 kg", "折标差 kg",
                     "原金额 CNY", "新金额 CNY", "差额 CNY", "证据"])
    for entry in doc["entries"]:
        writer.writerow([
            doc["id"], doc["run_id"], doc["status"], entry.get("line", ""),
            entry["id"], entry["bin"], entry["moisture"], entry["dry_kg"],
            doc["old_standard"], doc["new_standard"],
            entry["old_standard_kg"], entry["new_standard_kg"], entry["delta_kg"],
            entry["old_amount"], entry.get("new_amount", ""),
            entry.get("amount_delta", ""),
            json.dumps(serial(entry), ensure_ascii=False),
        ])
    writer.writerow([
        doc["id"], doc["run_id"], doc["status"], "", "合计", "",
        "", "", doc["old_standard"], doc["new_standard"], "", "", "",
        doc["old_total"], doc.get("new_total", ""), doc.get("total_delta", ""),
        "不调收购上限；不做付款",
    ])
    return Response(
        "﻿" + stream.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=adjustment-{doc['id']}.csv"})
