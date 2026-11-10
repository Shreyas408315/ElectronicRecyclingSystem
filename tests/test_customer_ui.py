import uuid

import pytest
from PySide6.QtWidgets import QApplication

from database.connection import get_db
from services.security_service import authenticate, create_user, logout
from services.customer_service import create_customer
from ui.customer_detail_dialog import CustomerDetailDialog
from ui.customer_form_dialog import CustomerFormDialog
from ui.customer_page import CustomerPage
from ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_customer_page_search_filters_and_navigation(qapp, authenticated_actor):
    unique = uuid.uuid4().hex[:8]
    with get_db() as db:
        customer = create_customer(db, {
            "customer_name": f"UI Customer {unique}",
            "customer_type": "Distributor",
            "phone": "9876543210",
            "opening_balance": 250,
        }, actor_context=authenticated_actor)
        customer_id = customer.id
        customer_code = customer.customer_code

    page = CustomerPage()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == customer_code
    assert page.table.item(0, 8).text() == "Not available"
    assert page.metric_labels["total_outstanding_receivables"].text() == "Not available"
    page.table.selectRow(0)
    assert page.btn_details.isEnabled()
    page.combo_type.setCurrentText("Distributor")
    assert page.table.rowCount() == 1
    page.txt_search.setText("does-not-exist")
    assert page.table.rowCount() == 0

    detail = CustomerDetailDialog(page, customer_id)
    assert detail.tabs.count() == 4
    assert detail.tabs.tabText(1) == "Sales History"
    assert "order commitments only" in detail.tabs.widget(1).layout().itemAt(0).widget().text()

    window = MainWindow()
    window.nav.setCurrentRow(5)
    assert window.stack.currentIndex() == 5
    assert window.stack.currentWidget() is window.customer_page


def test_customer_form_billing_copy_and_financial_fields(qapp):
    dialog = CustomerFormDialog()
    dialog.chk_copy_address.setChecked(True)
    dialog.txt_billing.setPlainText("Factory Street, Unit 9")
    assert dialog.txt_shipping.toPlainText() == "Factory Street, Unit 9"
    assert dialog.txt_shipping.isReadOnly()
    dialog.combo_terms.setCurrentText("Custom")
    dialog.spin_days.setValue(21)
    dialog.chk_credit_limit.setChecked(True)
    dialog.spin_credit.setValue(25000)
    payload = dialog._payload()
    assert payload["billing_address"] == payload["shipping_address"]
    assert payload["payment_terms"] == "Custom"
    assert payload["custom_payment_days"] == 21
    assert payload["credit_limit"] == 25000


def test_customer_page_clears_protected_data_after_session_logout(
    qapp, authenticated_actor
):
    with get_db() as db:
        create_customer(
            db,
            {
                "customer_name": "Visible Before Logout",
                "customer_type": "Distributor",
            },
            actor_context=authenticated_actor,
        )
    page = CustomerPage()
    assert page.table.rowCount() == 1

    logout(actor_context=authenticated_actor)
    page.load_customers()

    assert page.table.rowCount() == 0
    assert page.metric_labels["total"].text() == "—"
    assert "Sign in is required" in page.lbl_access_error.text()
    assert not page.btn_export.isEnabled()


def test_main_window_discards_page_cache_on_session_lock(qapp, authenticated_actor):
    window = MainWindow(current_user=authenticated_actor)
    assert window.stack.count() > 0

    window.clear_protected_page_state()

    assert window.stack.count() == 0
    assert window.current_user is None
    assert window.customer_page is None
    assert window.settings_page is None
    window.close()
    qapp.processEvents()


def test_relogin_creates_pages_for_new_actor_without_old_customer_data(
    qapp, authenticated_actor,
):
    with get_db() as session:
        create_customer(
            session,
            {
                "customer_name": "First Account Customer",
                "customer_type": "Distributor",
            },
            actor_context=authenticated_actor,
        )
        inventory_user = create_user(
            session,
            actor_context=authenticated_actor,
            username="inventory-window-user",
            password="Inventory-window-user-2026!",
            full_name="Inventory Window User",
            role_names=["Inventory Operator"],
        )

    first_window = MainWindow(current_user=authenticated_actor)
    first_window.nav.setCurrentRow(5)
    assert first_window.customer_page.table.rowCount() == 1
    first_window.clear_protected_page_state()
    first_window.close()

    with get_db() as session:
        second_actor = authenticate(
            session, inventory_user.username, "Inventory-window-user-2026!"
        )
    second_window = MainWindow(current_user=second_actor)
    second_window.nav.setCurrentRow(5)
    assert second_window.current_user.user_id == second_actor.user_id
    assert second_window.customer_page.table.rowCount() == 0
    assert second_window.customer_page.lbl_access_error.text()

    logout(actor_context=second_actor)
    second_window.close()
    qapp.processEvents()
