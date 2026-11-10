from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from database.models import (
    DismantlingEntry,
    InventoryItem,
    Purchase,
    StockMovement,
    Supplier,
)
from services.security_service import ActorContext, require_permission
from utils.validators import clean_string, to_decimal


ZERO = Decimal("0.00")
MONEY = Decimal("0.01")
MOVEMENT_TYPES = {
    "ISSUE",
    "ADJUSTMENT_IN",
    "ADJUSTMENT_OUT",
    "RETURN",
    "DAMAGE",
    "MISSING",
}


class InventoryValidationError(ValueError):
    """Raised when an inventory operation violates stock or data rules."""


class InventoryNotFoundError(ValueError):
    """Raised when an inventory item or movement cannot be found."""


def _decimal(value: object, label: str, *, allow_zero: bool = True) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)
        if not result.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InventoryValidationError(f"{label} must be a valid decimal amount.") from exc
    if result < ZERO or (not allow_zero and result == ZERO):
        requirement = "greater than zero" if not allow_zero else "non-negative"
        raise InventoryValidationError(f"{label} must be {requirement}.")
    return result


def _clean_location(data: dict) -> dict:
    keys = (
        "storage_warehouse",
        "storage_area",
        "rack_number",
        "shelf_number",
        "bin_number",
    )
    location = {key: clean_string(data.get(key)) for key in keys}
    location["storage_location"] = clean_string(data.get("storage_location"))
    if not location["storage_location"]:
        location["storage_location"] = " / ".join(
            value for key, value in location.items() if key != "storage_location" and value
        ) or None
    return location


def _location_label(item: InventoryItem) -> str:
    fields = (
        item.storage_warehouse,
        item.storage_area,
        item.rack_number,
        item.shelf_number,
        item.bin_number,
    )
    return " / ".join(value for value in fields if value) or item.storage_location or "Unassigned"


def generate_next_part_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    require_permission(actor_context, "inventory.manage", db_session=session)
    codes = session.scalars(select(InventoryItem.part_code)).all()
    max_num = max(
        (
            int(code[5:])
            for code in codes
            if code and code.startswith("PART-") and code[5:].isdigit()
        ),
        default=0,
    )
    return f"PART-{max_num + 1:06d}"


def create_inventory_item(
    session: Session, data: dict, *, actor_context: ActorContext
) -> InventoryItem:
    """Creates a zero-stock catalogue record; stock changes require a movement."""
    require_permission(actor_context, "inventory.manage", db_session=session)
    name = clean_string(data.get("part_name"))
    category = clean_string(data.get("category"))
    if not name or not category:
        raise InventoryValidationError("Component name and category are required.")
    if to_decimal(data.get("quantity_available", 0)) != ZERO or to_decimal(data.get("total_weight", 0)) != ZERO:
        raise InventoryValidationError(
            "New catalogue items must start at zero stock. Use an audited stock movement to add stock."
        )

    part_code = clean_string(data.get("part_code")) or generate_next_part_code(
        session, actor_context=actor_context
    )
    if session.scalar(select(InventoryItem.id).where(InventoryItem.part_code == part_code)):
        raise InventoryValidationError(f"Part ID '{part_code}' already exists.")
    condition = clean_string(data.get("condition")) or "Working"
    if condition.lower() in {"unusable waste", "waste"}:
        raise InventoryValidationError("Unusable waste cannot be created as inventory stock.")

    item = InventoryItem(
        part_code=part_code,
        part_name=name,
        category=category,
        unit=clean_string(data.get("unit")) or "pieces",
        quantity_available=ZERO,
        total_weight=ZERO,
        condition=condition,
        cost_price=_decimal(data.get("cost_price", 0), "Cost price"),
        cost_price_verified=bool(data.get("cost_price_verified", False)),
        estimated_selling_price=_decimal(data.get("estimated_selling_price", 0), "Selling price"),
        minimum_stock_level=_decimal(data.get("minimum_stock_level", 0), "Minimum stock level"),
        weight_status="Unverified",
        is_active=True,
        photographs=clean_string(data.get("photographs")),
        remarks=clean_string(data.get("remarks")),
        created_at=datetime.now(),
        updated_at=datetime.now(),
        **_clean_location(data),
        appliance_source=clean_string(data.get("appliance_source")),
        brand=clean_string(data.get("brand")),
        model_number=clean_string(data.get("model_number")),
    )
    session.add(item)
    session.flush()
    return item


