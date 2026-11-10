from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from database.models import (
    AccountingException,
    CustomerReceipt,
    CustomerReceiptAllocation,
    Expense,
    ExpenseCategory,
    ExpensePayment,
    JournalEntry,
    Purchase,
    SalesInvoice,
    SalesOrderFulfilment,
    SalesOrderFulfilmentItem,
    SupplierPayment,
    SupplierPaymentAllocation,
    DismantlingEntry,
)
from services.accounting_common import (
    AccountingValidationError,
    ZERO,
    accounting_money,
    _get_mapped_account,
)
from services.journal_service import _create_posted_source_journal
from services.security_service import ActorContext, require_permission


def payment_account_mapping_key(payment_method: str, bank_account_name: str | None) -> str:
    method = " ".join(str(payment_method or "").strip().casefold().split())
    account = " ".join(str(bank_account_name or "").strip().casefold().split())
    if method == "cash":
        account = account or "cash in hand"
    if not account:
        raise AccountingValidationError(
            f"Select and map the account used for {payment_method or 'this payment method'}."
        )
    return f"payment:{method}:{account}"


def _account_line(
    session: Session, mapping_key: str, amount: object, *, side: str,
    description: str | None = None, party_type: str | None = None,
    party_id: int | None = None, source_reference: str | None = None,
) -> dict | None:
    value = accounting_money(amount, f"{mapping_key} amount")
    if value == ZERO:
        return None
    account = _get_mapped_account(session, mapping_key)
    return {
        "account_id": account.id,
        "description": description,
        "debit": value if side == "debit" else ZERO,
        "credit": value if side == "credit" else ZERO,
        "party_type": party_type,
        "party_id": party_id,
        "source_reference": source_reference,
    }


def _require_source(session: Session, model, source_id: int, label: str):
    source = session.get(model, source_id)
    if source is None:
        raise AccountingValidationError(f"{label} #{source_id} does not exist.")
    return source


