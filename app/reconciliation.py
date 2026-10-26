from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import MAX_AMOUNT, CENT, money_string, normalize_merchant


REQUIRED_COLUMNS = {
    "settlement_id", "lifecycle_id", "account_id", "merchant_name", "transaction_date",
    "settlement_date", "settlement_amount", "settlement_type", "currency",
}


def _configured_upload_size_mb() -> int:
    raw_value = os.environ.get("MAX_UPLOAD_SIZE_MB", "5").strip()
    try:
        size_mb = int(raw_value)
    except ValueError as error:
        raise RuntimeError("MAX_UPLOAD_SIZE_MB must be a positive whole number.") from error
    if size_mb <= 0:
        raise RuntimeError("MAX_UPLOAD_SIZE_MB must be a positive whole number.")
    return size_mb


MAX_UPLOAD_SIZE_MB = _configured_upload_size_mb()
MAX_CSV_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
MAX_UPLOAD_SIZE_LABEL = f"{MAX_UPLOAD_SIZE_MB} MB"
MAX_UPLOAD_SIZE_ERROR = f"CSV exceeds the {MAX_UPLOAD_SIZE_LABEL} upload limit."
MONEY_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")


class ReconciliationInputError(Exception):
    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.errors = errors or []


class ReconciliationConflict(Exception):
    pass


@dataclass(frozen=True)
class SettlementInput:
    settlement_id: str
    lifecycle_id: str | None
    account_id: str
    merchant_name: str
    transaction_date: str
    settlement_date: str
    settlement_amount: Decimal
    settlement_type: str
    currency: str

    def signature(self) -> tuple[Any, ...]:
        return (
            self.lifecycle_id, self.account_id, normalize_merchant(self.merchant_name),
            self.transaction_date, self.settlement_date, self.settlement_amount,
            self.settlement_type, self.currency,
        )


def _read_date(value: str, column: str, row_number: int, errors: list[dict[str, Any]]) -> str | None:
    value = value.strip()
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
    except ValueError:
        errors.append({"row": row_number, "field": column, "message": "Use a valid YYYY-MM-DD date."})
        return None
    return value


def parse_csv(contents: bytes) -> tuple[list[SettlementInput], int]:
    if len(contents) > MAX_CSV_BYTES:
        raise ReconciliationInputError(MAX_UPLOAD_SIZE_ERROR)
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ReconciliationInputError("CSV must be UTF-8 encoded.") from error
    if not text.strip():
        raise ReconciliationInputError("CSV is empty.")

    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ReconciliationInputError("CSV header is malformed.") from error
    if fieldnames is None:
        raise ReconciliationInputError("CSV header row is missing.")
    if any(header is None for header in fieldnames):
        raise ReconciliationInputError("CSV contains an empty column name.")
    reader.fieldnames = [header.strip() for header in fieldnames]
    if len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ReconciliationInputError("CSV contains duplicate column names.")
    headers = set(reader.fieldnames)
    missing = sorted(REQUIRED_COLUMNS - headers)
    if missing:
        raise ReconciliationInputError("CSV is missing required columns.", [
            {"field": field, "message": "Required CSV column is missing."} for field in missing
        ])

    records: list[SettlementInput] = []
    errors: list[dict[str, Any]] = []
    seen_ids: dict[str, tuple[Any, ...]] = {}
    duplicate_rows = 0
    row_number = 1
    while True:
        try:
            raw = next(reader)
        except StopIteration:
            break
        except csv.Error as error:
            errors.append({"row": row_number + 1, "message": f"Malformed CSV: {error}."})
            break
        row_number += 1
        if None in raw:
            errors.append({"row": row_number, "message": "Row has more values than the header."})
            continue
        row = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
        required_text = ("settlement_id", "account_id", "merchant_name", "settlement_type", "currency")
        lifecycle_id = row.get("lifecycle_id") or None
        row_failed = False
        for field in required_text:
            if not row.get(field):
                errors.append({"row": row_number, "field": field, "message": "Value is required."})
                row_failed = True
        if row_failed:
            continue

        for field, maximum in (("settlement_id", 50), ("account_id", 50), ("merchant_name", 100)):
            if len(row[field]) > maximum:
                errors.append({"row": row_number, "field": field, "message": f"Value must be at most {maximum} characters."})
                row_failed = True
        if lifecycle_id and len(lifecycle_id) > 50:
            errors.append({"row": row_number, "field": "lifecycle_id", "message": "Value must be at most 50 characters."})
            row_failed = True
        if row_failed:
            continue

        settlement_id = row["settlement_id"]
        transaction_date = _read_date(row.get("transaction_date", ""), "transaction_date", row_number, errors)
        settlement_date = _read_date(row.get("settlement_date", ""), "settlement_date", row_number, errors)
        try:
            raw_amount = row.get("settlement_amount", "")
            if not MONEY_PATTERN.fullmatch(raw_amount):
                raise InvalidOperation
            amount = Decimal(raw_amount).quantize(CENT)
            if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors.append({"row": row_number, "field": "settlement_amount", "message": "Use a positive amount with at most two decimal places."})
            amount = Decimal("0.00")
        settlement_type = row["settlement_type"].upper()
        if settlement_type not in {"DEBIT", "CREDIT"}:
            errors.append({"row": row_number, "field": "settlement_type", "message": "Use DEBIT or CREDIT."})
        currency = row["currency"].upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            errors.append({"row": row_number, "field": "currency", "message": "Use a three-letter currency code."})
        if not transaction_date or not settlement_date or settlement_type not in {"DEBIT", "CREDIT"} or len(currency) != 3 or amount <= 0:
            continue
        # Commented to complete the task to show Warning for settlement date before transaction date instead of Critical
        # if settlement_date < transaction_date:
        #     errors.append({"row": row_number, "field": "settlement_date", "message": "Settlement date cannot precede transaction date."})
        #     continue

        record = SettlementInput(
            settlement_id=settlement_id,
            lifecycle_id=lifecycle_id,
            account_id=row["account_id"],
            merchant_name=row["merchant_name"],
            transaction_date=transaction_date,
            settlement_date=settlement_date,
            settlement_amount=amount,
            settlement_type=settlement_type,
            currency=currency,
        )
        if settlement_id in seen_ids:
            if seen_ids[settlement_id] != record.signature():
                errors.append({"row": row_number, "field": "settlement_id", "message": "Settlement ID appears more than once with different values."})
            else:
                duplicate_rows += 1
            continue
        seen_ids[settlement_id] = record.signature()
        records.append(record)

    if errors:
        raise ReconciliationInputError("CSV contains invalid rows; no settlement records were imported.", errors)
    return records, duplicate_rows


