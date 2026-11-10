from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload
from database.models import (
    DismantlingEntry,
    RecoveredComponent,
    ScrapMaterial,
    InventoryItem,
    StockMovement,
    Purchase,
    PurchaseItem,
    Supplier,
)
from services.security_service import ActorContext, require_permission
from utils.validators import clean_string, to_decimal, validate_positive_decimal

class DismantlingValidationError(ValueError):
    """Raised when dismantling validation or constraint fails."""
    pass

class DismantlingNotFoundError(ValueError):
    """Raised when dismantling record cannot be found."""
    pass


COMPONENT_CATEGORIES = [
    "Compressors",
    "Electric Motors",
    "Copper Wiring & Harnesses",
    "Aluminium Heat Sinks & Condensers",
    "Steel & Iron Frames",
    "Printed Circuit Boards (PCBs)",
    "Plastics (ABS/HIPS)",
    "Fans & Blowers",
    "Cooling Coils & Tubes",
    "Transformers & Inductors",
    "Glass & Acrylic Panels",
    "Other Recyclable Material",
]

COMPONENT_CONDITIONS = [
    "Working",
    "Recyclable Raw Material",
    "Non-working",
    "Damaged",
]

SCRAP_CLASSIFICATIONS = [
    "Recyclable Scrap",
    "Unusable Waste",
    "Hazardous Waste",
]

DISMANTLING_STATUSES = [
    "Draft",
    "Pending Approval",
    "Approved",
    "Rejected",
    "Reversed",
]

# Conditions that may enter saleable / recyclable inventory. Damaged and
# non-working recovered parts stay on the dismantling record only.
STOCKABLE_COMPONENT_CONDITIONS = {"Working", "Recyclable Raw Material"}
STOCKABLE_SCRAP_CLASSIFICATIONS = {"Recyclable Scrap"}


def is_component_stockable(condition: str | None) -> bool:
    return (condition or "") in STOCKABLE_COMPONENT_CONDITIONS


def is_scrap_stockable(classification: str | None) -> bool:
    return (classification or "") in STOCKABLE_SCRAP_CLASSIFICATIONS


def _parse_optional_weight(raw) -> Decimal | None:
    """Treats missing or zero input appliance weight as unavailable (None)."""
    if raw is None or raw == "":
        return None
    weight = to_decimal(raw)
    if weight <= Decimal("0.00"):
        return None
    return weight


def _generate_next_dismantling_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    require_permission(actor_context, "dismantling.view", db_session=session)
    codes = session.scalars(select(DismantlingEntry.dismantling_code)).all()
    max_num = 0
    for code in codes:
        if code and code.startswith("DIS-"):
            num_part = code[4:]
            if num_part.isdigit():
                max_num = max(max_num, int(num_part))
    return f"DIS-{max_num + 1:06d}"


def generate_next_dismantling_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    """Generates the next sequential dismantling code for an authorized user."""
    require_permission(actor_context, "dismantling.view", db_session=session)
    return _generate_next_dismantling_code(
        session, actor_context=actor_context
    )


def _generate_next_part_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    require_permission(actor_context, "inventory.view", db_session=session)
    codes = session.scalars(select(InventoryItem.part_code)).all()
    max_num = 0
    for code in codes:
        if code and code.startswith("PART-"):
            num_part = code[5:]
            if num_part.isdigit():
                max_num = max(max_num, int(num_part))
    return f"PART-{max_num + 1:06d}"


def generate_next_part_code(
    session: Session, *, actor_context: ActorContext
) -> str:
    """Generates the next part code for users allowed to view inventory."""
    require_permission(actor_context, "inventory.view", db_session=session)
    return _generate_next_part_code(session, actor_context=actor_context)


def _get_remaining_undismantled_quantity(
    session: Session,
    purchase_item_id: int,
    exclude_dismantling_id: int | None = None,
    *,
    actor_context: ActorContext,
) -> int:
    """Computes the remaining undismantled appliance quantity for a specific purchase item."""
    require_permission(actor_context, "dismantling.view", db_session=session)
    p_item = session.get(PurchaseItem, purchase_item_id)
    if not p_item:
        return 0

    # Query active/committed batches for this purchase item
    stmt = (
        select(func.coalesce(func.sum(DismantlingEntry.appliances_dismantled), 0))
        .where(DismantlingEntry.purchase_item_id == purchase_item_id)
        .where(DismantlingEntry.status.in_(["Draft", "Pending Approval", "Approved"]))
    )
    if exclude_dismantling_id:
        stmt = stmt.where(DismantlingEntry.id != exclude_dismantling_id)

    total_dismantled = session.scalar(stmt) or 0
    remaining = p_item.quantity - total_dismantled
    return max(0, int(remaining))


def get_remaining_undismantled_quantity(
    session: Session,
    purchase_item_id: int,
    exclude_dismantling_id: int | None = None,
    *,
    actor_context: ActorContext,
) -> int:
    """Returns the remaining purchased quantity for an authorized user."""
    require_permission(actor_context, "dismantling.view", db_session=session)
    return _get_remaining_undismantled_quantity(
        session,
        purchase_item_id,
        exclude_dismantling_id,
        actor_context=actor_context,
    )


