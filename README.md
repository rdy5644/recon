# Transaction Reconciliation System

A small, local reconciliation service for importing settlement CSVs, matching provider records to transactions, and reviewing exceptions in a responsive operations dashboard.

## What is included

- Python REST API built with FastAPI and SQLite.
- A browser dashboard for summary metrics, transaction search and filters, transaction detail, settlement history, issue recommendations, and a settlement timeline.
- Advanced transaction filtering by status, issue, currency, transaction date, last settlement date, and amount range; filtered rows can be exported as CSV or PDF reports.
- CSV validation, lifecycle matching with composite-key fallback, duplicate-safe imports, debit and credit aggregation, and a retained unmatched-settlement queue.
- SQL schema and sample seed data based on the exercise, plus unit and API tests.

## Run locally

Python 3.10 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/init_db.py --seed
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) for the dashboard. The interactive API reference is at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

The sample transactions are inserted only when their transaction IDs are absent. The import file at `data/settlement_report.csv` is copied from the exercise examples. Upload it in the dashboard or send it to the API:

```bash
curl -X POST http://127.0.0.1:8000/reconcile \
  -H 'Content-Type: text/csv' \
  --data-binary @data/settlement_report.csv
```

The upload limit defaults to 5 MB and can be changed with the `MAX_UPLOAD_SIZE_MB` environment variable. It must be a positive whole number and is read when the service starts. For example, run `MAX_UPLOAD_SIZE_MB=25 uvicorn app.main:app --reload` to allow uploads up to 25 MB. The dashboard reads the configured limit from the service, and the API returns HTTP 413 when a request exceeds it.

To use an existing exercise database, set `DATABASE_PATH` to its path before starting the application. The service creates any missing reconciliation tables and indexes without deleting existing rows. The database defaults to `data/reconciliation.db`.

## Reconciliation rules

1. A non-empty `lifecycle_id` is the primary match key. It must identify exactly one transaction, and the accompanying account, merchant, and transaction date must agree. A typo in a supplied lifecycle ID is retained as unmatched instead of silently falling back to a different transaction.
2. When `lifecycle_id` is blank, the service matches on `account_id`, normalized `merchant_name`, and `transaction_date`. Ambiguous matches are not guessed.
3. Matched settlements are stored in `settlement_history`. The settlement ID is unique. An identical re-import is skipped; a different row using an existing ID is rejected with HTTP 409.
4. The import is atomic for malformed data: any invalid CSV row rejects the file before records are written. Valid but unmatchable provider rows are stored in `unmatched_settlements` and listed for review.
5. DEBIT amounts increase settled value; CREDIT amounts reduce net settled value. Currency must match the linked transaction. Amount arithmetic uses decimal values and stores two decimal places.
6. Completed transactions with no settlements are `PENDING`; net below the transaction with no refund is `PARTIAL`; equal net is `FULLY_SETTLED`; net above is `OVER_SETTLED`; and a credit that leaves net below the purchase is `REFUNDED`. Non-completed transaction states use `NOT_APPLICABLE` and are flagged if a settlement is linked.
7. A refund below the original purchase is not automatically treated as an under-settlement warning when the original debit amount reached the purchase amount. This reconciles the exercise's refund scenarios, which expect a refund status and no shortfall issue. If the debit total itself is below the purchase, the warning remains.
8. An unsettled completed transaction becomes critical only after more than seven calendar days. The threshold is evaluated against the service's local date.
9. Outstanding amount measures the original debit amount not yet received, capped at zero. It does not treat an intentional credit/refund as a collection shortfall, and it is zero for non-completed transactions. The dashboard groups outstanding balances by currency; the aggregate field is `null` when there is more than one currency, since unlike currencies must not be added together.
10. Average days to settle and settlement rate use the first date cumulative debits reached the original transaction amount. Refunds do not erase the fact that the original settlement target was reached.

## API