def _match_transaction(connection: sqlite3.Connection, row: SettlementInput) -> tuple[sqlite3.Row | None, str | None]:
    if row.lifecycle_id:
        matches = connection.execute(
            "SELECT * FROM transactions WHERE lifecycle_id = ? ORDER BY transaction_id",
            (row.lifecycle_id,),
        ).fetchall()
        if len(matches) == 0:
            return None, "LIFECYCLE_NOT_FOUND"
        if len(matches) > 1:
            return None, "AMBIGUOUS_LIFECYCLE"
        transaction = matches[0]
    else:
        candidates = connection.execute(
            "SELECT * FROM transactions WHERE account_id = ? AND transaction_date = ? ORDER BY transaction_id",
            (row.account_id, row.transaction_date),
        ).fetchall()
        matches = [candidate for candidate in candidates if normalize_merchant(candidate["merchant_name"]) == normalize_merchant(row.merchant_name)]
        if len(matches) == 0:
            return None, "FALLBACK_MATCH_NOT_FOUND"
        if len(matches) > 1:
            return None, "AMBIGUOUS_FALLBACK_MATCH"
        transaction = matches[0]

    if transaction["account_id"] != row.account_id or normalize_merchant(transaction["merchant_name"]) != normalize_merchant(row.merchant_name) or str(transaction["transaction_date"]) != row.transaction_date:
        return None, "LIFECYCLE_ATTRIBUTE_MISMATCH"
    if str(transaction["currency"]).upper() != row.currency:
        raise ReconciliationInputError(
            f"Currency mismatch for settlement {row.settlement_id}.",
            [{"settlement_id": row.settlement_id, "expected": transaction["currency"], "received": row.currency}],
        )
    return transaction, None


def _stored_settlement_signature(row: sqlite3.Row) -> tuple[Any, ...]:
    return (
        str(row["settlement_date"]), Decimal(str(row["settlement_amount"])).quantize(CENT),
        str(row["settlement_type"]).upper(), str(row["currency"]).upper(),
    )


