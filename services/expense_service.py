from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from database.models import (
    Expense,
    ExpenseCategory,
    ExpenseItem,
    ExpensePayment,
    Supplier,
)
from services.expense_common import (
    CENT,
    ZERO,
    ExpenseValidationError,
    _append_expense_history,
    clean_text,
    decimal_value,
    _expense_balance,
    expense_date,
    _expense_paid,
    _expense_payment_status,
    money_value,
)
from services.security_service import ActorContext, require_permission

DEFAULT_EXPENSE_CATEGORIES = (
    "Electricity & Utilities",
    "Factory Rent",
    "Repairs & Maintenance",
    "Transportation & Logistics",
    "Fuel",
    "Tools & Consumables",
    "Packaging",
    "Waste Disposal",
    "Office & Administration",
    "Communication & Internet",
    "Insurance",
    "Professional Fees",
    "Employee Welfare",
    "Other",
)

EXPENSE_STATUSES = ("Draft", "Posted", "Voided")
PAYMENT_STATUSES = ("Unpaid", "Partially Paid", "Paid", "Overdue", "Not Payable")


def seed_default_expense_categories(
    session: Session, *, actor_context: ActorContext
) -> None:
    require_permission(actor_context, "expenses.manage", db_session=session)
    existing = {
        name.casefold(): name
        for name in session.scalars(select(ExpenseCategory.name)).all()
    }
    for name in DEFAULT_EXPENSE_CATEGORIES:
        if name.casefold() not in existing:
            session.add(ExpenseCategory(name=name, is_active=True, is_system=True))
    session.flush()


def list_expense_categories(
    session: Session, *, actor_context: ActorContext, active_only: bool = True
) -> list[ExpenseCategory]:
    require_permission(actor_context, "expenses.view", db_session=session)
    stmt = select(ExpenseCategory)
    if active_only:
        stmt = stmt.where(ExpenseCategory.is_active.is_(True))
    return list(session.scalars(stmt.order_by(ExpenseCategory.name, ExpenseCategory.id)))


def create_expense_category(
    session: Session, name: str, description: str | None = None, *,
    actor_context: ActorContext,
) -> ExpenseCategory:
    require_permission(actor_context, "expenses.manage", db_session=session)
    normalized = clean_text(name, "Category name", 100)
    if not normalized:
        raise ExpenseValidationError("Category name is required.")
    if session.scalar(select(ExpenseCategory.id).where(
        func.lower(ExpenseCategory.name) == normalized.casefold()
    )):
        raise ExpenseValidationError("An expense category with this name already exists.")
    category = ExpenseCategory(
        name=normalized,
        description=clean_text(description, "Category description", 500),
        is_active=True,
        is_system=False,
    )
    session.add(category)
    session.flush()
    return category


def update_expense_category(
    session: Session, category_id: int, *, actor_context: ActorContext,
    name: str, description: str | None = None
) -> ExpenseCategory:
    require_permission(actor_context, "expenses.manage", db_session=session)
    category = session.get(ExpenseCategory, category_id)
    if category is None:
        raise ExpenseValidationError(f"Expense category #{category_id} does not exist.")
    normalized = clean_text(name, "Category name", 100)
    if not normalized:
        raise ExpenseValidationError("Category name is required.")
    duplicate = session.scalar(select(ExpenseCategory.id).where(
        func.lower(ExpenseCategory.name) == normalized.casefold(),
        ExpenseCategory.id != category.id,
    ))
    if duplicate:
        raise ExpenseValidationError("An expense category with this name already exists.")
    category.name = normalized
    category.description = clean_text(description, "Category description", 500)
    session.flush()
    return category


def deactivate_expense_category(
    session: Session, category_id: int, *, actor_context: ActorContext
) -> ExpenseCategory:
    require_permission(actor_context, "expenses.manage", db_session=session)
    category = session.get(ExpenseCategory, category_id)
    if category is None:
        raise ExpenseValidationError(f"Expense category #{category_id} does not exist.")
    category.is_active = False
    session.flush()
    return category


