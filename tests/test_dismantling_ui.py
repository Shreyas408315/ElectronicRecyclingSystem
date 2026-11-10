import uuid
from decimal import Decimal
from unittest.mock import patch
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidgetItem
from PySide6.QtCore import Qt
from database.connection import get_db, initialize_database
from services.supplier_service import create_supplier
from services.purchase_service import create_purchase
from services.dismantling_service import create_dismantling_entry, get_dismantling_by_id
from services.security_service import authenticate, create_user, logout
from ui.main_window import MainWindow
from ui.dismantling_page import DismantlingPage
from ui.dismantling_form_dialog import DismantlingFormDialog
from ui.dismantling_detail_dialog import DismantlingDetailDialog


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def init_db():
    initialize_database()


def test_dismantling_form_dialog_validation_and_calculations(qapp, authenticated_actor):
    dialog = DismantlingFormDialog()
    dialog.show()
    assert dialog.txt_code.text().startswith("DIS-")

    # 1. Save without selecting purchase/item should show error
    dialog._save_entry("Draft")
    assert not dialog.lbl_error.isHidden()
    assert "select an inward purchase order" in dialog.lbl_error.text()

    # 2. Create supplier & purchase with appliances
    run_id = uuid.uuid4().hex[:6]
    with get_db() as db:
        sup = create_supplier(db, {
            "business_name": f"DismantleSupplier_{run_id}",
            "phone": "9812345672",
            "is_active": True,
        }, actor_context=authenticated_actor)
        p = create_purchase(
            db,
            {"supplier_id": sup.id},
            [{"appliance_category": "Refrigerators", "brand": "Whirlpool", "quantity": 5, "rate": Decimal("1000.00")}],
            actor_context=authenticated_actor,
        )
        p_id = p.id
        p_item_id = p.items[0].id

    # Reload purchases into combo
    dialog._load_purchases_data()
    idx_p = dialog.combo_purchase.findData(p_id)
    assert idx_p >= 0
    dialog.combo_purchase.setCurrentIndex(idx_p)

    idx_it = dialog.combo_item.findData(p_item_id)
    assert idx_it >= 0
    dialog.combo_item.setCurrentIndex(idx_it)
    assert f"DismantleSupplier_{run_id}" in dialog.txt_supplier.text()
    assert dialog.txt_category.text() == "Refrigerators"
    assert dialog.txt_brand.text() == "Whirlpool"

    # 3. Save without components or scrap should show error
    dialog._save_entry("Draft")
    assert not dialog.lbl_error.isHidden()
    assert "at least one recovered component or scrap" in dialog.lbl_error.text()

    # 4. Add component and scrap
    dialog.combo_c_name.setEditText("Heavy Compressor")
    dialog.spin_c_qty.setValue(2.0)
    dialog.spin_c_weight.setValue(25.0)
    dialog.spin_c_val.setValue(1200.0)
    dialog._add_component_row()
    assert len(dialog.components_data) == 1
    assert dialog.table_comp.rowCount() == 1
    dialog.table_comp.selectRow(0)
    dialog._edit_selected_component()
    dialog.combo_c_name.setEditText("Updated Heavy Compressor")
    dialog._add_component_row()
    assert len(dialog.components_data) == 1
    assert dialog.components_data[0]["component_name"] == "Updated Heavy Compressor"

    dialog.combo_s_type.setEditText("Plastic Shards")
    dialog.spin_s_weight.setValue(15.0)
    dialog.spin_s_val.setValue(100.0)
    dialog._add_scrap_row()
    assert len(dialog.scrap_data) == 1
    assert dialog.table_scrap.rowCount() == 1
    dialog.table_scrap.selectRow(0)
    dialog._edit_selected_scrap()
    dialog.combo_s_type.setEditText("Recycled Plastic Shards")
    dialog._add_scrap_row()
    assert len(dialog.scrap_data) == 1
    assert dialog.scrap_data[0]["scrap_type"] == "Recycled Plastic Shards"

    # Verify live calculated metrics
    # Recovered weight: 25.00 kg. Scrap: 15.00 kg.
    assert "25.00 kg" in dialog.lbl_rec_weight.text()
    assert "15.00 kg" in dialog.lbl_scrap_weight.text()
    # Valuation: (2 * 1200) + 100 = 2500.00
    assert "₹ 2,500.00" in dialog.lbl_est_value.text()

    dialog.close()


