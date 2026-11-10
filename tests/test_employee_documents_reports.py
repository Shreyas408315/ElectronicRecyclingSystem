from datetime import date
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import Permission, RolePermission, UserRole
from services.document_service import (
    DocumentValidationError,
    get_document_content,
    list_documents,
    update_document_metadata,
    upload_document,
)
from services.employee_report_service import (
    generate_employee_excel_report,
    generate_employee_pdf_report,
    get_employee_report_rows,
)
from services.employee_service import (
    create_department,
    create_designation,
    create_employee,
    separate_employee,
    seed_default_departments,
)
from services.settings_service import update_settings
from services.security_service import (
    AuthorizationError, authenticate, create_user, logout,
)


@pytest.fixture
def employee_session(tmp_path, authenticated_actor_factory):
    engine = create_engine(f"sqlite:///{(tmp_path / 'employee-reports.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        actor_context = authenticated_actor_factory(session)
        for code in (
            "employees.view",
            "employees.manage",
            "employees.private.view",
            "employees.export_private",
        ):
            permission = session.scalar(
                select(Permission).where(Permission.code == code)
            )
            if permission is None:
                permission = Permission(
                    code=code,
                    module="Employees",
                    action=code.split(".")[-1],
                    description=code,
                )
                session.add(permission)
        session.flush()
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
            session, "Quality", actor_context=actor_context
        )
        designation = create_designation(
            session, "Inspector", actor_context=actor_context
        )
        update_settings(
            session,
            {"document_storage_path": str((tmp_path / "documents").resolve())},
            actor_context=actor_context,
        )
        employee = create_employee(session, {
            "full_name": "Report Test Employee",
            "phone": "9876543210",
            "alternate_phone": "9876543211",
            "email": "report@example.test",
            "residential_address": "Private residential address",
            "city": "Pune",
            "state": "Maharashtra",
            "pin_code": "411001",
            "department_id": department.id,
            "designation_id": designation.id,
            "employment_type": "Full-time",
            "employment_status": "Active",
            "date_of_joining": date(2025, 2, 1),
            "date_of_leaving": None,
            "emergency_contact_name": "Private contact",
            "emergency_contact_phone": "9876543212",
        }, actor_context=actor_context)
        session.commit()
        yield session, employee, actor_context
    engine.dispose()


def test_employee_document_links_support_multiple_files_and_metadata(employee_session, tmp_path):
    session, employee, actor_context = employee_session
    first = upload_document(
        session,
        filename="appointment.pdf",
        content=b"%PDF-1.7\nappointment record",
        category="Appointment Letter",
        related_entity_type="Employee",
        related_entity_id=employee.id,
        description="Initial offer",
        actor_context=actor_context,
    )
    second = upload_document(
        session,
        filename="training.pdf",
        content=b"%PDF-1.7\ntraining record",
        category="Training Certificate",
        related_entity_type="Employee",
        related_entity_id=employee.id,
        actor_context=actor_context,
    )
    documents = list_documents(
        session,
        include_archived=True,
        related_entity_type="Employee",
        related_entity_id=employee.id,
        actor_context=actor_context,
    )
    assert {document.id for document in documents} == {first.id, second.id}
    loaded, content = get_document_content(
        session, first.id, actor_context=actor_context
    )
    assert loaded.related_entity_type == "Employee"
    assert content.startswith(b"%PDF-")
    update_document_metadata(
        session,
        second.id,
        description="Forklift safety training",
        actor_context=actor_context,
    )
    assert second.description == "Forklift safety training"

    Path(first.storage_path).unlink()
    with pytest.raises(DocumentValidationError, match="Unable to read"):
        get_document_content(session, first.id, actor_context=actor_context)


def test_employee_document_permission_cannot_be_bypassed_by_document_access(
    employee_session,
):
    session, employee, administrator = employee_session
    document = upload_document(
        session,
        filename="private-appointment.pdf",
        content=b"%PDF-1.7\nprivate employee document",
        category="Appointment Letter",
        related_entity_type="Employee",
        related_entity_id=employee.id,
        actor_context=administrator,
    )
    user = create_user(
        session,
        actor_context=administrator,
        username="procurement-document-viewer",
        password="Procurement-document-viewer-2026!",
        full_name="Procurement Document Viewer",
        role_names=["Procurement"],
    )
    viewer = authenticate(
        session, user.username, "Procurement-document-viewer-2026!"
    )

    assert list_documents(session, actor_context=viewer) == []
    with pytest.raises(AuthorizationError):
        get_document_content(session, document.id, actor_context=viewer)
    with pytest.raises(AuthorizationError):
        upload_document(
            session,
            filename="unauthorized-employee-record.pdf",
            content=b"%PDF-1.7\nunauthorized",
            category="Appointment Letter",
            related_entity_type="Employee",
            related_entity_id=employee.id,
            actor_context=viewer,
        )
    logout(session, actor_context=viewer)


def test_employee_reports_exclude_private_data_by_default_and_export_files(
    employee_session, tmp_path
):
    session, _employee, actor_context = employee_session
    default_rows = get_employee_report_rows(
        session, "directory", actor_context=actor_context
    )
    assert len(default_rows) == 1
    assert "phone" not in default_rows[0]
    assert "email" not in default_rows[0]
    assert "residential_address" not in default_rows[0]
    private_rows = get_employee_report_rows(
        session, "directory", actor_context=actor_context, include_private=True
    )
    assert private_rows[0]["phone"] == "9876543210"
    assert private_rows[0]["residential_address"] == "Private residential address"

    pdf_path = tmp_path / "reports" / "directory.pdf"
    excel_path = tmp_path / "reports" / "directory.xlsx"
    generate_employee_pdf_report(
        session, str(pdf_path), actor_context=actor_context
    )
    generate_employee_excel_report(
        session, str(excel_path), actor_context=actor_context
    )
    confidential_path = tmp_path / "reports" / "confidential.xlsx"
    generate_employee_excel_report(
        session, str(confidential_path),
        actor_context=actor_context, include_private=True,
    )
    assert pdf_path.is_file() and pdf_path.stat().st_size > 0
    workbook = load_workbook(excel_path, read_only=True, data_only=True)
    headers = next(workbook.active.iter_rows(values_only=True))
    assert "Phone" not in headers
    assert "Residential Address" not in headers
    workbook.close()
    private_workbook = load_workbook(confidential_path, read_only=True, data_only=True)
    private_headers = next(private_workbook.active.iter_rows(values_only=True))
    assert "Residential Address" in private_headers
    private_workbook.close()


def test_employee_joining_department_type_and_separation_reports(employee_session):
    session, employee, actor_context = employee_session
    assert len(get_employee_report_rows(
        session, "joining", actor_context=actor_context,
        start_date=date(2025, 1, 1), end_date=date(2025, 3, 1)
    )) == 1
    assert len(get_employee_report_rows(
        session, "department", actor_context=actor_context
    )) == 1
    assert len(get_employee_report_rows(
        session, "employment_type", actor_context=actor_context
    )) == 1
    assert len(get_employee_report_rows(
        session, "active", actor_context=actor_context
    )) == 1
    separate_employee(
        session, employee.id, date(2025, 8, 1),
        actor_context=actor_context, reason="End of contract",
    )
    assert len(get_employee_report_rows(
        session, "active", actor_context=actor_context
    )) == 0
    assert len(get_employee_report_rows(
        session, "separation", actor_context=actor_context,
        start_date=date(2025, 7, 1), end_date=date(2025, 9, 1)
    )) == 1
