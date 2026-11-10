from datetime import datetime
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from database.models import Supplier
from utils.validators import (
    clean_string,
    validate_business_name,
    validate_phone,
    validate_email,
    validate_gstin,
    validate_pin_code,
    validate_opening_balance,
)
from services.security_service import ActorContext, require_permission

class SupplierValidationError(ValueError):
    """Custom exception raised when supplier validation fails."""
    pass

class SupplierNotFoundError(ValueError):
    """Custom exception raised when supplier cannot be found."""
    pass


def _next_supplier_code(session: Session) -> str:
    """Generate the next code for already-authorized internal operations."""
    codes = session.scalars(select(Supplier.supplier_code)).all()
    max_num = 0
    for code in codes:
        if code and code.startswith("SUP-"):
            num_part = code[4:]
            if num_part.isdigit():
                max_num = max(max_num, int(num_part))
    return f"SUP-{max_num + 1:06d}"


def generate_next_supplier_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    """Generate the next sequential supplier code for an authorized actor."""
    require_permission(actor_context, "suppliers.manage", db_session=session)
    return _next_supplier_code(session)


def validate_supplier_payload(payload: dict, is_update: bool = False) -> dict:
    """Validates raw dictionary payload and returns cleaned data or raises SupplierValidationError."""
    errors = []

    # Business Name
    b_name = payload.get("business_name")
    valid_name, err = validate_business_name(b_name)
    if not valid_name:
        errors.append(err)

    # Phone numbers
    phone = payload.get("phone")
    valid_phone, err = validate_phone(phone, "Phone number")
    if not valid_phone:
        errors.append(err)

    alt_phone = payload.get("alternate_phone")
    valid_alt, err = validate_phone(alt_phone, "Alternate phone")
    if not valid_alt:
        errors.append(err)

    # Email
    email = payload.get("email")
    valid_email, err = validate_email(email)
    if not valid_email:
        errors.append(err)

    # GSTIN
    gstin = payload.get("gstin")
    valid_gstin, err = validate_gstin(gstin)
    if not valid_gstin:
        errors.append(err)

    # PIN Code
    pin_code = payload.get("pin_code")
    valid_pin, err = validate_pin_code(pin_code)
    if not valid_pin:
        errors.append(err)

    # Opening Balance
    opening_bal = payload.get("opening_balance", 0.0)
    valid_bal, parsed_bal, err = validate_opening_balance(opening_bal)
    if not valid_bal:
        errors.append(err)

    if errors:
        raise SupplierValidationError(" \n".join(errors))

    cleaned = {
        "business_name": clean_string(b_name),
        "contact_person": clean_string(payload.get("contact_person")),
        "phone": clean_string(phone),
        "alternate_phone": clean_string(alt_phone),
        "email": clean_string(email),
        "address": clean_string(payload.get("address")),
        "city": clean_string(payload.get("city")),
        "state": clean_string(payload.get("state")),
        "pin_code": clean_string(pin_code),
        "gstin": clean_string(gstin).upper() if clean_string(gstin) else None,
        "opening_balance": parsed_bal,
        "remarks": clean_string(payload.get("remarks")),
    }
    if "is_active" in payload:
        cleaned["is_active"] = bool(payload["is_active"])

    return cleaned


