
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QFileDialog,
    QDoubleSpinBox,
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
    QTextEdit,
    QVBoxLayout,
)

from database.connection import get_db
from database.models import InventoryItem
from services.inventory_service import (
    InventoryValidationError,
    create_inventory_item,
    get_inventory_item,
    get_inventory_movements,
    get_inventory_source_references,
    list_inventory_items,
    record_stock_movement,
    transfer_inventory_location,
    update_inventory_item,
)
from services.security_service import AuthorizationError, current_actor_context, require_permission
from services.sales_order_service import get_inventory_sales_order_activity


class InventoryItemDialog(QDialog):
    item_saved = Signal(int)

    def __init__(self, parent=None, item: InventoryItem | None = None):
        super().__init__(parent)
        self.item_id = item.id if item else None
        self.setWindowTitle("Edit Inventory Item" if item else "Add Inventory Catalogue Item")
        self.resize(620, 720)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.txt_code = QLineEdit(item.part_code if item else "")
        self.txt_code.setReadOnly(True)
        form.addRow("Part ID:", self.txt_code)
        self.txt_name = QLineEdit(item.part_name if item else "")
        form.addRow("Component name *:", self.txt_name)
        self.combo_category = QComboBox()
        self.combo_category.setEditable(True)
        self.combo_category.addItems([
            "Compressors", "Electric Motors", "Copper Wiring & Harnesses",
            "Aluminium Heat Sinks & Condensers", "Steel & Iron Frames",
            "Printed Circuit Boards (PCBs)", "Plastics (ABS/HIPS)", "Fans & Blowers",
            "Cooling Coils & Tubes", "Recyclable Raw Materials", "Other",
        ])
        self.combo_category.setCurrentText(item.category if item else "")
        form.addRow("Category *:", self.combo_category)
        self.txt_source = QLineEdit(item.appliance_source or "" if item else "")
        form.addRow("Appliance source:", self.txt_source)
        self.txt_brand = QLineEdit(item.brand or "" if item else "")
        form.addRow("Brand:", self.txt_brand)
        self.txt_model = QLineEdit(item.model_number or "" if item else "")
        form.addRow("Model:", self.txt_model)
        self.combo_unit = QComboBox()
        self.combo_unit.setEditable(True)
        self.combo_unit.addItems(["pieces", "kg", "grams", "meters"])
        self.combo_unit.setCurrentText(item.unit if item else "pieces")
        form.addRow("Stock unit:", self.combo_unit)
        self.combo_condition = QComboBox()
        self.combo_condition.addItems(["Working", "Recyclable Raw Material", "Non-working", "Damaged"])
        self.combo_condition.setCurrentText(item.condition if item else "Working")
        form.addRow("Condition:", self.combo_condition)
        self.spin_cost = self._amount_box(item.cost_price if item else 0)
        form.addRow("Cost price per stock unit:", self.spin_cost)
        self.chk_cost_verified = QCheckBox("Verified cost basis for accounting")
        self.chk_cost_verified.setChecked(bool(item and item.cost_price_verified))
        form.addRow("Cost verification:", self.chk_cost_verified)
        self.spin_selling = self._amount_box(item.estimated_selling_price if item else 0)
        form.addRow("Estimated selling price per unit:", self.spin_selling)
        self.spin_minimum = self._amount_box(item.minimum_stock_level if item and item.minimum_stock_level is not None else 0)
        form.addRow("Minimum stock alert:", self.spin_minimum)
        self.txt_warehouse = QLineEdit(item.storage_warehouse or "" if item else "")
        form.addRow("Warehouse:", self.txt_warehouse)
        self.txt_area = QLineEdit(item.storage_area or "" if item else "")
        form.addRow("Storage area:", self.txt_area)
        self.txt_rack = QLineEdit(item.rack_number or "" if item else "")
        form.addRow("Rack:", self.txt_rack)
        self.txt_shelf = QLineEdit(item.shelf_number or "" if item else "")
        form.addRow("Shelf:", self.txt_shelf)
        self.txt_bin = QLineEdit(item.bin_number or "" if item else "")
        form.addRow("Bin:", self.txt_bin)
        self.txt_location = QLineEdit(item.storage_location or "" if item else "")
        form.addRow("Location label:", self.txt_location)

        photos_row = QHBoxLayout()
        self.txt_photos = QLineEdit(item.photographs or "" if item else "")
        self.txt_photos.setReadOnly(True)
        btn_photos = QPushButton("Choose photographs")
        btn_photos.clicked.connect(self._choose_photos)
        photos_row.addWidget(self.txt_photos)
        photos_row.addWidget(btn_photos)
        form.addRow("Photographs:", photos_row)
        self.txt_remarks = QTextEdit(item.remarks or "" if item else "")
        self.txt_remarks.setMaximumHeight(80)
        form.addRow("Remarks:", self.txt_remarks)
        layout.addLayout(form)

        self.lbl_note = QLabel(
            "New catalogue items start at zero stock. Use a recorded movement to add quantities."
        )
        self.lbl_note.setWordWrap(True)
        layout.addWidget(self.lbl_note)
        self.lbl_error = QLabel()
        self.lbl_error.setStyleSheet("color: #b91c1c;")
        self.lbl_error.setWordWrap(True)
        layout.addWidget(self.lbl_error)
        buttons = QHBoxLayout()
        buttons.addStretch()
        save = QPushButton("Save Item")
        self.btn_save = save
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("secondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.btn_save.setEnabled(
            self._has_permission("inventory.manage")
        )
        if not self.btn_save.isEnabled():
            self.lbl_error.setText(
                "Inventory catalogue management permission is required to save items."
            )

    @staticmethod
    def _amount_box(value) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0, 1000000000)
        box.setDecimals(2)
        box.setValue(float(value or 0))
        return box

    def _choose_photos(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Inventory Photographs", "", "Images (*.png *.jpg *.jpeg *.bmp);;All Files (*)"
        )
        if paths:
            self.txt_photos.setText(" | ".join(paths))

    def _payload(self) -> dict:
        return {
            "part_name": self.txt_name.text(),
            "category": self.combo_category.currentText(),
            "appliance_source": self.txt_source.text(),
            "brand": self.txt_brand.text(),
            "model_number": self.txt_model.text(),
            "unit": self.combo_unit.currentText(),
            "condition": self.combo_condition.currentText(),
            "cost_price": self.spin_cost.value(),
            "cost_price_verified": self.chk_cost_verified.isChecked(),
            "estimated_selling_price": self.spin_selling.value(),
            "minimum_stock_level": self.spin_minimum.value(),
            "storage_warehouse": self.txt_warehouse.text(),
            "storage_area": self.txt_area.text(),
            "rack_number": self.txt_rack.text(),
            "shelf_number": self.txt_shelf.text(),
            "bin_number": self.txt_bin.text(),
            "storage_location": self.txt_location.text(),
            "photographs": self.txt_photos.text(),
            "remarks": self.txt_remarks.toPlainText(),
        }

    def _save(self):
        actor_context = current_actor_context()
        if actor_context is None:
            self.lbl_error.setText("Sign in is required to save inventory items.")
            return
        payload = self._payload()
        try:
            with get_db() as db:
                item = (
                    update_inventory_item(
                        db, self.item_id, payload, actor_context=actor_context
                    )
                    if self.item_id
                    else create_inventory_item(
                        db, payload, actor_context=actor_context
                    )
                )
                item_id = item.id
            self.item_saved.emit(item_id)
            self.accept()
        except AuthorizationError as exc:
            self.btn_save.setEnabled(False)
            self.lbl_error.setText(f"Inventory access denied: {exc}")
        except (InventoryValidationError, ValueError) as exc:
            self.lbl_error.setText(str(exc))
        except Exception as exc:
            self.lbl_error.setText(f"Could not save inventory item: {exc}")

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


