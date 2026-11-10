from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from database.models import (
    Purchase,
    Supplier,
    SupplierPayment,
    SupplierPaymentAllocation,
)
from services.payment_common import (
    CENT,
    ZERO,
    PaymentValidationError,
    _append_payment_history,
    clean_optional,
    _ensure_unique_reference,
    money,
    payment_date,
    payment_method,
)
from services.security_service import ActorContext, require_permission


def _allocation_total(session: Session, payment_id: int) -> Decimal:
    return session.scalar(
        select(func.coalesce(func.sum(SupplierPaymentAllocation.amount), ZERO))
        .where(SupplierPaymentAllocation.payment_id == payment_id)
    ) or ZERO


def _posted_purchase_allocated(session: Session, purchase_id: int) -> Decimal:
    return session.scalar(
        select(func.coalesce(func.sum(SupplierPaymentAllocation.amount), ZERO))
        .join(SupplierPayment, SupplierPayment.id == SupplierPaymentAllocation.payment_id)
        .where(
            SupplierPaymentAllocation.purchase_id == purchase_id,
            SupplierPayment.status == "Posted",
        )
    ) or ZERO


def _posted_opening_allocated(session: Session, supplier_id: int) -> Decimal:
    return session.scalar(
        select(func.coalesce(func.sum(SupplierPaymentAllocation.amount), ZERO))
        .join(SupplierPayment, SupplierPayment.id == SupplierPaymentAllocation.payment_id)
        .where(
            SupplierPayment.supplier_id == supplier_id,
            SupplierPaymentAllocation.allocation_type == "Opening Balance",
            SupplierPayment.status == "Posted",
        )
    ) or ZERO


def calculate_purchase_balance(
    session: Session, purchase_id: int, *, actor_context: ActorContext
) -> dict:
    require_permission(
        actor_context, "supplier_payments.view", db_session=session
    )
    purchase = session.get(Purchase, purchase_id)
    if purchase is None:
        raise PaymentValidationError(f"Purchase #{purchase_id} does not exist.")
    total = Decimal(str(purchase.total_amount)).quantize(CENT)
    historical_paid = Decimal(str(purchase.amount_paid)).quantize(CENT)
    new_paid = _posted_purchase_allocated(session, purchase_id)
    balance = max(ZERO, total - historical_paid - new_paid).quantize(CENT)
    return {
        "purchase": purchase,
        "purchase_total": total,
        "historical_amount_paid": historical_paid,
        "new_supplier_payments": new_paid,
        "balance": balance,
    }


def get_supplier_payment_targets(
    session: Session, supplier_id: int, *, actor_context: ActorContext
) -> list[dict]:
    require_permission(
        actor_context, "supplier_payments.view", db_session=session
    )
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise PaymentValidationError(f"Supplier #{supplier_id} does not exist.")
    original_opening = Decimal(str(supplier.opening_balance or 0)).quantize(CENT)
    opening_received = _posted_opening_allocated(session, supplier_id)
    opening_remaining = max(ZERO, original_opening - opening_received).quantize(CENT)
    targets: list[dict] = []
    if opening_remaining > ZERO:
        targets.append({
            "allocation_type": "Opening Balance",
            "purchase_id": None,
            "code": "OPENING BALANCE",
            "purchase_date": None,
            "total": original_opening,
            "historical_amount_paid": ZERO,
            "previously_allocated": opening_received,
            "outstanding": opening_remaining,
        })
    purchases = session.scalars(
        select(Purchase)
        .where(Purchase.supplier_id == supplier_id)
        .order_by(Purchase.purchase_date, Purchase.id)
    ).all()
    for purchase in purchases:
        balance = calculate_purchase_balance(
            session, purchase.id, actor_context=actor_context
        )
        if balance["balance"] > ZERO:
            targets.append({
                "allocation_type": "Purchase",
                "purchase_id": purchase.id,
                "code": purchase.purchase_code,
                "purchase_date": purchase.purchase_date,
                "total": balance["purchase_total"],
                "historical_amount_paid": balance["historical_amount_paid"],
                "previously_allocated": balance["new_supplier_payments"],
                "outstanding": balance["balance"],
            })
    return targets


