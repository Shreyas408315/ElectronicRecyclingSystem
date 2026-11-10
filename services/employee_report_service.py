from html import escape
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from database.models import Employee
from services.security_service import ActorContext, require_permission
from services.employee_service import EmployeeValidationError, list_employees


REPORT_TYPES = {
    "directory": "Employee Directory",
    "active": "Active Employee List",
    "inactive": "Inactive Employee List",
    "department": "Employees by Department",
    "designation": "Employees by Designation",
    "employment_type": "Employees by Employment Type",
    "joining": "Joining Report",
    "separation": "Separation Report",
}
BASE_FIELDS = (
    ("employee_code", "Employee ID"),
    ("full_name", "Employee Name"),
    ("designation", "Designation"),
    ("department", "Department"),
    ("employment_type", "Employment Type"),
    ("date_of_joining", "Date of Joining"),
    ("employment_status", "Status"),
    ("date_of_leaving", "Date of Leaving"),
)
PRIVATE_FIELDS = (
    ("phone", "Phone"),
    ("alternate_phone", "Alternate Phone"),
    ("email", "Email"),
    ("residential_address", "Residential Address"),
    ("city", "City"),
    ("state", "State"),
    ("pin_code", "PIN Code"),
    ("emergency_contact_name", "Emergency Contact"),
    ("emergency_contact_phone", "Emergency Contact Phone"),
)


def get_employee_report_rows(
    session: Session,
    report_type: str = "directory",
    *,
    actor_context: ActorContext,
    department_id: int | None = None,
    designation_id: int | None = None,
    employment_type: str | None = None,
    start_date=None,
    end_date=None,
    include_private: bool = False,
) -> list[dict[str, str]]:
    require_permission(actor_context, "employees.view", db_session=session)
    if include_private:
        require_permission(actor_context, "employees.private.view", db_session=session)
        require_permission(actor_context, "employees.export_private", db_session=session)
    if report_type not in REPORT_TYPES:
        raise EmployeeValidationError("Unsupported employee report type.")
    if start_date and end_date and start_date > end_date:
        raise EmployeeValidationError("Report start date cannot be after its end date.")

    status_filter = {
        "active": "Active",
        "inactive": "Inactive",
        "separation": "Separated",
    }.get(report_type, "ALL")
    if report_type == "separation":
        statement = select(Employee).options(
            selectinload(Employee.department), selectinload(Employee.designation)
        ).where(Employee.employment_status == "Separated")
        if start_date:
            statement = statement.where(Employee.date_of_leaving >= start_date)
        if end_date:
            statement = statement.where(Employee.date_of_leaving <= end_date)
        employees = list(session.scalars(
            statement.order_by(Employee.date_of_leaving.desc(), Employee.employee_code)
        ))
    else:
        join_start = start_date if report_type == "joining" else None
        join_end = end_date if report_type == "joining" else None
        employees = []
        offset = 0
        while True:
            batch = list_employees(
                session,
                actor_context=actor_context,
                department_id=department_id,
                designation_id=designation_id,
                employment_type=employment_type,
                employment_status=status_filter,
                joining_start=join_start,
                joining_end=join_end,
                sort_by="name_asc",
                offset=offset,
                limit=1000,
                include_private=include_private,
            )
            employees.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000

    fields = BASE_FIELDS + (PRIVATE_FIELDS if include_private else ())
    return [
        {
            key: (
                employee.department.name if key == "department" else
                employee.designation.name if key == "designation" else
                getattr(employee, key).isoformat() if key in {"date_of_joining", "date_of_leaving"}
                and getattr(employee, key) else
                str(getattr(employee, key) or "")
            )
            for key, _label in fields
        }
        for employee in employees
    ]


def _report_fields(include_private: bool) -> tuple[tuple[str, str], ...]:
    return BASE_FIELDS + (PRIVATE_FIELDS if include_private else ())


def _report_title(report_type: str, include_private: bool) -> str:
    title = REPORT_TYPES[report_type]
    return f"{title} — CONFIDENTIAL ADMINISTRATIVE EXPORT" if include_private else title


def generate_employee_pdf_report(
    session: Session,
    output_path: str,
    report_type: str = "directory",
    *,
    actor_context: ActorContext,
    department_id: int | None = None,
    designation_id: int | None = None,
    employment_type: str | None = None,
    start_date=None,
    end_date=None,
    include_private: bool = False,
) -> str:
    rows = get_employee_report_rows(
        session,
        report_type,
        actor_context=actor_context,
        department_id=department_id,
        designation_id=designation_id,
        employment_type=employment_type,
        start_date=start_date,
        end_date=end_date,
        include_private=include_private,
    )
    fields = _report_fields(include_private)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    table_rows = [[label for _key, label in fields]]
    for row in rows:
        table_rows.append([
            Paragraph(escape(row[key]), styles["BodyText"]) for key, _label in fields
        ])
    column_width = 275 * mm / max(len(fields), 1)
    table = Table(table_rows, repeatRows=1, colWidths=[column_width] * len(fields))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("PADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    subtitle = (
        "Includes personal contact/address information; store and distribute securely."
        if include_private else "Personal contact, residential and emergency-contact details are excluded."
    )
    SimpleDocTemplate(
        output_path,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    ).build([
        Paragraph(escape(_report_title(report_type, include_private)), styles["Title"]),
        Paragraph(escape(subtitle), styles["Normal"]),
        Paragraph(f"Records: {len(rows)}", styles["Normal"]),
        Spacer(1, 8),
        table,
    ])
    return output_path


def generate_employee_excel_report(
    session: Session,
    output_path: str,
    report_type: str = "directory",
    *,
    actor_context: ActorContext,
    department_id: int | None = None,
    designation_id: int | None = None,
    employment_type: str | None = None,
    start_date=None,
    end_date=None,
    include_private: bool = False,
) -> str:
    rows = get_employee_report_rows(
        session,
        report_type,
        actor_context=actor_context,
        department_id=department_id,
        designation_id=designation_id,
        employment_type=employment_type,
        start_date=start_date,
        end_date=end_date,
        include_private=include_private,
    )
    fields = _report_fields(include_private)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = REPORT_TYPES[report_type][:31]
    worksheet.append([label for _key, label in fields])
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1E3A8A")
        cell.alignment = Alignment(horizontal="center")
    for row in rows:
        worksheet.append([row[key] for key, _label in fields])
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    for column in worksheet.columns:
        width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 42)
        worksheet.column_dimensions[column[0].column_letter].width = width
    workbook.save(output_path)
    return output_path
