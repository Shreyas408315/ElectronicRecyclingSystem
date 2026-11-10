import os
from datetime import date
from PySide6.QtCore import Qt, QDate, QPoint, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QDateEdit, QCheckBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QFrame, QMessageBox, QMenu,
    QFileDialog, QInputDialog
)
from database.connection import get_db
from services.dismantling_service import (
    list_dismantling_entries,
    get_dismantling_metrics,
    get_dismantling_by_id,
    get_recent_dismantling_entries,
    approve_dismantling_entry,
    reject_dismantling_entry,
)
from services.purchase_service import APPLIANCE_CATEGORIES
from services.dismantling_report_service import (
    generate_dismantling_pdf_report,
    generate_dismantling_excel_report,
)
from ui.dismantling_form_dialog import DismantlingFormDialog
from ui.dismantling_detail_dialog import DismantlingDetailDialog
from services.security_service import (
    AuthorizationError,
    current_actor_context,
    require_permission,
)
from utils.validators import format_currency


class DismantlingPage(QWidget):
    """Main Dismantling & Material Recovery dashboard and batch tracking register."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_dismantling_id: int | None = None
        self._init_ui()
        self.load_dismantling_entries()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 20, 24, 20)
        main_layout.setSpacing(14)

        # 1. Page Header (Title + Actions)
        header_bar = QHBoxLayout()
        header_text_layout = QVBoxLayout()
        page_title = QLabel("Dismantling & Material Recovery")
        page_title.setObjectName("supplierPageTitle")
        page_subtitle = QLabel("Process raw incoming e-waste appliances, extract reusable parts, recover scrap metals, and track yields.")
        page_subtitle.setObjectName("supplierPageSubtitle")
        header_text_layout.addWidget(page_title)
        header_text_layout.addWidget(page_subtitle)
        header_bar.addLayout(header_text_layout)

        header_bar.addStretch()

        self.btn_refresh = QPushButton("↻ Refresh")
        self.btn_refresh.setObjectName("secondaryButton")
        self.btn_refresh.clicked.connect(self.load_dismantling_entries)
        header_bar.addWidget(self.btn_refresh)

        self.btn_export = QPushButton("📊 Export Report")
        self.btn_export.setObjectName("secondaryButton")
        self.btn_export.clicked.connect(self._export_report_dialog)
        header_bar.addWidget(self.btn_export)

        self.btn_new_entry = QPushButton("+ New Dismantling Batch")
        self.btn_new_entry.setObjectName("primaryButton")
        self.btn_new_entry.clicked.connect(self._open_new_entry_dialog)
        header_bar.addWidget(self.btn_new_entry)

        main_layout.addLayout(header_bar)
        self.lbl_access_error = QLabel()
        self.lbl_access_error.setObjectName("errorBanner")
        self.lbl_access_error.setWordWrap(True)
        self.lbl_access_error.hide()
        main_layout.addWidget(self.lbl_access_error)

        # 2. Metric Cards Bar (8 KPIs across two compact rows)
        metrics_frame = QFrame()
        metrics_frame.setObjectName("metricsContainer")
        metrics_layout = QVBoxLayout(metrics_frame)
        metrics_layout.setContentsMargins(14, 10, 14, 10)
        metrics_layout.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self.card_purchased = self._create_metric_card("Appliances Purchased", "0", "#0f172a")
        self.card_dismantled = self._create_metric_card("Appliances Dismantled", "0", "#15803d")
        self.card_pending = self._create_metric_card("Pending Undismantled", "0", "#b45309")
        self.card_batches = self._create_metric_card("Total Batches", "0", "#2563eb")
        row1.addWidget(self.card_purchased["frame"])
        row1.addWidget(self.card_dismantled["frame"])
        row1.addWidget(self.card_pending["frame"])
        row1.addWidget(self.card_batches["frame"])
        metrics_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.card_comps = self._create_metric_card("Recovered Components", "0", "#0284c7")
        self.card_rec_wt = self._create_metric_card("Total Recovered Weight", "0.00 kg", "#16a34a")
        self.card_scrap_wt = self._create_metric_card("Total Scrap Weight", "0.00 kg", "#b91c1c")
        self.card_labour = self._create_metric_card("Total Labour Expenses", "₹ 0.00", "#7c3aed")
        row2.addWidget(self.card_comps["frame"])
        row2.addWidget(self.card_rec_wt["frame"])
        row2.addWidget(self.card_scrap_wt["frame"])
        row2.addWidget(self.card_labour["frame"])
        metrics_layout.addLayout(row2)

        main_layout.addWidget(metrics_frame)

        recent_frame = QFrame()
        recent_frame.setObjectName("filterBar")
        recent_layout = QVBoxLayout(recent_frame)
        recent_layout.setContentsMargins(16, 10, 16, 10)
        recent_layout.setSpacing(6)
        recent_title = QLabel("Recent Dismantling Activities")
        recent_title.setObjectName("filterLabel")
        recent_layout.addWidget(recent_title)
        self.table_recent = QTableWidget()
        self.table_recent.setColumnCount(6)
        self.table_recent.setHorizontalHeaderLabels([
            "Batch ID", "Date", "Purchase ID", "Category", "Qty", "Status"
        ])
        recent_header = self.table_recent.horizontalHeader()
        recent_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        recent_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        recent_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        recent_header.setSectionResizeMode(3, QHeaderView.Stretch)
        recent_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        recent_header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table_recent.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_recent.setSelectionMode(QTableWidget.NoSelection)
        self.table_recent.verticalHeader().setVisible(False)
        self.table_recent.setMaximumHeight(150)
        recent_layout.addWidget(self.table_recent)
        main_layout.addWidget(recent_frame)

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
        self.txt_search.setPlaceholderText("🔍  Search by Batch ID, Category, Team Leader, Purchase Code, Supplier...")
        self.txt_search.setClearButtonEnabled(True)
        self.txt_search.textChanged.connect(self._on_filter_changed)
        top_filter_row.addWidget(self.txt_search, 2)

        # Category
        lbl_cat = QLabel("Category:")
        lbl_cat.setObjectName("filterLabel")
        self.combo_category = QComboBox()
        self.combo_category.addItem("All Categories", "ALL")
        for cat in APPLIANCE_CATEGORIES:
            self.combo_category.addItem(cat, cat)
        self.combo_category.currentIndexChanged.connect(self._on_filter_changed)
        top_filter_row.addWidget(lbl_cat)
        top_filter_row.addWidget(self.combo_category, 1)

        # Status
        lbl_st = QLabel("Status:")
        lbl_st.setObjectName("filterLabel")
        self.combo_status = QComboBox()
        self.combo_status.addItem("All Statuses", "ALL")
        self.combo_status.addItem("Draft", "Draft")
        self.combo_status.addItem("Pending Approval", "Pending Approval")
        self.combo_status.addItem("Approved", "Approved")
        self.combo_status.addItem("Rejected", "Rejected")
        self.combo_status.addItem("Reversed", "Reversed")
        self.combo_status.currentIndexChanged.connect(self._on_filter_changed)
        top_filter_row.addWidget(lbl_st)
        top_filter_row.addWidget(self.combo_status, 1)

        # Sort
        lbl_sort = QLabel("Sort By:")
        lbl_sort.setObjectName("filterLabel")
        self.combo_sort = QComboBox()
        self.combo_sort.addItem("Date (Newest)", "date_desc")
        self.combo_sort.addItem("Date (Oldest)", "date_asc")
        self.combo_sort.addItem("Dismantled Qty (Highest)", "qty_desc")
        self.combo_sort.addItem("Recovery Value (Highest)", "val_desc")
        self.combo_sort.addItem("Batch ID (Ascending)", "code_asc")
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

        # 4. Context Toolbar
        toolbar = QHBoxLayout()
        self.lbl_table_count = QLabel("Showing 0 batches")
        self.lbl_table_count.setObjectName("tableCountLabel")
        toolbar.addWidget(self.lbl_table_count)
        toolbar.addStretch()

        self.btn_view_details = QPushButton("View Details")
        self.btn_view_details.setObjectName("secondaryButton")
        self.btn_view_details.setEnabled(False)
        self.btn_view_details.clicked.connect(self._view_selected_entry)
        toolbar.addWidget(self.btn_view_details)

        self.btn_approve_selected = QPushButton("Approve")
        self.btn_approve_selected.setObjectName("successButton")
        self.btn_approve_selected.setEnabled(False)
        self.btn_approve_selected.clicked.connect(self._approve_selected_entry)
        toolbar.addWidget(self.btn_approve_selected)

        self.btn_reject_selected = QPushButton("Reject")
        self.btn_reject_selected.setObjectName("dangerButton")
        self.btn_reject_selected.setEnabled(False)
        self.btn_reject_selected.clicked.connect(self._reject_selected_entry)
        toolbar.addWidget(self.btn_reject_selected)

        main_layout.addLayout(toolbar)

        # 5. Dismantling Entries Table
        self.table = QTableWidget()
        self.table.setColumnCount(11)
        self.table.setHorizontalHeaderLabels([
            "Batch ID",
            "Date",
            "Purchase ID",
            "Category",
            "Dismantled",
            "Rec. Wt (kg)",
            "Scrap Wt (kg)",
            "Recovery %",
            "Labour Cost",
            "Est. Valuation",
            "Status",
        ])

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(9, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(10, QHeaderView.ResizeToContents)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setObjectName("supplierTable")

        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self._on_table_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        main_layout.addWidget(self.table)

    def _create_metric_card(self, title: str, initial_value: str, value_color: str) -> dict:
        card = QFrame()
        card.setObjectName("metricCard")
        l = QVBoxLayout(card)
        l.setContentsMargins(12, 8, 12, 8)
        l.setSpacing(3)

        t_lbl = QLabel(title)
        t_lbl.setObjectName("metricTitle")

        v_lbl = QLabel(initial_value)
        v_lbl.setObjectName("metricBigValue")
        v_lbl.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {value_color};")

        l.addWidget(t_lbl)
        l.addWidget(v_lbl)
        return {"frame": card, "val_label": v_lbl}

    def _has_permission(self, permission_code: str) -> bool:
        actor_context = current_actor_context()
        if actor_context is None:
            return False
        try:
            with get_db() as db:
                require_permission(
                    actor_context, permission_code, db_session=db
                )
            return True
        except AuthorizationError:
            return False

    def _toggle_date_filter(self, enabled: bool):
        self.date_from.setEnabled(enabled)
        self.date_to.setEnabled(enabled)
        self._on_filter_changed()

    def _on_filter_changed(self):
        self.load_dismantling_entries()

    def load_dismantling_entries(self):
        """Loads dismantling records and updates dashboard KPIs."""
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
            if actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                metrics = get_dismantling_metrics(
                    db,
                    start_date=start_date,
                    end_date=end_date,
                    category_filter=cat_filter,
                    status_filter=status_filter,
                    actor_context=actor_context,
                )
                recent = get_recent_dismantling_entries(
                    db, limit=5, actor_context=actor_context
                )
                entries = list_dismantling_entries(
                    db,
                    search=search_query,
                    start_date=start_date,
                    end_date=end_date,
                    category_filter=cat_filter,
                    status_filter=status_filter,
                    sort_by=sort_by,
                    actor_context=actor_context,
                )
            self.table_recent.setRowCount(len(recent))
            for r_idx, rec in enumerate(recent):
                self.table_recent.setItem(r_idx, 0, QTableWidgetItem(rec.dismantling_code))
                d_str = rec.dismantling_date.strftime("%d-%b-%Y") if rec.dismantling_date else "—"
                self.table_recent.setItem(r_idx, 1, QTableWidgetItem(d_str))
                p_code = rec.purchase.purchase_code if rec.purchase else "—"
                self.table_recent.setItem(r_idx, 2, QTableWidgetItem(p_code))
                self.table_recent.setItem(r_idx, 3, QTableWidgetItem(rec.appliance_category))
                self.table_recent.setItem(r_idx, 4, QTableWidgetItem(str(rec.appliances_dismantled)))
                self.table_recent.setItem(r_idx, 5, QTableWidgetItem(rec.status))
        except AuthorizationError as exc:
            self.table.setRowCount(0)
            self.table_recent.setRowCount(0)
            self.lbl_table_count.setText("No dismantling batches available")
            for card in (
                self.card_purchased, self.card_dismantled, self.card_pending,
                self.card_batches, self.card_comps, self.card_rec_wt,
                self.card_scrap_wt, self.card_labour,
            ):
                card["val_label"].setText("—")
            self.lbl_access_error.setText(
                f"Authentication or permission required to view dismantling data: {exc}"
            )
            self.lbl_access_error.show()
            self.btn_export.setEnabled(False)
            self.btn_new_entry.setEnabled(False)
            self.selected_dismantling_id = None
            self.btn_view_details.setEnabled(False)
            self.btn_approve_selected.setEnabled(False)
            self.btn_reject_selected.setEnabled(False)
            return

        self.lbl_access_error.hide()
        self.btn_export.setEnabled(True)
        self.btn_new_entry.setEnabled(self._has_permission("dismantling.manage"))

        # Update KPI metric cards
        self.card_purchased["val_label"].setText(str(metrics["total_appliances_purchased"]))
        self.card_dismantled["val_label"].setText(str(metrics["total_appliances_dismantled"]))
        self.card_pending["val_label"].setText(str(metrics["pending_dismantling_quantity"]))
        self.card_batches["val_label"].setText(str(metrics["total_dismantling_batches"]))
        self.card_comps["val_label"].setText(f"{metrics['total_recovered_components']:.0f} pcs")
        self.card_rec_wt["val_label"].setText(f"{metrics['total_recovered_weight']:.2f} kg")
        self.card_scrap_wt["val_label"].setText(f"{metrics['total_scrap_weight']:.2f} kg")
        self.card_labour["val_label"].setText(format_currency(metrics["total_labour_cost"]))

        # Populate table rows
        self.table.setRowCount(len(entries))
        for row_idx, e in enumerate(entries):
            # 0: Batch ID
            id_item = QTableWidgetItem(e.dismantling_code)
            id_item.setData(Qt.UserRole, e.id)
            id_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 0, id_item)

            # 1: Date
            d_str = e.dismantling_date.strftime("%d-%b-%Y") if e.dismantling_date else "—"
            d_item = QTableWidgetItem(d_str)
            d_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 1, d_item)

            # 2: Purchase ID
            p_code = e.purchase.purchase_code if e.purchase else "—"
            self.table.setItem(row_idx, 2, QTableWidgetItem(p_code))

            # 3: Category
            bm = f"{e.brand or ''} {e.model_number or ''}".strip()
            cat_desc = f"{e.appliance_category} ({bm})" if bm else e.appliance_category
            self.table.setItem(row_idx, 3, QTableWidgetItem(cat_desc))

            # 4: Dismantled Qty
            qty_item = QTableWidgetItem(str(e.appliances_dismantled))
            qty_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 4, qty_item)

            # 5: Recovered Wt
            rec_wt_item = QTableWidgetItem(f"{e.total_recovered_weight:.2f}")
            rec_wt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row_idx, 5, rec_wt_item)

            # 6: Scrap Wt
            scrap_wt_item = QTableWidgetItem(f"{e.total_scrap_weight:.2f}")
            scrap_wt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row_idx, 6, scrap_wt_item)

            # 7: Recovery %
            rec_pct_str = f"{e.recovery_percentage:.1f}%" if e.recovery_percentage is not None else "N/A"
            pct_item = QTableWidgetItem(rec_pct_str)
            pct_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 7, pct_item)

            # 8: Labour Cost
            lab_item = QTableWidgetItem(format_currency(e.labour_charges))
            lab_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row_idx, 8, lab_item)

            # 9: Est. Recovery Valuation
            val_item = QTableWidgetItem(format_currency(e.estimated_recovery_value))
            val_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row_idx, 9, val_item)

            # 10: Status Badge
            badge_widget = QWidget()
            b_layout = QHBoxLayout(badge_widget)
            b_layout.setContentsMargins(6, 2, 6, 2)
            b_layout.setAlignment(Qt.AlignCenter)

            status_label = QLabel(f" {e.status} ")
            if e.status == "Approved":
                status_label.setStyleSheet(
                    "background: #dcfce7; color: #15803d; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                )
            elif e.status == "Pending Approval":
                status_label.setStyleSheet(
                    "background: #fef3c7; color: #b45309; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                )
            elif e.status == "Rejected":
                status_label.setStyleSheet(
                    "background: #fee2e2; color: #b91c1c; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                )
            elif e.status == "Reversed":
                status_label.setStyleSheet(
                    "background: #f1f5f9; color: #475569; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                )
            else:
                status_label.setStyleSheet(
                    "background: #eff6ff; color: #1d4ed8; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                )
            b_layout.addWidget(status_label)
            self.table.setCellWidget(row_idx, 10, badge_widget)

        self.lbl_table_count.setText(f"Showing {len(entries)} dismantling batches")

        self._on_selection_changed()

    def _on_selection_changed(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if selected_rows:
            row = selected_rows[0].row()
            item = self.table.item(row, 0)
            if item:
                self.selected_dismantling_id = item.data(Qt.UserRole)
                self.btn_view_details.setEnabled(True)

                with get_db() as db:
                    try:
                        actor_context = current_actor_context()
                        if actor_context is None:
                            raise AuthorizationError("Sign in is required.")
                        e = get_dismantling_by_id(
                            db,
                            self.selected_dismantling_id,
                            actor_context=actor_context,
                        )
                    except AuthorizationError:
                        self.btn_view_details.setEnabled(False)
                        self.btn_approve_selected.setEnabled(False)
                        self.btn_reject_selected.setEnabled(False)
                        QMessageBox.warning(
                            self,
                            "Authentication Required",
                            "Sign in with dismantling-view permission to view this batch.",
                        )
                        return
                    can_review = e and e.status in ["Draft", "Pending Approval"]
                    self.btn_approve_selected.setEnabled(
                        bool(can_review)
                        and self._has_permission("dismantling.approve")
                        and self._has_permission("inventory.adjust")
                    )
                    self.btn_reject_selected.setEnabled(
                        bool(can_review)
                        and self._has_permission("dismantling.approve")
                    )
                return

        self.selected_dismantling_id = None
        self.btn_view_details.setEnabled(False)
        self.btn_approve_selected.setEnabled(False)
        self.btn_reject_selected.setEnabled(False)

    def _open_new_entry_dialog(self):
        dialog = DismantlingFormDialog(self)
        dialog.entry_saved.connect(self._on_entry_saved)
        dialog.exec()

    def _on_entry_saved(self, entry_id: int):
        self.load_dismantling_entries()

    def _on_table_double_clicked(self, index):
        row = index.row()
        item = self.table.item(row, 0)
        if item:
            eid = item.data(Qt.UserRole)
            self._show_entry_details(eid)

    def _view_selected_entry(self):
        if self.selected_dismantling_id:
            self._show_entry_details(self.selected_dismantling_id)

    def _show_entry_details(self, entry_id: int):
        dialog = DismantlingDetailDialog(self, entry_id=entry_id)
        dialog.entry_updated.connect(self._on_entry_updated)
        dialog.exec()

    def _on_entry_updated(self, entry_id: int):
        self.load_dismantling_entries()

    def _approve_selected_entry(self):
        if not self.selected_dismantling_id:
            return

        confirm = QMessageBox.question(
            self,
            "Confirm Batch Approval",
            "Are you sure you want to approve this dismantling entry?\n\n"
            "This will credit recovered components and recyclable scrap to inventory and record stock audit movements.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if confirm == QMessageBox.Yes:
            try:
                actor_context = current_actor_context()
                if actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                self.btn_approve_selected.setEnabled(False)
                with get_db() as db:
                    approve_dismantling_entry(
                        db,
                        self.selected_dismantling_id,
                        approved_by="Supervisor",
                        actor_context=actor_context,
                    )
                QMessageBox.information(self, "Approved", "Dismantling batch approved and inventory credited.")
                self.load_dismantling_entries()
            except AuthorizationError as e:
                QMessageBox.warning(self, "Permission Denied", str(e))
                self.load_dismantling_entries()
            except Exception as e:
                QMessageBox.critical(self, "Approval Failed", str(e))

    def _reject_selected_entry(self):
        if not self.selected_dismantling_id:
            return

        reason, ok = QInputDialog.getText(
            self,
            "Reject Batch",
            "Enter reason for rejecting this dismantling batch:"
        )
        if ok and reason.strip():
            try:
                actor_context = current_actor_context()
                if actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                self.btn_reject_selected.setEnabled(False)
                with get_db() as db:
                    reject_dismantling_entry(
                        db,
                        self.selected_dismantling_id,
                        reason=reason.strip(),
                        actor_context=actor_context,
                    )
                QMessageBox.information(self, "Rejected", "Batch marked as rejected.")
                self.load_dismantling_entries()
            except AuthorizationError as e:
                QMessageBox.warning(self, "Permission Denied", str(e))
                self.load_dismantling_entries()
            except Exception as e:
                QMessageBox.critical(self, "Rejection Failed", str(e))

    def _export_report_dialog(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("Export Dismantling Report")
        msg.setText("Select report format to export:")
        btn_pdf = msg.addButton("📄 Export PDF Report", QMessageBox.ActionRole)
        btn_excel = msg.addButton("📊 Export Excel (.xlsx)", QMessageBox.ActionRole)
        btn_cancel = msg.addButton(QMessageBox.Cancel)
        msg.exec()

        start_date = None
        end_date = None
        if self.chk_date_filter.isChecked():
            start_date = self.date_from.date().toPython()
            end_date = self.date_to.date().toPython()

        cat = self.combo_category.currentData() or "ALL"
        st = self.combo_status.currentData() or "ALL"

        if msg.clickedButton() == btn_pdf:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Dismantling Report PDF", "Dismantling_Report.pdf", "PDF Files (*.pdf)"
            )
            if file_path:
                try:
                    actor_context = current_actor_context()
                    if actor_context is None:
                        raise AuthorizationError("Sign in is required.")
                    with get_db() as db:
                        saved = generate_dismantling_pdf_report(
                            db, file_path, start_date=start_date, end_date=end_date,
                            category_filter=cat, status_filter=st,
                            actor_context=actor_context,
                        )
                    QMessageBox.information(self, "Report Saved", f"PDF report saved:\n\n{saved}")
                    QDesktopServices.openUrl(QUrl.fromLocalFile(saved))
                except Exception as e:
                    QMessageBox.critical(self, "Export Failed", str(e))

        elif msg.clickedButton() == btn_excel:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Dismantling Report Excel", "Dismantling_Report.xlsx", "Excel Files (*.xlsx)"
            )
            if file_path:
                try:
                    actor_context = current_actor_context()
                    if actor_context is None:
                        raise AuthorizationError("Sign in is required.")
                    with get_db() as db:
                        saved = generate_dismantling_excel_report(
                            db, file_path, start_date=start_date, end_date=end_date,
                            category_filter=cat, status_filter=st,
                            actor_context=actor_context,
                        )
                    QMessageBox.information(self, "Report Saved", f"Excel workbook saved:\n\n{saved}")
                    QDesktopServices.openUrl(QUrl.fromLocalFile(saved))
                except Exception as e:
                    QMessageBox.critical(self, "Export Failed", str(e))

    def _on_context_menu(self, pos: QPoint):
        item = self.table.itemAt(pos)
        if not item:
            return
        row = item.row()
        id_item = self.table.item(row, 0)
        if not id_item:
            return
        eid = id_item.data(Qt.UserRole)
        self.selected_dismantling_id = eid

        menu = QMenu(self)
        action_view = menu.addAction("👁 View Details & Audit")

        try:
            actor_context = current_actor_context()
            if actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                e = get_dismantling_by_id(
                    db, eid, actor_context=actor_context
                )
                can_review = e and e.status in ["Draft", "Pending Approval"]
        except AuthorizationError:
            QMessageBox.warning(
                self,
                "Authentication Required",
                "Sign in with dismantling-view permission to view this batch.",
            )
            return

        action_approve = None
        action_reject = None
        if can_review and self._has_permission("dismantling.approve"):
            action_reject = menu.addAction("⛔ Reject Batch")
        else:
            action_reject = None
        if (
            can_review
            and self._has_permission("dismantling.approve")
            and self._has_permission("inventory.adjust")
        ):
            action_approve = menu.addAction("✔ Approve & Credit Inventory")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == action_view:
            self._show_entry_details(eid)
        elif chosen == action_approve:
            self._approve_selected_entry()
        elif chosen == action_reject:
            self._reject_selected_entry()
