from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from openpyxl import load_workbook

from database.connection import Base, _migrate_payment_reference_scopes
from database.models import CustomerReceiptAllocation, Purchase
from services.customer_receipt_service import (
    PaymentValidationError,
    apply_customer_advance,
    calculate_invoice_balance,
    create_customer_receipt,
    get_customer_financial_summary,
    post_customer_receipt,
    update_customer_receipt,
    update_customer_receipt,
    void_customer_receipt,
)
from services.customer_service import create_customer
from services.inventory_service import create_inventory_item, record_stock_movement
from services.sales_invoice_service import (
    create_invoice_from_fulfilled_order,
    issue_sales_invoice,
    update_invoice_factory_settings,
)
from services.sales_order_service import (
    confirm_sales_order,
    create_draft_order,
    fulfil_sales_order,
)
from services.supplier_payment_service import (
    calculate_purchase_balance,
    create_supplier_payment,
    get_supplier_financial_summary,
    post_supplier_payment,
    void_supplier_payment,
)
from services.supplier_service import create_supplier
from services.purchase_service import create_purchase
from services.payment_report_service import (
    generate_customer_receipt_pdf,
    generate_payment_excel_report,
    generate_payment_pdf_report,
    generate_supplier_payment_voucher_pdf,
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


def test_payment_reference_scope_migration_drops_global_indexes_and_backfills():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE customer_receipts ("
            "id INTEGER PRIMARY KEY, transaction_reference VARCHAR(100), "
            "payment_method VARCHAR(30), bank_account_name VARCHAR(150), status VARCHAR(20))"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX ix_customer_receipts_transaction_reference "
            "ON customer_receipts (transaction_reference)"
        ))
        connection.execute(text(
            "CREATE TABLE supplier_payments ("
            "id INTEGER PRIMARY KEY, transaction_reference VARCHAR(100), "
            "payment_method VARCHAR(30), bank_account_name VARCHAR(150), status VARCHAR(20))"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX ix_supplier_payments_transaction_reference "
            "ON supplier_payments (transaction_reference)"
        ))
        connection.execute(text(
            "INSERT INTO customer_receipts VALUES "
            "(1, 'RC-1', 'UPI', '  Bank A ', 'Posted'), "
            "(2, 'RC-2', 'NEFT', 'Bank B', 'Draft')"
        ))
        connection.execute(text(
            "INSERT INTO supplier_payments VALUES "
            "(1, 'SP-1', 'Cheque', 'Bank C', 'Voided')"
        ))
        Base.metadata.tables["payment_transaction_reference_scopes"].create(connection)

        _migrate_payment_reference_scopes(connection)

        rows = connection.execute(text(
            "SELECT transaction_reference, transaction_kind, payment_method, "
            "account_scope, transaction_id "
            "FROM payment_transaction_reference_scopes ORDER BY transaction_kind"
        )).all()
        assert rows == [
            ("RC-1", "Customer Receipt", "UPI", "bank a", 1),
            ("SP-1", "Supplier Payment", "Cheque", "bank c", 1),
        ]
        for table_name in ("customer_receipts", "supplier_payments"):
            index = next(
                item for item in inspect(connection).get_indexes(table_name)
                if item["name"] == f"ix_{table_name}_transaction_reference"
            )
            assert not index["unique"]
    engine.dispose()


def _customer(session, name="Payment Customer", opening="25.00"):
    return create_customer(session, {
        "customer_name": name,
        "customer_type": "Distributor",
        "opening_balance": opening,
    }, actor_context=session.info["actor_context"], allow_potential_duplicate=True)


def _supplier(session, actor_context, name="Payment Supplier", opening="15.00"):
    return create_supplier(session, {
        "business_name": name,
        "opening_balance": float(opening),
        "is_active": True,
    }, actor_context=actor_context)


