from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from database.models import InventoryItem, StockMovement
from services.security_service import ActorContext, require_permission
from services.inventory_service import get_inventory_metrics, list_inventory_items


def _location(item: InventoryItem) -> str:
    parts = (
        item.storage_warehouse,
        item.storage_area,
        item.rack_number,
        item.shelf_number,
        item.bin_number,
    )
    return " / ".join(part for part in parts if part) or item.storage_location or "Unassigned"


def generate_inventory_excel_report(
    session: Session, output_path: str, *, actor_context: ActorContext
) -> str:
    require_permission(actor_context, "inventory.view", db_session=session)
    items = list_inventory_items(
        session, include_inactive=True, actor_context=actor_context
    )
    movements = list(
        session.scalars(
            select(StockMovement)
            .options(selectinload(StockMovement.inventory_item))
            .order_by(StockMovement.movement_date.desc(), StockMovement.id.desc())
        ).all()
    )
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1E3A8A")
    border = Border(bottom=Side(style="thin", color="CBD5E1"))

    def add_sheet(title: str, headers: list[str], rows: list[list[object]]) -> None:
        ws = wb.create_sheet(title=title)
        ws.append(headers)
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in rows:
            ws.append(row)
        for column in ws.columns:
            values = [str(cell.value or "") for cell in column]
            ws.column_dimensions[column[0].column_letter].width = min(max(max(map(len, values)) + 2, 12), 42)
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.border = border
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    wb.remove(wb.active)
    add_sheet(
        "Current Inventory",
        [
            "Part ID", "Component", "Category", "Appliance Source", "Brand", "Model",
            "Physical Quantity", "Reserved Quantity", "Available Quantity",
            "Unit", "Total Weight (kg)", "Weight Status", "Condition",
            "Cost Price / Unit", "Estimated Selling / Unit", "Cost Quantity Value",
            "Cost Weight Basis", "Estimated Selling Value", "Minimum Stock", "Stock Status",
            "Warehouse", "Area", "Rack", "Shelf", "Bin", "Location", "Active", "Remarks",
        ],
        [
            [
                i.part_code, i.part_name, i.category, i.appliance_source or "", i.brand or "",
                i.model_number or "", float(i.quantity_available or 0),
                float(i.reserved_quantity or 0),
                float((i.quantity_available or 0) - (i.reserved_quantity or 0)), i.unit,
                float(i.total_weight or 0), i.weight_status or "Unverified", i.condition,
                float(i.cost_price or 0), float(i.estimated_selling_price or 0),
                float((i.quantity_available or 0) * (i.cost_price or 0))
                if i.condition.lower() != "unusable waste" else 0,
                float((i.total_weight or 0) * (i.cost_price or 0)),
                float((i.quantity_available or 0) * (i.estimated_selling_price or 0))
                if i.condition.lower() != "unusable waste" else 0,
                float(i.minimum_stock_level) if i.minimum_stock_level is not None else "",
                "Out of stock" if (i.quantity_available or 0) - (i.reserved_quantity or 0) <= 0 else (
                "Low stock" if i.minimum_stock_level is not None and (i.quantity_available or 0) - (i.reserved_quantity or 0) <= i.minimum_stock_level else "In stock"
                ),
                i.storage_warehouse or "", i.storage_area or "", i.rack_number or "",
                i.shelf_number or "", i.bin_number or "", _location(i),
                "Yes" if i.is_active else "No", i.remarks or "",
            ]
            for i in items
        ],
    )

    categories = sorted({item.category for item in items})
    add_sheet(
        "Category Stock",
        ["Category", "Items", "Physical", "Reserved", "Available", "Verified Weight (kg)", "Cost Value", "Estimated Selling Value"],
        [
            [
                category,
                sum(1 for item in items if item.category == category and item.is_active),
                float(sum((item.quantity_available or 0 for item in items if item.category == category and item.is_active), 0)),
                float(sum((item.reserved_quantity or 0 for item in items if item.category == category and item.is_active), 0)),
                float(sum(((item.quantity_available or 0) - (item.reserved_quantity or 0) for item in items if item.category == category and item.is_active), 0)),
                float(sum((item.total_weight or 0 for item in items if item.category == category and item.is_active and item.weight_status == "Verified"), 0)),
                float(sum(((item.quantity_available or 0) * (item.cost_price or 0) for item in items if item.category == category and item.is_active), 0)),
                float(sum(((item.quantity_available or 0) * (item.estimated_selling_price or 0) for item in items if item.category == category and item.is_active), 0)),
            ]
            for category in categories
        ],
    )
    conditions = sorted({item.condition for item in items})
    add_sheet(
        "Condition Stock",
        ["Condition", "Items", "Physical", "Reserved", "Available", "Verified Weight (kg)", "Cost Value", "Estimated Selling Value"],
        [
            [
                condition,
                sum(1 for item in items if item.condition == condition and item.is_active),
                float(sum((item.quantity_available or 0 for item in items if item.condition == condition and item.is_active), 0)),
                float(sum((item.reserved_quantity or 0 for item in items if item.condition == condition and item.is_active), 0)),
                float(sum(((item.quantity_available or 0) - (item.reserved_quantity or 0) for item in items if item.condition == condition and item.is_active), 0)),
                float(sum((item.total_weight or 0 for item in items if item.condition == condition and item.is_active and item.weight_status == "Verified"), 0)),
                float(sum(((item.quantity_available or 0) * (item.cost_price or 0) for item in items if item.condition == condition and item.is_active), 0)),
                float(sum(((item.quantity_available or 0) * (item.estimated_selling_price or 0) for item in items if item.condition == condition and item.is_active), 0)),
            ]
            for condition in conditions
        ],
    )
    add_sheet(
        "Low Stock",
        ["Part ID", "Component", "Category", "Physical", "Reserved", "Available", "Minimum Stock", "Unit", "Location"],
        [
            [i.part_code, i.part_name, i.category, float(i.quantity_available), float(i.reserved_quantity or 0),
             float((i.quantity_available or 0) - (i.reserved_quantity or 0)),
             float(i.minimum_stock_level), i.unit, _location(i)]
            for i in items
            if i.is_active and i.minimum_stock_level is not None
            and (i.quantity_available or 0) - (i.reserved_quantity or 0) <= i.minimum_stock_level
        ],
    )
    add_sheet(
        "Storage Locations",
        ["Part ID", "Component", "Physical", "Reserved", "Available", "Unit", "Weight (kg)", "Weight Status", "Location"],
        [
            [i.part_code, i.part_name, float(i.quantity_available), float(i.reserved_quantity or 0),
             float((i.quantity_available or 0) - (i.reserved_quantity or 0)), i.unit,
             float(i.total_weight), i.weight_status or "Unverified", _location(i)]
            for i in items if i.is_active
        ],
    )
    add_sheet(
        "Stock Movements",
        [
            "Movement ID", "Part ID", "Component", "Type", "Quantity Change", "Weight Change (kg)",
            "Previous Quantity", "New Quantity", "Previous Weight (kg)", "New Weight (kg)",
            "Weight Verified", "Date", "Reference Type", "Reference ID", "From Location",
            "To Location", "Reason", "User",
        ],
        [
            [
                movement.id,
                movement.inventory_item.part_code if movement.inventory_item else "",
                movement.inventory_item.part_name if movement.inventory_item else "",
                movement.movement_type, float(movement.quantity), float(movement.weight or 0),
                float(movement.previous_quantity) if movement.previous_quantity is not None else "",
                float(movement.new_quantity) if movement.new_quantity is not None else "",
                float(movement.previous_weight) if movement.previous_weight is not None else "",
                float(movement.new_weight) if movement.new_weight is not None else "",
                movement.weight_verified,
                movement.movement_date.isoformat() if movement.movement_date else "",
                movement.reference_type, movement.reference_id or "",
                movement.from_location or "", movement.to_location or "",
                movement.reason or movement.remarks or "", movement.user_responsible or "",
            ]
            for movement in movements
        ],
    )
    metrics = get_inventory_metrics(session, actor_context=actor_context)
    add_sheet(
        "Valuation Summary",
        ["Measure", "Amount", "Basis"],
        [
            ["Quantity-based cost value", float(metrics["cost_quantity_value"]), "Physical quantity × cost price per stock unit"],
            ["Weight-based cost reference", float(metrics["cost_weight_value"]), "Recorded weight × cost price; alternative basis, not additive"],
            ["Estimated selling value", float(metrics["estimated_selling_value"]), "Physical saleable quantity × estimated selling price; not realized revenue"],
            ["Working components value", float(metrics["working_components_value"]), "Estimated selling value for Working condition only"],
            ["Non-working components value", float(metrics["non_working_components_value"]), "Estimated selling value; not realized revenue"],
            ["Verified stock weight (kg)", float(metrics["total_verified_weight"]), "Excludes weight entries that are unverified or partial"],
        ],
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return str(path.resolve())


def generate_inventory_pdf_report(
    session: Session, output_path: str, *, actor_context: ActorContext
) -> str:
    require_permission(actor_context, "inventory.view", db_session=session)
    items = list_inventory_items(
        session, include_inactive=True, actor_context=actor_context
    )
    metrics = get_inventory_metrics(session, actor_context=actor_context)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(path),
        pagesize=landscape(letter),
        leftMargin=28,
        rightMargin=28,
        topMargin=30,
        bottomMargin=30,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Electronic Recycling Factory — Inventory Report", styles["Title"]),
        Paragraph(
            f"Active catalogue items: {metrics['total_unique_items']} | "
            f"Physical quantity: {metrics['total_physical_quantity']} | "
            f"Reserved quantity: {metrics['total_reserved_quantity']} | "
            f"Available quantity: {metrics['total_available_quantity']} | "
            f"Verified weight: {metrics['total_verified_weight']} kg"
            + (" (some stock weights are unverified)" if metrics["has_unverified_weights"] else ""),
            styles["Normal"],
        ),
        Spacer(1, 10),
        Paragraph(
            f"Cost value by quantity: {metrics['cost_quantity_value']} | "
            f"Cost value by weight (alternative basis): {metrics['cost_weight_value']} | "
            f"Estimated selling value (not realized revenue): {metrics['estimated_selling_value']}",
            styles["Normal"],
        ),
        Spacer(1, 12),
    ]
    rows = [[
        "Part ID", "Component", "Category", "Physical", "Reserved", "Available", "Unit", "Weight kg",
        "Weight status", "Condition", "Cost value", "Est. sell value", "Location",
    ]]
    for item in items:
        rows.append([
            item.part_code,
            Paragraph(item.part_name, styles["BodyText"]),
            Paragraph(item.category, styles["BodyText"]),
            str(item.quantity_available),
            str(item.reserved_quantity or 0),
            str((item.quantity_available or 0) - (item.reserved_quantity or 0)),
            item.unit,
            str(item.total_weight),
            item.weight_status or "Unverified",
            item.condition,
            str((item.quantity_available or 0) * (item.cost_price or 0)),
            str((item.quantity_available or 0) * (item.estimated_selling_price or 0)),
            Paragraph(_location(item), styles["BodyText"]),
        ])
    table = Table(
        rows,
        repeatRows=1,
        colWidths=[0.65 * inch, 1.15 * inch, 0.85 * inch, 0.48 * inch, 0.48 * inch,
                   0.48 * inch, 0.4 * inch, 0.58 * inch, 0.68 * inch, 0.62 * inch,
                   0.65 * inch, 0.75 * inch, 1.0 * inch],
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
