from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Expense, ExpensePayment, ExpensePaymentHistory, ExpenseHistory

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MONEY_PRECISION = Decimal("0.0001")
PAYMENT_METHODS = (
    "Cash", "Bank Transfer", "UPI", "Cheque", "NEFT", "RTGS", "IMPS", "Other"
)


class ExpenseValidationError(ValueError):
    pass


def decimal_value(value: object, label: str, *, minimum: Decimal = ZERO) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
        if not result.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExpenseValidationError(f"{label} must be a valid decimal number.") from exc
    if result < minimum:
        raise ExpenseValidationError(f"{label} must be at least {minimum}.")
    return result


def money_value(value: object, label: str, *, minimum: Decimal = ZERO) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
        if not result.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExpenseValidationError(f"{label} must be a valid amount.") from exc
    if result < minimum:
        raise ExpenseValidationError(f"{label} must be at least {minimum}.")
    return result


def decimal_value(value: object, label: str, *, minimum: Decimal = ZERO) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        if not result.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExpenseValidationError(f"{label} must be a valid decimal number.") from exc
    if result < minimum:
        raise ExpenseValidationError(f"{label} must be at least {minimum}.")
    return result


def expense_date(value: object, label: str, *, optional: bool = False) -> date | None:
    if value in (None, "") and optional:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ExpenseValidationError(f"{label} must be a valid date.") from exc


def clean_text(value: object, label: str, limit: int | None = None) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if limit is not None and len(text) > limit:
        raise ExpenseValidationError(f"{label} must not exceed {limit} characters.")
    return text


def _expense_paid(session: Session, expense_id: int) -> Decimal:
    payments = session.scalars(
        select(ExpensePayment).where(
            ExpensePayment.expense_id == expense_id,
            ExpensePayment.status == "Posted",
        )
    ).all()
    return sum((row.amount_paid for row in payments), ZERO).quantize(CENT)


def _expense_balance(session: Session, expense: Expense) -> Decimal:
    return max(ZERO, (expense.total_amount - _expense_paid(session, expense.id))).quantize(CENT)


def _expense_payment_status(
    session: Session, expense: Expense, *, on_date: date | None = None
) -> str:
    if expense.status != "Posted":
        return "Not Payable"
    paid = _expense_paid(session, expense.id)
    balance = max(ZERO, expense.total_amount - paid)
    if balance == ZERO:
        return "Paid"
    if expense.due_date and expense.due_date < (on_date or date.today()):
        return "Overdue"
    if paid > ZERO:
        return "Partially Paid"
    return "Unpaid"


def _append_expense_history(
    session: Session,
    expense: Expense,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    reason: str | None = None,
    responsible_user: str | None = None,
) -> None:
    session.add(ExpenseHistory(
        expense_id=expense.id, event_type=event_type,
        from_status=from_status, to_status=to_status,
        reason=reason, responsible_user=responsible_user,
    ))


def _append_expense_payment_history(
    session: Session,
    payment: ExpensePayment,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    reason: str | None = None,
    responsible_user: str | None = None,
) -> None:
    session.add(ExpensePaymentHistory(
        payment_id=payment.id, event_type=event_type,
        from_status=from_status, to_status=to_status,
        reason=reason, responsible_user=responsible_user,
    ))


def payment_method(value: object) -> str:
    result = str(value or "Cash").strip()
    if result not in PAYMENT_METHODS:
        raise ExpenseValidationError(
            f"Payment method must be one of: {', '.join(PAYMENT_METHODS)}."
        )
    return result