def _issued_invoice(session, customer, *, due_date=None, amount="100.00"):
    update_invoice_factory_settings(session, {
        "name": "Test Recycling Works",
        "address": "Test Facility Address",
    }, actor_context=session.info["actor_context"])
    inventory = create_inventory_item(session, {
        "part_name": "Recovered Component",
        "category": "Components",
        "unit": "pieces",
        "condition": "Working",
        "cost_price": "1.00",
        "estimated_selling_price": amount,
    }, actor_context=session.info["actor_context"])
    record_stock_movement(
        session, inventory.id, "ADJUSTMENT_IN", "5", "5",
        reason="Test opening balance", user_responsible="Test",
        weight_verified=True,
        actor_context=session.info["actor_context"],
    )
    order = create_draft_order(session, {"customer_id": customer.id}, [{
        "inventory_item_id": inventory.id,
        "quantity_ordered": "1",
        "unit_selling_price": amount,
    }], actor_context=session.info["actor_context"])
    confirm_sales_order(session, order.id, actor_context=session.info["actor_context"])
    fulfil_sales_order(session, order.id, {order.items[0].id: "1"}, idempotency_key=uuid4().hex, actor_context=session.info["actor_context"])
    invoice = create_invoice_from_fulfilled_order(session, order.id, actor_context=session.info["actor_context"])
    if due_date is not None:
        invoice.due_date = due_date
    issue_sales_invoice(session, invoice.id, actor_context=session.info["actor_context"])
    return invoice


def _purchase(
    session,
    supplier,
    *,
    actor_context,
    code,
    total="10000.00",
    historical_paid="3000.00",
):
    return create_purchase(
        session,
        {
            "supplier_id": supplier.id,
            "purchase_code": code,
            "amount_paid": historical_paid,
        },
        [{
            "appliance_category": "Refrigerators",
            "quantity": 1,
            "rate": total,
        }],
        actor_context=actor_context,
    )


def test_customer_receipts_are_sequential_and_payments_apply_only_when_posted(db_session):
    customer = _customer(db_session)
    invoice = _issued_invoice(db_session, customer)
    first = create_customer_receipt(db_session, {
        "customer_id": customer.id,
        "amount_received": "40.005",
        "payment_method": "UPI",
    }, [{
        "allocation_type": "Invoice",
        "invoice_id": invoice.id,
        "amount": "40.00",
    }], actor_context=db_session.info["actor_context"])
    assert first.receipt_code == "RCPT-000001"
    assert calculate_invoice_balance(db_session, invoice.id, actor_context=db_session.info["actor_context"])["received"] == Decimal("0.00")
    post_customer_receipt(db_session, first.id, actor_context=db_session.info["actor_context"])
    assert calculate_invoice_balance(db_session, invoice.id, actor_context=db_session.info["actor_context"])["balance"] == Decimal("60.00")

    second = create_customer_receipt(db_session, {
        "customer_id": customer.id,
        "amount_received": "60.00",
        "payment_method": "Bank Transfer",
    }, [{
        "allocation_type": "Invoice",
        "invoice_id": invoice.id,
        "amount": "60.00",
    }], actor_context=db_session.info["actor_context"])
    assert second.receipt_code == "RCPT-000002"
    post_customer_receipt(db_session, second.id, actor_context=db_session.info["actor_context"])
    result = calculate_invoice_balance(db_session, invoice.id, actor_context=db_session.info["actor_context"])
    assert result["payment_status"] == "Paid"
    assert result["balance"] == Decimal("0.00")


