from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import func, or_, select, distinct
from sqlalchemy.orm import Session, selectinload
from database.models import Purchase, PurchaseItem, Supplier
from services.security_service import ActorContext, require_permission
from utils.validators import (
    clean_string,
    to_decimal,
    validate_positive_decimal,
)

class PurchaseValidationError(ValueError):
    """Raised when purchase validation fails."""
    pass

class PurchaseNotFoundError(ValueError):
    """Raised when purchase cannot be found."""
    pass


APPLIANCE_CATEGORIES = [
    "Refrigerators",
    "Washing Machines",
    "Air Conditioners",
    "Microwaves",
    "Televisions & Monitors",
    "Computers & Laptops",
    "Printers & Scanners",
    "Small Home Appliances",
    "Mixed E-Waste",
    "Other",
]

PAYMENT_METHODS = [
    "Cash",
    "Bank Transfer / NEFT",
    "UPI",
    "Cheque",
    "Credit",
]


def generate_next_purchase_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    """Generates the next sequential purchase code in the format PUR-000001."""
    require_permission(actor_context, "purchases.post", db_session=session)
    codes = session.scalars(select(Purchase.purchase_code)).all()
    max_num = 0
    for code in codes:
        if code and code.startswith("PUR-"):
            num_part = code[4:]
            if num_part.isdigit():
                max_num = max(max_num, int(num_part))
    return f"PUR-{max_num + 1:06d}"


def calculate_line_total(quantity: int, weight: Decimal | None, unit: str, rate: Decimal) -> Decimal:
    """Calculates the total for a single appliance line item based on unit type and rate."""
    q_dec = Decimal(str(quantity))
    if unit == "kg":
        w = weight if (weight is not None and weight > Decimal("0.00")) else q_dec
        line = w * rate
    else:
        line = q_dec * rate
    return line.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_payment_status(total_amount: Decimal, amount_paid: Decimal) -> str:
    """Determines payment status from total cost and amount paid."""
    if amount_paid <= Decimal("0.00"):
        return "Unpaid"
    elif amount_paid >= total_amount:
        return "Paid"
    else:
        return "Partially Paid"


