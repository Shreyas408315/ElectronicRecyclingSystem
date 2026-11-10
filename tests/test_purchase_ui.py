import uuid
from decimal import Decimal
import pytest
from PySide6.QtWidgets import QApplication, QLabel, QDialog
from PySide6.QtCore import Qt
from database.connection import get_db, initialize_database
from database.models import Supplier, Purchase
from services.supplier_service import create_supplier
from services.purchase_service import create_purchase, get_purchase_by_id
from ui.main_window import MainWindow
from ui.purchase_page import PurchasePage
from ui.purchase_form_dialog import PurchaseFormDialog
from ui.purchase_detail_dialog import PurchaseDetailDialog


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def init_db():
    initialize_database()


def test_purchase_form_dialog_validation_and_calculations(qapp, authenticated_actor):
    dialog = PurchaseFormDialog()
    dialog.show()
    assert dialog.txt_code.text().startswith("PUR-")

    # 1. Save without selecting supplier should show error
    dialog.combo_supplier.setCurrentIndex(0)  # "Select Active Supplier"
    dialog._save_purchase()
    assert not dialog.lbl_error.isHidden()
    assert "select a valid registered active supplier" in dialog.lbl_error.text()

    # 2. Select an active supplier
    with get_db() as db:
        run_id = uuid.uuid4().hex[:6]
        sup = create_supplier(db, {
            "business_name": f"FormSupplier_{run_id}",
            "contact_person": "Vikram",
            "phone": "9812345670",
            "is_active": True,
        }, actor_context=authenticated_actor)
        sup_id = sup.id

    # Reload suppliers into combo
    dialog._load_initial_data()
    idx = dialog.combo_supplier.findData(sup_id)
    assert idx >= 0
    dialog.combo_supplier.setCurrentIndex(idx)

    # 3. Save without line items should show error
    dialog._save_purchase()
    assert not dialog.lbl_error.isHidden()
    assert "add at least one appliance line item" in dialog.lbl_error.text()

    # 4. Add valid line items
    dialog.combo_item_category.setCurrentText("Refrigerators")
    dialog.spin_item_qty.setValue(3)
    dialog.spin_item_rate.setValue(2000.0)
    dialog._add_line_item()
    assert len(dialog.line_items) == 1
    assert dialog.table_items.rowCount() == 1

    dialog.combo_item_category.setCurrentText("Microwaves")
    dialog.spin_item_qty.setValue(2)
    dialog.spin_item_rate.setValue(500.0)
    dialog._add_line_item()
    assert len(dialog.line_items) == 2
    assert dialog.table_items.rowCount() == 2

    # Verify Subtotal: (3 * 2000) + (2 * 500) = 6000 + 1000 = 7000.00
    assert "₹ 7,000.00" in dialog.lbl_subtotal.text()

    # Add transportation charges: 500.00 -> Total = 7500.00
    dialog.spin_trans.setValue(500.0)
    assert "₹ 7,500.00" in dialog.lbl_total_cost.text()

    # Set amount paid: 3000.00 -> Balance = 4500.00, Status = Partially Paid
    dialog.spin_amount_paid.setValue(3000.0)
    assert "₹ 4,500.00" in dialog.lbl_balance.text()
    assert "Partially Paid" in dialog.lbl_status_badge.text()

    dialog.close()


def test_purchase_page_table_and_filtering(qapp, authenticated_actor):
    run_id = uuid.uuid4().hex[:6]
    with get_db() as db:
        sup = create_supplier(db, {
            "business_name": f"PageSupplier_{run_id}",
            "contact_person": "Prakash",
            "phone": "9812345671",
            "is_active": True,
        }, actor_context=authenticated_actor)
        p = create_purchase(
            db,
            {
                "supplier_id": sup.id,
                "amount_paid": Decimal("1000.00"),
                "remarks": f"SearchTag_{run_id}",
            },
            [{"appliance_category": "Air Conditioners", "quantity": 1, "rate": Decimal("3000.00")}],
            actor_context=authenticated_actor,
        )
        p_code = p.purchase_code

    page = PurchasePage()
    page.load_purchases()

    # Search by unique purchase code
    page.txt_search.setText(p_code)
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == p_code

    # Search by supplier business name
    page.txt_search.setText(f"PageSupplier_{run_id}")
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == p_code

    # Filter by category
    page.txt_search.setText("")
    idx_ac = page.combo_category.findData("Air Conditioners")
    page.combo_category.setCurrentIndex(idx_ac)
    assert page.table.rowCount() >= 1

    page.close()


