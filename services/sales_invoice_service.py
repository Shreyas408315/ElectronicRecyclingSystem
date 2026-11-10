from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, object_session, selectinload

from database.models import (
    ApplicationSetting,
    Customer,
    SalesInvoice,
    SalesInvoiceHistory,
    SalesInvoiceItem,
    SalesOrder,
    SalesOrderFulfilment,
    SalesOrderFulfilmentItem,
    SalesOrderItem,
)
from services.security_service import ActorContext, require_permission
from utils.validators import clean_string, validate_gstin


CENT = Decimal("0.01")
QTY = Decimal("0.01")
RATE_QUANTUM = Decimal("0.001")
ZERO = Decimal("0.00")
TAX_TREATMENTS = ("NO_GST", "INTRA_STATE_GST", "INTER_STATE_GST")
INVOICED_STATUSES = ("Issued", "Partially Paid", "Paid", "Overdue")
INVOICE_STATUSES = ("Draft", "Issued", "Partially Paid", "Paid", "Overdue", "Cancelled", "Void")


class SalesInvoiceValidationError(ValueError):
    """Raised when an invoice violates a billing or tax rule."""


class SalesInvoiceNotFoundError(ValueError):
    """Raised when an invoice cannot be found."""


def _require_invoice_permission(
    session: Session, actor_context: ActorContext, permission: str
) -> None:
    require_permission(actor_context, permission, db_session=session)


def _money(value: object, label: str, *, nonnegative: bool = True) -> Decimal:
    try:
        parsed = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
        if not parsed.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SalesInvoiceValidationError(f"{label} must be a valid decimal amount.") from exc
    if nonnegative and parsed < ZERO:
        raise SalesInvoiceValidationError(f"{label} cannot be negative.")
    return parsed


def _quantity(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value)).quantize(QTY, rounding=ROUND_HALF_UP)
        if not parsed.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SalesInvoiceValidationError("Invoice quantity must be a valid decimal amount.") from exc
    if parsed <= ZERO:
        raise SalesInvoiceValidationError("Invoice quantity must be greater than zero.")
    return parsed


def _rate(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value)).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
        if not parsed.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SalesInvoiceValidationError("Tax rate must be a valid decimal percentage.") from exc
    if parsed < ZERO or parsed > Decimal("100"):
        raise SalesInvoiceValidationError("Tax rate must be between 0 and 100 percent.")
    return parsed


def get_invoice_factory_settings(
    session: Session, *, actor_context: ActorContext
) -> dict[str, str | None]:
    _require_invoice_permission(session, actor_context, "invoices.view")
    keys = {
        "name": "factory_name",
        "address": "factory_address",
        "gstin": "factory_gstin",
        "state": "factory_state",
    }
    return {
        field: (
            setting.value.strip() or None
            if (setting := session.get(ApplicationSetting, key))
            else None
        )
        for field, key in keys.items()
    }


def update_invoice_factory_settings(
    session: Session,
    values: dict,
    *,
    actor_context: ActorContext,
) -> dict[str, str | None]:
    _require_invoice_permission(session, actor_context, "settings.manage")
    _require_invoice_permission(session, actor_context, "invoices.view")
    name = clean_string(values.get("name"))
    address = clean_string(values.get("address"))
    gstin = clean_string(values.get("gstin"))
    state = clean_string(values.get("state"))
    valid, error = validate_gstin(gstin)
    if not valid:
        raise SalesInvoiceValidationError(error)
    from services.settings_service import SettingsValidationError, update_settings

    try:
        update_settings(
            session,
            {
                "factory_name": name,
                "factory_address": address,
                "factory_gstin": gstin.upper() if gstin else None,
                "factory_state": state,
            },
            actor_context=actor_context,
            reason="Updated from invoice GST settings",
        )
    except SettingsValidationError as exc:
        raise SalesInvoiceValidationError(str(exc)) from exc
    return get_invoice_factory_settings(session, actor_context=actor_context)


def calculate_invoice_due_date(customer: Customer, invoice_date: date) -> date:
    terms = (customer.payment_terms or "Immediate").strip().casefold()
    if terms == "custom":
        days = customer.custom_payment_days or 0
    elif terms == "immediate":
        days = 0
    else:
        digits = "".join(character for character in terms if character.isdigit())
        days = int(digits) if digits else 0
    return invoice_date + timedelta(days=days)


