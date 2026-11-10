from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGridLayout, QGroupBox, QWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QFrame, QMessageBox, QTabWidget
)
from database.connection import get_db
from database.models import Supplier
from services.supplier_service import (
    deactivate_supplier,
    reactivate_supplier,
    get_supplier_by_id,
)
from services.supplier_payment_service import (
    get_supplier_financial_summary,
    get_supplier_payment_summary,
    get_supplier_statement,
    list_supplier_payments,
)
from utils.validators import format_currency
from services.security_service import AuthorizationError, current_actor_context


class SupplierDetailDialog(QDialog):
    """Dialog showing comprehensive supplier details, ledger balances, and purchase history."""
    supplier_modified = Signal(int)

    def __init__(self, parent=None, supplier_id: int = 0):
        super().__init__(parent)
        self.supplier_id = supplier_id
        self.supplier: Supplier | None = None
        self.supplier_purchases = []
        self.total_outstanding_balance = 0.0

        self.resize(800, 700)
        self.setMinimumSize(680, 560)
        self.setModal(True)

        self._load_supplier()
        self._init_ui()
        self._populate_data()

    def _load_supplier(self):
        self.access_error = None
        try:
            self._load_supplier_authorized()
        except AuthorizationError as error:
            self.access_error = str(error)
            self.supplier_data = None
            self.supplier_purchases = []
            self.supplier_payment_data = []
            self.supplier_statement_data = []
            self.financial_summary = {}
            self.total_outstanding_balance = 0.0

    def _load_supplier_authorized(self):
        with get_db() as db:
            actor_context = current_actor_context()
            s = get_supplier_by_id(db, self.supplier_id, actor_context=actor_context)
            if s:
                self.supplier_data = {
                    "id": s.id,
                    "supplier_code": s.supplier_code,
                    "business_name": s.business_name,
                    "contact_person": s.contact_person,
                    "phone": s.phone,
                    "alternate_phone": s.alternate_phone,
                    "email": s.email,
                    "address": s.address,
                    "city": s.city,
                    "state": s.state,
                    "pin_code": s.pin_code,
                    "gstin": s.gstin,
                    "opening_balance": s.opening_balance,
                    "remarks": s.remarks,
                    "is_active": s.is_active,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                }
                self.financial_summary = get_supplier_financial_summary(
                    db, self.supplier_id, actor_context=actor_context
                )
                self.total_outstanding_balance = self.financial_summary["total_outstanding_payables"]
                balance_by_id = {
                    row["purchase"].id: row
                    for row in self.financial_summary["purchase_balances"]
                }
                self.supplier_purchases = [
                    {
                        "id": p.id,
                        "code": p.purchase_code,
                        "date": p.purchase_date,
                        "items_summary": ", ".join(f"{it.quantity}x {it.appliance_category}" for it in p.items),
                        "total_amount": p.total_amount,
                        "amount_paid": balance_by_id[p.id]["historical_amount_paid"],
                        "new_payments": balance_by_id[p.id]["new_supplier_payments"],
                        "balance_amount": balance_by_id[p.id]["balance"],
                        "status": (
                            "Paid" if balance_by_id[p.id]["balance"] == 0
                            else "Partially Paid" if (
                                balance_by_id[p.id]["historical_amount_paid"]
                                + balance_by_id[p.id]["new_supplier_payments"]
                            ) > 0 else "Unpaid"
                        ),
                    }
                    for p in (row["purchase"] for row in self.financial_summary["purchase_balances"])
                ]
                self.supplier_payment_data = [
                    (
                        payment,
                        get_supplier_payment_summary(
                            db, payment, actor_context=actor_context
                        ),
                    )
                    for payment in list_supplier_payments(
                        db, actor_context=actor_context,
                        supplier_id=self.supplier_id, status="ALL"
                    )
                ]
                self.supplier_statement_data = get_supplier_statement(
                    db, self.supplier_id, actor_context=actor_context
                )
            else:
                self.supplier_data = None
                self.supplier_purchases = []
                self.supplier_payment_data = []
                self.supplier_statement_data = []
                self.financial_summary = {}

    def _init_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Header Profile Banner
        self.header_card = QFrame()
        self.header_card.setObjectName("detailHeaderCard")
        h_layout = QVBoxLayout(self.header_card)
        h_layout.setContentsMargins(24, 20, 24, 20)

        top_row = QHBoxLayout()
        self.lbl_name = QLabel()
        self.lbl_name.setObjectName("detailSupplierName")
        self.lbl_code_badge = QLabel()
        self.lbl_code_badge.setObjectName("codeBadge")
        self.lbl_status_badge = QLabel()
        self.lbl_status_badge.setObjectName("statusBadge")

        top_row.addWidget(self.lbl_name)
        top_row.addWidget(self.lbl_code_badge)
        top_row.addWidget(self.lbl_status_badge)
        top_row.addStretch()
        h_layout.addLayout(top_row)

        self.lbl_reg_date = QLabel()
        self.lbl_reg_date.setObjectName("detailRegDate")
        h_layout.addWidget(self.lbl_reg_date)

        root_layout.addWidget(self.header_card)

        # Financial Balance Metric Cards
        metric_bar = QWidget()
        metric_bar.setObjectName("detailMetricBar")
        m_layout = QHBoxLayout(metric_bar)
        m_layout.setContentsMargins(24, 12, 24, 12)
        m_layout.setSpacing(16)

        # Card 1: Opening Balance
        card1 = QFrame()
        card1.setObjectName("balanceCard")
        c1_layout = QVBoxLayout(card1)
        c1_layout.setContentsMargins(16, 12, 16, 12)
        lbl_c1_title = QLabel("Opening Balance")
        lbl_c1_title.setObjectName("metricLabel")
        self.lbl_opening_bal = QLabel("₹ 0.00")
        self.lbl_opening_bal.setObjectName("metricValue")
        lbl_c1_sub = QLabel("Initial recorded balance")
        lbl_c1_sub.setObjectName("metricSubtext")
        c1_layout.addWidget(lbl_c1_title)
        c1_layout.addWidget(self.lbl_opening_bal)
        c1_layout.addWidget(lbl_c1_sub)
        m_layout.addWidget(card1)

        # Card 2: Current Outstanding Balance
        card2 = QFrame()
        card2.setObjectName("balanceCard")
        c2_layout = QVBoxLayout(card2)
        c2_layout.setContentsMargins(16, 12, 16, 12)
        lbl_c2_title = QLabel("Current Outstanding Balance")
        lbl_c2_title.setObjectName("metricLabel")
        self.lbl_outstanding_bal = QLabel("₹ 0.00")
        self.lbl_outstanding_bal.setObjectName("metricValuePrimary")
        lbl_c2_sub = QLabel("Opening and purchase payables less posted allocations")
        lbl_c2_sub.setObjectName("metricSubtext")
        c2_layout.addWidget(lbl_c2_title)
        c2_layout.addWidget(self.lbl_outstanding_bal)
        c2_layout.addWidget(lbl_c2_sub)
        m_layout.addWidget(card2)

        root_layout.addWidget(metric_bar)

        # Tabs: Information & Purchase History
        self.tabs = QTabWidget()
        self.tabs.setObjectName("detailTabs")

        # Tab 1: Profile & Contact
        tab_profile = QWidget()
        p_layout = QVBoxLayout(tab_profile)
        p_layout.setContentsMargins(20, 16, 20, 16)
        p_layout.setSpacing(16)

        group_contact = QGroupBox("Contact & Business Identification")
        g1 = QGridLayout(group_contact)
        g1.setContentsMargins(16, 16, 16, 16)
        g1.setSpacing(12)

        g1.addWidget(QLabel("Contact Person:"), 0, 0)
        self.val_contact = QLabel("—")
        self.val_contact.setObjectName("fieldVal")
        g1.addWidget(self.val_contact, 0, 1)

        g1.addWidget(QLabel("GSTIN:"), 0, 2)
        self.val_gstin = QLabel("—")
        self.val_gstin.setObjectName("fieldVal")
        g1.addWidget(self.val_gstin, 0, 3)

        g1.addWidget(QLabel("Primary Phone:"), 1, 0)
        self.val_phone = QLabel("—")
        self.val_phone.setObjectName("fieldVal")
        g1.addWidget(self.val_phone, 1, 1)

        g1.addWidget(QLabel("Alternate Phone:"), 1, 2)
        self.val_alt_phone = QLabel("—")
        self.val_alt_phone.setObjectName("fieldVal")
        g1.addWidget(self.val_alt_phone, 1, 3)

        g1.addWidget(QLabel("Email Address:"), 2, 0)
        self.val_email = QLabel("—")
        self.val_email.setObjectName("fieldVal")
        g1.addWidget(self.val_email, 2, 1, 1, 3)

        p_layout.addWidget(group_contact)

        group_address = QGroupBox("Location & Physical Address")
        g2 = QGridLayout(group_address)
        g2.setContentsMargins(16, 16, 16, 16)
        g2.setSpacing(12)

        g2.addWidget(QLabel("Street Address:"), 0, 0)
        self.val_address = QLabel("—")
        self.val_address.setObjectName("fieldVal")
        self.val_address.setWordWrap(True)
        g2.addWidget(self.val_address, 0, 1, 1, 3)

        g2.addWidget(QLabel("City:"), 1, 0)
        self.val_city = QLabel("—")
        self.val_city.setObjectName("fieldVal")
        g2.addWidget(self.val_city, 1, 1)

        g2.addWidget(QLabel("State:"), 1, 2)
        self.val_state = QLabel("—")
        self.val_state.setObjectName("fieldVal")
        g2.addWidget(self.val_state, 1, 3)

        g2.addWidget(QLabel("PIN Code:"), 2, 0)
        self.val_pin = QLabel("—")
        self.val_pin.setObjectName("fieldVal")
        g2.addWidget(self.val_pin, 2, 1)

        g2.addWidget(QLabel("Last Updated:"), 2, 2)
        self.val_updated = QLabel("—")
        self.val_updated.setObjectName("fieldVal")
        g2.addWidget(self.val_updated, 2, 3)

        p_layout.addWidget(group_address)

        group_remarks = QGroupBox("Remarks / Notes")
        g3 = QVBoxLayout(group_remarks)
        g3.setContentsMargins(16, 12, 16, 16)
        self.val_remarks = QLabel("No remarks recorded.")
        self.val_remarks.setObjectName("fieldVal")
        self.val_remarks.setWordWrap(True)
        g3.addWidget(self.val_remarks)
        p_layout.addWidget(group_remarks)

        p_layout.addStretch()
        self.tabs.addTab(tab_profile, "Supplier Details")

        # Tab 2: Purchase History
        tab_history = QWidget()
        h_box = QVBoxLayout(tab_history)
        h_box.setContentsMargins(20, 16, 20, 16)
        h_box.setSpacing(12)

        info_box = QFrame()
        info_box.setObjectName("noticeBox")
        info_l = QHBoxLayout(info_box)
        info_l.setContentsMargins(16, 12, 16, 12)
        note_icon = QLabel("ℹ")
        note_icon.setStyleSheet("font-size: 20px; color: #2563eb;")
        self.lbl_purchase_notice = QLabel(
            "Purchase History is linked directly to the Purchase Management module. "
            "Inward scrap batches, freight expenses, and outstanding balances are tracked here."
        )
        self.lbl_purchase_notice.setWordWrap(True)
        self.lbl_purchase_notice.setObjectName("noticeText")
        info_l.addWidget(note_icon)
        info_l.addWidget(self.lbl_purchase_notice, 1)
        h_box.addWidget(info_box)

        # Actual table structure for purchases
        self.tbl_purchases = QTableWidget(0, 8)
        self.tbl_purchases.setHorizontalHeaderLabels([
            "Purchase ID", "Date", "Appliance Summary", "Total (₹)",
            "Historical paid (₹)", "New payments (₹)", "Outstanding (₹)", "Status",
        ])
        h_header = self.tbl_purchases.horizontalHeader()
        h_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h_header.setSectionResizeMode(2, QHeaderView.Stretch)
        h_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        for column in range(4, 8):
            h_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.tbl_purchases.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_purchases.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_purchases.doubleClicked.connect(self._on_purchase_table_double_click)
        self.tbl_purchases.setObjectName("styledTable")
        h_box.addWidget(self.tbl_purchases)

        self.tabs.addTab(tab_history, "Purchase History (0)")

        tab_payments = QWidget()
        payment_layout = QVBoxLayout(tab_payments)
        payment_layout.addWidget(QLabel(
            "Historical amounts recorded on purchases remain separate from new posted supplier payments. "
            "Advances are not automatically allocated."
        ))
        self.lbl_supplier_financial_summary = QLabel()
        self.lbl_supplier_financial_summary.setWordWrap(True)
        payment_layout.addWidget(self.lbl_supplier_financial_summary)
        payment_table = QTableWidget(len(self.supplier_payment_data), 7)
        payment_table.setHorizontalHeaderLabels([
            "Payment ID", "Date", "Amount", "Allocated", "Advance", "Method", "Status",
        ])
        payment_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for row, (payment, summary) in enumerate(self.supplier_payment_data):
            cells = (
                payment.payment_code, payment.payment_date,
                format_currency(payment.amount_paid),
                format_currency(summary["allocated_amount"]),
                format_currency(summary["unallocated_amount"]),
                payment.payment_method, payment.status,
            )
            for col, value in enumerate(cells):
                payment_table.setItem(row, col, QTableWidgetItem(str(value)))
        payment_table.setEditTriggers(QTableWidget.NoEditTriggers)
        payment_layout.addWidget(payment_table)
        payment_layout.addWidget(QLabel("Supplier statement"))
        statement_table = QTableWidget(len(self.supplier_statement_data), 6)
        statement_table.setHorizontalHeaderLabels([
            "Date", "Reference", "Description", "Debit", "Credit", "Status",
        ])
        statement_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for row, values in enumerate(self.supplier_statement_data):
            cells = (
                values["date"] or "—", values["reference"], values["description"],
                format_currency(values["debit"]), format_currency(values["credit"]),
                values["status"],
            )
            for col, value in enumerate(cells):
                statement_table.setItem(row, col, QTableWidgetItem(str(value)))
        statement_table.setEditTriggers(QTableWidget.NoEditTriggers)
        payment_layout.addWidget(statement_table)
        self.tabs.addTab(tab_payments, "Payments & Statement")

        root_layout.addWidget(self.tabs)

        # Footer Actions
        footer = QFrame()
        footer.setObjectName("dialogFooter")
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(24, 16, 24, 16)
        f_layout.setSpacing(12)

        self.btn_toggle_active = QPushButton()
        self.btn_toggle_active.clicked.connect(self._toggle_active_status)

        self.btn_edit = QPushButton("Edit Supplier")
        self.btn_edit.setObjectName("primaryButton")
        self.btn_edit.clicked.connect(self._edit_supplier)

        self.btn_close = QPushButton("Close")
        self.btn_close.setObjectName("secondaryButton")
        self.btn_close.clicked.connect(self.accept)

        f_layout.addWidget(self.btn_toggle_active)
        f_layout.addStretch()
        f_layout.addWidget(self.btn_edit)
        f_layout.addWidget(self.btn_close)
        root_layout.addWidget(footer)

    def _populate_data(self):
        if not self.supplier_data:
            message = self.access_error or "Supplier record was not found."
            self.lbl_supplier_financial_summary.setText(
                f"Supplier and payment data unavailable: {message}"
            )
            self.btn_edit.setEnabled(False)
            self.btn_toggle_active.setEnabled(False)
            return

        data = self.supplier_data
        self.setWindowTitle(f"Supplier Profile — {data['business_name']} ({data['supplier_code']})")
        self.lbl_name.setText(data["business_name"])
        self.lbl_code_badge.setText(f" {data['supplier_code']} ")

        is_act = data["is_active"]
        if is_act:
            self.lbl_status_badge.setText(" ACTIVE ")
            self.lbl_status_badge.setStyleSheet(
                "background: #dcfce7; color: #15803d; border: 1px solid #86efac; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )
            self.btn_toggle_active.setText("Deactivate Supplier")
            self.btn_toggle_active.setObjectName("dangerButton")
        else:
            self.lbl_status_badge.setText(" INACTIVE ")
            self.lbl_status_badge.setStyleSheet(
                "background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )
            self.btn_toggle_active.setText("Reactivate Supplier")
            self.btn_toggle_active.setObjectName("successButton")

        # Force re-styling of the button
        self.btn_toggle_active.style().unpolish(self.btn_toggle_active)
        self.btn_toggle_active.style().polish(self.btn_toggle_active)

        created_str = data["created_at"].strftime("%d %b %Y, %I:%M %p") if data["created_at"] else "N/A"
        updated_str = data["updated_at"].strftime("%d %b %Y, %I:%M %p") if data["updated_at"] else "N/A"
        self.lbl_reg_date.setText(f"Registered on: {created_str}")

        op_bal = data["opening_balance"] or 0.0
        self.lbl_opening_bal.setText(format_currency(op_bal))
        self.lbl_outstanding_bal.setText(format_currency(self.total_outstanding_balance))
        self.lbl_supplier_financial_summary.setText(
            f"Original opening payable: {format_currency(self.financial_summary['original_opening_payable'])}  |  "
            f"Remaining opening payable: {format_currency(self.financial_summary['remaining_opening_payable'])}  |  "
            f"Purchase payables: {format_currency(self.financial_summary['purchase_payables'])}  |  "
            f"Historical purchase payments: {format_currency(self.financial_summary['historical_purchase_payments'])}  |  "
            f"New posted supplier payments: {format_currency(self.financial_summary['new_supplier_payments'])}  |  "
            f"Unallocated advances: {format_currency(self.financial_summary['unallocated_advances'])}  |  "
            f"Outstanding payables: {format_currency(self.financial_summary['total_outstanding_payables'])}"
        )

        self.val_contact.setText(data["contact_person"] or "—")
        self.val_gstin.setText(data["gstin"] or "—")
        self.val_phone.setText(data["phone"] or "—")
        self.val_alt_phone.setText(data["alternate_phone"] or "—")
        self.val_email.setText(data["email"] or "—")
        self.val_address.setText(data["address"] or "—")
        self.val_city.setText(data["city"] or "—")
        self.val_state.setText(data["state"] or "—")
        self.val_pin.setText(data["pin_code"] or "—")
        self.val_updated.setText(updated_str)
        self.val_remarks.setText(data["remarks"] or "No remarks provided.")

        # Populate Purchases Table
        p_count = len(self.supplier_purchases)
        self.tabs.setTabText(1, f"Purchase History ({p_count})")
        self.tbl_purchases.setRowCount(p_count)

        if p_count > 0:
            self.lbl_purchase_notice.setText(
                f"Showing {p_count} inward purchase order(s) recorded from this vendor. "
                "Double-click any entry to view full receipt and voucher details."
            )
        else:
            self.lbl_purchase_notice.setText(
                "No purchase orders recorded yet for this supplier. "
                "New purchases entered via the Purchases module will automatically link here."
            )

        for r, p in enumerate(self.supplier_purchases):
            p_code_item = QTableWidgetItem(p["code"])
            p_code_item.setData(Qt.UserRole, p["id"])
            p_code_item.setTextAlignment(Qt.AlignCenter)
            self.tbl_purchases.setItem(r, 0, p_code_item)

            d_str = p["date"].strftime("%d-%b-%Y") if p["date"] else "—"
            d_item = QTableWidgetItem(d_str)
            d_item.setTextAlignment(Qt.AlignCenter)
            self.tbl_purchases.setItem(r, 1, d_item)

            self.tbl_purchases.setItem(r, 2, QTableWidgetItem(p["items_summary"] or "—"))

            for col, value in (
                (3, p["total_amount"]),
                (4, p["amount_paid"]),
                (5, p["new_payments"]),
                (6, p["balance_amount"]),
            ):
                amount_item = QTableWidgetItem(format_currency(value))
                amount_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.tbl_purchases.setItem(r, col, amount_item)

            status_item = QTableWidgetItem(p["status"])
            status_item.setTextAlignment(Qt.AlignCenter)
            self.tbl_purchases.setItem(r, 7, status_item)

    def _on_purchase_table_double_click(self, index):
        row = index.row()
        item = self.tbl_purchases.item(row, 0)
        if item:
            pid = item.data(Qt.UserRole)
            from ui.purchase_detail_dialog import PurchaseDetailDialog
            dialog = PurchaseDetailDialog(self, purchase_id=pid)
            dialog.exec()


    def _toggle_active_status(self):
        if not self.supplier_data:
            return

        is_active = self.supplier_data["is_active"]
        name = self.supplier_data["business_name"]
        code = self.supplier_data["supplier_code"]

        if is_active:
            confirm = QMessageBox.question(
                self,
                "Confirm Deactivation",
                f"Are you sure you want to deactivate supplier:\n\n{name} ({code})?\n\n"
                "Inactive suppliers will be preserved in historical records but hidden from active procurement workflows.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if confirm == QMessageBox.Yes:
                with get_db() as db:
                    deactivate_supplier(
                        db,
                        self.supplier_id,
                        actor_context=current_actor_context(),
                    )
                self.supplier_modified.emit(self.supplier_id)
                self._load_supplier()
                self._populate_data()
        else:
            confirm = QMessageBox.question(
                self,
                "Confirm Reactivation",
                f"Do you want to reactivate supplier:\n\n{name} ({code})?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if confirm == QMessageBox.Yes:
                with get_db() as db:
                    reactivate_supplier(db, self.supplier_id, actor_context=current_actor_context())
                self.supplier_modified.emit(self.supplier_id)
                self._load_supplier()
                self._populate_data()

    def _edit_supplier(self):
        from ui.supplier_form_dialog import SupplierFormDialog
        try:
            with get_db() as db:
                s = get_supplier_by_id(
                    db, self.supplier_id, actor_context=current_actor_context()
                )
                if not s:
                    return
                dialog = SupplierFormDialog(self, supplier=s)
                dialog.supplier_saved.connect(self._on_saved_after_edit)
                dialog.exec()
        except AuthorizationError:
            QMessageBox.warning(
                self,
                "Authentication Required",
                "Sign in with supplier-view permission to edit this supplier.",
            )

    def _on_saved_after_edit(self, sup_id: int):
        self.supplier_modified.emit(sup_id)
        self._load_supplier()
        self._populate_data()
