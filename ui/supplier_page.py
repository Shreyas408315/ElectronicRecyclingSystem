from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFrame, QMessageBox, QMenu
)
from database.connection import get_db
from services.supplier_service import (
    list_suppliers,
    get_supplier_metrics,
    deactivate_supplier,
    reactivate_supplier,
    get_supplier_by_id,
)
from ui.supplier_form_dialog import SupplierFormDialog
from ui.supplier_detail_dialog import SupplierDetailDialog
from utils.validators import format_currency
from services.security_service import AuthorizationError, current_actor_context


class SupplierPage(QWidget):
    """Main Supplier Management view featuring metrics, search/filtering, table listing, and CRUD actions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_selected_supplier_id: int | None = None
        self._init_ui()
        self.load_suppliers()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 20, 24, 20)
        main_layout.setSpacing(16)

        # 1. Page Header (Title + Actions)
        header_bar = QHBoxLayout()
        header_text_layout = QVBoxLayout()
        page_title = QLabel("Supplier Management")
        page_title.setObjectName("supplierPageTitle")
        page_subtitle = QLabel("Manage raw scrap suppliers, recycler vendors, GSTINs, and opening balances.")
        page_subtitle.setObjectName("supplierPageSubtitle")
        header_text_layout.addWidget(page_title)
        header_text_layout.addWidget(page_subtitle)
        header_bar.addLayout(header_text_layout)

        header_bar.addStretch()

        self.btn_refresh = QPushButton("↻ Refresh")
        self.btn_refresh.setObjectName("secondaryButton")
        self.btn_refresh.clicked.connect(self.load_suppliers)
        header_bar.addWidget(self.btn_refresh)

        self.btn_add_supplier = QPushButton("+ Add Supplier")
        self.btn_add_supplier.setObjectName("primaryButton")
        self.btn_add_supplier.clicked.connect(self._open_add_supplier_dialog)
        header_bar.addWidget(self.btn_add_supplier)

        main_layout.addLayout(header_bar)

        # 2. Metric Cards Bar
        metrics_frame = QFrame()
        metrics_frame.setObjectName("metricsContainer")
        metrics_layout = QHBoxLayout(metrics_frame)
        metrics_layout.setContentsMargins(16, 14, 16, 14)
        metrics_layout.setSpacing(16)

        self.card_total = self._create_metric_card("Total Suppliers", "0", "#1e293b")
        self.card_active = self._create_metric_card("Active Suppliers", "0", "#15803d")
        self.card_inactive = self._create_metric_card("Inactive Suppliers", "0", "#b91c1c")
        self.card_balance = self._create_metric_card("Total Opening Balance", "₹ 0.00", "#1d4ed8")

        metrics_layout.addWidget(self.card_total["frame"])
        metrics_layout.addWidget(self.card_active["frame"])
        metrics_layout.addWidget(self.card_inactive["frame"])
        metrics_layout.addWidget(self.card_balance["frame"])

        main_layout.addWidget(metrics_frame)

        # 3. Search & Filter Bar
        filter_card = QFrame()
        filter_card.setObjectName("filterBar")
        filter_layout = QHBoxLayout(filter_card)
        filter_layout.setContentsMargins(16, 12, 16, 12)
        filter_layout.setSpacing(12)

        # Search field
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("🔍  Search by Supplier ID, Name, Phone, Contact Person, City...")
        self.txt_search.setClearButtonEnabled(True)
        self.txt_search.textChanged.connect(self._on_search_changed)
        filter_layout.addWidget(self.txt_search, 2)

        # Status filter combo
        lbl_status = QLabel("Status:")
        lbl_status.setObjectName("filterLabel")
        self.combo_status = QComboBox()
        self.combo_status.addItem("All Suppliers", "ALL")
        self.combo_status.addItem("Active Only", "ACTIVE")
        self.combo_status.addItem("Inactive Only", "INACTIVE")
        self.combo_status.currentIndexChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(lbl_status)
        filter_layout.addWidget(self.combo_status, 1)

        # Sort combo
        lbl_sort = QLabel("Sort By:")
        lbl_sort.setObjectName("filterLabel")
        self.combo_sort = QComboBox()
        self.combo_sort.addItem("Supplier Name (A-Z)", "name_asc")
        self.combo_sort.addItem("Supplier Name (Z-A)", "name_desc")
        self.combo_sort.addItem("Registration Date (Newest)", "date_desc")
        self.combo_sort.addItem("Registration Date (Oldest)", "date_asc")
        self.combo_sort.addItem("Supplier ID (Ascending)", "code_asc")
        self.combo_sort.currentIndexChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(lbl_sort)
        filter_layout.addWidget(self.combo_sort, 1)

        main_layout.addWidget(filter_card)

        # 4. Context Toolbar for selected row
        toolbar = QHBoxLayout()
        self.lbl_table_count = QLabel("Showing 0 suppliers")
        self.lbl_table_count.setObjectName("tableCountLabel")
        toolbar.addWidget(self.lbl_table_count)
        toolbar.addStretch()

        self.btn_view_details = QPushButton("View Details")
        self.btn_view_details.setObjectName("secondaryButton")
        self.btn_view_details.setEnabled(False)
        self.btn_view_details.clicked.connect(self._view_selected_supplier)
        toolbar.addWidget(self.btn_view_details)

        self.btn_edit_selected = QPushButton("Edit")
        self.btn_edit_selected.setObjectName("secondaryButton")
        self.btn_edit_selected.setEnabled(False)
        self.btn_edit_selected.clicked.connect(self._edit_selected_supplier)
        toolbar.addWidget(self.btn_edit_selected)

        self.btn_toggle_selected = QPushButton("Deactivate / Reactivate")
        self.btn_toggle_selected.setObjectName("secondaryButton")
        self.btn_toggle_selected.setEnabled(False)
        self.btn_toggle_selected.clicked.connect(self._toggle_selected_status)
        toolbar.addWidget(self.btn_toggle_selected)

        main_layout.addLayout(toolbar)

        # 5. Suppliers Data Table
        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "Supplier ID",
            "Supplier Name",
            "Contact Person",
            "Phone",
            "Email",
            "GSTIN",
            "Address",
            "Opening Balance",
            "Status",
        ])

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
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
        l.setContentsMargins(16, 12, 16, 12)
        l.setSpacing(4)

        t_lbl = QLabel(title)
        t_lbl.setObjectName("metricTitle")

        v_lbl = QLabel(initial_value)
        v_lbl.setObjectName("metricBigValue")
        v_lbl.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {value_color};")

        l.addWidget(t_lbl)
        l.addWidget(v_lbl)
        return {"frame": card, "val_label": v_lbl}

    def _on_search_changed(self):
        self.load_suppliers()

    def _on_filter_changed(self):
        self.load_suppliers()

    def load_suppliers(self):
        """Fetches suppliers from the database and updates metrics & table."""
        search_query = self.txt_search.text().strip()
        status_filter = self.combo_status.currentData() or "ALL"
        sort_by = self.combo_sort.currentData() or "name_asc"

        with get_db() as db:
            try:
                metrics = get_supplier_metrics(db, actor_context=current_actor_context())
            except AuthorizationError:
                # Service authorization remains the boundary.  A locked or
                # expired session must not populate even aggregate supplier data.
                self.table.setRowCount(0)
                for card in (self.card_total, self.card_active, self.card_inactive, self.card_balance):
                    card["val_label"].setText("—")
                return
            suppliers = list_suppliers(
                db,
                search=search_query,
                status_filter=status_filter,
                sort_by=sort_by,
                actor_context=current_actor_context(),
            )

            # Update metrics cards
            self.card_total["val_label"].setText(str(metrics["total"]))
            self.card_active["val_label"].setText(str(metrics["active"]))
            self.card_inactive["val_label"].setText(str(metrics["inactive"]))
            self.card_balance["val_label"].setText(format_currency(metrics["total_opening_balance"]))

            # Populate table
            self.table.setRowCount(len(suppliers))
            for row_idx, s in enumerate(suppliers):
                # 0: Supplier ID
                id_item = QTableWidgetItem(s.supplier_code)
                id_item.setData(Qt.UserRole, s.id)
                id_item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row_idx, 0, id_item)

                # 1: Business Name
                name_item = QTableWidgetItem(s.business_name)
                name_item.setData(Qt.UserRole, s.id)
                self.table.setItem(row_idx, 1, name_item)

                # 2: Contact Person
                contact_item = QTableWidgetItem(s.contact_person or "—")
                self.table.setItem(row_idx, 2, contact_item)

                # 3: Phone
                phone_item = QTableWidgetItem(s.phone or "—")
                self.table.setItem(row_idx, 3, phone_item)

                # 4: Email
                email_item = QTableWidgetItem(s.email or "—")
                self.table.setItem(row_idx, 4, email_item)

                # 5: GSTIN
                gstin_item = QTableWidgetItem(s.gstin or "—")
                gstin_item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row_idx, 5, gstin_item)

                # 6: Address / City
                loc_parts = [p for p in [s.address, s.city, s.state] if p]
                loc_str = ", ".join(loc_parts) if loc_parts else "—"
                self.table.setItem(row_idx, 6, QTableWidgetItem(loc_str))

                # 7: Opening Balance
                bal_item = QTableWidgetItem(format_currency(s.opening_balance))
                bal_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row_idx, 7, bal_item)

                # 8: Status (Widget badge)
                status_widget = QWidget()
                status_layout = QHBoxLayout(status_widget)
                status_layout.setContentsMargins(6, 2, 6, 2)
                status_layout.setAlignment(Qt.AlignCenter)

                status_label = QLabel(" Active " if s.is_active else " Inactive ")
                if s.is_active:
                    status_label.setStyleSheet(
                        "background: #dcfce7; color: #15803d; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                    )
                else:
                    status_label.setStyleSheet(
                        "background: #fee2e2; color: #b91c1c; border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 11px;"
                    )
                status_layout.addWidget(status_label)
                self.table.setCellWidget(row_idx, 8, status_widget)

            # Update counter label
            total_count = metrics["total"]
            shown_count = len(suppliers)
            if search_query or status_filter != "ALL":
                self.lbl_table_count.setText(f"Showing {shown_count} of {total_count} suppliers (Filtered)")
            else:
                self.lbl_table_count.setText(f"Total Suppliers: {shown_count}")

        # Reset selection state
        self._on_selection_changed()

    def _on_selection_changed(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if selected_rows:
            row = selected_rows[0].row()
            item = self.table.item(row, 0)
            if item:
                self.current_selected_supplier_id = item.data(Qt.UserRole)
                self.btn_view_details.setEnabled(True)
                self.btn_edit_selected.setEnabled(True)
                self.btn_toggle_selected.setEnabled(True)

                # Update toggle button text based on status
                with get_db() as db:
                    s = get_supplier_by_id(db, self.current_selected_supplier_id, actor_context=current_actor_context())
                    if s and not s.is_active:
                        self.btn_toggle_selected.setText("Reactivate Supplier")
                    else:
                        self.btn_toggle_selected.setText("Deactivate Supplier")
                return

        self.current_selected_supplier_id = None
        self.btn_view_details.setEnabled(False)
        self.btn_edit_selected.setEnabled(False)
        self.btn_toggle_selected.setEnabled(False)
        self.btn_toggle_selected.setText("Deactivate / Reactivate")

    def _open_add_supplier_dialog(self):
        dialog = SupplierFormDialog(self)
        dialog.supplier_saved.connect(self._on_supplier_saved)
        dialog.exec()

    def _on_supplier_saved(self, supplier_id: int):
        self.load_suppliers()

    def _on_table_double_clicked(self, index):
        row = index.row()
        item = self.table.item(row, 0)
        if item:
            supplier_id = item.data(Qt.UserRole)
            self._show_details_dialog(supplier_id)

    def _view_selected_supplier(self):
        if self.current_selected_supplier_id:
            self._show_details_dialog(self.current_selected_supplier_id)

    def _show_details_dialog(self, supplier_id: int):
        dialog = SupplierDetailDialog(self, supplier_id=supplier_id)
        dialog.supplier_modified.connect(self._on_supplier_modified)
        dialog.exec()

    def _on_supplier_modified(self, supplier_id: int):
        self.load_suppliers()

    def _edit_selected_supplier(self):
        if not self.current_selected_supplier_id:
            return
        with get_db() as db:
            s = get_supplier_by_id(db, self.current_selected_supplier_id, actor_context=current_actor_context())
            if not s:
                QMessageBox.warning(self, "Not Found", "Selected supplier could not be found.")
                return
            dialog = SupplierFormDialog(self, supplier=s)
            dialog.supplier_saved.connect(self._on_supplier_saved)
            dialog.exec()

    def _toggle_selected_status(self):
        if not self.current_selected_supplier_id:
            return
        with get_db() as db:
            s = get_supplier_by_id(db, self.current_selected_supplier_id, actor_context=current_actor_context())
            if not s:
                return
            name = s.business_name
            code = s.supplier_code
            is_active = s.is_active

        if is_active:
            confirm = QMessageBox.question(
                self,
                "Confirm Deactivation",
                f"Are you sure you want to deactivate supplier:\n\n{name} ({code})?\n\n"
                "The supplier will be preserved in historical records but marked inactive.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if confirm == QMessageBox.Yes:
                with get_db() as db:
                    deactivate_supplier(
                        db,
                        self.current_selected_supplier_id,
                        actor_context=current_actor_context(),
                    )
                self.load_suppliers()
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
                    reactivate_supplier(db, self.current_selected_supplier_id, actor_context=current_actor_context())
                self.load_suppliers()

    def _on_context_menu(self, pos: QPoint):
        item = self.table.itemAt(pos)
        if not item:
            return
        row = item.row()
        id_item = self.table.item(row, 0)
        if not id_item:
            return
        sup_id = id_item.data(Qt.UserRole)
        self.current_selected_supplier_id = sup_id

        menu = QMenu(self)
        action_view = menu.addAction("👁 View Full Profile")
        action_edit = menu.addAction("✎ Edit Details")
        menu.addSeparator()

        with get_db() as db:
            s = get_supplier_by_id(db, sup_id, actor_context=current_actor_context())
            is_act = s.is_active if s else True

        if is_act:
            action_toggle = menu.addAction("⛔ Deactivate Supplier")
        else:
            action_toggle = menu.addAction("✔ Reactivate Supplier")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == action_view:
            self._show_details_dialog(sup_id)
        elif chosen == action_edit:
            self._edit_selected_supplier()
        elif chosen == action_toggle:
            self._toggle_selected_status()

