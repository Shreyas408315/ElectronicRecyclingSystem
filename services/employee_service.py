from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from database.models import Department, Designation, Employee, EmployeeHistory
from services.security_service import ActorContext, require_permission
from utils.validators import clean_string, validate_email, validate_phone, validate_pin_code


EMPLOYMENT_TYPES = ("Full-time", "Part-time", "Contract", "Temporary", "Intern", "Other")
EMPLOYMENT_STATUSES = ("Active", "On Leave", "Inactive", "Separated")
DEFAULT_DEPARTMENTS = (
    "Administration",
    "Procurement",
    "Dismantling",
    "Inventory",
    "Quality Control",
    "Sales",
    "Accounts & Finance",
    "Logistics",
    "Maintenance",
    "Other",
)
SORT_OPTIONS = {
    "name_asc": (Employee.full_name.asc(), Employee.id.asc()),
    "name_desc": (Employee.full_name.desc(), Employee.id.desc()),
    "joining_asc": (Employee.date_of_joining.asc(), Employee.id.asc()),
    "joining_desc": (Employee.date_of_joining.desc(), Employee.id.desc()),
    "code_asc": (Employee.employee_code.asc(), Employee.id.asc()),
}
PRIVATE_EMPLOYEE_FIELDS = frozenset({
    "phone",
    "alternate_phone",
    "email",
    "residential_address",
    "city",
    "state",
    "pin_code",
    "emergency_contact_name",
    "emergency_contact_phone",
    "notes",
    "profile_photo_path",
    "responsible_user",
})


class EmployeeValidationError(ValueError):
    pass


class EmployeeDuplicateWarning(EmployeeValidationError):
    def __init__(self, matches: list[Employee]):
        self.matches = matches
        summary = ", ".join(f"{row.employee_code} — {row.full_name}" for row in matches[:5])
        super().__init__(f"Possible duplicate employee record(s): {summary}. Confirm before continuing.")


def _authorize(session: Session, actor_context: ActorContext, permission: str) -> None:
    require_permission(actor_context, permission, db_session=session)


def _employee_record(
    employee: Employee, *, include_private: bool
) -> SimpleNamespace:
    values = {
        column.key: getattr(employee, column.key)
        for column in Employee.__table__.columns
    }
    if not include_private:
        for key in PRIVATE_EMPLOYEE_FIELDS:
            values[key] = None
    record = SimpleNamespace(**values)
    record.department = SimpleNamespace(
        id=employee.department.id,
        name=employee.department.name,
        is_active=employee.department.is_active,
    )
    record.designation = SimpleNamespace(
        id=employee.designation.id,
        name=employee.designation.name,
        is_active=employee.designation.is_active,
    )
    record.history = [
        _history_record(event, include_private=include_private)
        for event in employee.history
    ] if "history" in employee.__dict__ else []
    return record


def _history_record(
    event: EmployeeHistory, *, include_private: bool
) -> SimpleNamespace:
    values = {
        column.key: getattr(event, column.key)
        for column in EmployeeHistory.__table__.columns
    }
    if not include_private:
        values["details"] = None
        values["responsible_user"] = None
    return SimpleNamespace(**values)


def _get_employee(session: Session, employee_id: int) -> Employee | None:
    return session.scalar(
        select(Employee)
        .options(
            selectinload(Employee.department),
            selectinload(Employee.designation),
            selectinload(Employee.history),
        )
        .where(Employee.id == employee_id)
    )


def _text(value: object, label: str, max_length: int, *, required: bool = False) -> str | None:
    if value is not None and not isinstance(value, str):
        raise EmployeeValidationError(f"{label} must be text.")
    cleaned = clean_string(value)
    if required and not cleaned:
        raise EmployeeValidationError(f"{label} is required.")
    if cleaned and len(cleaned) > max_length:
        raise EmployeeValidationError(f"{label} cannot exceed {max_length} characters.")
    return cleaned


def _date(value: object, label: str, *, optional: bool = False) -> date | None:
    if value in (None, "") and optional:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise EmployeeValidationError(f"{label} must be a valid calendar date.")


def _normalized_name(name: str) -> str:
    return " ".join(name.casefold().split())


