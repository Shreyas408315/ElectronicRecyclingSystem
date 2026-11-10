from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import InventoryItem, StockMovement
from services.dismantling_service import (
    DismantlingValidationError,
    approve_dismantling_entry,
    create_dismantling_entry,
)
from services.inventory_service import (
    InventoryValidationError,
    create_inventory_item,
    generate_next_part_code,
    get_inventory_metrics,
    get_inventory_movements,
    list_inventory_items,
    record_stock_movement,
    transfer_inventory_location,
)
from services.inventory_report_service import (
    generate_inventory_excel_report,
    generate_inventory_pdf_report,
)
from services.purchase_service import create_purchase
from services.supplier_service import create_supplier


@pytest.fixture
def inventory_session(authenticated_actor_factory):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    test_session.info["actor_context"] = authenticated_actor_factory(test_session)
    try:
        yield test_session
    finally:
        test_session.close()
        engine.dispose()


def _create_item(session, **overrides):
    data = {
        "part_name": "Copper Wiring",
        "category": "Recyclable Raw Materials",
        "unit": "kg",
        "condition": "Recyclable Raw Material",
        "cost_price": "2.50",
        "estimated_selling_price": "5.00",
        "minimum_stock_level": "3",
        "storage_warehouse": "Main",
        "storage_area": "A",
    }
    data.update(overrides)
    return create_inventory_item(
        session, data, actor_context=session.info["actor_context"]
    )


def test_catalogue_ids_and_new_items_start_with_no_stock(inventory_session):
    item = _create_item(inventory_session)
    assert item.part_code == "PART-000001"
    assert item.quantity_available == Decimal("0.00")
    assert generate_next_part_code(
        inventory_session, actor_context=inventory_session.info["actor_context"]
    ) == "PART-000002"


def test_stock_movement_valuation_low_stock_and_history(inventory_session):
    item = _create_item(inventory_session)
    inventory_session.commit()
    record_stock_movement(
        inventory_session,
        item.id,
        "ADJUSTMENT_IN",
        "5",
        "12.5",
        reason="Initial verified stock count",
        user_responsible="Warehouse operator",
        weight_verified=True,
        actor_context=inventory_session.info["actor_context"],
    )
    inventory_session.commit()

    metrics = get_inventory_metrics(
        inventory_session, actor_context=inventory_session.info["actor_context"]
    )
    assert metrics["total_available_quantity"] == Decimal("5.00")
    assert metrics["total_verified_weight"] == Decimal("12.50")
    assert metrics["cost_quantity_value"] == Decimal("12.50")
    assert metrics["estimated_selling_value"] == Decimal("25.00")
    assert metrics["low_stock_count"] == 0
    assert len(get_inventory_movements(
        inventory_session, item.id,
        actor_context=inventory_session.info["actor_context"],
    )) == 1
    assert list_inventory_items(
        inventory_session, search=item.part_code,
        actor_context=inventory_session.info["actor_context"],
    ) == [item]

    record_stock_movement(
        inventory_session,
        item.id,
        "ISSUE",
        "3",
        "7.5",
        reason="Issued to production",
        user_responsible="Warehouse operator",
        actor_context=inventory_session.info["actor_context"],
    )
    inventory_session.commit()
    metrics = get_inventory_metrics(
        inventory_session, actor_context=inventory_session.info["actor_context"]
    )
    assert metrics["total_available_quantity"] == Decimal("2.00")
    assert metrics["low_stock_count"] == 1
    assert metrics["out_of_stock_count"] == 0
    assert len(get_inventory_movements(
        inventory_session, item.id,
        actor_context=inventory_session.info["actor_context"],
    )) == 2


def test_negative_stock_is_rejected_without_writing_movement(inventory_session):
    item = _create_item(inventory_session)
    inventory_session.commit()
    with pytest.raises(InventoryValidationError, match="negative"):
        record_stock_movement(
            inventory_session,
            item.id,
            "ISSUE",
            "1",
            reason="Invalid issue",
            user_responsible="Operator",
            actor_context=inventory_session.info["actor_context"],
        )
    inventory_session.commit()
    assert inventory_session.get(InventoryItem, item.id).quantity_available == Decimal("0.00")
    assert inventory_session.scalar(select(StockMovement.id)) is None


