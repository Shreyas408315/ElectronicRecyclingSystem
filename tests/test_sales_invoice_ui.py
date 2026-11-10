import pytest
from uuid import uuid4
from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidget, QTableWidgetItem

from database.connection import get_db
from services.customer_service import create_customer
from services.inventory_service import create_inventory_item, record_stock_movement
from services.security_service import authenticate, create_user, logout
from services.sales_invoice_service import (
    create_invoice_from_fulfilled_order,
    issue_sales_invoice,
    update_invoice_factory_settings,
)
from services.sales_order_service import confirm_sales_order, create_draft_order, fulfil_sales_order
from ui.customer_detail_dialog import CustomerDetailDialog
from ui.main_window import MainWindow
from ui.sales_invoice_page import SalesInvoiceForm, SalesInvoicePage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_invoice_page_navigation_form_and_customer_issued_history(
    qapp, authenticated_actor
):
    with get_db() as db:
        customer = create_customer(db, {
            "customer_name": "Invoice UI Customer",
            "customer_type": "Retailer",
            "phone": "9876543210",
            "billing_address": "Main Road",
            "state": "Maharashtra",
            "pin_code": "411001",
            "payment_terms": "15 days",
        }, actor_context=authenticated_actor)
        item = create_inventory_item(db, {
            "part_name": "Invoice UI Component",
            "category": "Components",
            "unit": "pieces",
            "condition": "Working",
            "estimated_selling_price": "10",
        }, actor_context=authenticated_actor)
        record_stock_movement(
            db, item.id, "ADJUSTMENT_IN", "5",
            reason="Verified test stock", user_responsible="Tester",
            actor_context=authenticated_actor,
        )
        order = create_draft_order(db, {
            "customer_id": customer.id,
        }, [{
            "inventory_item_id": item.id,
            "quantity_ordered": "2",
            "unit_selling_price": "10",
        }], actor_context=authenticated_actor)
        confirm_sales_order(db, order.id, actor_context=authenticated_actor)
        fulfil_sales_order(db, order.id, {order.items[0].id: "1"}, idempotency_key=uuid4().hex, actor_context=authenticated_actor)
        update_invoice_factory_settings(db, {
            "name": "Example Recycling Works",
            "address": "Industrial Area",
            "gstin": None,
            "state": "Maharashtra",
        }, actor_context=authenticated_actor)
        invoice = create_invoice_from_fulfilled_order(db, order.id, actor_context=authenticated_actor)
        issue_sales_invoice(db, invoice.id, actor_context=authenticated_actor)
        customer_id, invoice_code = customer.id, invoice.invoice_code

    window = MainWindow()
    window.nav.setCurrentRow(7)
    assert window.stack.currentWidget() is window.sales_invoice_page
    assert window.sales_invoice_page.table.rowCount() == 1
    assert window.sales_invoice_page.table.item(0, 0).text() == invoice_code

    form = SalesInvoiceForm()
    assert form.order.count() == 0

    detail = CustomerDetailDialog(customer_id=customer_id)
    assert detail.tabs.tabText(2) == "Invoice History"
    invoice_table = detail.tabs.widget(2).findChild(QTableWidget)
    assert invoice_table is not None
    assert invoice_table.rowCount() == 1


def test_sales_invoice_page_clears_stale_data_when_view_permission_is_lost(
    qapp, authenticated_actor, monkeypatch
):
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)
    page = SalesInvoicePage()
    page.customer.addItem("Stale protected customer", 999)
    page.table.setRowCount(1)
    page.table.setItem(0, 0, QTableWidgetItem("Stale protected invoice"))
    with get_db() as db:
        user = create_user(
            db,
            actor_context=authenticated_actor,
            username="sales-invoice-no-view",
            password="Sales-invoice-no-view-2026!",
            full_name="Sales Invoice No View",
            role_names=["Procurement"],
        )
        actor = authenticate(db, user.username, "Sales-invoice-no-view-2026!")

    try:
        page.load_invoices()
        assert page.table.rowCount() == 0
        assert page.customer.count() == 1
        assert page.customer.itemText(0) == "All customers"
        assert page.count_label.text() == "Invoice data unavailable"
        assert all(not label.text() for label in page.metric_labels.values())
        assert not page.btn_create.isEnabled()
        assert not page.btn_export.isEnabled()
        assert not page.btn_settings.isEnabled()
        assert all(not button.isEnabled() for button in page._action_buttons.values())
    finally:
        logout(actor_context=actor)
        page.close()


def test_sales_invoice_page_fails_closed_without_authentication(qapp, monkeypatch):
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: pytest.fail("Permission failure opened a modal"),
    )
    page = SalesInvoicePage()

    assert page.table.rowCount() == 0
    assert "Sign in is required" in page.access_warning.text()
    assert not page.btn_create.isEnabled()
    assert not page.btn_export.isEnabled()
    assert not page.btn_settings.isEnabled()
    assert all(not button.isEnabled() for button in page._action_buttons.values())
    page.close()
