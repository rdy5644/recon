from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable


CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("99999999.99")


def parse_money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")
    return amount


def money_string(value: Decimal | Any) -> str:
    return format(parse_money(value), ".2f")


def money_number(value: Decimal | Any) -> float:
    return float(parse_money(value))


def normalize_merchant(value: str) -> str:
    return " ".join(value.split()).casefold()


def aggregate_settlements(
    transaction: dict[str, Any], settlements: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    amount = parse_money(transaction["transaction_amount"])
    rows = list(settlements)
    debit_total = sum(
        (parse_money(row["settlement_amount"]) for row in rows if row["settlement_type"] == "DEBIT"),
        Decimal("0.00"),
    )
    credit_total = sum(
        (parse_money(row["settlement_amount"]) for row in rows if row["settlement_type"] == "CREDIT"),
        Decimal("0.00"),
    )
    net_total = debit_total - credit_total
    if transaction.get("status", "COMPLETED").upper() != "COMPLETED":
        settlement_status = "NOT_APPLICABLE"
    elif not rows:
        settlement_status = "PENDING"
    elif net_total > amount:
        settlement_status = "OVER_SETTLED"
    elif net_total == amount:
        settlement_status = "FULLY_SETTLED"
    elif credit_total > 0:
        settlement_status = "REFUNDED"
    else:
        settlement_status = "PARTIAL"

    last_settlement_date = max(
        (str(row["settlement_date"]) for row in rows), default=None
    )
    return {
        "debit_total": debit_total,
        "credit_total": credit_total,
        "net_total": net_total,
        "settlement_status": settlement_status,
        "last_settlement_date": last_settlement_date,
        "outstanding_amount": (
            max(amount - debit_total, Decimal("0.00"))
            if transaction.get("status", "COMPLETED").upper() == "COMPLETED"
            else Decimal("0.00")
        ),
    }


def issue_flags(
    transaction: dict[str, Any],
    settlements: Iterable[dict[str, Any]],
    as_of: date | None = None,
) -> list[dict[str, str]]:
    today = as_of or date.today()
    rows = list(settlements)
    totals = aggregate_settlements(transaction, rows)
    amount = parse_money(transaction["transaction_amount"])
    flags: list[dict[str, str]] = []

    if transaction.get("status", "").upper() != "COMPLETED":
        if rows:
            flags.append({
                "code": "unexpected_settlement",
                "severity": "critical",
                "message": "A settlement is linked to a transaction that did not complete.",
                "recommendation": "Confirm the payment outcome and reverse or reassign the settlement if needed.",
            })
        return flags

    if not rows:
        try:
            age_days = (today - date.fromisoformat(str(transaction["transaction_date"]))).days
        except ValueError:
            age_days = 0
        if age_days > 7:
            flags.append({
                "code": "settlement_overdue",
                "severity": "critical",
                "message": f"No settlement has been recorded {age_days} days after the transaction.",
                "recommendation": "Check the settlement provider report and confirm whether this transaction was included.",
            })

    if totals["net_total"] > amount:
        flags.append({
            "code": "over_settled",
            "severity": "critical",
            "message": f"Net settlement exceeds the transaction by {money_string(totals['net_total'] - amount)} {transaction['currency']}.",
            "recommendation": "Review duplicate or incorrect debit records and recover any excess funds.",
        })
    elif rows and totals["net_total"] < amount and totals["debit_total"] < amount:
        flags.append({
            "code": "under_settled",
            "severity": "warning",
            "message": f"Recorded debits are {money_string(amount - totals['debit_total'])} {transaction['currency']} below the transaction amount.",
            "recommendation": "Check for a missing settlement row or a settlement still in transit.",
        })
    return flags


def settlement_days_to_target(
    transaction: dict[str, Any], settlements: Iterable[dict[str, Any]]
) -> int | None:
    if transaction.get("status", "COMPLETED").upper() != "COMPLETED":
        return None
    target = parse_money(transaction["transaction_amount"])
    debit_total = Decimal("0.00")
    try:
        transaction_date = date.fromisoformat(str(transaction["transaction_date"]))
    except ValueError:
        return None
    rows = sorted(
        (row for row in settlements if row["settlement_type"] == "DEBIT"),
        key=lambda row: (str(row["settlement_date"]), str(row.get("settlement_id", ""))),
    )
    for row in rows:
        debit_total += parse_money(row["settlement_amount"])
        if debit_total >= target:
            try:
                return max(0, (date.fromisoformat(str(row["settlement_date"])) - transaction_date).days)
            except ValueError:
                return None
    return None

