from datetime import date
from decimal import Decimal
import sqlite3
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import (
    Account, AccountingException, JournalEntry, JournalLine, Permission,
    Purchase, Role, RolePermission, UserRole,
)
from services.account_service import (
    close_accounting_period, configure_account_mapping, ensure_accounting_period,
    list_accounts, list_account_mappings, list_accounting_periods,
    reopen_accounting_period, seed_default_accounts, set_fiscal_year_start,
    deactivate_account, create_account,
)
from services.accounting_posting_service import (
    capture_source_event, list_accounting_exceptions, retry_accounting_exception,
    reverse_source_event,
)
from services.accounting_report_service import (
    generate_accounting_excel_report, generate_accounting_pdf_report,
    get_account_balance, get_accounting_coverage, get_opening_balance_preview,
    get_trial_balance, list_journals,
)
from services.journal_service import (
    AccountingValidationError, create_draft_journal, get_journal_by_id,
    post_journal, reverse_journal, update_draft_journal,
)
from services.purchase_service import create_purchase
from services.customer_service import create_customer
from services.inventory_service import create_inventory_item, record_stock_movement
from services.sales_order_service import (
    confirm_sales_order, create_draft_order, fulfil_sales_order,
)
from services.supplier_service import create_supplier
from services.security_service import (
    AuthorizationError, authenticate, create_user, logout,
)
import database.connection as database_connection


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


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _accounts(session):
    rows = seed_default_accounts(
        session, actor_context=session.info["actor_context"]
    )
    return {account.system_key: account for account in rows}


def _bind_test_actor(function):
    def call(session, *args, **kwargs):
        kwargs.setdefault("actor_context", session.info["actor_context"])
        return function(session, *args, **kwargs)
    return call


for _api_name in (
    "close_accounting_period", "configure_account_mapping",
    "ensure_accounting_period", "list_accounts", "list_account_mappings",
    "list_accounting_periods", "reopen_accounting_period",
    "seed_default_accounts", "set_fiscal_year_start", "deactivate_account",
    "create_draft_journal", "get_journal_by_id", "list_journals",
    "post_journal", "reverse_journal", "update_draft_journal",
    "get_account_balance", "get_accounting_coverage",
    "get_opening_balance_preview", "get_trial_balance",
    "generate_accounting_excel_report", "generate_accounting_pdf_report",
    "list_accounting_exceptions", "retry_accounting_exception",
):
    globals()[_api_name] = _bind_test_actor(globals()[_api_name])


def test_default_chart_and_period_generation_are_idempotent(db_session):
    first = _accounts(db_session)
    second = _accounts(db_session)
    assert len(first) == len(second)
    assert len(db_session.scalars(select(Account)).all()) == len(first)

    period = ensure_accounting_period(db_session, date(2026, 11, 5))
    assert period.start_date == date(2026, 11, 1)
    assert period.end_date == date(2026, 11, 30)
    assert ensure_accounting_period(db_session, date(2026, 11, 5)).id == period.id


def test_manual_journal_balancing_posting_reversal_and_period_lock(db_session):
    accounts = _accounts(db_session)
    journal = create_draft_journal(
        db_session,
        journal_date=date(2026, 11, 5),
        description="Opening bank deposit",
        lines=[
            {"account_id": accounts["cash"].id, "debit": "100.25"},
            {"account_id": accounts["owners_capital"].id, "credit": "100.25"},
        ],
    )
    assert journal.total_debit == journal.total_credit == Decimal("100.25")
    post_journal(db_session, journal.id)
    reversal = reverse_journal(db_session, journal.id, "Incorrect source amount")
    assert journal.status == "Reversed"
    assert reversal.status == "Posted"
    assert reversal.total_debit == reversal.total_credit == Decimal("100.25")
    assert len(db_session.scalars(select(JournalLine)).all()) == 4
    assert get_account_balance(db_session, accounts["cash"].id) == Decimal("0.00")

    period = ensure_accounting_period(db_session, date(2026, 11, 5))
    close_accounting_period(db_session, period.id, "Month end complete")
    with pytest.raises(AccountingValidationError, match="closed"):
        create_draft_journal(
            db_session,
            journal_date=date(2026, 11, 6),
            description="Late entry",
            lines=[
                {"account_id": accounts["cash"].id, "debit": "1"},
                {"account_id": accounts["owners_capital"].id, "credit": "1"},
            ],
        )


