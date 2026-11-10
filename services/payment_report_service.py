from html import escape
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

from database.models import (
    Customer,
    CustomerReceipt,
    CustomerReceiptAllocation,
    Purchase,
    SalesInvoice,
    Supplier,
    SupplierPayment,
    SupplierPaymentAllocation,
)
from services.customer_receipt_service import (
    calculate_invoice_balance,
    get_customer_financial_summary,
    get_customer_statement,
    get_customer_receipt_summary,
    list_customer_receipts,
)
from services.payment_common import PaymentValidationError, ZERO
from services.sales_invoice_report_service import amount_in_words
from services.supplier_payment_service import (
    calculate_purchase_balance,
    get_supplier_financial_summary,
    get_supplier_statement,
    get_supplier_payment_summary,
    list_supplier_payments,
)
from services.security_service import ActorContext, require_permission


def _factory_settings(
    session: Session, *, actor_context: ActorContext
) -> dict[str, str]:
    from services.settings_service import get_factory_profile

    profile = get_factory_profile(session, actor_context=actor_context)
    return {
        "factory_name": profile["factory_name"] or "Factory name not configured",
        "factory_address": profile["factory_address"] or "Factory address not configured",
        "factory_phone": profile["factory_phone"] or "Phone not configured",
        "factory_gstin": profile["factory_gstin"] or "GSTIN not configured",
    }


def _allocation_rows(
    session: Session, receipt: CustomerReceipt, *, actor_context: ActorContext
) -> list[list[str]]:
    rows = [["Allocation", "Date", "Amount", "Reference"]]
    for allocation in receipt.allocations:
        target = (
            session.get(SalesInvoice, allocation.invoice_id).invoice_code
            if allocation.invoice_id
            else "Customer opening balance"
        )
        rows.append([
            target,
            str(allocation.allocation_date),
            f"{allocation.amount:.2f}",
            allocation.reference or allocation.allocation_source,
        ])
    summary = get_customer_receipt_summary(
        session, receipt, actor_context=actor_context
    )
    if summary["unallocated_amount"] > ZERO:
        rows.append(["Unallocated advance", "—", f"{summary['unallocated_amount']:.2f}", ""])
    return rows


def _payment_allocation_rows(
    session: Session, payment: SupplierPayment, *, actor_context: ActorContext
) -> list[list[str]]:
    rows = [["Allocation", "Date", "Amount", "Reference"]]
    for allocation in payment.allocations:
        target = (
            session.get(Purchase, allocation.purchase_id).purchase_code
            if allocation.purchase_id
            else "Supplier opening balance"
        )
        rows.append([
            target,
            str(allocation.allocation_date),
            f"{allocation.amount:.2f}",
            allocation.reference or allocation.allocation_source,
        ])
    summary = get_supplier_payment_summary(
        session, payment, actor_context=actor_context
    )
    if summary["unallocated_amount"] > ZERO:
        rows.append(["Unallocated supplier advance", "—", f"{summary['unallocated_amount']:.2f}", ""])
    return rows


def _payment_document(
    *,
    title: str,
    code: str,
    transaction_date: object,
    party_name: str,
    party_code: str,
    amount: object,
    method: str,
    reference: str | None,
    status: str,
    notes: str | None,
    factory: dict[str, str],
    allocation_rows: list[list[str]],
    output_path: str,
) -> str:
    if status == "Draft":
        title = f"DRAFT {title}"
    elif status == "Voided":
        title = f"VOID {title}"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(
        output_path, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
    )
    allocation_table = Table(
        allocation_rows, repeatRows=1, colWidths=[64 * mm, 30 * mm, 30 * mm, 50 * mm]
    )
    allocation_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story = [
        Paragraph(escape(factory["factory_name"]), styles["Title"]),
        Paragraph(escape(factory["factory_address"]), styles["Normal"]),
        Paragraph(f"Phone: {escape(factory['factory_phone'])} | GSTIN: {escape(factory['factory_gstin'])}", styles["Normal"]),
        Spacer(1, 8),
        Paragraph(escape(title), styles["Heading1"]),
        Paragraph(
            f"<b>ID:</b> {escape(code)} &nbsp;&nbsp; "
            f"<b>Date:</b> {escape(str(transaction_date))} &nbsp;&nbsp; "
            f"<b>Status:</b> {escape(status)}",
            styles["Normal"],
        ),
        Spacer(1, 8),
        Paragraph(f"<b>Party:</b> {escape(party_name)} ({escape(party_code)})", styles["Normal"]),
        Paragraph(f"<b>Amount:</b> ₹ {amount:.2f}", styles["Normal"]),
        Paragraph(f"<b>Payment method:</b> {escape(method)}", styles["Normal"]),
        Paragraph(f"<b>Transaction reference:</b> {escape(reference or '—')}", styles["Normal"]),
        Spacer(1, 10),
        allocation_table,
        Spacer(1, 8),
        Paragraph(f"<b>Amount in words:</b> {escape(amount_in_words(amount))}", styles["Normal"]),
        Paragraph(f"<b>Notes:</b> {escape(notes or '—')}", styles["Normal"]),
    ]
    document.build(story)
    return output_path


