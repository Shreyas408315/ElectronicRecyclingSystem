from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import (
    Employee,
    EmployeeHistory,
    Permission,
    Role,
    RolePermission,
    UserRole,
)
from services.security_service import (
    AuthorizationError,
    authenticate,
    create_user,
    logout,
)
from services.employee_report_service import (
    generate_employee_excel_report,
    generate_employee_pdf_report,
    get_employee_report_rows,
)
from services.employee_service import (
    EmployeeDuplicateWarning,
    EmployeeValidationError,
    create_department,
    create_designation,
    create_employee,
    deactivate_employee,
    find_possible_duplicate_employees,
    get_employee_by_id,
    get_employee_summary,
    list_employees,
    reactivate_employee,
    seed_default_departments,
    separate_employee,
    set_department_active,
    set_designation_active,
    update_department,
    update_designation,
    update_employee,
)


@pytest.fixture
def employee_session(tmp_path, authenticated_actor_factory):
    engine = create_engine(f"sqlite:///{(tmp_path / 'employees.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        for code in (
            "employees.view",
            "employees.manage",
            "employees.private.view",
            "employees.export_private",
        ):
            session.add(Permission(
                code=code,
                module="Employees",
                action=code.split(".")[-1],
                description=code,
            ))
        session.flush()
        actor_context = authenticated_actor_factory(session)
        role_ids = session.scalars(
            select(UserRole.role_id).where(UserRole.user_id == actor_context.user_id)
        ).all()
        permission_ids = session.scalars(select(Permission.id)).all()
        for role_id in role_ids:
            for permission_id in permission_ids:
                if session.scalar(select(RolePermission.id).where(
                    RolePermission.role_id == role_id,
                    RolePermission.permission_id == permission_id,
                )) is None:
                    session.add(RolePermission(
                        role_id=role_id, permission_id=permission_id
                    ))
        seed_default_departments(session, actor_context=actor_context)
        department = create_department(
            session, "Operations", actor_context=actor_context
        )
        designation = create_designation(
            session, "Recovery Technician", actor_context=actor_context
        )
        session.commit()
        yield session, department, designation, actor_context
    engine.dispose()


def employee_values(department, designation, **overrides):
    values = {
        "full_name": "Asha Technician",
        "phone": "+91 9876543210",
        "alternate_phone": "",
        "email": "asha@example.test",
        "residential_address": "Optional home address",
        "city": "Pune",
        "state": "Maharashtra",
        "pin_code": "411001",
        "department_id": department.id,
        "designation_id": designation.id,
        "employment_type": "Full-time",
        "employment_status": "Active",
        "date_of_joining": date(2024, 1, 15),
        "date_of_leaving": None,
        "emergency_contact_name": "",
        "emergency_contact_phone": "",
        "notes": "",
        "responsible_user": "Factory admin",
    }
    return {**values, **overrides}


def test_department_and_designation_defaults_and_soft_deactivation(employee_session):
    session, department, designation, actor_context = employee_session
    initial_count = len(seed_default_departments(session, actor_context=actor_context))
    assert len(seed_default_departments(session, actor_context=actor_context)) == initial_count
    with pytest.raises(EmployeeValidationError, match="already exists"):
        create_department(session, " operations ", actor_context=actor_context)
    with pytest.raises(EmployeeValidationError, match="already exists"):
        create_designation(session, "recovery TECHNICIAN", actor_context=actor_context)

    renamed_department = update_department(
        session, department.id, "Factory Operations", actor_context=actor_context
    )
    renamed_designation = update_designation(
        session, designation.id, "Recovery Specialist", actor_context=actor_context
    )
    assert renamed_department.name == "Factory Operations"
    assert renamed_designation.name == "Recovery Specialist"
    set_department_active(session, department.id, False, actor_context=actor_context)
    set_designation_active(session, designation.id, False, actor_context=actor_context)
    assert not session.get(type(department), department.id).is_active
    assert not session.get(type(designation), designation.id).is_active