class StockMovementDialog(QDialog):
    movement_saved = Signal(int)

    def __init__(self, parent=None, inventory_item_id: int = 0):
        super().__init__(parent)
        self.item_id = inventory_item_id
        self.setWindowTitle("Record Stock Movement")
        self.setModal(True)
        self.resize(560, 520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        actor_context = current_actor_context()
        item_choices = []
        if actor_context is not None:
            try:
                with get_db() as db:
                    items = list_inventory_items(
                        db, actor_context=actor_context
                    )
                    item_choices = [
                        (i.id, f"{i.part_code} — {i.part_name} ({i.quantity_available} {i.unit})")
                        for i in items
                    ]
            except AuthorizationError as exc:
                self._inventory_access_error = str(exc)
        else:
            self._inventory_access_error = "Sign in is required to view inventory."
        self.combo_item = QComboBox()
        for item_id, title in item_choices:
            self.combo_item.addItem(title, item_id)
        index = self.combo_item.findData(inventory_item_id)
        if index >= 0:
            self.combo_item.setCurrentIndex(index)
        form.addRow("Inventory item:", self.combo_item)
        self.combo_type = QComboBox()
        self.combo_type.addItem("Issue stock", "ISSUE")
        self.combo_type.addItem("Adjustment increase", "ADJUSTMENT_IN")
        self.combo_type.addItem("Adjustment decrease", "ADJUSTMENT_OUT")
        self.combo_type.addItem("Stock return", "RETURN")
        self.combo_type.addItem("Damaged stock removed", "DAMAGE")
        self.combo_type.addItem("Missing stock", "MISSING")
        self.combo_type.addItem("Storage transfer", "TRANSFER")
        self.combo_type.currentIndexChanged.connect(self._toggle_transfer_fields)
        form.addRow("Movement type:", self.combo_type)
        self.spin_quantity = self._amount_box()
        form.addRow("Quantity:", self.spin_quantity)
        self.spin_weight = self._amount_box()
        form.addRow("Weight (kg):", self.spin_weight)
        self.chk_weight_verified = QPushButton("Weight is verified: No")
        self.chk_weight_verified.setCheckable(True)
        self.chk_weight_verified.toggled.connect(
            lambda checked: self.chk_weight_verified.setText(f"Weight is verified: {'Yes' if checked else 'No'}")
        )
        form.addRow("Weight status:", self.chk_weight_verified)
        self.txt_to_location = QLineEdit()
        form.addRow("Destination location:", self.txt_to_location)
        self.txt_warehouse = QLineEdit()
        form.addRow("Destination warehouse:", self.txt_warehouse)
        self.txt_area = QLineEdit()
        form.addRow("Destination area:", self.txt_area)
        self.txt_rack = QLineEdit()
        form.addRow("Destination rack:", self.txt_rack)
        self.txt_shelf = QLineEdit()
        form.addRow("Destination shelf:", self.txt_shelf)
        self.txt_bin = QLineEdit()
        form.addRow("Destination bin:", self.txt_bin)
        self.txt_reason = QTextEdit()
        self.txt_reason.setMaximumHeight(80)
        form.addRow("Reason *:", self.txt_reason)
        self.txt_user = QLineEdit()
        self.txt_user.setPlaceholderText("Name of responsible user")
        form.addRow("Responsible user *:", self.txt_user)
        self.txt_reference = QLineEdit("MANUAL")
        form.addRow("Reference type:", self.txt_reference)
        self.txt_reference_id = QLineEdit()
        self.txt_reference_id.setPlaceholderText("Optional numeric reference")
        form.addRow("Reference ID:", self.txt_reference_id)
        layout.addLayout(form)
        self.lbl_error = QLabel()
        self.lbl_error.setStyleSheet("color: #b91c1c;")
        self.lbl_error.setWordWrap(True)
        layout.addWidget(self.lbl_error)
        buttons = QHBoxLayout()
        buttons.addStretch()
        save = QPushButton("Confirm & Record Movement")
        self.btn_save = save
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("secondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.btn_save.setEnabled(
            bool(item_choices) and self._has_permission("inventory.adjust")
        )
        if not item_choices or not self.btn_save.isEnabled():
            self.lbl_error.setText(
                getattr(
                    self,
                    "_inventory_access_error",
                    "Inventory adjustment permission is required to record movements.",
                )
            )
        self._toggle_transfer_fields()

    @staticmethod
    def _amount_box() -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0, 1000000000)
        box.setDecimals(2)
        return box

    def _toggle_transfer_fields(self):
        is_transfer = self.combo_type.currentData() == "TRANSFER"
        for widget in (
            self.spin_quantity,
            self.spin_weight,
            self.chk_weight_verified,
            self.txt_reference,
            self.txt_reference_id,
        ):
            widget.setEnabled(not is_transfer)
        for widget in (
            self.txt_to_location,
            self.txt_warehouse,
            self.txt_area,
            self.txt_rack,
            self.txt_shelf,
            self.txt_bin,
        ):
            widget.setEnabled(is_transfer)

    def _save(self):
        actor_context = current_actor_context()
        if actor_context is None:
            self.lbl_error.setText("Sign in is required to change inventory.")
            return
        item_id = self.combo_item.currentData()
        if not item_id:
            self.lbl_error.setText("Create a zero-stock inventory catalogue item before recording stock.")
            return
        reason = self.txt_reason.toPlainText().strip()
        user = self.txt_user.text().strip()
        if not reason or not user:
            self.lbl_error.setText("A reason and responsible user are required.")
            return
        type_code = self.combo_type.currentData()
        confirm = QMessageBox.question(
            self,
            "Confirm Stock Movement",
            f"Record {self.combo_type.currentText()} for the selected item?\n\nReason: {reason}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                if type_code == "TRANSFER":
                    movement = transfer_inventory_location(
                        db,
                        item_id,
                        {
                            "storage_location": self.txt_to_location.text(),
                            "storage_warehouse": self.txt_warehouse.text(),
                            "storage_area": self.txt_area.text(),
                            "rack_number": self.txt_rack.text(),
                            "shelf_number": self.txt_shelf.text(),
                            "bin_number": self.txt_bin.text(),
                        },
                        reason=reason,
                        user_responsible=user,
                        actor_context=actor_context,
                    )
                else:
                    ref_text = self.txt_reference_id.text().strip()
                    movement = record_stock_movement(
                        db,
                        item_id,
                        type_code,
                        self.spin_quantity.value(),
                        self.spin_weight.value(),
                        reason=reason,
                        user_responsible=user,
                        reference_type=self.txt_reference.text().strip() or "MANUAL",
                        reference_id=int(ref_text) if ref_text else None,
                        weight_verified=self.chk_weight_verified.isChecked(),
                        actor_context=actor_context,
                    )
                movement_id = movement.id
            self.movement_saved.emit(movement_id)
            self.accept()
        except AuthorizationError as exc:
            self.btn_save.setEnabled(False)
            self.combo_item.clear()
            self.lbl_error.setText(f"Inventory access denied: {exc}")
        except (ValueError, TypeError) as exc:
            self.lbl_error.setText(str(exc))
        except Exception as exc:
            self.lbl_error.setText(f"Could not record stock movement: {exc}")

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


class InventoryDetailDialog(QDialog):
    def __init__(self, parent=None, item_id: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Inventory Item Details")
        self.resize(920, 680)
        layout = QVBoxLayout(self)
        actor_context = current_actor_context()
        if actor_context is None:
            layout.addWidget(QLabel("Sign in with inventory-view permission to view this item."))
            return
        try:
            with get_db() as db:
                item = get_inventory_item(
                    db, item_id, actor_context=actor_context
                )
                if not item:
                    layout.addWidget(QLabel("Inventory item was not found."))
                    return
                item_data = {
                    "part_code": item.part_code,
                    "part_name": item.part_name,
                    "category": item.category,
                    "appliance_source": item.appliance_source,
                    "brand": item.brand,
                    "model_number": item.model_number,
                    "quantity_available": item.quantity_available,
                    "reserved_quantity": item.reserved_quantity or 0,
                    "available_quantity": (item.quantity_available or 0) - (item.reserved_quantity or 0),
                    "unit": item.unit,
                    "total_weight": item.total_weight,
                    "weight_status": item.weight_status or "Unverified",
                    "condition": item.condition,
                    "cost_price": item.cost_price,
                    "cost_price_verified": item.cost_price_verified,
                    "estimated_selling_price": item.estimated_selling_price,
                    "storage_location": item.storage_location,
                    "storage_warehouse": item.storage_warehouse,
                    "storage_area": item.storage_area,
                    "rack_number": item.rack_number,
                    "shelf_number": item.shelf_number,
                    "bin_number": item.bin_number,
                    "minimum_stock_level": item.minimum_stock_level,
                    "photographs": item.photographs,
                    "remarks": item.remarks,
                }
                movements = get_inventory_movements(
                    db, item_id, actor_context=actor_context
                )
                sources = get_inventory_source_references(
                    db, item_id, actor_context=actor_context
                )
                sales_activity = get_inventory_sales_order_activity(
                    db, item_id, actor_context=actor_context
                )
        except AuthorizationError as exc:
            layout.addWidget(QLabel(f"Inventory is unavailable: {exc}"))
            return

        heading = QLabel(f"{item_data['part_code']} — {item_data['part_name']}")
        heading.setStyleSheet("font-size: 20px; font-weight: 700; color: #0f172a;")
        layout.addWidget(heading)
        group = QGroupBox("Item Profile")
        grid = QGridLayout(group)
        fields = [
            ("Category", item_data["category"]), ("Appliance source", item_data["appliance_source"]),
            ("Brand / model", " ".join(x for x in (item_data["brand"], item_data["model_number"]) if x) or "—"),
            ("Physical stock", f"{item_data['quantity_available']} {item_data['unit']}"),
            ("Reserved stock", f"{item_data['reserved_quantity']} {item_data['unit']}"),
            ("Available stock", f"{item_data['available_quantity']} {item_data['unit']}"),
            ("Weight", f"{item_data['total_weight']} kg ({item_data['weight_status']})"),
            ("Condition", item_data["condition"]),
            ("Cost price", str(item_data["cost_price"])),
            ("Accounting cost verification", "Verified" if item_data["cost_price_verified"] else "Pending review"),
            ("Estimated selling price", str(item_data["estimated_selling_price"])),
            ("Minimum stock", str(item_data["minimum_stock_level"] or 0)),
            ("Warehouse", item_data["storage_warehouse"]), ("Area", item_data["storage_area"]),
            ("Rack / shelf / bin", " / ".join(x for x in (item_data["rack_number"], item_data["shelf_number"], item_data["bin_number"]) if x) or "—"),
            ("Location", item_data["storage_location"]),
            ("Photographs", item_data["photographs"]), ("Remarks", item_data["remarks"]),
        ]
        for idx, (label, value) in enumerate(fields):
            grid.addWidget(QLabel(f"<b>{label}:</b>"), idx // 2, (idx % 2) * 2)
            value_label = QLabel(str(value or "—"))
            value_label.setWordWrap(True)
            grid.addWidget(value_label, idx // 2, (idx % 2) * 2 + 1)
        layout.addWidget(group)

        source_group = QGroupBox("Approved Dismantling Source References")
        source_layout = QVBoxLayout(source_group)
        if sources:
            for source in sources:
                source_layout.addWidget(QLabel(
                    f"{source['dismantling_code']} ← {source['purchase_code']} "
                    f"({source['supplier_name']}) — {source['appliance_category']} "
                    f"{source['brand'] or ''} {source['model_number'] or ''}"
                ))
        else:
            source_layout.addWidget(QLabel("No dismantling source movements recorded."))
        layout.addWidget(source_group)

        history = QTableWidget(len(movements), 10)
        history.setHorizontalHeaderLabels([
            "Movement", "Type", "Quantity", "Weight kg", "Previous", "New",
            "Date", "Reference", "Reason", "User",
        ])
        history.horizontalHeader().setSectionResizeMode(8, QHeaderView.Stretch)
        for row, movement in enumerate(movements):
            values = [
                movement.id, movement.movement_type, movement.quantity, movement.weight,
                movement.previous_quantity if movement.previous_quantity is not None else "—",
                movement.new_quantity if movement.new_quantity is not None else "—",
                movement.movement_date.strftime("%d-%b-%Y %H:%M") if movement.movement_date else "—",
                f"{movement.reference_type} #{movement.reference_id or '—'}",
                movement.reason or movement.remarks or "—",
                movement.user_responsible or "—",
            ]
            for col, value in enumerate(values):
                history.setItem(row, col, QTableWidgetItem(str(value)))
        history.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(history)
        reservation_group = QGroupBox("Sales Order Reservation History")
        reservation_layout = QVBoxLayout(reservation_group)
        if sales_activity["reservation_history"]:
            for event in sales_activity["reservation_history"]:
                order_code = event.sales_order.order_code if event.sales_order else "Order unavailable"
                reservation_layout.addWidget(QLabel(
                    f"{event.created_at:%d-%b-%Y %H:%M} · {event.event_type} "
                    f"{event.quantity} {event.unit_snapshot} · {order_code} · {event.reason or ''}"
                ))
        else:
            reservation_layout.addWidget(QLabel("No sales order reservations recorded."))
        reservation_layout.addWidget(QLabel("Dispatch history"))
        if sales_activity["dispatch_movements"]:
            for movement in sales_activity["dispatch_movements"]:
                reservation_layout.addWidget(QLabel(
                    f"{movement.movement_date:%d-%b-%Y %H:%M} · {movement.quantity} "
                    f"{item_data['unit']} · fulfilment #{movement.reference_id or '—'}"
                ))
        else:
            reservation_layout.addWidget(QLabel("No sales order dispatches recorded."))
        layout.addWidget(reservation_group)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)