def _build_source_event(
    session: Session, source_module: str, source_id: int, event_type: str
) -> tuple[date, str, str | None, list[dict], str | None]:
    if source_module == "Purchase" and event_type == "CREATED":
        purchase = _require_source(session, Purchase, source_id, "Purchase")
        gross = accounting_money(purchase.total_amount, "Purchase total")
        paid = accounting_money(purchase.amount_paid or ZERO, "Historical initial payment")
        if paid > gross:
            raise AccountingValidationError("Purchase initial payment exceeds the purchase total.")
        lines = [
            _account_line(
                session, "inventory:materials_awaiting_dismantling", gross,
                side="debit", description=f"Materials purchased — {purchase.purchase_code}",
                party_type="Supplier", party_id=purchase.supplier_id,
                source_reference=purchase.purchase_code,
            ),
            _account_line(
                session, "control:accounts_payable", gross - paid,
                side="credit", description=f"Purchase payable — {purchase.purchase_code}",
                party_type="Supplier", party_id=purchase.supplier_id,
                source_reference=purchase.purchase_code,
            ),
        ]
        if paid > ZERO:
            lines.append(_account_line(
                session,
                payment_account_mapping_key(purchase.payment_method, None),
                paid,
                side="credit",
                description=f"Initial payment recorded with purchase {purchase.purchase_code}",
                party_type="Supplier",
                party_id=purchase.supplier_id,
                source_reference=purchase.purchase_code,
            ))
        return purchase.purchase_date, f"Purchase {purchase.purchase_code}", purchase.purchase_code, [x for x in lines if x], None

    if source_module == "Sales Invoice" and event_type == "ISSUED":
        invoice = _require_source(session, SalesInvoice, source_id, "Sales invoice")
        if invoice.status != "Issued":
            raise AccountingValidationError("Only an issued invoice can be posted to accounting.")
        lines = [
            _account_line(
                session, "control:accounts_receivable", invoice.grand_total, side="debit",
                description=f"Receivable — {invoice.invoice_code}",
                party_type="Customer", party_id=invoice.customer_id,
                source_reference=invoice.invoice_code,
            ),
            _account_line(
                session, "income:sales_revenue", invoice.taxable_amount + invoice.additional_charges,
                side="credit", description=f"Sales revenue — {invoice.invoice_code}",
                party_type="Customer", party_id=invoice.customer_id,
                source_reference=invoice.invoice_code,
            ),
            _account_line(
                session, "tax:output_cgst", invoice.cgst_amount, side="credit",
                description="Output CGST", party_type="Customer",
                party_id=invoice.customer_id, source_reference=invoice.invoice_code,
            ),
            _account_line(
                session, "tax:output_sgst", invoice.sgst_amount, side="credit",
                description="Output SGST", party_type="Customer",
                party_id=invoice.customer_id, source_reference=invoice.invoice_code,
            ),
            _account_line(
                session, "tax:output_igst", invoice.igst_amount, side="credit",
                description="Output IGST", party_type="Customer",
                party_id=invoice.customer_id, source_reference=invoice.invoice_code,
            ),
        ]
        return invoice.invoice_date, f"Invoice issued {invoice.invoice_code}", invoice.invoice_code, [x for x in lines if x], None

    if source_module == "Customer Receipt":
        receipt = _require_source(session, CustomerReceipt, source_id, "Customer receipt")
        if event_type == "POSTED":
            if receipt.status != "Posted":
                raise AccountingValidationError("Only a posted customer receipt can be accounted.")
            allocations = list(session.scalars(
                select(CustomerReceiptAllocation).where(
                    CustomerReceiptAllocation.receipt_id == receipt.id
                )
            ))
            allocated = sum((accounting_money(a.amount) for a in allocations), ZERO)
            received = accounting_money(receipt.amount_received)
            if allocated > received:
                raise AccountingValidationError("Receipt allocations exceed the received amount.")
            advance = received - allocated
            lines = [
                _account_line(
                    session, payment_account_mapping_key(receipt.payment_method, receipt.bank_account_name),
                    received, side="debit", description=f"Receipt {receipt.receipt_code}",
                    party_type="Customer", party_id=receipt.customer_id,
                    source_reference=receipt.receipt_code,
                ),
                _account_line(
                    session, "control:accounts_receivable", allocated, side="credit",
                    description=f"Allocated receipt {receipt.receipt_code}",
                    party_type="Customer", party_id=receipt.customer_id,
                    source_reference=receipt.receipt_code,
                ),
                _account_line(
                    session, "control:customer_advances", advance, side="credit",
                    description=f"Unallocated customer advance {receipt.receipt_code}",
                    party_type="Customer", party_id=receipt.customer_id,
                    source_reference=receipt.receipt_code,
                ),
            ]
            return receipt.receipt_date, f"Customer receipt {receipt.receipt_code}", receipt.receipt_code, [x for x in lines if x], None
        if event_type == "ADVANCE_APPLIED":
            allocation = _require_source(
                session, CustomerReceiptAllocation, source_id, "Customer receipt allocation"
            )
            receipt = _require_source(session, CustomerReceipt, allocation.receipt_id, "Customer receipt")
            if receipt.status != "Posted" or allocation.allocation_source != "Advance Application":
                raise AccountingValidationError("Only a posted advance application can be accounted.")
            amount = accounting_money(allocation.amount)
            lines = [
                _account_line(
                    session, "control:customer_advances", amount, side="debit",
                    description="Customer advance applied",
                    party_type="Customer", party_id=receipt.customer_id,
                    source_reference=allocation.reference or receipt.receipt_code,
                ),
                _account_line(
                    session, "control:accounts_receivable", amount, side="credit",
                    description="Customer advance applied to receivable",
                    party_type="Customer", party_id=receipt.customer_id,
                    source_reference=allocation.reference or receipt.receipt_code,
                ),
            ]
            return allocation.allocation_date, f"Customer advance applied — {receipt.receipt_code}", allocation.reference or receipt.receipt_code, [x for x in lines if x], None

    if source_module == "Supplier Payment":
        payment = _require_source(session, SupplierPayment, source_id, "Supplier payment")
        if event_type == "POSTED":
            if payment.status != "Posted":
                raise AccountingValidationError("Only a posted supplier payment can be accounted.")
            allocations = list(session.scalars(
                select(SupplierPaymentAllocation).where(
                    SupplierPaymentAllocation.payment_id == payment.id
                )
            ))
            allocated = sum((accounting_money(a.amount) for a in allocations), ZERO)
            paid = accounting_money(payment.amount_paid)
            if allocated > paid:
                raise AccountingValidationError("Payment allocations exceed the paid amount.")
            advance = paid - allocated
            lines = [
                _account_line(
                    session, "control:accounts_payable", allocated, side="debit",
                    description=f"Allocated supplier payment {payment.payment_code}",
                    party_type="Supplier", party_id=payment.supplier_id,
                    source_reference=payment.payment_code,
                ),
                _account_line(
                    session, "asset:supplier_advances", advance, side="debit",
                    description=f"Unallocated supplier advance {payment.payment_code}",
                    party_type="Supplier", party_id=payment.supplier_id,
                    source_reference=payment.payment_code,
                ),
                _account_line(
                    session, payment_account_mapping_key(payment.payment_method, payment.bank_account_name),
                    paid, side="credit", description=f"Supplier payment {payment.payment_code}",
                    party_type="Supplier", party_id=payment.supplier_id,
                    source_reference=payment.payment_code,
                ),
            ]
            return payment.payment_date, f"Supplier payment {payment.payment_code}", payment.payment_code, [x for x in lines if x], None
        if event_type == "ADVANCE_APPLIED":
            allocation = _require_source(
                session, SupplierPaymentAllocation, source_id, "Supplier payment allocation"
            )
            payment = _require_source(session, SupplierPayment, allocation.payment_id, "Supplier payment")
            if payment.status != "Posted" or allocation.allocation_source != "Advance Application":
                raise AccountingValidationError("Only a posted advance application can be accounted.")
            amount = accounting_money(allocation.amount)
            lines = [
                _account_line(
                    session, "control:accounts_payable", amount, side="debit",
                    description="Supplier advance applied to payable",
                    party_type="Supplier", party_id=payment.supplier_id,
                    source_reference=allocation.reference or payment.payment_code,
                ),
                _account_line(
                    session, "asset:supplier_advances", amount, side="credit",
                    description="Supplier advance applied",
                    party_type="Supplier", party_id=payment.supplier_id,
                    source_reference=allocation.reference or payment.payment_code,
                ),
            ]
            return allocation.allocation_date, f"Supplier advance applied — {payment.payment_code}", allocation.reference or payment.payment_code, [x for x in lines if x], None

    if source_module == "Expense" and event_type == "POSTED":
        expense = _require_source(session, Expense, source_id, "Expense")
        if expense.status != "Posted":
            raise AccountingValidationError("Only a posted expense can be accounted.")
        category = session.get(ExpenseCategory, expense.category_id)
        if category is None:
            raise AccountingValidationError("Expense category is missing.")
        expense_mapping = f"expense_category:{category.name.casefold()}"
        amount = accounting_money(expense.total_amount, "Expense total")
        lines = [
            _account_line(
                session, expense_mapping,
                expense.total_amount,
                side="debit", description=expense.title,
                party_type="Supplier" if expense.supplier_id else None,
                party_id=expense.supplier_id,
                source_reference=expense.expense_code,
            ),
            _account_line(
                session, "control:expense_payables", amount, side="credit",
                description=f"Expense payable — {expense.expense_code}",
                party_type="Supplier" if expense.supplier_id else None,
                party_id=expense.supplier_id,
                source_reference=expense.expense_code,
            ),
        ]
        # Taxable input tax is not separately recognized unless an explicit
        # recoverable-tax model and supporting source fields are available.
        if expense.tax_amount:
            raise AccountingValidationError(
                "Expense tax amount has no verified recoverability classification or input-tax mapping."
            )
        return expense.expense_date, f"Expense posted {expense.expense_code}", expense.expense_code, [x for x in lines if x], None

    if source_module == "Expense Payment":
        payment = _require_source(session, ExpensePayment, source_id, "Expense payment")
        if event_type != "POSTED" or payment.status != "Posted":
            raise AccountingValidationError("Only a posted expense payment can be accounted.")
        expense = _require_source(session, Expense, payment.expense_id, "Expense")
        lines = [
            _account_line(
                session, "control:expense_payables", payment.amount_paid, side="debit",
                description=f"Expense payment {payment.payment_code}",
                source_reference=payment.payment_code,
            ),
            _account_line(
                session, payment_account_mapping_key(payment.payment_method, payment.bank_account_name),
                payment.amount_paid, side="credit",
                description=f"Expense payment {payment.payment_code}",
                source_reference=payment.payment_code,
            ),
        ]
        return payment.payment_date, f"Expense payment — {expense.expense_code}", payment.payment_code, [x for x in lines if x], None

    if source_module == "Sales Dispatch" and event_type == "DISPATCH":
        fulfilment_item = _require_source(
            session, SalesOrderFulfilmentItem, source_id, "Sales dispatch line"
        )
        if (
            fulfilment_item.accounting_cost_status != "Verified"
            or fulfilment_item.unit_cost_snapshot is None
            or fulfilment_item.unit_cost_snapshot <= ZERO
        ):
            raise AccountingValidationError(
                "No reliable positive historical cost was available at dispatch; COGS is awaiting review."
            )
        fulfilment = _require_source(
            session, SalesOrderFulfilment, fulfilment_item.fulfilment_id, "Sales fulfilment"
        )
        amount = accounting_money(
            fulfilment_item.quantity * fulfilment_item.unit_cost_snapshot,
            "Dispatch cost",
        )
        lines = [
            _account_line(
                session, "expense:cost_of_goods_sold", amount, side="debit",
                description="Cost of goods sold at dispatch",
                source_reference=fulfilment.fulfilment_code,
            ),
            _account_line(
                session, "inventory:recovered_components", amount, side="credit",
                description="Recovered inventory dispatched",
                source_reference=fulfilment.fulfilment_code,
            ),
        ]
        return fulfilment.fulfilment_date.date(), f"Cost of goods sold — {fulfilment.fulfilment_code}", fulfilment.fulfilment_code, [x for x in lines if x], None

    if source_module == "Dismantling" and event_type == "APPROVED":
        entry = _require_source(session, DismantlingEntry, source_id, "Dismantling entry")
        raise AccountingValidationError(
            f"Approved batch {entry.dismantling_code} has no approved source-cost allocation method; estimated recovery value is not a cost basis."
        )

    raise AccountingValidationError(
        f"Unsupported accounting event {source_module}:{event_type}."
    )