def _validate_attachment(value: object) -> str | None:
    path = clean_text(value, "Attachment path", 1000)
    if path and not Path(path).expanduser().is_file():
        raise ExpenseValidationError("The attachment path must point to an accessible file.")
    return path


def calculate_expense_totals(
    payload: dict, items: list[dict] | None = None
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, list[dict]]:
    item_rows = items or []
    subtotal = ZERO
    item_discount = ZERO
    tax_amount = ZERO
    normalized_items: list[dict] = []
    for index, row in enumerate(item_rows, start=1):
        description = clean_text(row.get("description"), f"Item #{index} description", 300)
        if not description:
            raise ExpenseValidationError(f"Item #{index} description is required.")
        quantity = decimal_value(
            row.get("quantity", "1"), f"Item #{index} quantity",
            minimum=Decimal("0.001"),
        )
        unit_price = money_value(row.get("unit_price"), f"Item #{index} unit price")
        discount = money_value(row.get("discount", "0"), f"Item #{index} discount")
        tax_rate = money_value(row.get("tax_rate", "0"), f"Item #{index} tax rate")
        if tax_rate > Decimal("100.00"):
            raise ExpenseValidationError(f"Item #{index} tax rate cannot exceed 100%.")
        raw_subtotal = (quantity * unit_price).quantize(CENT)
        if discount > raw_subtotal:
            raise ExpenseValidationError(f"Item #{index} discount cannot exceed its subtotal.")
        taxable = raw_subtotal - discount
        line_tax = (taxable * tax_rate / Decimal("100")).quantize(CENT)
        item_total = taxable + line_tax
        subtotal += raw_subtotal
        item_discount += discount
        tax_amount += line_tax
        normalized_items.append({
            "description": description,
            "category_name": clean_text(row.get("category_name"), f"Item #{index} category", 100),
            "quantity": quantity,
            "unit": clean_text(row.get("unit") or "unit", f"Item #{index} unit", 30) or "unit",
            "unit_price": unit_price,
            "discount": discount,
            "tax_rate": tax_rate,
            "tax_amount": line_tax,
            "line_subtotal": raw_subtotal,
            "line_total": item_total,
        })

    if not item_rows:
        subtotal = money_value(payload.get("subtotal"), "Expense subtotal")
        tax_amount = money_value(payload.get("tax_amount", "0"), "Tax amount")

    header_discount = money_value(payload.get("discount", "0"), "Discount")
    total_discount = item_discount + header_discount
    if total_discount > subtotal:
        raise ExpenseValidationError("Total discounts cannot exceed the expense subtotal.")
    additional_charges = money_value(
        payload.get("additional_charges", "0"), "Additional charges"
    )
    taxable_amount = subtotal - total_discount
    total = (taxable_amount + additional_charges + tax_amount).quantize(CENT)
    return (
        subtotal.quantize(CENT),
        total_discount.quantize(CENT),
        additional_charges,
        tax_amount.quantize(CENT),
        total,
        taxable_amount.quantize(CENT),
        normalized_items,
    )