def test_dismantling_page_filtering_and_navigation(qapp, authenticated_actor):
    run_id = uuid.uuid4().hex[:6]
    with get_db() as db:
        sup = create_supplier(db, {
            "business_name": f"PageDismantleSup_{run_id}",
            "phone": "9812345673",
            "is_active": True,
        }, actor_context=authenticated_actor)
        p = create_purchase(
            db,
            {"supplier_id": sup.id},
            [{"appliance_category": "Microwaves", "quantity": 4, "rate": Decimal("500.00")}],
            actor_context=authenticated_actor,
        )
        entry = create_dismantling_entry(
            db,
            {
                "purchase_id": p.id,
                "purchase_item_id": p.items[0].id,
                "appliances_dismantled": 2,
                "team_leader": f"TeamLead_{run_id}",
                "status": "Pending Approval",
            },
            [{"component_name": "Magnetron", "quantity": Decimal("2.00"), "weight": Decimal("4.00")}],
            [],
            actor_context=authenticated_actor,
        )
        entry_code = entry.dismantling_code

    page = DismantlingPage()
    page.load_dismantling_entries()
    assert page.table_recent.columnCount() == 6

    # Search by unique code
    page.txt_search.setText(entry_code)
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == entry_code

    # Search by team leader
    page.txt_search.setText(f"TeamLead_{run_id}")
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == entry_code

    # Clear search
    page.txt_search.setText("")

    # Filter status Pending Approval
    idx_st = page.combo_status.findData("Pending Approval")
    page.combo_status.setCurrentIndex(idx_st)
    assert page.table.rowCount() >= 1

    page.close()


def test_main_window_dismantling_navigation(qapp):
    window = MainWindow()
    assert window.dismantling_page is not None
    assert not window.dismantling_page.lbl_access_error.isHidden()
    assert window.dismantling_page.table.rowCount() == 0

    # Navigate to Dismantling & Recovery (index 3)
    window.nav.setCurrentRow(3)
    assert window.stack.currentIndex() == 3
    assert isinstance(window.stack.currentWidget(), DismantlingPage)

    # Check that other modules still exist
    assert window.purchase_page is not None
    assert window.supplier_page is not None

    window.close()


def test_dismantling_detail_dialog_approval_action(qapp, authenticated_actor):
    with get_db() as db:
        sup = create_supplier(db, {
            "business_name": "ApprovalTest Sup",
            "phone": "9812345674",
            "is_active": True,
        }, actor_context=authenticated_actor)
        p = create_purchase(
            db,
            {"supplier_id": sup.id},
            [{"appliance_category": "Air Conditioners", "quantity": 2, "rate": Decimal("2000.00")}],
            actor_context=authenticated_actor,
        )
        entry = create_dismantling_entry(
            db,
            {
                "purchase_id": p.id,
                "purchase_item_id": p.items[0].id,
                "appliances_dismantled": 1,
                "status": "Draft",
            },
            [{"component_name": "AC Blower Motor", "quantity": Decimal("1.00"), "condition": "Working"}],
            [],
            actor_context=authenticated_actor,
        )
        e_id = entry.id
        e_code = entry.dismantling_code

    dialog = DismantlingDetailDialog(entry_id=e_id)
    dialog.show()
    assert e_code in dialog.lbl_title.text()
    assert "DRAFT" in dialog.lbl_status_badge.text()
    assert dialog.tbl_comp.rowCount() == 1

    # Buttons check in Draft mode
    assert not dialog.btn_approve.isHidden()
    assert not dialog.btn_edit.isHidden()

    dialog.close()


def test_dismantling_detail_dialog_fails_closed_without_view_permission(qapp, monkeypatch):
    import ui.dismantling_detail_dialog as detail_dialog_module

    monkeypatch.setattr(detail_dialog_module, "current_actor_context", lambda: None)
    with patch.object(detail_dialog_module.QMessageBox, "warning") as warning:
        dialog = DismantlingDetailDialog(entry_id=1)

    assert dialog.entry_data is None
    assert dialog.btn_edit.isHidden()
    assert dialog.btn_approve.isHidden()
    warning.assert_called_once()
    dialog.close()