def test_employee_sequential_codes_duplicate_warning_and_persistence(employee_session):
    session, department, designation, actor_context = employee_session
    first = create_employee(
        session, employee_values(department, designation), actor_context=actor_context
    )
    assert first.employee_code == "EMP-000001"

    with pytest.raises(EmployeeDuplicateWarning) as warning:
        create_employee(
            session, employee_values(department, designation), actor_context=actor_context
        )
    assert warning.value.matches[0].id == first.id
    assert len(list(session.scalars(select(Employee)))) == 1

    second = create_employee(
        session,
        employee_values(
            department,
            designation,
            full_name="Asha Technician",
            phone="",
            email="",
        ),
        actor_context=actor_context,
        allow_possible_duplicate=True,
    )
    assert second.employee_code == "EMP-000002"
    assert get_employee_by_id(
        session, second.id, actor_context=actor_context
    ).full_name == "Asha Technician"
    assert find_possible_duplicate_employees(
        session, full_name="Asha Technician", actor_context=actor_context
    )


def test_employee_search_filters_sort_pagination_and_summary(employee_session):
    session, department, designation, actor_context = employee_session
    first = create_employee(
        session, employee_values(department, designation), actor_context=actor_context
    )
    create_employee(
        session,
        employee_values(
            department,
            designation,
            full_name="Mira Inventory",
            phone="",
            email="mira@example.test",
            employment_type="Contract",
            date_of_joining=date(2025, 6, 1),
        ),
        actor_context=actor_context,
    )
    assert [row.id for row in list_employees(
        session, search="EMP-000001", actor_context=actor_context
    )] == [first.id]
    assert len(list_employees(
        session, search="asha@example.test", actor_context=actor_context,
        include_private=True,
    )) == 1
    assert len(list_employees(
        session, employment_type="Contract", actor_context=actor_context
    )) == 1
    assert len(list_employees(
        session,
        employment_status="Active",
        joining_start=date(2025, 1, 1),
        joining_end=date(2025, 12, 31),
        actor_context=actor_context,
    )) == 1
    page = list_employees(
        session, sort_by="joining_desc", offset=0, limit=1,
        actor_context=actor_context,
    )
    assert page[0].full_name == "Mira Inventory"
    summary = get_employee_summary(session, actor_context=actor_context)
    assert summary["total"] == 2
    assert summary["active"] == 2
    assert summary["by_department"]["Operations"] == 2
    with pytest.raises(EmployeeValidationError, match="Pagination"):
        list_employees(session, offset=-1, actor_context=actor_context)


def test_employee_update_validation_and_status_history(employee_session):
    session, department, designation, actor_context = employee_session
    employee = create_employee(
        session, employee_values(department, designation), actor_context=actor_context
    )
    with pytest.raises(EmployeeValidationError, match="Phone"):
        update_employee(
            session, employee.id, {"phone": "123"}, actor_context=actor_context
        )
    with pytest.raises(EmployeeValidationError, match="PIN"):
        update_employee(
            session, employee.id, {"pin_code": "123"}, actor_context=actor_context
        )
    with pytest.raises(EmployeeValidationError, match="leaving"):
        update_employee(
            session,
            employee.id,
            {
                "employment_status": "Separated",
                "date_of_leaving": date(2023, 12, 31),
            },
            actor_context=actor_context,
        )

    deactivated = deactivate_employee(
        session, employee.id, actor_context=actor_context,
        reason="Temporary inactive status",
    )
    assert deactivated.employment_status == "Inactive"
    reactivated = reactivate_employee(
        session, employee.id, actor_context=actor_context, reason="Returned to work"
    )
    assert reactivated.employment_status == "Active"
    separated = separate_employee(
        session, employee.id, date(2025, 1, 10),
        actor_context=actor_context, reason="Employment ended",
    )
    assert separated.employment_status == "Separated"
    with pytest.raises(EmployeeValidationError, match="Separated"):
        reactivate_employee(session, employee.id, actor_context=actor_context)
    history = list(session.scalars(
        select(EmployeeHistory).where(EmployeeHistory.employee_id == employee.id)
    ))
    assert {row.event_type for row in history} >= {
        "CREATED", "DEACTIVATED", "REACTIVATED", "SEPARATED",
    }


def test_department_deactivation_does_not_remove_employee_records(employee_session):
    session, department, designation, actor_context = employee_session
    employee = create_employee(
        session, employee_values(department, designation), actor_context=actor_context
    )
    set_department_active(session, department.id, False, actor_context=actor_context)
    assert session.get(Employee, employee.id) is not None
    with pytest.raises(EmployeeValidationError, match="active departments"):
        create_employee(
            session,
            employee_values(department, designation, full_name="New Employee"),
            actor_context=actor_context,
        )