def update_inventory_item(
    session: Session,
    item_id: int,
    data: dict,
    *,
    actor_context: ActorContext,
) -> InventoryItem:
    require_permission(actor_context, "inventory.manage", db_session=session)
    item = session.get(InventoryItem, item_id)
    if not item:
        raise InventoryNotFoundError(f"Inventory item #{item_id} not found.")
    if not item.is_active:
        raise InventoryValidationError("Inactive inventory items cannot be edited.")

    for field in ("part_name", "category", "unit", "condition"):
        if field in data:
            value = clean_string(data[field])
            if not value:
                raise InventoryValidationError(f"{field.replace('_', ' ').title()} is required.")
            if field == "condition" and value.lower() in {"unusable waste", "waste"}:
                raise InventoryValidationError("Unusable waste cannot be maintained as inventory stock.")
            setattr(item, field, value)
    for field, label in (
        ("cost_price", "Cost price"),
        ("estimated_selling_price", "Selling price"),
        ("minimum_stock_level", "Minimum stock level"),
    ):
        if field in data:
            parsed_value = _decimal(data[field], label)
            if field == "cost_price" and parsed_value != item.cost_price and "cost_price_verified" not in data:
                item.cost_price_verified = False
            setattr(item, field, parsed_value)
    if "cost_price_verified" in data:
        item.cost_price_verified = bool(data["cost_price_verified"])
    if item.cost_price_verified and (item.cost_price or ZERO) <= ZERO:
        raise InventoryValidationError("A verified accounting cost must be greater than zero.")
    for field in (
        "appliance_source",
        "brand",
        "model_number",
        "photographs",
        "remarks",
    ):
        if field in data:
            setattr(item, field, clean_string(data[field]))
    if any(key in data for key in ("storage_location", "storage_warehouse", "storage_area", "rack_number", "shelf_number", "bin_number")):
        for key, value in _clean_location(data).items():
            setattr(item, key, value)
    item.updated_at = datetime.now()
    session.flush()
    return item


def deactivate_inventory_item(
    session: Session, item_id: int, *, actor_context: ActorContext
) -> InventoryItem:
    require_permission(actor_context, "inventory.manage", db_session=session)
    item = session.get(InventoryItem, item_id)
    if not item:
        raise InventoryNotFoundError(f"Inventory item #{item_id} not found.")
    if item.quantity_available != ZERO:
        raise InventoryValidationError("Inventory items can only be deactivated when their available quantity is zero.")
    item.is_active = False
    item.updated_at = datetime.now()
    session.flush()
    return item


def _apply_stock_change(
    session: Session,
    item: InventoryItem,
    *,
    movement_type: str,
    quantity_delta: Decimal,
    weight_delta: Decimal,
    reference_type: str,
    reference_id: int | None,
    reason: str,
    user_responsible: str,
    weight_verified: bool,
    from_location: str | None = None,
    to_location: str | None = None,
    actor_context: ActorContext,
) -> StockMovement:
    require_permission(actor_context, "inventory.adjust", db_session=session)
    previous_quantity = item.quantity_available or ZERO
    previous_weight = item.total_weight or ZERO
    new_quantity = previous_quantity + quantity_delta
    new_weight = previous_weight + weight_delta
    if new_quantity < ZERO:
        raise InventoryValidationError("Stock movement would make the available quantity negative.")
    if new_quantity < (item.reserved_quantity or ZERO):
        raise InventoryValidationError(
            "Stock movement cannot reduce physical stock below quantities reserved for sales orders."
        )
    if new_weight < ZERO:
        raise InventoryValidationError("Stock movement would make the available weight negative.")
    if quantity_delta < ZERO and previous_weight > ZERO and weight_delta == ZERO:
        raise InventoryValidationError(
            "Enter the removed weight when reducing stock with a recorded inventory weight."
        )
    if new_quantity == ZERO and new_weight > ZERO:
        raise InventoryValidationError(
            "The remaining stock weight must be zero when the available quantity reaches zero."
        )

    old_weight_status = item.weight_status or "Unverified"
    if weight_verified and weight_delta > ZERO:
        if previous_quantity == ZERO:
            next_weight_status = "Verified"
        elif old_weight_status == "Verified":
            next_weight_status = "Verified"
        else:
            next_weight_status = "Partial"
    elif quantity_delta != ZERO and new_quantity > ZERO and previous_quantity > ZERO:
        next_weight_status = "Partial"
    else:
        next_weight_status = old_weight_status

    stmt = (
        update(InventoryItem)
        .where(InventoryItem.id == item.id)
        .where(InventoryItem.quantity_available == previous_quantity)
        .where(InventoryItem.total_weight == previous_weight)
        .where(
            func.coalesce(InventoryItem.reserved_quantity, ZERO)
            <= new_quantity
        )
        .values(
            quantity_available=new_quantity,
            total_weight=new_weight,
            weight_status=next_weight_status,
            updated_at=datetime.now(),
        )
        .execution_options(synchronize_session=False)
    )
    if session.execute(stmt).rowcount != 1:
        raise InventoryValidationError(
            "Inventory was updated concurrently. Refresh the item and retry the movement."
        )

    item.quantity_available = new_quantity
    item.total_weight = new_weight
    item.weight_status = next_weight_status
    item.updated_at = datetime.now()
    movement = StockMovement(
        inventory_item_id=item.id,
        movement_type=movement_type,
        quantity=quantity_delta,
        weight=weight_delta,
        reference_type=reference_type,
        reference_id=reference_id,
        movement_date=datetime.now(),
        remarks=reason,
        reason=reason,
        user_responsible=clean_string(user_responsible) or "Unspecified",
        previous_quantity=previous_quantity,
        new_quantity=new_quantity,
        previous_weight=previous_weight,
        new_weight=new_weight,
        weight_verified=weight_verified,
        from_location=from_location,
        to_location=to_location,
        created_at=datetime.now(),
    )
    session.add(movement)
    session.flush()
    return movement


