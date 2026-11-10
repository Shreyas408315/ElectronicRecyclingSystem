from datetime import date

from PySide6.QtCore import Qt, QDate
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)
from services.customer_receipt_service import calculate_invoice_balance
from services.inventory_service import get_inventory_movements
from services.security_service import (
    AuthorizationError,
    actor_has_permission,
    current_actor_context,
)

from database.connection import get_db
from services.sales_invoice_report_service import (
    generate_sales_invoice_excel_report,
    generate_sales_invoice_pdf,
    generate_sales_invoice_pdf_report,
)
from services.sales_invoice_service import (
    INVOICE_STATUSES,
    cancel_draft_invoice,
    create_invoice_from_fulfilled_order,
    get_invoice_factory_settings,
    get_sales_invoice_by_id,
    get_sales_invoice_metrics,
    get_uninvoiced_fulfilment_lines,
    issue_sales_invoice,
    list_invoiceable_sales_orders,
    list_sales_invoices,
    update_draft_invoice,
    update_invoice_factory_settings,
    void_sales_invoice,
)
from utils.validators import format_currency


def _trusted_actor_context():
    actor_context = current_actor_context()
    if actor_context is None:
        raise AuthorizationError("Sign in is required.")
    return actor_context


def _as_qdate(value: date | None) -> QDate:
    return QDate(value.year, value.month, value.day) if value else QDate.currentDate()


class InvoiceFactorySettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Invoice Factory & GST Settings")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        self.fields: dict[str, QLineEdit] = {}
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                values = get_invoice_factory_settings(
                    db, actor_context=actor_context
                )
        except AuthorizationError as error:
            QMessageBox.warning(self, "Invoice settings unavailable", str(error))
            self.reject()
            return
        for key, label in (
            ("name", "Factory name"),
            ("address", "Registered address"),
            ("gstin", "Factory GSTIN"),
            ("state", "Factory state"),
        ):
            field = QLineEdit(values.get(key) or "")
            self.fields[key] = field
            form.addRow(label, field)
        form.addRow(QLabel(
            "GST is applied only when you explicitly choose an intra-state or inter-state treatment. "
            "No registration or tax applicability is inferred."
        ))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self):
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                update_invoice_factory_settings(
                    db,
                    {key: field.text() for key, field in self.fields.items()},
                    actor_context=actor_context,
                )
        except (ValueError, TypeError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Settings not saved", str(exc))
            return
        self.accept()


class SalesInvoiceForm(QDialog):
    def __init__(self, parent=None, invoice_id: int | None = None):
        super().__init__(parent)
        self.invoice_id = invoice_id
        self.actor_context = current_actor_context()
        self.setWindowTitle("Edit Draft Invoice" if invoice_id else "Create Invoice from Fulfilled Order")
        self.setMinimumSize(900, 600)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDate(QDate.currentDate())
        self.due_date = QDateEdit()
        self.due_date.setCalendarPopup(True)
        self.due_date.setDate(QDate.currentDate())
        self.order = QComboBox()
        self.order.setEnabled(invoice_id is None)
        self.tax_treatment = QComboBox()
        self.tax_treatment.addItem("No GST (explicit)", "NO_GST")
        self.tax_treatment.addItem("Intra-state GST (CGST + SGST)", "INTRA_STATE_GST")
        self.tax_treatment.addItem("Inter-state GST (IGST)", "INTER_STATE_GST")
        self.notes = QLineEdit()
        self.terms = QLineEdit()
        form.addRow("Fulfilled sales order", self.order)
        form.addRow("Invoice date", self.date)
        form.addRow("Due date", self.due_date)
        form.addRow("Tax treatment", self.tax_treatment)
        form.addRow("Notes", self.notes)
        form.addRow("Terms and conditions", self.terms)
        layout.addLayout(form)
        self.lines = QTableWidget(0, 8)
        self.lines.setHorizontalHeaderLabels([
            "Fulfilment", "Dispatch date", "Part ID", "Product",
            "Dispatched", "Previously invoiced", "Available", "Invoice quantity",
        ])
        self.lines.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.lines.setSelectionBehavior(QTableWidget.SelectRows)
        self.lines.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        layout.addWidget(self.lines, 1)
        self.warning = QLabel(
            "Invoice only quantities already dispatched. Payment status is derived only from posted receipts."
        )
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.order.currentIndexChanged.connect(self._load_source_lines)
        self.source_lines: list[dict] = []
        if invoice_id:
            self._load_invoice()
        else:
            self.orders = []
            try:
                if self.actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                with get_db() as db:
                    self.orders = list_invoiceable_sales_orders(
                        db, actor_context=self.actor_context
                    )
            except AuthorizationError as error:
                self.warning.setText(f"Invoice creation unavailable: {error}")
                buttons.button(QDialogButtonBox.Save).setEnabled(False)
            for order in self.orders:
                self.order.addItem(
                    f"{order.order_code} — {order.customer.customer_name}", order.id
                )
            if not self.orders:
                self.warning.setText("No uninvoiced fulfilled quantities are available.")
                buttons.button(QDialogButtonBox.Save).setEnabled(False)
            else:
                self._load_source_lines()
        save_button = buttons.button(QDialogButtonBox.Save)
        if self.actor_context is None:
            save_button.setEnabled(False)
        else:
            with get_db() as db:
                save_button.setEnabled(
                    save_button.isEnabled()
                    and actor_has_permission(
                        self.actor_context, "invoices.manage", db_session=db
                    )
                )

    def _load_source_lines(self, *_args):
        order_id = self.order.currentData()
        if not order_id:
            return
        try:
            if self.actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                self.source_lines = get_uninvoiced_fulfilment_lines(
                    db, order_id, actor_context=self.actor_context
                )
        except AuthorizationError as error:
            self.source_lines = []
            self.lines.setRowCount(0)
            self.warning.setText(f"Invoice source data unavailable: {error}")
            return
        self.lines.setRowCount(len(self.source_lines))
        for row, item in enumerate(self.source_lines):
            values = (
                item["fulfilment_code"], item["fulfilment_date"], item["part_code"],
                item["product_name"], item["quantity_fulfilled"], item["quantity_invoiced"],
                item["quantity_available"], item["quantity_available"],
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.UserRole, item["fulfilment_item_id"])
                if column != 7:
                    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                self.lines.setItem(row, column, cell)

    def _load_invoice(self):
        try:
            if self.actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                invoice = get_sales_invoice_by_id(
                    db, self.invoice_id, actor_context=self.actor_context
                )
                source_lines = (
                    get_uninvoiced_fulfilment_lines(
                        db, invoice.sales_order_id,
                        actor_context=self.actor_context,
                    )
                    if invoice else []
                )
        except AuthorizationError as error:
            QMessageBox.warning(self, "Invoice unavailable", str(error))
            self.reject()
            return
        if not invoice or invoice.status != "Draft":
            QMessageBox.warning(self, "Cannot edit", "Only draft invoices can be edited.")
            self.reject()
            return
        self.invoice = invoice
        self.date.setDate(_as_qdate(invoice.invoice_date))
        self.due_date.setDate(_as_qdate(invoice.due_date))
        self.tax_treatment.setCurrentIndex(
            max(0, self.tax_treatment.findData(invoice.tax_treatment))
        )
        self.notes.setText(invoice.notes or "")
        self.terms.setText(invoice.terms_and_conditions or "")
        self.order.addItem(
            f"{invoice.sales_order.order_code} — {invoice.customer_name_snapshot}",
            invoice.sales_order_id,
        )
        existing_quantities = {
            item.fulfilment_item_id: item.quantity for item in invoice.items
        }
        self.source_lines = source_lines
        self.lines.setRowCount(len(source_lines))
        for row, item in enumerate(source_lines):
            values = (
                item["fulfilment_code"], item["fulfilment_date"], item["part_code"],
                item["product_name"], item["quantity_fulfilled"], item["quantity_invoiced"],
                item["quantity_available"], existing_quantities.get(item["fulfilment_item_id"], 0),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.UserRole, item["fulfilment_item_id"])
                if column != 7:
                    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                self.lines.setItem(row, column, cell)
        self.lines.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        self.warning.setText("Issued invoices are immutable. This form updates draft header and tax settings only.")

    def _save(self):
        try:
            if self.actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                if self.invoice_id:
                    update_draft_invoice(
                        db,
                        self.invoice_id,
                        invoice_date=self.date.date().toPython(),
                        due_date=self.due_date.date().toPython(),
                        tax_treatment=self.tax_treatment.currentData(),
                        notes=self.notes.text(),
                        terms_and_conditions=self.terms.text(),
                        source_quantities={
                            self.lines.item(row, 0).data(Qt.UserRole): self.lines.item(row, 7).text().strip()
                            for row in range(self.lines.rowCount())
                        },
                        actor_context=self.actor_context,
                    )
                else:
                    source_quantities: dict[int, str] = {}
                    for row in range(self.lines.rowCount()):
                        item_id = self.lines.item(row, 0).data(Qt.UserRole)
                        quantity_text = self.lines.item(row, 7).text().strip()
                        source_quantities[item_id] = quantity_text
                    create_invoice_from_fulfilled_order(
                        db,
                        self.order.currentData(),
                        tax_treatment=self.tax_treatment.currentData(),
                        source_quantities=source_quantities,
                        invoice_date=self.date.date().toPython(),
                        due_date=self.due_date.date().toPython(),
                        notes=self.notes.text(),
                        terms_and_conditions=self.terms.text(),
                        actor_context=self.actor_context,
                    )
        except (ValueError, TypeError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Invoice not saved", str(exc))
            return
        self.accept()


class SalesInvoiceDetailDialog(QDialog):
    def __init__(self, parent, invoice_id: int):
        super().__init__(parent)
        self.setWindowTitle("Sales Invoice Details")
        self.resize(850, 620)
        layout = QVBoxLayout(self)
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                invoice = get_sales_invoice_by_id(
                    db, invoice_id, actor_context=actor_context
                )
                if invoice is None:
                    layout.addWidget(QLabel("Invoice not found."))
                    return
                fulfilment_ids = {
                    line.fulfilment_item.fulfilment_id
                    for line in invoice.items
                    if line.fulfilment_item and line.fulfilment_item.fulfilment_id
                }
                movements = []
                movement_access_error = None
                try:
                    for item_id in {
                        line.inventory_item_id for line in invoice.items
                    }:
                        movements.extend(
                            movement
                            for movement in get_inventory_movements(
                                db, item_id, actor_context=actor_context
                            )
                            if movement.reference_type == "SALES_FULFILLMENT"
                            and movement.reference_id in fulfilment_ids
                        )
                except AuthorizationError as error:
                    movement_access_error = str(error)
                movement_ids_by_fulfilment: dict[int, list[str]] = {}
                for movement in movements:
                    movement_ids_by_fulfilment.setdefault(
                        movement.reference_id, []
                    ).append(f"SM-{movement.id:06d}")
        except AuthorizationError as error:
            layout.addWidget(QLabel(f"Invoice details unavailable: {error}"))
            return
        if not invoice:
            layout.addWidget(QLabel("Invoice not found."))
            return
        layout.addWidget(QLabel(
            f"<h2>{invoice.invoice_code}</h2><b>{invoice.status}</b> · "
            f"{invoice.customer_name_snapshot} · Invoice date {invoice.invoice_date} · Due {invoice.due_date or '—'}"
        ))
        layout.addWidget(QLabel(
            "Sales orders and invoices are billing/commitment records, not payment records. "
            "Paid amount and balance are unavailable until Payments is implemented."
        ))
        items = QTableWidget(len(invoice.items), 9)
        items.setHorizontalHeaderLabels([
            "Part ID", "Product", "Fulfilment / date", "Stock movements",
            "Qty", "Unit", "Rate", "Tax", "Line total"
        ])
        items.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        if movement_access_error:
            layout.addWidget(QLabel(
                f"Stock movement references unavailable: {movement_access_error}"
            ))
        for row, line in enumerate(invoice.items):
            fulfilment = line.fulfilment_item.fulfilment if line.fulfilment_item else None
            values = (
                line.part_code_snapshot, line.product_name_snapshot,
                f"{fulfilment.fulfilment_code} / {fulfilment.fulfilment_date}" if fulfilment else line.fulfilment_item_id,
                ", ".join(movement_ids_by_fulfilment.get(fulfilment.id, [])) if fulfilment else "",
                line.quantity, line.unit, format_currency(line.unit_price),
                format_currency(line.tax_amount), format_currency(line.line_total),
            )
            for column, value in enumerate(values):
                items.setItem(row, column, QTableWidgetItem(str(value or "")))
        items.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(items)
        totals = QGroupBox("Invoice totals")
        grid = QGridLayout(totals)
        for row, (label, amount) in enumerate((
            ("Subtotal", invoice.subtotal), ("Discount", invoice.discount_amount),
            ("Additional charges", invoice.additional_charges),
            ("Taxable amount", invoice.taxable_amount), ("CGST", invoice.cgst_amount),
            ("SGST", invoice.sgst_amount), ("IGST", invoice.igst_amount),
            ("Grand total", invoice.grand_total),
        )):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(QLabel(format_currency(amount)), row, 1)
        layout.addWidget(totals)
        history = QTableWidget(len(invoice.history), 5)
        history.setHorizontalHeaderLabels(["Event", "From", "To", "Date", "Reason"])
        history.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        for row, event in enumerate(invoice.history):
            for column, value in enumerate((
                event.event_type, event.from_status or "", event.to_status or "",
                event.created_at, event.reason or "",
            )):
                history.setItem(row, column, QTableWidgetItem(str(value)))
        history.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(QLabel("Status and generation history"))
        layout.addWidget(history)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class SalesInvoicePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("salesInvoicePage")
        self._action_buttons: dict[str, QPushButton] = {}
        self._init_ui()
        self.load_invoices()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.addWidget(QLabel("Sales Invoice & Billing"))
        title_box.addWidget(QLabel("Bill dispatched quantities only; invoices are not payment receipts."))
        header.addLayout(title_box)
        header.addStretch()
        self.btn_settings = QPushButton("Factory / GST settings")
        self.btn_settings.clicked.connect(self._settings)
        header.addWidget(self.btn_settings)
        self.btn_export = QPushButton("Reports PDF / Excel")
        self.btn_export.clicked.connect(self._export)
        header.addWidget(self.btn_export)
        self.btn_create = QPushButton("+ Create Invoice")
        self.btn_create.setObjectName("primaryButton")
        self.btn_create.clicked.connect(self._create)
        header.addWidget(self.btn_create)
        layout.addLayout(header)
        self.access_warning = QLabel()
        self.access_warning.setObjectName("muted")
        self.access_warning.setWordWrap(True)
        self.access_warning.setVisible(False)
        layout.addWidget(self.access_warning)

        self.metric_labels: dict[str, QLabel] = {}
        cards = QGridLayout()
        for index, (key, title) in enumerate((
            ("total_invoices", "Invoices"), ("draft_invoices", "Draft"),
            ("issued_invoices", "Issued"), ("cancelled_invoices", "Cancelled"),
            ("voided_invoices", "Void"), ("invoiced_total", "Issued total (not collected)"),
            ("outstanding_receivables", "Outstanding (posted receipts)"),
        )):
            group = QGroupBox(title)
            value = QLabel("0")
            value.setObjectName("metricBigValue")
            box = QVBoxLayout(group)
            box.addWidget(value)
            cards.addWidget(group, index // 3, index % 3)
            self.metric_labels[key] = value
        layout.addLayout(cards)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search invoice ID or customer")
        self.search.textChanged.connect(self.load_invoices)
        filters.addWidget(self.search, 2)
        self.customer = QComboBox()
        self.customer.addItem("All customers", None)
        self.customer.currentIndexChanged.connect(self.load_invoices)
        filters.addWidget(self.customer)
        self.status = QComboBox()
        self.status.addItem("All statuses", "ALL")
        for status in INVOICE_STATUSES:
            self.status.addItem(status, status)
        self.status.currentIndexChanged.connect(self.load_invoices)
        filters.addWidget(self.status)
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setSpecialValueText("Invoice from")
        self.start_date.setDate(self.start_date.minimumDate())
        self.start_date.dateChanged.connect(self.load_invoices)
        filters.addWidget(self.start_date)
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setSpecialValueText("Invoice to")
        self.end_date.setDate(self.end_date.minimumDate())
        self.end_date.dateChanged.connect(self.load_invoices)
        filters.addWidget(self.end_date)
        self.due_start = QDateEdit()
        self.due_start.setCalendarPopup(True)
        self.due_start.setSpecialValueText("Due from")
        self.due_start.setDate(self.due_start.minimumDate())
        self.due_start.dateChanged.connect(self.load_invoices)
        filters.addWidget(self.due_start)
        self.due_end = QDateEdit()
        self.due_end.setCalendarPopup(True)
        self.due_end.setSpecialValueText("Due to")
        self.due_end.setDate(self.due_end.minimumDate())
        self.due_end.dateChanged.connect(self.load_invoices)
        filters.addWidget(self.due_end)
        layout.addLayout(filters)

        actions = QHBoxLayout()
        self.count_label = QLabel("Showing 0 invoices")
        actions.addWidget(self.count_label)
        actions.addStretch()
        for label, permission, callback in (
            ("Details", "invoices.view", self._details),
            ("Edit Draft", "invoices.manage", self._edit),
            ("Issue", "invoices.issue", self._issue),
            ("Cancel Draft", "invoices.manage", self._cancel),
            ("Void Issued", "invoices.void", self._void),
            ("Invoice PDF", "invoices.view", self._pdf),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
            self._action_buttons[label] = button
        layout.addLayout(actions)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Invoice ID", "Customer", "Invoice Date", "Due Date",
            "Grand Total", "Status", "Payment status",
        ])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.doubleClicked.connect(lambda *_: self._details())
        layout.addWidget(self.table, 1)

    def load_invoices(self, *args):
        if not hasattr(self, "table"):
            return
        actor_context = current_actor_context()
        if actor_context is None:
            self._clear_protected_data("Sign in is required to view invoices.")
            return
        try:
            with get_db() as db:
                for label, permission in (
                    ("Details", "invoices.view"),
                    ("Edit Draft", "invoices.manage"),
                    ("Issue", "invoices.issue"),
                    ("Cancel Draft", "invoices.manage"),
                    ("Void Issued", "invoices.void"),
                    ("Invoice PDF", "invoices.view"),
                ):
                    self._action_buttons[label].setEnabled(
                        actor_has_permission(
                            actor_context, permission, db_session=db
                        )
                    )
                self.btn_create.setEnabled(
                    actor_has_permission(
                        actor_context, "invoices.manage", db_session=db
                    )
                )
                self.btn_export.setEnabled(
                    actor_has_permission(
                        actor_context, "invoices.view", db_session=db
                    )
                )
                self.btn_settings.setEnabled(
                    actor_has_permission(
                        actor_context, "settings.manage", db_session=db
                    )
                    and actor_has_permission(
                        actor_context, "invoices.view", db_session=db
                    )
                )
                invoices = list_sales_invoices(
                    db,
                    search=self.search.text(),
                    customer_id=self.customer.currentData(),
                    status=self.status.currentData(),
                    invoice_start=self.start_date.date().toPython() if self.start_date.date() != self.start_date.minimumDate() else None,
                    invoice_end=self.end_date.date().toPython() if self.end_date.date() != self.end_date.minimumDate() else None,
                    due_start=self.due_start.date().toPython() if self.due_start.date() != self.due_start.minimumDate() else None,
                    due_end=self.due_end.date().toPython() if self.due_end.date() != self.due_end.minimumDate() else None,
                    actor_context=actor_context,
                )
                all_invoices = list_sales_invoices(
                    db, status="ALL", actor_context=actor_context
                )
                payment_access_error = None
                try:
                    metrics = get_sales_invoice_metrics(
                        db, actor_context=actor_context
                    )
                    payment_statuses = [
                        calculate_invoice_balance(
                            db, invoice.id, actor_context=actor_context
                        )["payment_status"]
                        for invoice in invoices
                    ]
                except AuthorizationError as error:
                    payment_access_error = str(error)
                    metrics = self._invoice_metrics_without_receipts(all_invoices)
                    payment_statuses = ["Unavailable"] * len(invoices)
        except AuthorizationError as exc:
            self._clear_protected_data(str(exc))
            return
        selected_customer = self.customer.currentData()
        self.customer.blockSignals(True)
        self.customer.clear()
        self.customer.addItem("All customers", None)
        customer_options = {
            (
                invoice.customer_id,
                invoice.customer_code_snapshot,
                invoice.customer_name_snapshot,
            )
            for invoice in all_invoices
        }
        for customer_id, customer_code, customer_name in sorted(
            customer_options, key=lambda row: (row[2] or "", row[0])
        ):
            self.customer.addItem(
                f"{customer_code or ''} — {customer_name or ''}",
                customer_id,
            )
        if selected_customer is not None:
            index = self.customer.findData(selected_customer)
            if index >= 0:
                self.customer.setCurrentIndex(index)
        self.customer.blockSignals(False)
        for key, label in self.metric_labels.items():
            value = metrics[key]
            if value is None:
                label.setText("Unavailable")
                continue
            label.setText(
                format_currency(value)
                if key in ("invoiced_total", "outstanding_receivables")
                else str(value)
            )
        self.table.setRowCount(len(invoices))
        for row, (invoice, payment_status) in enumerate(
            zip(invoices, payment_statuses)
        ):
            values = (
                invoice.invoice_code, invoice.customer_name_snapshot, invoice.invoice_date,
                invoice.due_date or "—", format_currency(invoice.grand_total), invoice.status,
                payment_status,
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.UserRole, invoice.id)
                self.table.setItem(row, column, cell)
        self.count_label.setText(f"Showing {len(invoices)} invoices")
        self.access_warning.clear()
        self.access_warning.setVisible(False)
        if payment_access_error:
            self.metric_labels["outstanding_receivables"].setText("Unavailable")
            self.access_warning.setText(
                f"Invoice payment data unavailable: {payment_access_error}"
            )
            self.access_warning.setVisible(True)

    @staticmethod
    def _invoice_metrics_without_receipts(invoices):
        counts = {
            status: sum(invoice.status == status for invoice in invoices)
            for status in INVOICE_STATUSES
        }
        return {
            "total_invoices": len(invoices),
            "draft_invoices": counts["Draft"],
            "issued_invoices": counts["Issued"],
            "cancelled_invoices": counts["Cancelled"],
            "voided_invoices": counts["Void"],
            "invoiced_total": sum(
                (
                    invoice.grand_total or 0
                    for invoice in invoices
                    if invoice.status in ("Issued", "Partially Paid", "Paid", "Overdue")
                ),
                0,
            ),
            "outstanding_receivables": None,
        }

    def _clear_protected_data(self, message: str) -> None:
        self.table.setRowCount(0)
        self.customer.blockSignals(True)
        self.customer.clear()
        self.customer.addItem("All customers", None)
        self.customer.blockSignals(False)
        self.count_label.setText("Invoice data unavailable")
        for label in self.metric_labels.values():
            label.clear()
        self.btn_create.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_settings.setEnabled(False)
        for button in self._action_buttons.values():
            button.setEnabled(False)
        self.access_warning.setText(f"Invoice access unavailable: {message}")
        self.access_warning.setVisible(True)

    def _selected_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).data(Qt.UserRole)

    def _create(self):
        dialog = SalesInvoiceForm(self)
        if dialog.exec():
            self.load_invoices()

    def _edit(self):
        invoice_id = self._selected_id()
        if invoice_id and SalesInvoiceForm(self, invoice_id).exec():
            self.load_invoices()

    def _details(self):
        invoice_id = self._selected_id()
        if invoice_id:
            SalesInvoiceDetailDialog(self, invoice_id).exec()

    def _issue(self):
        invoice_id = self._selected_id()
        if not invoice_id:
            return
        if QMessageBox.question(
            self, "Issue sales invoice",
            "Issue this invoice and reserve its dispatched quantities against duplicate billing? "
            "The invoice will become immutable.",
        ) != QMessageBox.Yes:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                issue_sales_invoice(
                    db, invoice_id, actor_context=actor_context
                )
        except (ValueError, TypeError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Invoice not issued", str(exc))
            return
        self.load_invoices()

    def _cancel(self):
        invoice_id = self._selected_id()
        if not invoice_id:
            return
        reason, accepted = QInputDialog.getText(self, "Cancel draft", "Reason:")
        if not accepted:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                cancel_draft_invoice(
                    db, invoice_id, reason, actor_context=actor_context
                )
        except (ValueError, TypeError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Invoice not cancelled", str(exc))
            return
        self.load_invoices()

    def _void(self):
        invoice_id = self._selected_id()
        if not invoice_id:
            return
        reason, accepted = QInputDialog.getText(self, "Void issued invoice", "Reason (required):")
        if not accepted:
            return
        if QMessageBox.question(
            self, "Confirm void", "Void this issued invoice and release its billing allocation?"
        ) != QMessageBox.Yes:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                void_sales_invoice(
                    db, invoice_id, reason, actor_context=actor_context
                )
        except (ValueError, TypeError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Invoice not voided", str(exc))
            return
        self.load_invoices()

    def _pdf(self):
        invoice_id = self._selected_id()
        if not invoice_id:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Export invoice PDF", "", "PDF files (*.pdf)")
        if not filename:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                generate_sales_invoice_pdf(
                    db, invoice_id, filename, actor_context=actor_context
                )
        except (ValueError, OSError, AuthorizationError) as exc:
            QMessageBox.warning(self, "PDF not exported", str(exc))

    def _export(self):
        filename, selected_filter = QFileDialog.getSaveFileName(
            self, "Export invoice register", "", "Excel workbook (*.xlsx);;PDF document (*.pdf)"
        )
        if not filename:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                if filename.lower().endswith(".pdf") or "PDF" in selected_filter:
                    if not filename.lower().endswith(".pdf"):
                        filename += ".pdf"
                    generate_sales_invoice_pdf_report(
                        db, filename, actor_context=actor_context
                    )
                else:
                    if not filename.lower().endswith(".xlsx"):
                        filename += ".xlsx"
                    generate_sales_invoice_excel_report(
                        db, filename, actor_context=actor_context
                    )
        except (ValueError, OSError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Report not exported", str(exc))

    def _settings(self):
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                if not (
                    actor_has_permission(
                        actor_context, "settings.manage", db_session=db
                    )
                    and actor_has_permission(
                        actor_context, "invoices.view", db_session=db
                    )
                ):
                    raise AuthorizationError(
                        "Permission required: settings.manage and invoices.view."
                    )
            if InvoiceFactorySettingsDialog(self).exec():
                self.load_invoices()
        except AuthorizationError as error:
            QMessageBox.warning(self, "Invoice settings unavailable", str(error))
