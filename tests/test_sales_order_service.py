from decimal import Decimal
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import (
    Customer,
    InventoryItem,
    SalesOrderFulfilment,
    SalesOrderReservation,
    StockMovement,
)
from services.customer_service import create_customer
from services.inventory_service import InventoryValidationError, create_inventory_item, record_stock_movement
from services.sales_order_report_service import (
    generate_order_confirmation_pdf,
    generate_sales_order_excel_report,
    generate_sales_order_pdf_report,
)
from services.sales_order_service import (
    SalesOrderValidationError,
    cancel_sales_order,
    confirm_sales_order,
    create_draft_order,
    fulfil_sales_order,
    evaluate_proposed_order_credit_warning,
    get_order_item_reserved_quantity,
    get_sales_order_by_id,
    get_sales_order_metrics,
    get_sales_order_summary,
    hold_sales_order,
    resume_sales_order,
    update_draft_order,
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


def _create_customer(session):
    return create_customer(session, {
        "customer_name": "North Star Components",
        "customer_type": "Distributor",
        "phone": "9876543210",
        "email": "orders@example.com",
        "billing_address": "Warehouse Road",
        "city": "Pune",
        "state": "Maharashtra",
        "pin_code": "411001",
        "opening_balance": "100.00",
        "credit_limit": "10000.00",
        "payment_terms": "30 days",
    }, actor_context=session.info["actor_context"])


def _create_inventory(session, *, quantity="10", condition="Working"):
    item = create_inventory_item(session, {
        "part_name": "Recovered Compressor",
        "category": "Compressor",
        "unit": "pieces",
        "condition": condition,
        "cost_price": "10.00",
        "estimated_selling_price": "25.00",
        "minimum_stock_level": "2",
    }, actor_context=session.info["actor_context"])
    if Decimal(quantity) > 0:
        record_stock_movement(
            session, item.id, "ADJUSTMENT_IN", quantity, "20",
            reason="Verified opening count", user_responsible="Warehouse",
            weight_verified=True,
            actor_context=session.info["actor_context"],
        )
    session.flush()
    return item


def _payload(customer, item, quantity="3"):
    return {
        "header": {
            "customer_id": customer.id,
            "discount_amount": "1.00",
            "additional_charges": "2.00",
        },
        "items": [{
            "inventory_item_id": item.id,
            "quantity_ordered": quantity,
            "unit_selling_price": "12.50",
            "discount": "0.50",
            "tax_rate": "18",
        }],
    }


def test_draft_creation_decimal_totals_snapshots_and_sequential_codes(db_session):
    customer = _create_customer(db_session)
    item = _create_inventory(db_session)
    payload = _payload(customer, item)

    first = create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])
    db_session.commit()
    assert first.order_code == "SO-000001"
    assert first.status == "Draft"
    assert first.subtotal == Decimal("37.50")
    assert first.tax_amount == Decimal("6.66")
    assert first.grand_total == Decimal("44.66")
    assert first.items[0].product_name_snapshot == item.part_name
    assert item.quantity_available == Decimal("10.00")
    assert item.reserved_quantity == Decimal("0.00")

    second = create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])
    assert second.order_code == "SO-000002"


def test_confirm_reserves_once_and_prevents_overselling(db_session):
    customer = _create_customer(db_session)
    item = _create_inventory(db_session, quantity="5")
    first = create_draft_order(db_session, _payload(customer, item, "3")["header"], _payload(customer, item, "3")["items"], actor_context=db_session.info["actor_context"])
    confirm_sales_order(db_session, first.id, actor_context=db_session.info["actor_context"])
    confirm_sales_order(db_session, first.id, actor_context=db_session.info["actor_context"])
    db_session.commit()
    assert item.quantity_available == Decimal("5.00")
    assert item.reserved_quantity == Decimal("3.00")
    assert get_order_item_reserved_quantity(db_session, first.items[0].id, actor_context=db_session.info["actor_context"]) == Decimal("3.00")

    second = create_draft_order(db_session, _payload(customer, item, "3")["header"], _payload(customer, item, "3")["items"], actor_context=db_session.info["actor_context"])
    with pytest.raises(SalesOrderValidationError, match="Insufficient available stock"):
        confirm_sales_order(db_session, second.id, actor_context=db_session.info["actor_context"])
    assert item.reserved_quantity == Decimal("3.00")
    assert second.status == "Draft"