def _upsert_exception(
    session: Session, source_module: str, source_id: int, event_type: str,
    reference: str | None, reason: str,
) -> AccountingException:
    item = session.scalar(select(AccountingException).where(
        AccountingException.source_module == source_module,
        AccountingException.source_transaction_id == source_id,
        AccountingException.source_event_type == event_type,
    ))
    if item is None:
        item = AccountingException(
            source_module=source_module,
            source_transaction_id=source_id,
            source_event_type=event_type,
            source_reference=reference,
            reason=reason,
            status="Open",
        )
        session.add(item)
    else:
        item.source_reference = reference or item.source_reference
        item.reason = reason
        item.status = "Open"
        item.resolved_at = None
    session.flush()
    return item


def _source_permission(source_module: str, event_type: str) -> str:
    permissions = {
        ("Purchase", "CREATED"): "purchases.post",
        ("Sales Invoice", "ISSUED"): "invoices.issue",
        ("Sales Invoice", "VOIDED"): "invoices.void",
        ("Customer Receipt", "POSTED"): "receipts.post",
        ("Customer Receipt", "ADVANCE_APPLIED"): "receipts.post",
        ("Customer Receipt", "VOIDED"): "receipts.void",
        ("Supplier Payment", "POSTED"): "supplier_payments.post",
        ("Supplier Payment", "ADVANCE_APPLIED"): "supplier_payments.post",
        ("Supplier Payment", "VOIDED"): "supplier_payments.void",
        ("Expense", "POSTED"): "expenses.post",
        ("Expense", "VOIDED"): "expenses.void",
        ("Expense Payment", "POSTED"): "expenses.post",
        ("Expense Payment", "VOIDED"): "expenses.void",
        ("Sales Dispatch", "DISPATCH"): "sales.dispatch",
        ("Dismantling", "APPROVED"): "dismantling.approve",
    }
    try:
        return permissions[(source_module, event_type)]
    except KeyError as exc:
        raise AccountingValidationError(
            f"Unsupported accounting event {source_module}:{event_type}."
        ) from exc


