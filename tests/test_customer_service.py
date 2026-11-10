from decimal import Decimal
import sqlite3

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import database.connection as database_connection
from database.connection import Base, create_verified_sqlite_backup
from database.models import Customer
from services.customer_report_service import (
    generate_customer_excel_report,
    generate_customer_pdf_report,
)
from services.customer_service import (
    CustomerDuplicateWarning,
    CustomerValidationError,
    create_customer,
    deactivate_customer,
    evaluate_credit_limit,
    generate_next_customer_code,
    get_customer_by_code,
    get_customer_by_id,
    get_customer_metrics,
    list_customers,
    reactivate_customer,
    update_customer,
)


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


def customer_payload(**overrides):
    payload = {
        "customer_name": "Green Circuit Buyers",
        "customer_type": "Distributor",
        "contact_person": "Asha Rao",
        "phone": "+91 9876543210",
        "alternate_phone": "022-23456789",
        "email": "sales@example.com",
        "gstin": "27ABCDE1234F1Z5",
        "pan": "ABCDE1234F",
        "billing_address": "12 Recycle Road",
        "shipping_address": "Dock 3, 12 Recycle Road",
        "city": "Pune",
        "state": "Maharashtra",
        "pin_code": "411001",
        "country": "India",
        "opening_balance": "1250.50",
        "credit_limit": "5000",
        "payment_terms": "30 days",
        "preferred_payment_method": "Bank Transfer",
        "notes": "Regular component buyer",
        "is_active": True,
    }
    payload.update(overrides)
    return payload


def test_customer_creation_sequential_ids_and_persistence(db_session):
    actor = db_session.info["actor_context"]
    first = create_customer(db_session, customer_payload(), actor_context=actor)
    db_session.commit()
    assert first.customer_code == "CUS-000001"
    assert first.opening_balance == Decimal("1250.50")
    assert first.credit_limit == Decimal("5000.00")
    assert first.gstin == "27ABCDE1234F1Z5"
    assert first.pan == "ABCDE1234F"

    second = create_customer(db_session, customer_payload(
        customer_name="Retail Store",
        phone="9876543211",
        email="store@example.com",
        gstin="",
        pan="",
    ), actor_context=actor)
    db_session.commit()
    assert second.customer_code == "CUS-000002"
    assert generate_next_customer_code(db_session, actor_context=actor) == "CUS-000003"
    assert get_customer_by_code(db_session, "CUS-000001", actor_context=actor).id == first.id
    assert get_customer_by_id(db_session, first.id, actor_context=actor).customer_name == "Green Circuit Buyers"


def test_customer_update_search_type_status_sort_and_pagination(db_session):
    actor = db_session.info["actor_context"]
    first = create_customer(db_session, customer_payload(), actor_context=actor)
    create_customer(db_session, customer_payload(
        customer_name="Retail Store",
        customer_type="Retailer",
        phone="9876543211",
        email="store@example.com",
        gstin="",
        pan="",
        is_active=False,
    ), actor_context=actor)
    updated = update_customer(db_session, first.id, customer_payload(
        customer_name="Green Circuit Wholesale",
        opening_balance="2000",
    ), actor_context=actor)
    db_session.commit()
    assert updated.customer_name == "Green Circuit Wholesale"
    assert list_customers(db_session, search="CUS-000001", actor_context=actor)[0].id == first.id
    assert list_customers(db_session, search="9876543210", actor_context=actor)[0].id == first.id
    assert list_customers(db_session, search="sales@example.com", actor_context=actor)[0].id == first.id
    assert list_customers(db_session, search="27ABCDE1234F1Z5", actor_context=actor)[0].id == first.id
    assert [c.customer_type for c in list_customers(db_session, customer_type="Retailer", actor_context=actor)] == ["Retailer"]
    assert len(list_customers(db_session, status_filter="ACTIVE", actor_context=actor)) == 1
    assert len(list_customers(db_session, status_filter="INACTIVE", actor_context=actor)) == 1
    assert list_customers(db_session, limit=1, offset=1, actor_context=actor)[0].customer_name == "Retail Store"
    assert list_customers(db_session, sort_by="name_desc", actor_context=actor)[0].customer_name == "Retail Store"


