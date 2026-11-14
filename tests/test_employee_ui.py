from datetime import date

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from sqlalchemy import select

from database.connection import get_db
from database.models import Employee, EmployeeHistory
from services.employee_service import (
    create_department,
    create_designation,
    create_employee,
    seed_default_departments,
)
from services.security_service import authenticate, create_user, logout
from ui import employee_page as employee_page_module
from ui.employee_page import EmployeeFormDialog, EmployeePage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def employee_and_actor(authenticated_actor):
    with get_db() as session:
        seed_default_departments(session, actor_context=authenticated_actor)
        department = create_department(
            session, "UI Test Department", actor_context=authenticated_actor
        )
        designation = create_designation(
            session, "UI Test Designation", actor_context=authenticated_actor
        )
        employee = create_employee(
            session,
            {
                "full_name": "UI Test Employee",
                "phone": "9876543210",
                "alternate_phone": "9876543211",
                "email": "ui.employee@example.test",
                "residential_address": "12 Test Street",
                "city": "Pune",
                "state": "Maharashtra",
                "pin_code": "411001",
                "department_id": department.id,
                "designation_id": designation.id,
                "employment_type": "Full-time",
                "employment_status": "Active",
                "date_of_joining": date(2024, 2, 3),
                "date_of_leaving": None,
                "emergency_contact_name": "Emergency Contact",
                "emergency_contact_phone": "9876543212",
                "notes": "Employee UI test notes",
                "responsible_user": "Acceptance tester",
            },
            actor_context=authenticated_actor,
        )
        employee_id = employee.id

        manager = create_user(
            session,
            actor_context=authenticated_actor,
            username="employee-ui-manager",
            password="Employee-ui-manager-2026!",
            full_name="Employee UI Manager",
            role_names=["Factory Manager"],
        )
        hr_manager = create_user(
            session,
            actor_context=authenticated_actor,
            username="employee-ui-hr-manager",
            password="Employee-ui-hr-manager-2026!",
            full_name="Employee UI HR Manager",
            role_names=["HR/Employee Manager"],
        )
        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="employee-ui-procurement",
            password="Employee-ui-procurement-2026!",
            full_name="Employee UI Procurement",
            role_names=["Procurement"],
        )

    def login(username, password):
        with get_db() as session:
            return authenticate(session, username, password)

    actors = {
        "factory": login("employee-ui-manager", "Employee-ui-manager-2026!"),
        "hr": login("employee-ui-hr-manager", "Employee-ui-hr-manager-2026!"),
        "procurement": login(
            "employee-ui-procurement", "Employee-ui-procurement-2026!"
        ),
    }
    try:
        yield employee_id, actors
    finally:
        for actor in actors.values():
            logout(actor_context=actor)


def _edit_page_for_actor(actor, qapp):
    from services.security_service import _current_session

    _current_session.set(actor)
    return EmployeePage()


def _select_employee(page, employee_id):
    for row in range(page.table.rowCount()):
        if page.table.item(row, 0).data(employee_page_module.Qt.UserRole) == employee_id:
            page.table.selectRow(row)
            return row
    raise AssertionError(f"Employee id {employee_id} was not listed.")


def test_factory_manager_edit_button_opens_populated_edit_dialog(
    qapp, employee_and_actor, monkeypatch
):
    employee_id, actors = employee_and_actor
    page = _edit_page_for_actor(actors["factory"], qapp)
    row = _select_employee(page, employee_id)
    assert page.edit_button.isEnabled()

    opened = {}

    def inspect_dialog(dialog):
        opened["dialog"] = dialog
        assert dialog.employee_id == employee_id
        assert dialog.full_name.text() == "UI Test Employee"
        assert dialog.employee_code.text() == page.table.item(row, 0).text()
        assert dialog.department.currentData() is not None
        assert dialog.designation.currentData() is not None
        assert dialog.employment_type.currentText() == "Full-time"
        assert dialog.status.currentText() == "Active"
        assert dialog.joining.date().toPython() == date(2024, 2, 3)
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(EmployeeFormDialog, "exec", inspect_dialog)
    page.edit_button.click()

    assert "dialog" in opened
    opened["dialog"].deleteLater()
    page.close()