def test_damaged_inventory_and_nonpositive_order_quantities_are_rejected(db_session):
    customer = _create_customer(db_session)
    damaged = _create_inventory(db_session, condition="Damaged")
    payload = _payload(customer, damaged, "1")
    with pytest.raises(SalesOrderValidationError, match="Only active"):
        create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])

    working = _create_inventory(db_session)
    payload = _payload(customer, working, "0")
    with pytest.raises(SalesOrderValidationError, match="greater than zero"):
        create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])


def test_confirmed_edit_recalculates_reservation_and_cancel_releases_it(db_session):
    customer = _create_customer(db_session)
    item = _create_inventory(db_session)
    payload = _payload(customer, item, "3")
    order = create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])
    confirm_sales_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    update_draft_order(
        db_session, order.id, payload["header"],
        [{**payload["items"][0], "quantity_ordered": "6"}],

        actor_context=db_session.info["actor_context"],
        )
    db_session.commit()
    assert item.reserved_quantity == Decimal("6.00")
    events = list(db_session.scalars(
        select(SalesOrderReservation).where(SalesOrderReservation.sales_order_id == order.id)
    ))
    assert [event.event_type for event in events] == ["RESERVE", "RELEASE", "RESERVE"]

    cancel_sales_order(db_session, order.id, "Customer cancelled", actor_context=db_session.info["actor_context"])
    db_session.commit()
    assert item.reserved_quantity == Decimal("0.00")
    assert get_sales_order_by_id(db_session, order.id, actor_context=db_session.info["actor_context"]).status == "Cancelled"


def test_hold_resume_partial_and_full_fulfilment_change_physical_stock(db_session):
    customer = _create_customer(db_session)
    item = _create_inventory(db_session)
    payload = _payload(customer, item, "5")
    order = create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])
    confirm_sales_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    assert item.quantity_available == Decimal("10.00")
    hold_sales_order(db_session, order.id, "Warehouse check", actor_context=db_session.info["actor_context"])
    assert item.reserved_quantity == Decimal("0.00")
    resume_sales_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    assert item.reserved_quantity == Decimal("5.00")

    fulfil_sales_order(db_session, order.id, {order.items[0].id: "2"}, idempotency_key=uuid4().hex, actor_context=db_session.info["actor_context"])
    db_session.commit()
    assert order.status == "Partially Fulfilled"
    assert item.quantity_available == Decimal("8.00")
    assert item.reserved_quantity == Decimal("3.00")

    fulfil_sales_order(db_session, order.id, {order.items[0].id: "3"}, idempotency_key=uuid4().hex, actor_context=db_session.info["actor_context"])
    db_session.commit()
    assert order.status == "Fulfilled"
    assert item.quantity_available == Decimal("5.00")
    assert item.reserved_quantity == Decimal("0.00")
    assert db_session.scalar(select(func_count(SalesOrderFulfilment))) == 2
    dispatches = list(db_session.scalars(
        select(StockMovement).where(StockMovement.reference_type == "SALES_FULFILLMENT")
    ))
    assert len(dispatches) == 2
    assert [movement.movement_type for movement in dispatches] == ["SALE_DISPATCH", "SALE_DISPATCH"]


def func_count(model):
    from sqlalchemy import func

    return func.count(model.id)


