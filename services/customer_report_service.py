from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy.orm import Session

from database.models import Customer
from services.security_service import ActorContext, require_permission
from services.customer_service import (
    CUSTOMER_TYPES,
    get_customer_metrics,
    list_customers,
)


def _payment_term(customer: Customer) -> str:
    if customer.payment_terms == "Custom":
        return f"Custom ({customer.custom_payment_days or 0} days)"
    return customer.payment_terms


def generate_customer_excel_report(
    session: Session, output_path: str, *, actor_context: ActorContext
) -> str:
    require_permission(actor_context, "customers.view", db_session=session)
    customers = list_customers(
        session, sort_by="code_asc", actor_context=actor_context
    )
    metrics = get_customer_metrics(session, actor_context=actor_context)
    workbook = Workbook()
    workbook.remove(workbook.active)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1E3A8A")
    border = Border(bottom=Side(style="thin", color="CBD5E1"))

    def add_sheet(title: str, headers: list[str], rows: list[list[object]]) -> None:
        sheet = workbook.create_sheet(title=title)
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in rows:
            sheet.append(row)
        for column in sheet.columns:
            lengths = [len(str(cell.value or "")) for cell in column]
            sheet.column_dimensions[column[0].column_letter].width = min(max(max(lengths, default=10) + 2, 12), 42)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.border = border

    add_sheet(
        "Customer Directory",
        [
            "Customer ID", "Name", "Type", "Contact Person", "Phone", "Alternate Phone",
            "Email", "GSTIN", "PAN", "Billing Address", "Shipping Address", "City",
            "State", "PIN", "Country", "Opening Balance", "Credit Limit",
            "Payment Terms", "Preferred Payment Method", "Status", "Created",
        ],
        [[
            customer.customer_code, customer.customer_name, customer.customer_type,
            customer.contact_person or "", customer.phone or "", customer.alternate_phone or "",
            customer.email or "", customer.gstin or "", customer.pan or "",
            customer.billing_address or "", customer.shipping_address or "", customer.city or "",
            customer.state or "", customer.pin_code or "", customer.country,
            float(customer.opening_balance), "" if customer.credit_limit is None else float(customer.credit_limit),
            _payment_term(customer), customer.preferred_payment_method or "",
            "Active" if customer.is_active else "Inactive",
            customer.created_at.isoformat(sep=" ", timespec="seconds") if customer.created_at else "",
        ] for customer in customers],
    )
    add_sheet(
        "By Customer Type",
        ["Customer Type", "Total", "Active", "Inactive"],
        [[
            customer_type,
            sum(customer.customer_type == customer_type for customer in customers),
            sum(customer.customer_type == customer_type and customer.is_active for customer in customers),
            sum(customer.customer_type == customer_type and not customer.is_active for customer in customers),
        ] for customer_type in CUSTOMER_TYPES],
    )
    add_sheet(
        "Active Status",
        ["Status", "Customers"],
        [
            ["Active", metrics["active"]],
            ["Inactive", metrics["inactive"]],
        ],
    )
    add_sheet(
        "Opening Balances",
        ["Customer ID", "Name", "Type", "Opening Balance", "Credit Limit", "Status"],
        [[
            customer.customer_code, customer.customer_name, customer.customer_type,
            float(customer.opening_balance),
            "" if customer.credit_limit is None else float(customer.credit_limit),
            "Active" if customer.is_active else "Inactive",
        ] for customer in customers if customer.opening_balance > 0],
    )
    add_sheet(
        "Credit Review",
        ["Metric", "Value", "Interpretation"],
        [
            ["Sales-related outstanding receivables", "Not available", "Sales/invoice and payment ledgers are not implemented."],
            ["Customers exceeding credit limits", "Not available", "Cannot determine without authoritative sales-related outstanding balances."],
            ["Overdue customers", "Not available", "No invoice due dates or payment records are available."],
            ["Total opening balances", float(metrics["total_opening_balance"]), "Opening balances only; not sales-related receivables."],
        ],
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return str(path.resolve())


def generate_customer_pdf_report(
    session: Session, output_path: str, *, actor_context: ActorContext
) -> str:
    require_permission(actor_context, "customers.view", db_session=session)
    customers = list_customers(
        session, sort_by="name_asc", actor_context=actor_context
    )
    metrics = get_customer_metrics(session, actor_context=actor_context)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(path), pagesize=landscape(letter), leftMargin=28, rightMargin=28, topMargin=30, bottomMargin=30
    )
    story = [
        Paragraph("Electronic Recycling Factory — Customer Directory", styles["Title"]),
        Paragraph(
            f"Customers: {metrics['total']} | Active: {metrics['active']} | "
            f"Inactive: {metrics['inactive']} | Total opening balances: "
            f"{metrics['total_opening_balance']}",
            styles["Normal"],
        ),
        Spacer(1, 6),
        Paragraph(
            "Sales-related receivables, overdue status, and credit-limit breaches are not reported: "
            "Sales/Invoice and Payment ledgers are not implemented. Opening balances are shown separately.",
            styles["Normal"],
        ),
        Spacer(1, 10),
    ]
    rows = [["ID", "Customer", "Type", "Contact", "Phone", "City", "GSTIN", "Opening ₹", "Credit Limit ₹", "Status"]]
    for customer in customers:
        rows.append([
            customer.customer_code,
            Paragraph(customer.customer_name, styles["BodyText"]),
            customer.customer_type,
            Paragraph(customer.contact_person or "—", styles["BodyText"]),
            customer.phone or "—",
            customer.city or "—",
            customer.gstin or "—",
            f"{customer.opening_balance:,.2f}",
            "Not set" if customer.credit_limit is None else f"{customer.credit_limit:,.2f}",
            "Active" if customer.is_active else "Inactive",
        ])
    table = Table(
        rows, repeatRows=1,
        colWidths=[0.7 * inch, 1.55 * inch, 0.9 * inch, 1.0 * inch, 0.9 * inch,
                   0.8 * inch, 1.1 * inch, 0.9 * inch, 0.95 * inch, 0.7 * inch],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    doc.build(story)
    return str(path.resolve())
