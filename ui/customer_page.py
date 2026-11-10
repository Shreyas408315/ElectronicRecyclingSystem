from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
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
)

from database.connection import get_db
from services.security_service import AuthorizationError, current_actor_context, require_permission
from services.customer_report_service import (
    generate_customer_excel_report,
    generate_customer_pdf_report,
)
from services.customer_service import (
    CUSTOMER_TYPES,
    deactivate_customer,
    get_customer_metrics,
    get_customer_by_id,
    list_customers,
    reactivate_customer,
)
from ui.customer_detail_dialog import CustomerDetailDialog
from ui.customer_form_dialog import CustomerFormDialog
from utils.validators import format_currency


class CustomerPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_customer_id: int | None = None
        self._init_ui()
        self.load_customers()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title_layout = QVBoxLayout()
        title = QLabel("Customer & Distributor Management")
        title.setObjectName("supplierPageTitle")
        subtitle = QLabel("Maintain customer profiles, account terms, and opening balances.")
        subtitle.setObjectName("supplierPageSubtitle")
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        header.addLayout(title_layout)
        header.addStretch()
        self.btn_export = QPushButton("Export PDF / Excel")
        self.btn_export.setObjectName("secondaryButton")
        self.btn_export.clicked.connect(self._export_report)
        header.addWidget(self.btn_export)
        self.btn_add = QPushButton("+ Add Customer")
        self.btn_add.setObjectName("primaryButton")
        self.btn_add.clicked.connect(self._add_customer)
        header.addWidget(self.btn_add)
        layout.addLayout(header)

        metrics_frame = QFrame()
        metrics_frame.setObjectName("metricsContainer")
        metrics_layout = QGridLayout(metrics_frame)
        metrics_layout.setContentsMargins(12, 10, 12, 10)
        metrics_layout.setSpacing(8)
        specs = [
            ("total", "Total Customers"),
            ("active", "Active Customers"),
            ("inactive", "Inactive Customers"),
            ("total_opening_balance", "Opening Balances"),
            ("total_outstanding_receivables", "Sales Receivables"),
            ("credit_limit_exceeded_count", "Over Credit Limit"),
            ("overdue_customer_count", "Overdue Customers"),
        ]
        self.metric_labels: dict[str, QLabel] = {}
        for index, (key, label) in enumerate(specs):
            card = QFrame()
            card.setObjectName("metricCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 8, 12, 8)
            title_label = QLabel(label)
            title_label.setObjectName("metricTitle")
            value_label = QLabel("0")
            value_label.setObjectName("metricBigValue")
            card_layout.addWidget(title_label)
            card_layout.addWidget(value_label)
            metrics_layout.addWidget(card, index // 4, index % 4)
            self.metric_labels[key] = value_label
        layout.addWidget(metrics_frame)
        note = QLabel(
            "Sales-related receivables, overdue balances, and credit-limit breaches are unavailable until "
            "Sales/Invoice and Payments records exist. Opening balances are shown separately."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.lbl_access_error = QLabel()
        self.lbl_access_error.setObjectName("errorLabel")
        self.lbl_access_error.setWordWrap(True)
        layout.addWidget(self.lbl_access_error)

        filters = QFrame()
        filters.setObjectName("filterBar")
        filter_layout = QHBoxLayout(filters)
        filter_layout.setContentsMargins(12, 8, 12, 8)
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search customer ID, name, phone, email, GSTIN or PAN")
        self.txt_search.setClearButtonEnabled(True)
        self.txt_search.textChanged.connect(self.load_customers)
        filter_layout.addWidget(self.txt_search, 3)
        self.combo_type = QComboBox()
        self.combo_type.addItem("All customer types", "ALL")
        self.combo_type.addItems(CUSTOMER_TYPES)
        self.combo_type.currentIndexChanged.connect(self.load_customers)
        filter_layout.addWidget(self.combo_type)
        self.combo_status = QComboBox()
        self.combo_status.addItem("All statuses", "ALL")
        self.combo_status.addItem("Active only", "ACTIVE")
        self.combo_status.addItem("Inactive only", "INACTIVE")
        self.combo_status.currentIndexChanged.connect(self.load_customers)
        filter_layout.addWidget(self.combo_status)
        self.combo_sort = QComboBox()
        self.combo_sort.addItem("Name (A–Z)", "name_asc")
        self.combo_sort.addItem("Name (Z–A)", "name_desc")
        self.combo_sort.addItem("Newest", "date_desc")
        self.combo_sort.addItem("Oldest", "date_asc")
        self.combo_sort.addItem("Customer ID", "code_asc")
        self.combo_sort.currentIndexChanged.connect(self.load_customers)
        filter_layout.addWidget(self.combo_sort)
        layout.addWidget(filters)

        toolbar = QHBoxLayout()
        self.lbl_count = QLabel("Showing 0 customers")
        self.lbl_count.setObjectName("tableCountLabel")
        toolbar.addWidget(self.lbl_count)
        toolbar.addStretch()
        for name, label in (
            ("details", "View Details"),
            ("edit", "Edit"),
            ("toggle", "Deactivate / Reactivate"),
        ):
            button = QPushButton(label)
            button.setObjectName("secondaryButton")
            button.setEnabled(False)
            button.clicked.connect(
                self._view_selected_customer if name == "details" else
                self._edit_selected_customer if name == "edit" else
                self._toggle_selected_customer
            )
            setattr(self, f"btn_{name}", button)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels([
            "Customer ID", "Customer Name", "Type", "Contact Person", "Phone",
            "City", "GSTIN", "Opening Balance", "Sales Receivable", "Status",
        ])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (2, 3, 4, 5, 6, 7, 8, 9):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setObjectName("supplierTable")
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _: self._view_selected_customer())
        layout.addWidget(self.table, 1)

    def load_customers(self, *_):
        actor_context = current_actor_context()
        if actor_context is None:
            self._clear_customer_data("Sign in is required.")
            return
        try:
            with get_db() as db:
                metrics = get_customer_metrics(db, actor_context=actor_context)
                customers = list_customers(
                    db,
                    search=self.txt_search.text(),
                    customer_type=self.combo_type.currentData() or "ALL",
                    status_filter=self.combo_status.currentData() or "ALL",
                    sort_by=self.combo_sort.currentData() or "name_asc",
                    actor_context=actor_context,
                )
                rows = [{
                    "id": customer.id,
                    "values": [
                        customer.customer_code,
                        customer.customer_name,
                        customer.customer_type,
                        customer.contact_person or "—",
                        customer.phone or "—",
                        customer.city or "—",
                        customer.gstin or "—",
                        format_currency(customer.opening_balance),
                        "Not available",
                        "Active" if customer.is_active else "Inactive",
                    ],
                    "active": customer.is_active,
                } for customer in customers]
        except AuthorizationError as error:
            self._clear_customer_data(str(error))
            return
        self.lbl_access_error.clear()
        self.btn_export.setEnabled(True)
        try:
            with get_db() as db:
                require_permission(actor_context, "customers.manage", db_session=db)
            can_manage = True
        except AuthorizationError:
            can_manage = False
        self.btn_add.setEnabled(can_manage)
        if not can_manage:
            self.lbl_access_error.setText(
                "Customer records are read-only for your role."
            )
        for key, label in self.metric_labels.items():
            value = metrics[key]
            if value is None:
                label.setText("Not available")
            elif key == "total_opening_balance":
                label.setText(format_currency(value))
            else:
                label.setText(str(value))
        self.lbl_count.setText(f"Showing {len(rows)} customers")
        self.table.setRowCount(len(rows))
        for row, data in enumerate(rows):
            customer_id = QTableWidgetItem(data["values"][0])
            customer_id.setData(Qt.UserRole, data["id"])
            self.table.setItem(row, 0, customer_id)
            for col, value in enumerate(data["values"][1:], start=1):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self._selection_changed()

    def _clear_customer_data(self, message: str):
        self.table.setRowCount(0)
        self.selected_customer_id = None
        self.lbl_count.setText("Customer data unavailable")
        for label in self.metric_labels.values():
            label.setText("—")
        self.btn_export.setEnabled(False)
        self.btn_add.setEnabled(False)
        self.lbl_access_error.setText(f"Customer access unavailable: {message}")
        self._selection_changed()

    def _selection_changed(self):
        selected = self.table.selectionModel().selectedRows()
        self.selected_customer_id = None
        customer_active = False
        if selected:
            row = selected[0].row()
            self.selected_customer_id = self.table.item(row, 0).data(Qt.UserRole)
            customer_active = self.table.item(row, 9).text() == "Active"
        actor_context = current_actor_context()
        can_manage = False
        if self.selected_customer_id is not None and actor_context is not None:
            try:
                with get_db() as db:
                    require_permission(actor_context, "customers.manage", db_session=db)
                can_manage = True
            except AuthorizationError:
                pass
        self.btn_details.setEnabled(self.selected_customer_id is not None)
        self.btn_edit.setEnabled(self.selected_customer_id is not None and can_manage)
        self.btn_toggle.setEnabled(self.selected_customer_id is not None and can_manage)
        self.btn_toggle.setText("Deactivate" if customer_active else "Reactivate")

    def _add_customer(self):
        if not self.btn_add.isEnabled():
            return
        dialog = CustomerFormDialog(self)
        dialog.customer_saved.connect(lambda _: self.load_customers())
        dialog.exec()

    def _edit_selected_customer(self):
        if not self.selected_customer_id or not self.btn_edit.isEnabled():
            return
        actor_context = current_actor_context()
        if actor_context is None:
            self._clear_customer_data("Sign in is required.")
            return
        try:
            with get_db() as db:
                customer = get_customer_by_id(
                    db, self.selected_customer_id, actor_context=actor_context
                )
                if not customer:
                    return
        except AuthorizationError as error:
            self._clear_customer_data(str(error))
            return
        dialog = CustomerFormDialog(self, customer=customer)
        dialog.customer_saved.connect(lambda _: self.load_customers())
        dialog.exec()

    def _view_selected_customer(self):
        if not self.selected_customer_id:
            return
        dialog = CustomerDetailDialog(self, self.selected_customer_id)
        dialog.customer_modified.connect(lambda _: self.load_customers())
        dialog.exec()

    def _toggle_selected_customer(self):
        if not self.selected_customer_id or not self.btn_toggle.isEnabled():
            return
        actor_context = current_actor_context()
        if actor_context is None:
            self._clear_customer_data("Sign in is required.")
            return
        try:
            with get_db() as db:
                customer = get_customer_by_id(
                    db, self.selected_customer_id, actor_context=actor_context
                )
                if not customer:
                    return
                is_active = customer.is_active
                display_name = customer.customer_name
        except AuthorizationError as error:
            self._clear_customer_data(str(error))
            return
        action = "deactivate" if is_active else "reactivate"
        answer = QMessageBox.question(
            self,
            f"{action.title()} Customer",
            f"Are you sure you want to {action} {display_name}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                (deactivate_customer if is_active else reactivate_customer)(
                    db, self.selected_customer_id, actor_context=actor_context
                )
        except AuthorizationError as error:
            self._clear_customer_data(str(error))
            return
        self.load_customers()

    def _export_report(self):
        choice = QMessageBox(self)
        choice.setWindowTitle("Export Customer Report")
        choice.setText("Choose a customer report format.")
        pdf_button = choice.addButton("PDF", QMessageBox.ActionRole)
        excel_button = choice.addButton("Excel (.xlsx)", QMessageBox.ActionRole)
        choice.addButton(QMessageBox.Cancel)
        choice.exec()
        if choice.clickedButton() not in (pdf_button, excel_button):
            return
        is_pdf = choice.clickedButton() == pdf_button
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Customer Report",
            "Customer_Directory.pdf" if is_pdf else "Customer_Directory.xlsx",
            "PDF Files (*.pdf)" if is_pdf else "Excel Files (*.xlsx)",
        )
        if not path:
            return
        try:
            actor_context = current_actor_context()
            if actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                saved = (
                    generate_customer_pdf_report(db, path, actor_context=actor_context)
                    if is_pdf else
                    generate_customer_excel_report(db, path, actor_context=actor_context)
                )
            QMessageBox.information(self, "Report Saved", f"Customer report saved:\n\n{saved}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(saved))
        except Exception as error:
            QMessageBox.critical(self, "Export Failed", str(error))