def test_hr_edit_dialog_populates_private_fields_and_four_tabs(
    qapp, employee_and_actor, monkeypatch
):
    employee_id, actors = employee_and_actor
    page = _edit_page_for_actor(actors["hr"], qapp)
    _select_employee(page, employee_id)
    opened = {}

    def inspect_dialog(dialog):
        opened["dialog"] = dialog
        assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
            "Personal", "Employment", "Additional", "Photograph"
        ]
        assert dialog.phone.text() == "9876543210"
        assert dialog.alternate_phone.text() == "9876543211"
        assert dialog.email.text() == "ui.employee@example.test"
        assert dialog.address.text() == "12 Test Street"
        assert dialog.emergency_name.text() == "Emergency Contact"
        assert dialog.emergency_phone.text() == "9876543212"
        assert dialog.notes.text() == "Employee UI test notes"
        assert dialog.responsible_user.text() == "Acceptance tester"
        assert dialog.photo_label.text() == "No profile photograph stored."
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(EmployeeFormDialog, "exec", inspect_dialog)
    page.edit_button.click()
    assert "dialog" in opened
    opened["dialog"].deleteLater()
    page.close()


def test_factory_manager_save_persists_allowed_change_and_refreshes_page(
    qapp, employee_and_actor, monkeypatch
):
    employee_id, actors = employee_and_actor
    page = _edit_page_for_actor(actors["factory"], qapp)
    _select_employee(page, employee_id)

    def save_change(dialog):
        dialog.full_name.setText("Updated UI Test Employee")
        dialog._save()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(EmployeeFormDialog, "exec", save_change)
    page.edit_button.click()

    assert page.table.item(page.table.currentRow(), 1).text() == (
        "Updated UI Test Employee"
    )
    with get_db() as session:
        employee = session.get(Employee, employee_id)
        assert employee.full_name == "Updated UI Test Employee"
        assert employee.phone == "9876543210"
        assert session.scalar(
            select(EmployeeHistory.id).where(
                EmployeeHistory.employee_id == employee_id,
                EmployeeHistory.event_type == "UPDATED",
            )
        ) is not None
    page.close()


def test_cancel_edit_leaves_employee_unchanged(
    qapp, employee_and_actor, monkeypatch
):
    employee_id, actors = employee_and_actor
    page = _edit_page_for_actor(actors["factory"], qapp)
    _select_employee(page, employee_id)

    def cancel_edit(dialog):
        dialog.full_name.setText("Cancelled Name")
        dialog.reject()
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(EmployeeFormDialog, "exec", cancel_edit)
    page.edit_button.click()

    with get_db() as session:
        assert session.get(Employee, employee_id).full_name == "UI Test Employee"
    assert page.table.item(page.table.currentRow(), 1).text() == "UI Test Employee"
    page.close()


def test_role_without_manage_cannot_edit_and_no_selection_disables_action(
    qapp, employee_and_actor
):
    employee_id, actors = employee_and_actor
    page = _edit_page_for_actor(actors["factory"], qapp)
    assert not page.edit_button.isEnabled()
    page.edit_selected()
    assert page.edit_button.isEnabled() is False
    page.close()

    page = _edit_page_for_actor(actors["procurement"], qapp)
    assert not page.can_manage
    assert page.edit_button.isHidden()
    assert page.table.rowCount() == 0
    page.edit_selected()
    page.close()


def test_edit_dialog_opening_exception_is_logged_and_shown(
    qapp, employee_and_actor, monkeypatch, caplog
):
    employee_id, actors = employee_and_actor
    page = _edit_page_for_actor(actors["factory"], qapp)
    _select_employee(page, employee_id)
    messages = []
    monkeypatch.setattr(
        QMessageBox, "critical", lambda *args: messages.append(args[-1])
    )

    def fail_constructor(*args, **kwargs):
        raise RuntimeError("dialog construction failed")

    monkeypatch.setattr(employee_page_module, "EmployeeFormDialog", fail_constructor)
    page.edit_button.click()

    assert messages == ["dialog construction failed"]
    assert "dialog construction failed" in caplog.text
    page.close()
