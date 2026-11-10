from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from database.models import (
    Customer,
    InventoryItem,
    SalesOrder,
    SalesOrderFulfilment,
    SalesOrderFulfilmentItem,
    SalesOrderItem,
    SalesOrderReservation,
    SalesOrderStatusHistory,
    StockMovement,
)
from services.customer_service import evaluate_credit_limit
from services.security_service import ActorContext, require_permission
from utils.validators import clean_string


CENT = Decimal("0.01")
QTY = Decimal("0.01")
ZERO = Decimal("0.00")
SALEABLE_CONDITIONS = {"working", "recyclable raw material"}
ORDER_STATUSES = (
    "Draft",
    "Confirmed",
    "Partially Fulfilled",
    "Fulfilled",
    "Cancelled",
    "On Hold",
)


class SalesOrderValidationError(ValueError):
    """Raised when an order or stock operation violates a business rule."""


class SalesOrderNotFoundError(ValueError):
    """Raised when a sales order cannot be found."""


def _require_sales_permission(
    session: Session, actor_context: ActorContext, permission: str
) -> None:
    require_permission(actor_context, permission, db_session=session)


def _decimal(value: object, label: str, *, allow_zero: bool = True, scale: Decimal = CENT) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(scale, rounding=ROUND_HALF_UP)
        if not result.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SalesOrderValidationError(f"{label} must be a valid decimal amount.") from exc
    if result < ZERO or (not allow_zero and result == ZERO):
        requirement = "greater than zero" if not allow_zero else "non-negative"
        raise SalesOrderValidationError(f"{label} must be {requirement}.")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def generate_next_sales_order_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    _require_sales_permission(session, actor_context, "sales.manage")
    codes = session.scalars(select(SalesOrder.order_code)).all()
    highest = max(
        (int(code[3:]) for code in codes if code and code.startswith("SO-") and code[3:].isdigit()),
        default=0,
    )
    return f"SO-{highest + 1:06d}"


def generate_next_fulfilment_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    _require_sales_permission(session, actor_context, "sales.dispatch")
    codes = session.scalars(select(SalesOrderFulfilment.fulfilment_code)).all()
    highest = max(
        (int(code[4:]) for code in codes if code and code.startswith("FUL-") and code[4:].isdigit()),
        default=0,
    )
    return f"FUL-{highest + 1:06d}"


def _validate_item(session: Session, row: dict, *, allow_reserved: bool = False) -> dict:
    try:
        inventory_id = int(row.get("inventory_item_id"))
    except (TypeError, ValueError) as exc:
        raise SalesOrderValidationError("Select a valid inventory item for every order line.") from exc
    item = session.get(InventoryItem, inventory_id)
    if not item or not item.is_active or item.condition.casefold() not in SALEABLE_CONDITIONS:
        raise SalesOrderValidationError("Only active working or recyclable raw-material inventory can be ordered.")
    quantity = _decimal(row.get("quantity_ordered", row.get("quantity")), "Order quantity", allow_zero=False, scale=QTY)
    price = _decimal(row.get("unit_selling_price", item.estimated_selling_price), "Unit selling price")
    discount = _decimal(row.get("discount", 0), "Line discount")
    tax_rate = _decimal(row.get("tax_rate", 0), "Tax rate", scale=Decimal("0.001"))
    subtotal = _money(quantity * price)
    if discount > subtotal:
        raise SalesOrderValidationError("A line discount cannot exceed the line subtotal.")
    tax_amount = _money((subtotal - discount) * tax_rate / Decimal("100"))
    line_total = _money(subtotal - discount + tax_amount)
    available = (item.quantity_available or ZERO) - (item.reserved_quantity or ZERO)
    if not allow_reserved and quantity > available:
        raise SalesOrderValidationError(
            f"Insufficient available stock for {item.part_code}: requested {quantity}, available {available}."
        )
    return {
        "inventory_item": item,
        "inventory_item_id": item.id,
        "product_name_snapshot": item.part_name,
        "part_code_snapshot": item.part_code,
        "quantity_ordered": quantity,
        "quantity_fulfilled": ZERO,
        "unit": item.unit,
        "unit_selling_price": price,
        "discount": discount,
        "tax_rate": tax_rate,
        "line_subtotal": subtotal,
        "line_total": line_total,
        "_tax_amount": tax_amount,
    }