def _party_snapshot(
    invoice: SalesInvoice,
    customer: Customer,
    session: Session,
    actor_context: ActorContext,
) -> None:
    settings = get_invoice_factory_settings(session, actor_context=actor_context)
    invoice.factory_name_snapshot = settings["name"]
    invoice.factory_address_snapshot = settings["address"]
    invoice.factory_gstin_snapshot = settings["gstin"]
    invoice.factory_state_snapshot = settings["state"]
    invoice.customer_name_snapshot = customer.customer_name
    invoice.customer_code_snapshot = customer.customer_code
    invoice.customer_contact_snapshot = customer.contact_person
    invoice.customer_phone_snapshot = customer.phone
    invoice.customer_gstin_snapshot = customer.gstin
    invoice.billing_address_snapshot = customer.billing_address
    invoice.shipping_address_snapshot = customer.shipping_address
    invoice.customer_state_snapshot = customer.state
    invoice.customer_pin_snapshot = customer.pin_code
    invoice.customer_country_snapshot = customer.country


def _validate_tax_treatment(
    treatment: str,
    customer: Customer,
    session: Session,
    actor_context: ActorContext,
) -> str:
    treatment = clean_string(treatment) or "NO_GST"
    if treatment not in TAX_TREATMENTS:
        raise SalesInvoiceValidationError(f"Tax treatment must be one of {', '.join(TAX_TREATMENTS)}.")
    if treatment == "NO_GST":
        return treatment
    settings = get_invoice_factory_settings(session, actor_context=actor_context)
    if not settings["gstin"]:
        raise SalesInvoiceValidationError("Configure the factory GSTIN before applying GST to an invoice.")
    valid, error = validate_gstin(settings["gstin"])
    if not valid:
        raise SalesInvoiceValidationError(f"Configured factory GSTIN is invalid: {error}")
    factory_state = settings["state"]
    customer_state = clean_string(customer.state)
    if not factory_state or not customer_state:
        raise SalesInvoiceValidationError(
            "Factory state and customer state are required to determine intra-state or inter-state GST."
        )
    if (customer.country or "India").casefold() != "india":
        raise SalesInvoiceValidationError("GST treatment is configured only for domestic Indian billing.")
    same_state = factory_state.casefold() == customer_state.casefold()
    if treatment == "INTRA_STATE_GST" and not same_state:
        raise SalesInvoiceValidationError("Intra-state GST requires matching configured factory and customer states.")
    if treatment == "INTER_STATE_GST" and same_state:
        raise SalesInvoiceValidationError("Inter-state GST requires different configured factory and customer states.")
    return treatment


