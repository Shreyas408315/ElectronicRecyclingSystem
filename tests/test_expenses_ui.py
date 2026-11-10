from PySide6.QtCore import QEvent
import pytest
from PySide6.QtWidgets import QApplication

from database.connection import get_db
from services.security_service import logout
from services.expense_service import create_draft_expense
from ui.expense_page import ExpenseFormDialog
from ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def close_expense_test_widgets(qapp):
    yield
    for widget in qapp.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_expense_navigation_dashboard_and_form(qapp, authenticated_actor):
    window = MainWindow()
    window.nav.setCurrentRow(9)
    page = window.expense_page
    assert window.stack.currentWidget() is page
    assert page.category_filter.count() > 1
    assert page.table.columnCount() == 10

    form = ExpenseFormDialog()
    assert form.category.count() > 0
    assert form.items.columnCount() == 7
    form._add_line({
        "description": "Maintenance",
        "quantity": "1",
        "unit": "service",
        "unit_price": "10",
        "discount": "0",
        "tax_rate": "0",
        "category_name": "",
    })
    assert form.items.rowCount() == 1

    window.nav.setCurrentRow(0)
    assert window.dashboard_page.values["total_expenses"].text().startswith("₹")


def test_expense_dashboard_uses_posted_totals_only(qapp, authenticated_actor):
    window = MainWindow()
    with get_db() as db:
        category_id = window.expense_page.category_filter.itemData(1)
        draft = create_draft_expense(db, {
            "title": "UI draft expense",
            "category_id": category_id,
            "payee_name": "UI Test Vendor",
            "subtotal": "500",
        }, [], actor_context=authenticated_actor)
        assert draft.status == "Draft"
    window.expense_page.load_expenses()
    assert window.expense_page.count_label.text() == "Showing 1 expenses"
    assert window.expense_page.metrics["total_expenses"].text() == "₹ 0.00"


def test_expense_payment_metrics_clear_after_logout(qapp, authenticated_actor):
    window = MainWindow()
    logout(actor_context=authenticated_actor)

    window.expense_page.load_expenses()
    window.dashboard_page.load_data()

    assert window.expense_page.table.rowCount() == 0
    assert all(not label.text() for label in window.expense_page.metrics.values())
    assert "Sign in is required" in window.expense_page.access_warning.text()
    assert not window.dashboard_page.values["total_payments"].text()
    assert "Sign in is required" in window.dashboard_page.expense_access_warning.text()
