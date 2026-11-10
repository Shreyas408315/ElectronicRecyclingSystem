from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from database.models import Expense, ExpensePayment
from services.expense_common import (
    ZERO,
    ExpenseValidationError,
    _append_expense_payment_history,
    clean_text,
    _expense_balance,
    expense_date,
    _expense_paid,
    _expense_payment_status,
    money_value,
    payment_method,
)
from services.expense_service import get_expense_by_id
from services.security_service import ActorContext, require_permission


def create_draft_expense_payment(
    session: Session, payload: dict, *, actor_context: ActorContext
) -> ExpensePayment:
    require_permission(actor_context, "expenses.manage", db_session=session)
    expense_id = payload.get("expense_id")
    expense = session.get(Expense, expense_id) if expense_id else None
    if expense is None or expense.status != "Posted":
        raise ExpenseValidationError("Expense payment must reference an existing posted expense.")
    amount = money_value(payload.get("amount_paid"), "Payment amount", minimum=Decimal("0.01"))
    if amount > _expense_balance(session, expense):
        raise ExpenseValidationError("Payment exceeds the remaining expense balance.")
    payment_day = expense_date(payload.get("payment_date", date.today()), "Payment date")
    payment = ExpensePayment(
        payment_code="EPAY-PENDING",
        expense_id=expense.id,
        payment_date=payment_day,
        amount_paid=amount.quantize(Decimal("0.01")),
        payment_method=payment_method(payload.get("payment_method")),
        transaction_reference=clean_text(
            payload.get("transaction_reference"), "Transaction reference", 100
        ),
        bank_account_name=clean_text(
            payload.get("bank_account_name"), "Bank/account name", 150
        ),
        cheque_number=clean_text(payload.get("cheque_number"), "Cheque number", 100),
        notes=clean_text(payload.get("notes"), "Notes"),
        responsible_user=clean_text(
            payload.get("responsible_user"), "Responsible user", 100
        ),
        status="Draft",
    )
    with session.begin_nested():
        session.add(payment)
        session.flush()
        payment.payment_code = f"EPAY-{payment.id:06d}"
        _append_expense_payment_history(
            session, payment, "CREATED", None, "Draft",
            responsible_user=payment.responsible_user,
        )
        session.flush()
        session.expire(payment, ["history"])
    return payment


def get_expense_payment_by_id(
    session: Session, payment_id: int, *, actor_context: ActorContext
) -> ExpensePayment | None:
    require_permission(actor_context, "expenses.view", db_session=session)
    return session.scalar(
        select(ExpensePayment).options(
            selectinload(ExpensePayment.expense),
            selectinload(ExpensePayment.history),
        ).where(ExpensePayment.id == payment_id)
    )


def update_draft_expense_payment(
    session: Session,
    payment_id: int,
    payload: dict,
    *,
    actor_context: ActorContext,
) -> ExpensePayment:
    require_permission(actor_context, "expenses.manage", db_session=session)
    payment = get_expense_payment_by_id(
        session, payment_id, actor_context=actor_context
    )
    if payment is None:
        raise ExpenseValidationError(f"Expense payment #{payment_id} does not exist.")
    if payment.status != "Draft":
        raise ExpenseValidationError("Only draft expense payments can be edited.")
    expense_id = payload.get("expense_id", payment.expense_id)
    expense = session.get(Expense, expense_id) if expense_id else None
    if expense is None or expense.status != "Posted":
        raise ExpenseValidationError("Expense payment must reference an existing posted expense.")
    amount = money_value(
        payload.get("amount_paid", payment.amount_paid),
        "Payment amount", minimum=Decimal("0.01"),
    ).quantize(Decimal("0.01"))
    if amount > _expense_balance(session, expense):
        raise ExpenseValidationError("Payment exceeds the remaining expense balance.")
    with session.begin_nested():
        payment.expense_id = expense.id
        payment.payment_date = expense_date(
            payload.get("payment_date", payment.payment_date), "Payment date"
        )
        payment.amount_paid = amount
        payment.payment_method = payment_method(
            payload.get("payment_method", payment.payment_method)
        )
        payment.transaction_reference = clean_text(
            payload.get("transaction_reference", payment.transaction_reference),
            "Transaction reference", 100,
        )
        payment.bank_account_name = clean_text(
            payload.get("bank_account_name", payment.bank_account_name),
            "Bank/account name", 150,
        )
        payment.cheque_number = clean_text(
            payload.get("cheque_number", payment.cheque_number), "Cheque number", 100
        )
        payment.notes = clean_text(payload.get("notes", payment.notes), "Notes")
        payment.responsible_user = clean_text(
            payload.get("responsible_user", payment.responsible_user),
            "Responsible user", 100,
        )
        payment.updated_at = datetime.now()
        _append_expense_payment_history(
            session, payment, "UPDATED", "Draft", "Draft",
            "Draft expense payment edited.", payment.responsible_user,
        )
        session.flush()
        session.expire(payment, ["history"])
    return payment


def list_expense_payments(
    session: Session,
    *,
    expense_id: int | None = None,
    payment_method_filter: str | None = None,
    status: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    search: str | None = None,
    actor_context: ActorContext,
) -> list[ExpensePayment]:
    require_permission(actor_context, "expenses.view", db_session=session)
    stmt = select(ExpensePayment).options(
        selectinload(ExpensePayment.expense)
    )
    if expense_id is not None:
        stmt = stmt.where(ExpensePayment.expense_id == expense_id)
    if payment_method_filter:
        stmt = stmt.where(ExpensePayment.payment_method == payment_method_filter)
    if status and status != "ALL":
        stmt = stmt.where(ExpensePayment.status == status)
    if start_date:
        stmt = stmt.where(ExpensePayment.payment_date >= start_date)
    if end_date:
        stmt = stmt.where(ExpensePayment.payment_date <= end_date)
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.join(Expense, Expense.id == ExpensePayment.expense_id).where(or_(
            ExpensePayment.payment_code.ilike(pattern),
            ExpensePayment.transaction_reference.ilike(pattern),
            Expense.title.ilike(pattern),
            Expense.expense_code.ilike(pattern),
            Expense.payee_name.ilike(pattern),
        ))
    return list(session.scalars(
        stmt.order_by(ExpensePayment.payment_date.desc(), ExpensePayment.id.desc())
    ).unique())