def _expense_payload(session: Session, payload: dict, items: list[dict] | None) -> dict:
    title = clean_text(payload.get("title"), "Expense title", 200)
    payee = clean_text(payload.get("payee_name"), "Payee/vendor name", 200)
    if not title or not payee:
        raise ExpenseValidationError("Expense title and payee/vendor name are required.")
    category_id = payload.get("category_id")
    category = session.get(ExpenseCategory, category_id) if category_id else None
    if category is None or not category.is_active:
        raise ExpenseValidationError("Select an active expense category.")
    supplier_id = payload.get("supplier_id") or None
    if supplier_id and session.get(Supplier, supplier_id) is None:
        raise ExpenseValidationError("Select an existing supplier or leave the supplier link empty.")
    expense_day = expense_date(payload.get("expense_date", date.today()), "Expense date")
    due_day = expense_date(payload.get("due_date"), "Due date", optional=True)
    if due_day and due_day < expense_day:
        raise ExpenseValidationError("Due date cannot be earlier than the expense date.")
    (
        subtotal, discount, additional_charges, tax_amount, total_amount,
        taxable_amount, normalized_items,
    ) = calculate_expense_totals(payload, items)
    if total_amount <= ZERO:
        raise ExpenseValidationError("Total expense amount must be greater than zero.")
    attachment_path = _validate_attachment(payload.get("attachment_path"))
    return {
        "expense_date": expense_day,
        "due_date": due_day,
        "title": title,
        "description": clean_text(payload.get("description"), "Description"),
        "category_id": category.id,
        "payee_name": payee,
        "supplier_id": supplier_id,
        "vendor_reference": clean_text(payload.get("vendor_reference"), "Vendor reference", 100),
        "subtotal": subtotal,
        "discount": discount,
        "additional_charges": additional_charges,
        "taxable_amount": taxable_amount,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "department": clean_text(payload.get("department"), "Department", 100),
        "attachment_path": attachment_path,
        "notes": clean_text(payload.get("notes"), "Notes"),
        "responsible_user": clean_text(payload.get("responsible_user"), "Responsible user", 100),
        "items": normalized_items,
    }


def _replace_items(session: Session, expense: Expense, items: list[dict]) -> None:
    for existing in list(expense.items):
        session.delete(existing)
    session.flush()
    for row in items:
        session.add(ExpenseItem(expense_id=expense.id, **row))
    session.flush()


def create_draft_expense(
    session: Session, payload: dict, items: list[dict] | None = None, *,
    actor_context: ActorContext,
) -> Expense:
    require_permission(actor_context, "expenses.manage", db_session=session)
    data = _expense_payload(session, payload, items)
    normalized_items = data.pop("items")
    expense = Expense(expense_code="EXP-PENDING", status="Draft", **data)
    with session.begin_nested():
        session.add(expense)
        session.flush()
        expense.expense_code = f"EXP-{expense.id:06d}"
        _replace_items(session, expense, normalized_items)
        _append_expense_history(
            session, expense, "CREATED", None, "Draft",
            responsible_user=expense.responsible_user,
        )
        session.flush()
        session.expire(expense, ["history"])
    return expense


def _get_expense_by_id(
    session: Session, expense_id: int, *, include_payments: bool = False
) -> Expense | None:
    options = [
        selectinload(Expense.category), selectinload(Expense.supplier),
        selectinload(Expense.items), selectinload(Expense.history),
    ]
    if include_payments:
        options.append(selectinload(Expense.payments))
    return session.scalar(
        select(Expense).options(*options).where(Expense.id == expense_id)
    )


def get_expense_by_id(
    session: Session, expense_id: int, *, actor_context: ActorContext
) -> Expense | None:
    require_permission(actor_context, "expenses.view", db_session=session)
    return _get_expense_by_id(
        session, expense_id, include_payments=True
    )


def update_draft_expense(
    session: Session, expense_id: int, payload: dict, items: list[dict] | None = None, *,
    actor_context: ActorContext,
) -> Expense:
    require_permission(actor_context, "expenses.manage", db_session=session)
    expense = _get_expense_by_id(session, expense_id)
    if expense is None:
        raise ExpenseValidationError(f"Expense #{expense_id} does not exist.")
    if expense.status != "Draft":
        raise ExpenseValidationError("Only draft expenses can be edited.")
    data = _expense_payload(session, payload, items)
    normalized_items = data.pop("items")
    with session.begin_nested():
        for key, value in data.items():
            setattr(expense, key, value)
        _replace_items(session, expense, normalized_items)
        _append_expense_history(
            session, expense, "UPDATED", "Draft", "Draft",
            "Draft expense edited.", expense.responsible_user,
        )
        session.flush()
        session.expire(expense, ["history"])
    return expense