def test_journal_rejects_unbalanced_amounts(db_session):
    accounts = _accounts(db_session)
    with pytest.raises(AccountingValidationError, match="not balanced"):
        create_draft_journal(
            db_session,
            journal_date=date.today(),
            description="Invalid journal",
            lines=[
                {"account_id": accounts["cash"].id, "debit": "10"},
                {"account_id": accounts["owners_capital"].id, "credit": "9"},
            ],
        )


def test_mapping_rejects_accounts_with_the_wrong_account_type(db_session):
    accounts = _accounts(db_session)
    with pytest.raises(ValueError, match="requires an assets account"):
        configure_account_mapping(
            db_session, "control:accounts_receivable", accounts["sales_revenue"].id,
        )


def test_chart_management_requires_accounting_configure(db_session):
    admin = db_session.info["actor_context"]
    factory_manager = create_user(
        db_session,
        actor_context=admin,
        username="factory-manager",
        password="Factory-manager-test-2026!",
        full_name="Factory Manager",
        role_names=["Factory Manager"],
    )
    manager_context = authenticate(
        db_session, factory_manager.username, "Factory-manager-test-2026!"
    )
    try:
        with pytest.raises(AuthorizationError):
            create_account(
                db_session,
                actor_context=manager_context,
                code="TEST-CHART-ACCOUNT",
                name="Unauthorized chart account",
                account_type="Assets",
            )
        assert db_session.scalar(
            select(Account.id).where(Account.code == "TEST-CHART-ACCOUNT")
        ) is None
    finally:
        logout(db_session, actor_context=manager_context)


def test_source_event_requires_actor_and_originating_permission(db_session):
    with pytest.raises(TypeError, match="actor_context"):
        capture_source_event(db_session, "Purchase", 1, "CREATED")

    admin = db_session.info["actor_context"]
    sales_user = create_user(
        db_session,
        actor_context=admin,
        username="sales-operator",
        password="Sales-operator-test-2026!",
        full_name="Sales Operator",
        role_names=["Sales Operator"],
    )
    sales_context = authenticate(
        db_session, sales_user.username, "Sales-operator-test-2026!"
    )
    try:
        with pytest.raises(AuthorizationError):
            capture_source_event(
                db_session, "Purchase", 1, "CREATED",
                actor_context=sales_context,
            )
        with pytest.raises(TypeError, match="actor_context"):
            reverse_source_event(
                db_session, "Expense", 1, "VOIDED",
                reason="Unauthorized reversal", reversal_date=date.today(),
            )
        with pytest.raises(AuthorizationError):
            reverse_source_event(
                db_session, "Expense", 1, "VOIDED",
                reason="Unauthorized reversal", reversal_date=date.today(),
                actor_context=sales_context,
            )
    finally:
        logout(db_session, actor_context=sales_context)


def test_purchase_source_posting_does_not_require_accounting_post(
    db_session, authenticated_actor_factory,
):
    admin = authenticated_actor_factory(db_session)
    accounts = _accounts(db_session)
    configure_account_mapping(
        db_session, "inventory:materials_awaiting_dismantling",
        accounts["inventory_raw"].id, actor_context=admin,
    )
    configure_account_mapping(
        db_session, "control:accounts_payable", accounts["accounts_payable"].id,
        actor_context=admin,
    )
    procurement_user = create_user(
        db_session,
        actor_context=admin,
        username="procurement-operator",
        password="Procurement-test-user-2026!",
        full_name="Procurement Operator",
        role_names=["Procurement"],
    )
    procurement_context = authenticate(
        db_session, procurement_user.username, "Procurement-test-user-2026!"
    )
    try:
        supplier = create_supplier(
            db_session,
            {
                "business_name": "Procurement Test Supplier",
                "contact_person": "Operations",
                "phone": "9000000099",
                "city": "Pune",
            },
            actor_context=procurement_context,
        )
        purchase = create_purchase(
            db_session,
            {"supplier_id": supplier.id, "purchase_date": date(2026, 11, 5)},
            [{"appliance_category": "Appliances", "quantity": 1, "rate": "50"}],
            actor_context=procurement_context,
        )
        assert isinstance(
            db_session.scalar(
                select(JournalEntry).where(
                    JournalEntry.source_module == "Purchase",
                    JournalEntry.source_transaction_id == purchase.id,
                    JournalEntry.status == "Posted",
                )
            ),
            JournalEntry,
        )
    finally:
        logout(db_session, actor_context=procurement_context)


