from datetime import date, timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import ExpenseCategory, ExpenseHistory, ExpensePaymentHistory
from services.expense_common import ExpenseValidationError
from services.accounting_common import AccountingValidationError
from services.accounting_posting_service import reverse_source_event
from services.expense_payment_service import (
    create_draft_expense_payment,
    get_expense_payment_summary,
    post_expense_payment,
    update_draft_expense_payment,
    void_expense_payment,
)
from services.expense_report_service import (
    generate_expense_excel_report,
    generate_expense_pdf,
    generate_expense_pdf_report,
)
from services.expense_service import (
    calculate_category_summary,
    calculate_dashboard_expense_summary,
    calculate_expense_summary,
    calculate_monthly_summary,
    create_draft_expense,
    create_expense_category,
    deactivate_expense_category,
    get_expense_by_id,
    get_expense_statement,
    list_expenses,
    list_expense_categories,
    post_expense,
    seed_default_expense_categories,
    update_draft_expense,
    void_expense,
)
from services.supplier_service import create_supplier


@pytest.fixture
def db_session(authenticated_actor_factory):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    session.info["actor_context"] = authenticated_actor_factory(session)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _category(session, name="Electricity & Utilities"):
    seed_default_expense_categories(session)
    return session.scalar(select(ExpenseCategory).where(ExpenseCategory.name == name))


def _expense_payload(category, **overrides):
    payload = {
        "expense_date": date.today(),
        "title": "Factory electricity",
        "category_id": category.id,
        "payee_name": "Power Company",
        "subtotal": "100.00",
        "discount": "0",
        "additional_charges": "0",
        "tax_amount": "0",
    }
    payload.update(overrides)
    return payload


def _bind_test_actor(function):
    def call(session, *args, **kwargs):
        kwargs.setdefault("actor_context", session.info["actor_context"])
        return function(session, *args, **kwargs)
    return call


for _api_name in (
    "create_draft_expense_payment", "get_expense_payment_summary",
    "post_expense_payment", "update_draft_expense_payment",
    "void_expense_payment", "generate_expense_excel_report",
    "generate_expense_pdf", "generate_expense_pdf_report",
    "calculate_category_summary", "calculate_dashboard_expense_summary",
    "calculate_expense_summary", "calculate_monthly_summary",
    "create_draft_expense", "create_expense_category",
    "deactivate_expense_category", "get_expense_by_id",
    "get_expense_statement", "list_expenses", "post_expense",
    "seed_default_expense_categories", "update_draft_expense",
    "void_expense", "list_expense_categories",
):
    globals()[_api_name] = _bind_test_actor(globals()[_api_name])


def test_default_categories_are_idempotent_and_categories_cannot_be_deleted(db_session):
    seed_default_expense_categories(db_session)
    expected = len(db_session.scalars(select(ExpenseCategory)).all())
    seed_default_expense_categories(db_session)
    assert len(db_session.scalars(select(ExpenseCategory)).all()) == expected

    custom = create_expense_category(db_session, "Workshop Supplies", "Repair materials")
    with pytest.raises(ExpenseValidationError, match="already exists"):
        create_expense_category(db_session, "workshop supplies")
    deactivate_expense_category(db_session, custom.id)
    assert not db_session.get(ExpenseCategory, custom.id).is_active


def test_expense_ids_draft_edit_itemized_decimal_totals_and_posted_immutability(db_session):
    category = _category(db_session)
    expense = create_draft_expense(
        db_session,
        _expense_payload(
            category, subtotal="0", additional_charges="5",
        ),
        [{
            "description": "Power consumption",
            "quantity": "2.5",
            "unit": "kWh",
            "unit_price": "100",
            "discount": "10",
            "tax_rate": "18",
        }],
    )
    assert expense.expense_code == "EXP-000001"
    assert expense.subtotal == Decimal("250.00")
    assert expense.discount == Decimal("10.00")
    assert expense.tax_amount == Decimal("43.20")
    assert expense.total_amount == Decimal("288.20")
    assert expense.status == "Draft"
    update_draft_expense(db_session, expense.id, _expense_payload(
        category, title="Updated electricity", subtotal="200",
    ), [])
    assert expense.title == "Updated electricity"
    post_expense(db_session, expense.id)
    with pytest.raises(ExpenseValidationError, match="Only draft"):
        update_draft_expense(db_session, expense.id, _expense_payload(
            category, subtotal="300",
        ), [])
    assert [row.event_type for row in expense.history] == [
        "CREATED", "UPDATED", "POSTED",
    ]