def test_customer_opening_balance_and_explicit_advance_application(db_session):
    customer = _customer(db_session, opening="30.00")
    invoice = _issued_invoice(db_session, customer, amount="100.00")
    receipt = create_customer_receipt(db_session, {
        "customer_id": customer.id,
        "amount_received": "50.00",
    }, [{
        "allocation_type": "Opening Balance",
        "amount": "30.00",
    }], actor_context=db_session.info["actor_context"])
    post_customer_receipt(db_session, receipt.id, actor_context=db_session.info["actor_context"])
    summary = get_customer_financial_summary(db_session, customer.id, actor_context=db_session.info["actor_context"])
    assert summary["remaining_opening_receivable"] == Decimal("0.00")
    assert summary["unallocated_advances"] == Decimal("20.00")
    assert calculate_invoice_balance(db_session, invoice.id, actor_context=db_session.info["actor_context"])["received"] == Decimal("0.00")

    apply_customer_advance(db_session, receipt.id, {
        "allocation_type": "Invoice",
        "invoice_id": invoice.id,
        "amount": "20.00",
    }, actor_context=db_session.info["actor_context"])
    summary = get_customer_financial_summary(db_session, customer.id, actor_context=db_session.info["actor_context"])
    assert summary["unallocated_advances"] == Decimal("0.00")
    assert calculate_invoice_balance(db_session, invoice.id, actor_context=db_session.info["actor_context"])["received"] == Decimal("20.00")
    assert db_session.scalar(
        select(CustomerReceiptAllocation).where(
            CustomerReceiptAllocation.receipt_id == receipt.id,
            CustomerReceiptAllocation.allocation_source == "Advance Application",
        )
    ).amount == Decimal("20.00")


def test_customer_receipt_validation_and_void_release_preserve_history(db_session):
    customer = _customer(db_session)
    other_customer = _customer(db_session, "Other Customer", "0")
    invoice = _issued_invoice(db_session, customer)
    other_invoice = _issued_invoice(db_session, other_customer)
    with pytest.raises(PaymentValidationError, match="issued receivable"):
        create_customer_receipt(db_session, {
            "customer_id": customer.id,
            "amount_received": "10",
        }, [{
            "allocation_type": "Invoice",
            "invoice_id": other_invoice.id,
            "amount": "10",
        }], actor_context=db_session.info["actor_context"])
    receipt = create_customer_receipt(db_session, {
        "customer_id": customer.id,
        "amount_received": "101",
    }, [{
        "allocation_type": "Invoice",
        "invoice_id": invoice.id,
        "amount": "100",
    }], actor_context=db_session.info["actor_context"])
    with pytest.raises(PaymentValidationError, match="Allocations cannot exceed"):
        update_customer_receipt(db_session, receipt.id, {"amount_received": "99"}, actor_context=db_session.info["actor_context"])
    assert receipt.status == "Draft"

    update_customer_receipt(db_session, receipt.id, {"amount_received": "100"}, actor_context=db_session.info["actor_context"])
    post_customer_receipt(db_session, receipt.id, actor_context=db_session.info["actor_context"])
    with pytest.raises(PaymentValidationError, match="reason"):
        void_customer_receipt(db_session, receipt.id, "", actor_context=db_session.info["actor_context"])
    void_customer_receipt(db_session, receipt.id, "Duplicate entry", actor_context=db_session.info["actor_context"])
    assert calculate_invoice_balance(db_session, invoice.id, actor_context=db_session.info["actor_context"])["balance"] == Decimal("100.00")
    assert len(receipt.allocations) == 1
    with pytest.raises(PaymentValidationError, match="Only a posted"):
        void_customer_receipt(db_session, receipt.id, "Duplicate void", actor_context=db_session.info["actor_context"])


def test_invoice_balance_due_date_is_derived_and_draft_voided_are_excluded(db_session):
    customer = _customer(db_session, opening="0")
    overdue = _issued_invoice(
        db_session, customer, due_date=date.today() - timedelta(days=1)
    )
    balance = calculate_invoice_balance(db_session, overdue.id, actor_context=db_session.info["actor_context"])
    assert balance["payment_status"] == "Overdue"


