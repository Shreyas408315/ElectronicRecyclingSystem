from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QVBoxLayout,
    QWidget,
)

from database.connection import get_db
from services.customer_service import get_customer_by_id
from services.security_service import AuthorizationError, current_actor_context
from services.sales_order_service import get_customer_sales_orders
from services.sales_invoice_service import get_customer_sales_invoices
from services.customer_receipt_service import (
    calculate_invoice_balance,
    get_customer_financial_summary,
    get_customer_statement,
    get_customer_receipt_summary,
    list_customer_receipts,
)
from utils.validators import format_currency


class CustomerDetailDialog(QDialog):
    customer_modified = Signal(int)

    def __init__(self, parent=None, customer_id: int = 0):
        super().__init__(parent)
        self.customer_id = customer_id
        self.setWindowTitle("Customer Details")
        self.setModal(True)
        self.resize(820, 650)
        self._load_data()
        self._init_ui()

    def _load_data(self):
        self.access_error = None
        try:
            actor_context = current_actor_context()
            if actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                customer = get_customer_by_id(
                    db, self.customer_id, actor_context=actor_context
                )
                if customer is None:
                    self.customer_data = None
                    self.financial_summary = {}
                    self.sales_invoice_data = []
                    self.sales_order_data = []
                    self.receipt_data = []
                    self.statement_data = []
                    return
                sales_orders = get_customer_sales_orders(
                    db, self.customer_id, actor_context=actor_context
                )
                self.financial_summary = get_customer_financial_summary(
                    db, self.customer_id, actor_context=actor_context
                )
                self.sales_invoice_data = [
                    (
                        invoice.invoice_code,
                        invoice.invoice_date,
                        invoice.due_date,
                        invoice.grand_total,
                        calculate_invoice_balance(
                            db, invoice.id, actor_context=actor_context
                        )["received"],
                        calculate_invoice_balance(
                            db, invoice.id, actor_context=actor_context
                        )["balance"],
                        calculate_invoice_balance(
                            db, invoice.id, actor_context=actor_context
                        )["payment_status"],
                    )
                    for invoice in get_customer_sales_invoices(
                        db, self.customer_id, actor_context=actor_context
                    )
                    if invoice.status in ("Issued", "Partially Paid", "Paid", "Overdue")
                ]
                self.receipt_data = [
                    (
                        receipt,
                        get_customer_receipt_summary(
                            db, receipt, actor_context=actor_context
                        ),
                    )
                    for receipt in list_customer_receipts(
                        db, actor_context=actor_context,
                        customer_id=self.customer_id, status="ALL"
                    )
                ]
                self.statement_data = get_customer_statement(
                    db, self.customer_id, actor_context=actor_context
                )
                self.sales_order_data = [
                    (order.order_code, order.order_date, order.status, order.grand_total)
                    for order in sales_orders
                ]
                self.customer_data = None if customer is None else {
                    "id": customer.id,
                    "code": customer.customer_code,
                    "name": customer.customer_name,
                    "type": customer.customer_type,
                    "contact_person": customer.contact_person,
                    "phone": customer.phone,
                    "alternate_phone": customer.alternate_phone,
                    "email": customer.email,
                    "gstin": customer.gstin,
                    "pan": customer.pan,
                    "billing_address": customer.billing_address,
                    "shipping_address": customer.shipping_address,
                    "city": customer.city,
                    "state": customer.state,
                    "pin_code": customer.pin_code,
                    "country": customer.country,
                    "opening_balance": customer.opening_balance,
                    "credit_limit": customer.credit_limit,
                    "payment_terms": customer.payment_terms,
                    "custom_payment_days": customer.custom_payment_days,
                    "preferred_payment_method": customer.preferred_payment_method,
                    "notes": customer.notes,
                    "is_active": customer.is_active,
                    "created_at": customer.created_at,
                    "updated_at": customer.updated_at,
                }
        except AuthorizationError as error:
            self.access_error = str(error)
            self.customer_data = None
            self.financial_summary = {}
            self.sales_invoice_data = []
            self.sales_order_data = []
            self.receipt_data = []
            self.statement_data = []
        if self.customer_data is None:
            self.financial_summary = {}
            self.receipt_data = []
            self.statement_data = []

    def _init_ui(self):
        layout = QVBoxLayout(self)
        if not self.customer_data:
            message = self.access_error or "Customer record was not found."
            layout.addWidget(QLabel(f"Customer details unavailable: {message}"))
            close = QPushButton("Close")
            close.clicked.connect(self.accept)
            layout.addWidget(close)
            return

        data = self.customer_data
        header = QGroupBox()
        header_layout = QVBoxLayout(header)
        title_row = QHBoxLayout()
        name = QLabel(data["name"])
        name.setObjectName("detailSupplierName")
        code = QLabel(data["code"])
        code.setObjectName("codeBadge")
        status = QLabel("Active" if data["is_active"] else "Inactive")
        status.setObjectName("statusBadge")
        title_row.addWidget(name)
        title_row.addWidget(code)
        title_row.addWidget(status)
        title_row.addStretch()
        header_layout.addLayout(title_row)
        header_layout.addWidget(QLabel(
            f"{data['type']}  ·  Registered {data['created_at']:%d-%b-%Y}" if data["created_at"] else data["type"]
        ))
        layout.addWidget(header)

        self.tabs = QTabWidget()
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        profile = QGroupBox("Customer and Contact Information")
        grid = QGridLayout(profile)
        fields = [
            ("Customer type", data["type"]),
            ("Contact person", data["contact_person"]),
            ("Primary phone", data["phone"]),
            ("Alternate phone", data["alternate_phone"]),
            ("Email", data["email"]),
            ("GSTIN", data["gstin"]),
            ("PAN", data["pan"]),
            ("City / State", ", ".join(part for part in (data["city"], data["state"]) if part)),
            ("PIN / Country", " ".join(part for part in (data["pin_code"], data["country"]) if part)),
            ("Billing address", data["billing_address"]),
            ("Shipping address", data["shipping_address"]),
        ]
        for index, (label, value) in enumerate(fields):
            row, col = divmod(index, 2)
            grid.addWidget(QLabel(f"<b>{label}:</b>"), row, col * 2)
            value_label = QLabel(str(value or "—"))
            value_label.setWordWrap(True)
            grid.addWidget(value_label, row, col * 2 + 1)
        overview_layout.addWidget(profile)

        finance = QGroupBox("Financial and Account Information")
        finance_layout = QGridLayout(finance)
        payment_terms = data["payment_terms"]
        if payment_terms == "Custom":
            payment_terms += f" ({data['custom_payment_days'] or 0} days)"
        financial_fields = [
            ("Original opening receivable", format_currency(self.financial_summary["original_opening_receivable"])),
            ("Remaining opening receivable", format_currency(self.financial_summary["remaining_opening_receivable"])),
            ("Issued invoice receivables", format_currency(self.financial_summary["issued_invoice_receivables"])),
            ("Total posted receipts", format_currency(self.financial_summary["total_posted_receipts"])),
            ("Unallocated customer advances", format_currency(self.financial_summary["unallocated_advances"])),
            ("Total outstanding receivables", format_currency(self.financial_summary["total_outstanding_receivables"])),
            ("Net customer balance", format_currency(self.financial_summary["net_customer_balance"])),
            ("Credit limit", "Not set" if data["credit_limit"] is None else format_currency(data["credit_limit"])),
            ("Payment terms", payment_terms),
            ("Preferred payment method", data["preferred_payment_method"]),
            ("Account status", "Active" if data["is_active"] else "Inactive"),
        ]
        for index, (label, value) in enumerate(financial_fields):
            row, col = divmod(index, 2)
            finance_layout.addWidget(QLabel(f"<b>{label}:</b>"), row, col * 2)
            value_label = QLabel(str(value or "—"))
            value_label.setWordWrap(True)
            finance_layout.addWidget(value_label, row, col * 2 + 1)
        notes_row = (len(financial_fields) + 1) // 2
        finance_layout.addWidget(QLabel("<b>Notes:</b>"), notes_row, 0)
        notes = QLabel(data["notes"] or "—")
        notes.setWordWrap(True)
        finance_layout.addWidget(notes, notes_row, 1, 1, 3)
        overview_layout.addWidget(finance)
        self.tabs.addTab(overview, "Overview")

        sales_tab = QWidget()
        sales_layout = QVBoxLayout(sales_tab)
        sales_note = QLabel(
            "Sales orders are shown as order commitments only; they are not recognized revenue, invoices, or receivables."
        )
        sales_note.setObjectName("muted")
        sales_note.setWordWrap(True)
        sales_layout.addWidget(sales_note)
        sales_table = QTableWidget(len(self.sales_order_data), 4)
        sales_table.setHorizontalHeaderLabels(["Order ID", "Order date", "Status", "Order total"])
        sales_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        sales_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for row, values in enumerate(self.sales_order_data):
            for col, value in enumerate(values):
                rendered = format_currency(value) if col == 3 else str(value)
                sales_table.setItem(row, col, QTableWidgetItem(rendered))
        sales_table.setEditTriggers(QTableWidget.NoEditTriggers)
        sales_layout.addWidget(sales_table)
        if not self.sales_order_data:
            sales_layout.addWidget(QLabel("No sales orders are recorded for this customer."))
        self.tabs.addTab(sales_tab, "Sales History")

        invoice_tab = QWidget()
        invoice_layout = QVBoxLayout(invoice_tab)
        invoice_note = QLabel(
            "Issued invoices only. Paid and outstanding balances are calculated from posted receipt allocations."
        )
        invoice_note.setObjectName("muted")
        invoice_note.setWordWrap(True)
        invoice_layout.addWidget(invoice_note)
        invoice_table = QTableWidget(len(self.sales_invoice_data), 7)
        invoice_table.setHorizontalHeaderLabels([
            "Invoice ID", "Invoice date", "Due date", "Invoice total",
            "Received", "Outstanding", "Payment status",
        ])
        invoice_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        invoice_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for row, values in enumerate(self.sales_invoice_data):
            for col, value in enumerate(values):
                rendered = format_currency(value) if col in (3, 4, 5) else str(value or "—")
                invoice_table.setItem(row, col, QTableWidgetItem(rendered))
        invoice_table.setEditTriggers(QTableWidget.NoEditTriggers)
        invoice_layout.addWidget(invoice_table)
        if not self.sales_invoice_data:
            invoice_layout.addWidget(QLabel("No issued invoices are recorded for this customer."))
        self.tabs.addTab(invoice_tab, "Invoice History")

        payment_tab = QWidget()
        payment_layout = QVBoxLayout(payment_tab)
        payment_note = QLabel(
            "Posted receipts affect balances; draft and voided receipts remain visible for audit only. "
            "Opening receivables and unallocated advances are shown separately."
        )
        payment_note.setWordWrap(True)
        payment_layout.addWidget(payment_note)
        receipt_table = QTableWidget(len(self.receipt_data), 7)
        receipt_table.setHorizontalHeaderLabels([
            "Receipt ID", "Date", "Amount", "Allocated", "Advance", "Method", "Status",
        ])
        receipt_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        receipt_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for row, (receipt, summary) in enumerate(self.receipt_data):
            values = (
                receipt.receipt_code, receipt.receipt_date,
                format_currency(receipt.amount_received),
                format_currency(summary["allocated_amount"]),
                format_currency(summary["unallocated_amount"]),
                receipt.payment_method, receipt.status,
            )
            for col, value in enumerate(values):
                receipt_table.setItem(row, col, QTableWidgetItem(str(value)))
        receipt_table.setEditTriggers(QTableWidget.NoEditTriggers)
        payment_layout.addWidget(receipt_table)
        payment_layout.addWidget(QLabel("Customer statement"))
        statement_table = QTableWidget(len(self.statement_data), 6)
        statement_table.setHorizontalHeaderLabels([
            "Date", "Reference", "Description", "Debit", "Credit", "Status",
        ])
        statement_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for row, values in enumerate(self.statement_data):
            cells = (
                values["date"] or "—", values["reference"], values["description"],
                format_currency(values["debit"]), format_currency(values["credit"]),
                values["status"],
            )
            for col, value in enumerate(cells):
                statement_table.setItem(row, col, QTableWidgetItem(str(value)))
        statement_table.setEditTriggers(QTableWidget.NoEditTriggers)
        payment_layout.addWidget(statement_table)
        payment_layout.addStretch()
        self.tabs.addTab(payment_tab, "Payment History")
        layout.addWidget(self.tabs, 1)
        footer = QHBoxLayout()
        footer.addStretch()
        close = QPushButton("Close")
        close.setObjectName("secondaryButton")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)