def calculate_order_totals(items: list[dict], order_discount: object = 0, additional_charges: object = 0) -> dict:
    subtotal = _money(sum((row["line_subtotal"] for row in items), ZERO))
    line_discounts = _money(sum((_decimal(row.get("discount", 0), "Line discount") for row in items), ZERO))
    discount = _decimal(order_discount, "Order discount")
    charges = _decimal(additional_charges, "Additional charges")
    if line_discounts + discount > subtotal:
        raise SalesOrderValidationError("Total discounts cannot exceed the order subtotal.")
    tax_amount = _money(sum((row.get("_tax_amount", ZERO) for row in items), ZERO))
    grand_total = _money(subtotal - line_discounts - discount + charges + tax_amount)
    if grand_total < ZERO:
        raise SalesOrderValidationError("Order total cannot be negative.")
    return {
        "subtotal": subtotal,
        "discount_amount": discount,
        "additional_charges": charges,
        "tax_amount": tax_amount,
        "grand_total": grand_total,
    }


def _customer_for_order(session: Session, customer_id: object) -> Customer:
    try:
        customer = session.get(Customer, int(customer_id))
    except (TypeError, ValueError):
        customer = None
    if not customer or not customer.is_active:
        raise SalesOrderValidationError("Select an active registered customer.")
    return customer


def _validated_order_payload(session: Session, header_data: dict, items_data: list[dict]) -> tuple[Customer, list[dict], dict]:
    customer = _customer_for_order(session, header_data.get("customer_id"))
    if not items_data:
        raise SalesOrderValidationError("Add at least one order item.")
    items = [_validate_item(session, row, allow_reserved=True) for row in items_data]
    seen: set[int] = set()
    for item in items:
        if item["inventory_item_id"] in seen:
            raise SalesOrderValidationError("An inventory item may appear only once per sales order.")
        seen.add(item["inventory_item_id"])
    totals = calculate_order_totals(
        items,
        header_data.get("discount_amount", 0),
        header_data.get("additional_charges", 0),
    )
    return customer, items, totals


def _append_status(
    session: Session,
    order: SalesOrder,
    new_status: str,
    *,
    reason: str | None = None,
    responsible_user: str | None = None,
) -> None:
    previous = order.status
    order.status = new_status
    order.updated_at = datetime.now()
    session.add(SalesOrderStatusHistory(
        sales_order_id=order.id,
        previous_status=previous,
        new_status=new_status,
        reason=clean_string(reason),
        responsible_user=clean_string(responsible_user),
        created_at=datetime.now(),
    ))
    session.flush()


def _record_reservation_event(
    session: Session,
    order: SalesOrder,
    order_item: SalesOrderItem,
    event_type: str,
    quantity: Decimal,
    reason: str,
    responsible_user: str | None,
    fulfilment_id: int | None = None,
) -> None:
    session.add(SalesOrderReservation(
        sales_order_id=order.id,
        sales_order_item_id=order_item.id,
        inventory_item_id=order_item.inventory_item_id,
        event_type=event_type,
        quantity=quantity,
        product_name_snapshot=order_item.product_name_snapshot,
        part_code_snapshot=order_item.part_code_snapshot,
        unit_snapshot=order_item.unit,
        fulfilment_id=fulfilment_id,
        reason=reason,
        responsible_user=clean_string(responsible_user),
        created_at=datetime.now(),
    ))


def get_order_item_reserved_quantity(
    session: Session, order_item_id: int, *, actor_context: ActorContext
) -> Decimal:
    _require_sales_permission(session, actor_context, "sales.view")
    result = session.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (SalesOrderReservation.event_type == "RESERVE", SalesOrderReservation.quantity),
                        else_=-SalesOrderReservation.quantity,
                    )
                ),
                ZERO,
            )
        ).where(SalesOrderReservation.sales_order_item_id == order_item_id)
    ).scalar_one()
    return max(ZERO, Decimal(str(result or ZERO)).quantize(QTY))


def _order_active_reservations(session: Session, order_id: int) -> dict[int, Decimal]:
    rows = session.execute(
        select(
            SalesOrderReservation.inventory_item_id,
            func.sum(
                case(
                    (SalesOrderReservation.event_type == "RESERVE", SalesOrderReservation.quantity),
                    else_=-SalesOrderReservation.quantity,
                )
            ),
        )
        .where(SalesOrderReservation.sales_order_id == order_id)
        .group_by(SalesOrderReservation.inventory_item_id)
    ).all()
    return {
        inventory_id: max(ZERO, Decimal(str(quantity or ZERO)).quantize(QTY))
        for inventory_id, quantity in rows
        if quantity and Decimal(str(quantity)) > ZERO
    }