def create_purchase(
    session: Session,
    header_data: dict,
    items_data: list[dict],
    *,
    actor_context: ActorContext,
) -> Purchase:
    """Creates a new purchase record with line items, validating suppliers, costs, and constraints."""
    require_permission(actor_context, "purchases.post", db_session=session)

    # 1. Supplier Validation
    supplier_id = header_data.get("supplier_id")
    if not supplier_id:
        raise PurchaseValidationError("A purchase must be associated with a valid supplier.")

    from services.supplier_service import get_supplier_by_id

    supplier = get_supplier_by_id(
        session, supplier_id, actor_context=actor_context
    )
    if not supplier:
        raise PurchaseValidationError(f"Supplier with ID #{supplier_id} does not exist.")
    if not supplier.is_active:
        raise PurchaseValidationError(
            f"Supplier '{supplier.business_name}' is currently inactive. "
            "Only active suppliers can be selected for new purchases."
        )

    # 2. Line Items Validation
    if not items_data or len(items_data) == 0:
        raise PurchaseValidationError("A purchase must contain at least one appliance item.")

    validated_items: list[PurchaseItem] = []
    subtotal = Decimal("0.00")

    for idx, item in enumerate(items_data, start=1):
        category = clean_string(item.get("appliance_category"))
        if not category:
            raise PurchaseValidationError(f"Item #{idx}: Appliance category is required.")

        # Quantity
        try:
            qty = int(item.get("quantity", 0))
            if qty <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise PurchaseValidationError(f"Item #{idx} ({category}): Quantity must be a positive integer (> 0).")

        # Unit
        unit = clean_string(item.get("unit")) or "pieces"
        if unit not in ["pieces", "kg"]:
            unit = "pieces"

        # Weight
        raw_weight = item.get("weight")
        weight = to_decimal(raw_weight, "0.00")
        if weight < Decimal("0.00"):
            raise PurchaseValidationError(f"Item #{idx} ({category}): Weight cannot be negative.")

        # Rate
        ok_rate, rate, err = validate_positive_decimal(item.get("rate"), f"Item #{idx} rate", allow_zero=True)
        if not ok_rate:
            raise PurchaseValidationError(err)

        # Line total calculation (backend authority)
        line_total = calculate_line_total(qty, weight, unit, rate)
        subtotal += line_total

        validated_items.append(
            PurchaseItem(
                appliance_category=category,
                brand=clean_string(item.get("brand")),
                model_number=clean_string(item.get("model_number")),
                quantity=qty,
                weight=weight,
                unit=unit,
                rate=rate,
                line_total=line_total,
            )
        )

    subtotal = subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # 3. Additional Charges Validation
    ok_trans, trans_charges, err = validate_positive_decimal(header_data.get("transportation_charges", 0), "Transportation charges")
    if not ok_trans: raise PurchaseValidationError(err)

    ok_load, load_charges, err = validate_positive_decimal(header_data.get("loading_charges", 0), "Loading charges")
    if not ok_load: raise PurchaseValidationError(err)

    ok_unload, unload_charges, err = validate_positive_decimal(header_data.get("unloading_charges", 0), "Unloading charges")
    if not ok_unload: raise PurchaseValidationError(err)

    ok_other, other_charges, err = validate_positive_decimal(header_data.get("other_charges", 0), "Other charges")
    if not ok_other: raise PurchaseValidationError(err)

    # 4. Total and Balances
    total_amount = (subtotal + trans_charges + load_charges + unload_charges + other_charges).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    raw_paid = header_data.get("amount_paid", 0)
    ok_paid, amount_paid, err = validate_positive_decimal(raw_paid, "Amount paid", allow_zero=True)
    if not ok_paid:
        raise PurchaseValidationError(err)

    if amount_paid > total_amount:
        raise PurchaseValidationError(
            f"Amount paid (₹ {amount_paid}) cannot exceed total purchase cost (₹ {total_amount})."
        )

    balance_amount = (total_amount - amount_paid).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    payment_status = calculate_payment_status(total_amount, amount_paid)

    # 5. Purchase Code
    purchase_code = clean_string(header_data.get("purchase_code"))
    if not purchase_code:
        purchase_code = generate_next_purchase_code(
            session, actor_context=actor_context
        )
    else:
        existing = session.scalar(select(Purchase).where(Purchase.purchase_code == purchase_code))
        if existing:
            raise PurchaseValidationError(f"Purchase ID '{purchase_code}' already exists.")

    # 6. Purchase Date
    p_date = header_data.get("purchase_date")
    if isinstance(p_date, str):
        try:
            p_date = datetime.strptime(p_date, "%Y-%m-%d").date()
        except ValueError:
            p_date = date.today()
    elif isinstance(p_date, datetime):
        p_date = p_date.date()
    elif not isinstance(p_date, date):
        p_date = date.today()

    payment_method = clean_string(header_data.get("payment_method")) or "Cash"
    remarks = clean_string(header_data.get("remarks"))

    purchase = Purchase(
        purchase_code=purchase_code,
        supplier_id=supplier.id,
        purchase_date=p_date,
        subtotal=subtotal,
        transportation_charges=trans_charges,
        loading_charges=load_charges,
        unloading_charges=unload_charges,
        other_charges=other_charges,
        total_amount=total_amount,
        amount_paid=amount_paid,
        balance_amount=balance_amount,
        payment_method=payment_method,
        payment_status=payment_status,
        remarks=remarks,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    for item in validated_items:
        purchase.items.append(item)

    session.add(purchase)
    session.flush()
    from services.accounting_posting_service import capture_source_event

    capture_source_event(
        session, "Purchase", purchase.id, "CREATED",
        actor_context=actor_context,
    )
    return purchase


def get_purchase_by_id(
    session: Session, purchase_id: int, *, actor_context: ActorContext
) -> Purchase | None:
    require_permission(actor_context, "purchases.view", db_session=session)
    stmt = (
        select(Purchase)
        .options(selectinload(Purchase.supplier), selectinload(Purchase.items))
        .where(Purchase.id == purchase_id)
    )
    return session.scalar(stmt)


def get_purchase_by_code(
    session: Session, purchase_code: str, *, actor_context: ActorContext
) -> Purchase | None:
    require_permission(actor_context, "purchases.view", db_session=session)
    stmt = (
        select(Purchase)
        .options(selectinload(Purchase.supplier), selectinload(Purchase.items))
        .where(Purchase.purchase_code == purchase_code.strip())
    )
    return session.scalar(stmt)


def list_purchases(
    session: Session,
    search: str = "",
    start_date: date | None = None,
    end_date: date | None = None,
    category_filter: str = "ALL",
    status_filter: str = "ALL",
    sort_by: str = "date_desc",
    *,
    actor_context: ActorContext,
) -> list[Purchase]:
    require_permission(actor_context, "purchases.view", db_session=session)
    stmt = select(Purchase).join(Supplier).options(
        selectinload(Purchase.supplier), selectinload(Purchase.items)
    )

    # Search filter (Purchase ID or Supplier name)
    search = search.strip()
    if search:
        search_pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Purchase.purchase_code.ilike(search_pattern),
                Supplier.business_name.ilike(search_pattern),
                Supplier.supplier_code.ilike(search_pattern),
            )
        )

    # Date range filters
    if start_date:
        stmt = stmt.where(Purchase.purchase_date >= start_date)
    if end_date:
        stmt = stmt.where(Purchase.purchase_date <= end_date)

    # Status filter
    status = status_filter.upper()
    if status in ["UNPAID", "PAID", "PARTIALLY PAID"]:
        stmt = stmt.where(Purchase.payment_status.ilike(status_filter))

    # Appliance Category filter
    if category_filter and category_filter != "ALL":
        stmt = stmt.where(
            Purchase.id.in_(
                select(PurchaseItem.purchase_id).where(
                    PurchaseItem.appliance_category == category_filter
                )
            )
        )

    # Sorting
    if sort_by == "date_desc":
        stmt = stmt.order_by(Purchase.purchase_date.desc(), Purchase.id.desc())
    elif sort_by == "date_asc":
        stmt = stmt.order_by(Purchase.purchase_date.asc(), Purchase.id.asc())
    elif sort_by == "amount_desc":
        stmt = stmt.order_by(Purchase.total_amount.desc())
    elif sort_by == "amount_asc":
        stmt = stmt.order_by(Purchase.total_amount.asc())
    elif sort_by == "code_asc":
        stmt = stmt.order_by(Purchase.purchase_code.asc())
    else:
        stmt = stmt.order_by(Purchase.id.desc())

    return list(session.scalars(stmt).all())


