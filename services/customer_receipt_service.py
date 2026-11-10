from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from database.models import (
    Customer,
    CustomerReceipt,
    CustomerReceiptAllocation,
    SalesInvoice,
)
from services.payment_common import (
    CENT,
    ZERO,
    PaymentValidationError,
    _append_receipt_history,
    clean_optional,
    _ensure_unique_reference,
    money,
    payment_date,
    payment_method,
)
from services.security_service import ActorContext, require_permission


_RECEIVABLE_INVOICE_STATUSES = ("Issued", "Partially Paid", "Paid", "Overdue")


def _allocation_total(session: Session, receipt_id: int) -> Decimal:
    return session.scalar(
        select(func.coalesce(func.sum(CustomerReceiptAllocation.amount), ZERO))
        .where(CustomerReceiptAllocation.receipt_id == receipt_id)
    ) or ZERO


def _posted_invoice_allocated(session: Session, invoice_id: int) -> Decimal:
    return session.scalar(
        select(func.coalesce(func.sum(CustomerReceiptAllocation.amount), ZERO))
        .join(CustomerReceipt, CustomerReceipt.id == CustomerReceiptAllocation.receipt_id)
        .where(
            CustomerReceiptAllocation.invoice_id == invoice_id,
            CustomerReceipt.status == "Posted",
        )
    ) or ZERO


def _posted_opening_allocated(session: Session, customer_id: int) -> Decimal:
    return session.scalar(
        select(func.coalesce(func.sum(CustomerReceiptAllocation.amount), ZERO))
        .join(CustomerReceipt, CustomerReceipt.id == CustomerReceiptAllocation.receipt_id)
        .where(
            CustomerReceipt.customer_id == customer_id,
            CustomerReceiptAllocation.allocation_type == "Opening Balance",
            CustomerReceipt.status == "Posted",
        )
    ) or ZERO


def calculate_invoice_balance(
    session: Session,
    invoice_id: int,
    *,
    actor_context: ActorContext,
    as_of: date | None = None,
) -> dict:
    require_permission(actor_context, "receipts.view", db_session=session)
    invoice = session.get(SalesInvoice, invoice_id)
    if invoice is None:
        raise PaymentValidationError(f"Sales invoice #{invoice_id} does not exist.")
    if invoice.status not in _RECEIVABLE_INVOICE_STATUSES:
        return {
            "invoice": invoice,
            "invoice_total": invoice.grand_total,
            "received": ZERO,
            "balance": ZERO,
            "payment_status": "Not Receivable",
        }
    received = _posted_invoice_allocated(session, invoice_id)
    balance = max(ZERO, (invoice.grand_total - received).quantize(CENT))
    if balance == ZERO:
        status = "Paid"
    elif received > ZERO:
        status = "Partially Paid"
    elif invoice.due_date and invoice.due_date < (as_of or date.today()):
        status = "Overdue"
    else:
        status = "Unpaid"
    return {
        "invoice": invoice,
        "invoice_total": invoice.grand_total,
        "received": received,
        "balance": balance,
        "payment_status": status,
    }


def get_customer_receipt_targets(
    session: Session, customer_id: int, *, actor_context: ActorContext
) -> list[dict]:
    require_permission(actor_context, "receipts.view", db_session=session)
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise PaymentValidationError(f"Customer #{customer_id} does not exist.")
    targets: list[dict] = []
    opening_remaining = max(
        ZERO, (Decimal(str(customer.opening_balance)) - _posted_opening_allocated(session, customer_id))
    ).quantize(CENT)
    if opening_remaining > ZERO:
        targets.append({
            "allocation_type": "Opening Balance",
            "invoice_id": None,
            "code": "OPENING BALANCE",
            "invoice_date": None,
            "due_date": None,
            "total": Decimal(str(customer.opening_balance)),
            "previously_received": _posted_opening_allocated(session, customer_id),
            "outstanding": opening_remaining,
        })
    invoices = session.scalars(
        select(SalesInvoice)
        .where(
            SalesInvoice.customer_id == customer_id,
            SalesInvoice.status.in_(_RECEIVABLE_INVOICE_STATUSES),
        )
        .order_by(SalesInvoice.invoice_date, SalesInvoice.id)
    ).all()
    for invoice in invoices:
        balance = calculate_invoice_balance(
            session, invoice.id, actor_context=actor_context
        )
        if balance["balance"] > ZERO:
            targets.append({
                "allocation_type": "Invoice",
                "invoice_id": invoice.id,
                "code": invoice.invoice_code,
                "invoice_date": invoice.invoice_date,
                "due_date": invoice.due_date,
                "total": invoice.grand_total,
                "previously_received": balance["received"],
                "outstanding": balance["balance"],
            })
    return targets