def record_stock_movement(
    session: Session,
    inventory_item_id: int,
    movement_type: str,
    quantity: object,
    weight: object = 0,
    *,
    reason: str,
    user_responsible: str,
    reference_type: str = "MANUAL",
    reference_id: int | None = None,
    weight_verified: bool = False,
    actor_context: ActorContext,
) -> StockMovement:
    """Records an auditable stock movement and updates quantity/weight atomically."""
    require_permission(actor_context, "inventory.adjust", db_session=session)
    movement_type = movement_type.strip().upper()
    if movement_type not in MOVEMENT_TYPES:
        raise InventoryValidationError(f"Unsupported stock movement type '{movement_type}'.")
    reason = clean_string(reason) or ""
    if not reason:
        raise InventoryValidationError("A reason is required for every stock movement.")
    qty = _decimal(quantity, "Movement quantity", allow_zero=False)
    wt = _decimal(weight, "Movement weight")
    item = session.get(InventoryItem, inventory_item_id)
    if not item or not item.is_active:
        raise InventoryNotFoundError("Active inventory item was not found.")
    if item.condition.lower() in {"unusable waste", "waste"}:
        raise InventoryValidationError("Unusable waste cannot be recorded as inventory stock.")

    positive = movement_type in {"ADJUSTMENT_IN", "RETURN"}
    delta_qty = qty if positive else -qty
    delta_weight = wt if positive else -wt
    with session.begin_nested():
        return _apply_stock_change(
            session,
            item,
            movement_type=movement_type,
            quantity_delta=delta_qty,
            weight_delta=delta_weight,
            reference_type=clean_string(reference_type) or "MANUAL",
            reference_id=reference_id,
            reason=reason,
            user_responsible=user_responsible,
            weight_verified=weight_verified,
            actor_context=actor_context,
        )


