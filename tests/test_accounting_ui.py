import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QHeaderView, QMessageBox
from sqlalchemy import func, select

from database.connection import get_db
from database.models import (
    Account, AccountingException, AccountingPeriod, AccountMapping,
    JournalEntry, UserAuditEvent,
)
from services.account_service import seed_default_accounts
from services.security_service import authenticate, create_user, logout
from ui.accounting_page import AccountingPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def delete_accounting_pages(qapp):
    yield
    for widget in list(qapp.topLevelWidgets()):
        if isinstance(widget, AccountingPage):
            widget.close()
            widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def _set_current_actor(actor):
    from services.security_service import _current_session

    _current_session.set(actor)


def _create_role_actor(admin_context, username, role_name):
    password = f"{username}-Secure-2026!"
    with get_db() as session:
        user = create_user(
            session,
            actor_context=admin_context,
            username=username,
            password=password,
            full_name=username.replace("-", " ").title(),
            role_names=[role_name],
        )
        return authenticate(session, user.username, password)


def test_refresh_button_invokes_reload_and_preserves_selected_tab(
    qapp, authenticated_actor, monkeypatch
):
    _set_current_actor(authenticated_actor)
    page = AccountingPage()
    page.tabs.setCurrentIndex(2)
    calls = []

    def reload_data(*_args):
        calls.append(True)
        return True

    monkeypatch.setattr(page, "load_data", reload_data)
    page.refresh_button.click()

    assert calls == [True]
    assert page.tabs.currentIndex() == 2
    assert page.refresh_status.text() == "Accounting data refreshed."
    page.close()


def test_refresh_failure_is_logged_and_shown(
    qapp, authenticated_actor, monkeypatch, caplog
):
    _set_current_actor(authenticated_actor)
    page = AccountingPage()
    messages = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args: messages.append(args[-1])
    )

    def fail_reload(*_args):
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(page, "load_data", fail_reload)
    page.refresh_button.click()

    assert page.refresh_status.text() == "Refresh failed."
    assert messages == [
        "Accounting data could not be refreshed: database temporarily unavailable"
    ]
    assert "Could not refresh accounting data" in caplog.text
    page.close()


def test_refresh_reloads_review_queue_without_mutating_accounting_data(
    qapp, authenticated_actor
):
    _set_current_actor(authenticated_actor)
    page = AccountingPage()
    page.tabs.setCurrentIndex(2)
    reason = (
        "Required accounting mapping 'control:expense_payables' is not configured. "
        "This full reason should remain accessible without resizing the column."
    )
    with get_db() as session:
        session.add(AccountingException(
            source_module="Expense Payment",
            source_transaction_id=921,
            source_event_type="POSTED",
            reason=reason,
            status="Open",
        ))

    def snapshot():
        with get_db() as session:
            return (
                session.scalar(select(func.count(Account.id))),
                session.scalar(select(func.count(AccountMapping.id))),
                session.scalar(select(func.count(JournalEntry.id))),
                session.scalar(select(func.count(AccountingException.id))),
                session.scalar(select(func.count(AccountingPeriod.id))),
                session.scalar(select(func.count(UserAuditEvent.id))),
            )

    before = snapshot()
    page.refresh_button.click()
    after = snapshot()

    assert page.exception_table.rowCount() == 1
    assert page.exception_table.item(0, 4).text() == reason
    assert page.tabs.currentIndex() == 2
    assert before == after
    assert page.refresh_status.text() == "Accounting data refreshed."
    page.close()


def test_review_queue_reason_wraps_has_minimum_width_and_tooltip(
    qapp, authenticated_actor
):
    _set_current_actor(authenticated_actor)
    reason = "Required accounting mapping " + ("missing configuration " * 12)
    with get_db() as session:
        session.add(AccountingException(
            source_module="Expense",
            source_transaction_id=922,
            source_event_type="POSTED",
            reason=reason,
            status="Open",
        ))

    page = AccountingPage()
    item = page.exception_table.item(0, 4)

    assert page.exception_table.wordWrap()
    assert page.exception_table.columnWidth(4) >= 400
    assert item.text() == reason
    assert item.toolTip() == reason
    assert (
        page.exception_table.verticalHeader().sectionResizeMode(0)
        == QHeaderView.ResizeMode.ResizeToContents
    )
    assert (
        page.exception_table.rowHeight(0)
        > page.exception_table.verticalHeader().defaultSectionSize()
    )
    page.close()


def test_empty_chart_and_mapping_tables_explain_authorized_setup(
    qapp, authenticated_actor
):
    _set_current_actor(authenticated_actor)
    page = AccountingPage()

    assert page.account_table.rowCount() == 0
    assert page.mapping_table.rowCount() == 0
    assert not page.account_empty_state.isHidden()
    assert "accounting.configure" in page.account_empty_state.text()
    assert not page.mapping_empty_state.isHidden()
    assert "accounting.configure" in page.mapping_empty_state.text()
    assert page.account_seed_button.isEnabled()
    assert page.account_add_button.isEnabled()
    assert not page.mapping_save_button.isEnabled()
    page.close()


def test_configuration_actions_follow_existing_role_permissions(
    qapp, authenticated_actor
):
    finance_actor = _create_role_actor(
        authenticated_actor, "accounting-ui-finance", "Accounts & Finance"
    )
    _set_current_actor(finance_actor)
    finance_page = AccountingPage()
    assert finance_page.account_seed_button.isEnabled()
    assert finance_page.account_add_button.isEnabled()
    assert finance_page.mapping_key.isEnabled()
    assert finance_page.mapping_account.isEnabled()
    assert not finance_page.mapping_save_button.isEnabled()

    with get_db() as session:
        seed_default_accounts(session, actor_context=finance_actor)
    finance_page.load_data()
    finance_page.mapping_account.setCurrentIndex(1)
    assert finance_page.mapping_save_button.isEnabled()
    finance_page.close()
    logout(actor_context=finance_actor)

    auditor_actor = _create_role_actor(
        authenticated_actor, "accounting-ui-auditor", "Read-only Auditor"
    )
    _set_current_actor(auditor_actor)
    auditor_page = AccountingPage()
    assert all(not button.isEnabled() for button in auditor_page.account_manage_buttons)
    assert not auditor_page.mapping_key.isEnabled()
    assert not auditor_page.mapping_account.isEnabled()
    assert not auditor_page.mapping_save_button.isEnabled()
    auditor_page.close()
    logout(actor_context=auditor_actor)
