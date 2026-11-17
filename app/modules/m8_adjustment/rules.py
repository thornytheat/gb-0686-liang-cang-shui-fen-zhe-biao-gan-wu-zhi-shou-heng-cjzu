import hashlib
import json
from decimal import Decimal as D
from app.normalize import number
from app.errors import DomainError


def adjustment_id(run_id, old_standard, new_standard, rule_version):
    """同一原批次、同一新旧标准、同一规则版本 => 同一调整单（幂等键）。"""
    basis = json.dumps(
        [run_id, str(old_standard), str(new_standard), rule_version],
        sort_keys=True, ensure_ascii=False,
    )
    return "ADJ" + hashlib.sha256(basis.encode()).hexdigest()[:17]


def new_standard(raw_value, old_standard, max_moisture):
    """校验新标准：仅允许调整标准水分；收购上限沿用原单，不接受外部传参。"""
    if raw_value is None:
        raise DomainError("MISSING", "请提供调整后的标准水分")
    old_standard = D(str(old_standard))
    max_moisture = D(str(max_moisture))
    candidate = number(raw_value)
    if not 0 <= candidate < max_moisture < 100:
        raise DomainError("RANGE", "新标准水分须低于原单收购上限且低于100")
    if candidate == old_standard:
        raise DomainError("RANGE", "新标准水分与原标准相同，无需调整")
    return candidate


def verify_dry_matter(record):
    """核对两版共同基准：净粮×(1-实测水分) 必须等于已保存的干物质。"""
    clean = D(str(record["clean_kg"]))
    moisture = D(str(record["moisture"]))
    expected = clean * (1 - moisture / D(100))
    stored = D(str(record["dry_kg"]))
    if expected != stored:
        raise DomainError(
            "DRY_MISMATCH",
            f"车次{record['id']}干物质核对不符：推算{expected}≠存档{stored}",
        )
    return stored


def restandardize(record, new_standard):
    """干物质不动，仅以新标准水分重算折标重；返回该车次的调整分录。"""
    dry = verify_dry_matter(record)
    factor = 1 - new_standard / D(100)
    new_standard_kg = dry / factor
    old_standard_kg = D(str(record["standard_kg"]))
    return {
        "id": record["id"],
        "bin": record["bin"],
        "line": record.get("_line", ""),
        "moisture": D(str(record["moisture"])),
        "dry_kg": dry,
        "old_standard_kg": old_standard_kg,
        "new_standard_kg": new_standard_kg,
        "delta_kg": new_standard_kg - old_standard_kg,
        "old_amount": D(str(record["amount"])),
    }