def test_new_purchase_posts_once_and_existing_purchase_is_not_backfilled(db_session, authenticated_actor_factory):
    accounts = _accounts(db_session)
    configure_account_mapping(
        db_session, "inventory:materials_awaiting_dismantling",
        accounts["inventory_raw"].id,
    )
    configure_account_mapping(
        db_session, "control:accounts_payable", accounts["accounts_payable"].id,
    )
    actor_context = authenticated_actor_factory(db_session)
    supplier = create_supplier(db_session, {
        "business_name": "Test Recycler", "contact_person": "Operations",
        "phone": "9000000000", "city": "Pune",
    }, actor_context=actor_context)
    purchase = create_purchase(
        db_session,
        {"supplier_id": supplier.id, "purchase_date": date(2026, 11, 5)},
        [{"appliance_category": "Appliances", "quantity": 2, "rate": "50.25"}],
        actor_context=actor_context,
    )
    journal = capture_source_event(
        db_session, "Purchase", purchase.id, "CREATED",
        actor_context=db_session.info["actor_context"],
    )
    assert isinstance(journal, JournalEntry)
    assert journal.status == "Posted"
    assert journal.total_debit == Decimal("100.50")
    assert len(db_session.scalars(select(JournalEntry)).all()) == 1

    historical = Purchase(
        purchase_code="PUR-099999", supplier_id=supplier.id,
        purchase_date=date(2025, 1, 1), subtotal=Decimal("10.00"),
        transportation_charges=0, loading_charges=0, unloading_charges=0,
        other_charges=0, total_amount=Decimal("10.00"), amount_paid=0,
        balance_amount=Decimal("10.00"), payment_method="Cash",
        payment_status="Unpaid",
    )
    db_session.add(historical)
    db_session.flush()
    assert db_session.scalar(select(JournalEntry.id).where(
        JournalEntry.source_transaction_id == historical.id
    )) is None


def test_unmapped_source_creates_exception_and_can_be_retried(db_session, authenticated_actor_factory):
    actor_context = authenticated_actor_factory(db_session)
    supplier = create_supplier(db_session, {
        "business_name": "Review Supplier", "contact_person": "Operations",
        "phone": "9000000001", "city": "Pune",
    }, actor_context=actor_context)
    purchase = create_purchase(
        db_session, {"supplier_id": supplier.id},
        [{"appliance_category": "Appliances", "quantity": 1, "rate": "25"}],
        actor_context=actor_context,
    )
    exception = db_session.scalar(select(AccountingException).where(
        AccountingException.source_module == "Purchase",
        AccountingException.source_transaction_id == purchase.id,
    ))
    assert exception is not None
    assert exception.status == "Open"

    accounts = _accounts(db_session)
    configure_account_mapping(
        db_session, "inventory:materials_awaiting_dismantling",
        accounts["inventory_raw"].id,
    )
    configure_account_mapping(
        db_session, "control:accounts_payable", accounts["accounts_payable"].id,
    )
    result = retry_accounting_exception(db_session, exception.id)
    assert isinstance(result, JournalEntry)
    assert exception.status == "Resolved"
    assert len(list_accounting_exceptions(db_session)) == 0


