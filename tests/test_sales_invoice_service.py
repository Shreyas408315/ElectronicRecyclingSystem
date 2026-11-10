from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import (
    SalesInvoiceHistory,
    SalesInvoiceItem,
    SalesOrderFulfilmentItem,
)
from services.customer_service import create_customer
from services.inventory_service import create_inventory_item, record_stock_movement
from services.sales_invoice_report_service import (
    amount_in_words,
    generate_sales_invoice_excel_report,
    generate_sales_invoice_pdf,
    generate_sales_invoice_pdf_report,
)
from services.sales_invoice_service import (
    SalesInvoiceValidationError,
    calculate_invoice_due_date,
    calculate_invoice_item,
    create_invoice_from_fulfilled_order,
    get_sales_invoice_by_id,
    get_uninvoiced_fulfilment_lines,
    issue_sales_invoice,
    update_draft_invoice,
    update_invoice_factory_settings,
    void_sales_invoice,
)
from services.sales_order_service import (
    confirm_sales_order,
    create_draft_order,
    fulfil_sales_order,
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


def _customer(session, *, state="Maharashtra", allow_potential_duplicate=False):
    return create_customer(session, {
        "customer_name": "North Star Components",
        "customer_type": "Distributor",
        "phone": "9876543210",
        "billing_address": "Warehouse Road",
        "state": state,
        "pin_code": "411001",
        "payment_terms": "30 days",
    }, actor_context=session.info["actor_context"],
        allow_potential_duplicate=allow_potential_duplicate)


def _fulfilled_order(
    session, *, state="Maharashtra", quantity="3", allow_potential_duplicate=False
):
    customer = _customer(
        session, state=state, allow_potential_duplicate=allow_potential_duplicate
    )
    item = create_inventory_item(session, {
        "part_name": "Recovered Compressor",
        "category": "Compressor",
        "unit": "pieces",
        "condition": "Working",
        "cost_price": "10",
        "estimated_selling_price": "25",
    }, actor_context=session.info["actor_context"])
    record_stock_movement(
        session, item.id, "ADJUSTMENT_IN", "10", "20",
        reason="Verified opening count", user_responsible="Warehouse",
        weight_verified=True,
        actor_context=session.info["actor_context"],
    )
    order = create_draft_order(
        session,
        {
            "customer_id": customer.id,
            "discount_amount": "1.00",
            "additional_charges": "2.00",
        },
        [{
            "inventory_item_id": item.id,
            "quantity_ordered": quantity,
            "unit_selling_price": "12.50",
            "discount": "0.50",
            "tax_rate": "18",
        }],

        actor_context=session.info["actor_context"],
        )
    confirm_sales_order(session, order.id, actor_context=session.info["actor_context"])
    fulfil_sales_order(session, order.id, {order.items[0].id: "2"}, idempotency_key=uuid4().hex, actor_context=session.info["actor_context"])
    session.flush()
    return customer, item, order


def _configure_factory(session, *, state="Maharashtra"):
    return update_invoice_factory_settings(session, {
        "name": "Example Recycling Works",
        "address": "Industrial Area, Pune",
        "gstin": "27ABCDE1234F1Z5",
        "state": state,
    }, actor_context=session.info["actor_context"])


def test_invoice_from_dispatched_quantity_snapshots_and_sequential_ids(db_session):
    _customer_record, _inventory, order = _fulfilled_order(db_session)
    _configure_factory(db_session)
    first = create_invoice_from_fulfilled_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    assert first.invoice_code == "INV-000001"
    assert first.status == "Draft"
    assert first.items[0].quantity == Decimal("2.00")
    assert first.customer_name_snapshot == "North Star Components"
    assert first.amount_paid is None
    assert first.balance_due is None
    assert first.due_date == date.today() + timedelta(days=30)
    assert first.subtotal == Decimal("25.00")
    assert first.discount_amount == Decimal("1.00")
    assert first.additional_charges == Decimal("1.33")

    issue_sales_invoice(db_session, first.id, actor_context=db_session.info["actor_context"])
    fulfil_sales_order(db_session, order.id, {order.items[0].id: "1"}, idempotency_key=uuid4().hex, actor_context=db_session.info["actor_context"])
    second = create_invoice_from_fulfilled_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    assert second.invoice_code == "INV-000002"
    assert second.items[0].quantity == Decimal("1.00")
    assert second.additional_charges == Decimal("0.67")


def test_partial_invoice_prevents_duplicate_billing_and_can_be_voided(db_session):
    _customer_record, _inventory, order = _fulfilled_order(db_session)
    _configure_factory(db_session)
    quantities = {get_uninvoiced_fulfilment_lines(db_session, order.id, actor_context=db_session.info["actor_context"])[0]["fulfilment_item_id"]: "2"}
    invoice = create_invoice_from_fulfilled_order(
        db_session, order.id, source_quantities=quantities
,
        actor_context=db_session.info["actor_context"],
        )
    second_draft = create_invoice_from_fulfilled_order(
        db_session, order.id, source_quantities=quantities
,
        actor_context=db_session.info["actor_context"],
        )
    issue_sales_invoice(db_session, invoice.id, actor_context=db_session.info["actor_context"])
    line = db_session.get(SalesInvoiceItem, invoice.items[0].id)
    source = db_session.get(SalesOrderFulfilmentItem, line.fulfilment_item_id)
    assert source.invoiced_quantity == Decimal("2.00")
    with pytest.raises(SalesInvoiceValidationError, match="already invoiced"):
        issue_sales_invoice(db_session, second_draft.id, actor_context=db_session.info["actor_context"])
    assert second_draft.status == "Draft"

    void_sales_invoice(
        db_session, invoice.id, "Billing correction",
        actor_context=db_session.info["actor_context"],
    )
    assert invoice.status == "Void"
    db_session.refresh(source)
    assert source.invoiced_quantity == Decimal("0.00")
    history = list(db_session.scalars(
        select(SalesInvoiceHistory).where(SalesInvoiceHistory.invoice_id == invoice.id)
    ))
    assert [(event.from_status, event.to_status) for event in history[-2:]] == [
        ("Draft", "Issued"), ("Issued", "Void")
    ]


def test_tax_calculation_and_explicit_gst_state_configuration(db_session):
    invoice_line = calculate_invoice_item("1", "100", "5", "18", "INTRA_STATE_GST")
    assert invoice_line["taxable_amount"] == Decimal("95.00")
    assert invoice_line["cgst_amount"] == Decimal("8.55")
    assert invoice_line["sgst_amount"] == Decimal("8.55")
    assert invoice_line["igst_amount"] == Decimal("0.00")
    interstate = calculate_invoice_item("1", "100", "0", "18", "INTER_STATE_GST")
    assert interstate["igst_amount"] == Decimal("18.00")
    assert calculate_invoice_item("1", "100", "0", "18", "NO_GST")["tax_amount"] == Decimal("0.00")

    _customer_record, _inventory, order = _fulfilled_order(db_session)
    with pytest.raises(SalesInvoiceValidationError, match="factory GSTIN"):
        create_invoice_from_fulfilled_order(
            db_session, order.id, tax_treatment="INTRA_STATE_GST"
,
            actor_context=db_session.info["actor_context"],
                )
    _configure_factory(db_session)
    invoice = create_invoice_from_fulfilled_order(
        db_session, order.id, tax_treatment="INTRA_STATE_GST"
,
        actor_context=db_session.info["actor_context"],
        )
    assert invoice.tax_amount > Decimal("0.00")
    update_invoice_factory_settings(db_session, {
        "name": "Example Recycling Works",
        "address": "Industrial Area",
        "gstin": "27ABCDE1234F1Z5",
        "state": "Maharashtra",
    }, actor_context=db_session.info["actor_context"])
    with pytest.raises(SalesInvoiceValidationError, match="matching configured"):
        _customer_record, _inventory, second_order = _fulfilled_order(
            db_session, state="Gujarat", allow_potential_duplicate=True
        )
        create_invoice_from_fulfilled_order(
            db_session, second_order.id,
            tax_treatment="INTRA_STATE_GST",

            actor_context=db_session.info["actor_context"],
                )


def test_invoice_draft_edit_and_issued_immutability(db_session):
    _customer_record, _inventory, order = _fulfilled_order(db_session)
    invoice = create_invoice_from_fulfilled_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    original_amount = invoice.grand_total
    update_draft_invoice(
        db_session,
        invoice.id,
        notes="Updated note",
        tax_treatment="NO_GST",
        source_quantities={invoice.items[0].fulfilment_item_id: "1"},

        actor_context=db_session.info["actor_context"],
        )
    assert invoice.notes == "Updated note"
    assert invoice.items[0].quantity == Decimal("1.00")
    assert invoice.grand_total < original_amount
    _configure_factory(db_session)
    issue_sales_invoice(db_session, invoice.id, actor_context=db_session.info["actor_context"])
    with pytest.raises(SalesInvoiceValidationError, match="Only draft invoices"):
        update_draft_invoice(db_session, invoice.id, notes="Forbidden", actor_context=db_session.info["actor_context"])


def test_due_date_decimal_rounding_reports_and_pdf(db_session, tmp_path):
    customer = _customer(db_session)
    assert calculate_invoice_due_date(customer, date(2026, 1, 1)) == date(2026, 1, 31)
    assert calculate_invoice_item("3", "0.335", "0", "0", "NO_GST")["line_total"] == Decimal("1.02")
    assert amount_in_words(Decimal("1234.56")).startswith("Rupees one thousand two hundred thirty four")

    _customer_record, _inventory, order = _fulfilled_order(
        db_session, state="Gujarat", allow_potential_duplicate=True
    )
    invoice = create_invoice_from_fulfilled_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    pdf_path = tmp_path / "invoice.pdf"
    register_pdf = tmp_path / "register.pdf"
    excel_path = tmp_path / "register.xlsx"
    generate_sales_invoice_pdf(db_session, invoice.id, str(pdf_path), actor_context=db_session.info["actor_context"])
    generate_sales_invoice_pdf_report(db_session, str(register_pdf), actor_context=db_session.info["actor_context"])
    generate_sales_invoice_excel_report(db_session, str(excel_path), actor_context=db_session.info["actor_context"])
    assert pdf_path.stat().st_size > 100
    assert register_pdf.stat().st_size > 100
    workbook = load_workbook(excel_path, read_only=True)
    assert {"Invoice Register", "Tax Summary", "Generation History"} <= set(workbook.sheetnames)
    assert workbook["Invoice Register"].max_row == 2
    assert get_sales_invoice_by_id(db_session, invoice.id, actor_context=db_session.info["actor_context"]).invoice_code == "INV-000001"


def test_invalid_gstin_and_nonpositive_or_overbilled_invoice_quantities(db_session):
    _customer_record, _inventory, order = _fulfilled_order(db_session)
    source_id = get_uninvoiced_fulfilment_lines(db_session, order.id, actor_context=db_session.info["actor_context"])[0]["fulfilment_item_id"]
    with pytest.raises(SalesInvoiceValidationError, match="exactly 15"):
        update_invoice_factory_settings(db_session, {
            "name": "Factory", "address": "Address", "gstin": "wrong", "state": "Maharashtra",
        }, actor_context=db_session.info["actor_context"])
    with pytest.raises(SalesInvoiceValidationError, match="no fulfilled quantities"):
        create_invoice_from_fulfilled_order(
            db_session, order.id, source_quantities={source_id: "0"}
,
            actor_context=db_session.info["actor_context"],
                )
    with pytest.raises(SalesInvoiceValidationError, match="uninvoiced units"):
        create_invoice_from_fulfilled_order(
            db_session, order.id, source_quantities={source_id: "3"}
,
            actor_context=db_session.info["actor_context"],
                )


def test_issue_failure_rolls_back_earlier_fulfilment_allocations(db_session):
    customer = _customer(db_session)
    items = []
    for name in ("First component", "Second component"):
        inventory = create_inventory_item(db_session, {
            "part_name": name,
            "category": "Components",
            "unit": "pieces",
            "condition": "Working",
            "estimated_selling_price": "10",
        }, actor_context=db_session.info["actor_context"])
        record_stock_movement(
            db_session, inventory.id, "ADJUSTMENT_IN", "5",
            reason="Verified test stock", user_responsible="Tester",
            actor_context=db_session.info["actor_context"],
        )
        items.append({
            "inventory_item_id": inventory.id,
            "quantity_ordered": "1",
            "unit_selling_price": "10",
        })
    order = create_draft_order(db_session, {"customer_id": customer.id}, items, actor_context=db_session.info["actor_context"])
    confirm_sales_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    fulfil_sales_order(db_session, order.id, {line.id: "1" for line in order.items}, idempotency_key=uuid4().hex, actor_context=db_session.info["actor_context"])
    invoice = create_invoice_from_fulfilled_order(db_session, order.id, actor_context=db_session.info["actor_context"])
    _configure_factory(db_session)

    blocked_line = invoice.items[-1]
    blocked_source = db_session.get(SalesOrderFulfilmentItem, blocked_line.fulfilment_item_id)
    blocked_source.invoiced_quantity = blocked_source.quantity
    db_session.flush()
    first_source = db_session.get(
        SalesOrderFulfilmentItem, invoice.items[0].fulfilment_item_id
    )
    with pytest.raises(SalesInvoiceValidationError, match="already invoiced"):
        issue_sales_invoice(db_session, invoice.id, actor_context=db_session.info["actor_context"])
    db_session.refresh(first_source)
    assert first_source.invoiced_quantity == Decimal("0.00")
    assert invoice.status == "Draft"
    assert not any(event.event_type == "ISSUED" for event in invoice.history)