def reconcile_csv(connection: sqlite3.Connection, contents: bytes) -> dict[str, Any]:
    rows, duplicate_file_rows = parse_csv(contents)
    mapped: list[tuple[SettlementInput, sqlite3.Row]] = []
    unmatched: list[tuple[SettlementInput, str]] = []
    for row in rows:
        transaction, reason = _match_transaction(connection, row)
        if transaction is None:
            unmatched.append((row, reason or "UNMATCHED"))
        else:
            mapped.append((row, transaction))

    inserted_count = 0
    duplicate_count = duplicate_file_rows
    affected_ids: set[str] = set()
    unmatched_result: list[dict[str, str]] = []
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for row, transaction in mapped:
            prior = connection.execute(
                "SELECT * FROM settlement_history WHERE settlement_id = ?", (row.settlement_id,)
            ).fetchone()
            expected = (row.settlement_date, row.settlement_amount, row.settlement_type, row.currency)
            if prior:
                same = str(prior["transaction_id"]) == str(transaction["transaction_id"]) and _stored_settlement_signature(prior) == expected
                if not same:
                    raise ReconciliationConflict(f"Settlement ID {row.settlement_id} already exists with different data.")
                duplicate_count += 1
                affected_ids.add(str(transaction["transaction_id"]))
                continue

            prior_unmatched = connection.execute(
                "SELECT * FROM unmatched_settlements WHERE settlement_id = ?", (row.settlement_id,)
            ).fetchone()
            if prior_unmatched:
                same_unmatched = (
                    str(prior_unmatched["account_id"]) == row.account_id
                    and normalize_merchant(str(prior_unmatched["merchant_name"])) == normalize_merchant(row.merchant_name)
                    and str(prior_unmatched["transaction_date"]) == row.transaction_date
                    and str(prior_unmatched["settlement_date"]) == row.settlement_date
                    and Decimal(str(prior_unmatched["settlement_amount"])).quantize(CENT) == row.settlement_amount
                    and str(prior_unmatched["settlement_type"]).upper() == row.settlement_type
                    and str(prior_unmatched["currency"]).upper() == row.currency
                )
                if not same_unmatched:
                    raise ReconciliationConflict(f"Settlement ID {row.settlement_id} already exists with different data.")
                connection.execute("DELETE FROM unmatched_settlements WHERE settlement_id = ?", (row.settlement_id,))
            connection.execute(
                """INSERT INTO settlement_history
                   (settlement_id, transaction_id, lifecycle_id, settlement_date, settlement_amount, settlement_type, currency)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (row.settlement_id, transaction["transaction_id"], row.lifecycle_id or transaction["lifecycle_id"],
                 row.settlement_date, money_string(row.settlement_amount), row.settlement_type, row.currency),
            )
            inserted_count += 1
            affected_ids.add(str(transaction["transaction_id"]))

        for row, reason in unmatched:
            existing_history = connection.execute(
                "SELECT settlement_id FROM settlement_history WHERE settlement_id = ?", (row.settlement_id,)
            ).fetchone()
            if existing_history:
                raise ReconciliationConflict(f"Settlement ID {row.settlement_id} is already linked to a transaction.")
            existing = connection.execute(
                "SELECT * FROM unmatched_settlements WHERE settlement_id = ?", (row.settlement_id,)
            ).fetchone()
            if existing:
                same = (
                    str(existing["settlement_date"]) == row.settlement_date
                    and Decimal(str(existing["settlement_amount"])).quantize(CENT) == row.settlement_amount
                    and str(existing["settlement_type"]).upper() == row.settlement_type
                    and str(existing["currency"]).upper() == row.currency
                    and str(existing["account_id"]) == row.account_id
                    and normalize_merchant(str(existing["merchant_name"])) == normalize_merchant(row.merchant_name)
                    and str(existing["transaction_date"]) == row.transaction_date
                )
                if not same:
                    raise ReconciliationConflict(f"Unmatched settlement ID {row.settlement_id} already exists with different data.")
                connection.execute(
                    "UPDATE unmatched_settlements SET reason = ?, last_seen_at = CURRENT_TIMESTAMP WHERE settlement_id = ?",
                    (reason, row.settlement_id),
                )
                duplicate_count += 1
            else:
                connection.execute(
                    """INSERT INTO unmatched_settlements
                       (settlement_id, lifecycle_id, account_id, merchant_name, transaction_date, settlement_date,
                        settlement_amount, settlement_type, currency, reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row.settlement_id, row.lifecycle_id, row.account_id, row.merchant_name, row.transaction_date,
                     row.settlement_date, money_string(row.settlement_amount), row.settlement_type, row.currency, reason),
                )
            unmatched_result.append({"settlement_id": row.settlement_id, "reason": reason})

        for transaction_id in affected_ids:
            transaction = connection.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
            ).fetchone()
            settlements = connection.execute(
                "SELECT settlement_type, settlement_amount, settlement_date FROM settlement_history WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchall()
            debits = sum((Decimal(str(item["settlement_amount"])) for item in settlements if item["settlement_type"] == "DEBIT"), Decimal("0.00"))
            credits = sum((Decimal(str(item["settlement_amount"])) for item in settlements if item["settlement_type"] == "CREDIT"), Decimal("0.00"))
            net = debits - credits
            last_date = max((str(item["settlement_date"]) for item in settlements), default=None)
            transaction_amount = Decimal(str(transaction["transaction_amount"])).quantize(CENT)
            if str(transaction["status"]).upper() != "COMPLETED":
                status = "NOT_APPLICABLE"
            elif not settlements:
                status = "PENDING"
            elif net > transaction_amount:
                status = "OVER_SETTLED"
            elif net == transaction_amount:
                status = "FULLY_SETTLED"
            elif credits > 0:
                status = "REFUNDED"
            else:
                status = "PARTIAL"
            connection.execute(
                "UPDATE transactions SET settlement_status = ?, total_settled_amount = ?, last_settlement_date = ? WHERE transaction_id = ?",
                (status, money_string(net), last_date, transaction_id),
            )

    return {
        "settlements_received": len(rows) + duplicate_file_rows,
        "settlements_added": inserted_count,
        "duplicates_skipped": duplicate_count,
        "unmatched_count": len(unmatched_result),
        "unmatched_settlements": unmatched_result,
        "transactions_updated": len(affected_ids),
    }