def test_expense_payments_partial_full_void_and_overpayment_protection(db_session):
    category = _category(db_session)
    expense = create_draft_expense(
        db_session, _expense_payload(category, subtotal="100"), []
    )
    with pytest.raises(ExpenseValidationError, match="posted expense"):
        create_draft_expense_payment(db_session, {
            "expense_id": expense.id, "amount_paid": "10",
        }, actor_context=db_session.info["actor_context"])
    post_expense(db_session, expense.id)
    payment_before_post = create_draft_expense_payment(db_session, {
        "expense_id": expense.id, "amount_paid": "40", "payment_method": "UPI",
    }, actor_context=db_session.info["actor_context"])
    update_draft_expense_payment(db_session, payment_before_post.id, {
        "amount_paid": "40", "payment_method": "UPI", "notes": "Reviewed",
    }, actor_context=db_session.info["actor_context"])
    assert len(payment_before_post.history) == 2
    payment = create_draft_expense_payment(db_session, {
        "expense_id": expense.id, "amount_paid": "60",
        "payment_method": "NEFT", "transaction_reference": "TX-1",
    }, actor_context=db_session.info["actor_context"])
    post_expense_payment(db_session, payment.id, actor_context=db_session.info["actor_context"])
    assert payment.payment_code == "EPAY-000002"
    assert get_expense_payment_summary(
        db_session, expense.id,
        actor_context=db_session.info["actor_context"],
    )["payment_status"] == "Partially Paid"
    with pytest.raises(ExpenseValidationError, match="remaining"):
        create_draft_expense_payment(db_session, {
            "expense_id": expense.id, "amount_paid": "41",
        }, actor_context=db_session.info["actor_context"])
    post_expense_payment(db_session, payment_before_post.id, actor_context=db_session.info["actor_context"])
    assert get_expense_payment_summary(
        db_session, expense.id,
        actor_context=db_session.info["actor_context"],
    )["payment_status"] == "Paid"
    with pytest.raises(ExpenseValidationError, match="reason"):
        void_expense_payment(db_session, payment.id, "", actor_context=db_session.info["actor_context"])
    void_expense_payment(db_session, payment.id, "Wrong amount", actor_context=db_session.info["actor_context"])
    assert get_expense_payment_summary(
        db_session, expense.id,
        actor_context=db_session.info["actor_context"],
    )["payment_status"] == "Partially Paid"
    assert payment.status == "Voided"
    assert len(payment.history) == 3
    assert len(db_session.scalars(select(ExpensePaymentHistory)).all()) == 6


def test_two_draft_payments_cannot_overpay_after_competing_post(db_session):
    category = _category(db_session)
    expense = create_draft_expense(
        db_session, _expense_payload(category, subtotal="100"), []
    )
    post_expense(db_session, expense.id)
    first = create_draft_expense_payment(db_session, {
        "expense_id": expense.id, "amount_paid": "70",
    }, actor_context=db_session.info["actor_context"])
    second = create_draft_expense_payment(db_session, {
        "expense_id": expense.id, "amount_paid": "70",
    }, actor_context=db_session.info["actor_context"])
    post_expense_payment(db_session, first.id, actor_context=db_session.info["actor_context"])
    with pytest.raises(ExpenseValidationError, match="balance changed"):
        post_expense_payment(db_session, second.id, actor_context=db_session.info["actor_context"])
    assert second.status == "Draft"
    assert calculate_expense_summary(db_session, actor_context=db_session.info["actor_context"])["total_payments"] == Decimal("70.00")


