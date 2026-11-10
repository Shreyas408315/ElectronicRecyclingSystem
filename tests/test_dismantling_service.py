import os
import tempfile
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from database.connection import Base
from database.models import (
    Supplier, Purchase, PurchaseItem, DismantlingEntry,
    RecoveredComponent, ScrapMaterial, InventoryItem, StockMovement, Role,
    Permission, RolePermission, User, UserAuditEvent
)
from services.supplier_service import create_supplier, list_suppliers
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_first_administrator,
    create_user,
    logout,
    _seed_security_catalogue,
)
from services.purchase_service import create_purchase, list_purchases, get_purchase_by_code
from services.dismantling_service import (
    DismantlingValidationError,
    generate_next_dismantling_code,
    get_remaining_undismantled_quantity,
    get_purchases_with_undismantled_items,
    create_dismantling_entry,
    update_dismantling_entry,
    submit_dismantling_for_approval,
    approve_dismantling_entry,
    reject_dismantling_entry,
    reverse_approved_dismantling_entry,
    get_dismantling_by_id,
    list_dismantling_entries,
    get_dismantling_metrics,
    calculate_dismantling_recovery_math,
    is_component_stockable,
    is_scrap_stockable,
)
from services.dismantling_report_service import (
    generate_dismantling_pdf_report,
    generate_dismantling_excel_report,
)


@pytest.fixture
def db_session():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    session = TestingSessionLocal()
    try:
        _seed_security_catalogue(session)
        create_first_administrator(session, username="dismantling-admin", password="Dismantling-password-2026!", full_name="Dismantling Administrator")
        session.commit()
        yield session
    finally:
        session.close()


@pytest.fixture
def dismantling_actor(db_session):
    return authenticate(db_session, "dismantling-admin", "Dismantling-password-2026!")


@pytest.fixture
def setup_purchase_with_items(db_session, dismantling_actor):
    sup = create_supplier(db_session, {
        "supplier_code": "SUP-000501",
        "business_name": "Apex Electronic Waste Logistics",
        "contact_person": "Ramesh Gupta",
        "phone": "9820011555",
        "is_active": True,
    }, actor_context=dismantling_actor)
    db_session.commit()

    p = create_purchase(
        db_session,
        {
            "purchase_code": "PUR-000501",
            "supplier_id": sup.id,
            "purchase_date": date.today(),
            "amount_paid": Decimal("10000.00"),
        },
        [
            {
                "appliance_category": "Refrigerators",
                "brand": "LG",
                "model_number": "GL-D201",
                "quantity": 10,
                "weight": Decimal("350.00"),  # 35 kg each
                "unit": "pieces",
                "rate": Decimal("1500.00"),
            },
            {
                "appliance_category": "Washing Machines",
                "brand": "Samsung",
                "model_number": "WA65",
                "quantity": 5,
                "weight": Decimal("150.00"),  # 30 kg each
                "unit": "pieces",
                "rate": Decimal("1000.00"),
            },
        ],
        actor_context=dismantling_actor,
    )
    db_session.commit()
    return sup, p


def test_dismantling_id_generation(db_session, dismantling_actor):
    code1 = generate_next_dismantling_code(
        db_session, actor_context=dismantling_actor
    )
    assert code1 == "DIS-000001"