def seed_default_departments(
    session: Session, *, actor_context: ActorContext
) -> list[Department]:
    _authorize(session, actor_context, "employees.manage")
    existing = {
        department.name_key: department
        for department in session.scalars(select(Department))
    }
    for name in DEFAULT_DEPARTMENTS:
        key = _normalized_name(name)
        if key not in existing:
            department = Department(name=name, name_key=key, is_active=True)
            session.add(department)
            existing[key] = department
    session.flush()
    return sorted(existing.values(), key=lambda item: item.name.casefold())


def create_department(
    session: Session, name: str, *, actor_context: ActorContext
) -> Department:
    _authorize(session, actor_context, "employees.manage")
    cleaned = _text(name, "Department name", 100, required=True)
    key = _normalized_name(cleaned)
    if session.scalar(select(Department.id).where(Department.name_key == key)) is not None:
        raise EmployeeValidationError("A department with this name already exists.")
    department = Department(name=cleaned, name_key=key)
    session.add(department)
    session.flush()
    return department


def update_department(
    session: Session, department_id: int, name: str, *, actor_context: ActorContext
) -> Department:
    _authorize(session, actor_context, "employees.manage")
    department = session.get(Department, department_id)
    if department is None:
        raise EmployeeValidationError("Department not found.")
    cleaned = _text(name, "Department name", 100, required=True)
    key = _normalized_name(cleaned)
    duplicate = session.scalar(
        select(Department.id).where(
            Department.name_key == key, Department.id != department_id
        )
    )
    if duplicate is not None:
        raise EmployeeValidationError("A department with this name already exists.")
    department.name = cleaned
    department.name_key = key
    session.flush()
    return department


def set_department_active(
    session: Session, department_id: int, active: bool, *, actor_context: ActorContext
) -> Department:
    _authorize(session, actor_context, "employees.manage")
    department = session.get(Department, department_id)
    if department is None:
        raise EmployeeValidationError("Department not found.")
    department.is_active = bool(active)
    session.flush()
    return department


def list_departments(
    session: Session, *, actor_context: ActorContext, active_only: bool = False
) -> list[Department]:
    _authorize(session, actor_context, "employees.view")
    statement = select(Department)
    if active_only:
        statement = statement.where(Department.is_active.is_(True))
    return list(session.scalars(statement.order_by(Department.name)))


def create_designation(
    session: Session, name: str, *, actor_context: ActorContext
) -> Designation:
    _authorize(session, actor_context, "employees.manage")
    cleaned = _text(name, "Designation name", 100, required=True)
    key = _normalized_name(cleaned)
    if session.scalar(select(Designation.id).where(Designation.name_key == key)) is not None:
        raise EmployeeValidationError("A designation with this name already exists.")
    designation = Designation(name=cleaned, name_key=key)
    session.add(designation)
    session.flush()
    return designation


def update_designation(
    session: Session, designation_id: int, name: str, *, actor_context: ActorContext
) -> Designation:
    _authorize(session, actor_context, "employees.manage")
    designation = session.get(Designation, designation_id)
    if designation is None:
        raise EmployeeValidationError("Designation not found.")
    cleaned = _text(name, "Designation name", 100, required=True)
    key = _normalized_name(cleaned)
    duplicate = session.scalar(
        select(Designation.id).where(
            Designation.name_key == key, Designation.id != designation_id
        )
    )
    if duplicate is not None:
        raise EmployeeValidationError("A designation with this name already exists.")
    designation.name = cleaned
    designation.name_key = key
    session.flush()
    return designation


def set_designation_active(
    session: Session, designation_id: int, active: bool, *, actor_context: ActorContext
) -> Designation:
    _authorize(session, actor_context, "employees.manage")
    designation = session.get(Designation, designation_id)
    if designation is None:
        raise EmployeeValidationError("Designation not found.")
    designation.is_active = bool(active)
    session.flush()
    return designation


def list_designations(
    session: Session, *, actor_context: ActorContext, active_only: bool = False
) -> list[Designation]:
    _authorize(session, actor_context, "employees.view")
    statement = select(Designation)
    if active_only:
        statement = statement.where(Designation.is_active.is_(True))
    return list(session.scalars(statement.order_by(Designation.name)))