def test_dashboard_and_monthly_summary_use_payment_date_and_all_open_payables(db_session):
    category = _category(db_session)
    today = date.today()
    month_start = today.replace(day=1)
    prior_month_day = month_start - timedelta(days=1)
    prior_expense = create_draft_expense(
        db_session,
        _expense_payload(category, expense_date=prior_month_day, subtotal="100"),
        [],
    )
    post_expense(db_session, prior_expense.id)
    payment = create_draft_expense_payment(db_session, {
        "expense_id": prior_expense.id,
        "amount_paid": "40",
        "payment_date": today,
    }, actor_context=db_session.info["actor_context"])
    post_expense_payment(db_session, payment.id, actor_context=db_session.info["actor_context"])
    current_expense = create_draft_expense(
        db_session,
        _expense_payload(category, expense_date=today, subtotal="50"),
        [],
    )
    post_expense(db_session, current_expense.id)

    summary = calculate_dashboard_expense_summary(
        db_session, start_date=month_start, end_date=today, actor_context=db_session.info["actor_context"]
    )
    assert summary == {
        "total_expenses": Decimal("50.00"),
        "total_payments": Decimal("40.00"),
        "outstanding": Decimal("110.00"),
    }
    monthly = {
        row["month"]: row for row in calculate_monthly_summary(db_session, actor_context=db_session.info["actor_context"])
    }
    assert monthly[prior_month_day.strftime("%Y-%m")]["expense_total"] == Decimal("100.00")
    assert monthly[prior_month_day.strftime("%Y-%m")]["payments"] == Decimal("0.00")
    assert monthly[today.strftime("%Y-%m")]["payments"] == Decimal("40.00")


def test_payment_status_filter_runs_before_pagination(db_session):
    category = _category(db_session)
    for _ in range(100):
        create_draft_expense(db_session, _expense_payload(category), [])
    older = create_draft_expense(
        db_session,
        _expense_payload(
            category, expense_date=date.today() - timedelta(days=365),
            subtotal="25",
        ),
        [],
    )
    post_expense(db_session, older.id)
    payment = create_draft_expense_payment(db_session, {
        "expense_id": older.id, "amount_paid": "25",
    }, actor_context=db_session.info["actor_context"])
    post_expense_payment(db_session, payment.id, actor_context=db_session.info["actor_context"])

    matching = list_expenses(
        db_session, payment_status="Paid", limit=10, sort_by="date_desc", actor_context=db_session.info["actor_context"]
    )
    assert [expense.id for expense in matching] == [older.id]


def test_expense_void_requires_reason_and_prevents_posted_payment_voiding_expense(db_session):
    category = _category(db_session)
    expense = create_draft_expense(
        db_session, _expense_payload(category), []
    )
    post_expense(db_session, expense.id)
    payment = create_draft_expense_payment(db_session, {
        "expense_id": expense.id, "amount_paid": "100",
    }, actor_context=db_session.info["actor_context"])
    post_expense_payment(db_session, payment.id, actor_context=db_session.info["actor_context"])
    with pytest.raises(ExpenseValidationError, match="payments"):
        void_expense(db_session, expense.id, "Incorrect", actor_context=db_session.info["actor_context"])
    void_expense_payment(db_session, payment.id, "Payment reversed", actor_context=db_session.info["actor_context"])
    with pytest.raises(ExpenseValidationError, match="reason"):
        void_expense(db_session, expense.id, "", actor_context=db_session.info["actor_context"])
    void_expense(db_session, expense.id, "Incorrect bill", actor_context=db_session.info["actor_context"])
    assert expense.status == "Voided"
    assert [row.event_type for row in expense.history][-1] == "VOIDED"


def test_accounting_reversal_rejects_an_active_expense_source(db_session):
    category = _category(db_session)
    expense = create_draft_expense(
        db_session, _expense_payload(category), []
    )
    post_expense(db_session, expense.id)

    with pytest.raises(AccountingValidationError, match="voided or cancelled"):
        reverse_source_event(
            db_session,
            "Expense",
            expense.id,
            "VOIDED",
            reason="Attempted premature reversal",
            reversal_date=date.today(),
            actor_context=db_session.info["actor_context"],
        )
    assert expense.status == "Posted"