def test_partial_dismantling_and_remaining_quantities(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    refrig_item = purchase.items[0]  # 10 refrigerators purchased

    assert get_remaining_undismantled_quantity(
        db_session, refrig_item.id, actor_context=dismantling_actor
    ) == 10

    # Batch 1: Dismantle 4 refrigerators
    b1 = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": refrig_item.id,
            "appliances_dismantled": 4,
            "input_weight": Decimal("140.00"),
            "status": "Draft",
        },
        [{"component_name": "Compressor", "quantity": 4, "weight": Decimal("40.00"), "condition": "Working"}],
        [{"scrap_type": "Crushed Plastic", "weight": Decimal("30.00")}],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    # Remaining should now be 6
    assert get_remaining_undismantled_quantity(
        db_session, refrig_item.id, actor_context=dismantling_actor
    ) == 6

    # Batch 2: Dismantle 3 refrigerators
    b2 = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": refrig_item.id,
            "appliances_dismantled": 3,
            "status": "Pending Approval",
        },
        [{"component_name": "Copper Coils", "quantity": 3, "weight": Decimal("15.00"), "condition": "Recyclable Raw Material"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    # Remaining should now be 3
    assert get_remaining_undismantled_quantity(
        db_session, refrig_item.id, actor_context=dismantling_actor
    ) == 3

    # Batch 3: Dismantle remaining 3 refrigerators
    b3 = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": refrig_item.id,
            "appliances_dismantled": 3,
            "status": "Pending Approval",
        },
        [{"component_name": "Steel Shells", "quantity": 3, "weight": Decimal("45.00"), "condition": "Recyclable Raw Material"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    # Remaining should now be 0
    assert get_remaining_undismantled_quantity(
        db_session, refrig_item.id, actor_context=dismantling_actor
    ) == 0


def test_prevention_of_excess_dismantling(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    wm_item = purchase.items[1]  # 5 washing machines

    # Try to dismantle 6 when only 5 were purchased
    with pytest.raises(DismantlingValidationError) as exc:
        create_dismantling_entry(
            db_session,
            {
                "purchase_id": purchase.id,
                "purchase_item_id": wm_item.id,
                "appliances_dismantled": 6,
            },
            [{"component_name": "Electric Motor", "quantity": 6, "weight": Decimal("30.00")}],
            [],
            actor_context=dismantling_actor,
        )
    assert "Cannot dismantle 6 appliances" in str(exc.value)
    assert "Only 5 remain undismantled" in str(exc.value)


def test_recovery_percentage_and_calculations():
    # Input weight: 100 kg. Recovered component: 70 kg. Scrap: 20 kg.
    # Total recovered weight: 70 kg. Total scrap weight: 20 kg.
    components = [
        {"quantity": 2, "weight": Decimal("50.00"), "estimated_unit_value": Decimal("1000.00"), "unit": "pieces"},
        {"quantity": 1, "weight": Decimal("20.00"), "estimated_unit_value": Decimal("500.00"), "unit": "pieces"},
    ]
    scrap = [
        {"quantity": 1, "weight": Decimal("20.00"), "estimated_value": Decimal("200.00")},
    ]

    calc = calculate_dismantling_recovery_math(
        components, scrap, labour_charges=Decimal("300.00"), other_expenses=Decimal("50.00"), input_weight=Decimal("100.00")
    )

    assert calc["total_recovered_weight"] == Decimal("70.00")
    assert calc["total_scrap_weight"] == Decimal("20.00")
    assert calc["total_processing_cost"] == Decimal("350.00")
    assert calc["estimated_recovery_value"] == Decimal("2700.00")  # (2*1000) + (1*500) + 200
    assert calc["recovery_percentage"] == Decimal("70.00")  # (70 / 100) * 100

    # Test when input weight is None / unavailable: Recovery % must be None (N/A)
    calc_no_weight = calculate_dismantling_recovery_math(
        components, scrap, labour_charges=Decimal("300.00"), other_expenses=Decimal("50.00"), input_weight=None
    )
    assert calc_no_weight["recovery_percentage"] is None


def test_draft_entries_do_not_modify_inventory(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    refrig_item = purchase.items[0]

    # Create Draft dismantling entry
    entry = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": refrig_item.id,
            "appliances_dismantled": 2,
            "status": "Draft",
        },
        [
            {"component_name": "Sealed Compressor", "component_category": "Compressors", "quantity": 2, "condition": "Working", "weight": Decimal("20.00")}
        ],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    assert entry.status == "Draft"
    assert entry.is_inventory_updated is False

    # Check InventoryItem table is empty
    inv_count = len(list(db_session.scalars(select(InventoryItem)).all()))
    assert inv_count == 0

    # Check StockMovement table is empty
    mov_count = len(list(db_session.scalars(select(StockMovement)).all()))
    assert mov_count == 0


def test_approval_updates_inventory_and_creates_stock_movements(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    refrig_item = purchase.items[0]

    entry = create_dismantling_entry(
        db_session,
        {
            "dismantling_code": "DIS-000101",
            "purchase_id": purchase.id,
            "purchase_item_id": refrig_item.id,
            "appliances_dismantled": 2,
            "status": "Pending Approval",
        },
        [
            {
                "component_name": "Heavy Duty Compressor",
                "component_category": "Compressors",
                "quantity": Decimal("2.00"),
                "unit": "pieces",
                "weight": Decimal("24.00"),
                "condition": "Working",
                "estimated_unit_value": Decimal("2200.00"),
                "storage_location": "Warehouse Bay 3",
            },
            {
                "component_name": "Stripped Copper Wire",
                "component_category": "Copper Wiring & Harnesses",
                "quantity": Decimal("5.00"),
                "unit": "kg",
                "weight": Decimal("5.00"),
                "condition": "Recyclable Raw Material",
                "estimated_unit_value": Decimal("650.00"),
                "storage_location": "Metals Bin A-1",
            },
            {
                "component_name": "Cracked Plastic Panel",
                "component_category": "Plastics",
                "quantity": Decimal("2.00"),
                "weight": Decimal("4.00"),
                "condition": "Damaged",  # Damaged rule: MUST NOT enter saleable inventory!
                "storage_location": "Scrap Pile",
            }
        ],
        [
            {
                "scrap_type": "Insulation Foam Residue",
                "material_classification": "Unusable Waste",  # Unusable waste rule: MUST NOT enter inventory!
                "quantity": Decimal("1.00"),
                "weight": Decimal("10.00"),
                "estimated_value": Decimal("0.00"),
            },
            {
                "scrap_type": "Shredded Steel Fragments",
                "material_classification": "Recyclable Scrap",  # Recyclable scrap enters inventory
                "quantity": Decimal("1.00"),
                "weight": Decimal("30.00"),
                "estimated_value": Decimal("450.00"),
            }
        ],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    # Now approve entry
    approved_entry = approve_dismantling_entry(
        db_session, entry.id, approved_by="Supervisor Sharma",
        actor_context=dismantling_actor,
    )
    db_session.commit()

    assert approved_entry.status == "Approved"
    assert approved_entry.is_inventory_updated is True
    assert approved_entry.approved_by == "Supervisor Sharma"
    assert approved_entry.approved_at is not None

    # Check inventory items created:
    # 1. Heavy Duty Compressor (Working)
    # 2. Stripped Copper Wire (Recyclable Raw Material)
    # 3. Scrap - Shredded Steel Fragments (Recyclable Raw Material)
    # Total = 3 inventory items (Damaged plastic & Unusable waste are excluded!)
    inv_items = list(db_session.scalars(select(InventoryItem)).all())
    assert len(inv_items) == 3

    comp_inv = db_session.scalar(select(InventoryItem).where(InventoryItem.part_name == "Heavy Duty Compressor"))
    assert comp_inv is not None
    assert comp_inv.quantity_available == Decimal("2.00")
    assert comp_inv.total_weight == Decimal("24.00")
    assert comp_inv.storage_location == "Warehouse Bay 3"

    copper_inv = db_session.scalar(select(InventoryItem).where(InventoryItem.part_name == "Stripped Copper Wire"))
    assert copper_inv is not None
    assert copper_inv.quantity_available == Decimal("5.00")

    # Check StockMovement records
    movements = list(db_session.scalars(select(StockMovement).where(StockMovement.reference_id == entry.id)).all())
    assert len(movements) == 3
    for m in movements:
        assert m.movement_type == "INWARD_DISMANTLING"
        assert m.reference_type == "DISMANTLING_ENTRY"
        assert "DIS-000101" in m.remarks


def test_duplicate_approval_prevention(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    entry = create_dismantling_entry(
        db_session,
        {"purchase_id": purchase.id, "purchase_item_id": purchase.items[0].id, "appliances_dismantled": 1},
        [{"component_name": "Motor", "quantity": 1, "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    approve_dismantling_entry(db_session, entry.id, actor_context=dismantling_actor)
    db_session.commit()

    # Attempt to approve again
    with pytest.raises(DismantlingValidationError) as exc:
        approve_dismantling_entry(
            db_session, entry.id, actor_context=dismantling_actor
        )
    assert "already been approved" in str(exc.value)


def test_reversal_correction_workflow(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    refrig_item = purchase.items[0]

    entry = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": refrig_item.id,
            "appliances_dismantled": 3,
        },
        [{"component_name": "Compressor A", "quantity": Decimal("3.00"), "weight": Decimal("30.00"), "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    approve_dismantling_entry(db_session, entry.id, actor_context=dismantling_actor)
    db_session.commit()

    inv = db_session.scalar(select(InventoryItem).where(InventoryItem.part_name == "Compressor A"))
    assert inv.quantity_available == Decimal("3.00")
    assert get_remaining_undismantled_quantity(
        db_session, refrig_item.id, actor_context=dismantling_actor
    ) == 7

    # Execute reversal
    reversed_entry = reverse_approved_dismantling_entry(
        db_session,
        entry.id,
        reason="Erroneous batch entry - wrong unit model",
        reversed_by="Manager",
        actor_context=dismantling_actor,
    )
    db_session.commit()

    assert reversed_entry.status == "Reversed"
    assert reversed_entry.is_inventory_updated is False

    # Check inventory deducted
    db_session.refresh(inv)
    assert inv.quantity_available == Decimal("0.00")
    assert inv.total_weight == Decimal("0.00")

    # Check reversal movement logged
    rev_movements = list(
        db_session.scalars(
            select(StockMovement)
            .where(StockMovement.reference_id == entry.id)
            .where(StockMovement.movement_type == "CORRECTION_REVERSAL")
        ).all()
    )
    assert len(rev_movements) == 1
    assert rev_movements[0].quantity == Decimal("-3.00")

    # Check undismantled appliance quantity restored
    assert get_remaining_undismantled_quantity(
        db_session, refrig_item.id, actor_context=dismantling_actor
    ) == 10


def test_dismantling_reports_pdf_and_excel(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    entry = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": purchase.items[0].id,
            "appliances_dismantled": 2,
            "input_weight": Decimal("70.00"),
            "labour_charges": Decimal("250.00"),
            "status": "Draft",
        },
        [{"component_name": "Test Coil", "quantity": Decimal("2.00"), "weight": Decimal("10.00"), "condition": "Working"}],
        [{"scrap_type": "Test Plastic", "weight": Decimal("15.00")}],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Test PDF Report
        pdf_path = os.path.join(tmpdir, "report.pdf")
        saved_pdf = generate_dismantling_pdf_report(
            db_session, pdf_path, actor_context=dismantling_actor
        )
        assert os.path.exists(saved_pdf)
        assert os.path.getsize(saved_pdf) > 1000

        # Test Excel Report
        excel_path = os.path.join(tmpdir, "report.xlsx")
        saved_excel = generate_dismantling_excel_report(
            db_session, excel_path, actor_context=dismantling_actor
        )
        assert os.path.exists(saved_excel)
        assert os.path.getsize(saved_excel) > 1000


def test_purchase_selection_and_supplier_relationship(db_session, setup_purchase_with_items, dismantling_actor):
    supplier, purchase = setup_purchase_with_items
    available = get_purchases_with_undismantled_items(
        db_session, actor_context=dismantling_actor
    )
    assert len(available) == 1
    assert available[0]["purchase_id"] == purchase.id
    assert available[0]["purchase_code"] == "PUR-000501"
    assert available[0]["supplier_name"] == supplier.business_name
    assert available[0]["supplier_code"] == supplier.supplier_code
    item_ids = {it["item_id"] for it in available[0]["available_items"]}
    assert purchase.items[0].id in item_ids
    assert purchase.items[1].id in item_ids

    entry = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": purchase.items[0].id,
            "appliances_dismantled": 2,
        },
        [{"component_name": "Fan", "quantity": 2, "weight": Decimal("2.00"), "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()
    loaded = get_dismantling_by_id(db_session, entry.id, actor_context=dismantling_actor)
    assert loaded.purchase.supplier_id == supplier.id
    assert loaded.purchase.supplier.business_name == "Apex Electronic Waste Logistics"


def test_multiple_recovered_components_and_scrap_separation(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    entry = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": purchase.items[0].id,
            "appliances_dismantled": 2,
            "input_weight": Decimal("70.00"),
        },
        [
            {"component_name": "Compressor", "quantity": 2, "weight": Decimal("20.00"), "condition": "Working"},
            {"component_name": "Copper Wiring", "quantity": Decimal("3.00"), "unit": "kg", "weight": Decimal("3.00"), "condition": "Recyclable Raw Material"},
            {"component_name": "Broken Fan", "quantity": 1, "weight": Decimal("1.00"), "condition": "Damaged"},
        ],
        [
            {"scrap_type": "PUF Foam", "material_classification": "Unusable Waste", "weight": Decimal("8.00")},
            {"scrap_type": "Steel Offcuts", "material_classification": "Recyclable Scrap", "weight": Decimal("12.00"), "estimated_value": Decimal("180.00")},
        ],
        actor_context=dismantling_actor,
    )
    db_session.commit()
    assert len(entry.recovered_components) == 3
    assert len(entry.scrap_materials) == 2
    assert entry.total_recovered_weight == Decimal("24.00")
    assert entry.total_scrap_weight == Decimal("20.00")
    assert not is_component_stockable("Damaged")
    assert not is_scrap_stockable("Unusable Waste")
    assert is_scrap_stockable("Recyclable Scrap")

    approve_dismantling_entry(db_session, entry.id, actor_context=dismantling_actor)
    db_session.commit()
    names = {i.part_name for i in db_session.scalars(select(InventoryItem)).all()}
    assert "Compressor" in names
    assert "Copper Wiring" in names
    assert "Scrap - Steel Offcuts" in names
    assert "Broken Fan" not in names
    assert "Scrap - PUF Foam" not in names


def test_data_persistence_and_id_sequence(db_session, setup_purchase_with_items, dismantling_actor):
    _, purchase = setup_purchase_with_items
    first = create_dismantling_entry(
        db_session,
        {"purchase_id": purchase.id, "purchase_item_id": purchase.items[0].id, "appliances_dismantled": 1},
        [{"component_name": "PCB", "quantity": 1, "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()
    assert first.dismantling_code == "DIS-000001"
    second_code = generate_next_dismantling_code(
        db_session, actor_context=dismantling_actor
    )
    assert second_code == "DIS-000002"

    reloaded = get_dismantling_by_id(db_session, first.id, actor_context=dismantling_actor)
    assert reloaded is not None
    assert reloaded.recovered_components[0].component_name == "PCB"
    listed = list_dismantling_entries(
        db_session, search="DIS-000001", actor_context=dismantling_actor
    )
    assert len(listed) == 1


def test_approved_record_cannot_be_silently_edited(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    entry = create_dismantling_entry(
        db_session,
        {"purchase_id": purchase.id, "purchase_item_id": purchase.items[0].id, "appliances_dismantled": 1},
        [{"component_name": "Motor", "quantity": 1, "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()
    approve_dismantling_entry(db_session, entry.id, actor_context=dismantling_actor)
    db_session.commit()

    with pytest.raises(DismantlingValidationError) as exc:
        update_dismantling_entry(
            db_session,
            entry.id,
            {"appliances_dismantled": 2, "purchase_item_id": purchase.items[0].id},
            [{"component_name": "Motor", "quantity": 2, "condition": "Working"}],
            [],
            actor_context=dismantling_actor,
        )
    assert "cannot be edited" in str(exc.value)


def test_rejected_entry_retains_history(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    entry = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": purchase.items[0].id,
            "appliances_dismantled": 1,
            "remarks": "Original inspection notes",
        },
        [{"component_name": "Coil", "quantity": 1, "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()
    submit_dismantling_for_approval(
        db_session, entry.id, actor_context=dismantling_actor
    )
    db_session.commit()
    rejected = reject_dismantling_entry(
        db_session,
        entry.id,
        reason="Incomplete recovery log",
        actor_context=dismantling_actor,
    )
    db_session.commit()
    assert rejected.status == "Rejected"
    assert "Original inspection notes" in (rejected.remarks or "")
    assert "Incomplete recovery log" in (rejected.remarks or "")
    assert get_remaining_undismantled_quantity(
        db_session, purchase.items[0].id, actor_context=dismantling_actor
    ) == 10
    assert len(list(db_session.scalars(select(InventoryItem)).all())) == 0


def test_rollback_when_stock_updates_fail(db_session, setup_purchase_with_items, dismantling_actor):
    _, purchase = setup_purchase_with_items
    entry = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": purchase.items[0].id,
            "appliances_dismantled": 1,
            "status": "Pending Approval",
        },
        [{"component_name": "Transformer", "quantity": 1, "weight": Decimal("4.00"), "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()
    entry_id = entry.id

    original_add = db_session.add

    def flaky_add(obj):
        if isinstance(obj, StockMovement):
            raise RuntimeError("Simulated stock ledger failure")
        return original_add(obj)

    db_session.add = flaky_add
    try:
        with pytest.raises(RuntimeError, match="Simulated stock ledger failure"):
            approve_dismantling_entry(
                db_session, entry_id, actor_context=dismantling_actor
            )
    finally:
        db_session.add = original_add
        db_session.rollback()

    db_session.expire_all()
    restored = get_dismantling_by_id(db_session, entry_id, actor_context=dismantling_actor)
    assert restored.status == "Pending Approval"
    assert restored.is_inventory_updated is False
    assert list(db_session.scalars(select(InventoryItem)).all()) == []
    assert list(db_session.scalars(select(StockMovement)).all()) == []


def test_purchase_and_supplier_modules_continue_to_work(db_session, setup_purchase_with_items, dismantling_actor):
    supplier, purchase = setup_purchase_with_items
    create_dismantling_entry(
        db_session,
        {"purchase_id": purchase.id, "purchase_item_id": purchase.items[1].id, "appliances_dismantled": 1},
        [{"component_name": "Drum", "quantity": 1, "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    suppliers = list_suppliers(db_session, actor_context=dismantling_actor)
    assert any(s.id == supplier.id for s in suppliers)
    purchases = list_purchases(db_session, actor_context=dismantling_actor)
    assert any(p.purchase_code == "PUR-000501" for p in purchases)
    persisted = get_purchase_by_code(
        db_session, "PUR-000501", actor_context=dismantling_actor
    )
    assert persisted is not None
    assert persisted.supplier.business_name == supplier.business_name
    assert len(persisted.items) == 2


def test_recovery_percentage_na_when_input_weight_missing(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    entry = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": purchase.items[0].id,
            "appliances_dismantled": 1,
            "input_weight": 0,
        },
        [{"component_name": "Steel Frame", "quantity": 1, "weight": Decimal("12.00"), "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()
    assert entry.input_weight is None
    assert entry.recovery_percentage is None


def test_get_dismantling_by_id_requires_actor_context(db_session):
    with pytest.raises(TypeError):
        get_dismantling_by_id(db_session, 1)


def test_get_dismantling_by_id_preserves_missing_record_result(db_session, dismantling_actor):
    assert get_dismantling_by_id(
        db_session, 999999, actor_context=dismantling_actor
    ) is None


def test_get_dismantling_by_id_rejects_fabricated_actor(db_session, dismantling_actor):
    fabricated = ActorContext(
        user_id=dismantling_actor.user_id,
        username=dismantling_actor.username,
        session_id="fabricated-session",
        issued_at=dismantling_actor.issued_at,
        database_identity=dismantling_actor.database_identity,
    )
    with pytest.raises(AuthorizationError):
        get_dismantling_by_id(db_session, 1, actor_context=fabricated)


def test_get_dismantling_by_id_rejects_expired_actor(db_session):
    expired_actor = authenticate(
        db_session, "dismantling-admin", "Dismantling-password-2026!", timeout_minutes=0
    )
    with pytest.raises(AuthorizationError):
        get_dismantling_by_id(db_session, 1, actor_context=expired_actor)


def test_get_dismantling_by_id_rejects_logged_out_actor(db_session, dismantling_actor):
    logout(db_session, actor_context=dismantling_actor)
    with pytest.raises(AuthorizationError):
        get_dismantling_by_id(db_session, 1, actor_context=dismantling_actor)


def test_get_dismantling_by_id_rejects_inactive_user(db_session, dismantling_actor):
    db_session.get(User, dismantling_actor.user_id).is_active = False
    with pytest.raises(AuthorizationError):
        get_dismantling_by_id(db_session, 1, actor_context=dismantling_actor)


def test_get_dismantling_by_id_denies_role_without_permission_before_record_query(
    db_session, dismantling_actor
):
    create_user(
        db_session,
        actor_context=dismantling_actor,
        username="dismantling-inventory",
        password="Strong-password-2026!",
        full_name="Dismantling Inventory User",
        role_names=["Inventory Operator"],
    )
    db_session.commit()
    denied_actor = authenticate(
        db_session, "dismantling-inventory", "Strong-password-2026!"
    )
    inventory_role = db_session.scalar(
        select(Role).where(Role.name == "Inventory Operator")
    )
    view_grant = db_session.scalar(
        select(RolePermission)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            RolePermission.role_id == inventory_role.id,
            Permission.code == "dismantling.view",
        )
    )
    assert view_grant is not None
    db_session.delete(view_grant)
    db_session.commit()
    statements = []

    def record_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(AuthorizationError):
            get_dismantling_by_id(db_session, 1, actor_context=denied_actor)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert not any("select" in sql and "dismantling_entries" in sql for sql in statements)
    assert db_session.query(UserAuditEvent).filter_by(action="authorization_denied").count() == 1


def test_get_dismantling_by_id_rejects_actor_from_another_database(
    db_session, dismantling_actor, tmp_path
):
    other_engine = create_engine(f"sqlite:///{tmp_path / 'other.db'}")
    Base.metadata.create_all(bind=other_engine)
    other_session = sessionmaker(bind=other_engine, expire_on_commit=False)()
    try:
        other_admin = create_first_administrator(
            other_session,
            username="other-admin",
            password="Dismantling-password-2026!",
            full_name="Other Database Administrator",
        )
        other_session.commit()
        assert other_admin.id == dismantling_actor.user_id
        assert other_session.query(User).count() == 1
        with pytest.raises(AuthorizationError):
            get_dismantling_by_id(other_session, 1, actor_context=dismantling_actor)
    finally:
        other_session.close()
        other_engine.dispose()


def test_get_dismantling_by_id_supports_independent_same_database_session(
    db_session, setup_purchase_with_items, dismantling_actor
):
    _, purchase = setup_purchase_with_items
    entry = create_dismantling_entry(
        db_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": purchase.items[0].id,
            "appliances_dismantled": 1,
        },
        [{"component_name": "Motor", "quantity": 1, "condition": "Working"}],
        [],
        actor_context=dismantling_actor,
    )
    db_session.commit()

    independent_session = sessionmaker(
        bind=db_session.get_bind(), expire_on_commit=False
    )()
    try:
        loaded = get_dismantling_by_id(
            independent_session, entry.id, actor_context=dismantling_actor
        )
        assert loaded is not None
        assert loaded.id == entry.id
        assert loaded.purchase.purchase_code == purchase.purchase_code
        assert loaded.status == "Draft"
        assert loaded.is_inventory_updated is False
        db_session.expire_all()
        persisted = db_session.get(DismantlingEntry, entry.id)
        assert persisted.status == "Draft"
        assert persisted.is_inventory_updated is False
        assert db_session.scalars(select(StockMovement)).all() == []
    finally:
        independent_session.close()