def test_employee_rbac_masks_private_fields_and_restricts_sensitive_operations(
    employee_session,
):
    session, department, designation, admin_context = employee_session
    employee = create_employee(
        session, employee_values(department, designation), actor_context=admin_context
    )
    hr_role = session.scalar(select(Role).where(Role.name == "HR/Employee Manager"))
    view_permission = session.scalar(
        select(Permission).where(Permission.code == "employees.view")
    )
    if session.scalar(select(RolePermission.id).where(
        RolePermission.role_id == hr_role.id,
        RolePermission.permission_id == view_permission.id,
    )) is None:
        session.add(RolePermission(
            role_id=hr_role.id, permission_id=view_permission.id
        ))
    create_user(
        session,
        actor_context=admin_context,
        username="employee-manager",
        password="Employee-manager-password-2026!",
        full_name="Employee Manager",
        role_names=["Factory Manager"],
    )
    manager_context = authenticate(
        session, "employee-manager", "Employee-manager-password-2026!"
    )
    try:
        record = get_employee_by_id(
            session, employee.id, actor_context=manager_context
        )
        assert record.full_name == "Asha Technician"
        assert record.phone is None
        assert record.email is None
        assert record.residential_address is None
        assert record.emergency_contact_name is None
        assert record.notes is None
        assert all(
            event.details is None and event.responsible_user is None
            for event in record.history
        )
        with pytest.raises(AuthorizationError, match="employees.private.view"):
            get_employee_by_id(
                session,
                employee.id,
                actor_context=manager_context,
                include_private=True,
            )
        with pytest.raises(AuthorizationError, match="employees.private.view"):
            find_possible_duplicate_employees(
                session,
                actor_context=manager_context,
                full_name="Asha Technician",
                phone="9876543210",
            )
        with pytest.raises(AuthorizationError, match="employees.private.view"):
            update_employee(
                session,
                employee.id,
                {"phone": "9876543210"},
                actor_context=manager_context,
            )
        with pytest.raises(AuthorizationError, match="employees.private.view"):
            create_employee(
                session,
                employee_values(department, designation, full_name="New Employee"),
                actor_context=manager_context,
            )
    finally:
        logout(actor_context=manager_context)


def test_private_employee_report_requires_export_permission(employee_session, tmp_path):
    session, department, designation, admin_context = employee_session
    create_employee(
        session, employee_values(department, designation), actor_context=admin_context
    )
    role = Role(name="Employee Private Viewer", is_system=False)
    session.add(role)
    session.flush()
    for code in ("employees.view", "employees.private.view"):
        permission = session.scalar(
            select(Permission).where(Permission.code == code)
        )
        session.add(RolePermission(
            role_id=role.id, permission_id=permission.id
        ))
    create_user(
        session,
        actor_context=admin_context,
        username="private-viewer",
        password="Private-viewer-password-2026!",
        full_name="Private Viewer",
        role_names=["Employee Private Viewer"],
    )
    viewer_context = authenticate(
        session, "private-viewer", "Private-viewer-password-2026!"
    )
    try:
        rows = get_employee_report_rows(
            session, "directory", actor_context=viewer_context
        )
        assert "phone" not in rows[0]
        with pytest.raises(AuthorizationError, match="employees.export_private"):
            get_employee_report_rows(
                session,
                "directory",
                actor_context=viewer_context,
                include_private=True,
            )
        private_pdf = tmp_path / "unauthorized-private.pdf"
        private_excel = tmp_path / "unauthorized-private.xlsx"
        with pytest.raises(AuthorizationError, match="employees.export_private"):
            generate_employee_pdf_report(
                session,
                str(private_pdf),
                actor_context=viewer_context,
                include_private=True,
            )
        with pytest.raises(AuthorizationError, match="employees.export_private"):
            generate_employee_excel_report(
                session,
                str(private_excel),
                actor_context=viewer_context,
                include_private=True,
            )
        assert not private_pdf.exists()
        assert not private_excel.exists()
    finally:
        logout(actor_context=viewer_context)
