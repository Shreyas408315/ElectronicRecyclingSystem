from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from database.models import SalesInvoice, SalesInvoiceHistory
from services.security_service import ActorContext, require_permission
from services.sales_invoice_service import (
    INVOICED_STATUSES,
    get_sales_invoice_by_id,
    list_sales_invoices,
)


_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _under_thousand(value: int) -> str:
    words: list[str] = []
    if value >= 100:
        words.extend((_ONES[value // 100], "hundred"))
        value %= 100
    if value >= 20:
        words.append(_TENS[value // 10])
        value %= 10
    if value:
        words.append(_ONES[value])
    return " ".join(words)


def amount_in_words(amount) -> str:
    paise_value = int((amount * 100).quantize(0)) if hasattr(amount, "quantize") else round(amount * 100)
    rupees, paise = divmod(paise_value, 100)
    if rupees == 0:
        rupee_words = "zero"
    else:
        groups = (
            (1_000_000_000, "arab"),
            (10_000_000, "crore"),
            (100_000, "lakh"),
            (1_000, "thousand"),
            (1, ""),
        )
        chunks: list[str] = []
        remainder = rupees
        for divisor, label in groups:
            part, remainder = divmod(remainder, divisor)
            if part:
                chunks.append(_under_thousand(part) + (f" {label}" if label else ""))
        rupee_words = " ".join(chunks)
    words = f"Rupees {rupee_words}"
    if paise:
        words += f" and {_under_thousand(paise)} paise"
    return words + " only"


def _invoice_rows(
    session: Session,
    invoices: list[SalesInvoice] | None,
    actor_context: ActorContext,
) -> list[SalesInvoice]:
    if invoices is not None:
        invoice_ids = [invoice.id for invoice in invoices]
        if not invoice_ids:
            return []
        found = session.scalars(
            select(SalesInvoice)
            .options(
                selectinload(SalesInvoice.sales_order),
                selectinload(SalesInvoice.items),
            )
            .where(SalesInvoice.id.in_(invoice_ids))
        ).unique().all()
        by_id = {invoice.id: invoice for invoice in found}
        if any(invoice_id not in by_id for invoice_id in invoice_ids):
            raise ValueError("Selected sales invoices are not available in this database.")
        return [by_id[invoice_id] for invoice_id in invoice_ids]
    return list_sales_invoices(session, actor_context=actor_context)


def generate_sales_invoice_excel_report(
    session: Session,
    output_path: str,
    invoices: list[SalesInvoice] | None = None,
    *,
    actor_context: ActorContext,
) -> str:
    require_permission(actor_context, "invoices.view", db_session=session)
    rows = _invoice_rows(session, invoices, actor_context)
    workbook = Workbook()
    register = workbook.active
    register.title = "Invoice Register"
    register.append([
        "Invoice ID", "Customer", "Sales Order", "Invoice Date", "Due Date", "Status",
        "Tax Treatment", "Subtotal", "Discount", "Taxable Amount", "CGST", "SGST",
        "IGST", "Tax", "Invoice Total",
    ])
    for invoice in rows:
        register.append([
            invoice.invoice_code, invoice.customer_name_snapshot,
            invoice.sales_order.order_code if invoice.sales_order else "",
            invoice.invoice_date, invoice.due_date, invoice.status, invoice.tax_treatment,
            float(invoice.subtotal), float(invoice.discount_amount),
            float(invoice.taxable_amount), float(invoice.cgst_amount),
            float(invoice.sgst_amount), float(invoice.igst_amount),
            float(invoice.tax_amount), float(invoice.grand_total),
        ])

    tax_sheet = workbook.create_sheet("Tax Summary")
    tax_sheet.append(["Invoice ID", "Status", "Tax Treatment", "Taxable Amount", "CGST", "SGST", "IGST", "Total Tax"])
    for invoice in rows:
        if invoice.status not in INVOICED_STATUSES:
            continue
        tax_sheet.append([
            invoice.invoice_code, invoice.status, invoice.tax_treatment,
            float(invoice.taxable_amount), float(invoice.cgst_amount),
            float(invoice.sgst_amount), float(invoice.igst_amount), float(invoice.tax_amount),
        ])

    customer_sheet = workbook.create_sheet("By Customer")
    customer_sheet.append(["Customer", "Invoice Count", "Invoice Register Total", "Issued Total"])
    customer_totals: dict[str, list] = {}
    for invoice in rows:
        totals = customer_totals.setdefault(invoice.customer_name_snapshot, [0, 0, 0])
        totals[0] += 1
        totals[1] += invoice.grand_total
        if invoice.status in INVOICED_STATUSES:
            totals[2] += invoice.grand_total
    for customer_name, totals in sorted(customer_totals.items()):
        customer_sheet.append([customer_name, totals[0], float(totals[1]), float(totals[2])])

    status_sheet = workbook.create_sheet("By Status")
    status_sheet.append(["Status", "Invoice Count", "Invoice Register Total"])
    status_totals: dict[str, list] = {}
    for invoice in rows:
        totals = status_totals.setdefault(invoice.status, [0, 0])
        totals[0] += 1
        totals[1] += invoice.grand_total
    for status, totals in sorted(status_totals.items()):
        status_sheet.append([status, totals[0], float(totals[1])])

    for sheet_name, selected_status in (
        ("Draft Invoices", "Draft"),
        ("Issued Invoices", "Issued"),
        ("Voided Invoices", "Void"),
    ):
        status_register = workbook.create_sheet(sheet_name)
        status_register.append(["Invoice ID", "Customer", "Invoice Date", "Due Date", "Total", "Status"])
        for invoice in rows:
            if invoice.status == selected_status:
                status_register.append([
                    invoice.invoice_code, invoice.customer_name_snapshot,
                    invoice.invoice_date, invoice.due_date,
                    float(invoice.grand_total), invoice.status,
                ])

    history_sheet = workbook.create_sheet("Generation History")
    history_sheet.append(["Invoice ID", "Event", "From Status", "To Status", "Date", "Reason", "Responsible User"])
    history_rows = session.scalars(
        select(SalesInvoiceHistory)
        .options(selectinload(SalesInvoiceHistory.invoice))
        .order_by(SalesInvoiceHistory.created_at.desc())
    ).all()
    for event in history_rows:
        history_sheet.append([
            event.invoice.invoice_code, event.event_type, event.from_status or "",
            event.to_status or "", event.created_at, event.reason or "",
            event.responsible_user or "",
        ])
    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1E3A8A")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for column in sheet.columns:
            lengths = [len(str(cell.value or "")) for cell in column]
            sheet.column_dimensions[column[0].column_letter].width = min(max(max(lengths) + 2, 12), 40)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    return output_path


def generate_sales_invoice_pdf_report(
    session: Session,
    output_path: str,
    invoices: list[SalesInvoice] | None = None,
    *,
    actor_context: ActorContext,
) -> str:
    require_permission(actor_context, "invoices.view", db_session=session)
    rows = _invoice_rows(session, invoices, actor_context)
    styles = getSampleStyleSheet()
    data = [["Invoice ID", "Customer", "Date", "Due Date", "Status", "Taxable", "Tax", "Total"]]
    for invoice in rows:
        data.append([
            invoice.invoice_code, Paragraph(invoice.customer_name_snapshot, styles["BodyText"]),
            str(invoice.invoice_date), str(invoice.due_date or "—"), invoice.status,
            f"{invoice.taxable_amount:.2f}", f"{invoice.tax_amount:.2f}", f"{invoice.grand_total:.2f}",
        ])
    document = SimpleDocTemplate(output_path, pagesize=landscape(A4), rightMargin=8 * mm, leftMargin=8 * mm)
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
    ]))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    document.build([
        Paragraph("Sales Invoice Register", styles["Title"]),
        Paragraph("Invoice billing records only. Collections and receivables are unavailable until Payments exists.", styles["BodyText"]),
        Spacer(1, 10), table,
    ])
    return output_path


def generate_sales_invoice_pdf(
    session: Session,
    invoice_id: int,
    output_path: str,
    *,
    actor_context: ActorContext,
) -> str:
    require_permission(actor_context, "invoices.view", db_session=session)
    invoice = get_sales_invoice_by_id(session, invoice_id, actor_context=actor_context)
    if not invoice:
        raise ValueError(f"Invoice #{invoice_id} was not found.")
    styles = getSampleStyleSheet()
    title = {
        "Draft": "DRAFT",
        "Cancelled": "CANCELLED DRAFT",
        "Void": "VOIDED SALES INVOICE",
    }.get(invoice.status, "SALES INVOICE")
    elements = [
        Paragraph(title, styles["Title"]),
        Paragraph(invoice.factory_name_snapshot or "Factory name not configured", styles["Heading2"]),
        Paragraph(invoice.factory_address_snapshot or "Factory address not configured", styles["BodyText"]),
        Paragraph(f"Factory GSTIN: {invoice.factory_gstin_snapshot or 'Not configured'}", styles["BodyText"]),
        Spacer(1, 6),
        Paragraph(f"<b>Invoice ID:</b> {invoice.invoice_code} &nbsp; <b>Date:</b> {invoice.invoice_date} "
                  f"&nbsp; <b>Due:</b> {invoice.due_date or '—'}<br/>"
                  f"<b>Source sales order:</b> {invoice.sales_order.order_code if invoice.sales_order else '—'}",
                  styles["BodyText"]),
        Paragraph(
            f"<b>Customer:</b> {invoice.customer_name_snapshot} ({invoice.customer_code_snapshot})<br/>"
            f"<b>GSTIN:</b> {invoice.customer_gstin_snapshot or 'Not provided'}<br/>"
            f"<b>Billing address:</b> {invoice.billing_address_snapshot or 'Not provided'}<br/>"
            f"<b>Shipping address:</b> {invoice.shipping_address_snapshot or 'Not provided'}",
            styles["BodyText"],
        ),
        Spacer(1, 8),
    ]
    headers = [
        "Part ID", "Description", "Fulfilment", "HSN/SAC", "Qty", "Unit",
        "Rate", "Discount", "Taxable", "CGST", "SGST", "IGST", "Line Total",
    ]
    table_rows = [headers]
    for line in invoice.items:
        table_rows.append([
            line.part_code_snapshot or "",
            Paragraph(line.description or line.product_name_snapshot, styles["BodyText"]),
            (
                f"{line.fulfilment_item.fulfilment.fulfilment_code} "
                f"({line.fulfilment_item.fulfilment.fulfilment_date})"
                if line.fulfilment_item and line.fulfilment_item.fulfilment else ""
            ),
            line.hsn_sac_code or "", str(line.quantity), line.unit,
            f"{line.unit_price:.2f}", f"{line.discount:.2f}", f"{line.taxable_amount:.2f}",
            f"{line.cgst_amount:.2f}", f"{line.sgst_amount:.2f}", f"{line.igst_amount:.2f}",
            f"{line.line_total:.2f}",
        ])
    table = Table(
        table_rows, repeatRows=1,
        colWidths=[15, 42, 30, 15, 12, 12, 17, 17, 18, 15, 15, 15, 19],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
    ]))
    elements.extend([
        table,
        Spacer(1, 8),
        Paragraph(
            f"Tax treatment: {invoice.tax_treatment.replace('_', ' ')}<br/>"
            f"Subtotal: {invoice.subtotal:.2f}<br/>Discount: {invoice.discount_amount:.2f}<br/>"
            f"Additional charges: {invoice.additional_charges:.2f}<br/>"
            f"Taxable amount: {invoice.taxable_amount:.2f}<br/>"
            f"CGST: {invoice.cgst_amount:.2f} &nbsp; SGST: {invoice.sgst_amount:.2f} "
            f"&nbsp; IGST: {invoice.igst_amount:.2f}<br/>"
            f"Grand total: <b>{invoice.grand_total:.2f}</b><br/>"
            f"<b>Amount in words:</b> {amount_in_words(invoice.grand_total)}",
            styles["BodyText"],
        ),
        Spacer(1, 8),
        Paragraph(f"<b>Terms and conditions:</b> {invoice.terms_and_conditions or 'Not specified'}", styles["BodyText"]),
    ])
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(output_path, pagesize=A4, rightMargin=12 * mm, leftMargin=12 * mm).build(elements)
    return output_path
