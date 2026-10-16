from datetime import date

from app.domain import aggregate_settlements, issue_flags


def transaction(amount="100.00"):
    return {
        "transaction_id": "T1",
        "transaction_date": "2025-08-14",
        "transaction_amount": amount,
        "currency": "USD",
        "status": "COMPLETED",
        "settlement_status": "PENDING",
    }


def settlement(settlement_id, amount, kind="DEBIT", settled_on="2025-08-15"):
    return {
        "settlement_id": settlement_id,
        "settlement_amount": amount,
        "settlement_type": kind,
        "settlement_date": settled_on,
    }


def test_partial_and_full_status_transitions():
    txn = transaction()
    partial = [settlement("S1", "50.00")]
    assert aggregate_settlements(txn, partial)["settlement_status"] == "PARTIAL"
    complete = partial + [settlement("S2", "50.00", settled_on="2025-08-16")]
    assert aggregate_settlements(txn, complete)["settlement_status"] == "FULLY_SETTLED"


def test_refund_is_net_settlement_but_not_an_original_debit_shortfall():
    txn = transaction()
    rows = [settlement("S1", "100.00"), settlement("S2", "20.00", "CREDIT")]
    totals = aggregate_settlements(txn, rows)
    assert str(totals["net_total"]) == "80.00"
    assert totals["settlement_status"] == "REFUNDED"
    assert issue_flags(txn, rows, date(2025, 8, 16)) == []


def test_under_and_over_settlement_flags():
    txn = transaction()
    assert issue_flags(txn, [settlement("S1", "80.00")], date(2025, 8, 16))[0]["code"] == "under_settled"
    assert issue_flags(txn, [settlement("S1", "120.00")], date(2025, 8, 16))[0]["severity"] == "critical"


def test_pdf_scenario_exact_settlement_has_no_issue():
    txn = transaction()
    rows = [settlement("S1", "100.00")]

    totals = aggregate_settlements(txn, rows)

    assert totals["net_total"] == 100
    assert totals["settlement_status"] == "FULLY_SETTLED"
    assert issue_flags(txn, rows, date(2025, 8, 16)) == []


def test_pdf_scenario_short_settlement_is_warning_only():
    txn = transaction()
    rows = [settlement("S1", "80.00")]

    totals = aggregate_settlements(txn, rows)
    issues = issue_flags(txn, rows, date(2025, 8, 16))

    assert totals["settlement_status"] == "PARTIAL"
    assert [issue["code"] for issue in issues] == ["under_settled"]
    assert issues[0]["severity"] == "warning"


def test_pdf_scenario_over_settlement_is_critical():
    txn = transaction()
    rows = [settlement("S1", "120.00")]

    totals = aggregate_settlements(txn, rows)
    issues = issue_flags(txn, rows, date(2025, 8, 16))

    assert totals["settlement_status"] == "OVER_SETTLED"
    assert [issue["code"] for issue in issues] == ["over_settled"]
    assert issues[0]["severity"] == "critical"


def test_unsettled_transaction_becomes_critical_after_seven_days():
    txn = transaction()
    assert issue_flags(txn, [], date(2025, 8, 21)) == []
    assert issue_flags(txn, [], date(2025, 8, 22))[0]["code"] == "settlement_overdue"


def test_pdf_refund_of_thirty_dollars_is_refunded_without_shortfall_issue():
    txn = transaction()
    rows = [settlement("S1", "100.00"), settlement("S2", "30.00", "CREDIT")]

    totals = aggregate_settlements(txn, rows)

    assert totals["net_total"] == 70
    assert totals["settlement_status"] == "REFUNDED"
    assert issue_flags(txn, rows, date(2025, 8, 16)) == []


def test_non_completed_transactions_are_not_applicable_and_flag_linked_settlement():
    txn = transaction()
    txn["status"] = "FAILED"
    txn["settlement_status"] = "PENDING"
    rows = [settlement("S1", "30.00")]

    totals = aggregate_settlements(txn, rows)

    assert totals["settlement_status"] == "NOT_APPLICABLE"
    assert totals["outstanding_amount"] == 0
    assert issue_flags(txn, rows, date(2025, 8, 16))[0]["code"] == "unexpected_settlement"


def test_settlement_performance_uses_first_debit_target_date():
    from app.domain import settlement_days_to_target

    txn = transaction()
    rows = [
        settlement("S2", "50.00", settled_on="2025-08-17"),
        settlement("S1", "50.00", settled_on="2025-08-16"),
        settlement("S3", "100.00", "CREDIT", settled_on="2025-08-18"),
    ]

    assert settlement_days_to_target(txn, rows) == 3
