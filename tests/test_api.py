import pytest
from fastapi.testclient import TestClient

from app.database import database_connection, initialize_database
from app.main import create_app


HEADER = "settlement_id,lifecycle_id,account_id,merchant_name,transaction_date,settlement_date,settlement_amount,settlement_type,currency\n"


def make_client(tmp_path):
    db_path = tmp_path / "reconciliation.db"
    initialize_database(db_path)
    with database_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO transactions
               (transaction_id, lifecycle_id, account_id, merchant_name, transaction_date,
                transaction_amount, currency, status, settlement_status, total_settled_amount)
               VALUES ('T1', 'L1', 'A1', 'Example Store', '2025-08-14', 100.00,
                       'USD', 'COMPLETED', 'PENDING', 0.00)"""
        )
    return TestClient(create_app(db_path))


def upload(client, csv):
    return client.post("/reconcile", content=csv, headers={"content-type": "text/csv"})


def test_reconcile_updates_transaction_and_is_idempotent(tmp_path):
    with make_client(tmp_path) as client:
        csv = HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-15,50.00,DEBIT,USD\n"
        first = upload(client, csv)
        assert first.status_code == 200
        assert first.json()["settlements_added"] == 1
        assert client.get("/transactions/T1").json()["settlement_status"] == "PARTIAL"

        second = upload(client, csv)
        assert second.status_code == 200
        assert second.json()["settlements_added"] == 0
        assert second.json()["duplicates_skipped"] == 1


def test_blank_lifecycle_uses_composite_fallback_and_unknown_is_retained(tmp_path):
    with make_client(tmp_path) as client:
        csv = HEADER + "S1,,A1, example   STORE ,2025-08-14,2025-08-15,100.00,DEBIT,USD\nS2,L999,A9,No Store,2025-08-14,2025-08-15,2.00,DEBIT,USD\n"
        response = upload(client, csv)
        assert response.status_code == 200
        assert response.json()["transactions_updated"] == 1
        assert response.json()["unmatched_count"] == 1
        assert client.get("/transactions/T1").json()["settlement_status"] == "FULLY_SETTLED"
        assert client.get("/unmatched-settlements").json()["total"] == 1


def test_validation_error_does_not_partially_import_rows(tmp_path):
    with make_client(tmp_path) as client:
        csv = HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-15,100.00,DEBIT,USD\nS2,L1,A1,Example Store,not-a-date,2025-08-15,1.00,DEBIT,USD\n"
        response = upload(client, csv)
        assert response.status_code == 422
        assert client.get("/transactions/T1").json()["settlement_count"] == 0


@pytest.mark.parametrize(
    "row,field",
    [
        ("S1,L1,A1,Example Store,2025-08-14,2025-08-15,0.00,DEBIT,USD", "settlement_amount"),
        ("S1,L1,A1,Example Store,2025-08-14,2025-08-15,-1.00,DEBIT,USD", "settlement_amount"),
        ("S1,L1,A1,Example Store,2025-08-14,2025-08-15,1.001,DEBIT,USD", "settlement_amount"),
        ("S1,L1,A1,Example Store,2025-08-14,2025-08-15,1.00,OTHER,USD", "settlement_type"),
        ("S1,L1,A1,Example Store,2025-08-14,2025-08-15,1.00,DEBIT,US", "currency"),
        ("S1,L1,A1,Example Store,2025-08-15,2025-08-14,1.00,DEBIT,USD", "settlement_date"),
        ("S1,L1,A1,Example Store,08/14/2025,2025-08-15,1.00,DEBIT,USD", "transaction_date"),
    ],
)
def test_invalid_values_are_reported_and_import_is_atomic(tmp_path, row, field):
    with make_client(tmp_path) as client:
        csv = HEADER + "SGOOD,L1,A1,Example Store,2025-08-14,2025-08-15,25.00,DEBIT,USD\n" + row + "\n"

        response = upload(client, csv)

        assert response.status_code == 422
        assert any(error.get("field") == field for error in response.json()["errors"])
        assert client.get("/transactions/T1").json()["settlement_count"] == 0


def test_missing_header_and_malformed_csv_return_validation_errors(tmp_path):
    with make_client(tmp_path) as client:
        missing = upload(client, "settlement_id,account_id\nS1,A1\n")
        malformed = upload(client, b'"settlement_id,lifecycle_id,account_id\n')

        assert missing.status_code == 422
        assert "missing required columns" in missing.json()["detail"].lower()
        assert malformed.status_code == 422
        assert "detail" in malformed.json()
        assert client.get("/transactions/T1").json()["settlement_count"] == 0


def test_utf8_bom_and_whitespace_around_headers_are_accepted(tmp_path):
    with make_client(tmp_path) as client:
        header = HEADER.rstrip("\n").replace(",", ", ")
        csv = ("\ufeff" + header + "\nS1,L1,A1,Example Store,2025-08-14,2025-08-15,100.00,DEBIT,USD\n")

        response = upload(client, csv.encode("utf-8"))

        assert response.status_code == 200
        assert client.get("/transactions/T1").json()["settlement_status"] == "FULLY_SETTLED"


def test_two_debits_transition_partial_to_fully_settled(tmp_path):
    with make_client(tmp_path) as client:
        first = upload(client, HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-15,50.00,DEBIT,USD\n")
        first_detail = client.get("/transactions/T1").json()
        second = upload(client, HEADER + "S2,L1,A1,Example Store,2025-08-14,2025-08-16,50.00,DEBIT,USD\n")
        second_detail = client.get("/transactions/T1").json()

        assert first.status_code == 200
        assert first_detail["settlement_status"] == "PARTIAL"
        assert first_detail["total_settled_amount"] == 50
        assert first_detail["last_settlement_date"] == "2025-08-15"
        assert second.status_code == 200
        assert second_detail["settlement_status"] == "FULLY_SETTLED"
        assert second_detail["total_settled_amount"] == 100
        assert second_detail["last_settlement_date"] == "2025-08-16"


def test_conflicting_settlement_id_is_rejected_without_changing_history(tmp_path):
    with make_client(tmp_path) as client:
        accepted = upload(client, HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-15,100.00,DEBIT,USD\n")
        conflict = upload(client, HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-16,80.00,DEBIT,USD\n")

        assert accepted.status_code == 200
        assert conflict.status_code == 409
        detail = client.get("/transactions/T1").json()
        assert detail["settlement_count"] == 1
        assert detail["total_settled_amount"] == 100


def test_conflicting_duplicate_id_inside_file_is_rejected_atomically(tmp_path):
    with make_client(tmp_path) as client:
        csv = HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-15,50.00,DEBIT,USD\nS1,L1,A1,Example Store,2025-08-14,2025-08-16,50.00,DEBIT,USD\n"

        response = upload(client, csv)

        assert response.status_code == 422
        assert client.get("/transactions/T1").json()["settlement_count"] == 0


def test_identical_duplicate_rows_inside_file_are_counted_and_inserted_once(tmp_path):
    with make_client(tmp_path) as client:
        row = "S1,L1,A1,Example Store,2025-08-14,2025-08-15,50.00,DEBIT,USD\n"

        response = upload(client, HEADER + row + row)

        assert response.status_code == 200
        assert response.json()["settlements_received"] == 2
        assert response.json()["settlements_added"] == 1
        assert response.json()["duplicates_skipped"] == 1
        assert client.get("/transactions/T1").json()["settlement_count"] == 1


def test_conflict_rolls_back_earlier_rows_in_same_import(tmp_path):
    with make_client(tmp_path) as client:
        upload(client, HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-15,50.00,DEBIT,USD\n")
        csv = HEADER + "S2,L1,A1,Example Store,2025-08-14,2025-08-16,20.00,DEBIT,USD\nS1,L1,A1,Example Store,2025-08-14,2025-08-17,40.00,DEBIT,USD\n"

        response = upload(client, csv)

        assert response.status_code == 409
        detail = client.get("/transactions/T1").json()
        assert detail["settlement_count"] == 1
        assert detail["total_settled_amount"] == 50


@pytest.mark.parametrize("transport", ["raw", "json", "multipart"])
def test_all_documented_upload_formats_are_supported(tmp_path, transport):
    with make_client(tmp_path) as client:
        csv = HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-15,100.00,DEBIT,USD\n"
        if transport == "raw":
            response = upload(client, csv)
        elif transport == "json":
            response = client.post("/reconcile", json={"csv": csv})
        else:
            response = client.post(
                "/reconcile", files={"file": ("settlements.csv", csv, "text/csv")}
            )

        assert response.status_code == 200
        assert response.json()["settlements_added"] == 1
        assert client.get("/transactions/T1").json()["settlement_status"] == "FULLY_SETTLED"


def test_invalid_utf8_and_upload_over_limit_are_rejected(tmp_path):
    from app.reconciliation import MAX_CSV_BYTES

    with make_client(tmp_path) as client:
        invalid_encoding = upload(client, b"\xff\xfe")
        too_large = upload(client, b"x" * (MAX_CSV_BYTES + 1))

        assert invalid_encoding.status_code == 422
        assert "utf-8" in invalid_encoding.json()["detail"].lower()
        assert too_large.status_code == 413
        assert client.get("/transactions/T1").json()["settlement_count"] == 0


def test_json_upload_with_invalid_unicode_returns_validation_error(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/reconcile",
            content='{"csv":"\\ud800"}',
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 422
        assert "utf-8" in response.json()["detail"].lower()
        assert client.get("/transactions/T1").json()["settlement_count"] == 0


def test_lifecycle_attribute_mismatch_is_retained_instead_of_fallback_match(tmp_path):
    with make_client(tmp_path) as client:
        response = upload(client, HEADER + "S1,L1,WRONG,Example Store,2025-08-14,2025-08-15,100.00,DEBIT,USD\n")

        assert response.status_code == 200
        assert client.get("/transactions/T1").json()["settlement_count"] == 0
        unmatched = client.get("/unmatched-settlements").json()["items"]
        assert unmatched[0]["reason"] == "LIFECYCLE_ATTRIBUTE_MISMATCH"


def test_ambiguous_fallback_is_retained_without_guessing(tmp_path):
    with make_client(tmp_path) as client:
        with database_connection(client.app.state.database_path) as connection:
            connection.execute(
                """INSERT INTO transactions
                   (transaction_id, lifecycle_id, account_id, merchant_name, transaction_date,
                    transaction_amount, currency, status, settlement_status, total_settled_amount)
                   VALUES ('T2', 'L2', 'A1', ' example   STORE ', '2025-08-14', 75.00,
                           'USD', 'COMPLETED', 'PENDING', 0.00)"""
            )

        response = upload(client, HEADER + "S1,,A1,Example Store,2025-08-14,2025-08-15,50.00,DEBIT,USD\n")

        assert response.status_code == 200
        assert client.get("/transactions/T1").json()["settlement_count"] == 0
        assert client.get("/transactions/T2").json()["settlement_count"] == 0
        assert client.get("/unmatched-settlements").json()["items"][0]["reason"] == "AMBIGUOUS_FALLBACK_MATCH"


def test_ambiguous_lifecycle_is_retained_without_guessing(tmp_path):
    with make_client(tmp_path) as client:
        with database_connection(client.app.state.database_path) as connection:
            connection.execute(
                """INSERT INTO transactions
                   (transaction_id, lifecycle_id, account_id, merchant_name, transaction_date,
                    transaction_amount, currency, status, settlement_status, total_settled_amount)
                   VALUES ('T2', 'L1', 'A1', 'Example Store', '2025-08-14', 100.00,
                           'USD', 'COMPLETED', 'PENDING', 0.00)"""
            )

        response = upload(client, HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-15,50.00,DEBIT,USD\n")

        assert response.status_code == 200
        assert client.get("/unmatched-settlements").json()["items"][0]["reason"] == "AMBIGUOUS_LIFECYCLE"


def test_currency_mismatch_rejects_whole_file(tmp_path):
    with make_client(tmp_path) as client:
        csv = HEADER + "SGOOD,L1,A1,Example Store,2025-08-14,2025-08-15,50.00,DEBIT,USD\nSBAD,L1,A1,Example Store,2025-08-14,2025-08-15,50.00,DEBIT,EUR\n"

        response = upload(client, csv)

        assert response.status_code == 422
        assert "currency mismatch" in response.json()["detail"].lower()
        assert client.get("/transactions/T1").json()["settlement_count"] == 0


def test_conflicting_unmatched_settlement_id_is_rejected(tmp_path):
    with make_client(tmp_path) as client:
        first = upload(client, HEADER + "S1,L999,A9,No Store,2025-08-14,2025-08-15,10.00,DEBIT,USD\n")
        conflict = upload(client, HEADER + "S1,L999,A9,No Store,2025-08-14,2025-08-15,11.00,DEBIT,USD\n")

        assert first.status_code == 200
        assert conflict.status_code == 409
        unmatched = client.get("/unmatched-settlements").json()
        assert unmatched["total"] == 1
        assert unmatched["items"][0]["settlement_amount"] == 10


def test_previously_unmatched_settlement_moves_to_history_when_match_becomes_available(tmp_path):
    with make_client(tmp_path) as client:
        row = "S1,,A2,New Store,2025-08-14,2025-08-15,100.00,DEBIT,USD\n"
        first = upload(client, HEADER + row)
        assert first.status_code == 200
        assert client.get("/unmatched-settlements").json()["total"] == 1

        with database_connection(client.app.state.database_path) as connection:
            connection.execute(
                """INSERT INTO transactions
                   (transaction_id, lifecycle_id, account_id, merchant_name, transaction_date,
                    transaction_amount, currency, status, settlement_status, total_settled_amount)
                   VALUES ('T2', 'L2', 'A2', 'New Store', '2025-08-14', 100.00,
                           'USD', 'COMPLETED', 'PENDING', 0.00)"""
            )

        second = upload(client, HEADER + row)

        assert second.status_code == 200
        assert second.json()["settlements_added"] == 1
        assert client.get("/unmatched-settlements").json()["total"] == 0
        assert client.get("/transactions/T2").json()["settlement_status"] == "FULLY_SETTLED"


def test_advanced_filters_summary_and_settlement_timeline(tmp_path):
    with make_client(tmp_path) as client:
        imported = upload(client, HEADER + "S1,L1,A1,Example Store,2025-08-14,2025-08-15,100.00,DEBIT,USD\nS2,L1,A1,Example Store,2025-08-14,2025-08-16,30.00,CREDIT,USD\n")
        assert imported.status_code == 200

        filters = client.get("/transactions", params={
            "settlement_status": "refunded",
            "issue": "none",
            "search": "example",
            "currency": "usd",
            "transaction_date_from": "2025-08-14",
            "transaction_date_to": "2025-08-14",
            "last_settlement_date_from": "2025-08-16",
            "last_settlement_date_to": "2025-08-16",
            "amount_min": 100,
            "amount_max": 100,
        })
        detail = client.get("/transactions/T1")
        summary = client.get("/dashboard/summary")

        assert filters.status_code == 200
        assert filters.json()["total"] == 1
        assert detail.json()["settlement_status"] == "REFUNDED"
        assert [step["net_settled_after_event"] for step in detail.json()["timeline"]] == [100, 70]
        assert summary.json()["transactions_processed"] == 1
        assert summary.json()["settlements_processed"] == 2
        assert summary.json()["average_days_to_settle"] == 1
        assert summary.json()["settlement_rate"] == 1
        assert summary.json()["total_outstanding_amount"] == 0


def test_dashboard_does_not_add_outstanding_across_currencies(tmp_path):
    with make_client(tmp_path) as client:
        with database_connection(client.app.state.database_path) as connection:
            connection.execute(
                """INSERT INTO transactions
                   (transaction_id, lifecycle_id, account_id, merchant_name, transaction_date,
                    transaction_amount, currency, status, settlement_status, total_settled_amount)
                   VALUES ('T2', 'L2', 'A2', 'Euro Store', '2025-08-14', 50.00,
                           'EUR', 'COMPLETED', 'PENDING', 0.00)"""
            )

        summary = client.get("/dashboard/summary").json()

        assert summary["total_outstanding_amount"] is None
        assert summary["outstanding_by_currency"] == {"EUR": 50, "USD": 100}


def test_seeded_exercise_sample_import_reconciles_matches_and_retains_exceptions(tmp_path):
    from pathlib import Path

    db_path = tmp_path / "exercise-sample.db"
    initialize_database(db_path)
    seed = Path(__file__).parents[1] / "sql" / "seed.sql"
    report = Path(__file__).parents[1] / "data" / "settlement_report.csv"
    with database_connection(db_path) as connection:
        connection.executescript(seed.read_text(encoding="utf-8"))

    with TestClient(create_app(db_path)) as client:
        response = upload(client, report.read_bytes())

        assert response.status_code == 200
        assert response.json()["settlements_received"] == 19
        assert response.json()["settlements_added"] == 17
        assert response.json()["unmatched_count"] == 2
        assert response.json()["transactions_updated"] == 12
        assert client.get("/transactions/TXN002").json()["total_settled_amount"] == 3.25
        assert client.get("/transactions/TXN005").json()["settlement_status"] == "FULLY_SETTLED"
        assert client.get("/transactions/TXN007").json()["issues"][0]["code"] == "over_settled"
        assert client.get("/transactions/TXN015").json()["settlement_status"] == "NOT_APPLICABLE"
        assert client.get("/unmatched-settlements").json()["total"] == 2


def test_reconciling_failed_transaction_persists_not_applicable_status(tmp_path):
    with make_client(tmp_path) as client:
        with database_connection(client.app.state.database_path) as connection:
            connection.execute(
                """INSERT INTO transactions
                   (transaction_id, lifecycle_id, account_id, merchant_name, transaction_date,
                    transaction_amount, currency, status, settlement_status, total_settled_amount)
                   VALUES ('TF', 'LF', 'AF', 'Failed Store', '2025-08-14', 20.00,
                           'USD', 'FAILED', 'PENDING', 0.00)"""
            )

        response = upload(client, HEADER + "SF,LF,AF,Failed Store,2025-08-14,2025-08-15,20.00,DEBIT,USD\n")

        assert response.status_code == 200
        detail = client.get("/transactions/TF").json()
        assert detail["settlement_status"] == "NOT_APPLICABLE"
        assert detail["issue_severity"] == "critical"
        assert detail["issues"][0]["code"] == "unexpected_settlement"


def test_invalid_filter_ranges_return_validation_error(tmp_path):
    with make_client(tmp_path) as client:
        bad_date = client.get("/transactions", params={
            "transaction_date_from": "2025-08-16",
            "transaction_date_to": "2025-08-14",
        })
        bad_amount = client.get("/transactions", params={"amount_min": 101, "amount_max": 100})
        bad_status = client.get("/transactions", params={"settlement_status": "impossible"})

        assert bad_date.status_code == 422
        assert bad_amount.status_code == 422
        assert bad_status.status_code == 422


def test_transaction_list_pagination_returns_every_record_once(tmp_path):
    with make_client(tmp_path) as client:
        with database_connection(client.app.state.database_path) as connection:
            connection.execute(
                """INSERT INTO transactions
                   (transaction_id, lifecycle_id, account_id, merchant_name, transaction_date,
                    transaction_amount, currency, status, settlement_status, total_settled_amount)
                   VALUES ('T2', 'L2', 'A2', 'Second Store', '2025-08-15', 25.00,
                           'USD', 'COMPLETED', 'PENDING', 0.00)"""
            )

        first = client.get("/transactions", params={"limit": 1, "offset": 0}).json()
        second = client.get("/transactions", params={"limit": 1, "offset": 1}).json()

        assert first["total"] == second["total"] == 2
        assert first["items"][0]["transaction_id"] == "T2"
        assert second["items"][0]["transaction_id"] == "T1"
        assert {first["items"][0]["transaction_id"], second["items"][0]["transaction_id"]} == {"T1", "T2"}
