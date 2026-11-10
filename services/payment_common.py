from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import (
    CustomerReceipt,
    CustomerReceiptHistory,
    PaymentTransactionReferenceScope,
    SupplierPayment,
    SupplierPaymentHistory,
)


CENT = Decimal("0.01")
ZERO = Decimal("0.00")
PAYMENT_METHODS = (
    "Cash", "Bank Transfer", "UPI", "Cheque", "NEFT", "RTGS", "IMPS", "Other"
)


class PaymentValidationError(ValueError):
    """Raised when a receipt, payment, or allocation violates a financial rule."""


def money(value: object, label: str, *, positive: bool = False) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
        if not amount.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaymentValidationError(f"{label} must be a valid decimal amount.") from exc
    if amount < ZERO or (positive and amount <= ZERO):
        requirement = "greater than zero" if positive else "nonnegative"
        raise PaymentValidationError(f"{label} must be {requirement}.")
    return amount


def payment_date(value: object, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise PaymentValidationError(f"{label} must be a valid date.") from exc


def payment_method(value: object) -> str:
    method = str(value or "Cash").strip()
    if method not in PAYMENT_METHODS:
        raise PaymentValidationError(
            f"Payment method must be one of: {', '.join(PAYMENT_METHODS)}."
        )
    return method


def clean_optional(value: object, label: str, limit: int | None = None) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if limit is not None and len(text) > limit:
        raise PaymentValidationError(f"{label} must not exceed {limit} characters.")
    return text


def reference_account_scope(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _ensure_unique_reference(
    session: Session, reference: str | None, *, transaction_kind: str | None = None,
    transaction_id: int | None = None, payment_method_value: str | None = None,
    bank_account_name: str | None = None, register: bool = True,
) -> None:
    if not reference or transaction_kind is None or payment_method_value is None:
        return
    expected_kind = "Customer Receipt" if transaction_kind == "receipt" else "Supplier Payment"
    account_scope = reference_account_scope(bank_account_name)
    registry = session.get(
        PaymentTransactionReferenceScope,
        (reference, expected_kind, payment_method_value, account_scope),
    )
    if registry is not None:
        if transaction_id is not None and registry.transaction_id == transaction_id:
            return
        raise PaymentValidationError(
            f"Transaction reference '{reference}' is already recorded for this "
            f"{payment_method_value} account and transaction direction."
        )

    record_type = CustomerReceipt if transaction_kind == "receipt" else SupplierPayment
    reference_column = record_type.transaction_reference
    method_column = record_type.payment_method
    duplicates = session.scalars(
        select(record_type).where(
            reference_column == reference,
            method_column == payment_method_value,
            record_type.id != transaction_id if transaction_id is not None else True,
        )
    ).all()
    if any(reference_account_scope(row.bank_account_name) == account_scope for row in duplicates):
        raise PaymentValidationError(
            f"Transaction reference '{reference}' is already in use for this "
            f"{payment_method_value} account and transaction direction."
        )
    if register and transaction_kind is not None and transaction_id is not None:
        session.add(PaymentTransactionReferenceScope(
            transaction_reference=reference,
            transaction_kind=expected_kind,
            transaction_id=transaction_id,
            payment_method=payment_method_value,
            account_scope=account_scope,
        ))
        session.flush()


def _append_receipt_history(
    session: Session,
    receipt: CustomerReceipt,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    reason: str | None,
    responsible_user: str | None,
) -> None:
    session.add(CustomerReceiptHistory(
        receipt_id=receipt.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        responsible_user=responsible_user,
    ))


def _append_payment_history(
    session: Session,
    payment: SupplierPayment,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    reason: str | None,
    responsible_user: str | None,
) -> None:
    session.add(SupplierPaymentHistory(
        payment_id=payment.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        responsible_user=responsible_user,
    ))


def temporary_code(prefix: str) -> str:
    return f"{prefix}-PENDING-{uuid4().hex}"