def _change_reserved_quantity(
    session: Session,
    inventory_item: InventoryItem,
    delta: Decimal,
) -> None:
    physical = inventory_item.quantity_available or ZERO
    reserved = inventory_item.reserved_quantity or ZERO
    next_reserved = reserved + delta
    if next_reserved < ZERO:
        raise SalesOrderValidationError("Reservation release would make reserved stock negative.")
    if next_reserved > physical:
        raise SalesOrderValidationError(
            f"Insufficient available stock for {inventory_item.part_code}: "
            f"physical {physical}, reserved {reserved}, requested {delta}."
        )
    result = session.execute(
        update(InventoryItem)
        .where(InventoryItem.id == inventory_item.id)
        .where(InventoryItem.quantity_available == physical)
        .where(func.coalesce(InventoryItem.reserved_quantity, ZERO) == reserved)
        .where(func.coalesce(InventoryItem.reserved_quantity, ZERO) + delta >= ZERO)
        .where(
            func.coalesce(InventoryItem.reserved_quantity, ZERO) + delta
            <= InventoryItem.quantity_available
        )
        .values(reserved_quantity=next_reserved, updated_at=datetime.now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise SalesOrderValidationError(
            f"Stock changed while updating {inventory_item.part_code} reservation. Refresh and retry."
        )
    inventory_item.reserved_quantity = next_reserved
    inventory_item.updated_at = datetime.now()


def _reserve_all(session: Session, order: SalesOrder, responsible_user: str | None, reason: str) -> None:
    for order_item in order.items:
        remaining = (order_item.quantity_ordered or ZERO) - (order_item.quantity_fulfilled or ZERO)
        if remaining <= ZERO:
            continue
        item = session.get(InventoryItem, order_item.inventory_item_id)
        if not item or not item.is_active or item.condition.casefold() not in SALEABLE_CONDITIONS:
            raise SalesOrderValidationError(f"Inventory item {order_item.part_code_snapshot} is no longer saleable.")
        _change_reserved_quantity(session, item, remaining)
        _record_reservation_event(session, order, order_item, "RESERVE", remaining, reason, responsible_user)


def _release_order_reservations(
    session: Session,
    order: SalesOrder,
    responsible_user: str | None,
    reason: str,
) -> None:
    active = _order_active_reservations(session, order.id)
    lines_by_inventory: dict[int, list[SalesOrderItem]] = {}
    for line in order.items:
        lines_by_inventory.setdefault(line.inventory_item_id, []).append(line)
    for inventory_id, quantity in active.items():
        item = session.get(InventoryItem, inventory_id)
        if not item:
            raise SalesOrderValidationError("A reserved inventory item no longer exists.")
        _change_reserved_quantity(session, item, -quantity)
        order_lines = lines_by_inventory.get(inventory_id, [])
        if not order_lines:
            raise SalesOrderValidationError("Reservation audit could not be matched to an order line.")
        line = order_lines[0]
        _record_reservation_event(session, order, line, "RELEASE", quantity, reason, responsible_user)


def _set_order_lines(session: Session, order: SalesOrder, rows: list[dict]) -> list[SalesOrderItem]:
    for old in list(order.items):
        session.delete(old)
    session.flush()
    created: list[SalesOrderItem] = []
    for row in rows:
        item = row["inventory_item"]
        line = SalesOrderItem(
            sales_order_id=order.id,
            inventory_item_id=item.id,
            product_name_snapshot=item.part_name,
            part_code_snapshot=item.part_code,
            quantity_ordered=row["quantity_ordered"],
            quantity_fulfilled=ZERO,
            unit=item.unit,
            unit_selling_price=row["unit_selling_price"],
            discount=row["discount"],
            tax_rate=row["tax_rate"],
            line_subtotal=row["line_subtotal"],
            line_total=row["line_total"],
        )
        session.add(line)
        created.append(line)
    session.flush()
    return created


def create_draft_order(
    session: Session,
    header_data: dict,
    items_data: list[dict],
    *,
    actor_context: ActorContext,
) -> SalesOrder:
    _require_sales_permission(session, actor_context, "sales.manage")
    require_permission(actor_context, "customers.view", db_session=session)
    require_permission(actor_context, "inventory.view", db_session=session)
    customer, validated_items, totals = _validated_order_payload(session, header_data, items_data)
    order_date = header_data.get("order_date") or date.today()
    expected_date = header_data.get("expected_delivery_date")
    if isinstance(expected_date, str) and expected_date:
        try:
            expected_date = date.fromisoformat(expected_date)
        except ValueError as exc:
            raise SalesOrderValidationError("Expected delivery date is invalid.") from exc
    if expected_date and expected_date < order_date:
        raise SalesOrderValidationError("Expected delivery date cannot be before the order date.")
    order = SalesOrder(
        order_code=generate_next_sales_order_code(session, actor_context=actor_context),
        customer_id=customer.id,
        order_date=order_date,
        expected_delivery_date=expected_date,
        status="Draft",
        notes=clean_string(header_data.get("notes")),
        responsible_user=clean_string(header_data.get("responsible_user")),
        created_at=datetime.now(),
        updated_at=datetime.now(),
        **totals,
    )
    with session.begin_nested():
        session.add(order)
        session.flush()
        order.items = _set_order_lines(session, order, validated_items)
        session.add(SalesOrderStatusHistory(
            sales_order_id=order.id,
            previous_status=None,
            new_status="Draft",
            reason="Order created",
            responsible_user=order.responsible_user,
            created_at=datetime.now(),
        ))
        session.flush()
    return order


def update_draft_order(
    session: Session,
    order_id: int,
    header_data: dict,
    items_data: list[dict],
    *,
    actor_context: ActorContext,
) -> SalesOrder:
    _require_sales_permission(session, actor_context, "sales.manage")
    require_permission(actor_context, "customers.view", db_session=session)
    require_permission(actor_context, "inventory.view", db_session=session)
    order = get_sales_order_by_id(session, order_id, actor_context=actor_context)
    if not order:
        raise SalesOrderNotFoundError(f"Sales order #{order_id} was not found.")
    if order.status not in {"Draft", "Confirmed"}:
        raise SalesOrderValidationError(
            "Only Draft and Confirmed orders can be edited. Fulfilled, partially fulfilled, "
            "cancelled, and on-hold orders require a controlled correction workflow."
        )
    with session.begin_nested():
        customer, validated_items, totals = _validated_order_payload(session, header_data, items_data)
        if order.status == "Confirmed":
            _release_order_reservations(session, order, order.responsible_user, "Release before confirmed-order edit")
        order.customer_id = customer.id
        order.order_date = header_data.get("order_date") or order.order_date
        expected = header_data.get("expected_delivery_date")
        if isinstance(expected, str) and expected:
            expected = date.fromisoformat(expected)
        if expected and expected < order.order_date:
            raise SalesOrderValidationError("Expected delivery date cannot be before the order date.")
        order.expected_delivery_date = expected
        order.notes = clean_string(header_data.get("notes"))
        for key, value in totals.items():
            setattr(order, key, value)
        order.updated_at = datetime.now()
        order.items = _set_order_lines(session, order, validated_items)
        if order.status == "Confirmed":
            _reserve_all(session, order, order.responsible_user, "Recalculate confirmed order reservation")
        session.flush()
    return order


def get_sales_order_by_id(
    session: Session, order_id: int, *, actor_context: ActorContext
) -> SalesOrder | None:
    _require_sales_permission(session, actor_context, "sales.view")
    return session.scalar(
        select(SalesOrder)
        .options(
            selectinload(SalesOrder.customer),
            selectinload(SalesOrder.items).selectinload(SalesOrderItem.inventory_item),
            selectinload(SalesOrder.fulfilments).selectinload(SalesOrderFulfilment.items),
            selectinload(SalesOrder.reservations),
            selectinload(SalesOrder.status_history),
        )
        .where(SalesOrder.id == order_id)
    )


def list_sales_orders(
    session: Session,
    search: str = "",
    customer_id: int | None = None,
    status: str = "ALL",
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int | None = None,
    offset: int = 0,
    *,
    actor_context: ActorContext,
) -> list[SalesOrder]:
    _require_sales_permission(session, actor_context, "sales.view")
    stmt = select(SalesOrder).join(Customer).options(
        selectinload(SalesOrder.customer),
        selectinload(SalesOrder.items),
    )
    if search.strip():
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(
            SalesOrder.order_code.ilike(pattern),
            Customer.customer_name.ilike(pattern),
            Customer.customer_code.ilike(pattern),
        ))
    if customer_id is not None:
        stmt = stmt.where(SalesOrder.customer_id == customer_id)
    if status != "ALL":
        stmt = stmt.where(SalesOrder.status == status)
    if start_date:
        stmt = stmt.where(SalesOrder.order_date >= start_date)
    if end_date:
        stmt = stmt.where(SalesOrder.order_date <= end_date)
    stmt = stmt.order_by(SalesOrder.order_date.desc(), SalesOrder.id.desc())
    if limit is not None:
        if limit < 0:
            raise SalesOrderValidationError("Page size cannot be negative.")
        stmt = stmt.limit(limit)
    if offset < 0:
        raise SalesOrderValidationError("Page offset cannot be negative.")
    if offset:
        stmt = stmt.offset(offset)
    return list(session.scalars(stmt).unique().all())


def check_inventory_availability(
    session: Session,
    items_data: list[dict],
    *,
    exclude_order_id: int | None = None,
    actor_context: ActorContext,
) -> list[dict]:
    _require_sales_permission(session, actor_context, "sales.view")
    require_permission(actor_context, "inventory.view", db_session=session)
    results = []
    for row in items_data:
        item_id = int(row.get("inventory_item_id"))
        item = session.get(InventoryItem, item_id)
        if not item or not item.is_active or item.condition.casefold() not in SALEABLE_CONDITIONS:
            raise SalesOrderValidationError("Only active saleable inventory items may be ordered.")
        available = (item.quantity_available or ZERO) - (item.reserved_quantity or ZERO)
        needed = _decimal(row.get("quantity_ordered", row.get("quantity")), "Order quantity", allow_zero=False, scale=QTY)
        if exclude_order_id is not None:
            active = _order_active_reservations(session, exclude_order_id)
            available += active.get(item.id, ZERO)
        results.append({
            "inventory_item_id": item.id,
            "part_code": item.part_code,
            "product_name": item.part_name,
            "physical_quantity": item.quantity_available or ZERO,
            "reserved_quantity": item.reserved_quantity or ZERO,
            "available_quantity": available,
            "requested_quantity": needed,
            "sufficient": needed <= available,
        })
    return results


def evaluate_order_credit_warning(
    session: Session, order: SalesOrder, *, actor_context: ActorContext
) -> dict:
    _require_sales_permission(session, actor_context, "sales.view")
    require_permission(actor_context, "customers.view", db_session=session)
    order = get_sales_order_by_id(session, order.id, actor_context=actor_context)
    if not order:
        raise SalesOrderNotFoundError("The sales order is not available in this database.")
    customer = session.get(Customer, order.customer_id)
    if not customer:
        raise SalesOrderValidationError("Order customer could not be found.")
    return evaluate_credit_limit(
        customer,
        order.grand_total,
    )


def evaluate_proposed_order_credit_warning(
    session: Session,
    customer_id: int,
    proposed_amount: object,
    *,
    actor_context: ActorContext,
) -> dict:
    _require_sales_permission(session, actor_context, "sales.view")
    require_permission(actor_context, "customers.view", db_session=session)
    customer = _customer_for_order(session, customer_id)
    return evaluate_credit_limit(customer, proposed_amount)


def confirm_sales_order(
    session: Session,
    order_id: int,
    responsible_user: str | None = None,
    *,
    actor_context: ActorContext,
) -> SalesOrder:
    _require_sales_permission(session, actor_context, "sales.confirm")
    require_permission(actor_context, "inventory.view", db_session=session)
    order = get_sales_order_by_id(session, order_id, actor_context=actor_context)
    if not order:
        raise SalesOrderNotFoundError(f"Sales order #{order_id} was not found.")
    if order.status == "Confirmed":
        return order
    if order.status not in {"Draft", "On Hold"}:
        raise SalesOrderValidationError(f"Cannot confirm an order in '{order.status}' status.")
    user = clean_string(responsible_user) or order.responsible_user
    with session.begin_nested():
        _reserve_all(session, order, user, "Sales order confirmed/resumed")
        order.responsible_user = user
        _append_status(
            session, order, "Confirmed",
            reason="Stock reserved for order confirmation",
            responsible_user=user,
        )
    return order


def hold_sales_order(
    session: Session,
    order_id: int,
    reason: str,
    responsible_user: str | None = None,
    *,
    actor_context: ActorContext,
) -> SalesOrder:
    _require_sales_permission(session, actor_context, "sales.confirm")
    require_permission(actor_context, "inventory.view", db_session=session)
    order = get_sales_order_by_id(session, order_id, actor_context=actor_context)
    if not order:
        raise SalesOrderNotFoundError(f"Sales order #{order_id} was not found.")
    if order.status not in {"Confirmed", "Partially Fulfilled"}:
        raise SalesOrderValidationError(f"Cannot put an order in '{order.status}' status on hold.")
    with session.begin_nested():
        _release_order_reservations(session, order, responsible_user, reason or "Order placed on hold")
        _append_status(session, order, "On Hold", reason=reason, responsible_user=responsible_user)
    return order


def resume_sales_order(
    session: Session,
    order_id: int,
    responsible_user: str | None = None,
    *,
    actor_context: ActorContext,
) -> SalesOrder:
    _require_sales_permission(session, actor_context, "sales.confirm")
    require_permission(actor_context, "inventory.view", db_session=session)
    order = get_sales_order_by_id(session, order_id, actor_context=actor_context)
    if not order:
        raise SalesOrderNotFoundError(f"Sales order #{order_id} was not found.")
    if order.status != "On Hold":
        raise SalesOrderValidationError(f"Only On Hold orders can be resumed (current status: '{order.status}').")
    prior_fulfilment = any((line.quantity_fulfilled or ZERO) > ZERO for line in order.items)
    with session.begin_nested():
        _reserve_all(session, order, responsible_user, "Order resumed")
        _append_status(
            session,
            order,
            "Partially Fulfilled" if prior_fulfilment else "Confirmed",
            reason="Reservations restored",
            responsible_user=responsible_user,
        )
    return order


def cancel_sales_order(
    session: Session,
    order_id: int,
    reason: str,
    responsible_user: str | None = None,
    *,
    actor_context: ActorContext,
) -> SalesOrder:
    _require_sales_permission(session, actor_context, "sales.manage")
    require_permission(actor_context, "inventory.view", db_session=session)
    order = get_sales_order_by_id(session, order_id, actor_context=actor_context)
    if not order:
        raise SalesOrderNotFoundError(f"Sales order #{order_id} was not found.")
    if order.status in {"Fulfilled", "Cancelled"}:
        raise SalesOrderValidationError(f"Cannot cancel an order in '{order.status}' status.")
    with session.begin_nested():
        _release_order_reservations(session, order, responsible_user, reason or "Order cancelled")
        _append_status(session, order, "Cancelled", reason=reason, responsible_user=responsible_user)
    return order


def fulfil_sales_order(
    session: Session,
    order_id: int,
    quantities: Mapping[int, object],
    *,
    fulfilment_date: datetime | None = None,
    responsible_user: str | None = None,
    remarks: str | None = None,
    idempotency_key: str,
    actor_context: ActorContext,
) -> SalesOrderFulfilment:
    _require_sales_permission(session, actor_context, "sales.dispatch")
    require_permission(actor_context, "inventory.dispatch", db_session=session)
    key = clean_string(idempotency_key)
    if not key:
        raise SalesOrderValidationError("A dispatch idempotency key is required.")
    if len(key) > 64:
        raise SalesOrderValidationError("Dispatch idempotency key cannot exceed 64 characters.")

    previous_fulfilment = session.scalar(
        select(SalesOrderFulfilment)
        .options(selectinload(SalesOrderFulfilment.items))
        .where(SalesOrderFulfilment.idempotency_key == key)
    )
    if previous_fulfilment is not None:
        existing_quantities = {
            item.sales_order_item_id: Decimal(str(item.quantity)).quantize(QTY)
            for item in previous_fulfilment.items
        }
        try:
            requested_quantities = {
                int(item_id): _decimal(
                    quantity, "Fulfilment quantity", allow_zero=False, scale=QTY
                )
                for item_id, quantity in quantities.items()
            }
        except (TypeError, ValueError) as exc:
            raise SalesOrderValidationError("Fulfilment quantities do not match the existing dispatch.") from exc
        if (
            previous_fulfilment.sales_order_id != order_id
            or requested_quantities != existing_quantities
        ):
            raise SalesOrderValidationError(
                "Dispatch idempotency key was already used for a different request."
            )
        return previous_fulfilment

    order = get_sales_order_by_id(session, order_id, actor_context=actor_context)
    if not order:
        raise SalesOrderNotFoundError(f"Sales order #{order_id} was not found.")
    if order.status not in {"Confirmed", "Partially Fulfilled"}:
        raise SalesOrderValidationError(f"Cannot fulfil an order in '{order.status}' status.")
    by_id = {line.id: line for line in order.items}
    parsed: list[tuple[SalesOrderItem, Decimal]] = []
    for item_id, raw_quantity in quantities.items():
        try:
            line = by_id[int(item_id)]
        except (KeyError, TypeError, ValueError) as exc:
            raise SalesOrderValidationError("Fulfilment contains an item not present on this order.") from exc
        quantity = _decimal(raw_quantity, "Fulfilment quantity", allow_zero=False, scale=QTY)
        remaining = (line.quantity_ordered or ZERO) - (line.quantity_fulfilled or ZERO)
        reserved = get_order_item_reserved_quantity(
            session, line.id, actor_context=actor_context
        )
        if quantity > remaining or quantity > reserved:
            raise SalesOrderValidationError(
                f"Fulfilment quantity for {line.part_code_snapshot} exceeds remaining ordered or reserved stock."
            )
        parsed.append((line, quantity))
    if not parsed:
        raise SalesOrderValidationError("Enter at least one positive fulfilment quantity.")

    with session.begin_nested():
        fulfilment = SalesOrderFulfilment(
            fulfilment_code=generate_next_fulfilment_code(
                session, actor_context=actor_context
            ),
            sales_order_id=order.id,
            idempotency_key=key,
            fulfilment_date=fulfilment_date or datetime.now(),
            responsible_user=clean_string(responsible_user),
            remarks=clean_string(remarks),
            created_at=datetime.now(),
        )
        session.add(fulfilment)
        session.flush()
        fulfilment_items: list[SalesOrderFulfilmentItem] = []
        for line, quantity in parsed:
            item = session.get(InventoryItem, line.inventory_item_id)
            if not item:
                raise SalesOrderValidationError("A reserved inventory item no longer exists.")
            from services.inventory_service import record_sales_dispatch_movement

            record_sales_dispatch_movement(
                session,
                item.id,
                quantity,
                reference_id=fulfilment.id,
                movement_date=fulfilment.fulfilment_date,
                reason=clean_string(remarks) or "Sales order fulfilment",
                remarks=f"Dispatched for sales order {order.order_code}",
                user_responsible=responsible_user,
                actor_context=actor_context,
            )
            line.quantity_fulfilled = (line.quantity_fulfilled or ZERO) + quantity
            fulfilment_item = SalesOrderFulfilmentItem(
                fulfilment_id=fulfilment.id,
                sales_order_item_id=line.id,
                inventory_item_id=item.id,
                quantity=quantity,
                unit_cost_snapshot=item.cost_price if (item.cost_price or ZERO) > ZERO else None,
                accounting_cost_status=(
                    "Verified" if item.cost_price_verified and (item.cost_price or ZERO) > ZERO
                    else "Pending Review" if (item.cost_price or ZERO) > ZERO
                    else "Missing"
                ),
            )
            session.add(fulfilment_item)
            session.flush()
            fulfilment_items.append(fulfilment_item)
            session.flush()
            _record_reservation_event(
                session, order, line, "DISPATCH", quantity,
                f"Dispatched under fulfilment {fulfilment.fulfilment_code}",
                responsible_user, fulfilment.id,
            )
        session.flush()
        complete = all(line.quantity_fulfilled >= line.quantity_ordered for line in order.items)
        new_status = "Fulfilled" if complete else "Partially Fulfilled"
        _append_status(
            session, order, new_status,
            reason=f"Fulfilment {fulfilment.fulfilment_code} recorded",
            responsible_user=responsible_user,
        )
        from services.accounting_posting_service import capture_source_event

        for fulfilment_item in fulfilment_items:
            capture_source_event(
                session, "Sales Dispatch", fulfilment_item.id, "DISPATCH",
                actor_context=actor_context,
                responsible_user=responsible_user,
            )
    return fulfilment


def get_sales_order_metrics(
    session: Session, *, actor_context: ActorContext
) -> dict:
    _require_sales_permission(session, actor_context, "sales.view")
    counts = {
        status: session.scalar(select(func.count(SalesOrder.id)).where(SalesOrder.status == status)) or 0
        for status in ORDER_STATUSES
    }
    orders = session.scalars(select(SalesOrder)).all()
    total_order_value = _money(sum(
        (
            order.grand_total or ZERO
            for order in orders
            if order.status in {"Draft", "Confirmed", "Partially Fulfilled", "On Hold"}
        ),
        ZERO,
    ))
    return {
        "total_orders": len(orders),
        "draft_orders": counts["Draft"],
        "confirmed_orders": counts["Confirmed"],
        "partially_fulfilled_orders": counts["Partially Fulfilled"],
        "fulfilled_orders": counts["Fulfilled"],
        "on_hold_orders": counts["On Hold"],
        "cancelled_orders": counts["Cancelled"],
        "open_order_value": total_order_value,
        "value_label": "Order value (not recognized revenue)",
    }


def get_sales_order_summary(
    session: Session, order: SalesOrder, *, actor_context: ActorContext
) -> dict:
    _require_sales_permission(session, actor_context, "sales.view")
    order = get_sales_order_by_id(session, order.id, actor_context=actor_context)
    if not order:
        raise SalesOrderNotFoundError("The sales order is not available in this database.")
    lines = []
    total_ordered = ZERO
    total_fulfilled = ZERO
    total_line_discount = ZERO
    reservation_by_item = _order_active_reservations(session, order.id)
    for line in order.items:
        ordered = line.quantity_ordered or ZERO
        fulfilled = line.quantity_fulfilled or ZERO
        total_ordered += ordered
        total_fulfilled += fulfilled
        total_line_discount += line.discount or ZERO
        lines.append({
            "order_item_id": line.id,
            "inventory_item_id": line.inventory_item_id,
            "part_code": line.part_code_snapshot,
            "product_name": line.product_name_snapshot,
            "quantity_ordered": ordered,
            "quantity_fulfilled": fulfilled,
            "quantity_remaining": ordered - fulfilled,
            "quantity_reserved": reservation_by_item.get(line.inventory_item_id, ZERO),
            "unit": line.unit,
            "unit_selling_price": line.unit_selling_price,
            "discount": line.discount,
            "tax_rate": line.tax_rate,
            "line_subtotal": line.line_subtotal,
            "line_total": line.line_total,
        })
    return {
        "order_code": order.order_code,
        "status": order.status,
        "customer_code": order.customer.customer_code if order.customer else None,
        "customer_name": order.customer.customer_name if order.customer else None,
        "order_date": order.order_date,
        "expected_delivery_date": order.expected_delivery_date,
        "subtotal": order.subtotal,
        "discount_amount": order.discount_amount,
        "line_discount_amount": total_line_discount,
        "additional_charges": order.additional_charges,
        "tax_amount": order.tax_amount,
        "grand_total": order.grand_total,
        "amount_paid": None,
        "balance_due": None,
        "financial_note": "Sales orders are commitments, not invoices, payments, receivables, or recognized revenue.",
        "total_ordered_quantity": total_ordered,
        "total_fulfilled_quantity": total_fulfilled,
        "fulfilment_percent": (total_fulfilled / total_ordered * Decimal("100")).quantize(CENT) if total_ordered else ZERO,
        "items": lines,
        "reservation_events": list(order.reservations),
        "fulfilments": list(order.fulfilments),
        "status_history": list(order.status_history),
    }


def get_customer_sales_orders(
    session: Session, customer_id: int, *, actor_context: ActorContext
) -> list[SalesOrder]:
    return list_sales_orders(
        session, customer_id=customer_id, actor_context=actor_context
    )


def get_inventory_sales_order_activity(
    session: Session, inventory_item_id: int, *, actor_context: ActorContext
) -> dict:
    _require_sales_permission(session, actor_context, "sales.view")
    require_permission(actor_context, "inventory.view", db_session=session)
    reservations = list(session.scalars(
        select(SalesOrderReservation)
        .options(selectinload(SalesOrderReservation.sales_order))
        .where(SalesOrderReservation.inventory_item_id == inventory_item_id)
        .order_by(SalesOrderReservation.created_at.desc(), SalesOrderReservation.id.desc())
    ).all())
    movements = list(session.scalars(
        select(StockMovement)
        .where(StockMovement.inventory_item_id == inventory_item_id)
        .where(StockMovement.reference_type == "SALES_FULFILLMENT")
        .order_by(StockMovement.movement_date.desc(), StockMovement.id.desc())
    ).all())
    return {"reservation_history": reservations, "dispatch_movements": movements}
