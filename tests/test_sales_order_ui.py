from decimal import Decimal

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidget, QTableWidgetItem

from database.connection import get_db
from database.models import InventoryItem
from services.customer_service import create_customer
from services.inventory_service import create_inventory_item, record_stock_movement
from services.security_service import authenticate, create_user, logout
from services.sales_order_service import confirm_sales_order, create_draft_order
from ui.customer_detail_dialog import CustomerDetailDialog
from ui.inventory_dialogs import InventoryDetailDialog
from ui.main_window import MainWindow
from ui.sales_order_page import SalesOrderForm, SalesOrderPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_sales_order_page_navigation_customer_history_and_inventory_reservations(
    qapp, authenticated_actor
):
    with get_db() as db:
        customer = create_customer(db, {
            "customer_name": "UI Order Customer",
            "customer_type": "Retailer",
            "phone": "9876543210",
            "email": "ui-order@example.com",
            "billing_address": "Main Road",
            "city": "Pune",
            "state": "Maharashtra",
            "pin_code": "411001",
            "opening_balance": "0",
            "payment_terms": "Immediate",
        }, actor_context=authenticated_actor)
        item = create_inventory_item(db, {
            "part_name": "UI Sales Component",
            "category": "Components",
            "unit": "pieces",
            "condition": "Working",
            "estimated_selling_price": "10",
        }, actor_context=authenticated_actor)
        record_stock_movement(
            db, item.id, "ADJUSTMENT_IN", "5", reason="UI test stock",
            user_responsible="Tester",
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
        customer_id, item_id, order_id = customer.id, item.id, order.id

    window = MainWindow()
    window.nav.setCurrentRow(6)
    assert window.stack.currentWidget() is window.sales_order_page
    assert window.sales_order_page.table.rowCount() == 1
    assert window.sales_order_page.table.item(0, 0).text() == "SO-000001"

    form = SalesOrderForm(order_id=order_id)
    assert form.customer_combo.currentData() == customer_id
    assert form.line_table.rowCount() == 1

    customer_detail = CustomerDetailDialog(customer_id=customer_id)
    assert customer_detail.tabs.tabText(1) == "Sales History"
    sales_history = customer_detail.tabs.widget(1).findChild(QTableWidget)
    assert sales_history.rowCount() == 1

    with get_db() as db:
        inventory = db.get(InventoryItem, item_id)
        assert inventory.quantity_available == Decimal("5.00")
        assert inventory.reserved_quantity == Decimal("2.00")

    inventory_detail = InventoryDetailDialog(item_id=item_id)
    assert inventory_detail.windowTitle() == "Inventory Item Details"


def test_sales_order_page_clears_stale_data_when_view_permission_is_lost(
    qapp, authenticated_actor, monkeypatch
):
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)
    page = SalesOrderPage()
    page.customer_filter.addItem("Stale protected customer", 999)
    page.table.setRowCount(1)
    page.table.setItem(0, 0, QTableWidgetItem("Stale protected order"))
    with get_db() as db:
        user = create_user(
            db,
            actor_context=authenticated_actor,
            username="sales-order-no-view",
            password="Sales-order-no-view-2026!",
            full_name="Sales Order No View",
            role_names=["Procurement"],
        )
        actor = authenticate(db, user.username, "Sales-order-no-view-2026!")

    try:
        page.load_orders()
        assert page.table.rowCount() == 0
        assert page.customer_filter.count() == 1
        assert page.customer_filter.itemText(0) == "Customer access unavailable"
        assert page.count_label.text() == "Sales order data unavailable"
        assert all(not label.text() for label in page.metric_labels.values())
        assert not page.btn_create.isEnabled()
        assert not page.btn_export.isEnabled()
        assert all(not button.isEnabled() for button in page._action_buttons.values())
    finally:
        logout(actor_context=actor)
        page.close()