def _capture_source_event(
    session: Session, source_module: str, source_id: int, event_type: str,
    *, actor_context: ActorContext, authorization_permission: str | None = None,
    responsible_user: str | None = None,
) -> JournalEntry | AccountingException:
    permission = authorization_permission or _source_permission(
        source_module, event_type
    )
    if permission not in {
        _source_permission(source_module, event_type),
        "accounting.exceptions.manage",
    }:
        raise AccountingValidationError("Invalid source-event authorization.")
    require_permission(actor_context, permission, db_session=session)
    existing = session.scalar(select(JournalEntry).where(
        JournalEntry.source_module == source_module,
        JournalEntry.source_transaction_id == source_id,
        JournalEntry.source_event_type == event_type,
    ))
    if existing is not None:
        return existing
    reference = None
    try:
        day, description, reference, lines, user = _build_source_event(
            session, source_module, source_id, event_type
        )
        journal = _create_posted_source_journal(
            session,
            journal_date=day,
            description=description,
            reference_number=reference,
            source_module=source_module,
            source_transaction_id=source_id,
            source_event_type=event_type,
            lines=lines,
            actor_context=actor_context,
            authorization_permission=permission,
            responsible_user=responsible_user or user,
        )
        exception = session.scalar(select(AccountingException).where(
            AccountingException.source_module == source_module,
            AccountingException.source_transaction_id == source_id,
            AccountingException.source_event_type == event_type,
            AccountingException.status == "Open",
        ))
        if exception is not None:
            exception.status = "Resolved"
            exception.resolved_at = datetime.now()
            session.flush()
        return journal
    except AccountingValidationError as exc:
        return _upsert_exception(
            session, source_module, source_id, event_type, reference, str(exc)
        )