def _tax_parts(taxable: Decimal, rate: Decimal, treatment: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if treatment == "NO_GST" or rate == ZERO:
        return ZERO, ZERO, ZERO, ZERO
    if treatment == "INTER_STATE_GST":
        igst = (taxable * rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        return ZERO, ZERO, igst, igst
    half_rate = rate / Decimal("2")
    cgst = (taxable * half_rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    sgst = (taxable * half_rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    return cgst, sgst, ZERO, cgst + sgst


def calculate_invoice_item(
    quantity: object,
    unit_price: object,
    discount: object,
    tax_rate: object,
    treatment: str,
) -> dict[str, Decimal]:
    qty = _quantity(quantity)
    price = _money(unit_price, "Unit price")
    line_discount = _money(discount, "Line discount")
    rate = _rate(tax_rate)
    subtotal = (qty * price).quantize(CENT, rounding=ROUND_HALF_UP)
    if line_discount > subtotal:
        raise SalesInvoiceValidationError("Line discount cannot exceed the line subtotal.")
    taxable = subtotal - line_discount
    cgst, sgst, igst, tax = _tax_parts(taxable, rate, treatment)
    return {
        "quantity": qty,
        "unit_price": price,
        "discount": line_discount,
        "tax_rate": rate,
        "taxable_amount": taxable,
        "cgst_amount": cgst,
        "sgst_amount": sgst,
        "igst_amount": igst,
        "tax_amount": tax,
        "line_total": taxable + tax,
        "line_subtotal": subtotal,
    }


def _recalculate_invoice(invoice: SalesInvoice) -> None:
    invoice.subtotal = sum((line.quantity * line.unit_price for line in invoice.items), ZERO).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    invoice.discount_amount = sum((line.discount for line in invoice.items), ZERO).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    invoice.additional_charges = _money(invoice.additional_charges or ZERO, "Additional charges")
    invoice.taxable_amount = sum((line.taxable_amount for line in invoice.items), ZERO).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    invoice.cgst_amount = sum((line.cgst_amount for line in invoice.items), ZERO).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    invoice.sgst_amount = sum((line.sgst_amount for line in invoice.items), ZERO).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    invoice.igst_amount = sum((line.igst_amount for line in invoice.items), ZERO).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    invoice.tax_amount = (invoice.cgst_amount + invoice.sgst_amount + invoice.igst_amount).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    invoice.grand_total = invoice.taxable_amount + invoice.tax_amount + invoice.additional_charges
    invoice.amount_paid = None
    invoice.balance_due = None


def _active_invoiced_totals(
    session: Session, order_id: int
) -> tuple[dict[int, tuple[Decimal, Decimal]], Decimal, Decimal, Decimal]:
    rows = session.execute(
        select(
            SalesInvoiceItem.sales_order_item_id,
            func.sum(SalesInvoiceItem.quantity),
            func.sum(
                SalesInvoiceItem.discount
                - func.coalesce(SalesInvoiceItem.order_discount_amount, ZERO)
            ),
        )
        .join(SalesInvoice, SalesInvoice.id == SalesInvoiceItem.invoice_id)
        .where(SalesInvoice.sales_order_id == order_id)
        .where(SalesInvoice.status.in_(INVOICED_STATUSES))
        .group_by(SalesInvoiceItem.sales_order_item_id)
    ).all()
    line_totals = {
        int(item_id): (Decimal(str(quantity or ZERO)), Decimal(str(discount or ZERO)))
        for item_id, quantity, discount in rows
    }
    aggregate = session.execute(
        select(
            func.coalesce(func.sum(SalesInvoiceItem.quantity * SalesInvoiceItem.unit_price), ZERO),
            func.coalesce(func.sum(SalesInvoiceItem.order_discount_amount), ZERO),
            func.coalesce(func.sum(SalesInvoice.additional_charges), ZERO),
        )
        .join(SalesInvoice, SalesInvoice.id == SalesInvoiceItem.invoice_id)
        .where(SalesInvoice.sales_order_id == order_id)
        .where(SalesInvoice.status.in_(INVOICED_STATUSES))
    ).one()
    return (
        line_totals,
        Decimal(str(aggregate[0] or ZERO)),
        Decimal(str(aggregate[1] or ZERO)),
        Decimal(str(aggregate[2] or ZERO)),
    )


def _fulfilled_sources(session: Session, order_id: int):
    return session.scalars(
        select(SalesOrderFulfilmentItem)
        .join(SalesOrderFulfilment, SalesOrderFulfilment.id == SalesOrderFulfilmentItem.fulfilment_id)
        .where(SalesOrderFulfilment.sales_order_id == order_id)
        .order_by(SalesOrderFulfilment.fulfilment_date, SalesOrderFulfilmentItem.id)
        .options(
            selectinload(SalesOrderFulfilmentItem.sales_order_item),
            selectinload(SalesOrderFulfilmentItem.fulfilment),
            selectinload(SalesOrderFulfilmentItem.inventory_item),
        )
    ).all()


def _append_history(
    session: Session,
    invoice: SalesInvoice,
    event_type: str,
    to_status: str | None,
    reason: str | None = None,
    responsible_user: str | None = None,
    from_status: str | None = None,
) -> None:
    session.add(SalesInvoiceHistory(
        invoice_id=invoice.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        reason=clean_string(reason),
        responsible_user=clean_string(responsible_user),
        created_at=datetime.now(),
    ))


def _build_invoice_items(
    session: Session,
    invoice: SalesInvoice,
    source_quantities: dict[int, object] | None = None,
) -> None:
    sources = _fulfilled_sources(session, invoice.sales_order_id)
    active_totals, prior_gross, prior_order_discount, prior_charges = _active_invoiced_totals(
        session, invoice.sales_order_id
    )
    order = invoice.sales_order
    allocations: list[tuple[SalesOrderFulfilmentItem, SalesOrderItem, Decimal]] = []
    seen_sources: set[int] = set()
    for source in sources:
        order_line = source.sales_order_item
        available = max(ZERO, (source.quantity or ZERO) - (source.invoiced_quantity or ZERO))
        if source_quantities is None:
            requested = available
        else:
            raw = source_quantities.get(source.id, ZERO)
            requested = _money(raw, "Invoice quantity")
        if requested > available:
            raise SalesInvoiceValidationError(
                f"Fulfilment {source.fulfilment_id} has {available} uninvoiced units available."
            )
        if requested > ZERO:
            allocations.append((source, order_line, _quantity(requested)))
            seen_sources.add(source.id)
    if source_quantities is not None:
        unknown = set(source_quantities) - seen_sources
        for source_id in source_quantities:
            if _money(source_quantities[source_id], "Invoice quantity") > ZERO and source_id in unknown:
                raise SalesInvoiceValidationError(
                    f"Fulfilment line {source_id} is not available for this sales order."
                )
    if not allocations:
        raise SalesInvoiceValidationError("This order has no fulfilled quantities available to invoice.")

    current_gross = sum(
        (quantity * order_line.unit_selling_price for _, order_line, quantity in allocations), ZERO
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    remaining_gross = max(ZERO, (order.subtotal or ZERO) - prior_gross)
    remaining_order_discount = max(ZERO, (order.discount_amount or ZERO) - prior_order_discount)
    allocated_order_discount = (
        remaining_order_discount
        if current_gross >= remaining_gross
        else (remaining_order_discount * current_gross / remaining_gross).quantize(
            CENT, rounding=ROUND_HALF_UP
        ) if remaining_gross > ZERO else ZERO
    )
    remaining_charges = max(ZERO, (order.additional_charges or ZERO) - prior_charges)
    allocated_charges = (
        remaining_charges
        if current_gross >= remaining_gross
        else (remaining_charges * current_gross / remaining_gross).quantize(
            CENT, rounding=ROUND_HALF_UP
        ) if remaining_gross > ZERO else ZERO
    )
    gross_by_source = {
        source.id: (quantity * order_line.unit_selling_price).quantize(CENT, rounding=ROUND_HALF_UP)
        for source, order_line, quantity in allocations
    }
    pending_discount = allocated_order_discount
    pending_charges = allocated_charges
    for allocation_index, (source, order_line, quantity) in enumerate(allocations):
        already_qty, already_discount = active_totals.get(order_line.id, (ZERO, ZERO))
        remaining_qty = max(ZERO, (order_line.quantity_ordered or ZERO) - already_qty)
        remaining_discount = max(ZERO, (order_line.discount or ZERO) - already_discount)
        discount = (
            remaining_discount
            if quantity >= remaining_qty
            else (remaining_discount * quantity / remaining_qty).quantize(CENT, rounding=ROUND_HALF_UP)
        )
        discount = min(discount, remaining_discount)
        gross = gross_by_source[source.id]
        if allocation_index == len(allocations) - 1:
            order_discount = pending_discount
            charges = pending_charges
        elif current_gross > ZERO:
            order_discount = min(
                pending_discount,
                (allocated_order_discount * gross / current_gross).quantize(
                    CENT, rounding=ROUND_HALF_UP
                ),
            )
            charges = min(
                pending_charges,
                (allocated_charges * gross / current_gross).quantize(
                    CENT, rounding=ROUND_HALF_UP
                ),
            )
        else:
            order_discount = ZERO
            charges = ZERO
        pending_discount -= order_discount
        pending_charges -= charges
        total_discount = discount + order_discount
        treatment = invoice.tax_treatment
        amounts = calculate_invoice_item(
            quantity, order_line.unit_selling_price, total_discount, order_line.tax_rate, treatment
        )
        session.add(SalesInvoiceItem(
            invoice=invoice,
            invoice_id=invoice.id,
            inventory_item_id=order_line.inventory_item_id,
            sales_order_item_id=order_line.id,
            fulfilment_item_id=source.id,
            product_name_snapshot=order_line.product_name_snapshot,
            part_code_snapshot=order_line.part_code_snapshot,
            description=order_line.product_name_snapshot,
            quantity=amounts["quantity"],
            unit=order_line.unit,
            unit_price=amounts["unit_price"],
            discount=amounts["discount"],
            order_discount_amount=order_discount,
            tax_rate=amounts["tax_rate"],
            taxable_amount=amounts["taxable_amount"],
            cgst_amount=amounts["cgst_amount"],
            sgst_amount=amounts["sgst_amount"],
            igst_amount=amounts["igst_amount"],
            tax_amount=amounts["tax_amount"],
            line_total=amounts["line_total"],
        ))
        active_totals[order_line.id] = (already_qty + quantity, already_discount + discount)
        invoice.additional_charges += charges
    session.flush()
    _recalculate_invoice(invoice)


def create_invoice_from_fulfilled_order(
    session: Session,
    sales_order_id: int,
    *,
    tax_treatment: str = "NO_GST",
    source_quantities: dict[int, object] | None = None,
    invoice_date: date | None = None,
    due_date: date | None = None,
    notes: str | None = None,
    terms_and_conditions: str | None = None,
    responsible_user: str | None = None,
    actor_context: ActorContext,
) -> SalesInvoice:
    _require_invoice_permission(session, actor_context, "invoices.manage")
    _require_invoice_permission(session, actor_context, "invoices.view")
    require_permission(actor_context, "sales.view", db_session=session)
    require_permission(actor_context, "customers.view", db_session=session)
    order = session.scalar(
        select(SalesOrder)
        .options(selectinload(SalesOrder.customer))
        .where(SalesOrder.id == sales_order_id)
    )
    if not order:
        raise SalesInvoiceValidationError(f"Sales order #{sales_order_id} was not found.")
    customer = order.customer
    treatment = _validate_tax_treatment(
        tax_treatment, customer, session, actor_context
    )
    billed_date = invoice_date or date.today()
    if due_date is not None and due_date < billed_date:
        raise SalesInvoiceValidationError("Invoice due date cannot be before its invoice date.")
    with session.begin_nested():
        invoice = SalesInvoice(
            invoice_code=f"INV-PENDING-{uuid4().hex}",
            customer_id=customer.id,
            sales_order_id=order.id,
            invoice_date=billed_date,
            due_date=due_date or calculate_invoice_due_date(customer, billed_date),
            status="Draft",
            tax_treatment=treatment,
            customer_name_snapshot=customer.customer_name,
            customer_code_snapshot=customer.customer_code,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            responsible_user=clean_string(responsible_user),
            notes=clean_string(notes),
            terms_and_conditions=clean_string(terms_and_conditions),
        )
        _party_snapshot(invoice, customer, session, actor_context)
        session.add(invoice)
        session.flush()
        invoice.invoice_code = f"INV-{invoice.id:06d}"
        _build_invoice_items(session, invoice, source_quantities)
        _append_history(session, invoice, "DRAFT_CREATED", "Draft", "Generated from fulfilled sales-order quantities", responsible_user)
        session.flush()
    return invoice


def _get_invoice(session: Session, invoice_id: int) -> SalesInvoice | None:
    return session.scalar(
        select(SalesInvoice)
        .options(
            selectinload(SalesInvoice.customer),
            selectinload(SalesInvoice.sales_order),
            selectinload(SalesInvoice.items).selectinload(SalesInvoiceItem.fulfilment_item),
            selectinload(SalesInvoice.history),
        )
        .where(SalesInvoice.id == invoice_id)
    )


def get_sales_invoice_by_id(
    session: Session, invoice_id: int, *, actor_context: ActorContext
) -> SalesInvoice | None:
    _require_invoice_permission(session, actor_context, "invoices.view")
    return _get_invoice(session, invoice_id)


def list_sales_invoices(
    session: Session,
    *,
    search: str = "",
    customer_id: int | None = None,
    status: str = "ALL",
    invoice_start: date | None = None,
    invoice_end: date | None = None,
    due_start: date | None = None,
    due_end: date | None = None,
    actor_context: ActorContext,
) -> list[SalesInvoice]:
    _require_invoice_permission(session, actor_context, "invoices.view")
    stmt = select(SalesInvoice).join(Customer).options(
        selectinload(SalesInvoice.customer),
        selectinload(SalesInvoice.items),
    )
    search = search.strip()
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(
            SalesInvoice.invoice_code.ilike(pattern),
            Customer.customer_name.ilike(pattern),
            Customer.customer_code.ilike(pattern),
        ))
    if customer_id is not None:
        stmt = stmt.where(SalesInvoice.customer_id == customer_id)
    if status != "ALL":
        stmt = stmt.where(SalesInvoice.status == status)
    if invoice_start:
        stmt = stmt.where(SalesInvoice.invoice_date >= invoice_start)
    if invoice_end:
        stmt = stmt.where(SalesInvoice.invoice_date <= invoice_end)
    if due_start:
        stmt = stmt.where(SalesInvoice.due_date >= due_start)
    if due_end:
        stmt = stmt.where(SalesInvoice.due_date <= due_end)
    return list(session.scalars(stmt.order_by(SalesInvoice.invoice_date.desc(), SalesInvoice.id.desc())).unique().all())


def update_draft_invoice(
    session: Session,
    invoice_id: int,
    *,
    invoice_date: date | None = None,
    due_date: date | None = None,
    tax_treatment: str | None = None,
    notes: str | None = None,
    terms_and_conditions: str | None = None,
    responsible_user: str | None = None,
    source_quantities: dict[int, object] | None = None,
    actor_context: ActorContext,
) -> SalesInvoice:
    _require_invoice_permission(session, actor_context, "invoices.manage")
    require_permission(actor_context, "sales.view", db_session=session)
    require_permission(actor_context, "customers.view", db_session=session)
    invoice = _get_invoice(session, invoice_id)
    if not invoice:
        raise SalesInvoiceNotFoundError(f"Invoice #{invoice_id} was not found.")
    if invoice.status != "Draft":
        raise SalesInvoiceValidationError("Only draft invoices can be edited.")
    customer = invoice.customer
    next_date = invoice_date or invoice.invoice_date
    if due_date is not None and due_date < next_date:
        raise SalesInvoiceValidationError("Invoice due date cannot be before its invoice date.")
    treatment = _validate_tax_treatment(
        tax_treatment or invoice.tax_treatment, customer, session, actor_context
    )
    with session.begin_nested():
        invoice.invoice_date = next_date
        invoice.due_date = due_date if due_date is not None else calculate_invoice_due_date(customer, next_date)
        invoice.tax_treatment = treatment
        invoice.notes = clean_string(notes)
        invoice.terms_and_conditions = clean_string(terms_and_conditions)
        invoice.responsible_user = clean_string(responsible_user) or invoice.responsible_user
        if source_quantities is not None:
            for line in list(invoice.items):
                session.delete(line)
            invoice.items.clear()
            session.flush()
            invoice.additional_charges = ZERO
            _build_invoice_items(session, invoice, source_quantities)
        for line in invoice.items:
            amounts = calculate_invoice_item(
                line.quantity, line.unit_price, line.discount, line.tax_rate, treatment
            )
            for key, value in amounts.items():
                if hasattr(line, key):
                    setattr(line, key, value)
        _recalculate_invoice(invoice)
        invoice.updated_at = datetime.now()
        _party_snapshot(invoice, customer, session, actor_context)
        _append_history(session, invoice, "DRAFT_UPDATED", "Draft", "Draft invoice edited", responsible_user)
        session.flush()
    return invoice


def _validate_invoice_issue_configuration(
    invoice: SalesInvoice, session: Session, actor_context: ActorContext
) -> None:
    customer = invoice.customer
    _validate_tax_treatment(invoice.tax_treatment, customer, session, actor_context)
    settings = get_invoice_factory_settings(session, actor_context=actor_context)
    if not settings["name"] or not settings["address"]:
        raise SalesInvoiceValidationError(
            "Configure the factory name and registered address before issuing an invoice."
        )


def issue_sales_invoice(
    session: Session,
    invoice_id: int,
    responsible_user: str | None = None,
    *,
    actor_context: ActorContext,
) -> SalesInvoice:
    _require_invoice_permission(session, actor_context, "invoices.issue")
    _require_invoice_permission(session, actor_context, "invoices.view")
    require_permission(actor_context, "sales.view", db_session=session)
    invoice = _get_invoice(session, invoice_id)
    if not invoice:
        raise SalesInvoiceNotFoundError(f"Invoice #{invoice_id} was not found.")
    if invoice.status != "Draft":
        raise SalesInvoiceValidationError(f"Only draft invoices can be issued (current status: {invoice.status}).")
    if not invoice.items:
        raise SalesInvoiceValidationError("An invoice must contain fulfilled order items.")
    _validate_invoice_issue_configuration(invoice, session, actor_context)
    with session.begin_nested():
        _party_snapshot(invoice, invoice.customer, session, actor_context)
        for line in invoice.items:
            result = session.execute(
                update(SalesOrderFulfilmentItem)
                .where(SalesOrderFulfilmentItem.id == line.fulfilment_item_id)
                .where(
                    func.coalesce(SalesOrderFulfilmentItem.invoiced_quantity, ZERO)
                    + line.quantity
                    <= SalesOrderFulfilmentItem.quantity
                )
                .values(
                    invoiced_quantity=func.coalesce(SalesOrderFulfilmentItem.invoiced_quantity, ZERO)
                    + line.quantity
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise SalesInvoiceValidationError(
                    "Fulfilled quantity was already invoiced or changed; refresh the draft before issuing."
                )
            if line.fulfilment_item is not None:
                session.expire(line.fulfilment_item, ["invoiced_quantity"])
        previous_status = invoice.status
        invoice.status = "Issued"
        invoice.issued_at = datetime.now()
        invoice.updated_at = datetime.now()
        invoice.responsible_user = clean_string(responsible_user) or invoice.responsible_user
        invoice.amount_paid = None
        invoice.balance_due = None
        _append_history(
            session, invoice, "ISSUED", "Issued", "Invoice issued", responsible_user, previous_status
        )
        session.flush()
        from services.accounting_posting_service import capture_source_event

        capture_source_event(session, "Sales Invoice", invoice.id, "ISSUED",
                             actor_context=actor_context,
                             responsible_user=responsible_user)
    return invoice


def cancel_draft_invoice(
    session: Session,
    invoice_id: int,
    reason: str,
    responsible_user: str | None = None,
    *,
    actor_context: ActorContext,
) -> SalesInvoice:
    _require_invoice_permission(session, actor_context, "invoices.manage")
    _require_invoice_permission(session, actor_context, "invoices.view")
    invoice = _get_invoice(session, invoice_id)
    if not invoice:
        raise SalesInvoiceNotFoundError(f"Invoice #{invoice_id} was not found.")
    if invoice.status != "Draft":
        raise SalesInvoiceValidationError("Only draft invoices can be cancelled; use the void workflow for issued invoices.")
    with session.begin_nested():
        previous_status = invoice.status
        invoice.status = "Cancelled"
        invoice.void_reason = clean_string(reason) or "Draft cancelled"
        invoice.updated_at = datetime.now()
        _append_history(
            session, invoice, "CANCELLED", "Cancelled", invoice.void_reason, responsible_user, previous_status
        )
        session.flush()
    return invoice


def void_sales_invoice(
    session: Session,
    invoice_id: int,
    reason: str,
    responsible_user: str | None = None,
    *,
    actor_context: ActorContext,
) -> SalesInvoice:
    _require_invoice_permission(session, actor_context, "invoices.void")
    _require_invoice_permission(session, actor_context, "invoices.view")
    require_permission(actor_context, "sales.view", db_session=session)
    require_permission(actor_context, "receipts.view", db_session=session)
    invoice = _get_invoice(session, invoice_id)
    if not invoice:
        raise SalesInvoiceNotFoundError(f"Invoice #{invoice_id} was not found.")
    reason = clean_string(reason) or ""
    if not reason:
        raise SalesInvoiceValidationError("A reason is required to void an invoice.")
    if invoice.status != "Issued":
        raise SalesInvoiceValidationError("Only issued invoices without recorded payments can be voided.")
    from services.customer_receipt_service import calculate_invoice_balance
    if calculate_invoice_balance(
        session, invoice.id, actor_context=actor_context
    )["received"] > ZERO:
        raise SalesInvoiceValidationError(
            "An invoice with posted customer receipt allocations cannot be voided."
        )
    with session.begin_nested():
        for line in invoice.items:
            result = session.execute(
                update(SalesOrderFulfilmentItem)
                .where(SalesOrderFulfilmentItem.id == line.fulfilment_item_id)
                .where(func.coalesce(SalesOrderFulfilmentItem.invoiced_quantity, ZERO) >= line.quantity)
                .values(
                    invoiced_quantity=func.coalesce(SalesOrderFulfilmentItem.invoiced_quantity, ZERO)
                    - line.quantity
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise SalesInvoiceValidationError("Could not release the invoiced quantity allocation.")
            if line.fulfilment_item is not None:
                session.expire(line.fulfilment_item, ["invoiced_quantity"])
        previous_status = invoice.status
        invoice.status = "Void"
        invoice.void_reason = reason
        invoice.updated_at = datetime.now()
        invoice.responsible_user = clean_string(responsible_user) or invoice.responsible_user
        _append_history(session, invoice, "VOIDED", "Void", reason, responsible_user, previous_status)
        session.flush()
        from services.accounting_posting_service import reverse_source_event

        reverse_source_event(
            session, "Sales Invoice", invoice.id, "VOIDED", reason=reason,
            reversal_date=date.today(), responsible_user=responsible_user,
            actor_context=actor_context,
        )
    return invoice


def get_sales_invoice_summary(
    invoice: SalesInvoice,
    session: Session | None = None,
    *,
    actor_context: ActorContext,
) -> dict:
    business_session = session or object_session(invoice)
    if business_session is None:
        raise SalesInvoiceValidationError(
            "An attached invoice session is required to authorize the summary."
        )
    _require_invoice_permission(
        business_session,
        actor_context,
        "invoices.view",
    )
    invoice = _get_invoice(business_session, invoice.id)
    if invoice is None:
        raise SalesInvoiceNotFoundError(
            "The sales invoice is not available in this database."
        )
    payment = None
    if session is not None:
        require_permission(actor_context, "receipts.view", db_session=session)
        from services.customer_receipt_service import calculate_invoice_balance
        payment = calculate_invoice_balance(
            session, invoice.id, actor_context=actor_context
        )
    return {
        "invoice_code": invoice.invoice_code,
        "status": invoice.status,
        "customer_name": invoice.customer_name_snapshot,
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "subtotal": invoice.subtotal,
        "discount_amount": invoice.discount_amount,
        "additional_charges": invoice.additional_charges,
        "taxable_amount": invoice.taxable_amount,
        "cgst_amount": invoice.cgst_amount,
        "sgst_amount": invoice.sgst_amount,
        "igst_amount": invoice.igst_amount,
        "tax_amount": invoice.tax_amount,
        "grand_total": invoice.grand_total,
        "amount_paid": payment["received"] if payment else None,
        "balance_due": payment["balance"] if payment else None,
        "payment_status": payment["payment_status"] if payment else "Not calculated",
        "items": list(invoice.items),
        "history": list(invoice.history),
    }


def get_sales_invoice_metrics(
    session: Session, *, actor_context: ActorContext
) -> dict:
    _require_invoice_permission(session, actor_context, "invoices.view")
    require_permission(actor_context, "receipts.view", db_session=session)
    counts = {
        status: session.scalar(select(func.count(SalesInvoice.id)).where(SalesInvoice.status == status)) or 0
        for status in INVOICE_STATUSES
    }
    issued_total = session.scalar(
        select(func.coalesce(func.sum(SalesInvoice.grand_total), ZERO))
        .where(SalesInvoice.status.in_(INVOICED_STATUSES))
    )
    from services.customer_receipt_service import calculate_invoice_balance
    receivable_invoices = session.scalars(
        select(SalesInvoice).where(SalesInvoice.status.in_(INVOICED_STATUSES))
    ).all()
    outstanding_receivables = sum(
        (calculate_invoice_balance(
            session, invoice.id, actor_context=actor_context
        )["balance"] for invoice in receivable_invoices),
        ZERO,
    )
    return {
        "total_invoices": sum(counts.values()),
        "draft_invoices": counts["Draft"],
        "issued_invoices": counts["Issued"],
        "cancelled_invoices": counts["Cancelled"],
        "voided_invoices": counts["Void"],
        "invoiced_total": _money(issued_total or ZERO, "Invoiced total"),
        "payment_status": "Derived from posted customer receipt allocations",
        "outstanding_receivables": _money(outstanding_receivables, "Outstanding receivables"),
    }


def get_customer_sales_invoices(
    _session: Session, customer_id: int, *, actor_context: ActorContext
) -> list[SalesInvoice]:
    return list_sales_invoices(
        _session, customer_id=customer_id, status="ALL",
        actor_context=actor_context,
    )


def list_invoiceable_sales_orders(
    session: Session, *, actor_context: ActorContext
) -> list[SalesOrder]:
    _require_invoice_permission(session, actor_context, "invoices.view")
    require_permission(actor_context, "sales.view", db_session=session)
    order_ids = session.scalars(
        select(SalesOrderFulfilment.sales_order_id).distinct()
    ).all()
    orders = session.scalars(
        select(SalesOrder)
        .options(selectinload(SalesOrder.customer))
        .where(SalesOrder.id.in_(order_ids))
        .order_by(SalesOrder.order_date.desc(), SalesOrder.id.desc())
    ).all() if order_ids else []
    return [
        order for order in orders
        if any(
            max(ZERO, (source.quantity or ZERO) - (source.invoiced_quantity or ZERO)) > ZERO
            for source in _fulfilled_sources(session, order.id)
        )
    ]


def get_uninvoiced_fulfilment_lines(
    session: Session, order_id: int, *, actor_context: ActorContext
) -> list[dict]:
    _require_invoice_permission(session, actor_context, "invoices.view")
    require_permission(actor_context, "sales.view", db_session=session)
    result: list[dict] = []
    for source in _fulfilled_sources(session, order_id):
        remaining = max(ZERO, (source.quantity or ZERO) - (source.invoiced_quantity or ZERO))
        if remaining <= ZERO:
            continue
        order_line = source.sales_order_item
        fulfilment = source.fulfilment
        inventory = source.inventory_item
        result.append({
            "fulfilment_item_id": source.id,
            "fulfilment_code": fulfilment.fulfilment_code,
            "fulfilment_date": fulfilment.fulfilment_date,
            "sales_order_item_id": order_line.id,
            "product_name": order_line.product_name_snapshot,
            "part_code": order_line.part_code_snapshot,
            "quantity_fulfilled": source.quantity,
            "quantity_invoiced": source.invoiced_quantity or ZERO,
            "quantity_available": remaining,
            "unit": order_line.unit,
            "unit_price": order_line.unit_selling_price,
            "tax_rate": order_line.tax_rate,
            "inventory_item_id": inventory.id,
        })
    return result