def _normalize_allocations(allocations: list[dict] | None, default_date: date) -> list[dict]:
    normalized = []
    for index, allocation in enumerate(allocations or [], start=1):
        allocation_type = str(
            allocation.get("allocation_type", allocation.get("type", ""))
        ).strip()
        if allocation_type not in ("Invoice", "Opening Balance"):
            raise PaymentValidationError(
                f"Allocation #{index} must target an invoice or opening balance."
            )
        invoice_id = allocation.get("invoice_id")
        if allocation_type == "Invoice":
            try:
                invoice_id = int(invoice_id)
            except (TypeError, ValueError) as exc:
                raise PaymentValidationError(f"Allocation #{index} requires an invoice.") from exc
        else:
            invoice_id = None
        normalized.append({
            "allocation_type": allocation_type,
            "invoice_id": invoice_id,
            "amount": money(allocation.get("amount"), f"Allocation #{index}", positive=True),
            "allocation_date": payment_date(
                allocation.get("allocation_date", default_date), f"Allocation #{index} date"
            ),
            "reference": clean_optional(allocation.get("reference"), "Allocation reference", 150),
        })
    return normalized


def _validate_allocation_targets(
    session: Session,
    customer_id: int,
    allocations: list[dict],
    *,
    source: str = "Posting",
    lock_targets: bool = False,
) -> None:
    by_invoice: dict[int, Decimal] = {}
    opening_amount = ZERO
    for allocation in allocations:
        if allocation["allocation_type"] == "Opening Balance":
            opening_amount += allocation["amount"]
        else:
            by_invoice[allocation["invoice_id"]] = (
                by_invoice.get(allocation["invoice_id"], ZERO) + allocation["amount"]
            )

    customer = session.get(Customer, customer_id)
    if customer is None:
        raise PaymentValidationError("The selected customer no longer exists.")
    if opening_amount:
        outstanding = Decimal(str(customer.opening_balance)) - _posted_opening_allocated(
            session, customer_id
        )
        if opening_amount > max(ZERO, outstanding):
            raise PaymentValidationError("Opening-balance allocation exceeds the remaining receivable.")
        if lock_targets:
            active = (
                select(func.coalesce(func.sum(CustomerReceiptAllocation.amount), ZERO))
                .join(CustomerReceipt, CustomerReceipt.id == CustomerReceiptAllocation.receipt_id)
                .where(
                    CustomerReceipt.customer_id == customer_id,
                    CustomerReceiptAllocation.allocation_type == "Opening Balance",
                    CustomerReceipt.status == "Posted",
                )
                .scalar_subquery()
            )
            result = session.execute(
                update(Customer)
                .where(Customer.id == customer_id)
                .where(Customer.opening_balance - active >= opening_amount)
                .values(opening_balance=Customer.opening_balance)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise PaymentValidationError("Opening-balance capacity changed; refresh and try again.")

    for invoice_id, amount in by_invoice.items():
        invoice = session.get(SalesInvoice, invoice_id)
        if (
            invoice is None
            or invoice.customer_id != customer_id
            or invoice.status not in _RECEIVABLE_INVOICE_STATUSES
        ):
            raise PaymentValidationError(
                f"Invoice #{invoice_id} is not an issued receivable for this customer."
            )
        if amount > max(ZERO, invoice.grand_total - _posted_invoice_allocated(session, invoice_id)):
            raise PaymentValidationError(
                f"Allocation exceeds the outstanding balance of invoice {invoice.invoice_code}."
            )
        if lock_targets:
            active = (
                select(func.coalesce(func.sum(CustomerReceiptAllocation.amount), ZERO))
                .join(CustomerReceipt, CustomerReceipt.id == CustomerReceiptAllocation.receipt_id)
                .where(
                    CustomerReceiptAllocation.invoice_id == invoice_id,
                    CustomerReceipt.status == "Posted",
                )
                .scalar_subquery()
            )
            result = session.execute(
                update(SalesInvoice)
                .where(SalesInvoice.id == invoice_id)
                .where(SalesInvoice.customer_id == customer_id)
                .where(SalesInvoice.status.in_(_RECEIVABLE_INVOICE_STATUSES))
                .where(SalesInvoice.grand_total - active >= amount)
                .values(grand_total=SalesInvoice.grand_total)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise PaymentValidationError(
                    f"Invoice {invoice.invoice_code} balance changed; refresh and try again."
                )


def _replace_draft_allocations(
    session: Session, receipt: CustomerReceipt, allocations: list[dict]
) -> None:
    total = sum((allocation["amount"] for allocation in allocations), ZERO)
    if total > receipt.amount_received:
        raise PaymentValidationError("Allocations cannot exceed the receipt amount.")
    _validate_allocation_targets(session, receipt.customer_id, allocations)
    session.query(CustomerReceiptAllocation).filter_by(receipt_id=receipt.id).delete(
        synchronize_session=False
    )
    session.flush()
    session.expire(receipt, ["allocations"])
    for allocation in allocations:
        session.add(CustomerReceiptAllocation(
            receipt_id=receipt.id,
            invoice_id=allocation["invoice_id"],
            allocation_type=allocation["allocation_type"],
            allocation_source="Posting",
            allocation_date=allocation["allocation_date"],
            amount=allocation["amount"],
            reference=allocation["reference"],
        ))
    session.flush()
    session.expire(receipt, ["allocations"])


def create_customer_receipt(
    session: Session,
    payload: dict,
    allocations: list[dict] | None = None,
    *,
    actor_context: ActorContext,
) -> CustomerReceipt:
    require_permission(actor_context, "receipts.manage", db_session=session)
    customer_id = payload.get("customer_id")
    customer = session.get(Customer, customer_id) if customer_id else None
    if customer is None:
        raise PaymentValidationError("Select an existing customer.")
    reference = clean_optional(payload.get("transaction_reference"), "Transaction reference", 100)
    method = payment_method(payload.get("payment_method"))
    bank_account_name = clean_optional(
        payload.get("bank_account_name"), "Bank/account name", 150
    )
    _ensure_unique_reference(
        session, reference, transaction_kind="receipt",
        payment_method_value=method, bank_account_name=bank_account_name,
        register=False,
    )
    amount = money(payload.get("amount_received"), "Receipt amount", positive=True)
    receipt_day = payment_date(payload.get("receipt_date", date.today()), "Receipt date")
    receipt = CustomerReceipt(
        receipt_code=f"RCPT-{uuid4().hex[:12]}",
        customer_id=customer.id,
        receipt_date=receipt_day,
        amount_received=amount,
        payment_method=method,
        transaction_reference=reference,
        bank_account_name=bank_account_name,
        cheque_number=clean_optional(payload.get("cheque_number"), "Cheque number", 100),
        notes=clean_optional(payload.get("notes"), "Notes"),
        responsible_user=clean_optional(payload.get("responsible_user"), "Responsible user", 100),
        status="Draft",
    )
    with session.begin_nested():
        session.add(receipt)
        session.flush()
        receipt.receipt_code = f"RCPT-{receipt.id:06d}"
        _ensure_unique_reference(
            session, receipt.transaction_reference,
            transaction_kind="receipt", transaction_id=receipt.id,
            payment_method_value=receipt.payment_method,
            bank_account_name=receipt.bank_account_name, register=False,
        )
        normalized = _normalize_allocations(allocations, receipt_day)
        _replace_draft_allocations(session, receipt, normalized)
        _append_receipt_history(session, receipt, "CREATED", None, "Draft", None, receipt.responsible_user)
        session.flush()
    return receipt


def update_customer_receipt(
    session: Session,
    receipt_id: int,
    payload: dict,
    allocations: list[dict] | None = None,
    *,
    actor_context: ActorContext,
) -> CustomerReceipt:
    require_permission(actor_context, "receipts.manage", db_session=session)
    receipt = get_customer_receipt_by_id(
        session, receipt_id, actor_context=actor_context
    )
    if receipt is None:
        raise PaymentValidationError(f"Receipt #{receipt_id} does not exist.")
    if receipt.status != "Draft":
        raise PaymentValidationError("Only draft receipts can be edited.")
    customer_id = payload.get("customer_id", receipt.customer_id)
    if session.get(Customer, customer_id) is None:
        raise PaymentValidationError("Select an existing customer.")
    reference = clean_optional(
        payload.get("transaction_reference", receipt.transaction_reference),
        "Transaction reference", 100,
    )
    method = payment_method(payload.get("payment_method", receipt.payment_method))
    bank_account_name = clean_optional(
        payload.get("bank_account_name", receipt.bank_account_name),
        "Bank/account name", 150,
    )
    _ensure_unique_reference(
        session, reference, transaction_kind="receipt", transaction_id=receipt.id,
        payment_method_value=method, bank_account_name=bank_account_name,
        register=False,
    )
    day = payment_date(payload.get("receipt_date", receipt.receipt_date), "Receipt date")
    amount = money(payload.get("amount_received", receipt.amount_received), "Receipt amount", positive=True)
    normalized = (
        _normalize_allocations(allocations, day)
        if allocations is not None
        else [
            {
                "allocation_type": row.allocation_type,
                "invoice_id": row.invoice_id,
                "amount": row.amount,
                "allocation_date": row.allocation_date,
                "reference": row.reference,
            }
            for row in receipt.allocations
        ]
    )
    with session.begin_nested():
        receipt.customer_id = customer_id
        receipt.receipt_date = day
        receipt.amount_received = amount
        receipt.payment_method = method
        receipt.transaction_reference = reference
        _ensure_unique_reference(
            session, reference, transaction_kind="receipt", transaction_id=receipt.id,
            payment_method_value=receipt.payment_method,
            bank_account_name=bank_account_name, register=False,
        )
        receipt.bank_account_name = bank_account_name
        receipt.cheque_number = clean_optional(
            payload.get("cheque_number", receipt.cheque_number), "Cheque number", 100
        )
        receipt.notes = clean_optional(payload.get("notes", receipt.notes), "Notes")
        receipt.responsible_user = clean_optional(
            payload.get("responsible_user", receipt.responsible_user), "Responsible user", 100
        )
        _replace_draft_allocations(session, receipt, normalized)
        _append_receipt_history(
            session, receipt, "UPDATED", "Draft", "Draft", "Draft receipt edited.",
            receipt.responsible_user,
        )
        session.flush()
    return receipt


def add_customer_receipt_allocation(
    session: Session,
    receipt_id: int,
    allocation: dict,
    *,
    actor_context: ActorContext,
) -> CustomerReceiptAllocation:
    require_permission(actor_context, "receipts.manage", db_session=session)
    receipt = get_customer_receipt_by_id(
        session, receipt_id, actor_context=actor_context
    )
    if receipt is None or receipt.status != "Draft":
        raise PaymentValidationError("Allocations can only be added to an existing draft receipt.")
    normalized = _normalize_allocations([allocation], receipt.receipt_date)[0]
    proposed = [
        {
            "allocation_type": row.allocation_type,
            "invoice_id": row.invoice_id,
            "amount": row.amount,
            "allocation_date": row.allocation_date,
            "reference": row.reference,
        }
        for row in receipt.allocations
    ] + [normalized]
    _validate_allocation_targets(session, receipt.customer_id, proposed)
    if sum((row["amount"] for row in proposed), ZERO) > receipt.amount_received:
        raise PaymentValidationError("Allocations cannot exceed the receipt amount.")
    record = CustomerReceiptAllocation(
        receipt_id=receipt.id,
        allocation_type=normalized["allocation_type"],
        invoice_id=normalized["invoice_id"],
        allocation_source="Posting",
        allocation_date=normalized["allocation_date"],
        amount=normalized["amount"],
        reference=normalized["reference"],
    )
    session.add(record)
    session.flush()
    return record


def remove_customer_receipt_allocation(
    session: Session,
    receipt_id: int,
    allocation_id: int,
    *,
    actor_context: ActorContext,
) -> None:
    require_permission(actor_context, "receipts.manage", db_session=session)
    receipt = get_customer_receipt_by_id(
        session, receipt_id, actor_context=actor_context
    )
    if receipt is None or receipt.status != "Draft":
        raise PaymentValidationError("Allocations can only be removed from a draft receipt.")
    allocation = session.get(CustomerReceiptAllocation, allocation_id)
    if allocation is None or allocation.receipt_id != receipt.id:
        raise PaymentValidationError("The selected allocation does not belong to this receipt.")
    session.delete(allocation)
    session.flush()


def get_customer_receipt_by_id(
    session: Session, receipt_id: int, *, actor_context: ActorContext
) -> CustomerReceipt | None:
    require_permission(actor_context, "receipts.view", db_session=session)
    return session.scalar(
        select(CustomerReceipt)
        .options(selectinload(CustomerReceipt.allocations), selectinload(CustomerReceipt.customer))
        .where(CustomerReceipt.id == receipt_id)
    )


def list_customer_receipts(
    session: Session,
    *,
    customer_id: int | None = None,
    payment_method_filter: str | None = None,
    status: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    search: str | None = None,
    actor_context: ActorContext,
) -> list[CustomerReceipt]:
    require_permission(actor_context, "receipts.view", db_session=session)
    stmt = select(CustomerReceipt).options(
        selectinload(CustomerReceipt.customer), selectinload(CustomerReceipt.allocations)
    )
    if customer_id is not None:
        stmt = stmt.where(CustomerReceipt.customer_id == customer_id)
    if payment_method_filter:
        stmt = stmt.where(CustomerReceipt.payment_method == payment_method_filter)
    if status and status != "ALL":
        stmt = stmt.where(CustomerReceipt.status == status)
    if start_date:
        stmt = stmt.where(CustomerReceipt.receipt_date >= start_date)
    if end_date:
        stmt = stmt.where(CustomerReceipt.receipt_date <= end_date)
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.join(Customer, Customer.id == CustomerReceipt.customer_id).where(
            or_(
                CustomerReceipt.receipt_code.ilike(pattern),
                CustomerReceipt.transaction_reference.ilike(pattern),
                Customer.customer_name.ilike(pattern),
                Customer.customer_code.ilike(pattern),
            )
        )
    return list(session.scalars(stmt.order_by(CustomerReceipt.receipt_date.desc(), CustomerReceipt.id.desc())).unique())


def post_customer_receipt(
    session: Session,
    receipt_id: int,
    *,
    actor_context: ActorContext,
    responsible_user: str | None = None,
) -> CustomerReceipt:
    require_permission(actor_context, "receipts.post", db_session=session)
    receipt = get_customer_receipt_by_id(
        session, receipt_id, actor_context=actor_context
    )
    if receipt is None:
        raise PaymentValidationError(f"Receipt #{receipt_id} does not exist.")
    if receipt.status != "Draft":
        raise PaymentValidationError("Only a draft receipt can be posted.")
    allocations = [
        {
            "allocation_type": row.allocation_type,
            "invoice_id": row.invoice_id,
            "amount": row.amount,
        }
        for row in receipt.allocations
    ]
    if _allocation_total(session, receipt.id) > receipt.amount_received:
        raise PaymentValidationError("Allocations cannot exceed the receipt amount.")
    user = clean_optional(responsible_user, "Responsible user", 100)
    with session.begin_nested():
        _validate_allocation_targets(
            session, receipt.customer_id, allocations, lock_targets=True
        )
        _ensure_unique_reference(
            session, receipt.transaction_reference,
            transaction_kind="receipt", transaction_id=receipt.id,
            payment_method_value=receipt.payment_method,
            bank_account_name=receipt.bank_account_name,
        )
        previous = receipt.status
        receipt.status = "Posted"
        receipt.responsible_user = user or receipt.responsible_user
        receipt.updated_at = datetime.now()
        _append_receipt_history(
            session, receipt, "POSTED", previous, "Posted", None, receipt.responsible_user
        )
        session.flush()
        from services.accounting_posting_service import capture_source_event

        capture_source_event(
            session, "Customer Receipt", receipt.id, "POSTED",
            actor_context=actor_context,
            responsible_user=receipt.responsible_user,
        )
    return receipt


def apply_customer_advance(
    session: Session,
    receipt_id: int,
    allocation: dict,
    *,
    actor_context: ActorContext,
    responsible_user: str | None = None,
) -> CustomerReceiptAllocation:
    require_permission(actor_context, "receipts.post", db_session=session)
    receipt = get_customer_receipt_by_id(
        session, receipt_id, actor_context=actor_context
    )
    if receipt is None or receipt.status != "Posted":
        raise PaymentValidationError("Only a posted receipt can provide an advance.")
    normalized = _normalize_allocations([allocation], payment_date(
        allocation.get("allocation_date", date.today()), "Allocation date"
    ))[0]
    if normalized["amount"] > receipt.amount_received - _allocation_total(session, receipt.id):
        raise PaymentValidationError("Allocation exceeds the unallocated customer advance.")
    with session.begin_nested():
        active_allocations = (
            select(func.coalesce(func.sum(CustomerReceiptAllocation.amount), ZERO))
            .where(CustomerReceiptAllocation.receipt_id == receipt.id)
            .scalar_subquery()
        )
        locked = session.execute(
            update(CustomerReceipt)
            .where(CustomerReceipt.id == receipt.id)
            .where(CustomerReceipt.status == "Posted")
            .where(CustomerReceipt.amount_received - active_allocations >= normalized["amount"])
            .values(amount_received=CustomerReceipt.amount_received)
            .execution_options(synchronize_session=False)
        )
        if locked.rowcount != 1:
            raise PaymentValidationError("Customer advance changed; refresh and try again.")
        _validate_allocation_targets(
            session,
            receipt.customer_id,
            [normalized],
            source="Advance Application",
            lock_targets=True,
        )
        record = CustomerReceiptAllocation(
            receipt_id=receipt.id,
            invoice_id=normalized["invoice_id"],
            allocation_type=normalized["allocation_type"],
            allocation_source="Advance Application",
            allocation_date=normalized["allocation_date"],
            amount=normalized["amount"],
            reference=normalized["reference"],
        )
        session.add(record)
        _append_receipt_history(
            session, receipt, "ADVANCE_APPLIED", "Posted", "Posted",
            normalized["reference"], clean_optional(responsible_user, "Responsible user", 100),
        )
        session.flush()
        from services.accounting_posting_service import capture_source_event

        capture_source_event(
            session, "Customer Receipt", record.id, "ADVANCE_APPLIED",
            actor_context=actor_context,
            responsible_user=clean_optional(responsible_user, "Responsible user", 100),
        )
    return record


def void_customer_receipt(
    session: Session,
    receipt_id: int,
    reason: str,
    *,
    actor_context: ActorContext,
    responsible_user: str | None = None,
) -> CustomerReceipt:
    require_permission(actor_context, "receipts.void", db_session=session)
    receipt = get_customer_receipt_by_id(
        session, receipt_id, actor_context=actor_context
    )
    if receipt is None:
        raise PaymentValidationError(f"Receipt #{receipt_id} does not exist.")
    if receipt.status != "Posted":
        raise PaymentValidationError("Only a posted receipt can be voided.")
    reason = clean_optional(reason, "Void reason")
    if not reason:
        raise PaymentValidationError("A reason is required to void a receipt.")
    user = clean_optional(responsible_user, "Responsible user", 100)
    with session.begin_nested():
        previous = receipt.status
        receipt.status = "Voided"
        receipt.void_reason = reason
        receipt.voided_at = datetime.now()
        receipt.responsible_user = user or receipt.responsible_user
        receipt.updated_at = datetime.now()
        _append_receipt_history(session, receipt, "VOIDED", previous, "Voided", reason, receipt.responsible_user)
        session.flush()
        from services.accounting_posting_service import reverse_source_event

        reverse_source_event(
            session, "Customer Receipt", receipt.id, "VOIDED", reason=reason,
            reversal_date=date.today(), responsible_user=receipt.responsible_user,
            actor_context=actor_context,
        )
    return receipt


def get_customer_receipt_summary(
    session: Session, receipt: CustomerReceipt, *, actor_context: ActorContext
) -> dict:
    require_permission(actor_context, "receipts.view", db_session=session)
    allocated = _allocation_total(session, receipt.id)
    return {
        "receipt": receipt,
        "allocated_amount": allocated,
        "unallocated_amount": max(ZERO, receipt.amount_received - allocated),
        "allocations": list(receipt.allocations),
    }


def get_customer_financial_summary(
    session: Session,
    customer_id: int,
    *,
    actor_context: ActorContext,
    as_of: date | None = None,
) -> dict:
    require_permission(actor_context, "receipts.view", db_session=session)
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise PaymentValidationError(f"Customer #{customer_id} does not exist.")
    invoices = session.scalars(
        select(SalesInvoice).where(
            SalesInvoice.customer_id == customer_id,
            SalesInvoice.status.in_(_RECEIVABLE_INVOICE_STATUSES),
        )
    ).all()
    invoice_balances = [
        calculate_invoice_balance(
            session, invoice.id, actor_context=actor_context, as_of=as_of
        )
        for invoice in invoices
    ]
    original_opening = Decimal(str(customer.opening_balance)).quantize(CENT)
    opening_remaining = max(
        ZERO, original_opening - _posted_opening_allocated(session, customer_id)
    ).quantize(CENT)
    invoice_receivables = sum((row["balance"] for row in invoice_balances), ZERO)
    posted_receipts = session.scalar(
        select(func.coalesce(func.sum(CustomerReceipt.amount_received), ZERO)).where(
            CustomerReceipt.customer_id == customer_id, CustomerReceipt.status == "Posted"
        )
    ) or ZERO
    advances = ZERO
    receipts = session.scalars(
        select(CustomerReceipt).where(
            CustomerReceipt.customer_id == customer_id, CustomerReceipt.status == "Posted"
        )
    ).all()
    for receipt in receipts:
        advances += max(ZERO, receipt.amount_received - _allocation_total(session, receipt.id))
    total_receivables = opening_remaining + invoice_receivables
    return {
        "customer_id": customer_id,
        "original_opening_receivable": original_opening,
        "remaining_opening_receivable": opening_remaining,
        "issued_invoice_receivables": invoice_receivables,
        "total_posted_receipts": posted_receipts,
        "unallocated_advances": advances,
        "total_outstanding_receivables": total_receivables,
        "net_customer_balance": total_receivables - advances,
        "invoice_balances": invoice_balances,
    }


def get_customer_statement(
    session: Session, customer_id: int, *, actor_context: ActorContext
) -> list[dict]:
    require_permission(actor_context, "receipts.view", db_session=session)
    if session.get(Customer, customer_id) is None:
        raise PaymentValidationError(f"Customer #{customer_id} does not exist.")
    rows = [{
        "date": None,
        "reference": "Opening Balance",
        "description": "Original opening receivable",
        "debit": Decimal(str(session.get(Customer, customer_id).opening_balance)),
        "credit": ZERO,
        "status": "Opening",
    }]
    invoices = session.scalars(
        select(SalesInvoice).where(SalesInvoice.customer_id == customer_id)
    ).all()
    for invoice in invoices:
        if invoice.status in _RECEIVABLE_INVOICE_STATUSES:
            rows.append({
                "date": invoice.invoice_date,
                "reference": invoice.invoice_code,
                "description": "Issued sales invoice",
                "debit": invoice.grand_total,
                "credit": ZERO,
                "status": calculate_invoice_balance(
                    session, invoice.id, actor_context=actor_context
                )["payment_status"],
            })
    receipts = list_customer_receipts(
        session, customer_id=customer_id, status="Posted",
        actor_context=actor_context,
    )
    for receipt in receipts:
        rows.append({
            "date": receipt.receipt_date,
            "reference": receipt.receipt_code,
            "description": f"Customer receipt — {receipt.payment_method}",
            "debit": ZERO,
            "credit": receipt.amount_received,
            "status": receipt.status,
        })
    rows.sort(key=lambda row: row["date"] or date.min)
    return rows


def get_customer_receipt_dashboard(
    session: Session,
    *,
    actor_context: ActorContext,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    require_permission(actor_context, "receipts.view", db_session=session)
    stmt = select(CustomerReceipt).where(CustomerReceipt.status == "Posted")
    if start_date:
        stmt = stmt.where(CustomerReceipt.receipt_date >= start_date)
    if end_date:
        stmt = stmt.where(CustomerReceipt.receipt_date <= end_date)
    receipts = session.scalars(stmt).all()
    customer_ids = session.scalars(select(Customer.id)).all()
    summaries = [
        get_customer_financial_summary(
            session, customer_id, actor_context=actor_context
        )
        for customer_id in customer_ids
    ]
    invoice_states = [
        row
        for summary in summaries
        for row in summary["invoice_balances"]
    ]
    return {
        "total_receipts": sum((row.amount_received for row in receipts), ZERO),
        "receipt_count": len(receipts),
        "unallocated_advances": sum((row["unallocated_advances"] for row in summaries), ZERO),
        "unpaid_invoices": sum(row["payment_status"] == "Unpaid" for row in invoice_states),
        "partially_paid_invoices": sum(row["payment_status"] == "Partially Paid" for row in invoice_states),
        "overdue_invoices": sum(row["payment_status"] == "Overdue" for row in invoice_states),
    }
