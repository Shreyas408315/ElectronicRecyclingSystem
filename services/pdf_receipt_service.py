from pathlib import Path
from decimal import Decimal
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, HRFlowable
)
from sqlalchemy import select
from sqlalchemy.orm import Session
from database.models import ApplicationSetting, Purchase
from services.security_service import ActorContext, require_permission
from utils.validators import format_currency


def get_factory_info(session: Session, *, actor_context: ActorContext) -> dict:
    """Reads configured factory identity; unconfigured values are never fabricated."""
    require_permission(actor_context, "purchases.view", db_session=session)
    factory = {
        "name": "[Factory name not configured]",
        "subtitle": "",
        "address": "[Address not configured]",
        "phone": "[Phone not configured]",
        "email": "[Email not configured]",
        "gstin": "[GSTIN not configured]",
    }
    profile_keys = {
        "name": "factory_name",
        "address": "factory_address",
        "phone": "factory_phone",
        "email": "factory_email",
        "gstin": "factory_gstin",
    }
    values = dict(session.execute(
        select(ApplicationSetting.key, ApplicationSetting.value).where(
            ApplicationSetting.key.in_(profile_keys.values())
        )
    ).all())
    for field, key in profile_keys.items():
        if values.get(key):
            factory[field] = values[key]
    return factory