def capture_source_event(
    session: Session, source_module: str, source_id: int, event_type: str,
    *, responsible_user: str | None = None,
    actor_context: ActorContext,
) -> JournalEntry | AccountingException:
    return _capture_source_event(
        session, source_module, source_id, event_type,
        actor_context=actor_context, responsible_user=responsible_user,
    )


def _reverse_source_event(
    session: Session, source_module: str, source_id: int, void_event_type: str,
    *, actor_context: ActorContext, authorization_permission: str | None = None,
    reason: str, reversal_date: date,
    responsible_user: str | None = None,
) -> JournalEntry | AccountingException | None:
    permission = authorization_permission or _source_permission(
        source_module, void_event_type
    )
    if permission != "accounting.exceptions.manage" and permission != _source_permission(
        source_module, void_event_type
    ):
        raise AccountingValidationError("Invalid source-event authorization.")
    require_permission(actor_context, permission, db_session=session)
    source_models = {
        "Sales Invoice": SalesInvoice,
        "Customer Receipt": CustomerReceipt,
        "Supplier Payment": SupplierPayment,
        "Expense": Expense,
        "Expense Payment": ExpensePayment,
    }
    source_model = source_models.get(source_module)
    if source_model is None:
        raise AccountingValidationError(
            f"Unsupported accounting reversal source {source_module}."
        )
    expected_status = {
        "Sales Invoice": {"VOIDED": "Void", "CANCELLED": "Cancelled"},
        "Customer Receipt": {"VOIDED": "Voided"},
        "Supplier Payment": {"VOIDED": "Voided"},
        "Expense": {"VOIDED": "Voided"},
        "Expense Payment": {"VOIDED": "Voided"},
    }.get(source_module, {}).get(void_event_type)
    if expected_status is None:
        raise AccountingValidationError(
            f"Unsupported {void_event_type} reversal event for {source_module}."
        )
    source = session.get(source_model, source_id)
    if source is None or source.status != expected_status:
        raise AccountingValidationError(
            "Only a voided or cancelled source transaction can be reversed in accounting."
        )
    existing = session.scalar(select(JournalEntry).where(
        JournalEntry.source_module == source_module,
        JournalEntry.source_transaction_id == source_id,
        JournalEntry.source_event_type == void_event_type,
    ))
    if existing is not None:
        return existing
    original = session.scalar(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(
            JournalEntry.source_module == source_module,
            JournalEntry.source_transaction_id == source_id,
            JournalEntry.status == "Posted",
        )
        .order_by(JournalEntry.id)
    )
    if original is None:
        return _upsert_exception(
            session, source_module, source_id, void_event_type, None,
            "Source transaction was voided before it had a posted accounting entry; review is required.",
        )
    if original.status != "Posted":
        return _upsert_exception(
            session, source_module, source_id, void_event_type,
            original.reference_number,
            f"Original accounting entry {original.journal_number} is not posted.",
        )
    try:
        lines = [{
            "account_id": line.account_id,
            "description": f"Reversal: {line.description}" if line.description else reason,
            "debit": line.credit,
            "credit": line.debit,
            "party_type": line.party_type,
            "party_id": line.party_id,
            "source_reference": line.source_reference or original.reference_number,
        } for line in original.lines]
        reversal = _create_posted_source_journal(
            session,
            journal_date=reversal_date,
            description=f"Reversal of {original.journal_number}: {reason}",
            reference_number=original.reference_number,
            source_module=source_module,
            source_transaction_id=source_id,
            source_event_type=void_event_type,
            lines=lines,
            actor_context=actor_context,
            authorization_permission=permission,
            responsible_user=responsible_user,
            reversal_of_id=original.id,
        )
        original.status = "Reversed"
        return reversal
    except AccountingValidationError as exc:
        return _upsert_exception(
            session, source_module, source_id, void_event_type,
            original.reference_number, str(exc),
        )


