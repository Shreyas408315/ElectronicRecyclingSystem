from html import escape
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from database.models import Expense, ExpenseCategory, ExpensePayment
from services.expense_common import (
    ExpenseValidationError,
    _expense_balance,
    _expense_paid,
    _expense_payment_status,
)
from services.expense_service import (
    calculate_category_summary,
    calculate_monthly_summary,
    get_expense_by_id,
    list_expenses,
)
from services.security_service import ActorContext, require_permission


def _expense_report_rows(
    session: Session, *, actor_context: ActorContext, start_date=None, end_date=None
) -> list[tuple[Expense, str, object, object]]:
    require_permission(actor_context, "expenses.view", db_session=session)
    expenses: list[Expense] = []
    offset = 0
    while True:
        page = list_expenses(
            session, start_date=start_date, end_date=end_date,
            status="ALL", offset=offset, limit=500,
            actor_context=actor_context,
        )
        expenses.extend(page)
        if len(page) < 500:
            break
        offset += len(page)
    return [
        (
            expense,
            _expense_payment_status(session, expense),
            _expense_paid(session, expense.id) if expense.status == "Posted" else 0,
            _expense_balance(session, expense) if expense.status == "Posted" else expense.total_amount,
        )
        for expense in expenses
    ]


def generate_expense_pdf(
    session: Session,
    expense_id: int,
    output_path: str,
    *,
    actor_context: ActorContext,
) -> str:
    require_permission(actor_context, "expenses.view", db_session=session)
    expense = get_expense_by_id(
        session, expense_id, actor_context=actor_context
    )
    if expense is None:
        raise ExpenseValidationError(f"Expense #{expense_id} does not exist.")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = f"{'DRAFT ' if expense.status == 'Draft' else 'VOID ' if expense.status == 'Voided' else ''}EXPENSE RECORD"
    rows = [["Description", "Qty", "Unit", "Unit price", "Discount", "Tax", "Line total"]]
    for item in expense.items:
        rows.append([
            Paragraph(escape(item.description), styles["BodyText"]),
            f"{item.quantity:g}", item.unit, f"{item.unit_price:.2f}",
            f"{item.discount:.2f}", f"{item.tax_amount:.2f}", f"{item.line_total:.2f}",
        ])
    if not expense.items:
        rows.append([
            Paragraph(escape(expense.title), styles["BodyText"]), "1", "unit",
            f"{expense.subtotal:.2f}", f"{expense.discount:.2f}",
            f"{expense.tax_amount:.2f}", f"{expense.total_amount:.2f}",
        ])
    table = Table(rows, repeatRows=1, colWidths=[72 * mm, 15 * mm, 18 * mm, 22 * mm, 22 * mm, 18 * mm, 25 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("PADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story = [
        Paragraph(escape(title), styles["Title"]),
        Paragraph(
            f"<b>Expense ID:</b> {escape(expense.expense_code)} &nbsp; "
            f"<b>Status:</b> {escape(expense.status)}",
            styles["Normal"],
        ),
        Paragraph(
            f"<b>Title:</b> {escape(expense.title)} &nbsp; "
            f"<b>Date:</b> {expense.expense_date} &nbsp; "
            f"<b>Due:</b> {expense.due_date or '—'}",
            styles["Normal"],
        ),
        Paragraph(
            f"<b>Category:</b> {escape(expense.category.name)} &nbsp; "
            f"<b>Payee:</b> {escape(expense.payee_name)} &nbsp; "
            f"<b>Vendor reference:</b> {escape(expense.vendor_reference or '—')}",
            styles["Normal"],
        ),
        Spacer(1, 8), table, Spacer(1, 8),
        Paragraph(
            f"Subtotal: {expense.subtotal:.2f} &nbsp; Discount: {expense.discount:.2f} "
            f"&nbsp; Tax: {expense.tax_amount:.2f} &nbsp; "
            f"Additional charges: {expense.additional_charges:.2f}",
            styles["Normal"],
        ),
        Paragraph(f"<b>Total expense: {expense.total_amount:.2f}</b>", styles["Heading2"]),
        Paragraph(
            f"<b>Posted payments:</b> {_expense_paid(session, expense.id):.2f} "
            f"&nbsp; <b>Outstanding:</b> {_expense_balance(session, expense):.2f} "
            f"&nbsp; <b>Payment status:</b> {escape(_expense_payment_status(session, expense))}",
            styles["Normal"],
        ),
        Paragraph(f"<b>Notes:</b> {escape(expense.notes or '—')}", styles["Normal"]),
        Paragraph(
            f"<b>Attachment path:</b> {escape(expense.attachment_path or '—')}",
            styles["Normal"],
        ),
    ]
    SimpleDocTemplate(
        output_path, pagesize=A4, rightMargin=14 * mm, leftMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
    ).build(story)
    return output_path


def generate_expense_excel_report(
    session: Session,
    output_path: str,
    *,
    actor_context: ActorContext,
    start_date=None,
    end_date=None,
) -> str:
    rows = _expense_report_rows(
        session, actor_context=actor_context,
        start_date=start_date, end_date=end_date,
    )
    workbook = Workbook()
    register = workbook.active
    register.title = "Expense Register"
    register.append([
        "Expense ID", "Date", "Due date", "Title", "Category", "Payee",
        "Supplier ID", "Vendor reference", "Subtotal", "Discount",
        "Additional charges", "Tax", "Total", "Posted payments",
        "Outstanding", "Payment status", "Expense status",
    ])
    for expense, payment_status, paid, balance in rows:
        register.append([
            expense.expense_code, expense.expense_date, expense.due_date,
            expense.title, expense.category.name, expense.payee_name,
            expense.supplier.supplier_code if expense.supplier else "",
            expense.vendor_reference or "", float(expense.subtotal),
            float(expense.discount), float(expense.additional_charges),
            float(expense.tax_amount), float(expense.total_amount),
            float(paid), float(balance), payment_status, expense.status,
        ])
    outstanding_sheet = workbook.create_sheet("Outstanding Expenses")
    outstanding_sheet.append([
        "Expense ID", "Date", "Due date", "Title", "Payee",
        "Total", "Paid", "Outstanding", "Payment status",
    ])
    for expense, payment_status, paid, balance in rows:
        if expense.status == "Posted" and balance > 0:
            outstanding_sheet.append([
                expense.expense_code, expense.expense_date, expense.due_date,
                expense.title, expense.payee_name, float(expense.total_amount),
                float(paid), float(balance), payment_status,
            ])

    category_sheet = workbook.create_sheet("Category Summary")
    category_sheet.append(["Category", "Posted expense total", "Expense count"])
    for row in calculate_category_summary(
        session, actor_context=actor_context,
        start_date=start_date, end_date=end_date,
    ):
        category_sheet.append([row["category"], float(row["total"]), row["count"]])

    monthly_sheet = workbook.create_sheet("Monthly Summary")
    monthly_sheet.append([
        "Month", "Posted expenses by expense date",
        "Posted payments by payment date", "Count",
    ])
    monthly_rows = calculate_monthly_summary(
        session, actor_context=actor_context,
        start_date=start_date, end_date=end_date
    )
    for row in monthly_rows:
        monthly_sheet.append([
            row["month"], float(row["expense_total"]), float(row["payments"]), row["count"]
        ])
    yearly_sheet = workbook.create_sheet("Yearly Summary")
    yearly_sheet.append(["Year", "Posted expense total", "Posted payments", "Expense count"])
    yearly = {}
    for row in monthly_rows:
        year = row["month"][:4]
        aggregate = yearly.setdefault(year, [0, 0, 0])
        aggregate[0] += row["expense_total"]
        aggregate[1] += row["payments"]
        aggregate[2] += row["count"]
    for year, values in sorted(yearly.items()):
        yearly_sheet.append([year, float(values[0]), float(values[1]), values[2]])

    payee_sheet = workbook.create_sheet("Payee Summary")
    payee_totals = {}
    for expense, _, _, _ in rows:
        if expense.status == "Posted":
            current = payee_totals.setdefault(expense.payee_name, [0, 0])
            current[0] += expense.total_amount
            current[1] += 1
    payee_sheet.append(["Payee", "Posted expense total", "Expense count"])
    for payee, values in sorted(payee_totals.items()):
        payee_sheet.append([payee, float(values[0]), values[1]])

    payments = session.scalars(
        select(ExpensePayment).options(selectinload(ExpensePayment.expense))
        .order_by(ExpensePayment.payment_date, ExpensePayment.id)
    ).all()
    payment_sheet = workbook.create_sheet("Expense Payments")
    payment_sheet.append([
        "Payment ID", "Expense ID", "Payee", "Date", "Amount",
        "Method", "Reference", "Status",
    ])
    for payment in payments:
        if start_date and payment.payment_date < start_date:
            continue
        if end_date and payment.payment_date > end_date:
            continue
        payment_sheet.append([
            payment.payment_code, payment.expense.expense_code,
            payment.expense.payee_name, payment.payment_date,
            float(payment.amount_paid), payment.payment_method,
            payment.transaction_reference or "", payment.status,
        ])

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1E3A8A")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for column in sheet.columns:
            widths = [len(str(cell.value or "")) for cell in column]
            sheet.column_dimensions[column[0].column_letter].width = min(
                max(max(widths, default=10) + 2, 12), 40
            )
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    return output_path


def generate_expense_pdf_report(
    session: Session,
    output_path: str,
    *,
    actor_context: ActorContext,
    start_date=None,
    end_date=None,
) -> str:
    rows = _expense_report_rows(
        session, actor_context=actor_context,
        start_date=start_date, end_date=end_date,
    )
    styles = getSampleStyleSheet()
    data = [[
        "ID", "Date", "Title", "Category", "Payee", "Total",
        "Paid", "Outstanding", "Payment status", "Expense status",
    ]]
    for expense, payment_status, paid, balance in rows:
        data.append([
            expense.expense_code, str(expense.expense_date),
            Paragraph(escape(expense.title), styles["BodyText"]),
            Paragraph(escape(expense.category.name), styles["BodyText"]),
            Paragraph(escape(expense.payee_name), styles["BodyText"]),
            f"{expense.total_amount:.2f}", f"{paid:.2f}",
            f"{balance:.2f}", payment_status, expense.status,
        ])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("PADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        output_path, pagesize=landscape(A4), rightMargin=10 * mm,
        leftMargin=10 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
    ).build([
        Paragraph("Expense Register & Payment Report", styles["Title"]),
        Paragraph(
            f"Expense date range: {start_date or 'All'} to {end_date or 'All'}. "
            "Expense totals and posted cash payments are shown separately.",
            styles["Normal"],
        ),
        Spacer(1, 8), table,
    ])
    return output_path