def test_dismantling_page_disables_protected_actions_without_view_permission(
    qapp, monkeypatch
):
    import ui.dismantling_page as dismantling_page_module

    monkeypatch.setattr(dismantling_page_module, "current_actor_context", lambda: None)
    page = DismantlingPage()
    page.table.setRowCount(1)
    id_item = QTableWidgetItem("DIS-000001")
    id_item.setData(Qt.UserRole, 1)
    page.table.setItem(0, 0, id_item)

    with patch.object(QMessageBox, "warning") as warning:
        page.table.selectRow(0)

    assert not page.btn_view_details.isEnabled()
    assert not page.btn_approve_selected.isEnabled()
    assert not page.btn_reject_selected.isEnabled()
    warning.assert_called_once()
    page.close()


def test_dismantling_page_clears_stale_rows_when_permission_is_unavailable(
    qapp, monkeypatch
):
    import ui.dismantling_page as dismantling_page_module

    monkeypatch.setattr(dismantling_page_module, "current_actor_context", lambda: None)
    page = DismantlingPage()
    page.table.setRowCount(1)
    page.table.setItem(0, 0, QTableWidgetItem("Previously loaded batch"))
    page.table_recent.setRowCount(1)
    page.card_batches["val_label"].setText("1")

    page.load_dismantling_entries()

    assert page.table.rowCount() == 0
    assert page.table_recent.rowCount() == 0
    assert page.card_batches["val_label"].text() == "—"
    assert not page.lbl_access_error.isHidden()
    page.close()


def test_dismantling_form_disables_protected_inputs_without_view_permission(
    qapp, monkeypatch
):
    import ui.dismantling_form_dialog as form_dialog_module

    monkeypatch.setattr(form_dialog_module, "current_actor_context", lambda: None)
    dialog = DismantlingFormDialog()

    assert dialog.available_purchases == []
    assert dialog.combo_purchase.count() == 0
    assert dialog.txt_code.text() == ""
    assert not dialog.btn_save_draft.isEnabled()
    assert not dialog.btn_submit.isEnabled()
    assert "Authentication or permission required" in dialog.lbl_error.text()
    dialog.close()


def test_inventory_operator_can_view_but_not_manage_or_approve_dismantling(
    qapp, authenticated_actor, monkeypatch
):
    import ui.dismantling_detail_dialog as detail_dialog_module
    import ui.dismantling_page as dismantling_page_module

    with get_db() as db:
        supplier = create_supplier(
            db,
            {
                "business_name": "Inventory Operator UI Supplier",
                "phone": "9812345688",
                "is_active": True,
            },
            actor_context=authenticated_actor,
        )
        purchase = create_purchase(
            db,
            {"supplier_id": supplier.id},
            [{
                "appliance_category": "Refrigerators",
                "quantity": 2,
                "rate": Decimal("100.00"),
            }],
            actor_context=authenticated_actor,
        )
        entry = create_dismantling_entry(
            db,
            {
                "purchase_id": purchase.id,
                "purchase_item_id": purchase.items[0].id,
                "appliances_dismantled": 1,
                "status": "Draft",
            },
            [{"component_name": "UI Test Motor", "quantity": 1}],
            [],
            actor_context=authenticated_actor,
        )
        create_user(
            db,
            actor_context=authenticated_actor,
            username="dismantling-inventory-ui",
            password="Dismantling-ui-test-2026!",
            full_name="Dismantling Inventory UI",
            role_names=["Inventory Operator"],
        )
        entry_id = entry.id

    with get_db() as db:
        operator = authenticate(
            db, "dismantling-inventory-ui", "Dismantling-ui-test-2026!"
        )

    monkeypatch.setattr(
        dismantling_page_module, "current_actor_context", lambda: operator
    )
    monkeypatch.setattr(
        detail_dialog_module, "current_actor_context", lambda: operator
    )
    page = DismantlingPage()
    assert not page.btn_new_entry.isEnabled()
    assert page.table.rowCount() == 1
    page.table.selectRow(0)
    assert page.btn_view_details.isEnabled()
    assert not page.btn_approve_selected.isEnabled()
    assert not page.btn_reject_selected.isEnabled()

    dialog = DismantlingDetailDialog(entry_id=entry_id)
    assert dialog.btn_edit.isHidden()
    assert dialog.btn_approve.isHidden()
    assert dialog.btn_reject.isHidden()

    dialog.close()
    page.close()
    with get_db() as db:
        logout(db, actor_context=operator)