def find_possible_duplicate_employees(
    session: Session,
    *,
    actor_context: ActorContext,
    full_name: str,
    phone: str | None = None,
    email: str | None = None,
    exclude_employee_id: int | None = None,
    include_private: bool = False,
) -> list[SimpleNamespace]:
    _authorize(session, actor_context, "employees.view")
    if phone or email or include_private:
        _authorize(session, actor_context, "employees.private.view")
    normalized_name = _normalized_name(full_name)
    conditions = [func.lower(func.trim(Employee.full_name)) == normalized_name]
    normalized_phone = "".join(char for char in (phone or "") if char.isdigit())
    if normalized_phone:
        conditions.append(Employee.phone.is_not(None) & (Employee.phone != ""))
        phone_matches = [
            row for row in session.scalars(select(Employee).options(
                selectinload(Employee.department), selectinload(Employee.designation)
            ))
            if "".join(char for char in (row.phone or "") if char.isdigit()) == normalized_phone
        ]
    else:
        phone_matches = []
    normalized_email = (email or "").strip().casefold()
    statement = select(Employee).options(
        selectinload(Employee.department), selectinload(Employee.designation)
    ).where(or_(
        func.lower(func.trim(Employee.full_name)) == normalized_name,
        (func.lower(Employee.email) == normalized_email) if normalized_email else False,
    ))
    if exclude_employee_id is not None:
        statement = statement.where(Employee.id != exclude_employee_id)
    matches_by_id = {employee.id: employee for employee in session.scalars(statement)}
    for employee in phone_matches:
        if employee.id != exclude_employee_id:
            matches_by_id[employee.id] = employee
    return [
        _employee_record(employee, include_private=include_private)
        for employee in matches_by_id.values()
    ]


def _validate_employee_values(
    session: Session,
    values: dict,
    *,
    existing: Employee | None = None,
) -> dict:
    full_name = _text(values.get("full_name", existing.full_name if existing else None),
                      "Full name", 200, required=True)
    department_id = values.get("department_id", existing.department_id if existing else None)
    designation_id = values.get("designation_id", existing.designation_id if existing else None)
    try:
        department_id = int(department_id)
        designation_id = int(designation_id)
    except (TypeError, ValueError) as exc:
        raise EmployeeValidationError("Select a department and designation.") from exc
    department = session.get(Department, department_id)
    designation = session.get(Designation, designation_id)
    if department is None or designation is None:
        raise EmployeeValidationError("Selected department or designation does not exist.")
    if existing is None and (not department.is_active or not designation.is_active):
        raise EmployeeValidationError("New employees must use active departments and designations.")
    if existing and department_id != existing.department_id and not department.is_active:
        raise EmployeeValidationError("An inactive department cannot be newly assigned.")
    if existing and designation_id != existing.designation_id and not designation.is_active:
        raise EmployeeValidationError("An inactive designation cannot be newly assigned.")

    employment_type = values.get("employment_type", existing.employment_type if existing else None)
    if employment_type not in EMPLOYMENT_TYPES:
        raise EmployeeValidationError("Choose a supported employment type.")
    employment_status = values.get(
        "employment_status", existing.employment_status if existing else "Active"
    )
    if employment_status not in EMPLOYMENT_STATUSES:
        raise EmployeeValidationError("Choose a supported employment status.")
    if existing and existing.employment_status == "Separated" and employment_status != "Separated":
        raise EmployeeValidationError("Separated employees cannot be reactivated or returned to active status.")

    joining = _date(
        values.get("date_of_joining", existing.date_of_joining if existing else None),
        "Date of joining",
    )
    leaving_value = values.get(
        "date_of_leaving", existing.date_of_leaving if existing else None
    )
    leaving = _date(leaving_value, "Date of leaving", optional=True)
    if leaving and leaving < joining:
        raise EmployeeValidationError("Date of leaving cannot precede date of joining.")
    if employment_status == "Separated" and leaving is None:
        raise EmployeeValidationError("Date of leaving is required for separated employees.")
    if employment_status != "Separated" and leaving is not None:
        raise EmployeeValidationError("Date of leaving is only valid for separated employees.")

    output = {
        "full_name": full_name,
        "department_id": department_id,
        "designation_id": designation_id,
        "employment_type": employment_type,
        "employment_status": employment_status,
        "date_of_joining": joining,
        "date_of_leaving": leaving,
    }
    for key, label, max_length in (
        ("phone", "Phone", 20),
        ("alternate_phone", "Alternate phone", 20),
        ("email", "Email", 120),
        ("residential_address", "Residential address", 500),
        ("city", "City", 100),
        ("state", "State", 100),
        ("pin_code", "PIN code", 20),
        ("emergency_contact_name", "Emergency contact name", 100),
        ("emergency_contact_phone", "Emergency contact phone", 20),
        ("notes", "Notes", 5000),
        ("responsible_user", "Responsible user", 100),
    ):
        output[key] = _text(
            values.get(key, getattr(existing, key, None)),
            label,
            max_length,
        )
    for key, label in (
        ("phone", "Phone"),
        ("alternate_phone", "Alternate phone"),
        ("emergency_contact_phone", "Emergency contact phone"),
    ):
        valid, error = validate_phone(output[key], label)
        if not valid:
            raise EmployeeValidationError(error)
    valid, error = validate_email(output["email"])
    if not valid:
        raise EmployeeValidationError(error)
    valid, error = validate_pin_code(output["pin_code"])
    if not valid:
        raise EmployeeValidationError(error)
    return output


