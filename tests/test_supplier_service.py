import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database.connection import Base
from database.models import Supplier
from services.supplier_service import (
    SupplierValidationError,
    SupplierNotFoundError,
    generate_next_supplier_code,
    create_supplier,
    update_supplier,
    deactivate_supplier,
    reactivate_supplier,
    get_supplier_by_id,
    list_suppliers,
    get_supplier_metrics,
)
from services.security_service import authenticate, create_first_administrator, _seed_security_catalogue
from utils.validators import (
    validate_business_name,
    validate_phone,
    validate_email,
    validate_gstin,
    validate_pin_code,
    validate_opening_balance,
    format_currency,
)

@pytest.fixture
def db_session():
    # In-memory SQLite for testing isolation
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    session = TestingSessionLocal()
    try:
        _seed_security_catalogue(session)
        create_first_administrator(
            session, username="supplier-admin", password="Supplier-password-2026!", full_name="Supplier Admin"
        )
        session.commit()
        yield session
    finally:
        session.close()
        test_engine.dispose()


@pytest.fixture
def supplier_actor(db_session):
    return authenticate(db_session, "supplier-admin", "Supplier-password-2026!")


def test_validators():
    # Business Name
    ok, _ = validate_business_name("Eco Recyclers Ltd")
    assert ok
    ok, err = validate_business_name("")
    assert not ok and "required" in err

    # Phone
    ok, _ = validate_phone("9876543210")
    assert ok
    ok, _ = validate_phone("+91 9876543210")
    assert ok
    ok, err = validate_phone("123")
    assert not ok

    # Email
    ok, _ = validate_email("contact@ecorecycle.com")
    assert ok
    ok, err = validate_email("invalid-email")
    assert not ok

    # GSTIN
    ok, _ = validate_gstin("27AAAAA0000A1Z5")
    assert ok
    ok, err = validate_gstin("INVALIDGSTIN")
    assert not ok

    # PIN Code
    ok, _ = validate_pin_code("400001")
    assert ok
    ok, err = validate_pin_code("12345")
    assert not ok

    # Currency format
    assert format_currency(1234567.50) == "₹ 12,34,567.50"
    assert format_currency(500) == "₹ 500.00"


def test_supplier_code_generation(db_session, supplier_actor):
    code1 = generate_next_supplier_code(db_session, actor_context=supplier_actor)
    assert code1 == "SUP-000001"

    s1 = create_supplier(db_session, {
        "business_name": "Supplier One",
        "supplier_code": code1,
    }, actor_context=supplier_actor)
    db_session.commit()

    code2 = generate_next_supplier_code(db_session, actor_context=supplier_actor)
    assert code2 == "SUP-000002"


def test_create_and_read_supplier(db_session, supplier_actor):
    payload = {
        "business_name": "Green Tech E-Waste",
        "contact_person": "Rahul Sharma",
        "phone": "9876543210",
        "email": "rahul@greentech.com",
        "city": "Mumbai",
        "state": "Maharashtra",
        "pin_code": "400001",
        "gstin": "27AAAAA0000A1Z5",
        "opening_balance": 15000.50,
        "remarks": "Major PCB scrap supplier",
    }
    supplier = create_supplier(db_session, payload, actor_context=supplier_actor)
    db_session.commit()

    assert supplier.id is not None
    assert supplier.supplier_code == "SUP-000001"
    assert supplier.business_name == "Green Tech E-Waste"
    assert supplier.opening_balance == 15000.50
    assert supplier.is_active is True

    fetched = get_supplier_by_id(db_session, supplier.id, actor_context=supplier_actor)
    assert fetched is not None
    assert fetched.contact_person == "Rahul Sharma"


def test_duplicate_supplier_code_prevention(db_session, supplier_actor):
    create_supplier(db_session, {
        "supplier_code": "SUP-000001",
        "business_name": "Supplier Alpha",
    }, actor_context=supplier_actor)
    db_session.commit()

    with pytest.raises(SupplierValidationError) as exc:
        create_supplier(db_session, {
            "supplier_code": "SUP-000001",
            "business_name": "Supplier Beta",
        }, actor_context=supplier_actor)
    assert "already exists" in str(exc.value)


def test_update_supplier(db_session, supplier_actor):
    s = create_supplier(db_session, {
        "business_name": "Original Name",
        "phone": "9876543210",
    }, actor_context=supplier_actor)
    db_session.commit()

    orig_code = s.supplier_code
    updated = update_supplier(db_session, s.id, {
        "business_name": "Updated Name",
        "phone": "9811122233",
        "city": "Pune",
    }, actor_context=supplier_actor)
    db_session.commit()

    assert updated.business_name == "Updated Name"
    assert updated.phone == "9811122233"
    assert updated.city == "Pune"
    assert updated.supplier_code == orig_code  # Supplier code preserved!


def test_deactivate_and_reactivate_supplier(db_session, supplier_actor):
    s = create_supplier(db_session, {"business_name": "Test Supplier"}, actor_context=supplier_actor)
    db_session.commit()
    assert s.is_active is True

    deactivated = deactivate_supplier(db_session, s.id, actor_context=supplier_actor)
    db_session.commit()
    assert deactivated.is_active is False

    reactivated = reactivate_supplier(db_session, s.id, actor_context=supplier_actor)
    db_session.commit()
    assert reactivated.is_active is True


def test_search_and_filters(db_session, supplier_actor):
    create_supplier(db_session, {
        "business_name": "Apex Electronics Scrap",
        "contact_person": "Amit Patel",
        "phone": "9998887771",
        "city": "Ahmedabad",
    }, actor_context=supplier_actor)
    s2 = create_supplier(db_session, {
        "business_name": "Bharat Battery Waste",
        "contact_person": "Suresh Gupta",
        "phone": "9998887772",
        "city": "Delhi",
    }, actor_context=supplier_actor)
    deactivate_supplier(db_session, s2.id, actor_context=supplier_actor)
    db_session.commit()

    # Search by name
    res = list_suppliers(db_session, search="Apex", actor_context=supplier_actor)
    assert len(res) == 1
    assert res[0].business_name == "Apex Electronics Scrap"

    # Search by phone
    res = list_suppliers(db_session, search="9998887772", actor_context=supplier_actor)
    assert len(res) == 1
    assert res[0].contact_person == "Suresh Gupta"

    # Filter Active
    active_list = list_suppliers(db_session, status_filter="ACTIVE", actor_context=supplier_actor)
    assert len(active_list) == 1
    assert active_list[0].business_name == "Apex Electronics Scrap"

    # Filter Inactive
    inactive_list = list_suppliers(db_session, status_filter="INACTIVE", actor_context=supplier_actor)
    assert len(inactive_list) == 1
    assert inactive_list[0].business_name == "Bharat Battery Waste"

    # Metrics
    metrics = get_supplier_metrics(db_session, actor_context=supplier_actor)
    assert metrics["total"] == 2
    assert metrics["active"] == 1
    assert metrics["inactive"] == 1