def generate_purchase_receipt_pdf(
    session: Session,
    purchase_id: int,
    output_path: str,
    *,
    actor_context: ActorContext,
) -> str:
    """Generates a professional PDF purchase receipt for an inward appliance purchase."""
    require_permission(actor_context, "purchases.view", db_session=session)
    purchase = session.get(Purchase, purchase_id)
    if purchase is None:
        raise ValueError(f"Purchase #{purchase_id} does not exist.")
    factory = get_factory_info(session, actor_context=actor_context)
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0f172a"),
        fontName="Helvetica-Bold",
        alignment=0,
    )

    factory_sub_style = ParagraphStyle(
        "FactorySub",
        parent=styles["Normal"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#475569"),
    )

    box_header_style = ParagraphStyle(
        "BoxHeader",
        parent=styles["Normal"],
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#1e293b"),
        fontName="Helvetica-Bold",
    )

    box_content_style = ParagraphStyle(
        "BoxContent",
        parent=styles["Normal"],
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#334155"),
    )

    th_style = ParagraphStyle(
        "TH",
        parent=styles["Normal"],
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#ffffff"),
        fontName="Helvetica-Bold",
        alignment=1,
    )

    td_style = ParagraphStyle(
        "TD",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#0f172a"),
    )

    td_num_style = ParagraphStyle(
        "TDNum",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#0f172a"),
        alignment=2,
    )

    td_bold_style = ParagraphStyle(
        "TDBold",
        parent=styles["Normal"],
        fontSize=9,
        leading=12,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#0f172a"),
        alignment=2,
    )

    story = []

    # 1. Header & Brand Block
    header_data = [
        [
            Paragraph(f"<b>{factory['name']}</b><br/><font size=8.5 color='#64748b'>{factory['subtitle']}<br/>{factory['address']}<br/>Phone: {factory['phone']} | GSTIN: {factory['gstin']}</font>", factory_sub_style),
            Paragraph(
                f"<font size=16 color='#1e3a8a'><b>PURCHASE RECEIPT</b></font><br/>"
                f"<font size=9 color='#475569'><b>Receipt No:</b> REC-{purchase.purchase_code}<br/>"
                f"<b>Purchase ID:</b> {purchase.purchase_code}<br/>"
                f"<b>Date:</b> {purchase.purchase_date.strftime('%d-%b-%Y')}<br/>"
                f"<b>Payment Status:</b> <font color='{'#15803d' if purchase.payment_status=='Paid' else '#b91c1c'}'><b>{purchase.payment_status.upper()}</b></font></font>",
                box_content_style
            ),
        ]
    ]

    header_table = Table(header_data, colWidths=[3.6 * inch, 3.8 * inch])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(header_table)
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1e3a8a"), spaceAfter=12))

    # 2. Supplier & Procurement Meta Cards
    sup = purchase.supplier
    supplier_info = (
        f"<b>{sup.business_name}</b> ({sup.supplier_code})<br/>"
        f"<b>Contact Person:</b> {sup.contact_person or '—'}<br/>"
        f"<b>Phone:</b> {sup.phone or '—'} | <b>Email:</b> {sup.email or '—'}<br/>"
        f"<b>Address:</b> {sup.address or '—'}, {sup.city or ''} {sup.state or ''} {sup.pin_code or ''}<br/>"
        f"<b>GSTIN:</b> {sup.gstin or '—'}"
    )

    payment_info = (
        f"<b>Payment Method:</b> {purchase.payment_method}<br/>"
        f"<b>Payment Status:</b> {purchase.payment_status}<br/>"
        f"<b>Recorded On:</b> {purchase.created_at.strftime('%d-%b-%Y %I:%M %p')}<br/>"
        f"<b>Raw Incoming Stock:</b> Awaiting Dismantling Entry"
    )

    info_data = [
        [
            Paragraph("<b>SUPPLIER DETAILS</b>", box_header_style),
            Paragraph("<b>TRANSACTION OVERVIEW</b>", box_header_style)
        ],
        [
            Paragraph(supplier_info, box_content_style),
            Paragraph(payment_info, box_content_style)
        ]
    ]
    info_table = Table(info_data, colWidths=[4.2 * inch, 3.2 * inch])
    info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 14))

    # 3. Appliance Items Table
    items_header = [
        Paragraph("#", th_style),
        Paragraph("Appliance Category", th_style),
        Paragraph("Brand & Model", th_style),
        Paragraph("Qty", th_style),
        Paragraph("Unit", th_style),
        Paragraph("Weight (kg)", th_style),
        Paragraph("Rate (₹)", th_style),
        Paragraph("Line Total (₹)", th_style),
    ]

    items_rows = [items_header]
    for idx, item in enumerate(purchase.items, start=1):
        brand_model = f"{item.brand or ''} {item.model_number or ''}".strip() or "—"
        items_rows.append([
            Paragraph(str(idx), td_style),
            Paragraph(item.appliance_category, td_style),
            Paragraph(brand_model, td_style),
            Paragraph(str(item.quantity), td_num_style),
            Paragraph(item.unit, td_style),
            Paragraph(f"{item.weight:.2f}" if item.weight else "—", td_num_style),
            Paragraph(f"{item.rate:.2f}", td_num_style),
            Paragraph(f"{item.line_total:.2f}", td_num_style),
        ])

    items_table = Table(
        items_rows,
        colWidths=[0.35 * inch, 1.8 * inch, 1.45 * inch, 0.55 * inch, 0.55 * inch, 0.85 * inch, 0.9 * inch, 0.95 * inch]
    )
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#ffffff"), colors.HexColor("#f8fafc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # 4. Additional Charges & Financial Breakdown
    financial_rows = [
        [Paragraph("Appliance Subtotal:", td_style), Paragraph(f"₹ {purchase.subtotal:.2f}", td_num_style)],
        [Paragraph("Transportation Charges (+):", td_style), Paragraph(f"₹ {purchase.transportation_charges:.2f}", td_num_style)],
        [Paragraph("Loading Charges (+):", td_style), Paragraph(f"₹ {purchase.loading_charges:.2f}", td_num_style)],
        [Paragraph("Unloading Charges (+):", td_style), Paragraph(f"₹ {purchase.unloading_charges:.2f}", td_num_style)],
        [Paragraph("Other Incurred Expenses (+):", td_style), Paragraph(f"₹ {purchase.other_charges:.2f}", td_num_style)],
        [Paragraph("<b>TOTAL PURCHASE COST:</b>", td_bold_style), Paragraph(f"<b>₹ {purchase.total_amount:.2f}</b>", td_bold_style)],
        [Paragraph("<b>Amount Paid:</b>", td_style), Paragraph(f"₹ {purchase.amount_paid:.2f}", td_num_style)],
        [Paragraph("<b>Remaining Balance Outstanding:</b>", td_bold_style), Paragraph(f"<b>₹ {purchase.balance_amount:.2f}</b>", td_bold_style)],
    ]

    fin_table = Table(financial_rows, colWidths=[2.2 * inch, 1.3 * inch])
    fin_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("BACKGROUND", (0, 5), (-1, 5), colors.HexColor("#eff6ff")),
        ("BACKGROUND", (0, 7), (-1, 7), colors.HexColor("#fef2f2") if purchase.balance_amount > 0 else colors.HexColor("#f0fdf4")),
        ("PADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))

    # Wrap Remarks and Financial Summary side by side
    remarks_text = purchase.remarks if purchase.remarks else "No special terms or remarks recorded."
    remarks_content = [
        Paragraph("<b>Remarks & Inspection Notes:</b>", box_header_style),
        Spacer(1, 4),
        Paragraph(remarks_text, box_content_style),
        Spacer(1, 8),
        Paragraph("<font size=7.5 color='#64748b'><i>Note: In accordance with environmental factory SOP, purchased appliances are raw incoming e-waste. Recovered materials enter active stock only upon formal Dismantling & Recovery certification.</i></font>", box_content_style),
    ]

    summary_wrapper_data = [
        [remarks_content, fin_table]
    ]
    summary_wrapper = Table(summary_wrapper_data, colWidths=[3.9 * inch, 3.5 * inch])
    summary_wrapper.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (1, 0), (1, 0), 10),
    ]))
    story.append(KeepTogether(summary_wrapper))

    story.append(Spacer(1, 28))

    # 5. Signatures Block
    sig_data = [
        [
            Paragraph("____________________________<br/><b>Weighbridge / Inward Clerk</b>", box_content_style),
            Paragraph("____________________________<br/><b>Supplier / Carrier Representative</b>", box_content_style),
            Paragraph("____________________________<br/><b>Authorized Factory Signatory</b>", box_content_style),
        ]
    ]
    sig_table = Table(sig_data, colWidths=[2.4 * inch, 2.5 * inch, 2.5 * inch])
    sig_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(KeepTogether(sig_table))

    doc.build(story)
    return str(Path(output_path).resolve())