def test_reports_are_decimal_based_and_export_review_queue(db_session, tmp_path):
    _accounts(db_session)
    preview = get_opening_balance_preview(db_session)
    assert "Preview only" in preview["mode"]
    assert preview["customer_receivable_total"] == Decimal("0.00")
    assert get_trial_balance(db_session)["difference"] == Decimal("0.00")
    assert get_accounting_coverage(db_session)["eligible_source_events"] == 0

    excel_path = tmp_path / "trial-balance.xlsx"
    pdf_path = tmp_path / "trial-balance.pdf"
    generate_accounting_excel_report(
        db_session, str(excel_path),
        actor_context=db_session.info["actor_context"],
        report_type="Trial Balance"
    )
    generate_accounting_pdf_report(
        db_session, str(pdf_path),
        actor_context=db_session.info["actor_context"],
        report_type="Trial Balance"
    )
    workbook = load_workbook(excel_path)
    assert workbook.sheetnames == ["Trial Balance", "Review Queue"]
    assert pdf_path.stat().st_size > 0


def test_dispatch_posts_cogs_only_from_explicitly_verified_inventory_cost(db_session):
    accounts = _accounts(db_session)
    configure_account_mapping(
        db_session, "expense:cost_of_goods_sold", accounts["cost_of_goods_sold"].id,
    )
    configure_account_mapping(
        db_session, "inventory:recovered_components", accounts["inventory_recovered"].id,
    )
    customer = create_customer(db_session, {
        "customer_name": "Dispatch Customer", "customer_type": "Distributor",
        "phone": "9000000010", "city": "Pune",
    }, actor_context=db_session.info["actor_context"])
    item = create_inventory_item(db_session, {
        "part_name": "Verified compressor", "category": "Compressor",
        "condition": "Working", "unit": "pieces", "cost_price": "12.50",
        "cost_price_verified": True,
    }, actor_context=db_session.info["actor_context"])
    record_stock_movement(
        db_session, item.id, "ADJUSTMENT_IN", "3", "30",
        reason="Verified opening count", user_responsible="Accounting test",
        weight_verified=True,
        actor_context=db_session.info["actor_context"],
    )
    order = create_draft_order(
        db_session, {"customer_id": customer.id}, [{
            "inventory_item_id": item.id, "quantity_ordered": "2",
            "unit_selling_price": "20",
        }],

        actor_context=db_session.info["actor_context"],
        )
    confirm_sales_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    fulfilment = fulfil_sales_order(db_session, order.id, {order.items[0].id: "1"}, idempotency_key=uuid4().hex, actor_context=db_session.info["actor_context"])
    journal = db_session.scalar(select(JournalEntry).where(
        JournalEntry.source_module == "Sales Dispatch",
        JournalEntry.source_transaction_id == fulfilment.items[0].id,
    ))
    assert journal is not None
    assert journal.total_debit == Decimal("12.50")
    assert db_session.scalar(select(AccountingException.id).where(
        AccountingException.source_module == "Sales Dispatch"
    )) is None


def test_accounting_ui_opens_with_isolated_database(qapp):
    from PySide6.QtCore import QEvent
    from ui.accounting_page import AccountingPage

    page = AccountingPage()
    assert page.tabs.count() == 5
    assert page.summary.text()
    page.close()
    page.deleteLater()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_accounting_schema_migration_is_additive_backed_up_and_idempotent(
    tmp_path, monkeypatch,
):
    root = tmp_path / "accounting-migration"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    database_path = data_dir / "factory.db"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE inventory_items (id INTEGER PRIMARY KEY, part_code TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO inventory_items (id, part_code) VALUES (7, 'PART-LEGACY')"
        )

    monkeypatch.setattr(database_connection, "ROOT", root)
    monkeypatch.setattr(database_connection, "DB_PATH", database_path)
    monkeypatch.setattr(database_connection, "engine", engine)
    database_connection.initialize_database()
    database_connection.initialize_database()

    inspector = inspect(engine)
    assert "accounts" in inspector.get_table_names()
    assert "journal_entries" in inspector.get_table_names()
    assert "accounting_exceptions" in inspector.get_table_names()
    assert "cost_price_verified" in {
        column["name"] for column in inspector.get_columns("inventory_items")
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT id, part_code FROM inventory_items"
        ).one() == (7, "PART-LEGACY")
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM journal_entries").scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

    backups = list((root / "reports" / "backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT id, part_code FROM inventory_items"
        ).fetchone() == (7, "PART-LEGACY")
    engine.dispose()