def test_supplier_historical_amount_paid_is_counted_once_and_immutable(db_session, authenticated_actor_factory):
    actor_context = authenticated_actor_factory(db_session)
    supplier = _supplier(db_session, actor_context)
    purchase = _purchase(
        db_session, supplier, actor_context=actor_context, code="PUR-TEST-1",
        total="10000.00", historical_paid="3000.00",
    )
    assert calculate_purchase_balance(db_session, purchase.id, actor_context=db_session.info["actor_context"])["balance"] == Decimal("7000.00")
    payment = create_supplier_payment(db_session, {
        "supplier_id": supplier.id,
        "amount_paid": "2000.00",
        "payment_method": "NEFT",
    }, [{
        "allocation_type": "Purchase",
        "purchase_id": purchase.id,
        "amount": "2000.00",
    }], actor_context=db_session.info["actor_context"])
    assert payment.payment_code == "SPAY-000001"
    post_supplier_payment(db_session, payment.id, actor_context=db_session.info["actor_context"])
    result = calculate_purchase_balance(db_session, purchase.id, actor_context=db_session.info["actor_context"])
    assert result["balance"] == Decimal("5000.00")
    assert result["historical_amount_paid"] == Decimal("3000.00")
    db_session.refresh(purchase)
    assert purchase.amount_paid == Decimal("3000.00")
    assert purchase.balance_amount == Decimal("7000.00")


def test_supplier_multiple_allocations_advances_and_voiding(db_session, authenticated_actor_factory):
    actor_context = authenticated_actor_factory(db_session)
    supplier = _supplier(db_session, actor_context, opening="100.00")
    first = _purchase(
        db_session, supplier, actor_context=actor_context,
        code="PUR-TEST-2", total="500.00", historical_paid="100.00",
    )
    second = _purchase(
        db_session, supplier, actor_context=actor_context,
        code="PUR-TEST-3", total="400.00", historical_paid="50.00",
    )
    payment = create_supplier_payment(db_session, {
        "supplier_id": supplier.id,
        "amount_paid": "300.00",
    }, [
        {"allocation_type": "Opening Balance", "amount": "100.00"},
        {"allocation_type": "Purchase", "purchase_id": first.id, "amount": "150.00"},
    ], actor_context=db_session.info["actor_context"])
    post_supplier_payment(db_session, payment.id, actor_context=db_session.info["actor_context"])
    summary = get_supplier_financial_summary(db_session, supplier.id, actor_context=db_session.info["actor_context"])
    assert summary["remaining_opening_payable"] == Decimal("0.00")
    assert summary["purchase_payables"] == Decimal("600.00")
    assert summary["unallocated_advances"] == Decimal("50.00")
    assert calculate_purchase_balance(db_session, first.id, actor_context=db_session.info["actor_context"])["balance"] == Decimal("250.00")
    assert calculate_purchase_balance(db_session, second.id, actor_context=db_session.info["actor_context"])["balance"] == Decimal("350.00")

    void_supplier_payment(db_session, payment.id, "Wrong allocation", actor_context=db_session.info["actor_context"])
    summary = get_supplier_financial_summary(db_session, supplier.id, actor_context=db_session.info["actor_context"])
    assert summary["remaining_opening_payable"] == Decimal("100.00")
    assert calculate_purchase_balance(db_session, first.id, actor_context=db_session.info["actor_context"])["balance"] == Decimal("400.00")
    assert summary["unallocated_advances"] == Decimal("0.00")
    assert payment.allocations