def post_expense(
    session: Session, expense_id: int, *, actor_context: ActorContext,
    responsible_user: str | None = None
) -> Expense:
    require_permission(actor_context, "expenses.post", db_session=session)
    expense = _get_expense_by_id(session, expense_id)
    if expense is None:
        raise ExpenseValidationError(f"Expense #{expense_id} does not exist.")
    if expense.status != "Draft":
        raise ExpenseValidationError("Only a draft expense can be posted.")
    with session.begin_nested():
        expense.status = "Posted"
        expense.responsible_user = clean_text(
            responsible_user, "Responsible user", 100
        ) or expense.responsible_user
        expense.updated_at = datetime.now()
        _append_expense_history(
            session, expense, "POSTED", "Draft", "Posted",
            responsible_user=expense.responsible_user,
        )
        session.flush()
        session.expire(expense, ["history"])
        from services.accounting_posting_service import capture_source_event

        capture_source_event(
            session, "Expense", expense.id, "POSTED",
            actor_context=actor_context,
            responsible_user=expense.responsible_user,
        )
    return expense


def void_expense(
    session: Session,
    expense_id: int,
    reason: str,
    *,
    actor_context: ActorContext,
    responsible_user: str | None = None,
) -> Expense:
    require_permission(actor_context, "expenses.void", db_session=session)
    expense = _get_expense_by_id(session, expense_id)
    if expense is None:
        raise ExpenseValidationError(f"Expense #{expense_id} does not exist.")
    if expense.status not in ("Draft", "Posted"):
        raise ExpenseValidationError("Only a draft or posted expense can be voided.")
    reason_text = clean_text(reason, "Void reason")
    if not reason_text:
        raise ExpenseValidationError("A reason is required to void an expense.")
    if _expense_paid(session, expense.id) > ZERO:
        raise ExpenseValidationError(
            "Void or reverse posted expense payments before voiding this expense."
        )
    previous = expense.status
    with session.begin_nested():
        expense.status = "Voided"
        expense.void_reason = reason_text
        expense.voided_at = datetime.now()
        expense.responsible_user = clean_text(
            responsible_user, "Responsible user", 100
        ) or expense.responsible_user
        expense.updated_at = datetime.now()
        _append_expense_history(
            session, expense, "VOIDED", previous, "Voided",
            reason_text, expense.responsible_user,
        )
        session.flush()
        session.expire(expense, ["history"])
        if previous == "Posted":
            from services.accounting_posting_service import reverse_source_event

            reverse_source_event(
                session, "Expense", expense.id, "VOIDED", reason=reason_text,
                reversal_date=date.today(),
                actor_context=actor_context,
                responsible_user=expense.responsible_user,
            )
    return expense


def list_expenses(
    session: Session,
    *,
    search: str | None = None,
    category_id: int | None = None,
    payee: str | None = None,
    status: str | None = None,
    payment_status: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    sort_by: str = "date_desc",
    offset: int = 0,
    limit: int = 100,
    actor_context: ActorContext,
) -> list[Expense]:
    require_permission(actor_context, "expenses.view", db_session=session)
    if start_date and end_date and start_date > end_date:
        raise ExpenseValidationError("Start date cannot be later than end date.")
    stmt = select(Expense).options(
        selectinload(Expense.category), selectinload(Expense.supplier),
        selectinload(Expense.items), selectinload(Expense.payments),
    )
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(
            Expense.expense_code.ilike(pattern),
            Expense.title.ilike(pattern),
            Expense.payee_name.ilike(pattern),
            Expense.vendor_reference.ilike(pattern),
        ))
    if category_id is not None:
        stmt = stmt.where(Expense.category_id == category_id)
    if payee:
        stmt = stmt.where(Expense.payee_name.ilike(f"%{payee.strip()}%"))
    if status and status != "ALL":
        stmt = stmt.where(Expense.status == status)
    if start_date:
        stmt = stmt.where(Expense.expense_date >= start_date)
    if end_date:
        stmt = stmt.where(Expense.expense_date <= end_date)
    if payment_status and payment_status != "ALL":
        posted_payments = (
            select(func.coalesce(func.sum(ExpensePayment.amount_paid), ZERO))
            .where(
                ExpensePayment.expense_id == Expense.id,
                ExpensePayment.status == "Posted",
            )
            .correlate(Expense)
            .scalar_subquery()
        )
        status_expression = case(
            (Expense.status != "Posted", "Not Payable"),
            (posted_payments >= Expense.total_amount, "Paid"),
            (
                Expense.due_date.is_not(None)
                & (Expense.due_date < date.today())
                & (posted_payments < Expense.total_amount),
                "Overdue",
            ),
            (posted_payments > ZERO, "Partially Paid"),
            else_="Unpaid",
        )
        stmt = stmt.where(status_expression == payment_status)
    orderings = {
        "date_desc": (Expense.expense_date.desc(), Expense.id.desc()),
        "date_asc": (Expense.expense_date.asc(), Expense.id.asc()),
        "amount_desc": (Expense.total_amount.desc(), Expense.id.desc()),
        "amount_asc": (Expense.total_amount.asc(), Expense.id.asc()),
        "code_asc": (Expense.expense_code.asc(), Expense.id.asc()),
    }
    stmt = stmt.order_by(*orderings.get(sort_by, orderings["date_desc"]))
    return list(
        session.scalars(
            stmt.offset(max(0, offset)).limit(max(1, min(limit, 500)))
        ).unique()
    )