def _normalize_allocations(allocations: list[dict] | None, default_date: date) -> list[dict]:
    normalized = []
    for index, allocation in enumerate(allocations or [], start=1):
        allocation_type = str(
            allocation.get("allocation_type", allocation.get("type", ""))
        ).strip()
        if allocation_type not in ("Purchase", "Opening Balance"):
            raise PaymentValidationError(
                f"Allocation #{index} must target a purchase or opening balance."
            )
        purchase_id = allocation.get("purchase_id")
        if allocation_type == "Purchase":
            try:
                purchase_id = int(purchase_id)
            except (TypeError, ValueError) as exc:
                raise PaymentValidationError(f"Allocation #{index} requires a purchase.") from exc
        else:
            purchase_id = None
        normalized.append({
            "allocation_type": allocation_type,
            "purchase_id": purchase_id,
            "amount": money(allocation.get("amount"), f"Allocation #{index}", positive=True),
            "allocation_date": payment_date(
                allocation.get("allocation_date", default_date), f"Allocation #{index} date"
            ),
            "reference": clean_optional(allocation.get("reference"), "Allocation reference", 150),
        })
    return normalized


def _validate_allocation_targets(
    session: Session,
    supplier_id: int,
    allocations: list[dict],
    *,
    actor_context: ActorContext,
    lock_targets: bool = False,
) -> None:
    by_purchase: dict[int, Decimal] = {}
    opening_amount = ZERO
    for allocation in allocations:
        if allocation["allocation_type"] == "Opening Balance":
            opening_amount += allocation["amount"]
        else:
            purchase_id = allocation["purchase_id"]
            by_purchase[purchase_id] = (
                by_purchase.get(purchase_id, ZERO) + allocation["amount"]
            )
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise PaymentValidationError("The selected supplier no longer exists.")
    if opening_amount:
        original_opening = Decimal(str(supplier.opening_balance or 0)).quantize(CENT)
        remaining = original_opening - _posted_opening_allocated(session, supplier_id)
        if opening_amount > max(ZERO, remaining):
            raise PaymentValidationError("Opening-balance allocation exceeds the remaining payable.")
        if lock_targets:
            active = (
                select(func.coalesce(func.sum(SupplierPaymentAllocation.amount), ZERO))
                .join(SupplierPayment, SupplierPayment.id == SupplierPaymentAllocation.payment_id)
                .where(
                    SupplierPayment.supplier_id == supplier_id,
                    SupplierPaymentAllocation.allocation_type == "Opening Balance",
                    SupplierPayment.status == "Posted",
                )
                .scalar_subquery()
            )
            result = session.execute(
                update(Supplier)
                .where(Supplier.id == supplier_id)
                .where(func.round(Supplier.opening_balance - active, 2) >= opening_amount)
                .values(opening_balance=Supplier.opening_balance)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise PaymentValidationError("Supplier opening-balance capacity changed; refresh and try again.")

    for purchase_id, amount in by_purchase.items():
        purchase = session.get(Purchase, purchase_id)
        if purchase is None or purchase.supplier_id != supplier_id:
            raise PaymentValidationError(
                f"Purchase #{purchase_id} does not belong to the selected supplier."
            )
        balance = calculate_purchase_balance(
            session, purchase_id, actor_context=actor_context
        )
        if amount > balance["balance"]:
            raise PaymentValidationError(
                f"Allocation exceeds the outstanding balance of purchase {purchase.purchase_code}."
            )
        if lock_targets:
            active = (
                select(func.coalesce(func.sum(SupplierPaymentAllocation.amount), ZERO))
                .join(SupplierPayment, SupplierPayment.id == SupplierPaymentAllocation.payment_id)
                .where(
                    SupplierPaymentAllocation.purchase_id == purchase_id,
                    SupplierPayment.status == "Posted",
                )
                .scalar_subquery()
            )
            result = session.execute(
                update(Purchase)
                .where(Purchase.id == purchase_id)
                .where(Purchase.supplier_id == supplier_id)
                .where(Purchase.total_amount - Purchase.amount_paid - active >= amount)
                .values(total_amount=Purchase.total_amount)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise PaymentValidationError(
                    f"Purchase {purchase.purchase_code} balance changed; refresh and try again."
                )


def _replace_draft_allocations(
    session: Session,
    payment: SupplierPayment,
    allocations: list[dict],
    *,
    actor_context: ActorContext,
) -> None:
    if sum((row["amount"] for row in allocations), ZERO) > payment.amount_paid:
        raise PaymentValidationError("Allocations cannot exceed the supplier payment amount.")
    _validate_allocation_targets(
        session, payment.supplier_id, allocations, actor_context=actor_context
    )
    session.query(SupplierPaymentAllocation).filter_by(payment_id=payment.id).delete(
        synchronize_session=False
    )
    session.flush()
    session.expire(payment, ["allocations"])
    for allocation in allocations:
        session.add(SupplierPaymentAllocation(
            payment_id=payment.id,
            purchase_id=allocation["purchase_id"],
            allocation_type=allocation["allocation_type"],
            allocation_source="Posting",
            allocation_date=allocation["allocation_date"],
            amount=allocation["amount"],
            reference=allocation["reference"],
        ))
    session.flush()
    session.expire(payment, ["allocations"])


def create_supplier_payment(
    session: Session,
    payload: dict,
    allocations: list[dict] | None = None,
    *,
    actor_context: ActorContext,
) -> SupplierPayment:
    require_permission(
        actor_context, "supplier_payments.manage", db_session=session
    )
    supplier_id = payload.get("supplier_id")
    supplier = session.get(Supplier, supplier_id) if supplier_id else None
    if supplier is None:
        raise PaymentValidationError("Select an existing supplier.")
    reference = clean_optional(payload.get("transaction_reference"), "Transaction reference", 100)
    method = payment_method(payload.get("payment_method"))
    bank_account_name = clean_optional(
        payload.get("bank_account_name"), "Bank/account name", 150
    )
    _ensure_unique_reference(
        session, reference, transaction_kind="payment",
        payment_method_value=method, bank_account_name=bank_account_name,
        register=False,
    )
    amount = money(payload.get("amount_paid"), "Payment amount", positive=True)
    payment_day = payment_date(payload.get("payment_date", date.today()), "Payment date")
    payment = SupplierPayment(
        payment_code=f"SPAY-PENDING-{uuid4().hex[:12]}",
        supplier_id=supplier.id,
        payment_date=payment_day,
        amount_paid=amount,
        payment_method=method,
        transaction_reference=reference,
        bank_account_name=bank_account_name,
        cheque_number=clean_optional(payload.get("cheque_number"), "Cheque number", 100),
        notes=clean_optional(payload.get("notes"), "Notes"),
        responsible_user=clean_optional(payload.get("responsible_user"), "Responsible user", 100),
        status="Draft",
    )
    with session.begin_nested():
        session.add(payment)
        session.flush()
        payment.payment_code = f"SPAY-{payment.id:06d}"
        _ensure_unique_reference(
            session, payment.transaction_reference,
            transaction_kind="payment", transaction_id=payment.id,
            payment_method_value=payment.payment_method,
            bank_account_name=payment.bank_account_name, register=False,
        )
        normalized = _normalize_allocations(allocations, payment_day)
        _replace_draft_allocations(
            session, payment, normalized, actor_context=actor_context
        )
        _append_payment_history(session, payment, "CREATED", None, "Draft", None, payment.responsible_user)
        session.flush()
    return payment


def get_supplier_payment_by_id(
    session: Session, payment_id: int, *, actor_context: ActorContext
) -> SupplierPayment | None:
    require_permission(
        actor_context, "supplier_payments.view", db_session=session
    )
    return session.scalar(
        select(SupplierPayment)
        .options(selectinload(SupplierPayment.allocations), selectinload(SupplierPayment.supplier))
        .where(SupplierPayment.id == payment_id)
    )


def update_supplier_payment(
    session: Session,
    payment_id: int,
    payload: dict,
    allocations: list[dict] | None = None,
    *,
    actor_context: ActorContext,
) -> SupplierPayment:
    require_permission(
        actor_context, "supplier_payments.manage", db_session=session
    )
    payment = get_supplier_payment_by_id(
        session, payment_id, actor_context=actor_context
    )
    if payment is None:
        raise PaymentValidationError(f"Payment #{payment_id} does not exist.")
    if payment.status != "Draft":
        raise PaymentValidationError("Only draft supplier payments can be edited.")
    supplier_id = payload.get("supplier_id", payment.supplier_id)
    if session.get(Supplier, supplier_id) is None:
        raise PaymentValidationError("Select an existing supplier.")
    reference = clean_optional(
        payload.get("transaction_reference", payment.transaction_reference),
        "Transaction reference", 100,
    )
    method = payment_method(payload.get("payment_method", payment.payment_method))
    bank_account_name = clean_optional(
        payload.get("bank_account_name", payment.bank_account_name),
        "Bank/account name", 150,
    )
    _ensure_unique_reference(
        session, reference, transaction_kind="payment", transaction_id=payment.id,
        payment_method_value=method, bank_account_name=bank_account_name,
        register=False,
    )
    day = payment_date(payload.get("payment_date", payment.payment_date), "Payment date")
    amount = money(payload.get("amount_paid", payment.amount_paid), "Payment amount", positive=True)
    normalized = (
        _normalize_allocations(allocations, day)
        if allocations is not None
        else [
            {
                "allocation_type": row.allocation_type,
                "purchase_id": row.purchase_id,
                "amount": row.amount,
                "allocation_date": row.allocation_date,
                "reference": row.reference,
            }
            for row in payment.allocations
        ]
    )
    with session.begin_nested():
        payment.supplier_id = supplier_id
        payment.payment_date = day
        payment.amount_paid = amount
        payment.payment_method = method
        payment.transaction_reference = reference
        _ensure_unique_reference(
            session, reference, transaction_kind="payment", transaction_id=payment.id,
            payment_method_value=payment.payment_method,
            bank_account_name=bank_account_name, register=False,
        )
        payment.bank_account_name = bank_account_name
        payment.cheque_number = clean_optional(
            payload.get("cheque_number", payment.cheque_number), "Cheque number", 100
        )
        payment.notes = clean_optional(payload.get("notes", payment.notes), "Notes")
        payment.responsible_user = clean_optional(
            payload.get("responsible_user", payment.responsible_user), "Responsible user", 100
        )
        _replace_draft_allocations(
            session, payment, normalized, actor_context=actor_context
        )
        _append_payment_history(
            session, payment, "UPDATED", "Draft", "Draft", "Draft payment edited.",
            payment.responsible_user,
        )
        session.flush()
    return payment


def add_supplier_payment_allocation(
    session: Session,
    payment_id: int,
    allocation: dict,
    *,
    actor_context: ActorContext,
) -> SupplierPaymentAllocation:
    require_permission(
        actor_context, "supplier_payments.manage", db_session=session
    )
    payment = get_supplier_payment_by_id(
        session, payment_id, actor_context=actor_context
    )
    if payment is None or payment.status != "Draft":
        raise PaymentValidationError("Allocations can only be added to an existing draft payment.")
    normalized = _normalize_allocations([allocation], payment.payment_date)[0]
    proposed = [
        {
            "allocation_type": row.allocation_type,
            "purchase_id": row.purchase_id,
            "amount": row.amount,
        }
        for row in payment.allocations
    ] + [normalized]
    _validate_allocation_targets(
        session, payment.supplier_id, proposed, actor_context=actor_context
    )
    if sum((row["amount"] for row in proposed), ZERO) > payment.amount_paid:
        raise PaymentValidationError("Allocations cannot exceed the supplier payment amount.")
    record = SupplierPaymentAllocation(
        payment_id=payment.id,
        purchase_id=normalized["purchase_id"],
        allocation_type=normalized["allocation_type"],
        allocation_source="Posting",
        allocation_date=normalized["allocation_date"],
        amount=normalized["amount"],
        reference=normalized["reference"],
    )
    session.add(record)
    session.flush()
    return record


def remove_supplier_payment_allocation(
    session: Session,
    payment_id: int,
    allocation_id: int,
    *,
    actor_context: ActorContext,
) -> None:
    require_permission(
        actor_context, "supplier_payments.manage", db_session=session
    )
    payment = get_supplier_payment_by_id(
        session, payment_id, actor_context=actor_context
    )
    if payment is None or payment.status != "Draft":
        raise PaymentValidationError("Allocations can only be removed from a draft payment.")
    allocation = session.get(SupplierPaymentAllocation, allocation_id)
    if allocation is None or allocation.payment_id != payment.id:
        raise PaymentValidationError("The selected allocation does not belong to this payment.")
    session.delete(allocation)
    session.flush()


def list_supplier_payments(
    session: Session,
    *,
    supplier_id: int | None = None,
    payment_method_filter: str | None = None,
    status: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    search: str | None = None,
    actor_context: ActorContext,
) -> list[SupplierPayment]:
    require_permission(
        actor_context, "supplier_payments.view", db_session=session
    )
    stmt = select(SupplierPayment).options(
        selectinload(SupplierPayment.supplier), selectinload(SupplierPayment.allocations)
    )
    if supplier_id is not None:
        stmt = stmt.where(SupplierPayment.supplier_id == supplier_id)
    if payment_method_filter:
        stmt = stmt.where(SupplierPayment.payment_method == payment_method_filter)
    if status and status != "ALL":
        stmt = stmt.where(SupplierPayment.status == status)
    if start_date:
        stmt = stmt.where(SupplierPayment.payment_date >= start_date)
    if end_date:
        stmt = stmt.where(SupplierPayment.payment_date <= end_date)
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.join(Supplier, Supplier.id == SupplierPayment.supplier_id).where(
            or_(
                SupplierPayment.payment_code.ilike(pattern),
                SupplierPayment.transaction_reference.ilike(pattern),
                Supplier.business_name.ilike(pattern),
                Supplier.supplier_code.ilike(pattern),
            )
        )
    return list(session.scalars(stmt.order_by(SupplierPayment.payment_date.desc(), SupplierPayment.id.desc())).unique())


def post_supplier_payment(
    session: Session,
    payment_id: int,
    *,
    actor_context: ActorContext,
    responsible_user: str | None = None,
) -> SupplierPayment:
    require_permission(
        actor_context, "supplier_payments.post", db_session=session
    )
    payment = get_supplier_payment_by_id(
        session, payment_id, actor_context=actor_context
    )
    if payment is None:
        raise PaymentValidationError(f"Payment #{payment_id} does not exist.")
    if payment.status != "Draft":
        raise PaymentValidationError("Only a draft supplier payment can be posted.")
    allocations = [
        {
            "allocation_type": row.allocation_type,
            "purchase_id": row.purchase_id,
            "amount": row.amount,
        }
        for row in payment.allocations
    ]
    if _allocation_total(session, payment.id) > payment.amount_paid:
        raise PaymentValidationError("Allocations cannot exceed the supplier payment amount.")
    user = clean_optional(responsible_user, "Responsible user", 100)
    with session.begin_nested():
        _validate_allocation_targets(
            session, payment.supplier_id, allocations,
            actor_context=actor_context, lock_targets=True
        )
        _ensure_unique_reference(
            session, payment.transaction_reference,
            transaction_kind="payment", transaction_id=payment.id,
            payment_method_value=payment.payment_method,
            bank_account_name=payment.bank_account_name,
        )
        previous = payment.status
        payment.status = "Posted"
        payment.responsible_user = user or payment.responsible_user
        payment.updated_at = datetime.now()
        _append_payment_history(
            session, payment, "POSTED", previous, "Posted", None, payment.responsible_user
        )
        session.flush()
        from services.accounting_posting_service import capture_source_event

        capture_source_event(
            session, "Supplier Payment", payment.id, "POSTED",
            actor_context=actor_context,
            responsible_user=payment.responsible_user,
        )
    return payment


def apply_supplier_advance(
    session: Session,
    payment_id: int,
    allocation: dict,
    *,
    actor_context: ActorContext,
    responsible_user: str | None = None,
) -> SupplierPaymentAllocation:
    require_permission(
        actor_context, "supplier_payments.post", db_session=session
    )
    payment = get_supplier_payment_by_id(
        session, payment_id, actor_context=actor_context
    )
    if payment is None or payment.status != "Posted":
        raise PaymentValidationError("Only a posted supplier payment can provide an advance.")
    normalized = _normalize_allocations([allocation], payment_date(
        allocation.get("allocation_date", date.today()), "Allocation date"
    ))[0]
    if normalized["amount"] > payment.amount_paid - _allocation_total(session, payment.id):
        raise PaymentValidationError("Allocation exceeds the unallocated supplier advance.")
    with session.begin_nested():
        active_allocations = (
            select(func.coalesce(func.sum(SupplierPaymentAllocation.amount), ZERO))
            .where(SupplierPaymentAllocation.payment_id == payment.id)
            .scalar_subquery()
        )
        locked = session.execute(
            update(SupplierPayment)
            .where(SupplierPayment.id == payment.id)
            .where(SupplierPayment.status == "Posted")
            .where(SupplierPayment.amount_paid - active_allocations >= normalized["amount"])
            .values(amount_paid=SupplierPayment.amount_paid)
            .execution_options(synchronize_session=False)
        )
        if locked.rowcount != 1:
            raise PaymentValidationError("Supplier advance changed; refresh and try again.")
        _validate_allocation_targets(
            session,
            payment.supplier_id,
            [normalized],
            actor_context=actor_context,
            lock_targets=True,
        )
        record = SupplierPaymentAllocation(
            payment_id=payment.id,
            purchase_id=normalized["purchase_id"],
            allocation_type=normalized["allocation_type"],
            allocation_source="Advance Application",
            allocation_date=normalized["allocation_date"],
            amount=normalized["amount"],
            reference=normalized["reference"],
        )
        session.add(record)
        _append_payment_history(
            session, payment, "ADVANCE_APPLIED", "Posted", "Posted",
            normalized["reference"], clean_optional(responsible_user, "Responsible user", 100),
        )
        session.flush()
        from services.accounting_posting_service import capture_source_event

        capture_source_event(
            session, "Supplier Payment", record.id, "ADVANCE_APPLIED",
            actor_context=actor_context,
            responsible_user=clean_optional(responsible_user, "Responsible user", 100),
        )
    return record


def void_supplier_payment(
    session: Session,
    payment_id: int,
    reason: str,
    *,
    actor_context: ActorContext,
    responsible_user: str | None = None,
) -> SupplierPayment:
    require_permission(
        actor_context, "supplier_payments.void", db_session=session
    )
    payment = get_supplier_payment_by_id(
        session, payment_id, actor_context=actor_context
    )
    if payment is None:
        raise PaymentValidationError(f"Payment #{payment_id} does not exist.")
    if payment.status != "Posted":
        raise PaymentValidationError("Only a posted supplier payment can be voided.")
    reason = clean_optional(reason, "Void reason")
    if not reason:
        raise PaymentValidationError("A reason is required to void a supplier payment.")
    user = clean_optional(responsible_user, "Responsible user", 100)
    with session.begin_nested():
        previous = payment.status
        payment.status = "Voided"
        payment.void_reason = reason
        payment.voided_at = datetime.now()
        payment.responsible_user = user or payment.responsible_user
        payment.updated_at = datetime.now()
        _append_payment_history(
            session, payment, "VOIDED", previous, "Voided", reason, payment.responsible_user
        )
        session.flush()
        from services.accounting_posting_service import reverse_source_event

        reverse_source_event(
            session, "Supplier Payment", payment.id, "VOIDED", reason=reason,
            reversal_date=date.today(), responsible_user=payment.responsible_user,
            actor_context=actor_context,
        )
    return payment


def get_supplier_payment_summary(
    session: Session, payment: SupplierPayment, *, actor_context: ActorContext
) -> dict:
    require_permission(
        actor_context, "supplier_payments.view", db_session=session
    )
    allocated = _allocation_total(session, payment.id)
    return {
        "payment": payment,
        "allocated_amount": allocated,
        "unallocated_amount": max(ZERO, payment.amount_paid - allocated),
        "allocations": list(payment.allocations),
    }


def get_supplier_financial_summary(
    session: Session, supplier_id: int, *, actor_context: ActorContext
) -> dict:
    require_permission(
        actor_context, "supplier_payments.view", db_session=session
    )
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise PaymentValidationError(f"Supplier #{supplier_id} does not exist.")
    opening = Decimal(str(supplier.opening_balance or 0)).quantize(CENT)
    opening_remaining = max(
        ZERO, opening - _posted_opening_allocated(session, supplier_id)
    ).quantize(CENT)
    purchases = session.scalars(
        select(Purchase).where(Purchase.supplier_id == supplier_id)
    ).all()
    purchase_balances = [calculate_purchase_balance(
        session, purchase.id, actor_context=actor_context
    ) for purchase in purchases]
    total_purchase_payables = sum((row["balance"] for row in purchase_balances), ZERO)
    historical_payments = sum(
        (Decimal(str(purchase.amount_paid)).quantize(CENT) for purchase in purchases), ZERO
    )
    posted_payment_records = session.scalars(
        select(SupplierPayment).where(
            SupplierPayment.supplier_id == supplier_id,
            SupplierPayment.status == "Posted",
        )
    ).all()
    total_new_payments = sum((row.amount_paid for row in posted_payment_records), ZERO)
    advances = sum(
        (
            max(ZERO, row.amount_paid - _allocation_total(session, row.id))
            for row in posted_payment_records
        ),
        ZERO,
    )
    total_payables = opening_remaining + total_purchase_payables
    return {
        "supplier_id": supplier_id,
        "original_opening_payable": opening,
        "remaining_opening_payable": opening_remaining,
        "purchase_payables": total_purchase_payables,
        "historical_purchase_payments": historical_payments,
        "new_supplier_payments": total_new_payments,
        "unallocated_advances": advances,
        "total_outstanding_payables": total_payables,
        "net_supplier_balance": total_payables - advances,
        "purchase_balances": purchase_balances,
    }


def get_supplier_statement(
    session: Session, supplier_id: int, *, actor_context: ActorContext
) -> list[dict]:
    require_permission(
        actor_context, "supplier_payments.view", db_session=session
    )
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise PaymentValidationError(f"Supplier #{supplier_id} does not exist.")
    rows = [{
        "date": None,
        "reference": "Opening Balance",
        "description": "Original opening payable",
        "debit": ZERO,
        "credit": Decimal(str(supplier.opening_balance or 0)),
        "status": "Opening",
    }]
    purchases = session.scalars(
        select(Purchase).where(Purchase.supplier_id == supplier_id)
    ).all()
    for purchase in purchases:
        balance = calculate_purchase_balance(
            session, purchase.id, actor_context=actor_context
        )
        rows.append({
            "date": purchase.purchase_date,
            "reference": purchase.purchase_code,
            "description": "Purchase payable",
            "debit": ZERO,
            "credit": purchase.total_amount,
            "status": f"Outstanding {balance['balance']}; historical paid {balance['historical_amount_paid']}",
        })
        if balance["historical_amount_paid"] > ZERO:
            rows.append({
                "date": purchase.purchase_date,
                "reference": purchase.purchase_code,
                "description": "Historical payment recorded on purchase (not a new payment transaction)",
                "debit": balance["historical_amount_paid"],
                "credit": ZERO,
                "status": "Historical",
            })
    for payment in list_supplier_payments(
        session, supplier_id=supplier_id, status="Posted",
        actor_context=actor_context,
    ):
        rows.append({
            "date": payment.payment_date,
            "reference": payment.payment_code,
            "description": f"Supplier payment — {payment.payment_method}",
            "debit": payment.amount_paid,
            "credit": ZERO,
            "status": payment.status,
        })
    rows.sort(key=lambda row: row["date"] or date.min)
    return rows


def get_supplier_payment_dashboard(
    session: Session,
    *,
    actor_context: ActorContext,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    require_permission(
        actor_context, "supplier_payments.view", db_session=session
    )
    stmt = select(SupplierPayment).where(SupplierPayment.status == "Posted")
    if start_date:
        stmt = stmt.where(SupplierPayment.payment_date >= start_date)
    if end_date:
        stmt = stmt.where(SupplierPayment.payment_date <= end_date)
    payments = session.scalars(stmt).all()
    supplier_ids = session.scalars(select(Supplier.id)).all()
    summaries = [
        get_supplier_financial_summary(
            session, supplier_id, actor_context=actor_context
        )
        for supplier_id in supplier_ids
    ]
    return {
        "total_payments": sum((row.amount_paid for row in payments), ZERO),
        "payment_count": len(payments),
        "unallocated_advances": sum((row["unallocated_advances"] for row in summaries), ZERO),
        "outstanding_purchases": sum((row["purchase_payables"] for row in summaries), ZERO),
    }
