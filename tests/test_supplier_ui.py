import os
import pytest
from PySide6.QtWidgets import QApplication, QLabel, QDialog
from PySide6.QtCore import Qt
from database.connection import get_db, initialize_database
from database.models import Supplier
from services.supplier_service import (
    create_supplier,
    generate_next_supplier_code,
    list_suppliers,
    get_supplier_by_id,
)
from services.settings_service import update_settings
from ui.main_window import MainWindow
from services.security_service import authenticate, create_first_administrator, logout


@pytest.fixture(autouse=True)
def authenticated_supplier_ui_actor():
    """Exercise Supplier UI through the same trusted login session as the app."""
    with get_db() as db:
        create_first_administrator(
            db, username="ui-admin", password="Ui-admin-password-2026!", full_name="UI Administrator"
        )
    with get_db() as db:
        yield authenticate(db, "ui-admin", "Ui-admin-password-2026!")
from ui.supplier_page import SupplierPage
from ui.supplier_form_dialog import SupplierFormDialog
from ui.supplier_detail_dialog import SupplierDetailDialog

# Ensure a single QApplication instance for tests
@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def init_db():
    initialize_database()


def test_supplier_form_dialog_validation(qapp):
    """Test validation errors in SupplierFormDialog."""
    dialog = SupplierFormDialog()
    dialog.show()
    assert dialog.txt_code.text().startswith("SUP-")

    # 1. Empty business name should show error
    dialog.txt_name.setText("")
    dialog._save_supplier()
    assert not dialog.lbl_error.isHidden()
    assert "Business/Supplier name is required" in dialog.lbl_error.text()

    # 2. Invalid email format
    dialog.txt_name.setText("Test Electronics")
    dialog.txt_email.setText("bad_email_at_com")
    dialog._save_supplier()
    assert not dialog.lbl_error.isHidden()
    assert "valid email address" in dialog.lbl_error.text()

    # 3. Invalid phone format
    dialog.txt_email.setText("test@test.com")
    dialog.txt_phone.setText("123")
    dialog._save_supplier()
    assert not dialog.lbl_error.isHidden()
    assert "Phone number must contain 10 to 13 digits" in dialog.lbl_error.text()

    # 4. Invalid GSTIN format
    dialog.txt_phone.setText("9876543210")
    dialog.txt_gstin.setText("12345")
    dialog._save_supplier()
    assert not dialog.lbl_error.isHidden()
    assert "GSTIN must be exactly 15 characters" in dialog.lbl_error.text()

    dialog.close()


def test_supplier_edit_dialog_updates_with_authenticated_actor(
    qapp, authenticated_supplier_ui_actor, monkeypatch
):
    with get_db() as db:
        supplier = create_supplier(
            db,
            {"business_name": "UI edit before", "phone": "9876543210"},
            actor_context=authenticated_supplier_ui_actor,
        )
        supplier_id = supplier.id
    monkeypatch.setattr(
        "ui.supplier_form_dialog.QMessageBox.information", lambda *args, **kwargs: None
    )
    with get_db() as db:
        supplier = get_supplier_by_id(
            db, supplier_id, actor_context=authenticated_supplier_ui_actor
        )
    dialog = SupplierFormDialog(supplier=supplier)
    dialog.txt_name.setText("UI edit after")
    dialog._save_supplier()
    assert dialog.result() == QDialog.DialogCode.Accepted
    with get_db() as db:
        updated = get_supplier_by_id(
            db, supplier_id, actor_context=authenticated_supplier_ui_actor
        )
        assert updated.business_name == "UI edit after"


def test_supplier_edit_dialog_fails_closed_without_authentication(
    qapp, authenticated_supplier_ui_actor
):
    with get_db() as db:
        supplier = create_supplier(
            db,
            {"business_name": "UI unauthenticated before", "phone": "9876543210"},
            actor_context=authenticated_supplier_ui_actor,
        )
        supplier_id = supplier.id
    with get_db() as db:
        supplier = get_supplier_by_id(
            db, supplier_id, actor_context=authenticated_supplier_ui_actor
        )

    logout(actor_context=authenticated_supplier_ui_actor)
    dialog = SupplierFormDialog(supplier=supplier)
    dialog.txt_name.setText("Unauthorized UI change")
    dialog._save_supplier()

    assert "Sign in with supplier-management permission" in dialog.lbl_error.text()
    with get_db() as db:
        unchanged = db.get(Supplier, supplier_id)
        assert unchanged.business_name == "UI unauthenticated before"


def test_supplier_detail_edit_handles_expired_authentication(
    qapp, authenticated_supplier_ui_actor, monkeypatch
):
    with get_db() as db:
        supplier = create_supplier(
            db,
            {"business_name": "Detail expired actor", "phone": "9876543210"},
            actor_context=authenticated_supplier_ui_actor,
        )
        supplier_id = supplier.id
    dialog = SupplierDetailDialog(supplier_id=supplier_id)
    warnings = []
    monkeypatch.setattr(
        "ui.supplier_detail_dialog.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(args[1:]),
    )
    logout(actor_context=authenticated_supplier_ui_actor)

    dialog._edit_supplier()

    assert warnings
    assert warnings[0][0] == "Authentication Required"
    assert "supplier-view permission" in warnings[0][1]
    dialog.close()