def calculate_expense_summary(
    session: Session,
    *,
    actor_context: ActorContext,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    require_permission(actor_context, "expenses.view", db_session=session)
    stmt = select(Expense).where(Expense.status == "Posted")
    if start_date:
        stmt = stmt.where(Expense.expense_date >= start_date)
    if end_date:
        stmt = stmt.where(Expense.expense_date <= end_date)
    expenses = session.scalars(
        stmt.options(selectinload(Expense.category), selectinload(Expense.payments))
    ).all()
    total_expenses = sum((row.total_amount for row in expenses), ZERO).quantize(CENT)
    total_payments = sum((_expense_paid(session, row.id) for row in expenses), ZERO).quantize(CENT)
    outstanding = sum((_expense_balance(session, row) for row in expenses), ZERO).quantize(CENT)
    statuses = [_expense_payment_status(session, row) for row in expenses]
    return {
        "total_expenses": total_expenses,
        "total_payments": total_payments,
        "outstanding": outstanding,
        "expense_count": len(expenses),
        "unpaid_count": statuses.count("Unpaid"),
        "partially_paid_count": statuses.count("Partially Paid"),
        "overdue_count": statuses.count("Overdue"),
        "category_totals": calculate_category_summary(
            session, actor_context=actor_context,
            start_date=start_date, end_date=end_date
        ),
    }


def calculate_dashboard_expense_summary(
    session: Session,
    *,
    actor_context: ActorContext,
    start_date: date,
    end_date: date,
) -> dict[str, Decimal]:
    require_permission(actor_context, "expenses.view", db_session=session)
    expense_total = session.scalar(
        select(func.coalesce(func.sum(Expense.total_amount), ZERO)).where(
            Expense.status == "Posted",
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
    )
    payment_total = session.scalar(
        select(func.coalesce(func.sum(ExpensePayment.amount_paid), ZERO))
        .join(Expense, Expense.id == ExpensePayment.expense_id)
        .where(
            Expense.status == "Posted",
            ExpensePayment.status == "Posted",
            ExpensePayment.payment_date >= start_date,
            ExpensePayment.payment_date <= end_date,
        )
    )
    posted_expenses = session.scalars(
        select(Expense).where(Expense.status == "Posted")
    ).all()
    outstanding = sum(
        (_expense_balance(session, expense) for expense in posted_expenses), ZERO
    )
    return {
        "total_expenses": Decimal(str(expense_total or ZERO)).quantize(CENT),
        "total_payments": Decimal(str(payment_total or ZERO)).quantize(CENT),
        "outstanding": outstanding.quantize(CENT),
    }


def calculate_category_summary(
    session: Session,
    *,
    actor_context: ActorContext,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict]:
    require_permission(actor_context, "expenses.view", db_session=session)
    stmt = (
        select(ExpenseCategory.name, func.sum(Expense.total_amount), func.count(Expense.id))
        .join(Expense, Expense.category_id == ExpenseCategory.id)
        .where(Expense.status == "Posted")
    )
    if start_date:
        stmt = stmt.where(Expense.expense_date >= start_date)
    if end_date:
        stmt = stmt.where(Expense.expense_date <= end_date)
    return [
        {"category": name, "total": Decimal(str(total or 0)).quantize(CENT), "count": count}
        for name, total, count in session.execute(
            stmt.group_by(ExpenseCategory.id, ExpenseCategory.name)
            .order_by(ExpenseCategory.name)
        )
    ]


def calculate_monthly_summary(
    session: Session,
    *,
    year: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    actor_context: ActorContext,
) -> list[dict]:
    require_permission(actor_context, "expenses.view", db_session=session)
    stmt = select(Expense).where(Expense.status == "Posted")
    if year is not None:
        stmt = stmt.where(func.strftime("%Y", Expense.expense_date) == str(year))
    if start_date:
        stmt = stmt.where(Expense.expense_date >= start_date)
    if end_date:
        stmt = stmt.where(Expense.expense_date <= end_date)
    expenses = session.scalars(stmt).all()
    grouped: dict[str, dict] = defaultdict(
        lambda: {"expense_total": ZERO, "payments": ZERO, "count": 0}
    )
    for expense in expenses:
        key = expense.expense_date.strftime("%Y-%m")
        grouped[key]["expense_total"] += expense.total_amount
        grouped[key]["count"] += 1
    payment_stmt = select(
        func.strftime("%Y-%m", ExpensePayment.payment_date),
        func.sum(ExpensePayment.amount_paid),
    ).join(Expense, Expense.id == ExpensePayment.expense_id).where(
        Expense.status == "Posted",
        ExpensePayment.status == "Posted",
    )
    if year is not None:
        payment_stmt = payment_stmt.where(
            func.strftime("%Y", ExpensePayment.payment_date) == str(year)
        )
    if start_date:
        payment_stmt = payment_stmt.where(ExpensePayment.payment_date >= start_date)
    if end_date:
        payment_stmt = payment_stmt.where(ExpensePayment.payment_date <= end_date)
    for month, total in session.execute(
        payment_stmt.group_by(func.strftime("%Y-%m", ExpensePayment.payment_date))
    ):
        grouped[month]["payments"] += Decimal(str(total or 0))
    return [
        {
            "month": key,
            "expense_total": values["expense_total"].quantize(CENT),
            "payments": values["payments"].quantize(CENT),
            "count": values["count"],
        }
        for key, values in sorted(grouped.items())
    ]


def get_expense_statement(
    session: Session, expense_id: int, *, actor_context: ActorContext
) -> list[dict]:
    require_permission(actor_context, "expenses.view", db_session=session)
    expense = get_expense_by_id(
        session, expense_id, actor_context=actor_context
    )
    if expense is None:
        raise ExpenseValidationError(f"Expense #{expense_id} does not exist.")
    rows = [{
        "date": expense.expense_date,
        "reference": expense.expense_code,
        "description": f"Expense posted — {expense.title}",
        "expense": expense.total_amount,
        "payment": ZERO,
        "status": expense.status,
    }]
    rows.extend({
        "date": payment.payment_date,
        "reference": payment.payment_code,
        "description": f"Expense payment — {payment.payment_method}",
        "expense": ZERO,
        "payment": payment.amount_paid,
        "status": payment.status,
    } for payment in expense.payments)
    return sorted(rows, key=lambda row: row["date"])


def get_expense_dashboard(
    session: Session,
    *,
    actor_context: ActorContext,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    require_permission(actor_context, "expenses.view", db_session=session)
    return calculate_expense_summary(
        session, actor_context=actor_context,
        start_date=start_date, end_date=end_date,
    )