def test_fulfilment_failure_rolls_back_stock_movement_and_reservation(monkeypatch, db_session):
    import services.sales_order_service as service

    customer = _create_customer(db_session)
    item = _create_inventory(db_session)
    payload = _payload(customer, item, "3")
    order = create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])
    confirm_sales_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    db_session.commit()

    def fail_after_stock_update(*args, **kwargs):
        raise RuntimeError("Injected event-write failure")

    monkeypatch.setattr(service, "_record_reservation_event", fail_after_stock_update)
    with pytest.raises(RuntimeError, match="Injected"):
        fulfil_sales_order(db_session, order.id, {order.items[0].id: "2"}, idempotency_key=uuid4().hex, actor_context=db_session.info["actor_context"])
    db_session.expire_all()
    item = db_session.get(InventoryItem, item.id)
    order = get_sales_order_by_id(db_session, order.id, actor_context=db_session.info["actor_context"])
    assert item.quantity_available == Decimal("10.00")
    assert item.reserved_quantity == Decimal("3.00")
    assert order.status == "Confirmed"
    assert not list(db_session.scalars(select(SalesOrderFulfilment)))
    assert not list(db_session.scalars(
        select(StockMovement).where(StockMovement.reference_type == "SALES_FULFILLMENT")
    ))


def test_manual_issue_cannot_consume_reserved_stock(db_session):
    customer = _create_customer(db_session)
    item = _create_inventory(db_session, quantity="5")
    payload = _payload(customer, item, "4")
    order = create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])
    confirm_sales_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    with pytest.raises(InventoryValidationError, match="reserved"):
        record_stock_movement(
            db_session, item.id, "ISSUE", "2", reason="Manual issue",
            user_responsible="Warehouse",
            actor_context=db_session.info["actor_context"],
        )
    assert item.quantity_available == Decimal("5.00")
    assert item.reserved_quantity == Decimal("4.00")


def test_reports_include_order_register_fulfilment_reservations_and_confirmation(
    tmp_path, db_session
):
    customer = _create_customer(db_session)
    item = _create_inventory(db_session)
    payload = _payload(customer, item, "2")
    order = create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])
    confirm_sales_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    fulfil_sales_order(db_session, order.id, {order.items[0].id: "1"}, idempotency_key=uuid4().hex, actor_context=db_session.info["actor_context"])

    excel_path = tmp_path / "sales-orders.xlsx"
    pdf_path = tmp_path / "sales-orders.pdf"
    confirmation_path = tmp_path / "order-confirmation.pdf"
    generate_sales_order_excel_report(db_session, str(excel_path), actor_context=db_session.info["actor_context"])
    generate_sales_order_pdf_report(db_session, str(pdf_path), actor_context=db_session.info["actor_context"])
    generate_order_confirmation_pdf(db_session, order.id, str(confirmation_path), actor_context=db_session.info["actor_context"])

    workbook = load_workbook(excel_path, read_only=True)
    assert {"Sales Order Register", "Fulfilments", "Reservation History"} <= set(workbook.sheetnames)
    assert workbook["Sales Order Register"]["A2"].value == "SO-000001"
    assert workbook["Fulfilments"].max_row == 2
    workbook.close()
    assert confirmation_path.read_bytes().startswith(b"%PDF-")
    assert pdf_path.stat().st_size > 0


def test_order_metrics_exclude_completed_order_value_and_customer_credit_is_warning_only(db_session):
    customer = _create_customer(db_session)
    customer.credit_limit = Decimal("100.00")
    item = _create_inventory(db_session)
    payload = _payload(customer, item, "1")
    order = create_draft_order(db_session, payload["header"], payload["items"], actor_context=db_session.info["actor_context"])
    warning = evaluate_proposed_order_credit_warning(db_session, customer.id, order.grand_total, actor_context=db_session.info["actor_context"])
    assert warning["would_exceed_limit"] is True
    assert warning["hard_restriction"] is False
    assert warning["balance_basis"] == "opening_balance_only"
    assert get_sales_order_metrics(db_session, actor_context=db_session.info["actor_context"])["open_order_value"] == order.grand_total
    confirm_sales_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    fulfil_sales_order(db_session, order.id, {order.items[0].id: "1"}, idempotency_key=uuid4().hex, actor_context=db_session.info["actor_context"])
    assert get_sales_order_metrics(db_session, actor_context=db_session.info["actor_context"])["open_order_value"] == Decimal("0.00")
    assert get_sales_order_summary(db_session, order, actor_context=db_session.info["actor_context"])["balance_due"] is None
