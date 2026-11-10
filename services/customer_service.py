from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from database.models import Customer
from services.security_service import ActorContext, require_permission
from utils.validators import (
    clean_string,
    validate_email,
    validate_gstin,
    validate_pan,
    validate_phone,
    validate_pin_code,
)


CENT = Decimal("0.01")
ZERO = Decimal("0.00")
CUSTOMER_TYPES = (
    "Individual",
    "Retailer",
    "Wholesaler",
    "Distributor",
    "Scrap Dealer",
    "Manufacturer",
    "Other",
)
PAYMENT_TERMS = ("Immediate", "7 days", "15 days", "30 days", "45 days", "60 days", "Custom")
PAYMENT_METHODS = ("Cash", "Bank Transfer", "UPI", "Cheque", "Card", "Other")
_PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$", re.IGNORECASE)


class CustomerValidationError(ValueError):
    """Raised when customer data violates the module's business rules."""


class CustomerNotFoundError(ValueError):
    """Raised when a requested customer does not exist."""


class CustomerDuplicateWarning(CustomerValidationError):
    """Potential duplicates require explicit UI confirmation, never automatic merging."""

    def __init__(self, customers: list[Customer]):
        self.customers = customers
        summary = ", ".join(f"{item.customer_code} — {item.customer_name}" for item in customers)
        super().__init__(f"Potential matching customer records were found: {summary}. Confirm to save anyway.")


def _money(value: object, label: str, *, optional: bool = False) -> Decimal | None:
    if optional and (value is None or value == ""):
        return None
    try:
        result = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
        if not result.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CustomerValidationError(f"{label} must be a valid amount.") from exc
    if result < ZERO:
        raise CustomerValidationError(f"{label} cannot be negative.")
    return result


def generate_next_customer_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    require_permission(actor_context, "customers.manage", db_session=session)
    codes = session.scalars(select(Customer.customer_code)).all()
    highest = max(
        (
            int(code[4:])
            for code in codes
            if code and code.startswith("CUS-") and code[4:].isdigit()
        ),
        default=0,
    )
    return f"CUS-{highest + 1:06d}"


def _validate_payload(payload: dict) -> dict:
    errors: list[str] = []
    name = clean_string(payload.get("customer_name"))
    if not name:
        errors.append("Customer name is required.")
    elif len(name) < 2 or len(name) > 200:
        errors.append("Customer name must be between 2 and 200 characters.")

    customer_type = clean_string(payload.get("customer_type")) or "Other"
    if customer_type not in CUSTOMER_TYPES:
        errors.append(f"Customer type must be one of: {', '.join(CUSTOMER_TYPES)}.")

    phone = clean_string(payload.get("phone"))
    for field, value, validator, label in (
        ("phone", phone, validate_phone, "Primary phone"),
        ("alternate_phone", payload.get("alternate_phone"), validate_phone, "Alternate phone"),
        ("email", payload.get("email"), validate_email, "Email"),
        ("gstin", payload.get("gstin"), validate_gstin, "GSTIN"),
        ("pan", payload.get("pan"), validate_pan, "PAN"),
        ("pin_code", payload.get("pin_code"), validate_pin_code, "PIN code"),
    ):
        valid, error = validator(value, label) if validator is validate_phone else validator(value)
        if not valid:
            errors.append(error)

    terms = clean_string(payload.get("payment_terms")) or "Immediate"
    if terms not in PAYMENT_TERMS:
        errors.append(f"Payment terms must be one of: {', '.join(PAYMENT_TERMS)}.")
    custom_days = payload.get("custom_payment_days")
    parsed_custom_days: int | None = None
    if terms == "Custom":
        try:
            parsed_custom_days = int(custom_days)
            if parsed_custom_days < 0 or parsed_custom_days > 3650:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("Custom payment days must be a whole number between 0 and 3650.")
    elif custom_days not in (None, ""):
        try:
            parsed_custom_days = int(custom_days)
            if parsed_custom_days < 0 or parsed_custom_days > 3650:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("Custom payment days must be a whole number between 0 and 3650.")

    try:
        opening_balance = _money(payload.get("opening_balance", ZERO), "Opening balance")
    except CustomerValidationError as exc:
        errors.append(str(exc))
        opening_balance = ZERO
    try:
        credit_limit = _money(payload.get("credit_limit"), "Credit limit", optional=True)
    except CustomerValidationError as exc:
        errors.append(str(exc))
        credit_limit = None

    if errors:
        raise CustomerValidationError("\n".join(errors))

    return {
        "customer_name": name,
        "customer_type": customer_type,
        "contact_person": clean_string(payload.get("contact_person")),
        "phone": phone,
        "alternate_phone": clean_string(payload.get("alternate_phone")),
        "email": clean_string(payload.get("email")).lower() if clean_string(payload.get("email")) else None,
        "gstin": clean_string(payload.get("gstin")).upper() if clean_string(payload.get("gstin")) else None,
        "pan": clean_string(payload.get("pan")).upper() if clean_string(payload.get("pan")) else None,
        "billing_address": clean_string(payload.get("billing_address")),
        "shipping_address": clean_string(payload.get("shipping_address")),
        "city": clean_string(payload.get("city")),
        "state": clean_string(payload.get("state")),
        "pin_code": clean_string(payload.get("pin_code")),
        "country": clean_string(payload.get("country")) or "India",
        "opening_balance": opening_balance,
        "credit_limit": credit_limit,
        "payment_terms": terms,
        "custom_payment_days": parsed_custom_days if terms == "Custom" else None,
        "preferred_payment_method": clean_string(payload.get("preferred_payment_method")),
        "notes": clean_string(payload.get("notes")),
        "created_by": clean_string(payload.get("created_by")),
        "is_active": bool(payload.get("is_active", True)),
    }


