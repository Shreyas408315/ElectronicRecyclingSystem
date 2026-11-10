import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database.connection import Base
from database.models import Supplier, Purchase, PurchaseItem
from services.supplier_service import create_supplier
from services.purchase_service import (
    PurchaseValidationError,
    generate_next_purchase_code,
    create_purchase,
    get_purchase_by_id,
    get_purchase_by_code,
    list_purchases,
    get_purchase_metrics,
    get_supplier_purchase_ledger_balance,
    calculate_line_total,
    calculate_payment_status,
)
from services.pdf_receipt_service import (
    generate_purchase_receipt_pdf,
    get_factory_info,
)


@pytest.fixture
def db_session():
    # In-memory SQLite for testing isolation
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def supplier_actor(db_session, authenticated_actor_factory):
    return authenticated_actor_factory(db_session)


@pytest.fixture
def active_supplier(db_session, supplier_actor):
    sup = create_supplier(db_session, {
        "supplier_code": "SUP-000101",
        "business_name": "Metro Recyclers Pvt Ltd",
        "contact_person": "Kishore Kumar",
        "phone": "9820011223",
        "city": "Mumbai",
        "opening_balance": 10000.0,
        "is_active": True,
    }, actor_context=supplier_actor)
    db_session.commit()
    return sup


@pytest.fixture
def inactive_supplier(db_session, supplier_actor):
    sup = create_supplier(db_session, {
        "supplier_code": "SUP-000102",
        "business_name": "Dormant Scrap Corp",
        "contact_person": "Sunil Shetty",
        "phone": "9820011224",
        "is_active": False,
    }, actor_context=supplier_actor)
    db_session.commit()
    return sup


def test_purchase_id_generation(db_session, active_supplier, supplier_actor):
    code1 = generate_next_purchase_code(
        db_session, actor_context=supplier_actor
    )
    assert code1 == "PUR-000001"

    # Create a purchase with code1
    create_purchase(
        db_session,
        {"purchase_code": code1, "supplier_id": active_supplier.id},
        [{"appliance_category": "Refrigerators", "quantity": 2, "rate": 1500.0}],
        actor_context=supplier_actor,
    )
    db_session.commit()

    code2 = generate_next_purchase_code(
        db_session, actor_context=supplier_actor
    )
    assert code2 == "PUR-000002"


def test_purchase_code_generation_requires_actor_context(db_session):
    with pytest.raises(TypeError, match="actor_context"):
        generate_next_purchase_code(db_session)


def test_purchase_creation_with_multiple_items_and_calculations(
    db_session, active_supplier, supplier_actor
):
    """Test creating a purchase with multiple items and automatic subtotal & total cost computation."""
    header = {
        "supplier_id": active_supplier.id,
        "transportation_charges": Decimal("500.00"),
        "loading_charges": Decimal("200.00"),
        "unloading_charges": Decimal("150.00"),
        "other_charges": Decimal("50.00"),
        "amount_paid": Decimal("5000.00"),
        "payment_method": "Bank Transfer / NEFT",
        "remarks": "Inward truck MH-04-1234",
    }
    items = [
        {"appliance_category": "Refrigerators", "brand": "LG", "quantity": 5, "unit": "pieces", "rate": 1200.0},
        {"appliance_category": "Washing Machines", "brand": "Whirlpool", "quantity": 3, "unit": "pieces", "rate": 800.0},
        {"appliance_category": "Microwaves", "brand": "Samsung", "quantity": 4, "unit": "pieces", "rate": 400.0},
        {"appliance_category": "Air Conditioners", "brand": "Voltas", "quantity": 2, "unit": "pieces", "rate": 2000.0},
    ]

    purchase = create_purchase(
        db_session, header, items, actor_context=supplier_actor
    )
    db_session.commit()

    # Calculations check:
    # 5 * 1200 = 6000
    # 3 * 800  = 2400
    # 4 * 400  = 1600
    # 2 * 2000 = 4000
    # Subtotal = 6000 + 2400 + 1600 + 4000 = 14000.00
    # Additional costs = 500 + 200 + 150 + 50 = 900.00
    # Total = 14000 + 900 = 14900.00
    # Amount paid = 5000.00
    # Balance = 14900 - 5000 = 9900.00
    # Payment status = Partially Paid

    assert purchase.subtotal == Decimal("14000.00")
    assert purchase.total_amount == Decimal("14900.00")
    assert purchase.amount_paid == Decimal("5000.00")
    assert purchase.balance_amount == Decimal("9900.00")
    assert purchase.payment_status == "Partially Paid"
    assert len(purchase.items) == 4
    assert purchase.supplier_id == active_supplier.id
    assert purchase.supplier.business_name == "Metro Recyclers Pvt Ltd"


