from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import (
    Account,
    AccountingException,
    Customer,
    CustomerReceipt,
    CustomerReceiptAllocation,
    DismantlingEntry,
    Expense,
    ExpensePayment,
    JournalEntry,
    JournalLine,
    Purchase,
    SalesInvoice,
    SalesOrderFulfilment,
    SalesOrderFulfilmentItem,
    Supplier,
    SupplierPayment,
    SupplierPaymentAllocation,
)
from services.accounting_common import ZERO
from services.account_service import list_accounts
from services.security_service import ActorContext, require_permission


def _posted_line_totals(
    session: Session, *, as_of: date | None = None,
    start_date: date | None = None, end_date: date | None = None,
) -> dict[int, tuple[Decimal, Decimal]]:
    stmt = (
        select(
            JournalLine.account_id,
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_id)
        .where(JournalEntry.status.in_(("Posted", "Reversed")))
    )
    if as_of:
        stmt = stmt.where(JournalEntry.journal_date <= as_of)
    if start_date:
        stmt = stmt.where(JournalEntry.journal_date >= start_date)
    if end_date:
        stmt = stmt.where(JournalEntry.journal_date <= end_date)
    return {
        int(account_id): (Decimal(str(debit or 0)), Decimal(str(credit or 0)))
        for account_id, debit, credit in session.execute(
            stmt.group_by(JournalLine.account_id)
        )
    }


def get_account_balance(
    session: Session, account_id: int, *, actor_context: ActorContext,
    as_of: date | None = None
) -> Decimal:
    require_permission(actor_context, "accounting.view", db_session=session)
    totals = _posted_line_totals(session, as_of=as_of).get(account_id, (ZERO, ZERO))
    return (totals[0] - totals[1]).quantize(Decimal("0.01"))


def get_general_ledger(
    session: Session, account_id: int, *, start_date: date | None = None,
    end_date: date | None = None, actor_context: ActorContext,
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    account = session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account #{account_id} does not exist.")
    base = select(
        func.coalesce(func.sum(JournalLine.debit), 0),
        func.coalesce(func.sum(JournalLine.credit), 0),
    ).join(JournalEntry, JournalEntry.id == JournalLine.journal_id).where(
        JournalEntry.status.in_(("Posted", "Reversed")),
        JournalLine.account_id == account.id,
    )
    if start_date:
        base = base.where(JournalEntry.journal_date < start_date)
    opening_debit, opening_credit = session.execute(base).one()
    opening_net = Decimal(str(opening_debit or 0)) - Decimal(str(opening_credit or 0))
    if account.normal_balance == "Credit":
        opening = -opening_net
    else:
        opening = opening_net

    stmt = (
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_id)
        .where(
            JournalEntry.status.in_(("Posted", "Reversed")),
            JournalLine.account_id == account.id,
        )
    )
    if start_date:
        stmt = stmt.where(JournalEntry.journal_date >= start_date)
    if end_date:
        stmt = stmt.where(JournalEntry.journal_date <= end_date)
    rows = session.execute(
        stmt.order_by(JournalEntry.journal_date, JournalEntry.id, JournalLine.id)
    ).all()
    running = opening
    result = []
    for line, journal in rows:
        movement = line.debit - line.credit
        if account.normal_balance == "Credit":
            movement = -movement
        running += movement
        result.append({
            "date": journal.journal_date,
            "journal_number": journal.journal_number,
            "description": line.description or journal.description,
            "reference": line.source_reference or journal.reference_number or "",
            "debit": line.debit,
            "credit": line.credit,
            "running_balance": running.quantize(Decimal("0.01")),
            "journal_id": journal.id,
        })
    return {"account": account, "opening_balance": opening, "rows": result}


