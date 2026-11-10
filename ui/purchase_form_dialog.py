from datetime import date
from decimal import Decimal
from PySide6.QtCore import Qt, Signal, QDate
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QTextEdit, QDoubleSpinBox, QSpinBox, QComboBox, QDateEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QGroupBox, QScrollArea, QWidget, QMessageBox, QFrame, QGridLayout
)
from database.connection import get_db
from database.models import Supplier
from services.purchase_service import (
    APPLIANCE_CATEGORIES,
    PAYMENT_METHODS,
    PurchaseValidationError,
    generate_next_purchase_code,
    create_purchase,
    calculate_line_total,
)
from services.supplier_service import list_suppliers
from services.security_service import (
    AuthorizationError,
    current_actor_context,
    require_permission,
)
from utils.validators import to_decimal, format_currency


class PurchaseFormDialog(QDialog):
    """Dialog for entering a new purchase order with multiple appliance line items and additional charges."""
    purchase_saved = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Record Inward Appliance Purchase")
        self.resize(960, 780)
        self.setMinimumSize(850, 650)
        self.setModal(True)

        self.line_items: list[dict] = []
        self.active_suppliers: list[Supplier] = []

        self._init_ui()
        self._load_initial_data()

    def _init_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Dialog Header
        header = QFrame()
        header.setObjectName("dialogHeader")
        header_l = QVBoxLayout(header)
        header_l.setContentsMargins(24, 18, 24, 18)

        title = QLabel("New Inward Appliance Purchase")
        title.setObjectName("dialogTitle")
        desc = QLabel(
            "Record raw incoming appliance procurement, calculate freight & handling expenses, and register inward payment status."
        )
        desc.setObjectName("dialogDesc")
        desc.setWordWrap(True)

        header_l.addWidget(title)
        header_l.addWidget(desc)
        root_layout.addWidget(header)

        # 2. Scrollable Body
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("formScrollArea")

        container = QWidget()
        container.setObjectName("formContainer")
        form_layout = QVBoxLayout(container)
        form_layout.setContentsMargins(24, 16, 24, 16)
        form_layout.setSpacing(16)

        # Section A: Header Details & Supplier Selection
        group_meta = QGroupBox("Purchase Header & Supplier Information")
        grid_meta = QGridLayout(group_meta)
        grid_meta.setContentsMargins(16, 16, 16, 16)
        grid_meta.setSpacing(12)

        # Purchase ID
        grid_meta.addWidget(QLabel("Purchase ID:"), 0, 0)
        self.txt_code = QLineEdit()
        self.txt_code.setReadOnly(True)
        self.txt_code.setObjectName("readOnlyField")
        grid_meta.addWidget(self.txt_code, 0, 1)

        # Purchase Date
        grid_meta.addWidget(QLabel("Purchase Date:"), 0, 2)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setDisplayFormat("dd-MM-yyyy")
        grid_meta.addWidget(self.date_edit, 0, 3)

        # Supplier Dropdown
        grid_meta.addWidget(QLabel("Select Supplier *:"), 1, 0)
        self.combo_supplier = QComboBox()
        self.combo_supplier.currentIndexChanged.connect(self._on_supplier_selected)
        grid_meta.addWidget(self.combo_supplier, 1, 1, 1, 3)

        # Supplier Preview Card
        self.supplier_info_box = QFrame()
        self.supplier_info_box.setObjectName("noticeBox")
        box_l = QVBoxLayout(self.supplier_info_box)
        box_l.setContentsMargins(12, 8, 12, 8)
        self.lbl_supplier_details = QLabel("Select an active supplier to view contact and address details.")
        self.lbl_supplier_details.setObjectName("noticeText")
        self.lbl_supplier_details.setWordWrap(True)
        box_l.addWidget(self.lbl_supplier_details)
        grid_meta.addWidget(self.supplier_info_box, 2, 0, 1, 4)

        form_layout.addWidget(group_meta)

        # Section B: Multiple Appliance Items
        group_items = QGroupBox("Appliance Line Items (Raw Incoming Scrap)")
        items_layout = QVBoxLayout(group_items)
        items_layout.setContentsMargins(16, 16, 16, 16)
        items_layout.setSpacing(12)

        # Inline Line Item Entry Form
        item_entry_frame = QFrame()
        item_entry_frame.setStyleSheet("background-color: #f1f5f9; border-radius: 6px; padding: 6px;")
        grid_item = QGridLayout(item_entry_frame)
        grid_item.setContentsMargins(10, 8, 10, 8)
        grid_item.setSpacing(8)

        grid_item.addWidget(QLabel("Category *:"), 0, 0)
        self.combo_item_category = QComboBox()
        for cat in APPLIANCE_CATEGORIES:
            self.combo_item_category.addItem(cat)
        grid_item.addWidget(self.combo_item_category, 0, 1)

        grid_item.addWidget(QLabel("Brand:"), 0, 2)
        self.txt_item_brand = QLineEdit()
        self.txt_item_brand.setPlaceholderText("e.g. LG, Samsung")
        grid_item.addWidget(self.txt_item_brand, 0, 3)

        grid_item.addWidget(QLabel("Model No:"), 0, 4)
        self.txt_item_model = QLineEdit()
        self.txt_item_model.setPlaceholderText("Optional model")
        grid_item.addWidget(self.txt_item_model, 0, 5)

        grid_item.addWidget(QLabel("Quantity *:"), 1, 0)
        self.spin_item_qty = QSpinBox()
        self.spin_item_qty.setRange(1, 100000)
        self.spin_item_qty.setValue(1)
        grid_item.addWidget(self.spin_item_qty, 1, 1)

        grid_item.addWidget(QLabel("Unit:"), 1, 2)
        self.combo_item_unit = QComboBox()
        self.combo_item_unit.addItem("pieces", "pieces")
        self.combo_item_unit.addItem("kg", "kg")
        self.combo_item_unit.currentIndexChanged.connect(self._on_unit_changed)
        grid_item.addWidget(self.combo_item_unit, 1, 3)

        grid_item.addWidget(QLabel("Weight (kg):"), 1, 4)
        self.spin_item_weight = QDoubleSpinBox()
        self.spin_item_weight.setRange(0.0, 1000000.0)
        self.spin_item_weight.setDecimals(2)
        self.spin_item_weight.setValue(0.0)
        grid_item.addWidget(self.spin_item_weight, 1, 5)

        grid_item.addWidget(QLabel("Rate (₹) *:"), 2, 0)
        self.spin_item_rate = QDoubleSpinBox()
        self.spin_item_rate.setRange(0.0, 10000000.0)
        self.spin_item_rate.setDecimals(2)
        self.spin_item_rate.setPrefix("₹ ")
        self.spin_item_rate.setValue(0.0)
        grid_item.addWidget(self.spin_item_rate, 2, 1)

        self.btn_add_item = QPushButton("+ Add Line Item")
        self.btn_add_item.setObjectName("primaryButton")
        self.btn_add_item.clicked.connect(self._add_line_item)
        grid_item.addWidget(self.btn_add_item, 2, 4, 1, 2)

        items_layout.addWidget(item_entry_frame)

        # Table of added items
        self.table_items = QTableWidget(0, 8)
        self.table_items.setHorizontalHeaderLabels([
            "Category", "Brand", "Model", "Qty", "Unit", "Weight (kg)", "Rate (₹)", "Line Total (₹)"
        ])
        h = self.table_items.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self.table_items.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_items.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_items.setFixedHeight(180)
        self.table_items.setObjectName("styledTable")
        items_layout.addWidget(self.table_items)

        # Button to delete selected row
        btn_del_row = QPushButton("🗑 Remove Selected Item")
        btn_del_row.setObjectName("secondaryButton")
        btn_del_row.clicked.connect(self._remove_selected_item)
        items_layout.addWidget(btn_del_row, alignment=Qt.AlignRight)

        form_layout.addWidget(group_items)

        # Section C: Additional Charges & Financial Totals
        group_charges = QGroupBox("Additional Incurred Costs & Financial Settlement")
        grid_fin = QGridLayout(group_charges)
        grid_fin.setContentsMargins(16, 16, 16, 16)
        grid_fin.setSpacing(12)

        # Transportation
        grid_fin.addWidget(QLabel("Transportation Charges (₹):"), 0, 0)
        self.spin_trans = self._create_currency_spinbox()
        grid_fin.addWidget(self.spin_trans, 0, 1)

        # Loading
        grid_fin.addWidget(QLabel("Loading Charges (₹):"), 0, 2)
        self.spin_load = self._create_currency_spinbox()
        grid_fin.addWidget(self.spin_load, 0, 3)

        # Unloading
        grid_fin.addWidget(QLabel("Unloading Charges (₹):"), 1, 0)
        self.spin_unload = self._create_currency_spinbox()
        grid_fin.addWidget(self.spin_unload, 1, 1)

        # Other expenses
        grid_fin.addWidget(QLabel("Other Incurred Expenses (₹):"), 1, 2)
        self.spin_other = self._create_currency_spinbox()
        grid_fin.addWidget(self.spin_other, 1, 3)

        # Subtotal display
        grid_fin.addWidget(QLabel("Appliance Subtotal:"), 2, 0)
        self.lbl_subtotal = QLabel("₹ 0.00")
        self.lbl_subtotal.setObjectName("metricValue")
        grid_fin.addWidget(self.lbl_subtotal, 2, 1)

        # Total cost display
        grid_fin.addWidget(QLabel("Total Purchase Cost:"), 2, 2)
        self.lbl_total_cost = QLabel("₹ 0.00")
        self.lbl_total_cost.setObjectName("metricValuePrimary")
        grid_fin.addWidget(self.lbl_total_cost, 2, 3)

        # Payment Method
        grid_fin.addWidget(QLabel("Payment Method:"), 3, 0)
        self.combo_payment_method = QComboBox()
        for method in PAYMENT_METHODS:
            self.combo_payment_method.addItem(method)
        grid_fin.addWidget(self.combo_payment_method, 3, 1)

        # Amount Paid
        grid_fin.addWidget(QLabel("Amount Paid (₹):"), 3, 2)
        self.spin_amount_paid = self._create_currency_spinbox()
        grid_fin.addWidget(self.spin_amount_paid, 3, 3)

        # Remaining Balance
        grid_fin.addWidget(QLabel("Remaining Balance:"), 4, 0)
        self.lbl_balance = QLabel("₹ 0.00")
        self.lbl_balance.setObjectName("metricValue")
        grid_fin.addWidget(self.lbl_balance, 4, 1)

        # Payment Status Badge
        grid_fin.addWidget(QLabel("Payment Status:"), 4, 2)
        self.lbl_status_badge = QLabel(" Unpaid ")
        self.lbl_status_badge.setAlignment(Qt.AlignCenter)
        self.lbl_status_badge.setStyleSheet(
            "background: #fee2e2; color: #b91c1c; border-radius: 4px; font-weight: 700; padding: 4px 10px;"
        )
        grid_fin.addWidget(self.lbl_status_badge, 4, 3)

        form_layout.addWidget(group_charges)

        # Section D: Remarks & Regulatory Compliance Notice
        group_rem = QGroupBox("Remarks & Inventory Notice")
        v_rem = QVBoxLayout(group_rem)
        v_rem.setContentsMargins(16, 12, 16, 16)
        v_rem.setSpacing(8)

        self.txt_remarks = QTextEdit()
        self.txt_remarks.setPlaceholderText("Enter inspection notes, truck vehicle number, weighbridge ticket ID...")
        self.txt_remarks.setFixedHeight(65)
        v_rem.addWidget(self.txt_remarks)

        lbl_inventory_notice = QLabel(
            "🛡 Environmental Factory SOP: Incoming appliances are recorded strictly as raw scrap stock. "
            "Individual copper, steel, aluminium, and PCB parts are not entered into active stock until "
            "processed through the Dismantling & Recovery module."
        )
        lbl_inventory_notice.setObjectName("noticeText")
        lbl_inventory_notice.setWordWrap(True)
        v_rem.addWidget(lbl_inventory_notice)

        form_layout.addWidget(group_rem)

        scroll.setWidget(container)
        root_layout.addWidget(scroll)

        # Error notification banner
        self.lbl_error = QLabel()
        self.lbl_error.setObjectName("errorBanner")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setVisible(False)
        root_layout.addWidget(self.lbl_error)

        # Footer Actions
        footer = QFrame()
        footer.setObjectName("dialogFooter")
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(24, 16, 24, 16)
        f_layout.setSpacing(12)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("secondaryButton")
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_save = QPushButton("Save Purchase Order")
        self.btn_save.setObjectName("primaryButton")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._save_purchase)

        f_layout.addStretch()
        f_layout.addWidget(self.btn_cancel)
        f_layout.addWidget(self.btn_save)
        root_layout.addWidget(footer)

        # Connect charges triggers for live calculation
        for spin in [self.spin_trans, self.spin_load, self.spin_unload, self.spin_other, self.spin_amount_paid]:
            spin.valueChanged.connect(self._recalculate_totals)

    def _create_currency_spinbox(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 100000000.0)
        spin.setDecimals(2)
        spin.setPrefix("₹ ")
        spin.setValue(0.0)
        return spin

    def _on_unit_changed(self):
        unit = self.combo_item_unit.currentData() or self.combo_item_unit.currentText()
        if unit == "kg":
            self.spin_item_rate.setPrefix("₹ /kg ")
        else:
            self.spin_item_rate.setPrefix("₹ /pc ")

    def _load_initial_data(self):
        with get_db() as db:
            actor_context = current_actor_context()
            try:
                require_permission(
                    actor_context, "purchases.post", db_session=db
                )
            except AuthorizationError:
                self.txt_code.clear()
                self.combo_supplier.clear()
                self.btn_save.setEnabled(False)
                self._show_error(
                    "Sign in with purchase-post permission to record purchases."
                )
                return

            try:
                # The purchase form depends on both purchase and supplier-view access.
                self.active_suppliers = list_suppliers(
                    db,
                    status_filter="ACTIVE",
                    sort_by="name_asc",
                    actor_context=actor_context,
                )
            except AuthorizationError:
                self.active_suppliers = []
                self.txt_code.clear()
                self.btn_save.setEnabled(False)
                self._show_error(
                    "Supplier-view permission is required to select a supplier."
                )
                return
            self.txt_code.setText(
                generate_next_purchase_code(db, actor_context=actor_context)
            )
            self.combo_supplier.clear()
            self.combo_supplier.addItem("— Select Active Supplier —", None)
            for s in self.active_suppliers:
                self.combo_supplier.addItem(f"{s.business_name} ({s.supplier_code})", s.id)

    def _on_supplier_selected(self):
        sup_id = self.combo_supplier.currentData()
        if not sup_id:
            self.lbl_supplier_details.setText("Select an active supplier to view contact and address details.")
            return

        with get_db() as db:
            s = db.get(Supplier, sup_id)
            if s:
                details = (
                    f"<b>Contact Person:</b> {s.contact_person or '—'}  |  "
                    f"<b>Phone:</b> {s.phone or '—'}  |  "
                    f"<b>GSTIN:</b> {s.gstin or '—'}<br/>"
                    f"<b>Address:</b> {s.address or '—'}, {s.city or ''} {s.state or ''} {s.pin_code or ''}"
                )
                self.lbl_supplier_details.setText(details)

    def _add_line_item(self):
        category = self.combo_item_category.currentText()
        brand = self.txt_item_brand.text().strip()
        model = self.txt_item_model.text().strip()
        qty = self.spin_item_qty.value()
        unit = self.combo_item_unit.currentData() or self.combo_item_unit.currentText()
        weight = self.spin_item_weight.value()
        rate = self.spin_item_rate.value()

        if qty <= 0:
            self._show_error("Quantity must be greater than 0.")
            return
        if rate <= 0.0:
            self._show_error("Rate per unit/kg must be greater than zero.")
            return

        line_tot = calculate_line_total(qty, to_decimal(weight), unit, to_decimal(rate))

        item_dict = {
            "appliance_category": category,
            "brand": brand or None,
            "model_number": model or None,
            "quantity": qty,
            "unit": unit,
            "weight": to_decimal(weight),
            "rate": to_decimal(rate),
            "line_total": line_tot,
        }
        self.line_items.append(item_dict)

        # Clear fields for next entry
        self.txt_item_brand.clear()
        self.txt_item_model.clear()
        self.spin_item_qty.setValue(1)
        self.spin_item_weight.setValue(0.0)
        self.spin_item_rate.setValue(0.0)
        self._clear_error()

        self._refresh_items_table()
        self._recalculate_totals()

    def _remove_selected_item(self):
        selected = self.table_items.selectionModel().selectedRows()
        if not selected:
            return
        row = selected[0].row()
        if 0 <= row < len(self.line_items):
            self.line_items.pop(row)
            self._refresh_items_table()
            self._recalculate_totals()

    def _refresh_items_table(self):
        self.table_items.setRowCount(len(self.line_items))
        for r, item in enumerate(self.line_items):
            self.table_items.setItem(r, 0, QTableWidgetItem(item["appliance_category"]))
            self.table_items.setItem(r, 1, QTableWidgetItem(item["brand"] or "—"))
            self.table_items.setItem(r, 2, QTableWidgetItem(item["model_number"] or "—"))
            self.table_items.setItem(r, 3, QTableWidgetItem(str(item["quantity"])))
            self.table_items.setItem(r, 4, QTableWidgetItem(item["unit"]))
            self.table_items.setItem(r, 5, QTableWidgetItem(f"{item['weight']:.2f}" if item["weight"] else "—"))
            self.table_items.setItem(r, 6, QTableWidgetItem(f"{item['rate']:.2f}"))
            self.table_items.setItem(r, 7, QTableWidgetItem(f"{item['line_total']:.2f}"))

    def _recalculate_totals(self):
        subtotal = sum((item["line_total"] for item in self.line_items), Decimal("0.00"))
        trans = to_decimal(self.spin_trans.value())
        load = to_decimal(self.spin_load.value())
        unload = to_decimal(self.spin_unload.value())
        other = to_decimal(self.spin_other.value())

        total = subtotal + trans + load + unload + other
        paid = to_decimal(self.spin_amount_paid.value())

        # Update remaining balance
        bal = total - paid

        self.lbl_subtotal.setText(format_currency(subtotal))
        self.lbl_total_cost.setText(format_currency(total))
        self.lbl_balance.setText(format_currency(bal))

        # Update Status Badge
        if paid <= Decimal("0.00"):
            self.lbl_status_badge.setText(" Unpaid ")
            self.lbl_status_badge.setStyleSheet(
                "background: #fee2e2; color: #b91c1c; border-radius: 4px; font-weight: 700; padding: 4px 10px;"
            )
        elif paid >= total and total > Decimal("0.00"):
            self.lbl_status_badge.setText(" Paid ")
            self.lbl_status_badge.setStyleSheet(
                "background: #dcfce7; color: #15803d; border-radius: 4px; font-weight: 700; padding: 4px 10px;"
            )
        else:
            self.lbl_status_badge.setText(" Partially Paid ")
            self.lbl_status_badge.setStyleSheet(
                "background: #fef3c7; color: #b45309; border-radius: 4px; font-weight: 700; padding: 4px 10px;"
            )

    def _show_error(self, message: str):
        self.lbl_error.setText(f"⚠ {message}")
        self.lbl_error.setVisible(True)

    def _clear_error(self):
        self.lbl_error.setText("")
        self.lbl_error.setVisible(False)

    def _save_purchase(self):
        self._clear_error()

        supplier_id = self.combo_supplier.currentData()
        if not supplier_id:
            self._show_error("Please select a valid registered active supplier.")
            return

        if len(self.line_items) == 0:
            self._show_error("Please add at least one appliance line item to this purchase.")
            return

        qdate = self.date_edit.date()
        py_date = date(qdate.year(), qdate.month(), qdate.day())

        header_payload = {
            "purchase_code": self.txt_code.text().strip(),
            "supplier_id": supplier_id,
            "purchase_date": py_date,
            "transportation_charges": self.spin_trans.value(),
            "loading_charges": self.spin_load.value(),
            "unloading_charges": self.spin_unload.value(),
            "other_charges": self.spin_other.value(),
            "amount_paid": self.spin_amount_paid.value(),
            "payment_method": self.combo_payment_method.currentText(),
            "remarks": self.txt_remarks.toPlainText().strip(),
        }

        try:
            actor_context = current_actor_context()
            with get_db() as db:
                purchase = create_purchase(
                    db,
                    header_payload,
                    self.line_items,
                    actor_context=actor_context,
                )
                purchase_id = purchase.id
                code = purchase.purchase_code

            QMessageBox.information(
                self,
                "Purchase Recorded",
                f"Inward Purchase {code} has been successfully saved to the ledger."
            )
            self.purchase_saved.emit(purchase_id)
            self.accept()

        except AuthorizationError:
            self._show_error(
                "Sign in with purchase-post and supplier-view permissions to record purchases."
            )
        except PurchaseValidationError as e:
            self._show_error(str(e))
        except Exception as e:
            self._show_error(f"Database error: {str(e)}")