def test_recorded_weight_requires_a_weighted_stock_issue(inventory_session):
    item = _create_item(inventory_session)
    inventory_session.commit()
    record_stock_movement(
        inventory_session,
        item.id,
        "ADJUSTMENT_IN",
        "2",
        "4",
        reason="Verified receipt",
        user_responsible="Operator",
        weight_verified=True,
        actor_context=inventory_session.info["actor_context"],
    )
    inventory_session.commit()

    with pytest.raises(InventoryValidationError, match="Enter the removed weight"):
        record_stock_movement(
            inventory_session,
            item.id,
            "ISSUE",
            "1",
            reason="Issue without a weight",
            user_responsible="Operator",
            actor_context=inventory_session.info["actor_context"],
        )
    inventory_session.rollback()
    inventory_session.expire_all()
    assert inventory_session.get(InventoryItem, item.id).quantity_available == Decimal("2.00")


def test_non_finite_money_is_rejected(inventory_session):
    with pytest.raises(InventoryValidationError, match="valid decimal"):
        _create_item(inventory_session, cost_price="NaN")


def test_location_transfer_is_audited_without_changing_quantity(inventory_session):
    item = _create_item(inventory_session)
    inventory_session.commit()
    record_stock_movement(
        inventory_session,
        item.id,
        "ADJUSTMENT_IN",
        "4",
        "8",
        reason="Counted stock",
        user_responsible="Operator",
        weight_verified=True,
        actor_context=inventory_session.info["actor_context"],
    )
    movement = transfer_inventory_location(
        inventory_session,
        item.id,
        {"storage_warehouse": "Annex", "rack_number": "R2", "shelf_number": "S3"},
        reason="Warehouse reorganization",
        user_responsible="Operator",
        actor_context=inventory_session.info["actor_context"],
    )
    inventory_session.commit()
    assert movement.movement_type == "TRANSFER"
    assert movement.quantity == Decimal("0.00")
    assert movement.from_location == "Main / A"
    assert movement.to_location == "Annex / R2 / S3"
    assert inventory_session.get(InventoryItem, item.id).quantity_available == Decimal("4.00")


def test_failed_movement_insert_rolls_back_stock_update(inventory_session):
    item = _create_item(inventory_session)
    inventory_session.commit()

    def fail_before_flush(session, flush_context, instances):
        if any(isinstance(record, StockMovement) for record in session.new):
            raise RuntimeError("forced movement insert failure")

    event.listen(inventory_session, "before_flush", fail_before_flush)
    with pytest.raises(RuntimeError, match="forced"):
        record_stock_movement(
            inventory_session,
            item.id,
            "ADJUSTMENT_IN",
            "2",
            "4",
            reason="Rollback test",
            user_responsible="Operator",
            actor_context=inventory_session.info["actor_context"],
        )
    event.remove(inventory_session, "before_flush", fail_before_flush)
    inventory_session.expire_all()
    assert inventory_session.get(InventoryItem, item.id).quantity_available == Decimal("0.00")
    assert inventory_session.scalar(select(StockMovement.id)) is None


def test_inventory_reports_export_pdf_and_excel(inventory_session, tmp_path):
    _create_item(inventory_session)
    inventory_session.commit()
    pdf_path = tmp_path / "inventory.pdf"
    excel_path = tmp_path / "inventory.xlsx"

    actor_context = inventory_session.info["actor_context"]
    assert generate_inventory_pdf_report(
        inventory_session, str(pdf_path), actor_context=actor_context
    ) == str(pdf_path)
    assert generate_inventory_excel_report(
        inventory_session, str(excel_path), actor_context=actor_context
    ) == str(excel_path)
    assert pdf_path.read_bytes().startswith(b"%PDF")
    from openpyxl import load_workbook

    workbook = load_workbook(excel_path, read_only=True)
    assert "Current Inventory" in workbook.sheetnames
    assert "Stock Movements" in workbook.sheetnames