def test_expense_payment_status_search_summaries_and_supplier_link(db_session, tmp_path, authenticated_actor_factory):
    category = _category(db_session)
    supplier = create_supplier(db_session, {
        "business_name": "Electric Utility Vendor",
        "is_active": True,
    }, actor_context=authenticated_actor_factory(db_session))
    attachment = tmp_path / "bill.txt"
    attachment.write_text("test bill", encoding="utf-8")
    overdue = create_draft_expense(
        db_session,
        _expense_payload(
            category,
            title="Power bill 2025",
            expense_date=date.today() - timedelta(days=40),
            due_date=date.today() - timedelta(days=10),
            supplier_id=supplier.id,
            vendor_reference="BILL-001",
            attachment_path=str(attachment),
            subtotal="150.10",
        ),
        [],
    )
    draft = create_draft_expense(
        db_session, _expense_payload(category, title="Draft utility", subtotal="999"), []
    )
    post_expense(db_session, overdue.id)
    assert get_expense_payment_summary(
        db_session, overdue.id,
        actor_context=db_session.info["actor_context"],
    )["payment_status"] == "Overdue"
    assert len(list_expenses(db_session, search="BILL-001", actor_context=db_session.info["actor_context"])) == 1
    assert len(list_expenses(db_session, payment_status="Overdue", actor_context=db_session.info["actor_context"])) == 1
    summary = calculate_expense_summary(db_session, actor_context=db_session.info["actor_context"])
    assert summary["total_expenses"] == Decimal("150.10")
    assert summary["total_payments"] == Decimal("0.00")
    assert summary["outstanding"] == Decimal("150.10")
    assert summary["overdue_count"] == 1
    assert calculate_category_summary(
        db_session, actor_context=db_session.info["actor_context"]
    )[0]["total"] == Decimal("150.10")
    assert calculate_monthly_summary(db_session, actor_context=db_session.info["actor_context"])[0]["expense_total"] == Decimal("150.10")
    assert draft.status == "Draft"
    assert get_expense_by_id(db_session, overdue.id, actor_context=db_session.info["actor_context"]).supplier.business_name == "Electric Utility Vendor"
    assert get_expense_statement(db_session, overdue.id, actor_context=db_session.info["actor_context"])[0]["expense"] == Decimal("150.10")
    with pytest.raises(ExpenseValidationError, match="accessible file"):
        create_draft_expense(db_session, _expense_payload(
            category, attachment_path=str(tmp_path / "missing.pdf"),
        ), [])


def test_expense_pdf_and_excel_reports(db_session, tmp_path):
    category = _category(db_session)
    expense = create_draft_expense(
        db_session, _expense_payload(category, subtotal="120.50"), []
    )
    post_expense(db_session, expense.id)
    payment = create_draft_expense_payment(db_session, {
        "expense_id": expense.id, "amount_paid": "20.50",
    }, actor_context=db_session.info["actor_context"])
    post_expense_payment(db_session, payment.id, actor_context=db_session.info["actor_context"])
    detail_pdf = tmp_path / "expense.pdf"
    report_pdf = tmp_path / "register.pdf"
    report_xlsx = tmp_path / "register.xlsx"
    generate_expense_pdf(
        db_session, expense.id, str(detail_pdf),
        actor_context=db_session.info["actor_context"],
    )
    generate_expense_pdf_report(
        db_session, str(report_pdf),
        actor_context=db_session.info["actor_context"],
    )
    generate_expense_excel_report(
        db_session, str(report_xlsx),
        actor_context=db_session.info["actor_context"],
    )
    assert detail_pdf.read_bytes().startswith(b"%PDF")
    assert report_pdf.read_bytes().startswith(b"%PDF")
    workbook = load_workbook(report_xlsx, read_only=True)
    assert {
        "Expense Register", "Category Summary", "Monthly Summary",
        "Yearly Summary", "Payee Summary", "Expense Payments",
    }.issubset(workbook.sheetnames)
    assert workbook["Expense Register"].max_row == 2
    workbook.close()
