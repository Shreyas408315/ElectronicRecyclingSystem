import os
from datetime import date
from PySide6.QtCore import Qt, QDate, QPoint, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QDateEdit, QCheckBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QFrame, QMessageBox, QMenu,
    QFileDialog
)
from database.connection import get_db
from services.purchase_service import (
    APPLIANCE_CATEGORIES,
    list_purchases,
    get_purchase_metrics,
    get_purchase_by_id,
)
from services.security_service import AuthorizationError, current_actor_context
from services.pdf_receipt_service import generate_purchase_receipt_pdf
from ui.purchase_form_dialog import PurchaseFormDialog
from ui.purchase_detail_dialog import PurchaseDetailDialog
from utils.validators import format_currency


class PurchasePage(QWidget):
    """Main Purchase Management module dashboard and inward appliance register."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_purchase_id: int | None = None
        self._init_ui()
        self.load_purchases()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 20, 24, 20)
        main_layout.setSpacing(16)

        # 1. Page Header (Title + Actions)
        header_bar = QHBoxLayout()
        header_text_layout = QVBoxLayout()
        page_title = QLabel("Purchase Management")
        page_title.setObjectName("supplierPageTitle")
        page_subtitle = QLabel("Track raw inward appliance batches, freight & handling charges, and supplier payables.")
        page_subtitle.setObjectName("supplierPageSubtitle")
        header_text_layout.addWidget(page_title)
        header_text_layout.addWidget(page_subtitle)
        header_bar.addLayout(header_text_layout)

        header_bar.addStretch()

        self.btn_refresh = QPushButton("↻ Refresh")
        self.btn_refresh.setObjectName("secondaryButton")
        self.btn_refresh.clicked.connect(self.load_purchases)
        header_bar.addWidget(self.btn_refresh)

        self.btn_new_purchase = QPushButton("+ New Purchase")
        self.btn_new_purchase.setObjectName("primaryButton")
        self.btn_new_purchase.clicked.connect(self._open_new_purchase_dialog)
        header_bar.addWidget(self.btn_new_purchase)

        main_layout.addLayout(header_bar)

        # 2. Metric Cards Bar (6 KPIs)
        metrics_frame = QFrame()
        metrics_frame.setObjectName("metricsContainer")
        metrics_layout = QHBoxLayout(metrics_frame)
        metrics_layout.setContentsMargins(14, 12, 14, 12)
        metrics_layout.setSpacing(12)

        self.card_total = self._create_metric_card("Total Purchases", "0", "#0f172a")
        self.card_today = self._create_metric_card("Purchases Today", "0", "#2563eb")
        self.card_month = self._create_metric_card("This Month", "0", "#0284c7")
        self.card_cost = self._create_metric_card("Total Expenditure", "₹ 0.00", "#7c3aed")
        self.card_paid = self._create_metric_card("Total Paid", "₹ 0.00", "#15803d")
        self.card_outstanding = self._create_metric_card("Total Outstanding", "₹ 0.00", "#b91c1c")

        metrics_layout.addWidget(self.card_total["frame"])
        metrics_layout.addWidget(self.card_today["frame"])
        metrics_layout.addWidget(self.card_month["frame"])
        metrics_layout.addWidget(self.card_cost["frame"])
        metrics_layout.addWidget(self.card_paid["frame"])
        metrics_layout.addWidget(self.card_outstanding["frame"])

        main_layout.addWidget(metrics_frame)

        # 3. Search & Filter Bar
        filter_card = QFrame()
        filter_card.setObjectName("filterBar")
        filter_layout = QVBoxLayout(filter_card)
        filter_layout.setContentsMargins(16, 12, 16, 12)
        filter_layout.setSpacing(10)

        # Top row: Search, Category, Status, Sort
        top_filter_row = QHBoxLayout()
        top_filter_row.setSpacing(10)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("🔍  Search by Purchase ID, Supplier Name, Supplier Code...")
        self.txt_search.setClearButtonEnabled(True)
        self.txt_search.textChanged.connect(self._on_filter_changed)
        top_filter_row.addWidget(self.txt_search, 2)

        # Appliance category filter
        lbl_cat = QLabel("Category:")
        lbl_cat.setObjectName("filterLabel")
        self.combo_category = QComboBox()
        self.combo_category.addItem("All Categories", "ALL")
        for cat in APPLIANCE_CATEGORIES:
            self.combo_category.addItem(cat, cat)
        self.combo_category.currentIndexChanged.connect(self._on_filter_changed)
        top_filter_row.addWidget(lbl_cat)
        top_filter_row.addWidget(self.combo_category, 1)

        # Payment status filter
        lbl_status = QLabel("Payment:")
        lbl_status.setObjectName("filterLabel")
        self.combo_status = QComboBox()
        self.combo_status.addItem("All Statuses", "ALL")
        self.combo_status.addItem("Unpaid", "UNPAID")
        self.combo_status.addItem("Partially Paid", "PARTIALLY PAID")
        self.combo_status.addItem("Paid", "PAID")
        self.combo_status.currentIndexChanged.connect(self._on_filter_changed)
        top_filter_row.addWidget(lbl_status)
        top_filter_row.addWidget(self.combo_status, 1)

        # Sort combo
        lbl_sort = QLabel("Sort By:")
        lbl_sort.setObjectName("filterLabel")
        self.combo_sort = QComboBox()
        self.combo_sort.addItem("Purchase Date (Newest)", "date_desc")
        self.combo_sort.addItem("Purchase Date (Oldest)", "date_asc")
        self.combo_sort.addItem("Total Amount (Highest)", "amount_desc")
        self.combo_sort.addItem("Total Amount (Lowest)", "amount_asc")
        self.combo_sort.addItem("Purchase ID (Ascending)", "code_asc")
        self.combo_sort.currentIndexChanged.connect(self._on_filter_changed)
        top_filter_row.addWidget(lbl_sort)
        top_filter_row.addWidget(self.combo_sort, 1)

        filter_layout.addLayout(top_filter_row)

        # Bottom row: Date Range
        date_row = QHBoxLayout()
        date_row.setSpacing(10)

        self.chk_date_filter = QCheckBox("Filter by Date Range:")
        self.chk_date_filter.setObjectName("filterLabel")
        self.chk_date_filter.toggled.connect(self._toggle_date_filter)
        date_row.addWidget(self.chk_date_filter)

        lbl_from = QLabel("From:")
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDate(QDate.currentDate().addDays(-30))
        self.date_from.setDisplayFormat("dd-MM-yyyy")
        self.date_from.setEnabled(False)
        self.date_from.dateChanged.connect(self._on_filter_changed)
        date_row.addWidget(lbl_from)
        date_row.addWidget(self.date_from)

        lbl_to = QLabel("To:")
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDate(QDate.currentDate())
        self.date_to.setDisplayFormat("dd-MM-yyyy")
        self.date_to.setEnabled(False)
        self.date_to.dateChanged.connect(self._on_filter_changed)
        date_row.addWidget(lbl_to)
        date_row.addWidget(self.date_to)

        date_row.addStretch()
        filter_layout.addLayout(date_row)

        main_layout.addWidget(filter_card)

        # 4. Context Toolbar for selected row
        toolbar = QHBoxLayout()
        self.lbl_table_count = QLabel("Showing 0 purchases")
        self.lbl_table_count.setObjectName("tableCountLabel")
        toolbar.addWidget(self.lbl_table_count)
        toolbar.addStretch()

        self.btn_view_details = QPushButton("View Details")
        self.btn_view_details.setObjectName("secondaryButton")
        self.btn_view_details.setEnabled(False)
        self.btn_view_details.clicked.connect(self._view_selected_purchase)
        toolbar.addWidget(self.btn_view_details)

        self.btn_print_receipt = QPushButton("📄 Print Receipt (PDF)")
        self.btn_print_receipt.setObjectName("secondaryButton")
        self.btn_print_receipt.setEnabled(False)
        self.btn_print_receipt.clicked.connect(self._print_selected_receipt)
        toolbar.addWidget(self.btn_print_receipt)

        main_layout.addLayout(toolbar)

        # 5. Purchases Data Table
        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "Purchase ID",
            "Date",
            "Supplier",
            "Appliance Items",
            "Total Qty",
            "Total Cost",
            "Amount Paid",
            "Remaining Balance",
            "Payment Status",
        ])

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeToContents)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setObjectName("supplierTable")

        # Signals
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self._on_table_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        main_layout.addWidget(self.table)

    def _create_metric_card(self, title: str, initial_value: str, value_color: str) -> dict:
        card = QFrame()
        card.setObjectName("metricCard")
        l = QVBoxLayout(card)
        l.setContentsMargins(14, 10, 14, 10)
        l.setSpacing(4)

        t_lbl = QLabel(title)
        t_lbl.setObjectName("metricTitle")

        v_lbl = QLabel(initial_value)
        v_lbl.setObjectName("metricBigValue")
        v_lbl.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {value_color};")

        l.addWidget(t_lbl)
        l.addWidget(v_lbl)
        return {"frame": card, "val_label": v_lbl}

    def _toggle_date_filter(self, enabled: bool):
        self.date_from.setEnabled(enabled)
        self.date_to.setEnabled(enabled)
        self._on_filter_changed()

    def _on_filter_changed(self):
        self.load_purchases()

    def load_purchases(self):
        """Queries the database and populates KPI metrics and purchase table."""
        search_query = self.txt_search.text().strip()
        cat_filter = self.combo_category.currentData() or "ALL"
        status_filter = self.combo_status.currentData() or "ALL"
        sort_by = self.combo_sort.currentData() or "date_desc"

        start_date = None
        end_date = None
        if self.chk_date_filter.isChecked():
            q_start = self.date_from.date()
            q_end = self.date_to.date()
            start_date = date(q_start.year(), q_start.month(), q_start.day())
            end_date = date(q_end.year(), q_end.month(), q_end.day())

        actor_context = current_actor_context()
        try:
            with get_db() as db:
                purchases = list_purchases(
                    db,
                    search=search_query,
                    start_date=start_date,
                    end_date=end_date,
                    category_filter=cat_filter,
                    status_filter=status_filter,
                    sort_by=sort_by,
                    actor_context=actor_context,
                )
                metrics = get_purchase_metrics(
                    db, actor_context=actor_context
                )
        except AuthorizationError:
            self.table.setRowCount(0)
            self.selected_purchase_id = None
            self.btn_view_details.setEnabled(False)
            self.btn_print_receipt.setEnabled(False)
            for card in (
                self.card_total,
                self.card_today,
                self.card_month,
                self.card_cost,
                self.card_paid,
                self.card_outstanding,
            ):
                card["val_label"].setText("—")
            self.lbl_table_count.setText(
                "Sign in with purchase-view permission to view purchases."
            )
            return

        # Update KPI metric cards
        self.card_total["val_label"].setText(str(metrics["total_purchases"]))
        self.card_today["val_label"].setText(str(metrics["purchases_today"]))
        self.card_month["val_label"].setText(str(metrics["purchases_this_month"]))
        self.card_cost["val_label"].setText(format_currency(metrics["total_expenditure"]))
        self.card_paid["val_label"].setText(format_currency(metrics["total_paid"]))
        self.card_outstanding["val_label"].setText(format_currency(metrics["total_outstanding"]))

        # Populate table rows
        self.table.setRowCount(len(purchases))
        for row_idx, p in enumerate(purchases):
            # 0: Purchase ID
            id_item = QTableWidgetItem(p.purchase_code)
            id_item.setData(Qt.UserRole, p.id)
            id_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 0, id_item)

            # 1: Date
            d_str = p.purchase_date.strftime("%d-%b-%Y") if p.purchase_date else "—"
            d_item = QTableWidgetItem(d_str)
            d_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 1, d_item)

            # 2: Supplier
            sup_str = f"{p.supplier.business_name} ({p.supplier.supplier_code})"
            self.table.setItem(row_idx, 2, QTableWidgetItem(sup_str))

            # 3: Items summary
            item_descs = [f"{it.quantity}x {it.appliance_category}" for it in p.items]
            items_str = ", ".join(item_descs) if item_descs else "—"
            self.table.setItem(row_idx, 3, QTableWidgetItem(items_str))

            # 4: Total Qty
            total_qty = sum(it.quantity for it in p.items)
            qty_item = QTableWidgetItem(str(total_qty))
            qty_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 4, qty_item)

            # 5: Total Cost
            cost_item = QTableWidgetItem(format_currency(p.total_amount))
            cost_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row_idx, 5, cost_item)

            # 6: Paid
            paid_item = QTableWidgetItem(format_currency(p.amount_paid))
            paid_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row_idx, 6, paid_item)

            # 7: Balance
            bal_item = QTableWidgetItem(format_currency(p.balance_amount))
            bal_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row_idx, 7, bal_item)

            # 8: Payment Status Badge
            badge_widget = QWidget()
            b_layout = QHBoxLayout(badge_widget)
            b_layout.setContentsMargins(6, 2, 6, 2)
            b_layout.setAlignment(Qt.AlignCenter)

            status_label = QLabel(f" {p.payment_status} ")
            if p.payment_status == "Paid":
                status_label.setStyleSheet(
                    "background: #dcfce7; color: #15803d; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                )
            elif p.payment_status == "Partially Paid":
                status_label.setStyleSheet(
                    "background: #fef3c7; color: #b45309; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                )
            else:
                status_label.setStyleSheet(
                    "background: #fee2e2; color: #b91c1c; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                )
            b_layout.addWidget(status_label)
            self.table.setCellWidget(row_idx, 8, badge_widget)

        # Update count label
        self.lbl_table_count.setText(f"Showing {len(purchases)} purchases")

        self._on_selection_changed()

    def _on_selection_changed(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if selected_rows:
            row = selected_rows[0].row()
            item = self.table.item(row, 0)
            if item:
                self.selected_purchase_id = item.data(Qt.UserRole)
                self.btn_view_details.setEnabled(True)
                self.btn_print_receipt.setEnabled(True)
                return

        self.selected_purchase_id = None
        self.btn_view_details.setEnabled(False)
        self.btn_print_receipt.setEnabled(False)

    def _open_new_purchase_dialog(self):
        dialog = PurchaseFormDialog(self)
        dialog.purchase_saved.connect(self._on_purchase_saved)
        dialog.exec()

    def _on_purchase_saved(self, purchase_id: int):
        self.load_purchases()

    def _on_table_double_clicked(self, index):
        row = index.row()
        item = self.table.item(row, 0)
        if item:
            pid = item.data(Qt.UserRole)
            self._show_purchase_details(pid)

    def _view_selected_purchase(self):
        if self.selected_purchase_id:
            self._show_purchase_details(self.selected_purchase_id)

    def _show_purchase_details(self, purchase_id: int):
        dialog = PurchaseDetailDialog(self, purchase_id=purchase_id)
        dialog.exec()

    def _print_selected_receipt(self):
        if not self.selected_purchase_id:
            return

        try:
            with get_db() as db:
                p = get_purchase_by_id(
                    db,
                    self.selected_purchase_id,
                    actor_context=current_actor_context(),
                )
                if not p:
                    return
                code = p.purchase_code
        except AuthorizationError:
            QMessageBox.warning(
                self, "Authentication Required",
                "Sign in with purchase-view permission to access this purchase.",
            )
            return

        suggested = f"Purchase_Receipt_{code}.pdf"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Purchase Receipt PDF",
            suggested,
            "PDF Files (*.pdf)"
        )
        if not file_path:
            return

        try:
            with get_db() as db:
                p = get_purchase_by_id(
                    db,
                    self.selected_purchase_id,
                    actor_context=current_actor_context(),
                )
                if not p:
                    return
                saved_path = generate_purchase_receipt_pdf(
                    db,
                    self.selected_purchase_id,
                    file_path,
                    actor_context=current_actor_context(),
                )

            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Information)
            msg.setWindowTitle("PDF Generated")
            msg.setText(f"Purchase receipt has been saved successfully:\n\n{saved_path}")
            btn_open = msg.addButton("Open PDF", QMessageBox.ActionRole)
            btn_ok = msg.addButton(QMessageBox.Ok)
            msg.exec()

            if msg.clickedButton() == btn_open:
                QDesktopServices.openUrl(QUrl.fromLocalFile(saved_path))

        except AuthorizationError:
            QMessageBox.warning(
                self, "Authentication Required",
                "Sign in with purchase-view permission to access this purchase.",
            )
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Failed to generate receipt: {str(e)}")

    def _on_context_menu(self, pos: QPoint):
        item = self.table.itemAt(pos)
        if not item:
            return
        row = item.row()
        id_item = self.table.item(row, 0)
        if not id_item:
            return
        pid = id_item.data(Qt.UserRole)
        self.selected_purchase_id = pid

        menu = QMenu(self)
        action_view = menu.addAction("👁 View Purchase Voucher")
        action_print = menu.addAction("📄 Print Receipt (PDF)")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == action_view:
            self._show_purchase_details(pid)
        elif chosen == action_print:
            self._print_selected_receipt()
