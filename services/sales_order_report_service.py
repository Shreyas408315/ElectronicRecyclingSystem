from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from database.models import (
    SalesOrder,
    SalesOrderFulfilment,
    SalesOrderFulfilmentItem,
    SalesOrderReservation,
)
from services.security_service import ActorContext, require_permission
from services.sales_order_service import get_sales_order_by_id, list_sales_orders


def _orders(
    session: Session,
    rows: list[SalesOrder] | None,
    actor_context: ActorContext,
) -> list[SalesOrder]:
    if rows is not None:
        order_ids = [order.id for order in rows]
        if not order_ids:
            return []
        found = session.scalars(
            select(SalesOrder)
            .options(
                selectinload(SalesOrder.customer),
                selectinload(SalesOrder.items),
            )
            .where(SalesOrder.id.in_(order_ids))
        ).unique().all()
        by_id = {order.id: order for order in found}
        if any(order_id not in by_id for order_id in order_ids):
            raise ValueError("Selected sales orders are not available in this database.")
        return [by_id[order_id] for order_id in order_ids]
    return list_sales_orders(session, actor_context=actor_context)


def generate_sales_order_excel_report(
    session: Session,
    output_path: str,
    orders: list[SalesOrder] | None = None,
    *,
    actor_context: ActorContext,
) -> str:
    require_permission(actor_context, "sales.view", db_session=session)
    rows = _orders(session, orders, actor_context)
    workbook = Workbook()
    register = workbook.active
    register.title = "Sales Order Register"
    register.append([
        "Order ID", "Customer", "Order Date", "Expected Delivery", "Status",
        "Subtotal", "Item Discounts", "Order Discount", "Additional Charges", "Tax", "Order Total",
        "Quantity Ordered", "Quantity Fulfilled", "Notes",
    ])
    for order in rows:
        register.append([
            order.order_code, order.customer.customer_name if order.customer else "",
            order.order_date, order.expected_delivery_date, order.status,
            float(order.subtotal or 0),
            float(sum((line.discount or 0 for line in order.items), 0)),
            float(order.discount_amount or 0),
            float(order.additional_charges or 0), float(order.tax_amount or 0),
            float(order.grand_total or 0),
            float(sum((line.quantity_ordered or 0 for line in order.items), 0)),
            float(sum((line.quantity_fulfilled or 0 for line in order.items), 0)),
            order.notes or "",
        ])

    fulfilments = workbook.create_sheet("Fulfilments")
    fulfilments.append([
        "Fulfilment ID", "Order ID", "Customer", "Date", "Part ID",
        "Component", "Quantity Dispatched", "Unit", "Responsible User", "Remarks",
    ])
    fulfilment_rows = session.scalars(
        select(SalesOrderFulfilment)
        .options(
            selectinload(SalesOrderFulfilment.sales_order).selectinload(SalesOrder.customer),
            selectinload(SalesOrderFulfilment.items).selectinload(
                SalesOrderFulfilmentItem.inventory_item
            ),
        )
        .order_by(SalesOrderFulfilment.fulfilment_date.desc())
    ).all()
    for fulfilment in fulfilment_rows:
        for line in fulfilment.items:
            inventory_item = line.inventory_item
            fulfilments.append([
                fulfilment.fulfilment_code, fulfilment.sales_order.order_code,
                fulfilment.sales_order.customer.customer_name,
                fulfilment.fulfilment_date,
                inventory_item.part_code if inventory_item else "",
                inventory_item.part_name if inventory_item else "",
                float(line.quantity), inventory_item.unit if inventory_item else "",
                fulfilment.responsible_user or "", fulfilment.remarks or "",
            ])

    reservations = workbook.create_sheet("Reservation History")
    reservations.append([
        "Order ID", "Event", "Part ID", "Component", "Quantity", "Unit",
        "Date", "Reason", "Responsible User", "Fulfilment ID",
    ])
    reservation_rows = session.scalars(
        select(SalesOrderReservation)
        .options(selectinload(SalesOrderReservation.sales_order))
        .order_by(SalesOrderReservation.created_at.desc())
    ).all()
    for event in reservation_rows:
        reservations.append([
            event.sales_order.order_code if event.sales_order else "",
            event.event_type, event.part_code_snapshot, event.product_name_snapshot,
            float(event.quantity), event.unit_snapshot, event.created_at,
            event.reason, event.responsible_user or "", event.fulfilment_id or "",
        ])

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1E3A8A")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for column in sheet.columns:
            values = [str(cell.value or "") for cell in column]
            sheet.column_dimensions[column[0].column_letter].width = min(
                max(max(map(len, values)) + 2, 12), 40
            )
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    return output_path