def _append_history(
    session: Session,
    employee: Employee,
    event_type: str,
    *,
    previous_status: str | None = None,
    new_status: str | None = None,
    previous_department: str | None = None,
    new_department: str | None = None,
    previous_designation: str | None = None,
    new_designation: str | None = None,
    event_date: date | None = None,
    details: str | None = None,
    responsible_user: str | None = None,
) -> None:
    session.add(EmployeeHistory(
        employee_id=employee.id,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        previous_department=previous_department,
        new_department=new_department,
        previous_designation=previous_designation,
        new_designation=new_designation,
        event_date=event_date or date.today(),
        details=details,
        responsible_user=responsible_user,
    ))


def create_employee(
    session: Session,
    values: dict,
    *,
    actor_context: ActorContext,
    allow_possible_duplicate: bool = False,
) -> SimpleNamespace:
    _authorize(session, actor_context, "employees.manage")
    if PRIVATE_EMPLOYEE_FIELDS.intersection(values):
        _authorize(session, actor_context, "employees.private.view")
    validated = _validate_employee_values(session, values)
    possible_duplicates = find_possible_duplicate_employees(
        session,
        actor_context=actor_context,
        full_name=validated["full_name"],
        phone=validated["phone"] if "phone" in values else None,
        email=validated["email"] if "email" in values else None,
    )
    if possible_duplicates and not allow_possible_duplicate:
        raise EmployeeDuplicateWarning(possible_duplicates)
    employee = Employee(employee_code=f"EMP-PENDING-{uuid4().hex}", **validated)
    with session.begin_nested():
        session.add(employee)
        session.flush()
        employee.employee_code = f"EMP-{employee.id:06d}"
        _append_history(
            session,
            employee,
            "CREATED",
            new_status=employee.employment_status,
            new_department=employee.department.name,
            new_designation=employee.designation.name,
            event_date=employee.date_of_joining,
            details="Employee profile created.",
            responsible_user=employee.responsible_user,
        )
        session.flush()
    return _employee_record(employee, include_private=False)


def get_employee_by_id(
    session: Session,
    employee_id: int,
    *,
    actor_context: ActorContext,
    include_private: bool = False,
) -> SimpleNamespace | None:
    _authorize(session, actor_context, "employees.view")
    if include_private:
        _authorize(session, actor_context, "employees.private.view")
    employee = _get_employee(session, employee_id)
    return (
        _employee_record(employee, include_private=include_private)
        if employee is not None else None
    )