def get_purchases_with_undismantled_items(
    session: Session,
    exclude_dismantling_id: int | None = None,
    *,
    actor_context: ActorContext,
) -> list[dict]:
    """Retrieves all purchases that contain items with remaining undismantled appliances."""
    require_permission(actor_context, "dismantling.view", db_session=session)
    purchases = list(
        session.scalars(
            select(Purchase)
            .options(selectinload(Purchase.supplier), selectinload(Purchase.items))
            .order_by(Purchase.purchase_date.desc(), Purchase.id.desc())
        ).all()
    )

    result = []
    for p in purchases:
        available_items = []
        for it in p.items:
            rem = _get_remaining_undismantled_quantity(
                session,
                it.id,
                exclude_dismantling_id=exclude_dismantling_id,
                actor_context=actor_context,
            )
            if rem > 0:
                available_items.append({
                    "item_id": it.id,
                    "category": it.appliance_category,
                    "brand": it.brand or "—",
                    "model": it.model_number or "—",
                    "total_purchased": it.quantity,
                    "remaining_quantity": rem,
                    "unit": it.unit,
                    "weight": it.weight or Decimal("0.00"),
                })
        if available_items:
            result.append({
                "purchase_id": p.id,
                "purchase_code": p.purchase_code,
                "purchase_date": p.purchase_date,
                "supplier_name": p.supplier.business_name,
                "supplier_code": p.supplier.supplier_code,
                "available_items": available_items,
            })
    return result