def test_purchase_page_hides_protected_data_without_purchase_permission(
    qapp, authenticated_actor
):
    with get_db() as db:
        supplier = create_supplier(
            db,
            {"business_name": "Purchase page denied supplier", "phone": "9000000099"},
            actor_context=authenticated_actor,
        )
        create_purchase(
            db,
            {"supplier_id": supplier.id},
            [{"appliance_category": "Air Conditioners", "quantity": 1, "rate": "100"}],
            actor_context=authenticated_actor,
        )
    from services.security_service import logout

    logout(actor_context=authenticated_actor)
    page = PurchasePage()

    assert page.table.rowCount() == 0
    assert page.lbl_table_count.text() == (
        "Sign in with purchase-view permission to view purchases."
    )
    assert all(
        card["val_label"].text() == "—"
        for card in (
            page.card_total,
            page.card_today,
            page.card_month,
            page.card_cost,
            page.card_paid,
            page.card_outstanding,
        )
    )
    page.close()


def test_purchase_form_fails_closed_without_purchase_permissions(
    qapp, authenticated_actor
):
    from services.security_service import logout

    logout(actor_context=authenticated_actor)
    dialog = PurchaseFormDialog()

    assert not dialog.btn_save.isEnabled()
    assert dialog.txt_code.text() == ""
    assert dialog.combo_supplier.count() == 0
    assert "purchase-post permission" in dialog.lbl_error.text()

    dialog._save_purchase()
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.close()


def test_main_window_purchases_navigation(qapp):
    window = MainWindow()
    assert window.purchase_page is not None

    # Navigate to Purchases (index 1)
    window.nav.setCurrentRow(1)
    assert window.stack.currentIndex() == 1
    assert isinstance(window.stack.currentWidget(), PurchasePage)

    # Navigate to Suppliers (index 2)
    window.nav.setCurrentRow(2)
    assert window.stack.currentIndex() == 2

    # Navigate back to Dashboard (index 0)
    window.nav.setCurrentRow(0)
    assert window.stack.currentIndex() == 0

    window.close()


def test_purchase_detail_dialog(qapp, authenticated_actor):
    with get_db() as db:
        sup = create_supplier(db, {
            "business_name": "DetailDialog Vendor",
            "contact_person": "Rajesh",
            "phone": "9820055443",
            "is_active": True,
        }, actor_context=authenticated_actor)
        p = create_purchase(
            db,
            {
                "supplier_id": sup.id,
                "transportation_charges": 250.0,
                "amount_paid": 2250.0,
                "remarks": "Detail dialog test inward record",
            },
            [{"appliance_category": "Washing Machines", "quantity": 2, "rate": 1000.0}],
            actor_context=authenticated_actor,
        )
        p_id = p.id
        p_code = p.purchase_code

    dialog = PurchaseDetailDialog(purchase_id=p_id)
    assert p_code in dialog.lbl_title.text()
    assert "DetailDialog Vendor" in dialog.val_sup_name.text()
    assert "₹ 2,000.00" in dialog.val_subtotal.text()
    assert "₹ 2,250.00" in dialog.val_total.text()
    assert "₹ 2,250.00" in dialog.val_paid.text()
    assert "₹ 0.00" in dialog.val_balance.text()
    assert "PAID" in dialog.lbl_status_badge.text()
    assert dialog.table_items.rowCount() == 1

    dialog.close()


def test_purchase_detail_dialog_fails_closed_without_authentication(
    qapp, authenticated_actor, monkeypatch
):
    with get_db() as db:
        supplier = create_supplier(
            db,
            {"business_name": "Unauthenticated detail vendor", "phone": "9876543210"},
            actor_context=authenticated_actor,
        )
        purchase = create_purchase(
            db,
            {"supplier_id": supplier.id},
            [{"appliance_category": "Televisions & Monitors", "quantity": 1, "rate": "100"}],
            actor_context=authenticated_actor,
        )
        purchase_id = purchase.id
    from services.security_service import logout

    logout(actor_context=authenticated_actor)
    warnings = []
    monkeypatch.setattr(
        "ui.purchase_detail_dialog.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(args[1:]),
    )

    dialog = PurchaseDetailDialog(purchase_id=purchase_id)

    assert dialog.purchase_data is None
    assert warnings
    assert warnings[0][0] == "Authentication Required"
    assert "purchase-view permission" in warnings[0][1]
    dialog.close()


def test_purchase_receipt_action_handles_missing_authentication(
    qapp, authenticated_actor, monkeypatch
):
    with get_db() as db:
        supplier = create_supplier(
            db,
            {"business_name": "Unauthenticated receipt vendor", "phone": "9876543210"},
            actor_context=authenticated_actor,
        )
        purchase = create_purchase(
            db,
            {"supplier_id": supplier.id},
            [{"appliance_category": "Televisions & Monitors", "quantity": 1, "rate": "100"}],
            actor_context=authenticated_actor,
        )
        purchase_id = purchase.id
    from services.security_service import logout

    logout(actor_context=authenticated_actor)
    warnings = []
    monkeypatch.setattr(
        "ui.purchase_page.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(args[1:]),
    )
    page = PurchasePage()
    page.selected_purchase_id = purchase_id

    page._print_selected_receipt()

    assert warnings
    assert warnings[0][0] == "Authentication Required"
    assert "purchase-view permission" in warnings[0][1]
    page.close()
