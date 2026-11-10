import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, HRFlowable
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from database.models import DismantlingEntry, RecoveredComponent, ScrapMaterial, Purchase
from services.security_service import ActorContext, require_permission
from services.dismantling_service import list_dismantling_entries, get_dismantling_metrics
from utils.validators import format_currency


def generate_dismantling_pdf_report(
    session: Session,
    output_path: str,
    start_date: date | None = None,
    end_date: date | None = None,
    category_filter: str = "ALL",
    status_filter: str = "ALL",
    *,
    actor_context: ActorContext,
) -> str:
    """Generates an executive PDF report covering batches, components, scrap, labour, and recovery %."""
    require_permission(actor_context, "dismantling.view", db_session=session)
    entries = list_dismantling_entries(
        session,
        start_date=start_date,
        end_date=end_date,
        category_filter=category_filter,
        status_filter=status_filter,
        sort_by="date_desc",
        actor_context=actor_context,
    )

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0f172a"),
        fontName="Helvetica-Bold",
    )
    sub_style = ParagraphStyle(
        "DocSub",
        parent=styles["Normal"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#64748b"),
    )
    th_style = ParagraphStyle(
        "TH",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=10,
        textColor=colors.HexColor("#ffffff"),
        fontName="Helvetica-Bold",
        alignment=1,
    )
    td_style = ParagraphStyle(
        "TD",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#0f172a"),
    )
    td_num = ParagraphStyle(
        "TDNum",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#0f172a"),
        alignment=2,
    )

    story = []

    # Header
    story.append(Paragraph("ELECTRONIC RECYCLING FACTORY MANAGEMENT SYSTEM", sub_style))
    story.append(Paragraph("<b>Dismantling & Recovery Performance Report</b>", title_style))
    date_filter_str = f"Date Filter: {start_date.strftime('%d-%b-%Y') if start_date else 'All'} to {end_date.strftime('%d-%b-%Y') if end_date else 'Present'}"
    story.append(Paragraph(f"{date_filter_str} | Generated On: {datetime.now().strftime('%d-%b-%Y %I:%M %p')}", sub_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1e3a8a"), spaceAfter=10))

    # KPI Summary Cards in a Table
    metrics = get_dismantling_metrics(
        session,
        start_date=start_date,
        end_date=end_date,
        category_filter=category_filter,
        status_filter=status_filter,
        actor_context=actor_context,
    )
    kpi_data = [
        [
            Paragraph(f"<b>Purchased Appliances:</b> {metrics['total_appliances_purchased']}", td_style),
            Paragraph(f"<b>Dismantled Appliances:</b> {metrics['total_appliances_dismantled']}", td_style),
            Paragraph(f"<b>Pending Quantity:</b> {metrics['pending_dismantling_quantity']}", td_style),
        ],
        [
            Paragraph(f"<b>Total Batches:</b> {metrics['total_dismantling_batches']}", td_style),
            Paragraph(f"<b>Recovered Weight:</b> {metrics['total_recovered_weight']} kg", td_style),
            Paragraph(f"<b>Scrap Weight:</b> {metrics['total_scrap_weight']} kg", td_style),
        ],
        [
            Paragraph(f"<b>Labour Expenses:</b> ₹ {metrics['total_labour_cost']:.2f}", td_style),
            Paragraph(f"<b>Est. Recovery Value:</b> ₹ {metrics['total_recovery_value']:.2f}", td_style),
            Paragraph(f"<b>Filtered Batches in List:</b> {len(entries)}", td_style),
        ],
    ]
    kpi_table = Table(kpi_data, colWidths=[2.5 * inch, 2.5 * inch, 2.5 * inch])
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 14))

    # Batches Table
    batch_rows = [[
        Paragraph("Batch ID", th_style),
        Paragraph("Date", th_style),
        Paragraph("Appliance Category", th_style),
        Paragraph("Dismantled", th_style),
        Paragraph("Rec. Wt (kg)", th_style),
        Paragraph("Scrap Wt (kg)", th_style),
        Paragraph("Recov %", th_style),
        Paragraph("Labour (₹)", th_style),
        Paragraph("Est. Val (₹)", th_style),
        Paragraph("Status", th_style),
    ]]

    for e in entries:
        rec_pct_str = f"{e.recovery_percentage:.1f}%" if e.recovery_percentage is not None else "N/A"
        batch_rows.append([
            Paragraph(e.dismantling_code, td_style),
            Paragraph(e.dismantling_date.strftime("%d-%b-%y"), td_style),
            Paragraph(e.appliance_category, td_style),
            Paragraph(str(e.appliances_dismantled), td_num),
            Paragraph(f"{e.total_recovered_weight:.2f}", td_num),
            Paragraph(f"{e.total_scrap_weight:.2f}", td_num),
            Paragraph(rec_pct_str, td_num),
            Paragraph(f"{e.labour_charges:.2f}", td_num),
            Paragraph(f"{e.estimated_recovery_value:.2f}", td_num),
            Paragraph(e.status, td_style),
        ])

    batch_table = Table(
        batch_rows,
        colWidths=[0.85 * inch, 0.7 * inch, 1.4 * inch, 0.65 * inch, 0.75 * inch, 0.75 * inch, 0.65 * inch, 0.65 * inch, 0.85 * inch, 0.8 * inch]
    )
    batch_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#ffffff"), colors.HexColor("#f8fafc")]),
        ("PADDING", (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(batch_table)
    story.append(Spacer(1, 16))

    datasets = build_dismantling_report_datasets(entries)
    story.append(Paragraph("<b>Appliance-wise Recovery</b>", styles["Heading2"]))
    appliance_rows = [[
        Paragraph("Appliance", th_style),
        Paragraph("Batches", th_style),
        Paragraph("Dismantled", th_style),
        Paragraph("Rec. Wt (kg)", th_style),
        Paragraph("Scrap Wt (kg)", th_style),
        Paragraph("Est. Value (₹)", th_style),
        Paragraph("Labour (₹)", th_style),
    ]]
    for row in datasets["appliance_wise"]:
        appliance_rows.append([
            Paragraph(str(row["appliance_category"]), td_style),
            Paragraph(str(row["batches"]), td_num),
            Paragraph(str(row["appliances_dismantled"]), td_num),
            Paragraph(f"{row['recovered_weight']:.2f}", td_num),
            Paragraph(f"{row['scrap_weight']:.2f}", td_num),
            Paragraph(f"{row['estimated_value']:.2f}", td_num),
            Paragraph(f"{row['labour_cost']:.2f}", td_num),
        ])
    appliance_table = Table(appliance_rows, colWidths=[1.6 * inch, 0.8 * inch, 0.9 * inch, 1.1 * inch, 1.1 * inch, 1.2 * inch, 1.0 * inch])
    appliance_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#ffffff"), colors.HexColor("#f8fafc")]),
        ("PADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(appliance_table)

    doc.build(story)
    return str(Path(output_path).resolve())


def generate_dismantling_excel_report(
    session: Session,
    output_path: str,
    start_date: date | None = None,
    end_date: date | None = None,
    category_filter: str = "ALL",
    status_filter: str = "ALL",
    *,
    actor_context: ActorContext,
) -> str:
    """Generates a multi-tab Excel workbook with summary, batches, components, and scrap breakdown."""
    require_permission(actor_context, "dismantling.view", db_session=session)
    entries = list_dismantling_entries(
        session,
        start_date=start_date,
        end_date=end_date,
        category_filter=category_filter,
        status_filter=status_filter,
        sort_by="date_desc",
        actor_context=actor_context,
    )

    wb = Workbook()

    # Style presets
    header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    regular_font = Font(name="Segoe UI", size=10)
    border_thin = Border(
        left=Side(style="thin", color="E2E8F0"),
        right=Side(style="thin", color="E2E8F0"),
        top=Side(style="thin", color="E2E8F0"),
        bottom=Side(style="thin", color="E2E8F0"),
    )

    # Sheet 1: Dismantling Batches
    ws_batches = wb.active
    ws_batches.title = "Dismantling Batches"

    headers_1 = [
        "Batch ID", "Date", "Purchase ID", "Supplier", "Appliance Category", "Brand",
        "Model", "Dismantled Qty", "Input Wt (kg)", "Rec. Wt (kg)", "Scrap Wt (kg)",
        "Recovery %", "Labour Cost (₹)", "Other Cost (₹)", "Est. Value (₹)", "Status", "Team Leader"
    ]
    ws_batches.append(headers_1)

    for cell in ws_batches[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for e in entries:
        rec_pct = float(e.recovery_percentage) if e.recovery_percentage is not None else "N/A"
        ws_batches.append([
            e.dismantling_code,
            e.dismantling_date.strftime("%Y-%m-%d"),
            e.purchase.purchase_code if e.purchase else "—",
            e.purchase.supplier.business_name if e.purchase and e.purchase.supplier else "—",
            e.appliance_category,
            e.brand or "—",
            e.model_number or "—",
            e.appliances_dismantled,
            float(e.input_weight or 0),
            float(e.total_recovered_weight),
            float(e.total_scrap_weight),
            rec_pct,
            float(e.labour_charges),
            float(e.other_expenses),
            float(e.estimated_recovery_value),
            e.status,
            e.team_leader or "—",
        ])

    for row in ws_batches.iter_rows(min_row=2):
        for cell in row:
            cell.font = regular_font
            cell.border = border_thin

    # Sheet 2: Recovered Components
    ws_comp = wb.create_sheet(title="Recovered Components")
    headers_2 = [
        "Batch ID", "Component Name", "Category", "Quantity", "Unit", "Weight (kg)",
        "Condition", "Est. Unit Value (₹)", "Est. Total Value (₹)", "Storage Location"
    ]
    ws_comp.append(headers_2)
    for cell in ws_comp[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for e in entries:
        for c in e.recovered_components:
            ws_comp.append([
                e.dismantling_code,
                c.component_name,
                c.component_category,
                float(c.quantity),
                c.unit,
                float(c.weight or 0),
                c.condition,
                float(c.estimated_unit_value),
                float(c.estimated_total_value),
                c.storage_location or "—",
            ])

    for row in ws_comp.iter_rows(min_row=2):
        for cell in row:
            cell.font = regular_font
            cell.border = border_thin

    # Sheet 3: Scrap & Unusable Material
    ws_scrap = wb.create_sheet(title="Scrap & Waste")
    headers_3 = [
        "Batch ID", "Scrap Type", "Classification", "Quantity", "Weight (kg)",
        "Unit", "Est. Value (₹)", "Disposal Method"
    ]
    ws_scrap.append(headers_3)
    for cell in ws_scrap[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for e in entries:
        for s in e.scrap_materials:
            ws_scrap.append([
                e.dismantling_code,
                s.scrap_type,
                s.material_classification,
                float(s.quantity),
                float(s.weight),
                s.unit,
                float(s.estimated_value),
                s.disposal_method or "—",
            ])

    for row in ws_scrap.iter_rows(min_row=2):
        for cell in row:
            cell.font = regular_font
            cell.border = border_thin

    datasets = build_dismantling_report_datasets(entries)
    _write_simple_sheet(wb, "Daily Report", [
        "Date", "Batches", "Dismantled Qty", "Rec. Wt (kg)", "Scrap Wt (kg)", "Labour (₹)", "Est. Value (₹)"
    ], [
        [r["date"], r["batches"], r["appliances_dismantled"], float(r["recovered_weight"]),
         float(r["scrap_weight"]), float(r["labour_cost"]), float(r["estimated_value"])]
        for r in datasets["daily"]
    ], header_font, header_fill, regular_font, border_thin)
    _write_simple_sheet(wb, "Monthly Report", [
        "Month", "Batches", "Dismantled Qty", "Rec. Wt (kg)", "Scrap Wt (kg)", "Labour (₹)", "Est. Value (₹)"
    ], [
        [r["month"], r["batches"], r["appliances_dismantled"], float(r["recovered_weight"]),
         float(r["scrap_weight"]), float(r["labour_cost"]), float(r["estimated_value"])]
        for r in datasets["monthly"]
    ], header_font, header_fill, regular_font, border_thin)
    _write_simple_sheet(wb, "Appliance-wise Recovery", [
        "Appliance Category", "Batches", "Dismantled Qty", "Rec. Wt (kg)", "Scrap Wt (kg)", "Labour (₹)", "Est. Value (₹)"
    ], [
        [r["appliance_category"], r["batches"], r["appliances_dismantled"], float(r["recovered_weight"]),
         float(r["scrap_weight"]), float(r["labour_cost"]), float(r["estimated_value"])]
        for r in datasets["appliance_wise"]
    ], header_font, header_fill, regular_font, border_thin)
    _write_simple_sheet(wb, "Labour Cost", [
        "Batch ID", "Date", "Team Leader", "Labour Cost (₹)", "Other Cost (₹)", "Total Processing (₹)"
    ], [
        [r["batch_id"], r["date"], r["team_leader"], float(r["labour_cost"]),
         float(r["other_cost"]), float(r["total_processing_cost"])]
        for r in datasets["labour"]
    ], header_font, header_fill, regular_font, border_thin)
    _write_simple_sheet(wb, "Recovery Percentage", [
        "Batch ID", "Date", "Appliance", "Input Wt (kg)", "Rec. Wt (kg)", "Recovery %"
    ], [
        [r["batch_id"], r["date"], r["appliance_category"], r["input_weight"],
         float(r["recovered_weight"]), r["recovery_percentage"]]
        for r in datasets["recovery_percentage"]
    ], header_font, header_fill, regular_font, border_thin)
    _write_simple_sheet(wb, "Estimated Valuation", [
        "Batch ID", "Date", "Appliance", "Est. Recovery Value (₹)", "Status"
    ], [
        [r["batch_id"], r["date"], r["appliance_category"], float(r["estimated_value"]), r["status"]]
        for r in datasets["valuation"]
    ], header_font, header_fill, regular_font, border_thin)

    wb.save(output_path)
    return str(Path(output_path).resolve())


def build_dismantling_report_datasets(entries: list) -> dict:
    """Builds daily, monthly, appliance-wise, labour, recovery %, and valuation datasets."""
    daily: dict[str, dict] = {}
    monthly: dict[str, dict] = {}
    appliance: dict[str, dict] = {}
    labour = []
    recovery_percentage = []
    valuation = []

    def _bump(bucket: dict, key: str, entry):
        row = bucket.setdefault(key, {
            "batches": 0,
            "appliances_dismantled": 0,
            "recovered_weight": Decimal("0.00"),
            "scrap_weight": Decimal("0.00"),
            "labour_cost": Decimal("0.00"),
            "estimated_value": Decimal("0.00"),
        })
        row["batches"] += 1
        row["appliances_dismantled"] += int(entry.appliances_dismantled or 0)
        row["recovered_weight"] += entry.total_recovered_weight or Decimal("0.00")
        row["scrap_weight"] += entry.total_scrap_weight or Decimal("0.00")
        row["labour_cost"] += entry.labour_charges or Decimal("0.00")
        row["estimated_value"] += entry.estimated_recovery_value or Decimal("0.00")

    for e in entries:
        day_key = e.dismantling_date.strftime("%Y-%m-%d")
        month_key = e.dismantling_date.strftime("%Y-%m")
        _bump(daily, day_key, e)
        _bump(monthly, month_key, e)
        _bump(appliance, e.appliance_category, e)
        labour.append({
            "batch_id": e.dismantling_code,
            "date": e.dismantling_date.strftime("%Y-%m-%d"),
            "team_leader": e.team_leader or "—",
            "labour_cost": e.labour_charges or Decimal("0.00"),
            "other_cost": e.other_expenses or Decimal("0.00"),
            "total_processing_cost": e.total_processing_cost or Decimal("0.00"),
        })
        rec_pct = f"{e.recovery_percentage:.2f}" if e.recovery_percentage is not None else "N/A"
        input_wt = float(e.input_weight) if e.input_weight else "N/A"
        recovery_percentage.append({
            "batch_id": e.dismantling_code,
            "date": e.dismantling_date.strftime("%Y-%m-%d"),
            "appliance_category": e.appliance_category,
            "input_weight": input_wt,
            "recovered_weight": e.total_recovered_weight or Decimal("0.00"),
            "recovery_percentage": rec_pct,
        })
        valuation.append({
            "batch_id": e.dismantling_code,
            "date": e.dismantling_date.strftime("%Y-%m-%d"),
            "appliance_category": e.appliance_category,
            "estimated_value": e.estimated_recovery_value or Decimal("0.00"),
            "status": e.status,
        })

    daily_rows = [{"date": k, **v} for k, v in sorted(daily.items())]
    monthly_rows = [{"month": k, **v} for k, v in sorted(monthly.items())]
    appliance_rows = [{"appliance_category": k, **v} for k, v in sorted(appliance.items())]
    return {
        "daily": daily_rows,
        "monthly": monthly_rows,
        "appliance_wise": appliance_rows,
        "labour": labour,
        "recovery_percentage": recovery_percentage,
        "valuation": valuation,
    }


def _write_simple_sheet(wb, title, headers, rows, header_font, header_fill, regular_font, border_thin):
    ws = wb.create_sheet(title=title)
    ws.append(headers)
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in rows:
        ws.append(row)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = regular_font
            cell.border = border_thin