def test_supplier_page_load_and_filter(qapp, authenticated_supplier_ui_actor):
    """Test supplier table listing, search, status filters, and metrics."""
    import uuid
    run_id = uuid.uuid4().hex[:6]
    name_alpha = f"AlphaScrap_{run_id}"
    name_beta = f"BetaMetals_{run_id}"
    phone_alpha = "9800" + "".join(str(ord(c) % 10) for c in run_id)
    phone_beta = "9700" + "".join(str(ord(c) % 10) for c in run_id)

    with get_db() as db:
        s1 = create_supplier(db, {
            "business_name": name_alpha,
            "contact_person": "Vikram Seth",
            "phone": phone_alpha,
            "city": "Bengaluru",
            "opening_balance": 25000.0,
            "is_active": True,
        }, actor_context=authenticated_supplier_ui_actor)
        s2 = create_supplier(db, {
            "business_name": name_beta,
            "contact_person": "Anil Kapoor",
            "phone": phone_beta,
            "city": "Chennai",
            "opening_balance": 10000.0,
            "is_active": False,
        }, actor_context=authenticated_supplier_ui_actor)

    page = SupplierPage()
    page.load_suppliers()

    # Verify metrics show at least these two
    assert int(page.card_total["val_label"].text()) >= 2
    assert int(page.card_active["val_label"].text()) >= 1
    assert int(page.card_inactive["val_label"].text()) >= 1

    # Search for unique Alpha
    page.txt_search.setText(name_alpha)
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == name_alpha

    # Search by unique phone
    page.txt_search.setText(phone_beta)
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == name_beta

    # Clear search
    page.txt_search.setText("")

    # Filter Active Only
    idx_active = page.combo_status.findData("ACTIVE")
    page.combo_status.setCurrentIndex(idx_active)
    for r in range(page.table.rowCount()):
        sup_id = page.table.item(r, 0).data(Qt.UserRole)
        with get_db() as db:
            s = get_supplier_by_id(db, sup_id, actor_context=authenticated_supplier_ui_actor)
            assert s.is_active is True

    # Filter Inactive Only
    idx_inactive = page.combo_status.findData("INACTIVE")
    page.combo_status.setCurrentIndex(idx_inactive)
    for r in range(page.table.rowCount()):
        sup_id = page.table.item(r, 0).data(Qt.UserRole)
        with get_db() as db:
            s = get_supplier_by_id(db, sup_id, actor_context=authenticated_supplier_ui_actor)
            assert s.is_active is False

    page.close()


def test_main_window_navigation_and_brand(
    qapp, tmp_path, authenticated_supplier_ui_actor
):
    """Test MainWindow integration, sidebar navigation to Suppliers, and brand visibility."""
    with get_db() as db:
        update_settings(
            db,
            {
                "backup_directory": str((tmp_path / "backups").resolve()),
                "document_storage_path": str((tmp_path / "documents").resolve()),
            },
            actor_context=authenticated_supplier_ui_actor,
        )
    window = MainWindow()
    assert window.supplier_page is not None

    # Check brand label styling
    brand = window.findChild(QLabel, "brand")
    assert brand is not None
    assert "RECYCLING" in brand.text()

    # Navigate to Suppliers page (index 2)
    window.nav.setCurrentRow(2)
    assert window.stack.currentIndex() == 2
    assert isinstance(window.stack.currentWidget(), SupplierPage)

    # Navigate to Dashboard (index 0)
    window.nav.setCurrentRow(0)
    assert window.stack.currentIndex() == 0

    window.nav.setCurrentRow(11)
    assert window.employee_page is not None
    assert window.stack.currentWidget() is window.employee_page
    assert window.employee_page.metrics["total"].text() == "0"
    assert window.employee_page.department_filter.count() >= 11

    window.nav.setCurrentRow(13)
    assert window.settings_page is not None

    window.nav.setCurrentRow(13)
    assert window.stack.currentWidget() is window.settings_page
    window.settings_page.load_data()
    assert window.settings_page.tabs.count() == 4
    assert window.settings_page.tabs.tabText(2) == "Documents"
    assert window.settings_page.tabs.tabText(3) == "Backup & Restore"

    window.close()


def test_supplier_detail_dialog_outstanding_balance(qapp, authenticated_supplier_ui_actor):
    """Test SupplierDetailDialog shows complete details, opening balance, and outstanding balance."""
    with get_db() as db:
        s = create_supplier(db, {
            "business_name": "Detail Test Supplier",
            "contact_person": "Pooja Hegde",
            "phone": "9876500000",
            "email": "pooja@detailtest.com",
            "opening_balance": 45000.75,
            "remarks": "High-volume circuit board recycler",
        }, actor_context=authenticated_supplier_ui_actor)
        sup_id = s.id

    dialog = SupplierDetailDialog(supplier_id=sup_id)
    assert "Detail Test Supplier" in dialog.lbl_name.text()
    assert "₹ 45,000.75" in dialog.lbl_opening_bal.text()
    assert "₹ 45,000.75" in dialog.lbl_outstanding_bal.text()
    assert dialog.val_contact.text() == "Pooja Hegde"
    assert dialog.val_phone.text() == "9876500000"
    assert dialog.val_email.text() == "pooja@detailtest.com"
    assert "circuit board" in dialog.val_remarks.text()

    # Check purchase history tab exists and has placeholder notice
    assert dialog.tabs.count() == 3
    assert "Purchase History" in dialog.tabs.tabText(1)
    assert "Payments & Statement" in dialog.tabs.tabText(2)

    dialog.close()