def generate_sales_order_pdf_report(
    session: Session,
    output_path: str,
    orders: list[SalesOrder] | None = None,
    *,
    actor_context: ActorContext,
) -> str:
    require_permission(actor_context, "sales.view", db_session=session)
    rows = _orders(session, orders, actor_context)
    styles = getSampleStyleSheet()
    data = [[
        "Order ID", "Customer", "Date", "Status", "Order Total",
        "Ordered Qty", "Fulfilled Qty",
    ]]
    for order in rows:
        data.append([
            order.order_code,
            Paragraph(order.customer.customer_name if order.customer else "", styles["BodyText"]),
            str(order.order_date), order.status, f"{order.grand_total:.2f}",
            str(sum((line.quantity_ordered or 0 for line in order.items), 0)),
            str(sum((line.quantity_fulfilled or 0 for line in order.items), 0)),
        ])
    document = SimpleDocTemplate(
        output_path, pagesize=landscape(letter),
        rightMargin=0.35 * inch, leftMargin=0.35 * inch,
    )
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
        Paragraph("Sales Order Register", styles["Title"]),
        Paragraph("Order commitments only — not recognized sales revenue.", styles["BodyText"]),
        Spacer(1, 12), table,
    ])
    return output_path


def generate_order_confirmation_pdf(
    session: Session,
    order_id: int,
    output_path: str,
    *,
    actor_context: ActorContext,
) -> str:
    require_permission(actor_context, "sales.view", db_session=session)
    order = get_sales_order_by_id(session, order_id, actor_context=actor_context)
    if not order:
        raise ValueError(f"Sales order #{order_id} was not found.")
    styles = getSampleStyleSheet()
    item_rows = [["Part ID", "Component", "Qty", "Unit", "Unit Price", "Discount", "Tax %", "Line Total"]]
    for line in order.items:
        item_rows.append([
            line.part_code_snapshot,
            Paragraph(line.product_name_snapshot, styles["BodyText"]),
            str(line.quantity_ordered), line.unit,
            f"{line.unit_selling_price:.2f}", f"{line.discount:.2f}",
            f"{line.tax_rate:.3f}", f"{line.line_total:.2f}",
        ])
    item_table = Table(item_rows, repeatRows=1)
    item_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    summary = [
        ["Subtotal", f"{order.subtotal:.2f}"],
        ["Discount", f"{order.discount_amount:.2f}"],
        ["Additional charges", f"{order.additional_charges:.2f}"],
        ["Tax", f"{order.tax_amount:.2f}"],
        ["Order total", f"{order.grand_total:.2f}"],
    ]
    document = SimpleDocTemplate(output_path, pagesize=letter)
    story = [
        Paragraph("ORDER CONFIRMATION", styles["Title"]),
        Paragraph("This is an order confirmation only. It is not a tax invoice or payment receipt.", styles["Heading3"]),
        Spacer(1, 8),
        Paragraph(
            f"<b>Order:</b> {order.order_code} &nbsp;&nbsp; "
            f"<b>Status:</b> {order.status} &nbsp;&nbsp; <b>Date:</b> {order.order_date}",
            styles["BodyText"],
        ),
        Paragraph(
            f"<b>Customer:</b> {order.customer.customer_name}<br/>"
            f"<b>Contact:</b> {order.customer.phone or '—'}<br/>"
            f"<b>Expected delivery:</b> {order.expected_delivery_date or '—'}",
            styles["BodyText"],
        ),
        Spacer(1, 12), item_table, Spacer(1, 12),
        Table(summary, colWidths=[2.5 * inch, 1.2 * inch]),
        Spacer(1, 8), Paragraph(f"Notes: {order.notes or '—'}", styles["BodyText"]),
    ]
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    document.build(story)
    return output_path