| Method and path | Purpose |
| --- | --- |
| `POST /reconcile` | Import CSV as multipart field `file` (or `settlement_report`), raw `text/csv`, or JSON `{ "csv": "..." }`. Returns inserted, duplicate, unmatched, and updated counts. |
| `GET /transactions` | List/filter transactions. Supports `settlement_status`, `issue=critical\|warning\|none`, `search` across transaction/lifecycle/account/merchant IDs, `currency`, `transaction_date_from`, `transaction_date_to`, `last_settlement_date_from`, `last_settlement_date_to`, `amount_min`, `amount_max`, `limit` (1–500), and `offset`. The dashboard and overview provide previous/next paging so every matching transaction remains reachable. |
| `GET /transactions/{id}` | Transaction, issues and recommendations, settlement history, and a net settlement timeline. |
| `GET /dashboard/summary` | Counts by settlement status, issue counts, outstanding amount by currency, average days to settle, and settlement rate. |
| `GET /unmatched-settlements` | Review retained unmatched provider rows and their match reason. |
| `GET /health` | Liveness check. |
| `GET /config` | Public dashboard configuration, including the active upload size limit. |

Transaction and last-settlement date bounds are inclusive and use `YYYY-MM-DD`. Last-settlement filters use each transaction's most recent recorded settlement date; a transaction without settlement history is excluded when either bound is set.

CSV fields are `settlement_id,lifecycle_id,account_id,merchant_name,transaction_date,settlement_date,settlement_amount,settlement_type,currency`. Dates must be ISO `YYYY-MM-DD`; amounts must be positive with at most two decimal places; settlement type must be `DEBIT` or `CREDIT`; currency must be a three-letter code. The upload limit defaults to 5 MB and is configurable with `MAX_UPLOAD_SIZE_MB`. A blank lifecycle value is valid.

Validation errors return HTTP 422 with row/field details and do not partially import the file. Conflicting settlement IDs return HTTP 409. Unknown or ambiguous transaction matches are retained and reported as unmatched rather than discarded.

## Architecture

- `app/main.py` defines the API, dashboard routes, response shaping, summary metrics, and static-file serving.
- `app/reconciliation.py` parses and validates CSV, applies matching rules, performs an atomic import, stores unmatched rows, and updates transaction settlement fields.
- `app/domain.py` contains money aggregation, settlement-status rules, issue detection, and settlement-performance calculations.
- `app/database.py` opens SQLite connections and applies `sql/schema.sql` at startup.
- `app/static/` contains the responsive dashboard. It uses the API directly and does not require a separate frontend build step.
- `sql/seed.sql` and `data/settlement_report.csv` provide the exercise sample.

## Tests

Install the development dependencies and run:

```bash
pip install -r requirements-dev.txt
pytest
```

The test suite includes direct checks for all seven settlement examples in the exercise: exact, short and excess settlement; refunds; composite-key fallback; and staged partial-to-full settlement. It also imports the full supplied seed/report pair and checks its matched and unmatched outcomes. Other checks cover the seven-day overdue boundary, non-completed transactions, decimal and CSV validation, malformed and oversized uploads, UTF-8 BOM and invalid Unicode handling, all three upload formats, duplicate and conflicting settlement IDs, rollback on conflicts, ambiguous and mismatched matches, unmatched-row recovery, API pagination, advanced filters, settlement timelines, dashboard metrics, and currency-separated outstanding balances.

## Known limitations

- This is a local assessment implementation and has no authentication or role-based access control.
- SQLite is appropriate for the exercise and a single-operator workflow; high write concurrency would call for a server database and background import jobs.
- Currency consistency is enforced, but no currency conversion or cross-currency netting is performed.
- Unknown provider rows are retained and visible, but they must be corrected in the source and re-imported; there is no manual transaction-linking workflow.
- Settlement dates use date-only values, so timezone and intraday settlement cutoffs are outside the current model.
- The dashboard exports the current filtered transaction set as CSV or a landscape PDF report. Unmatched settlement records remain available for review but are not included in transaction exports.