def get_purchase_metrics(
    session: Session, *, actor_context: ActorContext
) -> dict:
    """Calculates summary KPIs for the Purchase Dashboard."""
    require_permission(actor_context, "purchases.view", db_session=session)
    today = date.today()
    start_of_month = date(today.year, today.month, 1)

    total_purchases = session.scalar(select(func.count(Purchase.id))) or 0

    purchases_today = session.scalar(
        select(func.count(Purchase.id)).where(Purchase.purchase_date == today)
    ) or 0

    purchases_this_month = session.scalar(
        select(func.count(Purchase.id)).where(Purchase.purchase_date >= start_of_month)
    ) or 0

    total_expenditure = session.scalar(
        select(func.coalesce(func.sum(Purchase.total_amount), Decimal("0.00")))
    ) or Decimal("0.00")

    total_paid = session.scalar(
        select(func.coalesce(func.sum(Purchase.amount_paid), Decimal("0.00")))
    ) or Decimal("0.00")

    total_outstanding = session.scalar(
        select(func.coalesce(func.sum(Purchase.balance_amount), Decimal("0.00")))
    ) or Decimal("0.00")

    return {
        "total_purchases": total_purchases,
        "purchases_today": purchases_today,
        "purchases_this_month": purchases_this_month,
        "total_expenditure": to_decimal(total_expenditure),
        "total_paid": to_decimal(total_paid),
        "total_outstanding": to_decimal(total_outstanding),
    }


def get_recent_purchases(
    session: Session, limit: int = 5, *, actor_context: ActorContext
) -> list[Purchase]:
    require_permission(actor_context, "purchases.view", db_session=session)
    stmt = (
        select(Purchase)
        .options(selectinload(Purchase.supplier), selectinload(Purchase.items))
        .order_by(Purchase.purchase_date.desc(), Purchase.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def get_supplier_purchase_ledger_balance(
    session: Session, supplier_id: int, *, actor_context: ActorContext
) -> tuple[Decimal, list[Purchase]]:
    """Returns the total outstanding balance (opening balance + unpaid purchases) and purchase list for a supplier."""
    require_permission(actor_context, "purchases.view", db_session=session)
    supplier = session.get(Supplier, supplier_id)
    if not supplier:
        return Decimal("0.00"), []

    purchases = list(
        session.scalars(
            select(Purchase)
            .options(selectinload(Purchase.items))
            .where(Purchase.supplier_id == supplier_id)
            .order_by(Purchase.purchase_date.desc(), Purchase.id.desc())
        ).all()
    )

    unpaid_purchases_total = sum((p.balance_amount for p in purchases), Decimal("0.00"))
    opening_bal = to_decimal(supplier.opening_balance)
    outstanding_total = (opening_bal + unpaid_purchases_total).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    return outstanding_total, purchases
