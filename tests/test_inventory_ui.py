import uuid

import pytest
from PySide6.QtWidgets import QApplication

from database.connection import get_db
from services.inventory_service import create_inventory_item, record_stock_movement
from services.security_service import authenticate, create_user, logout
from ui.inventory_page import InventoryPage
from ui.inventory_dialogs import InventoryDetailDialog, InventoryItemDialog, StockMovementDialog
from ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_inventory_page_filters_and_main_navigation(qapp, authenticated_actor):
    part_name = f"UI Copper {uuid.uuid4().hex[:8]}"
    with get_db() as db:
        create_inventory_item(db, {
            "part_name": part_name,
            "category": "Copper",
            "unit": "kg",
            "condition": "Recyclable Raw Material",
            "minimum_stock_level": 0,
        }, actor_context=authenticated_actor)

    page = InventoryPage()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == part_name
    page.table.selectRow(0)
    assert page.btn_details.isEnabled()
    page.txt_search.setText("not a matching part")
    assert page.table.rowCount() == 0

    window = MainWindow()
    window.nav.setCurrentRow(4)
    assert window.stack.currentIndex() == 4
    assert window.stack.currentWidget() is window.inventory_page
    window.close()


def test_inventory_ui_fails_closed_without_authentication(qapp, monkeypatch):
    import ui.inventory_dialogs as inventory_dialogs_module
    import ui.inventory_page as inventory_page_module

    monkeypatch.setattr(inventory_page_module, "current_actor_context", lambda: None)
    monkeypatch.setattr(inventory_dialogs_module, "current_actor_context", lambda: None)
    page = InventoryPage()
    page.table.setRowCount(1)
    page.table_recent.setRowCount(1)
    page.metric_labels["total_unique_items"].setText("1")
    page.load_inventory()

    assert page.table.rowCount() == 0
    assert page.table_recent.rowCount() == 0
    assert page.metric_labels["total_unique_items"].text() == "—"
    assert not page.btn_add.isEnabled()
    assert not page.btn_export.isEnabled()
    assert "Sign in" in page.lbl_access_error.text()

    movement_dialog = StockMovementDialog(page, 1)
    assert movement_dialog.combo_item.count() == 0
    assert not movement_dialog.btn_save.isEnabled()
    detail_dialog = InventoryDetailDialog(page, 1)
    assert "Sign in" in detail_dialog.layout().itemAt(0).widget().text()
    item_dialog = InventoryItemDialog(page)
    assert not item_dialog.btn_save.isEnabled()
    page.close()


def test_inventory_operator_can_adjust_but_not_manage_catalogue(
    qapp, authenticated_actor
):
    with get_db() as db:
        item = create_inventory_item(
            db,
            {
                "part_name": "Operator-visible item",
                "category": "Components",
                "unit": "pieces",
                "condition": "Working",
            },
            actor_context=authenticated_actor,
        )
        create_user(
            db,
            actor_context=authenticated_actor,
            username="inventory-ui-operator",
            password="Inventory-ui-test-2026!",
            full_name="Inventory UI Operator",
            role_names=["Inventory Operator"],
        )
        item_id = item.id

    with get_db() as db:
        actor_context = authenticate(
            db, "inventory-ui-operator", "Inventory-ui-test-2026!"
        )

    page = InventoryPage()
    assert page.table.rowCount() == 1
    page.table.selectRow(0)
    assert not page.btn_add.isEnabled()
    assert not page.btn_edit_catalogue.isEnabled()
    assert page.btn_record_movement.isEnabled()
    assert not page.btn_deactivate.isEnabled()

    dialog = InventoryItemDialog(page)
    assert not dialog.btn_save.isEnabled()
    dialog.close()
    movement = StockMovementDialog(page, item_id)
    assert movement.btn_save.isEnabled()
    movement.close()
    page.close()
    logout(actor_context=actor_context)