def generate_customer_receipt_pdf(
    session: Session,
    receipt_id: int,
    output_path: str,
    *,
    actor_context: ActorContext,
) -> str:
    require_permission(actor_context, "receipts.view", db_session=session)
    receipt = session.scalar(
        select(CustomerReceipt)
        .options(selectinload(CustomerReceipt.customer), selectinload(CustomerReceipt.allocations))
        .where(CustomerReceipt.id == receipt_id)
    )
    if receipt is None:
        raise PaymentValidationError(f"Receipt #{receipt_id} does not exist.")
    if receipt.status == "Draft":
        raise PaymentValidationError("A draft receipt is not proof of received funds.")
    return _payment_document(
        title="PAYMENT RECEIPT",
        code=receipt.receipt_code,
        transaction_date=receipt.receipt_date,
        party_name=receipt.customer.customer_name,
        party_code=receipt.customer.customer_code,
        amount=receipt.amount_received,
        method=receipt.payment_method,
        reference=receipt.transaction_reference,
        status=receipt.status,
        notes=receipt.notes,
        factory=_factory_settings(session, actor_context=actor_context),
        allocation_rows=_allocation_rows(
            session, receipt, actor_context=actor_context
        ),
        output_path=output_path,
    )


def generate_supplier_payment_voucher_pdf(
    session: Session,
    payment_id: int,
    output_path: str,
    *,
    actor_context: ActorContext,
) -> str:
    require_permission(
        actor_context, "supplier_payments.view", db_session=session
    )
    payment = session.scalar(
        select(SupplierPayment)
        .options(selectinload(SupplierPayment.supplier), selectinload(SupplierPayment.allocations))
        .where(SupplierPayment.id == payment_id)
    )
    if payment is None:
        raise PaymentValidationError(f"Supplier payment #{payment_id} does not exist.")
    if payment.status == "Draft":
        raise PaymentValidationError("A draft payment is not proof of a supplier disbursement.")
    return _payment_document(
        title="SUPPLIER PAYMENT VOUCHER",
        code=payment.payment_code,
        transaction_date=payment.payment_date,
        party_name=payment.supplier.business_name,
        party_code=payment.supplier.supplier_code,
        amount=payment.amount_paid,
        method=payment.payment_method,
        reference=payment.transaction_reference,
        status=payment.status,
        notes=payment.notes,
        factory=_factory_settings(session, actor_context=actor_context),
        allocation_rows=_payment_allocation_rows(
            session, payment, actor_context=actor_context
        ),
        output_path=output_path,
    )


def _payment_rows(
    session: Session, start_date, end_date, *, actor_context: ActorContext
) -> tuple[list, list]:
    return (
        list_customer_receipts(
            session, start_date=start_date, end_date=end_date,
            actor_context=actor_context,
        ),
        list_supplier_payments(
            session, start_date=start_date, end_date=end_date,
            actor_context=actor_context,
        ),
    )