def create_supplier(
    session: Session, payload: dict, *, actor_context: ActorContext
) -> Supplier:
    """Creates a new supplier record with validation and unique code check."""
    require_permission(actor_context, "suppliers.manage", db_session=session)
    cleaned = validate_supplier_payload(payload, is_update=False)

    supplier_code = clean_string(payload.get("supplier_code"))
    if not supplier_code:
        supplier_code = generate_next_supplier_code(session, actor_context=actor_context)
    else:
        # Check uniqueness
        existing = session.scalar(
            select(Supplier).where(Supplier.supplier_code == supplier_code)
        )
        if existing:
            raise SupplierValidationError(f"Supplier ID '{supplier_code}' already exists.")

    supplier = Supplier(
        supplier_code=supplier_code,
        business_name=cleaned["business_name"],
        contact_person=cleaned["contact_person"],
        phone=cleaned["phone"],
        alternate_phone=cleaned["alternate_phone"],
        email=cleaned["email"],
        address=cleaned["address"],
        city=cleaned["city"],
        state=cleaned["state"],
        pin_code=cleaned["pin_code"],
        gstin=cleaned["gstin"],
        opening_balance=cleaned["opening_balance"],
        remarks=cleaned["remarks"],
        is_active=cleaned.get("is_active", True),
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    session.add(supplier)
    session.flush()
    return supplier


def update_supplier(
    session: Session,
    supplier_id: int,
    payload: dict,
    *,
    actor_context: ActorContext,
) -> Supplier:
    """Updates an existing supplier record. Supplier ID (code) cannot be changed."""
    require_permission(actor_context, "suppliers.manage", db_session=session)
    supplier = session.get(Supplier, supplier_id)
    if not supplier:
        raise SupplierNotFoundError(f"Supplier with ID #{supplier_id} not found.")

    cleaned = validate_supplier_payload(payload, is_update=True)

    supplier.business_name = cleaned["business_name"]
    supplier.contact_person = cleaned["contact_person"]
    supplier.phone = cleaned["phone"]
    supplier.alternate_phone = cleaned["alternate_phone"]
    supplier.email = cleaned["email"]
    supplier.address = cleaned["address"]
    supplier.city = cleaned["city"]
    supplier.state = cleaned["state"]
    supplier.pin_code = cleaned["pin_code"]
    supplier.gstin = cleaned["gstin"]
    supplier.opening_balance = cleaned["opening_balance"]
    supplier.remarks = cleaned["remarks"]
    if "is_active" in cleaned:
        supplier.is_active = cleaned["is_active"]
    supplier.updated_at = datetime.now()

    session.flush()
    return supplier


def deactivate_supplier(session: Session, supplier_id: int, *, actor_context: ActorContext) -> Supplier:
    """Deactivates a supplier (soft delete)."""
    require_permission(actor_context, "suppliers.manage", db_session=session)
    supplier = session.get(Supplier, supplier_id)
    if not supplier:
        raise SupplierNotFoundError(f"Supplier with ID #{supplier_id} not found.")
    supplier.is_active = False
    supplier.updated_at = datetime.now()
    session.flush()
    return supplier


def reactivate_supplier(session: Session, supplier_id: int, *, actor_context: ActorContext) -> Supplier:
    """Reactivates an inactive supplier."""
    require_permission(actor_context, "suppliers.manage", db_session=session)
    supplier = session.get(Supplier, supplier_id)
    if not supplier:
        raise SupplierNotFoundError(f"Supplier with ID #{supplier_id} not found.")
    supplier.is_active = True
    supplier.updated_at = datetime.now()
    session.flush()
    return supplier


def get_supplier_by_id(session: Session, supplier_id: int, *, actor_context: ActorContext) -> Supplier | None:
    require_permission(actor_context, "suppliers.view", db_session=session)
    return session.get(Supplier, supplier_id)


def get_supplier_by_code(session: Session, code: str, *, actor_context: ActorContext) -> Supplier | None:
    require_permission(actor_context, "suppliers.view", db_session=session)
    return session.scalar(select(Supplier).where(Supplier.supplier_code == code.strip()))


def list_suppliers(
    session: Session,
    search: str = "",
    status_filter: str = "ALL",
    sort_by: str = "name_asc",
    *,
    actor_context: ActorContext,
) -> list[Supplier]:
    require_permission(actor_context, "suppliers.view", db_session=session)
    stmt = select(Supplier)

    # Search filter (Supplier ID, business name, phone, contact person)
    search = search.strip()
    if search:
        search_pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Supplier.supplier_code.ilike(search_pattern),
                Supplier.business_name.ilike(search_pattern),
                Supplier.contact_person.ilike(search_pattern),
                Supplier.phone.ilike(search_pattern),
                Supplier.city.ilike(search_pattern),
                Supplier.email.ilike(search_pattern),
            )
        )

    # Status filter
    status = status_filter.upper()
    if status == "ACTIVE":
        stmt = stmt.where(Supplier.is_active.is_(True))
    elif status == "INACTIVE":
        stmt = stmt.where(Supplier.is_active.is_(False))

    # Sorting
    if sort_by == "name_asc":
        stmt = stmt.order_by(Supplier.business_name.asc())
    elif sort_by == "name_desc":
        stmt = stmt.order_by(Supplier.business_name.desc())
    elif sort_by == "date_desc":
        stmt = stmt.order_by(Supplier.created_at.desc())
    elif sort_by == "date_asc":
        stmt = stmt.order_by(Supplier.created_at.asc())
    elif sort_by == "code_asc":
        stmt = stmt.order_by(Supplier.supplier_code.asc())
    else:
        stmt = stmt.order_by(Supplier.id.asc())

    return list(session.scalars(stmt).all())


def get_supplier_metrics(session: Session, *, actor_context: ActorContext) -> dict:
    require_permission(actor_context, "suppliers.view", db_session=session)
    total = session.scalar(select(func.count(Supplier.id))) or 0
    active = session.scalar(
        select(func.count(Supplier.id)).where(Supplier.is_active.is_(True))
    ) or 0
    inactive = session.scalar(
        select(func.count(Supplier.id)).where(Supplier.is_active.is_(False))
    ) or 0
    total_opening_bal = session.scalar(
        select(func.coalesce(func.sum(Supplier.opening_balance), 0.0))
    ) or 0.0

    return {
        "total": total,
        "active": active,
        "inactive": inactive,
        "total_opening_balance": total_opening_bal,
    }