def reverse_source_event(
    session: Session, source_module: str, source_id: int, void_event_type: str,
    *, reason: str, reversal_date: date, responsible_user: str | None = None,
    actor_context: ActorContext,
) -> JournalEntry | AccountingException | None:
    return _reverse_source_event(
        session, source_module, source_id, void_event_type,
        actor_context=actor_context, reason=reason, reversal_date=reversal_date,
        responsible_user=responsible_user,
    )


def retry_accounting_exception(
    session: Session, exception_id: int, *, actor_context: ActorContext,
    responsible_user: str | None = None
) -> JournalEntry | AccountingException:
    require_permission(
        actor_context, "accounting.exceptions.manage", db_session=session
    )
    exception = session.get(AccountingException, exception_id)
    if exception is None:
        raise AccountingValidationError(f"Accounting exception #{exception_id} does not exist.")
    if exception.status != "Open":
        raise AccountingValidationError("Only an open accounting exception can be retried.")
    if exception.source_event_type in {"VOIDED", "CANCELLED"}:
        result = _reverse_source_event(
            session,
            exception.source_module,
            exception.source_transaction_id,
            exception.source_event_type,
            actor_context=actor_context,
            authorization_permission="accounting.exceptions.manage",
            reason=exception.reason,
            reversal_date=date.today(),
            responsible_user=responsible_user,
        )
        return result or exception
    return _capture_source_event(
        session,
        exception.source_module,
        exception.source_transaction_id,
        exception.source_event_type,
        actor_context=actor_context,
        authorization_permission="accounting.exceptions.manage",
        responsible_user=responsible_user,
    )


def list_accounting_exceptions(
    session: Session, *, actor_context: ActorContext, status: str | None = "Open"
) -> list[AccountingException]:
    require_permission(
        actor_context, "accounting.exceptions.manage", db_session=session
    )
    stmt = select(AccountingException)
    if status and status != "ALL":
        stmt = stmt.where(AccountingException.status == status)
    return list(session.scalars(
        stmt.order_by(AccountingException.first_seen_at.desc(), AccountingException.id.desc())
    ))