def update_employee(
    session: Session,
    employee_id: int,
    values: dict,
    *,
    actor_context: ActorContext,
) -> SimpleNamespace:
    _authorize(session, actor_context, "employees.manage")
    if PRIVATE_EMPLOYEE_FIELDS.intersection(values):
        _authorize(session, actor_context, "employees.private.view")
    employee = _get_employee(session, employee_id)
    if employee is None:
        raise EmployeeValidationError(f"Employee #{employee_id} does not exist.")
    validated = _validate_employee_values(session, values, existing=employee)
    possible_duplicates = find_possible_duplicate_employees(
        session,
        actor_context=actor_context,
        full_name=validated["full_name"],
        phone=validated["phone"] if "phone" in values else None,
        email=validated["email"] if "email" in values else None,
        exclude_employee_id=employee.id,
    )
    if possible_duplicates and not values.get("allow_possible_duplicate", False):
        raise EmployeeDuplicateWarning(possible_duplicates)
    old_status = employee.employment_status
    old_department = employee.department.name
    old_designation = employee.designation.name
    with session.begin_nested():
        for key, value in validated.items():
            setattr(employee, key, value)
        employee.updated_at = datetime.now()
        new_department = session.get(Department, employee.department_id).name
        new_designation = session.get(Designation, employee.designation_id).name
        event_type = "UPDATED"
        if old_status != employee.employment_status:
            event_type = {
                "Active": "REACTIVATED",
                "Inactive": "DEACTIVATED",
                "On Leave": "STATUS_CHANGED",
                "Separated": "SEPARATED",
            }[employee.employment_status]
        _append_history(
            session,
            employee,
            event_type,
            previous_status=old_status if old_status != employee.employment_status else None,
            new_status=employee.employment_status if old_status != employee.employment_status else None,
            previous_department=old_department if old_department != new_department else None,
            new_department=new_department if old_department != new_department else None,
            previous_designation=old_designation if old_designation != new_designation else None,
            new_designation=new_designation if old_designation != new_designation else None,
            event_date=employee.date_of_leaving if employee.employment_status == "Separated"
            else date.today(),
            details="Employee profile updated.",
            responsible_user=employee.responsible_user,
        )
        session.flush()
    return _employee_record(employee, include_private=False)


def list_employees(
    session: Session,
    *,
    actor_context: ActorContext,
    search: str | None = None,
    department_id: int | None = None,
    designation_id: int | None = None,
    employment_type: str | None = None,
    employment_status: str | None = None,
    joining_start: date | None = None,
    joining_end: date | None = None,
    sort_by: str = "name_asc",
    offset: int = 0,
    limit: int = 100,
    include_private: bool = False,
) -> list[SimpleNamespace]:
    _authorize(session, actor_context, "employees.view")
    if include_private:
        _authorize(session, actor_context, "employees.private.view")
    if offset < 0 or limit <= 0 or limit > 1000:
        raise EmployeeValidationError("Pagination requires a nonnegative offset and limit from 1 to 1000.")
    if sort_by not in SORT_OPTIONS:
        raise EmployeeValidationError("Unsupported employee sort order.")
    if employment_type and employment_type not in EMPLOYMENT_TYPES:
        raise EmployeeValidationError("Unsupported employment type filter.")
    if employment_status and employment_status != "ALL" and employment_status not in EMPLOYMENT_STATUSES:
        raise EmployeeValidationError("Unsupported employment status filter.")
    if joining_start and joining_end and joining_start > joining_end:
        raise EmployeeValidationError("Joining date start cannot be after its end.")
    statement = select(Employee).options(
        selectinload(Employee.department), selectinload(Employee.designation)
    )
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        search_columns = [Employee.employee_code.ilike(pattern), Employee.full_name.ilike(pattern)]
        if include_private:
            search_columns.extend((Employee.phone.ilike(pattern), Employee.email.ilike(pattern)))
        statement = statement.where(or_(*search_columns))
    if department_id is not None:
        statement = statement.where(Employee.department_id == department_id)
    if designation_id is not None:
        statement = statement.where(Employee.designation_id == designation_id)
    if employment_type:
        statement = statement.where(Employee.employment_type == employment_type)
    if employment_status and employment_status != "ALL":
        statement = statement.where(Employee.employment_status == employment_status)
    if joining_start:
        statement = statement.where(Employee.date_of_joining >= joining_start)
    if joining_end:
        statement = statement.where(Employee.date_of_joining <= joining_end)
    employees = list(session.scalars(
        statement.order_by(*SORT_OPTIONS[sort_by]).offset(offset).limit(limit)
    ))
    return [
        _employee_record(employee, include_private=include_private)
        for employee in employees
    ]