def find_potential_customer_duplicates(
    session: Session,
    payload: dict,
    exclude_customer_id: int | None = None,
    *,
    actor_context: ActorContext,
) -> list[Customer]:
    """Finds likely matches for a warning; it never merges or changes any record."""
    require_permission(actor_context, "customers.view", db_session=session)
    candidate_name = (clean_string(payload.get("customer_name")) or "").casefold()
    candidate_email = (clean_string(payload.get("email")) or "").casefold()
    candidate_gstin = (clean_string(payload.get("gstin")) or "").upper()
    candidate_phone = re.sub(r"\D", "", clean_string(payload.get("phone")) or "")
    stmt = select(Customer)
    if exclude_customer_id is not None:
        stmt = stmt.where(Customer.id != exclude_customer_id)
    possible = session.scalars(stmt).all()
    matches: list[Customer] = []
    for customer in possible:
        same_name = candidate_name and candidate_name == customer.customer_name.casefold()
        same_email = candidate_email and candidate_email == (customer.email or "").casefold()
        same_gstin = candidate_gstin and candidate_gstin == (customer.gstin or "").upper()
        existing_phone = re.sub(r"\D", "", customer.phone or "")
        same_phone = candidate_phone and candidate_phone == existing_phone
        if same_name or same_email or same_gstin or same_phone:
            matches.append(customer)
    return matches


def create_customer(
    session: Session,
    payload: dict,
    *,
    actor_context: ActorContext,
    allow_potential_duplicate: bool = False,
) -> Customer:
    require_permission(actor_context, "customers.manage", db_session=session)
    cleaned = _validate_payload(payload)
    if not allow_potential_duplicate:
        duplicates = find_potential_customer_duplicates(
            session, cleaned, actor_context=actor_context
        )
        if duplicates:
            raise CustomerDuplicateWarning(duplicates)
    code = clean_string(payload.get("customer_code")) or generate_next_customer_code(
        session, actor_context=actor_context
    )
    if session.scalar(select(Customer.id).where(Customer.customer_code == code)):
        raise CustomerValidationError(f"Customer ID '{code}' already exists.")
    customer = Customer(customer_code=code, created_at=datetime.now(), updated_at=datetime.now(), **cleaned)
    session.add(customer)
    session.flush()
    return customer


def update_customer(
    session: Session,
    customer_id: int,
    payload: dict,
    *,
    actor_context: ActorContext,
    allow_potential_duplicate: bool = False,
) -> Customer:
    require_permission(actor_context, "customers.manage", db_session=session)
    customer = session.get(Customer, customer_id)
    if not customer:
        raise CustomerNotFoundError(f"Customer #{customer_id} was not found.")
    cleaned = _validate_payload(payload)
    if not allow_potential_duplicate:
        duplicates = find_potential_customer_duplicates(
            session, cleaned, customer_id, actor_context=actor_context
        )
        if duplicates:
            raise CustomerDuplicateWarning(duplicates)
    for key, value in cleaned.items():
        if key != "created_by" or value is not None:
            setattr(customer, key, value)
    customer.updated_at = datetime.now()
    session.flush()
    return customer


def get_customer_by_id(
    session: Session, customer_id: int, *, actor_context: ActorContext
) -> Customer | None:
    require_permission(actor_context, "customers.view", db_session=session)
    return session.get(Customer, customer_id)


def get_customer_by_code(
    session: Session, code: str, *, actor_context: ActorContext
) -> Customer | None:
    require_permission(actor_context, "customers.view", db_session=session)
    return session.scalar(select(Customer).where(Customer.customer_code == code.strip()))


