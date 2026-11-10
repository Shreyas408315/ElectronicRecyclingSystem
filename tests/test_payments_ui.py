import pytest
from PySide6.QtWidgets import QApplication

from database.connection import get_db
from services.customer_service import create_customer
from services.security_service import authenticate, create_user, logout
from services.supplier_service import create_supplier
from ui.main_window import MainWindow
from ui.payments_page import PaymentEntryDialog, PaymentsReceiptPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_payments_navigation_and_entry_party_lists(qapp, authenticated_actor):
    with get_db() as db:
        customer = create_customer(db, {
            "customer_name": "Payments UI Customer",
            "customer_type": "Retailer",
            "opening_balance": "0",
        }, actor_context=authenticated_actor)
        supplier = create_supplier(db, {
            "business_name": "Payments UI Supplier",
            "is_active": True,
        }, actor_context=authenticated_actor)
        customer_id, supplier_id = customer.id, supplier.id

    window = MainWindow()
    window.nav.setCurrentRow(8)
    page = window.payments_receipts_page

    assert window.stack.currentWidget() is page
    assert page.tabs.count() == 2
    assert page.customer_tab.party_filter.findData(customer_id) >= 0
    assert page.supplier_tab.party_filter.findData(supplier_id) >= 0
    assert page.customer_tab.metric_labels["overdue_invoices"].text() == "0"

    customer_dialog = PaymentEntryDialog(customer_side=True)
    supplier_dialog = PaymentEntryDialog(customer_side=False)
    assert customer_dialog.party.findData(customer_id) >= 0
    assert supplier_dialog.party.findData(supplier_id) >= 0


def test_payments_page_fails_closed_after_logout(qapp, authenticated_actor):
    logout(actor_context=authenticated_actor)

    page = PaymentsReceiptPage()

    for tab in (page.customer_tab, page.supplier_tab):
        assert tab.table.rowCount() == 0
        assert all(not label.text() for label in tab.metric_labels.values())
        assert not tab.create_button.isEnabled()
        assert "Sign in is required" in tab.access_warning.text()


def test_payment_actions_are_disabled_for_read_only_sales_role(
    qapp, authenticated_actor
):
    with get_db() as db:
        user = create_user(
            db,
            actor_context=authenticated_actor,
            username="payment-ui-sales",
            password="Sales-ui-password-2026!",
            full_name="Payment UI Sales",
            role_names=["Sales Operator"],
        )
        username = user.username
    with get_db() as db:
        sales_actor = authenticate(db, username, "Sales-ui-password-2026!")
    try:
        page = PaymentsReceiptPage()
        assert page.customer_tab.access_error is None
        assert not page.customer_tab.create_button.isEnabled()
        assert not page.customer_tab.edit_button.isEnabled()
        assert page.supplier_tab.access_error
        assert not page.supplier_tab.create_button.isEnabled()
    finally:
        logout(actor_context=sales_actor)