def calculate_dismantling_recovery_math(
    components: list[dict],
    scrap: list[dict],
    labour_charges: Decimal,
    other_expenses: Decimal,
    input_weight: Decimal | None,
) -> dict:
    """Calculates all weights, values, costs, and recovery percentages."""
    total_recovered_weight = Decimal("0.00")
    total_components_qty = Decimal("0.00")
    estimated_components_val = Decimal("0.00")

    for c in components:
        qty = to_decimal(c.get("quantity", 1))
        unit = clean_string(c.get("unit")) or "pieces"
        w = to_decimal(c.get("weight", 0))
        unit_val = to_decimal(c.get("estimated_unit_value", 0))

        # Value calculation
        if unit == "kg" and w > Decimal("0.00"):
            tot_val = (w * unit_val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            tot_val = (qty * unit_val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        c["estimated_total_value"] = tot_val
        total_recovered_weight += w
        total_components_qty += qty
        estimated_components_val += tot_val

    total_scrap_weight = Decimal("0.00")
    estimated_scrap_val = Decimal("0.00")
    for s in scrap:
        w = to_decimal(s.get("weight", 0))
        val = to_decimal(s.get("estimated_value", 0))
        total_scrap_weight += w
        estimated_scrap_val += val

    total_processing_cost = (labour_charges + other_expenses).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    estimated_recovery_value = (estimated_components_val + estimated_scrap_val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Recovery percentage by weight: (Recovered material weight / Total input appliance weight) * 100
    # If input weight is unavailable or zero, recovery percentage is None (N/A)
    recovery_percentage = None
    if input_weight is not None and input_weight > Decimal("0.00"):
        recovery_percentage = (
            (total_recovered_weight / input_weight) * Decimal("100.00")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {
        "total_recovered_weight": total_recovered_weight.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "total_components_qty": total_components_qty.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "total_scrap_weight": total_scrap_weight.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "total_processing_cost": total_processing_cost,
        "estimated_recovery_value": estimated_recovery_value,
        "recovery_percentage": recovery_percentage,
    }


def create_dismantling_entry(
    session: Session,
    header_data: dict,
    components_data: list[dict],
    scrap_data: list[dict],
    *,
    actor_context: ActorContext,
) -> DismantlingEntry:
    """Creates a new dismantling entry with full validation against remaining purchase quantities."""
    require_permission(actor_context, "dismantling.manage", db_session=session)
    purchase_id = header_data.get("purchase_id")
    purchase_item_id = header_data.get("purchase_item_id")

    if not purchase_id or not purchase_item_id:
        raise DismantlingValidationError("Dismantling must be linked to a valid Purchase and Purchase Item.")

    p_item = session.get(PurchaseItem, purchase_item_id)
    if not p_item or p_item.purchase_id != purchase_id:
        raise DismantlingValidationError(f"Invalid purchase item #{purchase_item_id} for purchase #{purchase_id}.")

    # Appliances quantity validation
    try:
        qty_to_dismantle = int(header_data.get("appliances_dismantled", 0))
        if qty_to_dismantle <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise DismantlingValidationError("Number of appliances being dismantled must be a positive integer (> 0).")

    remaining_qty = _get_remaining_undismantled_quantity(
        session, purchase_item_id, actor_context=actor_context
    )
    if qty_to_dismantle > remaining_qty:
        raise DismantlingValidationError(
            f"Cannot dismantle {qty_to_dismantle} appliances. "
            f"Only {remaining_qty} remain undismantled for '{p_item.appliance_category}'."
        )

    # Input weight — missing/zero is stored as None so recovery % stays N/A
    input_w = _parse_optional_weight(header_data.get("input_weight"))

    # Processing charges
    ok_labour, labour_charges, err = validate_positive_decimal(header_data.get("labour_charges", 0), "Labour charges")
    if not ok_labour: raise DismantlingValidationError(err)

    ok_other, other_expenses, err = validate_positive_decimal(header_data.get("other_expenses", 0), "Other processing expenses")
    if not ok_other: raise DismantlingValidationError(err)

    # Validate components & scrap
    if (not components_data or len(components_data) == 0) and (not scrap_data or len(scrap_data) == 0):
        raise DismantlingValidationError("At least one recovered component or scrap material must be recorded.")

    # Dismantling Code
    dismantling_code = clean_string(header_data.get("dismantling_code"))
    if not dismantling_code:
        dismantling_code = _generate_next_dismantling_code(
            session, actor_context=actor_context
        )
    else:
        existing = session.scalar(select(DismantlingEntry).where(DismantlingEntry.dismantling_code == dismantling_code))
        if existing:
            raise DismantlingValidationError(f"Dismantling ID '{dismantling_code}' already exists.")

    # Dismantling Date
    d_date = header_data.get("dismantling_date")
    if isinstance(d_date, str):
        try:
            d_date = datetime.strptime(d_date, "%Y-%m-%d").date()
        except ValueError:
            d_date = date.today()
    elif isinstance(d_date, datetime):
        d_date = d_date.date()
    elif not isinstance(d_date, date):
        d_date = date.today()

    # Calculate math
    calc = calculate_dismantling_recovery_math(
        components_data, scrap_data, labour_charges, other_expenses, input_w
    )

    requested_status = clean_string(header_data.get("status"))
    status = requested_status if requested_status in ("Draft", "Pending Approval") else "Draft"

    entry = DismantlingEntry(
        dismantling_code=dismantling_code,
        dismantling_date=d_date,
        purchase_id=purchase_id,
        purchase_item_id=purchase_item_id,
        appliance_category=p_item.appliance_category,
        brand=p_item.brand,
        model_number=p_item.model_number,
        appliances_dismantled=qty_to_dismantle,
        input_weight=input_w,
        team_leader=clean_string(header_data.get("team_leader")),
        labour_charges=labour_charges,
        other_expenses=other_expenses,
        total_processing_cost=calc["total_processing_cost"],
        total_recovered_weight=calc["total_recovered_weight"],
        total_scrap_weight=calc["total_scrap_weight"],
        estimated_recovery_value=calc["estimated_recovery_value"],
        recovery_percentage=calc["recovery_percentage"],
        status=status,
        remarks=clean_string(header_data.get("remarks")),
        attachments=clean_string(header_data.get("attachments")),
        is_inventory_updated=False,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    # Attach components
    for c in components_data:
        comp_name = clean_string(c.get("component_name"))
        if not comp_name:
            raise DismantlingValidationError("Component name is required for all recovered items.")

        comp_cat = clean_string(c.get("component_category")) or "Other Recyclable Material"
        c_qty = to_decimal(c.get("quantity", 1))
        c_unit = clean_string(c.get("unit")) or "pieces"
        c_weight = to_decimal(c.get("weight", 0))
        c_condition = clean_string(c.get("condition")) or "Working"
        c_unit_val = to_decimal(c.get("estimated_unit_value", 0))
        c_tot_val = c.get("estimated_total_value") or Decimal("0.00")

        entry.recovered_components.append(
            RecoveredComponent(
                component_name=comp_name,
                component_category=comp_cat,
                quantity=c_qty,
                unit=c_unit,
                weight=c_weight,
                condition=c_condition,
                estimated_unit_value=c_unit_val,
                estimated_total_value=c_tot_val,
                storage_location=clean_string(c.get("storage_location")),
                remarks=clean_string(c.get("remarks")),
            )
        )

    # Attach scrap items
    for s in scrap_data:
        s_type = clean_string(s.get("scrap_type"))
        if not s_type:
            raise DismantlingValidationError("Scrap type is required for all recorded scrap lines.")

        s_class = clean_string(s.get("material_classification")) or "Unusable Waste"
        s_qty = to_decimal(s.get("quantity", 1))
        s_weight = to_decimal(s.get("weight", 0))
        s_unit = clean_string(s.get("unit")) or "kg"
        s_val = to_decimal(s.get("estimated_value", 0))

        entry.scrap_materials.append(
            ScrapMaterial(
                scrap_type=s_type,
                material_classification=s_class,
                quantity=s_qty,
                weight=s_weight,
                unit=s_unit,
                estimated_value=s_val,
                disposal_method=clean_string(s.get("disposal_method")),
                remarks=clean_string(s.get("remarks")),
            )
        )

    session.add(entry)
    session.flush()
    return entry


def update_dismantling_entry(
    session: Session,
    entry_id: int,
    header_data: dict,
    components_data: list[dict],
    scrap_data: list[dict],
    *,
    actor_context: ActorContext,
) -> DismantlingEntry:
    """Updates an existing dismantling entry (only allowed in Draft or Pending status)."""
    require_permission(actor_context, "dismantling.manage", db_session=session)
    entry = session.get(DismantlingEntry, entry_id)
    if not entry:
        raise DismantlingNotFoundError(f"Dismantling entry #{entry_id} not found.")

    if entry.status != "Draft":
        raise DismantlingValidationError(
            f"Entry {entry.dismantling_code} is {entry.status} and cannot be edited. "
            "Only Draft batches can be modified. Approved batches require the reversal workflow."
        )

    purchase_item_id = header_data.get("purchase_item_id", entry.purchase_item_id)
    p_item = session.get(PurchaseItem, purchase_item_id)
    if not p_item:
        raise DismantlingValidationError("Invalid purchase item.")

    qty_to_dismantle = int(header_data.get("appliances_dismantled", entry.appliances_dismantled))
    remaining_qty = _get_remaining_undismantled_quantity(
        session,
        purchase_item_id,
        exclude_dismantling_id=entry.id,
        actor_context=actor_context,
    )
    if qty_to_dismantle > remaining_qty:
        raise DismantlingValidationError(
            f"Cannot dismantle {qty_to_dismantle} appliances. Only {remaining_qty} remain available."
        )

    if "input_weight" in header_data:
        input_w = _parse_optional_weight(header_data.get("input_weight"))
    else:
        input_w = entry.input_weight
    labour_charges = to_decimal(header_data.get("labour_charges", entry.labour_charges))
    other_expenses = to_decimal(header_data.get("other_expenses", entry.other_expenses))

    calc = calculate_dismantling_recovery_math(
        components_data, scrap_data, labour_charges, other_expenses, input_w
    )

    entry.purchase_id = p_item.purchase_id
    entry.purchase_item_id = purchase_item_id
    entry.appliance_category = p_item.appliance_category
    entry.brand = p_item.brand
    entry.model_number = p_item.model_number
    entry.appliances_dismantled = qty_to_dismantle
    entry.input_weight = input_w
    entry.team_leader = clean_string(header_data.get("team_leader", entry.team_leader))
    entry.labour_charges = labour_charges
    entry.other_expenses = other_expenses
    entry.total_processing_cost = calc["total_processing_cost"]
    entry.total_recovered_weight = calc["total_recovered_weight"]
    entry.total_scrap_weight = calc["total_scrap_weight"]
    entry.estimated_recovery_value = calc["estimated_recovery_value"]
    entry.recovery_percentage = calc["recovery_percentage"]
    entry.remarks = clean_string(header_data.get("remarks", entry.remarks))
    entry.attachments = clean_string(header_data.get("attachments", entry.attachments))
    requested_status = clean_string(header_data.get("status"))
    if requested_status in ("Draft", "Pending Approval"):
        entry.status = requested_status
    entry.updated_at = datetime.now()

    # Clear and replace components
    entry.recovered_components.clear()
    for c in components_data:
        comp_name = clean_string(c.get("component_name"))
        if not comp_name:
            raise DismantlingValidationError("Component name is required.")
        entry.recovered_components.append(
            RecoveredComponent(
                component_name=comp_name,
                component_category=clean_string(c.get("component_category")) or "Other Recyclable Material",
                quantity=to_decimal(c.get("quantity", 1)),
                unit=clean_string(c.get("unit")) or "pieces",
                weight=to_decimal(c.get("weight", 0)),
                condition=clean_string(c.get("condition")) or "Working",
                estimated_unit_value=to_decimal(c.get("estimated_unit_value", 0)),
                estimated_total_value=c.get("estimated_total_value") or Decimal("0.00"),
                storage_location=clean_string(c.get("storage_location")),
                remarks=clean_string(c.get("remarks")),
            )
        )

    # Clear and replace scrap
    entry.scrap_materials.clear()
    for s in scrap_data:
        s_type = clean_string(s.get("scrap_type"))
        if not s_type:
            raise DismantlingValidationError("Scrap type is required.")
        entry.scrap_materials.append(
            ScrapMaterial(
                scrap_type=s_type,
                material_classification=clean_string(s.get("material_classification")) or "Unusable Waste",
                quantity=to_decimal(s.get("quantity", 1)),
                weight=to_decimal(s.get("weight", 0)),
                unit=clean_string(s.get("unit")) or "kg",
                estimated_value=to_decimal(s.get("estimated_value", 0)),
                disposal_method=clean_string(s.get("disposal_method")),
                remarks=clean_string(s.get("remarks")),
            )
        )

    session.flush()
    return entry


def submit_dismantling_for_approval(
    session: Session, entry_id: int, *, actor_context: ActorContext
) -> DismantlingEntry:
    """Transitions status from Draft to Pending Approval."""
    require_permission(actor_context, "dismantling.manage", db_session=session)
    entry = session.get(DismantlingEntry, entry_id)
    if not entry:
        raise DismantlingNotFoundError(f"Dismantling entry #{entry_id} not found.")
    if entry.status != "Draft":
        raise DismantlingValidationError(f"Entry {entry.dismantling_code} is already in '{entry.status}' status.")

    entry.status = "Pending Approval"
    entry.updated_at = datetime.now()
    session.flush()
    return entry


def approve_dismantling_entry(
    session: Session,
    entry_id: int,
    approved_by: str = "Authorized Factory Supervisor",
    *,
    actor_context: ActorContext,
) -> DismantlingEntry:
    """
    Approves a dismantling entry and atomicly creates/updates InventoryItem records
    and StockMovement audit trails in the exact same transaction.
    """
    require_permission(actor_context, "dismantling.approve", db_session=session)
    require_permission(actor_context, "inventory.adjust", db_session=session)
    entry = session.get(DismantlingEntry, entry_id)
    if not entry:
        raise DismantlingNotFoundError(f"Dismantling entry #{entry_id} not found.")

    if entry.status == "Approved" or entry.is_inventory_updated:
        raise DismantlingValidationError(f"Batch {entry.dismantling_code} has already been approved and stock updated.")

    if entry.status in ["Rejected", "Reversed"]:
        raise DismantlingValidationError(f"Cannot approve batch in '{entry.status}' status.")
    existing_stock_posting = session.scalar(
        select(StockMovement.id)
        .where(StockMovement.reference_type == "DISMANTLING_ENTRY")
        .where(StockMovement.reference_id == entry.id)
        .where(StockMovement.movement_type == "INWARD_DISMANTLING")
        .limit(1)
    )
    if existing_stock_posting is not None:
        raise DismantlingValidationError(
            f"Batch {entry.dismantling_code} already has inventory movements; duplicate posting was prevented."
        )

    # Nested SAVEPOINT: if inventory posting fails, approval is rolled back with it.
    with session.begin_nested():
        _post_dismantling_inventory(
            session, entry, approved_by=approved_by, actor_context=actor_context
        )

    return entry


def _post_dismantling_inventory(
    session: Session,
    entry: DismantlingEntry,
    *,
    approved_by: str,
    actor_context: ActorContext,
) -> None:
    """Perform the complete, authorized approval and stock-posting transaction."""
    require_permission(actor_context, "dismantling.approve", db_session=session)
    require_permission(actor_context, "inventory.adjust", db_session=session)
    persisted_entry = session.get(DismantlingEntry, entry.id)
    if persisted_entry is None:
        raise DismantlingNotFoundError(f"Dismantling entry #{entry.id} not found.")
    if persisted_entry.status == "Approved" or persisted_entry.is_inventory_updated:
        raise DismantlingValidationError(
            f"Batch {persisted_entry.dismantling_code} has already been approved and stock updated."
        )
    if persisted_entry.status in ["Rejected", "Reversed"]:
        raise DismantlingValidationError(
            f"Cannot approve batch in '{persisted_entry.status}' status."
        )
    existing_stock_posting = session.scalar(
        select(StockMovement.id)
        .where(StockMovement.reference_type == "DISMANTLING_ENTRY")
        .where(StockMovement.reference_id == persisted_entry.id)
        .where(StockMovement.movement_type == "INWARD_DISMANTLING")
        .limit(1)
    )
    if existing_stock_posting is not None:
        raise DismantlingValidationError(
            f"Batch {persisted_entry.dismantling_code} already has inventory movements; duplicate posting was prevented."
        )

    entry = persisted_entry
    for comp in entry.recovered_components:
        if not is_component_stockable(comp.condition):
            continue

        inv_item = session.scalar(
            select(InventoryItem)
            .where(InventoryItem.part_name == comp.component_name)
            .where(InventoryItem.condition == comp.condition)
            .where(InventoryItem.category == comp.component_category)
            .where(InventoryItem.unit == comp.unit)
            .where(InventoryItem.storage_location == comp.storage_location)
            .where(InventoryItem.is_active.is_(True))
        )

        if not inv_item:
            inv_item = InventoryItem(
                part_code=_generate_next_part_code(
                    session, actor_context=actor_context
                ),
                part_name=comp.component_name,
                category=comp.component_category,
                unit=comp.unit,
                quantity_available=comp.quantity,
                total_weight=comp.weight or Decimal("0.00"),
                condition=comp.condition,
                cost_price=Decimal("0.00"),
                estimated_selling_price=comp.estimated_unit_value,
                storage_location=comp.storage_location,
                appliance_source=entry.appliance_category,
                brand=entry.brand,
                model_number=entry.model_number,
                weight_status="Verified" if comp.weight and comp.weight > Decimal("0.00") else "Unverified",
                is_active=True,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            session.add(inv_item)
            session.flush()
            previous_quantity = Decimal("0.00")
            previous_weight = Decimal("0.00")
        else:
            previous_quantity = inv_item.quantity_available or Decimal("0.00")
            previous_weight = inv_item.total_weight or Decimal("0.00")
            new_quantity = previous_quantity + comp.quantity
            new_weight = previous_weight + (comp.weight or Decimal("0.00"))
            if comp.weight and comp.weight > Decimal("0.00"):
                next_weight_status = "Verified" if (
                    previous_quantity == Decimal("0.00") or inv_item.weight_status == "Verified"
                ) else "Partial"
            elif previous_quantity > Decimal("0.00"):
                next_weight_status = "Partial"
            else:
                next_weight_status = "Unverified"
            changes = {
                "quantity_available": new_quantity,
                "total_weight": new_weight,
                "weight_status": next_weight_status,
                "updated_at": datetime.now(),
            }
            if not inv_item.appliance_source:
                changes.update(
                    appliance_source=entry.appliance_category,
                    brand=entry.brand,
                    model_number=entry.model_number,
                )
            if comp.estimated_unit_value > Decimal("0.00"):
                changes["estimated_selling_price"] = comp.estimated_unit_value
            result = session.execute(
                update(InventoryItem)
                .where(InventoryItem.id == inv_item.id)
                .where(InventoryItem.quantity_available == previous_quantity)
                .where(InventoryItem.total_weight == previous_weight)
                .values(**changes)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise DismantlingValidationError(
                    "Inventory changed during approval. Retry after refreshing the batch."
                )
            inv_item.quantity_available = new_quantity
            inv_item.total_weight = new_weight
            inv_item.weight_status = next_weight_status
            if not inv_item.appliance_source:
                inv_item.appliance_source = entry.appliance_category
                inv_item.brand = entry.brand
                inv_item.model_number = entry.model_number
            if comp.estimated_unit_value > Decimal("0.00"):
                inv_item.estimated_selling_price = comp.estimated_unit_value
            inv_item.updated_at = changes["updated_at"]
        new_quantity = inv_item.quantity_available
        new_weight = inv_item.total_weight

        comp.inventory_item_id = inv_item.id
        session.add(
            StockMovement(
                inventory_item_id=inv_item.id,
                movement_type="INWARD_DISMANTLING",
                quantity=comp.quantity,
                weight=comp.weight or Decimal("0.00"),
                reference_type="DISMANTLING_ENTRY",
                reference_id=entry.id,
                movement_date=datetime.now(),
                remarks=f"Recovered from batch {entry.dismantling_code} ({entry.appliance_category})",
                reason=f"Recovered from batch {entry.dismantling_code} ({entry.appliance_category})",
                user_responsible=entry.approved_by or "Authorized Factory Supervisor",
                previous_quantity=previous_quantity,
                new_quantity=new_quantity,
                previous_weight=previous_weight,
                new_weight=new_weight,
                weight_verified=bool(comp.weight and comp.weight > Decimal("0.00")),
                created_at=datetime.now(),
            )
        )

    for scrap in entry.scrap_materials:
        if not is_scrap_stockable(scrap.material_classification):
            continue

        scrap_name = f"Scrap - {scrap.scrap_type}"
        inv_item = session.scalar(
            select(InventoryItem)
            .where(InventoryItem.part_name == scrap_name)
            .where(InventoryItem.condition == "Recyclable Raw Material")
            .where(InventoryItem.unit == scrap.unit)
            .where(InventoryItem.is_active.is_(True))
        )

        if not inv_item:
            unit_price = (
                (scrap.estimated_value / scrap.weight)
                if scrap.weight and scrap.weight > Decimal("0.00")
                else Decimal("0.00")
            )
            inv_item = InventoryItem(
                part_code=_generate_next_part_code(
                    session, actor_context=actor_context
                ),
                part_name=scrap_name,
                category="Recyclable Raw Materials",
                unit=scrap.unit,
                quantity_available=scrap.quantity,
                total_weight=scrap.weight,
                condition="Recyclable Raw Material",
                cost_price=Decimal("0.00"),
                estimated_selling_price=unit_price,
                storage_location="Scrap Yard",
                appliance_source=entry.appliance_category,
                brand=entry.brand,
                model_number=entry.model_number,
                weight_status="Verified" if scrap.weight > Decimal("0.00") else "Unverified",
                is_active=True,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            session.add(inv_item)
            session.flush()
            previous_quantity = Decimal("0.00")
            previous_weight = Decimal("0.00")
        else:
            previous_quantity = inv_item.quantity_available or Decimal("0.00")
            previous_weight = inv_item.total_weight or Decimal("0.00")
            new_quantity = previous_quantity + scrap.quantity
            new_weight = previous_weight + scrap.weight
            next_weight_status = (
                "Verified" if scrap.weight > Decimal("0.00") and (
                    previous_quantity == Decimal("0.00") or inv_item.weight_status == "Verified"
                ) else ("Partial" if new_quantity > Decimal("0.00") else "Unverified")
            )
            changes = {
                "quantity_available": new_quantity,
                "total_weight": new_weight,
                "weight_status": next_weight_status,
                "updated_at": datetime.now(),
            }
            if not inv_item.appliance_source:
                changes.update(
                    appliance_source=entry.appliance_category,
                    brand=entry.brand,
                    model_number=entry.model_number,
                )
            result = session.execute(
                update(InventoryItem)
                .where(InventoryItem.id == inv_item.id)
                .where(InventoryItem.quantity_available == previous_quantity)
                .where(InventoryItem.total_weight == previous_weight)
                .values(**changes)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise DismantlingValidationError(
                    "Inventory changed during approval. Retry after refreshing the batch."
                )
            inv_item.quantity_available = new_quantity
            inv_item.total_weight = new_weight
            inv_item.weight_status = next_weight_status
            if not inv_item.appliance_source:
                inv_item.appliance_source = entry.appliance_category
                inv_item.brand = entry.brand
                inv_item.model_number = entry.model_number
            inv_item.updated_at = changes["updated_at"]
        new_quantity = inv_item.quantity_available
        new_weight = inv_item.total_weight

        session.add(
            StockMovement(
                inventory_item_id=inv_item.id,
                movement_type="INWARD_DISMANTLING",
                quantity=scrap.quantity,
                weight=scrap.weight,
                reference_type="DISMANTLING_ENTRY",
                reference_id=entry.id,
                movement_date=datetime.now(),
                remarks=f"Recyclable scrap recovered from batch {entry.dismantling_code}",
                reason=f"Recyclable scrap recovered from batch {entry.dismantling_code}",
                user_responsible=entry.approved_by or "Authorized Factory Supervisor",
                previous_quantity=previous_quantity,
                new_quantity=new_quantity,
                previous_weight=previous_weight,
                new_weight=new_weight,
                weight_verified=scrap.weight > Decimal("0.00"),
                created_at=datetime.now(),
            )
        )

    entry.status = "Approved"
    entry.approved_by = approved_by
    entry.approved_at = datetime.now()
    entry.is_inventory_updated = True
    entry.updated_at = datetime.now()
    session.flush()
    from services.accounting_posting_service import capture_source_event

    capture_source_event(
        session, "Dismantling", entry.id, "APPROVED",
        actor_context=actor_context,
        responsible_user=approved_by,
    )


def reject_dismantling_entry(
    session: Session,
    entry_id: int,
    reason: str = "",
    *,
    actor_context: ActorContext,
) -> DismantlingEntry:
    """Rejects a dismantling entry, leaving inventory untouched and restoring undismantled appliance counts."""
    require_permission(actor_context, "dismantling.approve", db_session=session)
    entry = session.get(DismantlingEntry, entry_id)
    if not entry:
        raise DismantlingNotFoundError(f"Dismantling entry #{entry_id} not found.")

    if entry.status in ["Approved", "Reversed"]:
        raise DismantlingValidationError(f"Cannot reject an entry in '{entry.status}' status.")

    entry.status = "Rejected"
    if reason:
        entry.remarks = f"{entry.remarks or ''} [Rejected: {reason}]".strip()
    entry.updated_at = datetime.now()
    session.flush()
    return entry


def reverse_approved_dismantling_entry(
    session: Session,
    entry_id: int,
    reason: str,
    reversed_by: str = "Authorized Supervisor",
    *,
    actor_context: ActorContext,
) -> DismantlingEntry:
    """
    Auditable Reversal Workflow: Reverses an approved dismantling entry, generates offsetting
    negative StockMovement records, adjusts inventory downward, and restores appliance counts.
    """
    require_permission(actor_context, "dismantling.approve", db_session=session)
    require_permission(actor_context, "inventory.adjust", db_session=session)
    entry = session.get(DismantlingEntry, entry_id)
    if not entry:
        raise DismantlingNotFoundError(f"Dismantling entry #{entry_id} not found.")

    if entry.status != "Approved" or not entry.is_inventory_updated:
        raise DismantlingValidationError(f"Only approved entries can be reversed. Current status: '{entry.status}'.")

    inward_movements = list(
        session.scalars(
            select(StockMovement)
            .where(StockMovement.reference_type == "DISMANTLING_ENTRY")
            .where(StockMovement.reference_id == entry.id)
            .where(StockMovement.movement_type == "INWARD_DISMANTLING")
        ).all()
    )

    with session.begin_nested():
        for movement in inward_movements:
            inv = session.get(InventoryItem, movement.inventory_item_id)
            if inv:
                previous_quantity = inv.quantity_available or Decimal("0.00")
                previous_weight = inv.total_weight or Decimal("0.00")
                quantity_to_reverse = movement.quantity
                weight_to_reverse = movement.weight or Decimal("0.00")
                new_quantity = previous_quantity - quantity_to_reverse
                new_weight = previous_weight - weight_to_reverse
                if new_quantity < Decimal("0.00") or new_weight < Decimal("0.00"):
                    raise DismantlingValidationError(
                        f"Cannot reverse batch {entry.dismantling_code}: stock from this batch has already been consumed or adjusted."
                    )
                result = session.execute(
                    update(InventoryItem)
                    .where(InventoryItem.id == inv.id)
                    .where(InventoryItem.quantity_available == previous_quantity)
                    .where(InventoryItem.total_weight == previous_weight)
                    .values(
                        quantity_available=new_quantity,
                        total_weight=new_weight,
                        weight_status="Unverified" if new_quantity == Decimal("0.00") else "Partial",
                        updated_at=datetime.now(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise DismantlingValidationError(
                        "Inventory changed during correction. Refresh and retry the reversal."
                    )
                inv.quantity_available = new_quantity
                inv.total_weight = new_weight
                inv.weight_status = (
                    "Unverified" if new_quantity == Decimal("0.00") else "Partial"
                )
                inv.updated_at = datetime.now()

            session.add(
                StockMovement(
                    inventory_item_id=movement.inventory_item_id,
                    movement_type="CORRECTION_REVERSAL",
                    quantity=-movement.quantity,
                    weight=-(movement.weight or Decimal("0.00")),
                    reference_type="DISMANTLING_ENTRY",
                    reference_id=entry.id,
                    movement_date=datetime.now(),
                    remarks=f"Auditable Reversal of batch {entry.dismantling_code}: {reason} (by {reversed_by})",
                    reason=reason,
                    user_responsible=reversed_by,
                    previous_quantity=previous_quantity if inv else None,
                    new_quantity=new_quantity if inv else None,
                    previous_weight=previous_weight if inv else None,
                    new_weight=new_weight if inv else None,
                    weight_verified=False,
                    created_at=datetime.now(),
                )
            )

        for comp in entry.recovered_components:
            comp.inventory_item_id = None

        entry.status = "Reversed"
        entry.is_inventory_updated = False
        entry.remarks = f"{entry.remarks or ''} [Reversed on {datetime.now().strftime('%d-%b-%Y')}: {reason}]".strip()
        entry.updated_at = datetime.now()
        session.flush()

    return entry


def get_dismantling_by_id(
    session: Session, entry_id: int, *, actor_context: ActorContext
) -> DismantlingEntry | None:
    require_permission(actor_context, "dismantling.view", db_session=session)
    stmt = (
        select(DismantlingEntry)
        .options(
            selectinload(DismantlingEntry.purchase).selectinload(Purchase.supplier),
            selectinload(DismantlingEntry.purchase_item),
            selectinload(DismantlingEntry.recovered_components),
            selectinload(DismantlingEntry.scrap_materials),
        )
        .where(DismantlingEntry.id == entry_id)
    )
    return session.scalar(stmt)


def get_dismantling_by_code(
    session: Session, code: str, *, actor_context: ActorContext
) -> DismantlingEntry | None:
    require_permission(actor_context, "dismantling.view", db_session=session)
    stmt = (
        select(DismantlingEntry)
        .options(
            selectinload(DismantlingEntry.purchase).selectinload(Purchase.supplier),
            selectinload(DismantlingEntry.purchase_item),
            selectinload(DismantlingEntry.recovered_components),
            selectinload(DismantlingEntry.scrap_materials),
        )
        .where(DismantlingEntry.dismantling_code == code.strip())
    )
    return session.scalar(stmt)


def list_dismantling_entries(
    session: Session,
    search: str = "",
    start_date: date | None = None,
    end_date: date | None = None,
    category_filter: str = "ALL",
    status_filter: str = "ALL",
    sort_by: str = "date_desc",
    *,
    actor_context: ActorContext,
) -> list[DismantlingEntry]:
    require_permission(actor_context, "dismantling.view", db_session=session)
    stmt = (
        select(DismantlingEntry)
        .options(
            selectinload(DismantlingEntry.purchase).selectinload(Purchase.supplier),
            selectinload(DismantlingEntry.recovered_components),
            selectinload(DismantlingEntry.scrap_materials),
        )
    )

    # Search filter
    search = search.strip()
    if search:
        search_pattern = f"%{search}%"
        stmt = stmt.join(Purchase).join(Supplier).where(
            or_(
                DismantlingEntry.dismantling_code.ilike(search_pattern),
                DismantlingEntry.appliance_category.ilike(search_pattern),
                DismantlingEntry.team_leader.ilike(search_pattern),
                Purchase.purchase_code.ilike(search_pattern),
                Supplier.business_name.ilike(search_pattern),
            )
        )

    # Date range filters
    if start_date:
        stmt = stmt.where(DismantlingEntry.dismantling_date >= start_date)
    if end_date:
        stmt = stmt.where(DismantlingEntry.dismantling_date <= end_date)

    # Status filter
    if status_filter and status_filter != "ALL":
        stmt = stmt.where(DismantlingEntry.status.ilike(status_filter))

    # Appliance Category filter
    if category_filter and category_filter != "ALL":
        stmt = stmt.where(DismantlingEntry.appliance_category == category_filter)

    # Sorting
    if sort_by == "date_desc":
        stmt = stmt.order_by(DismantlingEntry.dismantling_date.desc(), DismantlingEntry.id.desc())
    elif sort_by == "date_asc":
        stmt = stmt.order_by(DismantlingEntry.dismantling_date.asc(), DismantlingEntry.id.asc())
    elif sort_by == "qty_desc":
        stmt = stmt.order_by(DismantlingEntry.appliances_dismantled.desc())
    elif sort_by == "val_desc":
        stmt = stmt.order_by(DismantlingEntry.estimated_recovery_value.desc())
    elif sort_by == "code_asc":
        stmt = stmt.order_by(DismantlingEntry.dismantling_code.asc())
    else:
        stmt = stmt.order_by(DismantlingEntry.id.desc())

    return list(session.scalars(stmt).all())




def _apply_entry_filters(
    stmt,
    start_date: date | None = None,
    end_date: date | None = None,
    category_filter: str = "ALL",
    status_filter: str = "ALL",
):
    if start_date:
        stmt = stmt.where(DismantlingEntry.dismantling_date >= start_date)
    if end_date:
        stmt = stmt.where(DismantlingEntry.dismantling_date <= end_date)
    if status_filter and status_filter != "ALL":
        stmt = stmt.where(DismantlingEntry.status == status_filter)
    if category_filter and category_filter != "ALL":
        stmt = stmt.where(DismantlingEntry.appliance_category == category_filter)
    return stmt


def get_dismantling_metrics(
    session: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    category_filter: str = "ALL",
    status_filter: str = "ALL",
    *,
    actor_context: ActorContext,
) -> dict:
    """Computes summary metrics for the Dismantling Dashboard, honoring optional filters."""
    require_permission(actor_context, "dismantling.view", db_session=session)
    purchased_stmt = select(func.coalesce(func.sum(PurchaseItem.quantity), 0))
    if category_filter and category_filter != "ALL":
        purchased_stmt = purchased_stmt.where(PurchaseItem.appliance_category == category_filter)
    total_purchased = session.scalar(purchased_stmt) or 0

    approved_stmt = select(func.coalesce(func.sum(DismantlingEntry.appliances_dismantled), 0)).where(
        DismantlingEntry.status == "Approved"
    )
    if start_date:
        approved_stmt = approved_stmt.where(DismantlingEntry.dismantling_date >= start_date)
    if end_date:
        approved_stmt = approved_stmt.where(DismantlingEntry.dismantling_date <= end_date)
    if category_filter and category_filter != "ALL":
        approved_stmt = approved_stmt.where(DismantlingEntry.appliance_category == category_filter)
    total_dismantled = session.scalar(approved_stmt) or 0

    pending_dismantling = max(0, int(total_purchased) - int(total_dismantled))

    batch_stmt = _apply_entry_filters(
        select(func.count(DismantlingEntry.id)),
        start_date, end_date, category_filter, status_filter,
    )
    total_batches = session.scalar(batch_stmt) or 0

    kpi_status = status_filter if status_filter and status_filter != "ALL" else "Approved"

    def _sum_entry_col(column):
        stmt = select(func.coalesce(func.sum(column), Decimal("0.00")))
        if kpi_status != "ALL":
            stmt = stmt.where(DismantlingEntry.status == kpi_status)
        if start_date:
            stmt = stmt.where(DismantlingEntry.dismantling_date >= start_date)
        if end_date:
            stmt = stmt.where(DismantlingEntry.dismantling_date <= end_date)
        if category_filter and category_filter != "ALL":
            stmt = stmt.where(DismantlingEntry.appliance_category == category_filter)
        return session.scalar(stmt) or Decimal("0.00")

    comps_stmt = (
        select(func.coalesce(func.sum(RecoveredComponent.quantity), 0))
        .join(DismantlingEntry)
    )
    if kpi_status != "ALL":
        comps_stmt = comps_stmt.where(DismantlingEntry.status == kpi_status)
    if start_date:
        comps_stmt = comps_stmt.where(DismantlingEntry.dismantling_date >= start_date)
    if end_date:
        comps_stmt = comps_stmt.where(DismantlingEntry.dismantling_date <= end_date)
    if category_filter and category_filter != "ALL":
        comps_stmt = comps_stmt.where(DismantlingEntry.appliance_category == category_filter)
    total_recovered_comps = session.scalar(comps_stmt) or Decimal("0.00")

    total_recovered_wt = _sum_entry_col(DismantlingEntry.total_recovered_weight)
    total_scrap_wt = _sum_entry_col(DismantlingEntry.total_scrap_weight)
    total_labour_cost = _sum_entry_col(DismantlingEntry.labour_charges)
    total_recovery_val = _sum_entry_col(DismantlingEntry.estimated_recovery_value)

    return {
        "total_appliances_purchased": int(total_purchased),
        "total_appliances_dismantled": int(total_dismantled),
        "pending_dismantling_quantity": int(pending_dismantling),
        "total_dismantling_batches": int(total_batches),
        "total_recovered_components": to_decimal(total_recovered_comps),
        "total_recovered_weight": to_decimal(total_recovered_wt),
        "total_scrap_weight": to_decimal(total_scrap_wt),
        "total_labour_cost": to_decimal(total_labour_cost),
        "total_recovery_value": to_decimal(total_recovery_val),
    }


def get_recent_dismantling_entries(
    session: Session, limit: int = 5, *, actor_context: ActorContext
) -> list[DismantlingEntry]:
    require_permission(actor_context, "dismantling.view", db_session=session)
    stmt = (
        select(DismantlingEntry)
        .options(
            selectinload(DismantlingEntry.purchase).selectinload(Purchase.supplier),
            selectinload(DismantlingEntry.recovered_components),
        )
        .order_by(DismantlingEntry.dismantling_date.desc(), DismantlingEntry.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).all())