def test_approved_dismantling_adds_recovered_stock_once(
    inventory_session, authenticated_actor_factory
):
    actor_context = authenticated_actor_factory(inventory_session)
    supplier = create_supplier(inventory_session, {
        "business_name": "North Recycling Supplier",
        "phone": "9000000011",
        "is_active": True,
    }, actor_context=actor_context)
    purchase = create_purchase(
        inventory_session,
        {
            "supplier_id": supplier.id,
            "purchase_code": "PUR-INV-001",
            "purchase_date": date.today(),
            "amount_paid": Decimal("1000.00"),
        },
        [{
            "appliance_category": "Refrigerators",
            "brand": "Acme",
            "model_number": "R-1",
            "quantity": 2,
            "weight": Decimal("50.00"),
            "unit": "pieces",
            "rate": Decimal("500.00"),
        }],
        actor_context=actor_context,
    )
    entry = create_dismantling_entry(
        inventory_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": purchase.items[0].id,
            "appliances_dismantled": 1,
            "input_weight": "25",
            "status": "Pending Approval",
        },
        [
            {
                "component_name": "Recovered Compressor",
                "component_category": "Compressors",
                "quantity": "1",
                "unit": "pieces",
                "weight": "8",
                "condition": "Working",
                "estimated_unit_value": "35",
            },
            {
                "component_name": "Recovered Copper",
                "component_category": "Recyclable Raw Materials",
                "quantity": "2",
                "unit": "kg",
                "weight": "2",
                "condition": "Recyclable Raw Material",
                "estimated_unit_value": "12",
            },
            {
                "component_name": "Damaged Board",
                "component_category": "Printed Circuit Boards (PCBs)",
                "quantity": "1",
                "unit": "pieces",
                "weight": "1",
                "condition": "Damaged",
            },
        ],
        [{
            "scrap_type": "Steel",
            "material_classification": "Recyclable Scrap",
            "quantity": "3",
            "weight": "3",
            "unit": "kg",
        }, {
            "scrap_type": "Unusable",
            "material_classification": "Unusable Waste",
            "quantity": "1",
            "weight": "1",
            "unit": "kg",
        }],
        actor_context=actor_context,
    )
    inventory_session.commit()

    approve_dismantling_entry(
        inventory_session, entry.id, "Supervisor", actor_context=actor_context
    )
    inventory_session.commit()
    stock = list(inventory_session.scalars(select(InventoryItem).order_by(InventoryItem.part_code)))
    assert len(stock) == 3  # Two saleable components and recyclable scrap, not damaged/waste.
    assert {item.part_code for item in stock} == {"PART-000001", "PART-000002", "PART-000003"}
    movements = list(inventory_session.scalars(
        select(StockMovement).where(StockMovement.reference_id == entry.id)
    ))
    assert len(movements) == 3
    assert all(movement.reference_type == "DISMANTLING_ENTRY" for movement in movements)

    with pytest.raises(DismantlingValidationError, match="already been approved"):
        approve_dismantling_entry(
            inventory_session, entry.id, "Supervisor", actor_context=actor_context
        )


def test_approval_failure_rolls_back_approval_and_inventory(inventory_session, monkeypatch, authenticated_actor_factory):
    actor_context = authenticated_actor_factory(inventory_session)
    supplier = create_supplier(inventory_session, {
        "business_name": "Transactional Supplier",
        "phone": "9000000012",
        "is_active": True,
    }, actor_context=actor_context)
    purchase = create_purchase(
        inventory_session,
        {
            "supplier_id": supplier.id,
            "purchase_code": "PUR-INV-002",
            "purchase_date": date.today(),
            "amount_paid": Decimal("100.00"),
        },
        [{
            "appliance_category": "Televisions",
            "brand": "Acme",
            "model_number": "T-1",
            "quantity": 1,
            "weight": Decimal("10.00"),
            "unit": "pieces",
            "rate": Decimal("100.00"),
        }],
        actor_context=actor_context,
    )
    entry = create_dismantling_entry(
        inventory_session,
        {
            "purchase_id": purchase.id,
            "purchase_item_id": purchase.items[0].id,
            "appliances_dismantled": 1,
            "status": "Pending Approval",
        },
        [{
            "component_name": "Board",
            "component_category": "Boards",
            "quantity": "1",
            "unit": "pieces",
            "weight": "1",
            "condition": "Working",
        }],
        [],
        actor_context=actor_context,
    )
    inventory_session.commit()

    def fail_posting(*args, **kwargs):
        raise RuntimeError("forced inventory posting failure")

    monkeypatch.setattr("services.dismantling_service._post_dismantling_inventory", fail_posting)
    with pytest.raises(RuntimeError, match="forced"):
        approve_dismantling_entry(
            inventory_session, entry.id, "Supervisor", actor_context=actor_context
        )
    inventory_session.expire_all()
    assert inventory_session.get(type(entry), entry.id).status == "Pending Approval"
    assert inventory_session.scalar(select(InventoryItem.id)) is None
    assert inventory_session.scalar(select(StockMovement.id)) is None