def test_customer_deactivation_and_reactivation(db_session):
    actor = db_session.info["actor_context"]
    customer = create_customer(db_session, customer_payload(), actor_context=actor)
    deactivate_customer(db_session, customer.id, actor_context=actor)
    db_session.commit()
    assert get_customer_by_id(db_session, customer.id, actor_context=actor).is_active is False
    reactivate_customer(db_session, customer.id, actor_context=actor)
    db_session.commit()
    assert get_customer_by_id(db_session, customer.id, actor_context=actor).is_active is True


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("phone", "abc1234567890", "unsupported characters"),
        ("email", "bad-email", "valid email"),
        ("gstin", "not-a-gstin", "GSTIN"),
        ("pan", "bad-pan", "PAN"),
        ("pin_code", "ab411001", "only digits"),
    ],
)
def test_customer_validation_rejects_bad_contact_and_tax_fields(db_session, field, value, expected):
    with pytest.raises(CustomerValidationError) as error:
        create_customer(db_session, customer_payload(**{field: value}), actor_context=db_session.info["actor_context"])
    assert expected.lower() in str(error.value).lower()


@pytest.mark.parametrize(
    "field,value",
    [
        ("opening_balance", "-1"),
        ("credit_limit", "-1"),
        ("opening_balance", "NaN"),
        ("credit_limit", "Infinity"),
    ],
)
def test_customer_financial_values_must_be_finite_and_nonnegative(db_session, field, value):
    with pytest.raises(CustomerValidationError):
        create_customer(db_session, customer_payload(**{field: value}), actor_context=db_session.info["actor_context"])
    assert db_session.scalar(select(Customer.id)) is None


def test_duplicate_customer_warning_requires_explicit_override(db_session):
    actor = db_session.info["actor_context"]
    original = create_customer(db_session, customer_payload(), actor_context=actor)
    with pytest.raises(CustomerDuplicateWarning) as warning:
        create_customer(db_session, customer_payload(customer_name="Green Circuit Buyers Ltd"), actor_context=actor)
    assert original in warning.value.customers
    saved = create_customer(
        db_session,
        customer_payload(customer_name="Green Circuit Buyers Ltd"),
        actor_context=actor,
        allow_potential_duplicate=True,
    )
    db_session.commit()
    assert saved.id != original.id
    assert len(list_customers(db_session, actor_context=actor)) == 2


def test_billing_shipping_addresses_and_custom_payment_days(db_session):
    payload = customer_payload(
        billing_address="Billing",
        shipping_address="Shipping",
        payment_terms="Custom",
        custom_payment_days=42,
    )
    actor = db_session.info["actor_context"]
    customer = create_customer(db_session, payload, actor_context=actor)
    db_session.commit()
    assert customer.billing_address == "Billing"
    assert customer.shipping_address == "Shipping"
    assert customer.payment_terms == "Custom"
    assert customer.custom_payment_days == 42

    with pytest.raises(CustomerValidationError, match="Custom payment days"):
        create_customer(db_session, customer_payload(
            customer_name="Invalid Terms",
            phone="9876543211",
            email="invalid@example.com",
            gstin="",
            pan="",
            payment_terms="Custom",
            custom_payment_days=-2,
        ), actor_context=actor)


def test_opening_balance_credit_limit_warning_and_dashboard_scope(db_session):
    actor = db_session.info["actor_context"]
    customer = create_customer(db_session, customer_payload(), actor_context=actor)
    decision = evaluate_credit_limit(customer, "4000")
    assert decision["current_outstanding"] == Decimal("1250.50")
    assert decision["would_exceed_limit"] is True
    assert decision["warning"] is True
    assert decision["hard_restriction"] is False
    assert decision["balance_basis"] == "opening_balance_only"

    based_on_ledger = evaluate_credit_limit(customer, "1000", current_outstanding="4500")
    assert based_on_ledger["would_exceed_limit"] is True
    assert based_on_ledger["balance_basis"] == "provided_authoritative_balance"

    metrics = get_customer_metrics(db_session, actor_context=actor)
    assert metrics["total_opening_balance"] == Decimal("1250.50")
    assert metrics["total_outstanding_receivables"] is None
    assert metrics["credit_limit_exceeded_count"] is None
    assert metrics["overdue_customer_count"] is None
    assert metrics["financial_data_available"] is False


