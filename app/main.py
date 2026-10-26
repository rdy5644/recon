from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import database_connection, database_path, initialize_database
from .domain import (
    aggregate_settlements,
    issue_flags,
    money_number,
    money_string,
    parse_money,
    settlement_days_to_target,
)
from .reconciliation import (
    MAX_CSV_BYTES,
    MAX_UPLOAD_SIZE_ERROR,
    MAX_UPLOAD_SIZE_LABEL,
    ReconciliationConflict,
    ReconciliationInputError,
    reconcile_csv,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"
VALID_SETTLEMENT_STATUSES = {
    "PENDING", "PARTIAL", "FULLY_SETTLED", "OVER_SETTLED", "REFUNDED", "NOT_APPLICABLE"
}


def _dict_rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _settlements_for(connection, transaction_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT settlement_id, transaction_id, lifecycle_id, settlement_date,
                  settlement_amount, settlement_type, currency, processed_at
           FROM settlement_history WHERE transaction_id = ?
           ORDER BY settlement_date, settlement_id""",
        (transaction_id,),
    ).fetchall()
    result = _dict_rows(rows)
    for row in result:
        row["settlement_amount"] = money_number(row["settlement_amount"])
    return result


def _transaction_view(transaction: dict[str, Any], settlements: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
    totals = aggregate_settlements(transaction, settlements)
    issues = issue_flags(transaction, settlements, as_of)
    severity = "critical" if any(item["severity"] == "critical" for item in issues) else (
        "warning" if issues else "none"
    )
    transaction_date = date.fromisoformat(str(transaction["transaction_date"]))
    return {
        **transaction,
        "transaction_amount": money_number(transaction["transaction_amount"]),
        "total_settled_amount": money_number(totals["net_total"]),
        "gross_debit_amount": money_number(totals["debit_total"]),
        "total_credits": money_number(totals["credit_total"]),
        "outstanding_amount": money_number(totals["outstanding_amount"]),
        "settlement_status": totals["settlement_status"],
        "last_settlement_date": totals["last_settlement_date"],
        "settlement_count": len(settlements),
        "issue_flag": severity.upper() if severity != "none" else "NONE",
        "issue_severity": severity,
        "issues": issues,
        "days_since_transaction": max(0, (as_of - transaction_date).days),
        "settlements": settlements,
    }


def _all_transaction_views(connection, as_of: date | None = None) -> list[dict[str, Any]]:
    reference_date = as_of or date.today()
    transactions = _dict_rows(connection.execute(
        "SELECT * FROM transactions ORDER BY transaction_date DESC, transaction_id"
    ).fetchall())
    views = []
    for transaction in transactions:
        settlements = _settlements_for(connection, str(transaction["transaction_id"]))
        views.append(_transaction_view(transaction, settlements, reference_date))
    return views


def _summary(views: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = {status: 0 for status in sorted(VALID_SETTLEMENT_STATUSES)}
    critical_ids: set[str] = set()
    warning_ids: set[str] = set()
    outstanding: dict[str, Decimal] = {}
    settlement_days: list[int] = []
    eligible_count = 0
    reached_target_count = 0
    settlement_count = 0

    for view in views:
        status = view["settlement_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        settlement_count += int(view["settlement_count"])
        for issue in view["issues"]:
            if issue["severity"] == "critical":
                critical_ids.add(view["transaction_id"])
            elif issue["severity"] == "warning":
                warning_ids.add(view["transaction_id"])

        if str(view["status"]).upper() == "COMPLETED":
            eligible_count += 1
            debit_amount = parse_money(view["gross_debit_amount"])
            target = parse_money(view["transaction_amount"])
            if debit_amount >= target:
                reached_target_count += 1
                days = settlement_days_to_target(view, view["settlements"])
                if days is not None:
                    settlement_days.append(days)
            currency = str(view["currency"]).upper()
            outstanding[currency] = outstanding.get(currency, Decimal("0.00")) + parse_money(view["outstanding_amount"])

    single_currency = next(iter(outstanding)) if len(outstanding) == 1 else None
    return {
        "transactions_processed": len(views),
        "settlements_processed": settlement_count,
        "settlement_status_breakdown": {key.lower(): value for key, value in status_counts.items()},
        "critical_issue_count": len(critical_ids),
        "warning_count": len(warning_ids),
        "total_outstanding_amount": money_number(outstanding[single_currency]) if single_currency else None,
        "outstanding_currency": single_currency,
        "outstanding_by_currency": {currency: money_number(amount) for currency, amount in sorted(outstanding.items())},
        "average_days_to_settle": round(sum(settlement_days) / len(settlement_days), 2) if settlement_days else None,
        "settlement_rate": round(reached_target_count / eligible_count, 4) if eligible_count else 0,
        "settlement_rate_percent": round(reached_target_count / eligible_count * 100, 1) if eligible_count else 0,
        "completed_transactions": eligible_count,
        "transactions_reaching_debit_target": reached_target_count,
    }


def create_app(db_path: str | Path | None = None) -> FastAPI:
    resolved_db_path = database_path(db_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        initialize_database(resolved_db_path)
        yield

    application = FastAPI(
        title="Transaction Reconciliation System",
        description="Import settlement reports, match transactions, and review reconciliation issues.",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.database_path = resolved_db_path
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/health")
    def health():
        return {"status": "ok"}

    @application.get("/config", summary="Public dashboard configuration")
    def public_config():
        return {
            "max_upload_size_bytes": MAX_CSV_BYTES,
            "max_upload_size_label": MAX_UPLOAD_SIZE_LABEL,
        }

    @application.post(
        "/reconcile",
        summary="Import and reconcile a settlement CSV",
        description="Upload a multipart file using the `file` field, or send the CSV as a raw UTF-8 request body.",
        responses={409: {"description": "Settlement ID conflict"}, 413: {"description": "Upload too large"}, 422: {"description": "CSV validation failed"}},
    )
    async def reconcile(request: Request):
        content_type = request.headers.get("content-type", "").lower()
        try:
            if "multipart/form-data" in content_type:
                form = await request.form()
                upload = form.get("file") or form.get("settlement_report")
                if upload is None or not hasattr(upload, "read"):
                    raise ReconciliationInputError("Attach a CSV file in the `file` field.")
                contents = await upload.read(MAX_CSV_BYTES + 1)
                if len(contents) > MAX_CSV_BYTES:
                    raise HTTPException(status_code=413, detail=MAX_UPLOAD_SIZE_ERROR)
            elif "application/json" in content_type:
                try:
                    payload = await request.json()
                except ValueError as error:
                    raise ReconciliationInputError("JSON request body is malformed.") from error
                if not isinstance(payload, dict) or not isinstance(payload.get("csv"), str):
                    raise ReconciliationInputError("JSON requests must contain a string `csv` property.")
                try:
                    contents = payload["csv"].encode("utf-8")
                except UnicodeEncodeError as error:
                    raise ReconciliationInputError("CSV must contain valid UTF-8 text.") from error
            else:
                contents = await request.body()
            if len(contents) > MAX_CSV_BYTES:
                raise HTTPException(status_code=413, detail=MAX_UPLOAD_SIZE_ERROR)
        except ReconciliationInputError as error:
            return JSONResponse(status_code=422, content={"detail": str(error), "errors": error.errors})

        try:
            with database_connection(request.app.state.database_path) as connection:
                result = reconcile_csv(connection, contents)
            return result
        except ReconciliationInputError as error:
            return JSONResponse(status_code=422, content={"detail": str(error), "errors": error.errors})
        except ReconciliationConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/transactions")
    def list_transactions(
        request: Request,
        settlement_status: str | None = Query(default=None),
        issue: str | None = Query(default=None, pattern="^(critical|warning|none)$"),
        search: str | None = Query(default=None, max_length=120),
        currency: str | None = Query(default=None, min_length=3, max_length=3, pattern="^[A-Za-z]{3}$"),
        transaction_date_from: str | None = Query(default=None),
        transaction_date_to: str | None = Query(default=None),
        last_settlement_date_from: str | None = Query(default=None),
        last_settlement_date_to: str | None = Query(default=None),
        amount_min: float | None = Query(default=None, ge=0),
        amount_max: float | None = Query(default=None, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        if settlement_status and settlement_status.upper() not in VALID_SETTLEMENT_STATUSES:
            raise HTTPException(status_code=422, detail=f"Unknown settlement status: {settlement_status}")
        def filter_date(value: str | None, label: str) -> date | None:
            if not value:
                return None
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError
            except ValueError as error:
                raise HTTPException(status_code=422, detail=f"{label} must use YYYY-MM-DD format.") from error
            return parsed

        transaction_from = filter_date(transaction_date_from, "transaction_date_from")
        transaction_to = filter_date(transaction_date_to, "transaction_date_to")
        settlement_from = filter_date(last_settlement_date_from, "last_settlement_date_from")
        settlement_to = filter_date(last_settlement_date_to, "last_settlement_date_to")
        for lower, upper, label in (
            (transaction_from, transaction_to, "transaction date"),
            (settlement_from, settlement_to, "last settlement date"),
        ):
            if lower and upper and lower > upper:
                raise HTTPException(status_code=422, detail=f"The start of the {label} range must be on or before its end.")
        if amount_min is not None and amount_max is not None and amount_min > amount_max:
            raise HTTPException(status_code=422, detail="Minimum amount must be less than or equal to maximum amount.")
        with database_connection(request.app.state.database_path) as connection:
            views = _all_transaction_views(connection)
        if settlement_status:
            wanted = settlement_status.upper()
            views = [view for view in views if view["settlement_status"] == wanted]
        if issue:
            wanted_issue = issue.lower()
            views = [view for view in views if view["issue_severity"] == wanted_issue]
        if search:
            query = search.casefold().strip()
            views = [view for view in views if any(
                query in str(view.get(field) or "").casefold()
                for field in ("transaction_id", "lifecycle_id", "merchant_name", "account_id")
            )]
        if currency:
            wanted_currency = currency.upper()
            views = [view for view in views if str(view["currency"]).upper() == wanted_currency]
        if transaction_from:
            views = [view for view in views if date.fromisoformat(str(view["transaction_date"])) >= transaction_from]
        if transaction_to:
            views = [view for view in views if date.fromisoformat(str(view["transaction_date"])) <= transaction_to]
        if settlement_from:
            views = [view for view in views if view["last_settlement_date"] and date.fromisoformat(str(view["last_settlement_date"])) >= settlement_from]
        if settlement_to:
            views = [view for view in views if view["last_settlement_date"] and date.fromisoformat(str(view["last_settlement_date"])) <= settlement_to]
        if amount_min is not None:
            minimum = Decimal(str(amount_min))
            views = [view for view in views if Decimal(str(view["transaction_amount"])) >= minimum]
        if amount_max is not None:
            maximum = Decimal(str(amount_max))
            views = [view for view in views if Decimal(str(view["transaction_amount"])) <= maximum]
        total = len(views)
        return {"items": views[offset:offset + limit], "total": total, "limit": limit, "offset": offset}

    @application.get("/transactions/{transaction_id}")
    def transaction_detail(transaction_id: str, request: Request):
        with database_connection(request.app.state.database_path) as connection:
            transaction = connection.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
            ).fetchone()
            if transaction is None:
                raise HTTPException(status_code=404, detail="Transaction not found.")
            settlements = _settlements_for(connection, transaction_id)
            view = _transaction_view(dict(transaction), settlements, date.today())

        running = Decimal("0.00")
        timeline = []
        for settlement in settlements:
            value = parse_money(settlement["settlement_amount"])
            running += value if settlement["settlement_type"] == "DEBIT" else -value
            timeline.append({
                "date": settlement["settlement_date"],
                "settlement_id": settlement["settlement_id"],
                "settlement_type": settlement["settlement_type"],
                "amount": settlement["settlement_amount"],
                "net_settled_after_event": money_number(running),
            })
        view["timeline"] = timeline
        return view

    @application.get("/dashboard/summary")
    def dashboard_summary(request: Request):
        with database_connection(request.app.state.database_path) as connection:
            views = _all_transaction_views(connection)
            unmatched_count = connection.execute("SELECT COUNT(*) FROM unmatched_settlements").fetchone()[0]
        summary = _summary(views)
        summary["unmatched_settlement_count"] = unmatched_count
        return summary

    @application.get("/unmatched-settlements")
    def list_unmatched_settlements(request: Request, limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
        with database_connection(request.app.state.database_path) as connection:
            total = connection.execute("SELECT COUNT(*) FROM unmatched_settlements").fetchone()[0]
            rows = _dict_rows(connection.execute(
                "SELECT * FROM unmatched_settlements ORDER BY last_seen_at DESC, settlement_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall())
        for row in rows:
            row["settlement_amount"] = money_number(row["settlement_amount"])
        return {"items": rows, "total": total, "limit": limit, "offset": offset}

    @application.get("/dashboard/merchants")
    def dashboard_merchants(request: Request):
        response={}
        with database_connection(request.app.state.database_path) as connection:
            merchant_names = connection.execute("SELECT merchant_name from transactions").fetchall()
            for merchant_name in merchant_names:
                merchant_name = merchant_name[0]
                transactions_rows = connection.execute("SELECT * from transactions where merchant_name = ?",(merchant_name,)).fetchall()
                total_transactions_count = len(transactions_rows)
                net_settled = 0
                total_transaction_amount = 0
                critical_issue_count = 0
                for row in transactions_rows:
                    row = dict(row)
                    total_transaction_amount += parse_money(row["transaction_amount"])
                    net_settled += parse_money(row["total_settled_amount"])
                    settlement_rows = _settlements_for(connection, row["transaction_id"])
                    issues = issue_flags(row, settlement_rows, as_of=date.today())
                    severity = "critical" if any(item["severity"] == "critical" for item in issues) else (
                        "warning" if issues else "none"
                    )
                    if severity == "critical":
                        critical_issue_count += 1
                outstanding = total_transaction_amount - net_settled
                response[merchant_name] = {
                    "total_transactions_count": total_transactions_count,
                    "net_settled": money_number(net_settled),
                    "total_transaction_amount": money_number(total_transaction_amount),
                    "outstanding": money_number(outstanding),
                    "critical_issue_count": critical_issue_count
                }
        return response

    return application


app = create_app()