def test_payment_status_variations(db_session, active_supplier, supplier_actor):
    # 1. Unpaid purchase
    p_unpaid = create_purchase(
        db_session,
        {"supplier_id": active_supplier.id, "amount_paid": 0},
        [{"appliance_category": "Refrigerators", "quantity": 1, "rate": 1000.0}],
        actor_context=supplier_actor,
    )
    assert p_unpaid.payment_status == "Unpaid"
    assert p_unpaid.balance_amount == Decimal("1000.00")

    # 2. Fully Paid purchase
    p_paid = create_purchase(
        db_session,
        {"supplier_id": active_supplier.id, "amount_paid": 1000.0},
        [{"appliance_category": "Refrigerators", "quantity": 1, "rate": 1000.0}],
        actor_context=supplier_actor,
    )
    assert p_paid.payment_status == "Paid"
    assert p_paid.balance_amount == Decimal("0.00")


def test_kg_unit_weight_rate_calculation():
    # Rate per kg with weight
    line_total = calculate_line_total(quantity=10, weight=Decimal("45.50"), unit="kg", rate=Decimal("20.00"))
    assert line_total == Decimal("910.00")  # 45.50 * 20.00

    # Rate per piece
    line_piece = calculate_line_total(quantity=4, weight=Decimal("20.00"), unit="pieces", rate=Decimal("500.00"))
    assert line_piece == Decimal("2000.00")


def test_invalid_quantities_and_negative_amounts(
    db_session, active_supplier, supplier_actor
):
    # Quantity <= 0
    with pytest.raises(PurchaseValidationError) as exc:
        create_purchase(
            db_session,
            {"supplier_id": active_supplier.id},
            [{"appliance_category": "Microwaves", "quantity": 0, "rate": 500.0}],
            actor_context=supplier_actor,
        )
    assert "Quantity must be a positive integer" in str(exc.value)

    # Negative rate
    with pytest.raises(PurchaseValidationError) as exc:
        create_purchase(
            db_session,
            {"supplier_id": active_supplier.id},
            [{"appliance_category": "Microwaves", "quantity": 2, "rate": -100.0}],
            actor_context=supplier_actor,
        )
    assert "cannot be negative" in str(exc.value)

    # Negative additional charge
    with pytest.raises(PurchaseValidationError) as exc:
        create_purchase(
            db_session,
            {"supplier_id": active_supplier.id, "transportation_charges": -50.0},
            [{"appliance_category": "Microwaves", "quantity": 2, "rate": 100.0}],
            actor_context=supplier_actor,
        )
    assert "Transportation charges cannot be negative" in str(exc.value)

    # Amount paid exceeds total cost
    with pytest.raises(PurchaseValidationError) as exc:
        create_purchase(
            db_session,
            {"supplier_id": active_supplier.id, "amount_paid": 5000.0},
            [{"appliance_category": "Microwaves", "quantity": 2, "rate": 100.0}],  # total 200
            actor_context=supplier_actor,
        )
    assert "cannot exceed total purchase cost" in str(exc.value)


def test_invalid_or_inactive_supplier(db_session, inactive_supplier, supplier_actor):
    # Non-existent supplier ID
    with pytest.raises(PurchaseValidationError) as exc:
        create_purchase(
            db_session,
            {"supplier_id": 99999},
            [{"appliance_category": "Refrigerators", "quantity": 1, "rate": 500.0}],
            actor_context=supplier_actor,
        )
    assert "does not exist" in str(exc.value)

    # Inactive supplier
    with pytest.raises(PurchaseValidationError) as exc:
        create_purchase(
            db_session,
            {"supplier_id": inactive_supplier.id},
            [{"appliance_category": "Refrigerators", "quantity": 1, "rate": 500.0}],
            actor_context=supplier_actor,
        )
    assert "inactive" in str(exc.value)


def test_duplicate_purchase_code_prevention(
    db_session, active_supplier, supplier_actor
):
    create_purchase(
        db_session,
        {"purchase_code": "PUR-000001", "supplier_id": active_supplier.id},
        [{"appliance_category": "Refrigerators", "quantity": 1, "rate": 500.0}],
        actor_context=supplier_actor,
    )
    db_session.commit()

    with pytest.raises(PurchaseValidationError) as exc:
        create_purchase(
            db_session,
            {"purchase_code": "PUR-000001", "supplier_id": active_supplier.id},
            [{"appliance_category": "Washing Machines", "quantity": 1, "rate": 500.0}],
            actor_context=supplier_actor,
        )
    assert "already exists" in str(exc.value)


def test_database_rollback_on_failure(db_session, active_supplier, supplier_actor):
    initial_count = len(list_purchases(db_session, actor_context=supplier_actor))

    try:
        # Invalid item causes exception during creation
        create_purchase(
            db_session,
            {"purchase_code": "PUR-999999", "supplier_id": active_supplier.id},
            [
                {"appliance_category": "Refrigerators", "quantity": 1, "rate": 500.0},
                {"appliance_category": "Washing Machines", "quantity": -5, "rate": 500.0},  # Invalid!
            ],
            actor_context=supplier_actor,
        )
        db_session.commit()
    except PurchaseValidationError:
        db_session.rollback()

    assert len(list_purchases(db_session, actor_context=supplier_actor)) == initial_count
    assert get_purchase_by_code(
        db_session, "PUR-999999", actor_context=supplier_actor
    ) is None


