from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
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
from services.inventory_report_service import (
    generate_inventory_excel_report,
    generate_inventory_pdf_report,
)
from services.inventory_service import (
    InventoryValidationError,
    deactivate_inventory_item,
    get_inventory_categories,
    get_inventory_item,
    get_inventory_metrics,
    get_inventory_movements,
    list_inventory_items,
)
from ui.inventory_dialogs import InventoryDetailDialog, InventoryItemDialog, StockMovementDialog


class InventoryPage(QWidget):
    """Recovered-material inventory catalogue, stock controls, and dashboard."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_item_id: int | None = None
        self._init_ui()
        self.load_inventory()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("Inventory Management")
        title.setObjectName("supplierPageTitle")
        subtitle = QLabel("Manage approved recovered stock, auditable movements, storage, and valuation.")
        subtitle.setObjectName("supplierPageSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.btn_export = QPushButton("Export PDF / Excel")
        self.btn_export.setObjectName("secondaryButton")
        self.btn_export.clicked.connect(self._export_report)
        header.addWidget(self.btn_export)
        self.btn_add = QPushButton("+ Add Catalogue Item")
        self.btn_add.setObjectName("primaryButton")
        self.btn_add.clicked.connect(self._add_item)
        header.addWidget(self.btn_add)
        layout.addLayout(header)
        self.lbl_access_error = QLabel()
        self.lbl_access_error.setStyleSheet("color: #b91c1c;")
        self.lbl_access_error.setWordWrap(True)
        self.lbl_access_error.hide()
        layout.addWidget(self.lbl_access_error)

        metric_frame = QFrame()
        metric_frame.setObjectName("metricsContainer")
        metrics_layout = QGridLayout(metric_frame)
        metrics_layout.setContentsMargins(12, 10, 12, 10)
        metrics_layout.setSpacing(8)
        self.metric_labels: dict[str, QLabel] = {}
        metric_specs = [
            ("total_unique_items", "Unique Items"),
            ("total_physical_quantity", "Physical Quantity"),
            ("total_reserved_quantity", "Reserved Quantity"),
            ("total_available_quantity", "Available Quantity"),
            ("total_verified_weight", "Verified Weight (kg)"),
            ("cost_quantity_value", "Cost Value (Quantity)"),
            ("cost_weight_value", "Cost Value (Weight Basis)"),
            ("estimated_selling_value", "Estimated Selling Value"),
            ("working_components_value", "Working Components Value"),
            ("non_working_components_value", "Non-working Value"),
            ("damaged_quantity", "Damaged Quantity"),
            ("low_stock_count", "Low-stock Items"),
            ("out_of_stock_count", "Out of Stock"),
        ]
        for index, (key, label) in enumerate(metric_specs):
            card = QFrame()
            card.setObjectName("metricCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 6, 10, 6)
            title_label = QLabel(label)
            title_label.setObjectName("metricTitle")
            value = QLabel("0")
            value.setObjectName("metricBigValue")
            card_layout.addWidget(title_label)
            card_layout.addWidget(value)
            metrics_layout.addWidget(card, index // 4, index % 4)
            self.metric_labels[key] = value
        layout.addWidget(metric_frame)
        self.lbl_weight_note = QLabel()
        self.lbl_weight_note.setObjectName("muted")
        layout.addWidget(self.lbl_weight_note)

        filters = QFrame()
        filters.setObjectName("filterBar")
        filter_layout = QHBoxLayout(filters)
        filter_layout.setContentsMargins(12, 8, 12, 8)
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search Part ID or component name")
        self.txt_search.setClearButtonEnabled(True)
        self.txt_search.textChanged.connect(self.load_inventory)
        filter_layout.addWidget(self.txt_search, 2)
        self.combo_category = QComboBox()
        self.combo_category.addItem("All categories", "ALL")
        self.combo_category.currentIndexChanged.connect(self.load_inventory)
        filter_layout.addWidget(self.combo_category)
        self.combo_condition = QComboBox()
        self.combo_condition.addItem("All conditions", "ALL")
        self.combo_condition.addItems(["Working", "Recyclable Raw Material", "Non-working", "Damaged"])
        self.combo_condition.currentIndexChanged.connect(self.load_inventory)
        filter_layout.addWidget(self.combo_condition)
        self.txt_location = QLineEdit()
        self.txt_location.setPlaceholderText("Filter storage location")
        self.txt_location.textChanged.connect(self.load_inventory)
        filter_layout.addWidget(self.txt_location)
        self.chk_low_stock = QCheckBox("Low stock")
        self.chk_low_stock.toggled.connect(self.load_inventory)
        filter_layout.addWidget(self.chk_low_stock)
        self.combo_sort = QComboBox()
        self.combo_sort.addItem("Name (A–Z)", "name_asc")
        self.combo_sort.addItem("Name (Z–A)", "name_desc")
        self.combo_sort.addItem("Quantity (low first)", "quantity_asc")
        self.combo_sort.addItem("Quantity (high first)", "quantity_desc")
        self.combo_sort.addItem("Estimated value (high first)", "value_desc")
        self.combo_sort.currentIndexChanged.connect(self.load_inventory)
        filter_layout.addWidget(self.combo_sort)
        layout.addWidget(filters)

        toolbar = QHBoxLayout()
        self.lbl_count = QLabel("Showing 0 inventory items")
        self.lbl_count.setObjectName("tableCountLabel")
        toolbar.addWidget(self.lbl_count)
        toolbar.addStretch()
        for text, slot, object_name in [
            ("Details", self._view_details, "secondaryButton"),
            ("Edit Catalogue", self._edit_item, "secondaryButton"),
            ("Record Movement", self._record_movement, "primaryButton"),
            ("Deactivate", self._deactivate_item, "dangerButton"),
        ]:
            button = QPushButton(text)
            button.setObjectName(object_name)
            button.clicked.connect(slot)
            button.setEnabled(False)
            setattr(self, f"btn_{text.lower().replace(' ', '_')}", button)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        headers = [
            "Part ID", "Component", "Category", "Appliance Source", "Brand / Model",
            "Physical", "Reserved", "Available", "Unit", "Total Weight (kg)", "Weight Status", "Condition",
            "Cost Price", "Est. Selling Price", "Location", "Min. Stock", "Status",
        ]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        header = self.table.horizontalHeader()
        for col in range(len(headers)):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setObjectName("supplierTable")
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _: self._view_details())
        layout.addWidget(self.table, 3)

        recent_title = QLabel("Recent Stock Movements")
        recent_title.setObjectName("filterLabel")
        layout.addWidget(recent_title)
        self.table_recent = QTableWidget(0, 8)
        self.table_recent.setHorizontalHeaderLabels([
            "ID", "Date", "Part ID / Name", "Type", "Quantity change", "Weight change",
            "Reference", "Responsible user",
        ])
        self.table_recent.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table_recent.setMaximumHeight(175)
        self.table_recent.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_recent.setSelectionMode(QTableWidget.NoSelection)
        self.table_recent.verticalHeader().setVisible(False)
        layout.addWidget(self.table_recent, 1)
        self.btn_add.setEnabled(self._has_permission("inventory.manage"))
        self.btn_export.setEnabled(self._has_permission("inventory.view"))

    @staticmethod
    def _has_permission(permission_code: str) -> bool:
        actor_context = current_actor_context()
        if actor_context is None:
            return False
        try:
            with get_db() as db:
                require_permission(actor_context, permission_code, db_session=db)
            return True
        except AuthorizationError:
            return False

    def load_inventory(self, *_):
        actor_context = current_actor_context()
        try:
            if actor_context is None:
                raise AuthorizationError("Sign in is required to view inventory.")
            with get_db() as db:
                categories = get_inventory_categories(
                    db, actor_context=actor_context
                )
                selected_category = self.combo_category.currentData()
                if categories != [self.combo_category.itemText(i) for i in range(1, self.combo_category.count())]:
                    self.combo_category.blockSignals(True)
                    self.combo_category.clear()
                    self.combo_category.addItem("All categories", "ALL")
                    for category in categories:
                        self.combo_category.addItem(category, category)
                    index = self.combo_category.findData(selected_category)
                    self.combo_category.setCurrentIndex(index if index >= 0 else 0)
                    self.combo_category.blockSignals(False)
                items = list_inventory_items(
                    db,
                    search=self.txt_search.text(),
                    category=self.combo_category.currentData() or "ALL",
                    condition=self.combo_condition.currentText() if self.combo_condition.currentIndex() else "ALL",
                    location=self.txt_location.text().strip() or "ALL",
                    low_stock_only=self.chk_low_stock.isChecked(),
                    sort_by=self.combo_sort.currentData() or "name_asc",
                    actor_context=actor_context,
                )
                metrics = get_inventory_metrics(db, actor_context=actor_context)
                movements = get_inventory_movements(
                    db, limit=8, actor_context=actor_context
                )
                item_rows = [
                    {
                        "id": item.id,
                        "values": [
                            item.part_code,
                            item.part_name,
                            item.category,
                            item.appliance_source or "—",
                            " ".join(x for x in (item.brand, item.model_number) if x) or "—",
                            item.quantity_available,
                            item.reserved_quantity or 0,
                            (item.quantity_available or 0) - (item.reserved_quantity or 0),
                            item.unit,
                            item.total_weight,
                            item.weight_status or "Unverified",
                            item.condition,
                            item.cost_price,
                            item.estimated_selling_price,
                            " / ".join(
                                x for x in (
                                    item.storage_warehouse, item.storage_area, item.rack_number,
                                    item.shelf_number, item.bin_number,
                                ) if x
                            ) or item.storage_location or "Unassigned",
                            item.minimum_stock_level if item.minimum_stock_level is not None else "—",
                            "Inactive" if not item.is_active else (
                                "Out of stock" if (item.quantity_available or 0) - (item.reserved_quantity or 0) <= 0 else (
                                    "Low stock" if item.minimum_stock_level is not None and (item.quantity_available or 0) - (item.reserved_quantity or 0) <= item.minimum_stock_level else "In stock"
                                )
                            ),
                        ],
                        "low": item.is_active and item.minimum_stock_level is not None
                        and (item.quantity_available or 0) - (item.reserved_quantity or 0) <= item.minimum_stock_level,
                    }
                    for item in items
                ]
        except AuthorizationError as exc:
            self.table.setRowCount(0)
            self.table_recent.setRowCount(0)
            self.lbl_count.setText("Showing 0 inventory items")
            for value in self.metric_labels.values():
                value.setText("—")
            self.lbl_weight_note.clear()
            self.lbl_access_error.setText(
                f"Inventory is unavailable: {exc}"
            )
            self.lbl_access_error.show()
            self.combo_category.blockSignals(True)
            self.combo_category.clear()
            self.combo_category.addItem("All categories", "ALL")
            self.combo_category.blockSignals(False)
            self.btn_add.setEnabled(False)
            self.btn_export.setEnabled(False)
            self._selection_changed()
            return
        self.lbl_access_error.hide()
        self.btn_add.setEnabled(self._has_permission("inventory.manage"))
        self.btn_export.setEnabled(self._has_permission("inventory.view"))
        self.lbl_count.setText(f"Showing {len(item_rows)} inventory items")
        for key, value in metrics.items():
            if key not in self.metric_labels:
                continue
            if key.endswith("_value"):
                display = f"₹ {value:,.2f}"
            else:
                display = f"{value:,.2f}" if hasattr(value, "as_tuple") else str(value)
            self.metric_labels[key].setText(display)
        self.lbl_weight_note.setText(
            "Verified weight totals exclude unverified/partial stock weights. Selling values are estimates, not realized revenue."
            if metrics["has_unverified_weights"] else
            "Estimated selling values are not realized revenue."
        )
        self.table.setRowCount(len(item_rows))
        for row, item in enumerate(item_rows):
            code_item = QTableWidgetItem(str(item["values"][0]))
            code_item.setData(Qt.UserRole, item["id"])
            self.table.setItem(row, 0, code_item)
            for col, value in enumerate(item["values"][1:], start=1):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
            if item["low"]:
                for col in range(self.table.columnCount()):
                    self.table.item(row, col).setBackground(Qt.GlobalColor.yellow)
        self.table_recent.setRowCount(len(movements))
        for row, movement in enumerate(movements):
            part = movement.inventory_item
            values = [
                movement.id,
                movement.movement_date.strftime("%d-%b-%Y %H:%M") if movement.movement_date else "—",
                f"{part.part_code} — {part.part_name}" if part else "—",
                movement.movement_type,
                movement.quantity,
                f"{movement.weight} kg",
                f"{movement.reference_type} #{movement.reference_id or '—'}",
                movement.user_responsible or "—",
            ]
            for col, value in enumerate(values):
                self.table_recent.setItem(row, col, QTableWidgetItem(str(value)))
        self._selection_changed()

    def _selection_changed(self):
        selected = self.table.selectionModel().selectedRows()
        self.selected_item_id = None
        enabled = False
        if selected:
            code_item = self.table.item(selected[0].row(), 0)
            self.selected_item_id = code_item.data(Qt.UserRole) if code_item else None
            enabled = self.selected_item_id is not None
        self.btn_details.setEnabled(
            enabled and self._has_permission("inventory.view")
        )
        self.btn_edit_catalogue.setEnabled(
            enabled and self._has_permission("inventory.manage")
        )
        self.btn_record_movement.setEnabled(
            enabled and self._has_permission("inventory.adjust")
        )
        self.btn_deactivate.setEnabled(
            enabled and self._has_permission("inventory.manage")
        )

    def _add_item(self):
        dialog = InventoryItemDialog(self)
        dialog.item_saved.connect(lambda _: self.load_inventory())
        dialog.exec()

    def _edit_item(self):
        if not self.selected_item_id:
            return
        try:
            actor_context = current_actor_context()
            if actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                item = get_inventory_item(
                    db, self.selected_item_id, actor_context=actor_context
                )
        except AuthorizationError as exc:
            self.load_inventory()
            QMessageBox.warning(self, "Inventory Access Denied", str(exc))
            return
        if item:
            dialog = InventoryItemDialog(self, item=item)
            dialog.item_saved.connect(lambda _: self.load_inventory())
            dialog.exec()

    def _view_details(self):
        if self.selected_item_id:
            InventoryDetailDialog(self, self.selected_item_id).exec()

    def _record_movement(self):
        dialog = StockMovementDialog(self, self.selected_item_id or 0)
        dialog.movement_saved.connect(lambda _: self.load_inventory())
        dialog.exec()

    def _deactivate_item(self):
        if not self.selected_item_id:
            return
        confirmed = QMessageBox.question(
            self,
            "Deactivate Inventory Item",
            "Deactivate this catalogue item? Its history will be retained. Only zero-stock items can be deactivated.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                actor_context = current_actor_context()
                if actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                deactivate_inventory_item(
                    db, self.selected_item_id, actor_context=actor_context
                )
            self.load_inventory()
        except AuthorizationError as exc:
            self.load_inventory()
            QMessageBox.warning(self, "Inventory Access Denied", str(exc))
        except (InventoryValidationError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot Deactivate", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Deactivation Failed", str(exc))

    def _export_report(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Export Inventory Report")
        dialog.setText("Choose an inventory report format.")
        pdf_button = dialog.addButton("PDF", QMessageBox.ActionRole)
        excel_button = dialog.addButton("Excel (.xlsx)", QMessageBox.ActionRole)
        dialog.addButton(QMessageBox.Cancel)
        dialog.exec()
        if dialog.clickedButton() not in (pdf_button, excel_button):
            return
        is_pdf = dialog.clickedButton() == pdf_button
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Inventory Report",
            "Inventory_Report.pdf" if is_pdf else "Inventory_Report.xlsx",
            "PDF Files (*.pdf)" if is_pdf else "Excel Files (*.xlsx)",
        )
        if not file_path:
            return
        try:
            with get_db() as db:
                actor_context = current_actor_context()
                if actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                saved = (
                    generate_inventory_pdf_report(
                        db, file_path, actor_context=actor_context
                    )
                    if is_pdf else generate_inventory_excel_report(
                        db, file_path, actor_context=actor_context
                    )
                )
            QMessageBox.information(self, "Report Saved", f"Inventory report saved:\n\n{saved}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(saved))
        except AuthorizationError as exc:
            self.load_inventory()
            QMessageBox.warning(self, "Inventory Access Denied", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