def set_employee_status(
    session: Session,
    employee_id: int,
    status: str,
    *,
    actor_context: ActorContext,
    effective_date: date | None = None,
    reason: str | None = None,
    responsible_user: str | None = None,
) -> SimpleNamespace:
    _authorize(session, actor_context, "employees.manage")
    if responsible_user is not None:
        _authorize(session, actor_context, "employees.private.view")
    employee = _get_employee(session, employee_id)
    if employee is None:
        raise EmployeeValidationError(f"Employee #{employee_id} does not exist.")
    if status not in EMPLOYMENT_STATUSES:
        raise EmployeeValidationError("Choose a supported employment status.")
    old_status = employee.employment_status
    if old_status == status:
        raise EmployeeValidationError(f"Employee is already {status.lower()}.")
    if old_status == "Separated":
        raise EmployeeValidationError("Separated employees cannot be reactivated or changed to another status.")
    if status == "Active" and old_status != "Inactive" and old_status != "On Leave":
        raise EmployeeValidationError("Only inactive or on-leave employees can be activated.")
    day = _date(effective_date or date.today(), "Effective date")
    leaving = day if status == "Separated" else None
    if leaving and leaving < employee.date_of_joining:
        raise EmployeeValidationError("Date of leaving cannot precede date of joining.")
    employee.employment_status = status
    employee.date_of_leaving = leaving
    employee.responsible_user = _text(
        responsible_user, "Responsible user", 100
    ) or employee.responsible_user
    employee.updated_at = datetime.now()
    event_type = {
        "Inactive": "DEACTIVATED",
        "Active": "REACTIVATED",
        "On Leave": "STATUS_CHANGED",
        "Separated": "SEPARATED",
    }[status]
    _append_history(
        session,
        employee,
        event_type,
        previous_status=old_status,
        new_status=status,
        event_date=day,
        details=_text(reason, "Reason", 1000),
        responsible_user=employee.responsible_user,
    )
    session.flush()
    return _employee_record(employee, include_private=False)


def deactivate_employee(
    session: Session,
    employee_id: int,
    *,
    actor_context: ActorContext,
    reason: str | None = None,
) -> SimpleNamespace:
    return set_employee_status(
        session, employee_id, "Inactive", actor_context=actor_context, reason=reason
    )


def reactivate_employee(
    session: Session,
    employee_id: int,
    *,
    actor_context: ActorContext,
    reason: str | None = None,
) -> SimpleNamespace:
    return set_employee_status(
        session, employee_id, "Active", actor_context=actor_context, reason=reason
    )


def separate_employee(
    session: Session,
    employee_id: int,
    leaving_date: date,
    *,
    actor_context: ActorContext,
    reason: str | None = None,
) -> SimpleNamespace:
    return set_employee_status(
        session,
        employee_id,
        "Separated",
        actor_context=actor_context,
        effective_date=leaving_date,
        reason=reason,
    )


def get_employee_summary(
    session: Session, *, actor_context: ActorContext
) -> dict:
    _authorize(session, actor_context, "employees.view")
    totals = {
        status: int(session.scalar(
            select(func.count(Employee.id)).where(Employee.employment_status == status)
        ) or 0)
        for status in EMPLOYMENT_STATUSES
    }
    departments = session.execute(
        select(Department.name, func.count(Employee.id))
        .outerjoin(Employee, Employee.department_id == Department.id)
        .group_by(Department.id, Department.name)
        .order_by(Department.name)
    ).all()
    return {
        "total": sum(totals.values()),
        "active": totals["Active"],
        "on_leave": totals["On Leave"],
        "inactive": totals["Inactive"],
        "separated": totals["Separated"],
        "by_department": {name: int(count) for name, count in departments if count},
    }


def get_employee_employment_history(
    session: Session,
    employee_id: int,
    *,
    actor_context: ActorContext,
    include_private: bool = False,
) -> list[SimpleNamespace]:
    _authorize(session, actor_context, "employees.view")
    if include_private:
        _authorize(session, actor_context, "employees.private.view")
    if session.get(Employee, employee_id) is None:
        raise EmployeeValidationError(f"Employee #{employee_id} does not exist.")
    history = list(session.scalars(
        select(EmployeeHistory)
        .where(EmployeeHistory.employee_id == employee_id)
        .order_by(EmployeeHistory.event_date.desc(), EmployeeHistory.id.desc())
    ))
    return [
        _history_record(event, include_private=include_private)
        for event in history
    ]