def test_purchase_search_and_filtering(db_session, active_supplier, supplier_actor):
    # Create supplier 2
    sup2 = create_supplier(db_session, {
        "supplier_code": "SUP-000103",
        "business_name": "Apex Clean E-Waste",
        "phone": "9820011225",
        "is_active": True,
    }, actor_context=supplier_actor)
    db_session.commit()

    p1 = create_purchase(
        db_session,
        {
            "purchase_code": "PUR-000011",
            "supplier_id": active_supplier.id,
            "purchase_date": date.today(),
            "amount_paid": Decimal("1000.00"),
        },
        [{"appliance_category": "Refrigerators", "quantity": 1, "rate": 1000.0}],
        actor_context=supplier_actor,
    )
    p2 = create_purchase(
        db_session,
        {
            "purchase_code": "PUR-000012",
            "supplier_id": sup2.id,
            "purchase_date": date.today() - timedelta(days=5),
            "amount_paid": Decimal("0.00"),
        },
        [{"appliance_category": "Air Conditioners", "quantity": 2, "rate": 2000.0}],
        actor_context=supplier_actor,
    )
    db_session.commit()

    # Search by code
    res = list_purchases(db_session, search="PUR-000011", actor_context=supplier_actor)
    assert len(res) == 1
    assert res[0].purchase_code == "PUR-000011"

    # Search by supplier name
    res = list_purchases(db_session, search="Apex Clean", actor_context=supplier_actor)
    assert len(res) == 1
    assert res[0].purchase_code == "PUR-000012"

    # Filter by category
    res = list_purchases(
        db_session, category_filter="Air Conditioners", actor_context=supplier_actor
    )
    assert len(res) == 1
    assert res[0].purchase_code == "PUR-000012"

    # Filter by payment status
    res_paid = list_purchases(
        db_session, status_filter="PAID", actor_context=supplier_actor
    )
    assert any(p.purchase_code == "PUR-000011" for p in res_paid)

    res_unpaid = list_purchases(
        db_session, status_filter="UNPAID", actor_context=supplier_actor
    )
    assert any(p.purchase_code == "PUR-000012" for p in res_unpaid)

    # Date range filter
    res_range = list_purchases(
        db_session,
        start_date=date.today() - timedelta(days=2),
        actor_context=supplier_actor,
    )
    assert all(p.purchase_date >= date.today() - timedelta(days=2) for p in res_range)


def test_pdf_receipt_generation(db_session, active_supplier, supplier_actor):
    p = create_purchase(
        db_session,
        {
            "purchase_code": "PUR-000021",
            "supplier_id": active_supplier.id,
            "transportation_charges": 300.0,
            "amount_paid": 1500.0,
            "remarks": "PDF Receipt generation unit test",
        },
        [
            {"appliance_category": "Washing Machines", "brand": "Bosch", "model": "Series 6", "quantity": 2, "unit": "pieces", "rate": 1500.0},
            {"appliance_category": "Microwaves", "brand": "Panasonic", "model": "NN-ST34", "quantity": 1, "unit": "pieces", "rate": 500.0},
        ],
        actor_context=supplier_actor,
    )
    db_session.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "test_receipt.pdf")
        with pytest.raises(TypeError, match="actor_context"):
            get_factory_info(db_session)
        with pytest.raises(TypeError, match="actor_context"):
            generate_purchase_receipt_pdf(db_session, p.id, pdf_path)
        saved_file = generate_purchase_receipt_pdf(
            db_session,
            p.id,
            pdf_path,
            actor_context=supplier_actor,
        )
        assert os.path.exists(saved_file)
        assert os.path.getsize(saved_file) > 1000  # Generated valid PDF document bytes


def test_supplier_purchase_ledger_balance_integration(
    db_session, active_supplier, supplier_actor
):
    # Active supplier initial opening balance: 10,000.0
    p = create_purchase(
        db_session,
        {
            "supplier_id": active_supplier.id,
            "amount_paid": Decimal("2000.00"),
        },
        [{"appliance_category": "Refrigerators", "quantity": 1, "rate": Decimal("5000.00")}],  # Balance = 3000.00
        actor_context=supplier_actor,
    )
    db_session.commit()

    outstanding, purchases = get_supplier_purchase_ledger_balance(
        db_session, active_supplier.id, actor_context=supplier_actor
    )
    # Expected outstanding = opening_balance (10,000) + unpaid purchase balance (3,000) = 13,000.00
    assert outstanding == Decimal("13000.00")
    assert len(purchases) == 1
    assert purchases[0].id == p.id