def generate_payment_excel_report(
    session: Session,
    output_path: str,
    *,
    actor_context: ActorContext,
    start_date=None,
    end_date=None,
) -> str:
    require_permission(actor_context, "receipts.view", db_session=session)
    require_permission(actor_context, "supplier_payments.view", db_session=session)
    receipts, payments = _payment_rows(
        session, start_date, end_date, actor_context=actor_context
    )
    workbook = Workbook()
    customer_sheet = workbook.active
    customer_sheet.title = "Customer Receipts"
    customer_sheet.append([
        "Receipt ID", "Customer", "Date", "Amount", "Allocated", "Unallocated",
        "Method", "Reference", "Status",
    ])
    for receipt in receipts:
        summary = get_customer_receipt_summary(
            session, receipt, actor_context=actor_context
        )
        customer_sheet.append([
            receipt.receipt_code, receipt.customer.customer_name, receipt.receipt_date,
            float(receipt.amount_received), float(summary["allocated_amount"]),
            float(summary["unallocated_amount"]), receipt.payment_method,
            receipt.transaction_reference or "", receipt.status,
        ])

    supplier_sheet = workbook.create_sheet("Supplier Payments")
    supplier_sheet.append([
        "Payment ID", "Supplier", "Date", "Amount", "Allocated", "Unallocated",
        "Method", "Reference", "Status",
    ])
    for payment in payments:
        summary = get_supplier_payment_summary(
            session, payment, actor_context=actor_context
        )
        supplier_sheet.append([
            payment.payment_code, payment.supplier.business_name, payment.payment_date,
            float(payment.amount_paid), float(summary["allocated_amount"]),
            float(summary["unallocated_amount"]), payment.payment_method,
            payment.transaction_reference or "", payment.status,
        ])

    invoice_sheet = workbook.create_sheet("Invoice Outstanding")
    invoice_sheet.append(["Invoice ID", "Customer", "Date", "Due Date", "Total", "Received", "Balance", "Payment Status"])
    invoices = session.scalars(
        select(SalesInvoice).options(selectinload(SalesInvoice.customer)).order_by(SalesInvoice.invoice_date)
    ).all()
    for invoice in invoices:
        balance = calculate_invoice_balance(
            session, invoice.id, actor_context=actor_context
        )
        if balance["payment_status"] != "Not Receivable":
            invoice_sheet.append([
                invoice.invoice_code, invoice.customer.customer_name, invoice.invoice_date,
                invoice.due_date, float(invoice.grand_total), float(balance["received"]),
                float(balance["balance"]), balance["payment_status"],
            ])

    purchase_sheet = workbook.create_sheet("Purchase Outstanding")
    purchase_sheet.append([
        "Purchase ID", "Supplier", "Date", "Total", "Historical Paid",
        "New Allocations", "Balance",
    ])
    purchases = session.scalars(
        select(Purchase).options(selectinload(Purchase.supplier)).order_by(Purchase.purchase_date)
    ).all()
    for purchase in purchases:
        balance = calculate_purchase_balance(
            session, purchase.id, actor_context=actor_context
        )
        purchase_sheet.append([
            purchase.purchase_code, purchase.supplier.business_name, purchase.purchase_date,
            float(balance["purchase_total"]), float(balance["historical_amount_paid"]),
            float(balance["new_supplier_payments"]), float(balance["balance"]),
        ])

    customer_advance_sheet = workbook.create_sheet("Customer Advances")
    customer_advance_sheet.append(["Customer ID", "Customer", "Unallocated Advance"])
    for customer in session.scalars(select(Customer).order_by(Customer.customer_name)).all():
        summary = get_customer_financial_summary(
            session, customer.id, actor_context=actor_context
        )
        if summary["unallocated_advances"]:
            customer_advance_sheet.append([
                customer.customer_code, customer.customer_name,
                float(summary["unallocated_advances"]),
            ])

    supplier_advance_sheet = workbook.create_sheet("Supplier Advances")
    supplier_advance_sheet.append(["Supplier ID", "Supplier", "Unallocated Advance"])
    for supplier in session.scalars(select(Supplier).order_by(Supplier.business_name)).all():
        summary = get_supplier_financial_summary(
            session, supplier.id, actor_context=actor_context
        )
        if summary["unallocated_advances"]:
            supplier_advance_sheet.append([
                supplier.supplier_code, supplier.business_name,
                float(summary["unallocated_advances"]),
            ])

    allocation_sheet = workbook.create_sheet("Allocation History")
    allocation_sheet.append([
        "Direction", "Transaction ID", "Party", "Target", "Allocation type",
        "Source", "Date", "Amount", "Reference", "Transaction status",
    ])
    receipt_allocations = session.scalars(
        select(CustomerReceiptAllocation)
        .options(selectinload(CustomerReceiptAllocation.receipt))
        .order_by(CustomerReceiptAllocation.allocation_date, CustomerReceiptAllocation.id)
    ).all()
    for allocation in receipt_allocations:
        receipt = allocation.receipt
        if start_date and allocation.allocation_date < start_date:
            continue
        if end_date and allocation.allocation_date > end_date:
            continue
        customer = session.get(Customer, receipt.customer_id)
        target = (
            session.get(SalesInvoice, allocation.invoice_id).invoice_code
            if allocation.invoice_id else "Customer opening balance"
        )
        allocation_sheet.append([
            "Customer receipt", receipt.receipt_code, customer.customer_name if customer else "",
            target, allocation.allocation_type, allocation.allocation_source,
            allocation.allocation_date, float(allocation.amount), allocation.reference or "",
            receipt.status,
        ])
    payment_allocations = session.scalars(
        select(SupplierPaymentAllocation)
        .options(selectinload(SupplierPaymentAllocation.payment))
        .order_by(SupplierPaymentAllocation.allocation_date, SupplierPaymentAllocation.id)
    ).all()
    for allocation in payment_allocations:
        payment = allocation.payment
        if start_date and allocation.allocation_date < start_date:
            continue
        if end_date and allocation.allocation_date > end_date:
            continue
        supplier = session.get(Supplier, payment.supplier_id)
        target = (
            session.get(Purchase, allocation.purchase_id).purchase_code
            if allocation.purchase_id else "Supplier opening balance"
        )
        allocation_sheet.append([
            "Supplier payment", payment.payment_code, supplier.business_name if supplier else "",
            target, allocation.allocation_type, allocation.allocation_source,
            allocation.allocation_date, float(allocation.amount), allocation.reference or "",
            payment.status,
        ])

    daily_totals: dict[object, list] = {}
    monthly_totals: dict[str, list] = {}
    for receipt in receipts:
        if receipt.status != "Posted":
            continue
        day = receipt.receipt_date
        daily = daily_totals.setdefault(day, [0, 0, 0, 0])
        daily[0] += 1
        daily[1] += receipt.amount_received
        month = day.strftime("%Y-%m")
        monthly = monthly_totals.setdefault(month, [0, 0, 0, 0])
        monthly[0] += 1
        monthly[1] += receipt.amount_received
    for payment in payments:
        if payment.status != "Posted":
            continue
        day = payment.payment_date
        daily = daily_totals.setdefault(day, [0, 0, 0, 0])
        daily[2] += 1
        daily[3] += payment.amount_paid
        month = day.strftime("%Y-%m")
        monthly = monthly_totals.setdefault(month, [0, 0, 0, 0])
        monthly[2] += 1
        monthly[3] += payment.amount_paid

    daily_sheet = workbook.create_sheet("Daily Summary")
    daily_sheet.append([
        "Date", "Posted Receipt Count", "Posted Receipts", "Posted Supplier Payment Count",
        "Posted Supplier Payments",
    ])
    for day, totals in sorted(daily_totals.items()):
        daily_sheet.append([day, totals[0], float(totals[1]), totals[2], float(totals[3])])

    monthly_sheet = workbook.create_sheet("Monthly Summary")
    monthly_sheet.append([
        "Month", "Posted Receipt Count", "Posted Receipts",
        "Posted Supplier Payment Count", "Posted Supplier Payments",
    ])
    for month, totals in sorted(monthly_totals.items()):
        monthly_sheet.append([month, totals[0], float(totals[1]), totals[2], float(totals[3])])

    customer_statement_sheet = workbook.create_sheet("Customer Statements")
    customer_statement_sheet.append(["Customer", "Date", "Reference", "Description", "Debit", "Credit", "Status"])
    for customer in session.scalars(select(Customer).order_by(Customer.customer_name)).all():
        for row in get_customer_statement(
            session, customer.id, actor_context=actor_context
        ):
            customer_statement_sheet.append([
                customer.customer_name, row["date"], row["reference"], row["description"],
                float(row["debit"]), float(row["credit"]), row["status"],
            ])

    supplier_statement_sheet = workbook.create_sheet("Supplier Statements")
    supplier_statement_sheet.append(["Supplier", "Date", "Reference", "Description", "Debit", "Credit", "Status"])
    for supplier in session.scalars(select(Supplier).order_by(Supplier.business_name)).all():
        for row in get_supplier_statement(
            session, supplier.id, actor_context=actor_context
        ):
            supplier_statement_sheet.append([
                supplier.business_name, row["date"], row["reference"], row["description"],
                float(row["debit"]), float(row["credit"]), row["status"],
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


def generate_payment_pdf_report(
    session: Session,
    output_path: str,
    *,
    actor_context: ActorContext,
    start_date=None,
    end_date=None,
) -> str:
    require_permission(actor_context, "receipts.view", db_session=session)
    require_permission(actor_context, "supplier_payments.view", db_session=session)
    receipts, payments = _payment_rows(
        session, start_date, end_date, actor_context=actor_context
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Customer Receipt and Supplier Payment Register", styles["Title"]),
        Paragraph(
            f"Date range: {start_date or 'All'} to {end_date or 'All'} "
            "— cash movement register; not a profit or accounting report.",
            styles["Normal"],
        ),
        Spacer(1, 8),
    ]
    for title, transactions, is_receipt in (
        ("Customer Receipts", receipts, True),
        ("Supplier Payments", payments, False),
    ):
        data = [["ID", "Party", "Date", "Amount", "Allocated", "Advance", "Method", "Status"]]
        for transaction in transactions:
            summary = (
                get_customer_receipt_summary(
                    session, transaction, actor_context=actor_context
                )
                if is_receipt else get_supplier_payment_summary(
                    session, transaction, actor_context=actor_context
                )
            )
            party = (
                transaction.customer.customer_name
                if is_receipt else transaction.supplier.business_name
            )
            data.append([
                transaction.receipt_code if is_receipt else transaction.payment_code,
                Paragraph(escape(party), styles["BodyText"]),
                str(transaction.receipt_date if is_receipt else transaction.payment_date),
                f"{transaction.amount_received if is_receipt else transaction.amount_paid:.2f}",
                f"{summary['allocated_amount']:.2f}",
                f"{summary['unallocated_amount']:.2f}",
                transaction.payment_method,
                transaction.status,
            ])
        story.extend([Paragraph(title, styles["Heading2"]), Table(data, repeatRows=1), Spacer(1, 8)])
        story[-2].setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))

    customer_balances = [["Customer", "Opening due", "Invoice due", "Advances", "Total due"]]
    for customer in session.scalars(select(Customer).order_by(Customer.customer_name)).all():
        summary = get_customer_financial_summary(
            session, customer.id, actor_context=actor_context
        )
        customer_balances.append([
            Paragraph(escape(customer.customer_name), styles["BodyText"]),
            f"{summary['remaining_opening_receivable']:.2f}",
            f"{summary['issued_invoice_receivables']:.2f}",
            f"{summary['unallocated_advances']:.2f}",
            f"{summary['total_outstanding_receivables']:.2f}",
        ])
    story.extend([
        Paragraph("Customer Outstanding Summary", styles["Heading2"]),
        Table(customer_balances, repeatRows=1),
        Spacer(1, 8),
    ])
    supplier_balances = [["Supplier", "Opening due", "Purchase due", "Advances", "Total due"]]
    for supplier in session.scalars(select(Supplier).order_by(Supplier.business_name)).all():
        summary = get_supplier_financial_summary(
            session, supplier.id, actor_context=actor_context
        )
        supplier_balances.append([
            Paragraph(escape(supplier.business_name), styles["BodyText"]),
            f"{summary['remaining_opening_payable']:.2f}",
            f"{summary['purchase_payables']:.2f}",
            f"{summary['unallocated_advances']:.2f}",
            f"{summary['total_outstanding_payables']:.2f}",
        ])
    story.extend([
        Paragraph("Supplier Outstanding Summary", styles["Heading2"]),
        Table(supplier_balances, repeatRows=1),
        Spacer(1, 8),
    ])
    for table in (story[-2], story[-5]):
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(output_path, pagesize=landscape(A4)).build(story)
    return output_path