def record_sales_dispatch_movement(
    session: Session,
    inventory_item_id: int,
    quantity: object,
    *,
    reference_id: int,
    movement_date: datetime,
    reason: str,
    remarks: str,
    user_responsible: str | None,
    actor_context: ActorContext,
) -> StockMovement:
    """Atomically consume reserved stock for one sales fulfilment."""
    require_permission(actor_context, "inventory.dispatch", db_session=session)
    qty = _decimal(quantity, "Dispatch quantity", allow_zero=False)
    reason = clean_string(reason) or ""
    if not reason:
        raise InventoryValidationError("A reason is required for every stock movement.")

    item = session.get(InventoryItem, inventory_item_id)
    if not item or not item.is_active:
        raise InventoryNotFoundError("Active inventory item was not found.")
    if item.condition.lower() in {"unusable waste", "waste"}:
        raise InventoryValidationError("Unusable waste cannot be recorded as inventory stock.")
    if session.scalar(
        select(StockMovement.id).where(
            StockMovement.inventory_item_id == inventory_item_id,
            StockMovement.movement_type == "SALE_DISPATCH",
            StockMovement.reference_type == "SALES_FULFILLMENT",
            StockMovement.reference_id == reference_id,
        )
    ) is not None:
        raise InventoryValidationError("Sales dispatch stock movement was already recorded.")

    previous_quantity = item.quantity_available or ZERO
    previous_reserved = item.reserved_quantity or ZERO
    previous_weight = item.total_weight or ZERO
    weight = ZERO
    if (
        item.weight_status == "Verified"
        and previous_quantity > ZERO
        and previous_weight > ZERO
    ):
        weight = (previous_weight / previous_quantity * qty).quantize(
            MONEY, rounding=ROUND_HALF_UP
        )
    new_quantity = previous_quantity - qty
    new_reserved = previous_reserved - qty
    new_weight = max(ZERO, previous_weight - weight)
    if new_quantity < ZERO or new_reserved < ZERO or new_reserved > new_quantity:
        raise InventoryValidationError("Dispatch would violate physical or reserved stock balances.")

    with session.begin_nested():
        result = session.execute(
            update(InventoryItem)
            .where(InventoryItem.id == item.id)
            .where(InventoryItem.quantity_available == previous_quantity)
            .where(func.coalesce(InventoryItem.reserved_quantity, ZERO) == previous_reserved)
            .where(InventoryItem.total_weight == previous_weight)
            .where(func.coalesce(InventoryItem.reserved_quantity, ZERO) >= qty)
            .where(InventoryItem.quantity_available >= qty)
            .values(
                quantity_available=new_quantity,
                reserved_quantity=new_reserved,
                total_weight=new_weight,
                updated_at=datetime.now(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise InventoryValidationError(
                "Inventory was updated concurrently. Refresh the item and retry the dispatch."
            )
        item.quantity_available = new_quantity
        item.reserved_quantity = new_reserved
        item.total_weight = new_weight
        item.updated_at = datetime.now()
        movement = StockMovement(
            inventory_item_id=item.id,
            movement_type="SALE_DISPATCH",
            quantity=-qty,
            weight=-weight,
            reference_type="SALES_FULFILLMENT",
            reference_id=reference_id,
            movement_date=movement_date,
            remarks=remarks,
            reason=reason,
            user_responsible=clean_string(user_responsible),
            previous_quantity=previous_quantity,
            new_quantity=new_quantity,
            previous_weight=previous_weight,
            new_weight=new_weight,
            weight_verified=item.weight_status == "Verified",
            created_at=datetime.now(),
        )
        session.add(movement)
        session.flush()
    return movement


def transfer_inventory_location(
    session: Session,
    inventory_item_id: int,
    destination: dict,
    *,
    reason: str,
    user_responsible: str,
    actor_context: ActorContext,
) -> StockMovement:
    require_permission(actor_context, "inventory.adjust", db_session=session)
    item = session.get(InventoryItem, inventory_item_id)
    if not item or not item.is_active:
        raise InventoryNotFoundError("Active inventory item was not found.")
    reason = clean_string(reason) or ""
    if not reason:
        raise InventoryValidationError("A reason is required for every storage transfer.")
    new_location = _clean_location(destination)
    if not new_location["storage_location"]:
        raise InventoryValidationError("Select or enter a destination storage location.")
    old_label = _location_label(item)
    new_label = " / ".join(
        value
        for key, value in new_location.items()
        if key != "storage_location" and value
    ) or new_location["storage_location"]

    with session.begin_nested():
        previous_quantity = item.quantity_available or ZERO
        previous_weight = item.total_weight or ZERO
        result = session.execute(
            update(InventoryItem)
            .where(InventoryItem.id == item.id)
            .where(InventoryItem.quantity_available == previous_quantity)
            .where(InventoryItem.total_weight == previous_weight)
            .values(**new_location, updated_at=datetime.now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise InventoryValidationError(
                "Inventory was updated concurrently. Refresh the item and retry the transfer."
            )
        for key, value in new_location.items():
            setattr(item, key, value)
        item.updated_at = datetime.now()
        movement = StockMovement(
            inventory_item_id=item.id,
            movement_type="TRANSFER",
            quantity=ZERO,
            weight=ZERO,
            reference_type="STORAGE_TRANSFER",
            reference_id=item.id,
            movement_date=datetime.now(),
            remarks=reason,
            reason=reason,
            user_responsible=clean_string(user_responsible) or "Unspecified",
            previous_quantity=previous_quantity,
            new_quantity=previous_quantity,
            previous_weight=previous_weight,
            new_weight=previous_weight,
            weight_verified=item.weight_status == "Verified",
            from_location=old_label,
            to_location=new_label,
            created_at=datetime.now(),
        )
        session.add(movement)
        session.flush()
        return movement


def list_inventory_items(
    session: Session,
    search: str = "",
    category: str = "ALL",
    condition: str = "ALL",
    location: str = "ALL",
    low_stock_only: bool = False,
    sort_by: str = "name_asc",
    include_inactive: bool = False,
    *,
    actor_context: ActorContext,
) -> list[InventoryItem]:
    require_permission(actor_context, "inventory.view", db_session=session)
    stmt = select(InventoryItem)
    if not include_inactive:
        stmt = stmt.where(InventoryItem.is_active.is_(True))
    search = search.strip()
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(InventoryItem.part_code.ilike(pattern), InventoryItem.part_name.ilike(pattern)))
    if category != "ALL":
        stmt = stmt.where(InventoryItem.category == category)
    if condition != "ALL":
        stmt = stmt.where(InventoryItem.condition == condition)
    if location != "ALL":
        pattern = f"%{location}%"
        stmt = stmt.where(
            or_(
                InventoryItem.storage_location.ilike(pattern),
                InventoryItem.storage_warehouse.ilike(pattern),
                InventoryItem.storage_area.ilike(pattern),
                InventoryItem.rack_number.ilike(pattern),
                InventoryItem.shelf_number.ilike(pattern),
                InventoryItem.bin_number.ilike(pattern),
            )
        )
    if low_stock_only:
        stmt = stmt.where(InventoryItem.minimum_stock_level.is_not(None))
        stmt = stmt.where(
            InventoryItem.quantity_available - func.coalesce(InventoryItem.reserved_quantity, ZERO)
            <= InventoryItem.minimum_stock_level
        )
    if sort_by == "quantity_asc":
        stmt = stmt.order_by(InventoryItem.quantity_available.asc(), InventoryItem.part_name.asc())
    elif sort_by == "quantity_desc":
        stmt = stmt.order_by(InventoryItem.quantity_available.desc(), InventoryItem.part_name.asc())
    elif sort_by == "value_desc":
        stmt = stmt.order_by(
            (InventoryItem.quantity_available * InventoryItem.estimated_selling_price).desc()
        )
    elif sort_by == "name_desc":
        stmt = stmt.order_by(InventoryItem.part_name.desc())
    else:
        stmt = stmt.order_by(InventoryItem.part_name.asc(), InventoryItem.id.asc())
    return list(session.scalars(stmt).all())


def get_inventory_item(
    session: Session, item_id: int, *, actor_context: ActorContext
) -> InventoryItem | None:
    require_permission(actor_context, "inventory.view", db_session=session)
    return session.scalar(
        select(InventoryItem)
        .options(selectinload(InventoryItem.movements))
        .where(InventoryItem.id == item_id)
    )


def get_inventory_movements(
    session: Session,
    inventory_item_id: int | None = None,
    movement_type: str = "ALL",
    limit: int | None = None,
    *,
    actor_context: ActorContext,
) -> list[StockMovement]:
    require_permission(actor_context, "inventory.view", db_session=session)
    stmt = (
        select(StockMovement)
        .options(
            selectinload(StockMovement.inventory_item),
        )
        .order_by(StockMovement.movement_date.desc(), StockMovement.id.desc())
    )
    if inventory_item_id is not None:
        stmt = stmt.where(StockMovement.inventory_item_id == inventory_item_id)
    if movement_type != "ALL":
        stmt = stmt.where(StockMovement.movement_type == movement_type)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def get_inventory_metrics(session: Session, *, actor_context: ActorContext) -> dict:
    require_permission(actor_context, "inventory.view", db_session=session)
    items = list(session.scalars(select(InventoryItem).where(InventoryItem.is_active.is_(True))).all())
    physical_quantity = sum((item.quantity_available or ZERO for item in items), ZERO)
    reserved_quantity = sum((item.reserved_quantity or ZERO for item in items), ZERO)
    total_quantity = physical_quantity - reserved_quantity
    verified_weight = sum(
        (
            item.total_weight or ZERO
            for item in items
            if item.weight_status == "Verified"
        ),
        ZERO,
    )
    cost_quantity_value = sum(
        (
            (item.quantity_available or ZERO) * (item.cost_price or ZERO)
            for item in items if item.condition.lower() != "unusable waste"
        ),
        ZERO,
    ).quantize(MONEY, rounding=ROUND_HALF_UP)
    cost_weight_value = sum(
        ((item.total_weight or ZERO) * (item.cost_price or ZERO) for item in items),
        ZERO,
    ).quantize(MONEY, rounding=ROUND_HALF_UP)
    estimated_selling_value = sum(
        (
            (item.quantity_available or ZERO) * (item.estimated_selling_price or ZERO)
            for item in items
            if item.condition.lower() != "unusable waste"
        ),
        ZERO,
    ).quantize(MONEY, rounding=ROUND_HALF_UP)
    working_value = sum(
        (
            (item.quantity_available or ZERO) * (item.estimated_selling_price or ZERO)
            for item in items
            if item.condition.lower() == "working"
        ),
        ZERO,
    ).quantize(MONEY, rounding=ROUND_HALF_UP)
    non_working_value = sum(
        (
            (item.quantity_available or ZERO) * (item.estimated_selling_price or ZERO)
            for item in items
            if item.condition.lower() in {"non-working", "non working"}
        ),
        ZERO,
    ).quantize(MONEY, rounding=ROUND_HALF_UP)
    damaged_quantity = sum(
        (
            item.quantity_available or ZERO
            for item in items
            if item.condition.lower() == "damaged"
        ),
        ZERO,
    )
    low_stock = [
        item
        for item in items
        if item.minimum_stock_level is not None
        and (item.quantity_available or ZERO) - (item.reserved_quantity or ZERO) <= item.minimum_stock_level
    ]
    return {
        "total_unique_items": len(items),
        "total_available_quantity": total_quantity,
        "total_physical_quantity": physical_quantity,
        "total_reserved_quantity": reserved_quantity,
        "total_verified_weight": verified_weight,
        "has_unverified_weights": any(item.weight_status != "Verified" for item in items),
        "cost_quantity_value": cost_quantity_value,
        "cost_weight_value": cost_weight_value,
        "estimated_selling_value": estimated_selling_value,
        "working_components_value": working_value,
        "non_working_components_value": non_working_value,
        "damaged_quantity": damaged_quantity,
        "low_stock_count": len(low_stock),
        "out_of_stock_count": sum(
            1 for item in items
            if (item.quantity_available or ZERO) - (item.reserved_quantity or ZERO) <= ZERO
        ),
    }


def get_inventory_source_references(
    session: Session, item_id: int, *, actor_context: ActorContext
) -> list[dict]:
    require_permission(actor_context, "inventory.view", db_session=session)
    rows = session.execute(
        select(StockMovement, DismantlingEntry, Purchase, Supplier)
        .join(InventoryItem, StockMovement.inventory_item_id == InventoryItem.id)
        .join(
            DismantlingEntry,
            (StockMovement.reference_type == "DISMANTLING_ENTRY")
            & (StockMovement.reference_id == DismantlingEntry.id),
        )
        .join(Purchase, DismantlingEntry.purchase_id == Purchase.id)
        .join(Supplier, Purchase.supplier_id == Supplier.id)
        .where(InventoryItem.id == item_id)
        .order_by(StockMovement.movement_date.desc())
    ).all()
    return [
        {
            "dismantling_code": entry.dismantling_code,
            "purchase_code": purchase.purchase_code,
            "supplier_name": supplier.business_name,
            "appliance_category": entry.appliance_category,
            "brand": entry.brand,
            "model_number": entry.model_number,
            "quantity": movement.quantity,
            "weight": movement.weight,
        }
        for movement, entry, purchase, supplier in rows
    ]


def get_inventory_categories(
    session: Session, *, actor_context: ActorContext
) -> list[str]:
    require_permission(actor_context, "inventory.view", db_session=session)
    return list(
        session.scalars(
            select(InventoryItem.category)
            .where(InventoryItem.is_active.is_(True))
            .distinct()
            .order_by(InventoryItem.category)
        ).all()
    )