def post_expense_payment(
    session: Session,
    payment_id: int,
    *,
    actor_context: ActorContext,
    responsible_user: str | None = None,
) -> ExpensePayment:
    require_permission(actor_context, "expenses.post", db_session=session)
    payment = get_expense_payment_by_id(
        session, payment_id, actor_context=actor_context
    )
    if payment is None:
        raise ExpenseValidationError(f"Expense payment #{payment_id} does not exist.")
    if payment.status != "Draft":
        raise ExpenseValidationError("Only a draft expense payment can be posted.")
    with session.begin_nested():
        posted_total = (
            select(func.coalesce(func.sum(ExpensePayment.amount_paid), ZERO))
            .where(
                ExpensePayment.expense_id == payment.expense_id,
                ExpensePayment.status == "Posted",
            )
            .scalar_subquery()
        )
        lock = session.execute(
            update(Expense)
            .where(
                Expense.id == payment.expense_id,
                Expense.status == "Posted",
                Expense.total_amount - posted_total >= payment.amount_paid,
            )
            .values(total_amount=Expense.total_amount)
            .execution_options(synchronize_session=False)
        )
        if lock.rowcount != 1:
            raise ExpenseValidationError(
                "Expense balance changed or is no longer payable; refresh and try again."
            )
        payment.status = "Posted"
        payment.responsible_user = clean_text(
            responsible_user, "Responsible user", 100
        ) or payment.responsible_user
        payment.updated_at = datetime.now()
        _append_expense_payment_history(
            session, payment, "POSTED", "Draft", "Posted",
            responsible_user=payment.responsible_user,
        )
        session.flush()
        session.expire(payment, ["history"])
        from services.accounting_posting_service import capture_source_event

        capture_source_event(
            session, "Expense Payment", payment.id, "POSTED",
            actor_context=actor_context,
            responsible_user=payment.responsible_user,
        )
    return payment


def void_expense_payment(
    session: Session,
    payment_id: int,
    reason: str,
    *,
    actor_context: ActorContext,
    responsible_user: str | None = None,
) -> ExpensePayment:
    require_permission(actor_context, "expenses.void", db_session=session)
    payment = get_expense_payment_by_id(
        session, payment_id, actor_context=actor_context
    )
    if payment is None:
        raise ExpenseValidationError(f"Expense payment #{payment_id} does not exist.")
    if payment.status != "Posted":
        raise ExpenseValidationError("Only a posted expense payment can be voided.")
    reason_text = clean_text(reason, "Void reason")
    if not reason_text:
        raise ExpenseValidationError("A reason is required to void an expense payment.")
    with session.begin_nested():
        payment.status = "Voided"
        payment.void_reason = reason_text
        payment.voided_at = datetime.now()
        payment.responsible_user = clean_text(
            responsible_user, "Responsible user", 100
        ) or payment.responsible_user
        payment.updated_at = datetime.now()
        _append_expense_payment_history(
            session, payment, "VOIDED", "Posted", "Voided",
            reason_text, payment.responsible_user,
        )
        session.flush()
        session.expire(payment, ["history"])
        from services.accounting_posting_service import reverse_source_event

        reverse_source_event(
            session, "Expense Payment", payment.id, "VOIDED", reason=reason_text,
            reversal_date=date.today(),
            actor_context=actor_context,
            responsible_user=payment.responsible_user,
        )
    return payment


def get_expense_payment_summary(
    session: Session, expense_id: int, *, actor_context: ActorContext
) -> dict:
    require_permission(actor_context, "expenses.view", db_session=session)
    expense = get_expense_by_id(
        session, expense_id, actor_context=actor_context
    )
    if expense is None:
        raise ExpenseValidationError(f"Expense #{expense_id} does not exist.")
    paid = _expense_paid(session, expense_id) if expense.status == "Posted" else ZERO
    balance = max(ZERO, expense.total_amount - paid).quantize(Decimal("0.01"))
    return {
        "expense_amount": expense.total_amount,
        "total_paid": paid,
        "outstanding": balance,
        "payment_status": _expense_payment_status(session, expense),
    }


def get_expense_payment_history(
    session: Session, expense_id: int, *, actor_context: ActorContext
) -> list[ExpensePayment]:
    require_permission(actor_context, "expenses.view", db_session=session)
    return list(session.scalars(
        select(ExpensePayment).options(selectinload(ExpensePayment.history))
        .where(ExpensePayment.expense_id == expense_id)
        .order_by(ExpensePayment.payment_date, ExpensePayment.id)
    ))


def calculate_expense_payment_totals(
    session: Session,
    *,
    actor_context: ActorContext,
    start_date: date | None = None,
    end_date: date | None = None,
) -> Decimal:
    require_permission(actor_context, "expenses.view", db_session=session)
    stmt = select(func.coalesce(func.sum(ExpensePayment.amount_paid), ZERO)).where(
        ExpensePayment.status == "Posted"
    )
    if start_date:
        stmt = stmt.where(ExpensePayment.payment_date >= start_date)
    if end_date:
        stmt = stmt.where(ExpensePayment.payment_date <= end_date)
    return Decimal(str(session.scalar(stmt) or ZERO)).quantize(Decimal("0.01"))