def test_customer_reports_export_pdf_and_excel(db_session, tmp_path):
    actor = db_session.info["actor_context"]
    create_customer(db_session, customer_payload(), actor_context=actor)
    create_customer(db_session, customer_payload(
        customer_name="Zero Balance Dealer",
        customer_type="Scrap Dealer",
        phone="9876543211",
        email="dealer@example.com",
        gstin="",
        pan="",
        opening_balance=0,
    ), actor_context=actor)
    db_session.commit()
    pdf_path = tmp_path / "customers.pdf"
    excel_path = tmp_path / "customers.xlsx"
    assert generate_customer_pdf_report(db_session, str(pdf_path), actor_context=actor) == str(pdf_path)
    assert generate_customer_excel_report(db_session, str(excel_path), actor_context=actor) == str(excel_path)
    assert pdf_path.read_bytes().startswith(b"%PDF")
    from openpyxl import load_workbook

    workbook = load_workbook(excel_path, read_only=True)
    assert {"Customer Directory", "By Customer Type", "Active Status", "Opening Balances", "Credit Review"} <= set(workbook.sheetnames)
    assert workbook["Opening Balances"].max_row == 2
    assert workbook["Credit Review"]["B2"].value == "Not available"


def test_verified_backup_preserves_pre_migration_database_contents(tmp_path):
    source_path = tmp_path / "existing.db"
    backup_dir = tmp_path / "backups"
    connection = sqlite3.connect(source_path)
    connection.execute("CREATE TABLE business_data (id INTEGER PRIMARY KEY, label TEXT)")
    connection.execute("INSERT INTO business_data (label) VALUES ('preserve me')")
    connection.commit()
    connection.close()

    backup_path = create_verified_sqlite_backup(source_path, backup_dir)
    second_backup_path = create_verified_sqlite_backup(source_path, backup_dir)
    assert second_backup_path != backup_path
    assert backup_path.read_bytes()
    original = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    backup = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT label FROM business_data").fetchone()[0] == "preserve me"
        assert original.execute("SELECT label FROM business_data").fetchone()[0] == "preserve me"
    finally:
        original.close()
        backup.close()


def test_schema_initialization_backs_up_before_additive_migration(tmp_path, monkeypatch):
    database_path = tmp_path / "factory.db"
    legacy = sqlite3.connect(database_path)
    legacy.execute(
        "CREATE TABLE application_settings (key VARCHAR(100) PRIMARY KEY, value VARCHAR(500) NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO application_settings (key, value) VALUES ('factory_name', 'Preserve Factory')"
    )
    legacy.commit()
    legacy.close()
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setattr(database_connection, "DB_PATH", database_path)
    monkeypatch.setattr(database_connection, "ROOT", tmp_path)
    monkeypatch.setattr(database_connection, "engine", engine)

    try:
        database_connection.initialize_database()
        backup_paths = list((tmp_path / "reports" / "backups").glob("*pre-migration*.db"))
        assert len(backup_paths) == 1
        backup = sqlite3.connect(backup_paths[0].as_uri() + "?mode=ro", uri=True)
        migrated = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
        try:
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert backup.execute(
                "SELECT value FROM application_settings WHERE key = 'factory_name'"
            ).fetchone()[0] == "Preserve Factory"
            assert backup.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='customers'"
            ).fetchone() is None
            assert migrated.execute(
                "SELECT value FROM application_settings WHERE key = 'factory_name'"
            ).fetchone()[0] == "Preserve Factory"
            assert migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='customers'"
            ).fetchone()[0] == "customers"
        finally:
            backup.close()
            migrated.close()
    finally:
        engine.dispose()