def list_customers(
    session: Session,
    search: str = "",
    customer_type: str = "ALL",
    status_filter: str = "ALL",
    sort_by: str = "name_asc",
    limit: int | None = None,
    offset: int = 0,
    *,
    actor_context: ActorContext,
) -> list[Customer]:
    require_permission(actor_context, "customers.view", db_session=session)
    stmt = select(Customer)
    search = search.strip()
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(
            Customer.customer_code.ilike(pattern),
            Customer.customer_name.ilike(pattern),
            Customer.phone.ilike(pattern),
            Customer.alternate_phone.ilike(pattern),
            Customer.email.ilike(pattern),
            Customer.gstin.ilike(pattern),
            Customer.pan.ilike(pattern),
        ))
    if customer_type and customer_type != "ALL":
        stmt = stmt.where(Customer.customer_type == customer_type)
    status = status_filter.upper()
    if status == "ACTIVE":
        stmt = stmt.where(Customer.is_active.is_(True))
    elif status == "INACTIVE":
        stmt = stmt.where(Customer.is_active.is_(False))
    sort_columns = {
        "name_asc": (Customer.customer_name.asc(), Customer.id.asc()),
        "name_desc": (Customer.customer_name.desc(), Customer.id.desc()),
        "date_desc": (Customer.created_at.desc(), Customer.id.desc()),
        "date_asc": (Customer.created_at.asc(), Customer.id.asc()),
        "code_asc": (Customer.customer_code.asc(),),
    }
    stmt = stmt.order_by(*sort_columns.get(sort_by, sort_columns["name_asc"]))
    if limit is not None:
        if limit < 0:
            raise CustomerValidationError("Page size cannot be negative.")
        stmt = stmt.limit(limit)
    if offset < 0:
        raise CustomerValidationError("Page offset cannot be negative.")
    if offset:
        stmt = stmt.offset(offset)
    return list(session.scalars(stmt).all())


def set_customer_active(
    session: Session,
    customer_id: int,
    is_active: bool,
    *,
    actor_context: ActorContext,
) -> Customer:
    require_permission(actor_context, "customers.manage", db_session=session)
    customer = session.get(Customer, customer_id)
    if not customer:
        raise CustomerNotFoundError(f"Customer #{customer_id} was not found.")
    customer.is_active = bool(is_active)
    customer.updated_at = datetime.now()
    session.flush()
    return customer


def deactivate_customer(
    session: Session, customer_id: int, *, actor_context: ActorContext
) -> Customer:
    require_permission(actor_context, "customers.manage", db_session=session)
    return set_customer_active(
        session, customer_id, False, actor_context=actor_context
    )


def reactivate_customer(
    session: Session, customer_id: int, *, actor_context: ActorContext
) -> Customer:
    require_permission(actor_context, "customers.manage", db_session=session)
    return set_customer_active(
        session, customer_id, True, actor_context=actor_context
    )


def get_customer_metrics(session: Session, *, actor_context: ActorContext) -> dict:
    require_permission(actor_context, "customers.view", db_session=session)
    total = session.scalar(select(func.count(Customer.id))) or 0
    active = session.scalar(select(func.count(Customer.id)).where(Customer.is_active.is_(True))) or 0
    inactive = total - active
    opening_balance = session.scalar(
        select(func.coalesce(func.sum(Customer.opening_balance), 0))
    ) or ZERO
    return {
        "total": total,
        "active": active,
        "inactive": inactive,
        "total_opening_balance": Decimal(str(opening_balance)).quantize(CENT),
        "total_outstanding_receivables": None,
        "credit_limit_exceeded_count": None,
        "overdue_customer_count": None,
        "financial_data_available": False,
    }


def evaluate_credit_limit(
    customer: Customer,
    proposed_sale_amount: object,
    *,
    current_outstanding: object | None = None,
) -> dict:
    """Returns a warning decision only; it deliberately does not enforce/block sales."""
    proposed = _money(proposed_sale_amount, "Proposed sale amount")
    assert proposed is not None
    if current_outstanding is None:
        outstanding = customer.opening_balance or ZERO
        balance_basis = "opening_balance_only"
    else:
        outstanding = _money(current_outstanding, "Current outstanding balance")
        assert outstanding is not None
        balance_basis = "provided_authoritative_balance"
    limit = customer.credit_limit
    projected = outstanding + proposed
    exceeded = limit is not None and projected > limit
    return {
        "customer_id": customer.id,
        "credit_limit": limit,
        "current_outstanding": outstanding,
        "proposed_sale_amount": proposed,
        "projected_balance": projected,
        "would_exceed_limit": exceeded,
        "warning": exceeded,
        "hard_restriction": False,
        "balance_basis": balance_basis,
    }