def test_transaction_reference_uniqueness_is_scoped_by_direction_method_and_account(db_session, authenticated_actor_factory):
    customer = _customer(db_session, opening="0")
    supplier = _supplier(db_session, authenticated_actor_factory(db_session), opening="0")
    receipt = create_customer_receipt(db_session, {
        "customer_id": customer.id,
        "amount_received": "20",
        "transaction_reference": "BANK-REF-100",
        "payment_method": "UPI",
        "bank_account_name": "Receiving Bank A",
    }, actor_context=db_session.info["actor_context"])
    update_customer_receipt(db_session, receipt.id, {
        "customer_id": customer.id,
        "amount_received": "25",
        "transaction_reference": "BANK-REF-100",
        "payment_method": "UPI",
        "bank_account_name": "Receiving Bank A",
    }, actor_context=db_session.info["actor_context"])
    assert receipt.amount_received == Decimal("25.00")
    post_customer_receipt(db_session, receipt.id, actor_context=db_session.info["actor_context"])

    with pytest.raises(PaymentValidationError, match="already recorded"):
        create_customer_receipt(db_session, {
            "customer_id": customer.id,
            "amount_received": "10",
            "payment_method": "UPI",
            "bank_account_name": "receiving   bank a",
            "transaction_reference": "BANK-REF-100",
        }, actor_context=db_session.info["actor_context"])

    other_account = create_customer_receipt(db_session, {
        "customer_id": customer.id,
        "amount_received": "10",
        "payment_method": "UPI",
        "bank_account_name": "Receiving Bank B",
        "transaction_reference": "BANK-REF-100",
    }, actor_context=db_session.info["actor_context"])
    post_customer_receipt(db_session, other_account.id, actor_context=db_session.info["actor_context"])

    other_method = create_customer_receipt(db_session, {
        "customer_id": customer.id,
        "amount_received": "10",
        "payment_method": "NEFT",
        "bank_account_name": "Receiving Bank A",
        "transaction_reference": "BANK-REF-100",
    }, actor_context=db_session.info["actor_context"])
    post_customer_receipt(db_session, other_method.id, actor_context=db_session.info["actor_context"])

    supplier_payment = create_supplier_payment(db_session, {
        "supplier_id": supplier.id,
        "amount_paid": "10",
        "payment_method": "UPI",
        "bank_account_name": "Receiving Bank A",
        "transaction_reference": "BANK-REF-100",
    }, actor_context=db_session.info["actor_context"])
    post_supplier_payment(db_session, supplier_payment.id, actor_context=db_session.info["actor_context"])

    with pytest.raises(PaymentValidationError, match="greater than zero"):
        create_supplier_payment(db_session, {
            "supplier_id": supplier.id,
            "amount_paid": "0",
        }, actor_context=db_session.info["actor_context"])


def test_payment_documents_and_register_reports(db_session, tmp_path, authenticated_actor_factory):
    customer = _customer(db_session, opening="0")
    supplier = _supplier(db_session, authenticated_actor_factory(db_session), opening="0")
    receipt = create_customer_receipt(db_session, {
        "customer_id": customer.id,
        "amount_received": "25",
        "payment_method": "UPI",
        "transaction_reference": "UPI-TEST-1",
    }, actor_context=db_session.info["actor_context"])
    payment = create_supplier_payment(db_session, {
        "supplier_id": supplier.id,
        "amount_paid": "15",
        "payment_method": "NEFT",
        "transaction_reference": "NEFT-TEST-1",
    }, actor_context=db_session.info["actor_context"])
    post_customer_receipt(db_session, receipt.id, actor_context=db_session.info["actor_context"])
    post_supplier_payment(db_session, payment.id, actor_context=db_session.info["actor_context"])

    receipt_pdf = tmp_path / "receipt.pdf"
    voucher_pdf = tmp_path / "voucher.pdf"
    register_pdf = tmp_path / "register.pdf"
    register_excel = tmp_path / "register.xlsx"
    generate_customer_receipt_pdf(db_session, receipt.id, str(receipt_pdf), actor_context=db_session.info["actor_context"])
    generate_supplier_payment_voucher_pdf(db_session, payment.id, str(voucher_pdf), actor_context=db_session.info["actor_context"])
    generate_payment_pdf_report(db_session, str(register_pdf), actor_context=db_session.info["actor_context"])
    generate_payment_excel_report(db_session, str(register_excel), actor_context=db_session.info["actor_context"])
    assert receipt_pdf.read_bytes().startswith(b"%PDF")
    assert voucher_pdf.read_bytes().startswith(b"%PDF")
    assert register_pdf.read_bytes().startswith(b"%PDF")
    workbook = load_workbook(register_excel, read_only=True)
    assert "Customer Receipts" in workbook.sheetnames
    assert "Supplier Payments" in workbook.sheetnames
    assert "Invoice Outstanding" in workbook.sheetnames
    assert "Purchase Outstanding" in workbook.sheetnames
    workbook.close()