def get_trial_balance(
    session: Session, *, actor_context: ActorContext, as_of: date | None = None
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    accounts = list_accounts(session, actor_context=actor_context)
    totals = _posted_line_totals(session, as_of=as_of)
    rows = []
    debit_sum = ZERO
    credit_sum = ZERO
    for account in accounts:
        if session.scalar(select(Account.id).where(Account.parent_id == account.id).limit(1)):
            continue
        debit, credit = totals.get(account.id, (ZERO, ZERO))
        net = debit - credit
        debit_balance = max(ZERO, net).quantize(Decimal("0.01"))
        credit_balance = max(ZERO, -net).quantize(Decimal("0.01"))
        if debit_balance or credit_balance:
            rows.append({
                "account": account,
                "debit_balance": debit_balance,
                "credit_balance": credit_balance,
            })
            debit_sum += debit_balance
            credit_sum += credit_balance
    return {
        "rows": rows,
        "total_debit": debit_sum.quantize(Decimal("0.01")),
        "total_credit": credit_sum.quantize(Decimal("0.01")),
        "difference": (debit_sum - credit_sum).quantize(Decimal("0.01")),
        "as_of": as_of,
    }


def get_profit_and_loss(
    session: Session, *, actor_context: ActorContext,
    start_date: date | None = None, end_date: date | None = None
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    totals = _posted_line_totals(session, start_date=start_date, end_date=end_date)
    income, expenses = [], []
    for account in list_accounts(session, actor_context=actor_context):
        if session.scalar(select(Account.id).where(Account.parent_id == account.id).limit(1)):
            continue
        debit, credit = totals.get(account.id, (ZERO, ZERO))
        if account.account_type == "Income" and debit != credit:
            income.append({"account": account, "amount": (credit - debit).quantize(Decimal("0.01"))})
        elif account.account_type == "Expenses" and debit != credit:
            expenses.append({"account": account, "amount": (debit - credit).quantize(Decimal("0.01"))})
    income_total = sum((row["amount"] for row in income), ZERO)
    expense_total = sum((row["amount"] for row in expenses), ZERO)
    cogs = sum(
        (row["amount"] for row in expenses if row["account"].system_key == "cost_of_goods_sold"),
        ZERO,
    )
    return {
        "income": income,
        "expenses": expenses,
        "income_total": income_total.quantize(Decimal("0.01")),
        "expense_total": expense_total.quantize(Decimal("0.01")),
        "cogs": cogs.quantize(Decimal("0.01")),
        "gross_profit": (income_total - cogs).quantize(Decimal("0.01")),
        "net_profit": (income_total - expense_total).quantize(Decimal("0.01")),
        "start_date": start_date,
        "end_date": end_date,
    }


def get_balance_sheet(
    session: Session, *, actor_context: ActorContext, as_of: date | None = None
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    totals = _posted_line_totals(session, as_of=as_of)
    groups = {"Assets": [], "Liabilities": [], "Equity": []}
    for account in list_accounts(session, actor_context=actor_context):
        if account.account_type not in groups:
            continue
        if session.scalar(select(Account.id).where(Account.parent_id == account.id).limit(1)):
            continue
        debit, credit = totals.get(account.id, (ZERO, ZERO))
        balance = debit - credit if account.normal_balance == "Debit" else credit - debit
        balance = balance.quantize(Decimal("0.01"))
        if balance:
            groups[account.account_type].append({"account": account, "amount": balance})
    pnl = get_profit_and_loss(
        session, actor_context=actor_context, end_date=as_of
    )
    current_earnings = pnl["net_profit"]
    asset_total = sum((r["amount"] for r in groups["Assets"]), ZERO)
    liability_total = sum((r["amount"] for r in groups["Liabilities"]), ZERO)
    equity_total = sum((r["amount"] for r in groups["Equity"]), ZERO) + current_earnings
    groups["Equity"].append({
        "account": None,
        "name": "Current period earnings",
        "amount": current_earnings,
    })
    return {
        "groups": groups,
        "assets": asset_total.quantize(Decimal("0.01")),
        "liabilities": liability_total.quantize(Decimal("0.01")),
        "equity": equity_total.quantize(Decimal("0.01")),
        "difference": (asset_total - liability_total - equity_total).quantize(Decimal("0.01")),
        "as_of": as_of,
    }


def get_cash_bank_book(
    session: Session, account_id: int, *, start_date: date | None = None,
    end_date: date | None = None, actor_context: ActorContext,
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    account = session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account #{account_id} does not exist.")
    if account.subtype not in {"Cash", "Bank", "Bank Account", "UPI Settlement", "Clearing"}:
        raise ValueError("Select a cash, bank, UPI settlement or clearing account.")
    return get_general_ledger(
        session, account_id, start_date=start_date, end_date=end_date,
        actor_context=actor_context,
    )


def get_party_ledger(
    session: Session, party_type: str, party_id: int, *,
    start_date: date | None = None, end_date: date | None = None,
    actor_context: ActorContext,
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    if party_type not in {"Customer", "Supplier"}:
        raise ValueError("Party type must be Customer or Supplier.")
    party = session.get(Customer if party_type == "Customer" else Supplier, party_id)
    if party is None:
        raise ValueError(f"{party_type} #{party_id} does not exist.")
    stmt = (
        select(JournalLine, JournalEntry, Account)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalEntry.status.in_(("Posted", "Reversed")),
            JournalLine.party_type == party_type,
            JournalLine.party_id == party_id,
        )
    )
    if start_date:
        stmt = stmt.where(JournalEntry.journal_date >= start_date)
    if end_date:
        stmt = stmt.where(JournalEntry.journal_date <= end_date)
    rows = []
    for line, journal, account in session.execute(
        stmt.order_by(JournalEntry.journal_date, JournalEntry.id, JournalLine.id)
    ):
        rows.append({
            "date": journal.journal_date,
            "journal_number": journal.journal_number,
            "description": line.description or journal.description,
            "account": account,
            "debit": line.debit,
            "credit": line.credit,
            "reference": line.source_reference or journal.reference_number,
        })
    return {"party": party, "party_type": party_type, "rows": rows}


def get_accounting_coverage(
    session: Session, *, actor_context: ActorContext,
    start_date: date | None = None, end_date: date | None = None
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    sources: list[tuple[str, int, str, date | None]] = []
    for row in session.scalars(select(Purchase)):
        if (start_date and row.purchase_date < start_date) or (end_date and row.purchase_date > end_date):
            continue
        sources.append(("Purchase", row.id, "CREATED", row.purchase_date))
    for row in session.scalars(select(SalesInvoice).where(SalesInvoice.status.in_(("Issued", "Void")))):
        if (start_date and row.invoice_date < start_date) or (end_date and row.invoice_date > end_date):
            continue
        sources.append(("Sales Invoice", row.id, "ISSUED", row.invoice_date))
        if row.status == "Void":
            sources.append(("Sales Invoice", row.id, "VOIDED", row.invoice_date))
    for row in session.scalars(select(CustomerReceipt).where(CustomerReceipt.status.in_(("Posted", "Voided")))):
        if (start_date and row.receipt_date < start_date) or (end_date and row.receipt_date > end_date):
            continue
        sources.append(("Customer Receipt", row.id, "POSTED", row.receipt_date))
        if row.status == "Voided":
            sources.append(("Customer Receipt", row.id, "VOIDED", row.receipt_date))
    for row in session.scalars(
        select(CustomerReceiptAllocation).where(
            CustomerReceiptAllocation.allocation_source == "Advance Application"
        )
    ):
        if (start_date and row.allocation_date < start_date) or (end_date and row.allocation_date > end_date):
            continue
        sources.append(("Customer Receipt", row.id, "ADVANCE_APPLIED", row.allocation_date))
    for row in session.scalars(select(SupplierPayment).where(SupplierPayment.status.in_(("Posted", "Voided")))):
        if (start_date and row.payment_date < start_date) or (end_date and row.payment_date > end_date):
            continue
        sources.append(("Supplier Payment", row.id, "POSTED", row.payment_date))
        if row.status == "Voided":
            sources.append(("Supplier Payment", row.id, "VOIDED", row.payment_date))
    for row in session.scalars(
        select(SupplierPaymentAllocation).where(
            SupplierPaymentAllocation.allocation_source == "Advance Application"
        )
    ):
        if (start_date and row.allocation_date < start_date) or (end_date and row.allocation_date > end_date):
            continue
        sources.append(("Supplier Payment", row.id, "ADVANCE_APPLIED", row.allocation_date))
    for row in session.scalars(select(Expense).where(Expense.status.in_(("Posted", "Voided")))):
        if (start_date and row.expense_date < start_date) or (end_date and row.expense_date > end_date):
            continue
        sources.append(("Expense", row.id, "POSTED", row.expense_date))
        if row.status == "Voided":
            sources.append(("Expense", row.id, "VOIDED", row.expense_date))
    for row in session.scalars(select(ExpensePayment).where(ExpensePayment.status.in_(("Posted", "Voided")))):
        if (start_date and row.payment_date < start_date) or (end_date and row.payment_date > end_date):
            continue
        sources.append(("Expense Payment", row.id, "POSTED", row.payment_date))
        if row.status == "Voided":
            sources.append(("Expense Payment", row.id, "VOIDED", row.payment_date))
    dispatch_rows = session.execute(
        select(SalesOrderFulfilmentItem.id, SalesOrderFulfilment.fulfilment_date)
        .join(SalesOrderFulfilment, SalesOrderFulfilment.id == SalesOrderFulfilmentItem.fulfilment_id)
    ).all()
    for source_id, dispatch_date in dispatch_rows:
        day = dispatch_date.date()
        if (start_date and day < start_date) or (end_date and day > end_date):
            continue
        sources.append(("Sales Dispatch", source_id, "DISPATCH", day))
    for row in session.scalars(
        select(DismantlingEntry).where(DismantlingEntry.status == "Approved")
    ):
        if (start_date and row.dismantling_date < start_date) or (end_date and row.dismantling_date > end_date):
            continue
        sources.append(("Dismantling", row.id, "APPROVED", row.dismantling_date))

    posted_keys = set(session.execute(
        select(
            JournalEntry.source_module,
            JournalEntry.source_transaction_id,
            JournalEntry.source_event_type,
        ).where(JournalEntry.status.in_(("Posted", "Reversed")))
    ).all())
    missing = [
        {"source_module": module, "source_id": source_id, "event_type": event, "date": day}
        for module, source_id, event, day in sources
        if (module, source_id, event) not in posted_keys
    ]
    open_exceptions = session.scalar(
        select(func.count(AccountingException.id)).where(AccountingException.status == "Open")
    ) or 0
    return {
        "eligible_source_events": len(sources),
        "posted_source_events": len(sources) - len(missing),
        "unposted_source_events": len(missing),
        "open_exceptions": int(open_exceptions),
        "missing": missing,
        "complete": not missing and not open_exceptions,
    }


def get_opening_balance_preview(
    session: Session, *, actor_context: ActorContext
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    customers = session.scalars(select(Customer).order_by(Customer.customer_code)).all()
    suppliers = session.scalars(select(Supplier).order_by(Supplier.supplier_code)).all()
    customer_rows = [
        {"code": row.customer_code, "name": row.customer_name,
         "source_id": row.id, "debit": Decimal(str(row.opening_balance or 0)).quantize(Decimal("0.01"))}
        for row in customers if Decimal(str(row.opening_balance or 0)) > ZERO
    ]
    supplier_rows = [
        {"code": row.supplier_code, "name": row.business_name,
         "source_id": row.id, "credit": Decimal(str(row.opening_balance or 0)).quantize(Decimal("0.01"))}
        for row in suppliers if Decimal(str(row.opening_balance or 0)) > ZERO
    ]
    customer_total = sum((row["debit"] for row in customer_rows), ZERO)
    supplier_total = sum((row["credit"] for row in supplier_rows), ZERO)
    equity_difference = customer_total - supplier_total
    return {
        "mode": "Preview only — no opening balances have been posted",
        "customers": customer_rows,
        "suppliers": supplier_rows,
        "customer_receivable_total": customer_total,
        "supplier_payable_total": supplier_total,
        "opening_equity_debit": max(ZERO, -equity_difference),
        "opening_equity_credit": max(ZERO, equity_difference),
        "supporting_information_required": True,
        "unverified_sources_are_not_imported": True,
    }


def get_reconciliation_report(
    session: Session, *, actor_context: ActorContext
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    require_permission(actor_context, "expenses.view", db_session=session)
    from services.customer_receipt_service import get_customer_financial_summary
    from services.supplier_payment_service import get_supplier_financial_summary

    customer_total = sum(
        (get_customer_financial_summary(
            session, customer.id, actor_context=actor_context
        )["total_outstanding_receivables"]
         for customer in session.scalars(select(Customer))),
        ZERO,
    )
    supplier_total = sum(
        (get_supplier_financial_summary(
            session, supplier.id, actor_context=actor_context
        )["total_outstanding_payables"]
         for supplier in session.scalars(select(Supplier))),
        ZERO,
    )
    expense_total = ZERO
    for expense in session.scalars(select(Expense).where(Expense.status == "Posted")):
        paid = session.scalar(select(func.coalesce(func.sum(ExpensePayment.amount_paid), ZERO)).where(
            ExpensePayment.expense_id == expense.id, ExpensePayment.status == "Posted"
        )) or ZERO
        expense_total += max(ZERO, expense.total_amount - Decimal(str(paid)))

    def mapped_balance(key: str) -> Decimal | None:
        from services.accounting_common import _get_mapped_account
        try:
            account = _get_mapped_account(session, key)
        except ValueError:
            return None
        return get_account_balance(
            session, account.id, actor_context=actor_context
        )

    ar = mapped_balance("control:accounts_receivable")
    ap = mapped_balance("control:accounts_payable")
    expense_ap = mapped_balance("control:expense_payables")
    return {
        "receivables": {
            "subledger": customer_total,
            "control_account": ar,
            "difference": None if ar is None else customer_total - ar,
        },
        "payables": {
            "subledger": supplier_total,
            "control_account": None if ap is None else -ap,
            "difference": None if ap is None else supplier_total + ap,
        },
        "expense_payables": {
            "subledger": expense_total,
            "control_account": None if expense_ap is None else -expense_ap,
            "difference": None if expense_ap is None else expense_total + expense_ap,
        },
        "inventory": {
            "message": "Raw materials awaiting dismantling and recovered inventory have no automatic cost conversion basis; reconcile against a verified cost ledger.",
            "complete": False,
        },
    }


def _report_rows(
    session: Session, report_type: str, start_date=None, end_date=None, *,
    actor_context: ActorContext,
    account_id: int | None = None, party_type: str | None = None,
    party_id: int | None = None,
):
    coverage = get_accounting_coverage(
        session, actor_context=actor_context,
        start_date=start_date, end_date=end_date
    )
    if report_type == "Trial Balance":
        data = get_trial_balance(
            session, actor_context=actor_context, as_of=end_date
        )
        rows = [
            [row["account"].code, row["account"].name,
             row["debit_balance"], row["credit_balance"]]
            for row in data["rows"]
        ]
        return ["Account code", "Account name", "Debit balance", "Credit balance"], rows, coverage
    if report_type == "Profit & Loss":
        data = get_profit_and_loss(
            session, actor_context=actor_context,
            start_date=start_date, end_date=end_date
        )
        rows = [
            [row["account"].code, row["account"].name, row["amount"]]
            for row in data["income"] + data["expenses"]
        ]
        return ["Account code", "Account name", "Period amount"], rows, coverage
    if report_type == "Balance Sheet":
        data = get_balance_sheet(
            session, actor_context=actor_context, as_of=end_date
        )
        rows = [
            [kind, row.get("account").code if row.get("account") else "",
             row.get("account").name if row.get("account") else row.get("name"),
             row["amount"]]
            for kind, entries in data["groups"].items() for row in entries
        ]
        return ["Section", "Account code", "Account name", "Balance"], rows, coverage
    if report_type in {"General Ledger", "Cash/Bank Book"}:
        if account_id is None:
            raise ValueError(f"Select an account for the {report_type.lower()} report.")
        data = (
            get_cash_bank_book(
                session, account_id, start_date=start_date, end_date=end_date,
                actor_context=actor_context,
            )
            if report_type == "Cash/Bank Book"
            else get_general_ledger(
                session, account_id, start_date=start_date, end_date=end_date,
                actor_context=actor_context,
            )
        )
        rows = [
            [row["date"], row["journal_number"], row["description"], row["reference"],
             row["debit"], row["credit"], row["running_balance"]]
            for row in data["rows"]
        ]
        return [
            "Date", "Journal", "Description", "Reference", "Debit", "Credit", "Running balance"
        ], rows, coverage
    if report_type in {"Customer Ledger", "Supplier Ledger"}:
        if party_id is None:
            raise ValueError(f"Select a party for the {report_type.lower()} report.")
        kind = "Customer" if report_type == "Customer Ledger" else "Supplier"
        data = get_party_ledger(
            session, party_type or kind, party_id,
            start_date=start_date, end_date=end_date,
            actor_context=actor_context,
        )
        rows = [
            [row["date"], row["journal_number"], row["description"],
             row["reference"] or "", row["account"].code, row["account"].name,
             row["debit"], row["credit"]]
            for row in data["rows"]
        ]
        return [
            "Date", "Journal", "Description", "Reference", "Account code",
            "Account name", "Debit", "Credit",
        ], rows, coverage
    if report_type == "Accounting Coverage":
        rows = [
            [row["source_module"], row["source_id"], row["event_type"], row["date"]]
            for row in coverage["missing"]
        ]
        return ["Source", "ID", "Event", "Date"], rows, coverage
    if report_type == "Opening Balance Preview":
        preview = get_opening_balance_preview(
            session, actor_context=actor_context
        )
        rows = [
            ["Customer", row["code"], row["name"], row["debit"], ""]
            for row in preview["customers"]
        ] + [
            ["Supplier", row["code"], row["name"], "", row["credit"]]
            for row in preview["suppliers"]
        ]
        return ["Party type", "Code", "Name", "Debit preview", "Credit preview"], rows, coverage
    if report_type == "Reconciliation":
        reconciliation = get_reconciliation_report(
            session, actor_context=actor_context
        )
        rows = []
        for key in ("receivables", "payables", "expense_payables"):
            record = reconciliation[key]
            rows.append([
                key.replace("_", " ").title(), record["subledger"],
                record["control_account"] if record["control_account"] is not None else "Not configured",
                record["difference"] if record["difference"] is not None else "N/A",
            ])
        rows.append(["Inventory", "", "Review required", reconciliation["inventory"]["message"]])
        return ["Ledger", "Subledger", "Control account", "Difference / note"], rows, coverage
    journals = list_journals(
        session, start_date=start_date, end_date=end_date,
        actor_context=actor_context,
    )
    rows = [
        [journal.journal_number, journal.journal_date, journal.description,
         journal.reference_number or "", journal.source_module,
         journal.total_debit, journal.total_credit, journal.status]
        for journal in journals
    ]
    return [
        "Journal", "Date", "Description", "Reference", "Source",
        "Debit", "Credit", "Status",
    ], rows, coverage


def list_journals(
    session: Session, *, actor_context: ActorContext,
    start_date=None, end_date=None
):
    require_permission(actor_context, "accounting.view", db_session=session)
    stmt = select(JournalEntry).where(JournalEntry.status.in_(("Posted", "Reversed")))
    if start_date:
        stmt = stmt.where(JournalEntry.journal_date >= start_date)
    if end_date:
        stmt = stmt.where(JournalEntry.journal_date <= end_date)
    return list(session.scalars(stmt.order_by(JournalEntry.journal_date, JournalEntry.id)))


def generate_accounting_excel_report(
    session: Session, output_path: str, *, actor_context: ActorContext,
    report_type: str = "Trial Balance",
    start_date: date | None = None, end_date: date | None = None,
    account_id: int | None = None, party_type: str | None = None,
    party_id: int | None = None,
) -> str:
    require_permission(actor_context, "accounting.view", db_session=session)
    headers, rows, coverage = _report_rows(
        session, report_type, start_date, end_date, account_id=account_id,
        actor_context=actor_context, party_type=party_type, party_id=party_id,
    )
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = report_type[:31]
    sheet.append([report_type, f"Generated {datetime.now():%Y-%m-%d %H:%M}"])
    sheet.append([
        "Completeness",
        "Complete" if coverage["complete"] else
        f"Provisional — {coverage['unposted_source_events']} unposted source events; {coverage['open_exceptions']} open exceptions",
    ])
    sheet.append(headers)
    for row in rows:
        sheet.append([float(v) if isinstance(v, Decimal) else v for v in row])
    sheet.freeze_panes = "A4"
    sheet.auto_filter.ref = f"A3:{sheet.cell(sheet.max_row, sheet.max_column).coordinate}"
    for cell in sheet[3]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1E3A8A")
        cell.alignment = Alignment(horizontal="center")
    for col in sheet.columns:
        width = min(max(max((len(str(c.value or "")) for c in col), default=12) + 2, 12), 45)
        sheet.column_dimensions[col[0].column_letter].width = width
    exceptions = workbook.create_sheet("Review Queue")
    exceptions.append(["Source", "ID", "Event", "Reference", "Reason", "Status"])
    for row in session.scalars(select(AccountingException).order_by(AccountingException.id)):
        exceptions.append([
            row.source_module, row.source_transaction_id, row.source_event_type,
            row.source_reference or "", row.reason, row.status,
        ])
    for cell in exceptions[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1E3A8A")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    return output_path


def generate_accounting_pdf_report(
    session: Session, output_path: str, *, actor_context: ActorContext,
    report_type: str = "Trial Balance",
    start_date: date | None = None, end_date: date | None = None,
    account_id: int | None = None, party_type: str | None = None,
    party_id: int | None = None,
) -> str:
    require_permission(actor_context, "accounting.view", db_session=session)
    headers, rows, coverage = _report_rows(
        session, report_type, start_date, end_date, account_id=account_id,
        actor_context=actor_context, party_type=party_type, party_id=party_id,
    )
    styles = getSampleStyleSheet()
    table_data = [headers] + [[str(value) for value in row] for row in rows]
    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("PADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    completeness = "COMPLETE" if coverage["complete"] else (
        f"PROVISIONAL — {coverage['unposted_source_events']} unposted source events; "
        f"{coverage['open_exceptions']} open exceptions"
    )
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        output_path, pagesize=landscape(A4), rightMargin=10 * mm,
        leftMargin=10 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
    ).build([
        Paragraph(f"{report_type} — {completeness}", styles["Title"]),
        Paragraph(
            f"Date range: {start_date or 'All'} to {end_date or 'All'} · "
            f"Generated: {datetime.now():%Y-%m-%d %H:%M}",
            styles["Normal"],
        ),
        Spacer(1, 8),
        table,
        Spacer(1, 8),
        Paragraph(
            "This report includes posted journals only. Unposted, unverified or unmapped "
            "source transactions may make the report incomplete.",
            styles["BodyText"],
        ),
    ])
    return output_path
