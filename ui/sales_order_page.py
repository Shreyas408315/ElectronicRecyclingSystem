from datetime import date
from decimal import Decimal
from uuid import uuid4

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from database.connection import get_db
from database.models import InventoryItem, SalesOrder
from services.customer_service import list_customers
from services.inventory_service import list_inventory_items
from services.security_service import (
    AuthorizationError,
    actor_has_permission,
    current_actor_context,
)
from services.sales_order_report_service import (
    generate_order_confirmation_pdf,
    generate_sales_order_excel_report,
    generate_sales_order_pdf_report,
)
from services.sales_order_service import (
    ORDER_STATUSES,
    cancel_sales_order,
    confirm_sales_order,
    create_draft_order,
    evaluate_proposed_order_credit_warning,
    fulfil_sales_order,
    get_sales_order_by_id,
    get_inventory_sales_order_activity,
    get_sales_order_metrics,
    get_sales_order_summary,
    hold_sales_order,
    list_sales_orders,
    resume_sales_order,
    update_draft_order,
)
from utils.validators import format_currency


def _trusted_actor_context():
    actor_context = current_actor_context()
    if actor_context is None:
        raise AuthorizationError("Sign in is required.")
    return actor_context


class SalesOrderForm(QDialog):
    def __init__(self, parent=None, order_id: int | None = None):
        super().__init__(parent)
        self.order_id = order_id
        self.actor_context = current_actor_context()
        self.lines: list[dict] = []
        self.customers = []
        self.inventory: list[InventoryItem] = []
        self.setWindowTitle("Edit Sales Order" if order_id else "Create Sales Order")
        self.resize(850, 700)
        self._load_choices()
        self._build_ui()
        if order_id:
            self._load_order()
        self._refresh_credit_warning()

    def _load_choices(self) -> None:
        self.customer_access_error = None
        self._existing_order = None
        self._customer_data = {}
        if self.actor_context is None:
            self.customer_access_error = "Sign in is required."
            return
        with get_db() as db:
            try:
                customer_rows = list_customers(
                    db, status_filter="ACTIVE", actor_context=self.actor_context
                )
                inventory_rows = list_inventory_items(
                    db, condition="ALL", actor_context=self.actor_context
                )
                order = (
                    get_sales_order_by_id(
                        db, self.order_id, actor_context=self.actor_context
                    )
                    if self.order_id else None
                )
            except AuthorizationError as error:
                customer_rows = []
                inventory_rows = []
                order = None
                self.customer_access_error = str(error)
            self.customers = [
                (customer.id, customer.customer_code, customer.customer_name)
                for customer in customer_rows
            ]
            self.inventory = [
                item for item in inventory_rows
                if item.condition.casefold() in {"working", "recyclable raw material"}
            ]
            self._existing_order = order
            self._customer_data = {customer.id: customer for customer in customer_rows}

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        header = QGroupBox("Order Information")
        form = QFormLayout(header)
        self.customer_combo = QComboBox()
        for customer_id, code, name in self.customers:
            self.customer_combo.addItem(f"{code} — {name}", customer_id)
        self.customer_combo.currentIndexChanged.connect(self._refresh_credit_warning)
        form.addRow("Customer *", self.customer_combo)
        if self.customer_access_error:
            access_warning = QLabel(
                f"Customer selection unavailable: {self.customer_access_error}"
            )
            access_warning.setWordWrap(True)
            form.addRow(access_warning)
        self.contact_label = QLabel("Select a customer")
        form.addRow("Contact / terms", self.contact_label)
        self.order_date = QDateEdit()
        self.order_date.setCalendarPopup(True)
        self.order_date.setDisplayFormat("dd-MMM-yyyy")
        self.order_date.setDate(date.today())
        form.addRow("Order date", self.order_date)
        self.delivery_date = QDateEdit()
        self.delivery_date.setCalendarPopup(True)
        self.delivery_date.setDisplayFormat("dd-MMM-yyyy")
        self.delivery_date.setDate(date.today())
        self.delivery_date.setSpecialValueText("Not specified")
        form.addRow("Expected delivery", self.delivery_date)
        self.notes = QTextEdit()
        self.notes.setMaximumHeight(65)
        form.addRow("Notes", self.notes)
        layout.addWidget(header)

        warning = QGroupBox("Credit-limit check (warning only)")
        warning_layout = QVBoxLayout(warning)
        self.credit_warning = QLabel("Credit warning appears when the order total is calculated.")
        self.credit_warning.setWordWrap(True)
        warning_layout.addWidget(self.credit_warning)
        layout.addWidget(warning)

        items = QGroupBox("Order Items")
        items_layout = QVBoxLayout(items)
        controls = QHBoxLayout()
        self.item_combo = QComboBox()
        for item in self.inventory:
            available = (item.quantity_available or Decimal("0")) - (
                item.reserved_quantity or Decimal("0")
            )
            self.item_combo.addItem(
                f"{item.part_code} — {item.part_name} (available {available} {item.unit})",
                item.id,
            )
        controls.addWidget(self.item_combo, 3)
        self.quantity = self._number_box(0.01, 1_000_000, 2, 1)
        self.price = self._number_box(0, 100_000_000, 2, 0)
        self.discount = self._number_box(0, 100_000_000, 2, 0)
        self.tax = self._number_box(0, 100, 3, 0)
        for label, widget in (
            ("Qty", self.quantity), ("Unit price", self.price),
            ("Discount", self.discount), ("Tax %", self.tax),
        ):
            controls.addWidget(QLabel(label))
            controls.addWidget(widget)
        add_line = QPushButton("Add item")
        add_line.clicked.connect(self._add_line)
        controls.addWidget(add_line)
        items_layout.addLayout(controls)
        self.item_combo.currentIndexChanged.connect(self._set_default_price)
        self._set_default_price()
        self.line_table = QTableWidget(0, 8)
        self.line_table.setHorizontalHeaderLabels([
            "Part ID", "Component", "Qty", "Unit", "Unit price", "Discount", "Tax %", "Line total",
        ])
        self.line_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.line_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.line_table.setEditTriggers(QTableWidget.NoEditTriggers)
        items_layout.addWidget(self.line_table)
        line_actions = QHBoxLayout()
        remove_line = QPushButton("Remove selected item")
        remove_line.clicked.connect(self._remove_line)
        line_actions.addWidget(remove_line)
        line_actions.addStretch()
        self.order_discount = self._number_box(0, 100_000_000, 2, 0)
        self.charges = self._number_box(0, 100_000_000, 2, 0)
        line_actions.addWidget(QLabel("Order discount"))
        line_actions.addWidget(self.order_discount)
        line_actions.addWidget(QLabel("Additional charges"))
        line_actions.addWidget(self.charges)
        items_layout.addLayout(line_actions)
        self.total_label = QLabel("Order total: 0.00")
        self.total_label.setAlignment(Qt.AlignRight)
        items_layout.addWidget(self.total_label)
        self.order_discount.valueChanged.connect(self._refresh_totals)
        self.charges.valueChanged.connect(self._refresh_totals)
        layout.addWidget(items, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save Draft")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save)
        self.save_button = save
        if self.actor_context is None:
            save.setEnabled(False)
        else:
            with get_db() as db:
                save.setEnabled(
                    actor_has_permission(
                        self.actor_context, "sales.manage", db_session=db
                    )
                )
        if not self.customers or not self.inventory:
            save.setEnabled(False)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    @staticmethod
    def _number_box(minimum: float, maximum: float, decimals: int, value: float) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(minimum, maximum)
        box.setDecimals(decimals)
        box.setValue(value)
        box.setGroupSeparatorShown(True)
        return box

    def _set_default_price(self) -> None:
        item_id = self.item_combo.currentData()
        if item_id is None:
            return
        item = next((entry for entry in self.inventory if entry.id == item_id), None)
        if item:
            self.price.setValue(float(item.estimated_selling_price or 0))

    def _load_order(self) -> None:
        order = self._existing_order
        if not order:
            return
        index = self.customer_combo.findData(order.customer_id)
        if index >= 0:
            self.customer_combo.setCurrentIndex(index)
        self.order_date.setDate(QDate.fromString(order.order_date.isoformat(), "yyyy-MM-dd"))
        if order.expected_delivery_date:
            self.delivery_date.setDate(QDate.fromString(order.expected_delivery_date.isoformat(), "yyyy-MM-dd"))
        self.notes.setPlainText(order.notes or "")
        self.order_discount.setValue(float(order.discount_amount or 0))
        self.charges.setValue(float(order.additional_charges or 0))
        for line in order.items:
            self.lines.append({
                "inventory_item_id": line.inventory_item_id,
                "quantity_ordered": line.quantity_ordered,
                "unit_selling_price": line.unit_selling_price,
                "discount": line.discount,
                "tax_rate": line.tax_rate,
                "part_code": line.part_code_snapshot,
                "product_name": line.product_name_snapshot,
                "unit": line.unit,
            })
        self._render_lines()

    def _add_line(self) -> None:
        item_id = self.item_combo.currentData()
        if item_id is None:
            QMessageBox.warning(self, "Select inventory", "Add saleable inventory before creating an order.")
            return
        item = next((entry for entry in self.inventory if entry.id == item_id), None)
        if not item:
            return
        if any(line["inventory_item_id"] == item_id for line in self.lines):
            QMessageBox.warning(self, "Duplicate item", "An inventory item may appear only once on an order.")
            return
        self.lines.append({
            "inventory_item_id": item_id,
            "quantity_ordered": Decimal(str(self.quantity.value())),
            "unit_selling_price": Decimal(str(self.price.value())),
            "discount": Decimal(str(self.discount.value())),
            "tax_rate": Decimal(str(self.tax.value())),
            "part_code": item.part_code,
            "product_name": item.part_name,
            "unit": item.unit,
        })
        self._render_lines()

    def _remove_line(self) -> None:
        row = self.line_table.currentRow()
        if 0 <= row < len(self.lines):
            self.lines.pop(row)
            self._render_lines()

    def _render_lines(self) -> None:
        self.line_table.setRowCount(len(self.lines))
        for row, line in enumerate(self.lines):
            quantity = Decimal(str(line["quantity_ordered"]))
            price = Decimal(str(line["unit_selling_price"]))
            discount = Decimal(str(line["discount"]))
            tax_rate = Decimal(str(line["tax_rate"]))
            total = (quantity * price - discount) * (Decimal("1") + tax_rate / Decimal("100"))
            values = [
                line["part_code"], line["product_name"], quantity, line["unit"],
                f"{price:.2f}", f"{discount:.2f}", f"{tax_rate:.3f}", f"{total:.2f}",
            ]
            for col, value in enumerate(values):
                self.line_table.setItem(row, col, QTableWidgetItem(str(value)))
        self._refresh_totals()

    def _estimated_total(self) -> Decimal:
        subtotal = sum(
            (Decimal(str(line["quantity_ordered"])) * Decimal(str(line["unit_selling_price"])) for line in self.lines),
            Decimal("0"),
        )
        line_discounts = sum((Decimal(str(line["discount"])) for line in self.lines), Decimal("0"))
        tax_amount = sum((
            (Decimal(str(line["quantity_ordered"])) * Decimal(str(line["unit_selling_price"]))
             - Decimal(str(line["discount"])))
            * Decimal(str(line["tax_rate"])) / Decimal("100")
            for line in self.lines
        ), Decimal("0"))
        return subtotal - line_discounts - Decimal(str(self.order_discount.value())) \
            + Decimal(str(self.charges.value())) + tax_amount

    def _refresh_totals(self) -> None:
        subtotal = sum(
            (
                Decimal(str(line["quantity_ordered"]))
                * Decimal(str(line["unit_selling_price"]))
                for line in self.lines
            ),
            Decimal("0"),
        )
        line_discounts = sum((Decimal(str(line["discount"])) for line in self.lines), Decimal("0"))
        taxes = sum((
            (Decimal(str(line["quantity_ordered"])) * Decimal(str(line["unit_selling_price"]))
             - Decimal(str(line["discount"])))
            * Decimal(str(line["tax_rate"])) / Decimal("100")
            for line in self.lines
        ), Decimal("0"))
        order_discount = Decimal(str(self.order_discount.value()))
        charges = Decimal(str(self.charges.value()))
        total = subtotal - line_discounts - order_discount + charges + taxes
        self.total_label.setText(
            f"Subtotal {format_currency(subtotal)} · Discounts "
            f"{format_currency(line_discounts + order_discount)} · Charges {format_currency(charges)} · "
            f"Tax {format_currency(taxes)} · Grand total {format_currency(total)}. "
            "(authoritative total is recalculated by the service)"
        )
        self._refresh_credit_warning()

    def _refresh_credit_warning(self) -> None:
        customer_id = self.customer_combo.currentData() if hasattr(self, "customer_combo") else None
        customer = self._customer_data.get(customer_id) if customer_id else None
        if customer:
            terms = customer.payment_terms or "Not specified"
            if terms == "Custom" and customer.custom_payment_days:
                terms = f"Custom ({customer.custom_payment_days} days)"
            self.contact_label.setText(
                f"{customer.phone or 'No phone'} · Payment terms: {terms}"
            )
            try:
                with get_db() as db:
                    warning = evaluate_proposed_order_credit_warning(
                        db, customer.id, self._estimated_total() if self.lines else 0,
                        actor_context=self.actor_context,
                    )
                if warning["would_exceed_limit"]:
                    self.credit_warning.setText(
                        f"Warning: projected balance {format_currency(warning['projected_balance'])} "
                        f"exceeds credit limit {format_currency(warning['credit_limit'])}. "
                        "This is a warning only; it does not block the order."
                    )
                else:
                    self.credit_warning.setText(
                        f"Opening balance basis: {format_currency(warning['current_outstanding'])}. "
                        f"Credit limit: {format_currency(warning['credit_limit']) if warning['credit_limit'] is not None else 'Not set'}. "
                        "Sales orders are not receivables."
                    )
            except (ValueError, AuthorizationError) as exc:
                self.credit_warning.setText(str(exc))

    def _save(self) -> None:
        if self.save_button.isEnabled() is False:
            return
        if not self.lines:
            QMessageBox.warning(self, "Order items required", "Add at least one order item.")
            return
        header = {
            "customer_id": self.customer_combo.currentData(),
            "order_date": date.fromisoformat(self.order_date.date().toString("yyyy-MM-dd")),
            "expected_delivery_date": date.fromisoformat(self.delivery_date.date().toString("yyyy-MM-dd")),
            "notes": self.notes.toPlainText(),
            "discount_amount": self.order_discount.value(),
            "additional_charges": self.charges.value(),
        }
        self.save_button.setEnabled(False)
        try:
            if self.actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                if self.order_id:
                    order = update_draft_order(
                        db, self.order_id, header, self.lines,
                        actor_context=self.actor_context,
                    )
                else:
                    order = create_draft_order(
                        db, header, self.lines, actor_context=self.actor_context
                    )
                db.flush()
                self.order_id = order.id
        except (ValueError, TypeError, AuthorizationError) as exc:
            self.save_button.setEnabled(True)
            QMessageBox.warning(self, "Order not saved", str(exc))
            return
        except Exception as exc:
            self.save_button.setEnabled(True)
            QMessageBox.warning(self, "Order not saved", f"Could not save sales order: {exc}")
            return
        self.accept()


class SalesOrderDetailDialog(QDialog):
    def __init__(self, parent=None, order_id: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Sales Order Details")
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                order = get_sales_order_by_id(
                    db, order_id, actor_context=actor_context
                )
                if not order:
                    layout.addWidget(QLabel("Sales order was not found."))
                    return
                summary = get_sales_order_summary(
                    db, order, actor_context=actor_context
                )
                fulfilment_ids = {fulfilment.id for fulfilment in order.fulfilments}
                movement_rows = []
                for item_id in {line.inventory_item_id for line in order.items}:
                    sales_activity = get_inventory_sales_order_activity(
                        db, item_id, actor_context=actor_context
                    )
                    movement_rows.extend(
                        movement for movement in sales_activity["dispatch_movements"]
                        if movement.reference_id in fulfilment_ids
                    )
                self._render(layout, order, summary, movement_rows)
        except AuthorizationError as error:
            layout.addWidget(QLabel(f"Sales order details unavailable: {error}"))
            return

    def _render(self, layout, order: SalesOrder, summary: dict, movement_rows) -> None:
        layout.addWidget(QLabel(
            f"<h2>{order.order_code}</h2><b>{order.customer.customer_name}</b> · "
            f"{order.order_date} · {order.status}"
        ))
        layout.addWidget(QLabel(
            f"Subtotal {format_currency(order.subtotal)} · Item discounts "
            f"{format_currency(summary['line_discount_amount'])} · Order discount "
            f"{format_currency(order.discount_amount)} · "
            f"Tax {format_currency(order.tax_amount)} · Order total {format_currency(order.grand_total)}"
        ))
        layout.addWidget(QLabel(
            "Order value is a commitment, not recognized sales revenue, an invoice, or a receivable."
        ))
        table = QTableWidget(len(summary["items"]), 8)
        table.setHorizontalHeaderLabels([
            "Part ID", "Component", "Ordered", "Fulfilled", "Reserved", "Unit", "Price", "Line total",
        ])
        for row, line in enumerate(summary["items"]):
            values = [
                line["part_code"], line["product_name"], line["quantity_ordered"],
                line["quantity_fulfilled"], line["quantity_reserved"], line["unit"],
                format_currency(line["unit_selling_price"]), format_currency(line["line_total"]),
            ]
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(str(value)))
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(table)
        layout.addWidget(QLabel("Reservation event history"))
        reservation_table = QTableWidget(len(order.reservations), 5)
        reservation_table.setHorizontalHeaderLabels([
            "Event", "Part ID", "Quantity", "Date", "Reason",
        ])
        for row, event in enumerate(order.reservations):
            values = [
                event.event_type, event.part_code_snapshot, event.quantity,
                event.created_at, event.reason or "—",
            ]
            for col, value in enumerate(values):
                reservation_table.setItem(row, col, QTableWidgetItem(str(value)))
        reservation_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(reservation_table)
        layout.addWidget(QLabel("Fulfilment history"))
        fulfilments = QTableWidget(len(order.fulfilments), 4)
        fulfilments.setHorizontalHeaderLabels(["Fulfilment ID", "Date", "User", "Remarks"])
        for row, fulfilment in enumerate(order.fulfilments):
            for col, value in enumerate([
                fulfilment.fulfilment_code, fulfilment.fulfilment_date,
                fulfilment.responsible_user or "—", fulfilment.remarks or "—",
            ]):
                fulfilments.setItem(row, col, QTableWidgetItem(str(value)))
        layout.addWidget(fulfilments)
        layout.addWidget(QLabel("Status history"))
        history = QTableWidget(len(order.status_history), 4)
        history.setHorizontalHeaderLabels(["From", "To", "Date", "Reason"])
        for row, event in enumerate(order.status_history):
            for col, value in enumerate([
                event.previous_status or "—", event.new_status, event.created_at,
                event.reason or "—",
            ]):
                history.setItem(row, col, QTableWidgetItem(str(value)))
        layout.addWidget(history)
        layout.addWidget(QLabel("Dispatch stock movement references"))
        movements = QTableWidget(len(movement_rows), 6)
        movements.setHorizontalHeaderLabels([
            "Movement ID", "Fulfilment", "Part ID", "Quantity", "Physical before", "Physical after",
        ])
        for row, movement in enumerate(movement_rows):
            values = [
                movement.id, movement.reference_id,
                movement.inventory_item.part_code if movement.inventory_item else movement.inventory_item_id,
                movement.quantity, movement.previous_quantity, movement.new_quantity,
            ]
            for col, value in enumerate(values):
                movements.setItem(row, col, QTableWidgetItem(str(value)))
        movements.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(movements)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)


class SalesOrderFulfilmentDialog(QDialog):
    def __init__(self, parent=None, order_id: int = 0):
        super().__init__(parent)
        self.order_id = order_id
        self.actor_context = current_actor_context()
        self.setWindowTitle("Record Dispatch / Fulfilment")
        self.resize(700, 450)
        self.inputs: dict[int, QDoubleSpinBox] = {}
        layout = QVBoxLayout(self)
        try:
            if self.actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                order = get_sales_order_by_id(
                    db, order_id, actor_context=self.actor_context
                )
                if not order:
                    layout.addWidget(QLabel("Sales order was not found."))
                    return
                self._build(layout, order)
        except AuthorizationError as error:
            layout.addWidget(QLabel(f"Dispatch is unavailable: {error}"))
            return

    def _build(self, layout, order: SalesOrder) -> None:
        layout.addWidget(QLabel(
            f"Order {order.order_code}: record only quantities actually dispatched. "
            "Physical stock is deducted transactionally."
        ))
        table = QTableWidget(len(order.items), 5)
        table.setHorizontalHeaderLabels(["Part ID", "Component", "Remaining", "Reserved", "Dispatch now"])
        with get_db() as db:
            summary_by_id = {
                entry["order_item_id"]: entry
                for entry in get_sales_order_summary(
                    db, order, actor_context=self.actor_context
                )["items"]
            }
        for row, line in enumerate(order.items):
            summary = summary_by_id[line.id]
            remaining = Decimal(str(summary["quantity_remaining"]))
            reserved = Decimal(str(summary["quantity_reserved"]))
            box = QDoubleSpinBox()
            box.setRange(0, float(min(remaining, reserved)))
            box.setDecimals(2)
            table.setItem(row, 0, QTableWidgetItem(line.part_code_snapshot))
            table.setItem(row, 1, QTableWidgetItem(line.product_name_snapshot))
            table.setItem(row, 2, QTableWidgetItem(str(remaining)))
            table.setItem(row, 3, QTableWidgetItem(str(reserved)))
            table.setCellWidget(row, 4, box)
            self.inputs[line.id] = box
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(table)
        self.remarks = QLineEdit()
        self.remarks.setPlaceholderText("Dispatch remarks")
        layout.addWidget(self.remarks)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Record Fulfilment")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save)
        if self.actor_context is None:
            save.setEnabled(False)
        else:
            with get_db() as db:
                save.setEnabled(
                    actor_has_permission(
                        self.actor_context, "sales.dispatch", db_session=db
                    )
                    and actor_has_permission(
                        self.actor_context, "inventory.dispatch", db_session=db
                    )
                )
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _save(self) -> None:
        quantities = {
            line_id: box.value() for line_id, box in self.inputs.items() if box.value() > 0
        }
        if not quantities:
            QMessageBox.warning(self, "Quantity required", "Enter at least one quantity dispatched.")
            return
        try:
            if self.actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                fulfil_sales_order(
                    db,
                    self.order_id,
                    quantities,
                    remarks=self.remarks.text().strip(),
                    idempotency_key=uuid4().hex,
                    actor_context=self.actor_context,
                )
        except (ValueError, TypeError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Fulfilment not recorded", str(exc))
            return
        self.accept()


class SalesOrderPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_order_id: int | None = None
        self._action_buttons: dict[str, QPushButton] = {}
        self._build_ui()
        self.load_orders()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Sales Order Management")
        title.setObjectName("supplierPageTitle")
        subtitle = QLabel("Manage customer order commitments, reservations, and physical dispatches.")
        subtitle.setObjectName("supplierPageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.btn_export = QPushButton("Reports PDF / Excel")
        self.btn_export.setObjectName("secondaryButton")
        self.btn_export.clicked.connect(self._export)
        header.addWidget(self.btn_export)
        self.btn_create = QPushButton("+ Add Sales Order")
        self.btn_create.setObjectName("primaryButton")
        self.btn_create.clicked.connect(self._create)
        header.addWidget(self.btn_create)
        layout.addLayout(header)

        self.access_warning = QLabel()
        self.access_warning.setObjectName("muted")
        self.access_warning.setWordWrap(True)
        self.access_warning.setVisible(False)
        layout.addWidget(self.access_warning)

        self.metrics = QGridLayout()
        self.metric_labels: dict[str, QLabel] = {}
        for index, (key, text) in enumerate((
            ("total_orders", "Orders"), ("draft_orders", "Draft"),
            ("confirmed_orders", "Confirmed"), ("partially_fulfilled_orders", "Partially Fulfilled"),
            ("fulfilled_orders", "Fulfilled"), ("on_hold_orders", "On Hold"),
            ("open_order_value", "Order Value (not revenue)"),
        )):
            card = QGroupBox(text)
            card_layout = QVBoxLayout(card)
            value = QLabel("0")
            value.setObjectName("metricBigValue")
            card_layout.addWidget(value)
            self.metrics.addWidget(card, index // 4, index % 4)
            self.metric_labels[key] = value
        layout.addLayout(self.metrics)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search order ID or customer")
        self.search.textChanged.connect(self.load_orders)
        filters.addWidget(self.search, 2)
        self.customer_filter = QComboBox()
        self.customer_filter.addItem("All customers", None)
        try:
            actor_context = current_actor_context()
            if actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                for customer in list_customers(
                    db, actor_context=actor_context
                ):
                    self.customer_filter.addItem(
                        f"{customer.customer_code} — {customer.customer_name}", customer.id
                    )
        except AuthorizationError:
            self.customer_filter.clear()
            self.customer_filter.addItem("Customer access unavailable", None)
        self.customer_filter.currentIndexChanged.connect(self.load_orders)
        filters.addWidget(self.customer_filter)
        self.status_filter = QComboBox()
        self.status_filter.addItem("All statuses", "ALL")
        for status in ORDER_STATUSES:
            self.status_filter.addItem(status, status)
        self.status_filter.currentIndexChanged.connect(self.load_orders)
        filters.addWidget(self.status_filter)
        self.start_filter = QDateEdit()
        self.start_filter.setCalendarPopup(True)
        self.start_filter.setSpecialValueText("From date")
        self.start_filter.setDate(self.start_filter.minimumDate())
        self.start_filter.dateChanged.connect(self.load_orders)
        filters.addWidget(self.start_filter)
        self.end_filter = QDateEdit()
        self.end_filter.setCalendarPopup(True)
        self.end_filter.setSpecialValueText("To date")
        self.end_filter.setDate(self.end_filter.minimumDate())
        self.end_filter.dateChanged.connect(self.load_orders)
        filters.addWidget(self.end_filter)
        layout.addLayout(filters)

        toolbar = QHBoxLayout()
        self.count_label = QLabel("Showing 0 orders")
        toolbar.addWidget(self.count_label)
        toolbar.addStretch()
        for label, permission, callback in (
            ("Details", "sales.view", self._details),
            ("Edit Draft", "sales.manage", self._edit),
            ("Confirm", "sales.confirm", self._confirm),
            ("Hold / Resume", "sales.confirm", self._hold_resume),
            ("Cancel", "sales.manage", self._cancel),
            ("Fulfil / Dispatch", "sales.dispatch", self._fulfil),
            ("Order Confirmation PDF", "sales.view", self._confirmation_pdf),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
            self._action_buttons[label] = button
        layout.addLayout(toolbar)

        headers = ["Order ID", "Customer", "Order Date", "Expected Delivery", "Order Total", "Progress", "Status"]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _: self._details())
        layout.addWidget(self.table, 1)

    def load_orders(self, *_args) -> None:
        start = self.start_filter.date().toString("yyyy-MM-dd")
        end = self.end_filter.date().toString("yyyy-MM-dd")
        actor_context = current_actor_context()
        if actor_context is None:
            self._clear_protected_data("Sign in is required to view sales orders.")
            return
        try:
            with get_db() as db:
                metrics = get_sales_order_metrics(
                    db, actor_context=actor_context
                )
                orders = list_sales_orders(
                    db, search=self.search.text() if hasattr(self, "search") else "",
                    customer_id=self.customer_filter.currentData() if hasattr(self, "customer_filter") else None,
                    status=self.status_filter.currentData() if hasattr(self, "status_filter") else "ALL",
                    start_date=date.fromisoformat(start) if start_filter_is_set(self.start_filter) else None,
                    end_date=date.fromisoformat(end) if start_filter_is_set(self.end_filter) else None,
                    actor_context=actor_context,
                )
                summaries = {
                    order.id: get_sales_order_summary(
                        db, order, actor_context=actor_context
                    )
                    for order in orders
                }
                for key, permission in (
                    ("Details", "sales.view"),
                    ("Edit Draft", "sales.manage"),
                    ("Confirm", "sales.confirm"),
                    ("Hold / Resume", "sales.confirm"),
                    ("Cancel", "sales.manage"),
                    ("Fulfil / Dispatch", "sales.dispatch"),
                    ("Order Confirmation PDF", "sales.view"),
                ):
                    enabled = actor_has_permission(
                        actor_context, permission, db_session=db
                    )
                    if key == "Fulfil / Dispatch":
                        enabled = enabled and actor_has_permission(
                            actor_context, "inventory.dispatch", db_session=db
                        )
                    self._action_buttons[key].setEnabled(enabled)
                self.btn_create.setEnabled(
                    actor_has_permission(actor_context, "sales.manage", db_session=db)
                )
                self.btn_export.setEnabled(
                    actor_has_permission(actor_context, "sales.view", db_session=db)
                )
        except AuthorizationError as error:
            self._clear_protected_data(str(error))
            return
        self.access_warning.clear()
        self.access_warning.setVisible(False)
        for key, label in self.metric_labels.items():
            value = metrics[key]
            label.setText(format_currency(value) if key == "open_order_value" else str(value))
        self.table.setRowCount(len(orders))
        for row, order in enumerate(orders):
            summary = summaries[order.id]
            values = [
                order.order_code, order.customer.customer_name if order.customer else "—",
                order.order_date, order.expected_delivery_date or "—",
                format_currency(order.grand_total),
                f"{summary['total_fulfilled_quantity']} / {summary['total_ordered_quantity']} "
                f"({summary['fulfilment_percent']}%)",
                order.status,
            ]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.UserRole, order.id)
                self.table.setItem(row, col, cell)
        self.count_label.setText(f"Showing {len(orders)} orders")
        self._selection_changed()

    def _clear_protected_data(self, message: str) -> None:
        self.table.setRowCount(0)
        self.selected_order_id = None
        self.count_label.setText("Sales order data unavailable")
        if hasattr(self, "customer_filter"):
            self.customer_filter.blockSignals(True)
            self.customer_filter.clear()
            self.customer_filter.addItem("Customer access unavailable", None)
            self.customer_filter.blockSignals(False)
        for label in self.metric_labels.values():
            label.clear()
        self.btn_create.setEnabled(False)
        self.btn_export.setEnabled(False)
        for button in self._action_buttons.values():
            button.setEnabled(False)
        self.access_warning.setText(f"Sales order access unavailable: {message}")
        self.access_warning.setVisible(True)

    def _selected_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        cell = self.table.item(row, 0)
        return cell.data(Qt.UserRole) if cell else None

    def _selection_changed(self) -> None:
        self.selected_order_id = self._selected_id()

    def _create(self) -> None:
        dialog = SalesOrderForm(self)
        if dialog.exec():
            self.load_orders()

    def _edit(self) -> None:
        order_id = self._selected_id()
        if not order_id:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                order = get_sales_order_by_id(
                    db, order_id, actor_context=actor_context
                )
                if not order or order.status not in {"Draft", "Confirmed"}:
                    QMessageBox.warning(
                        self, "Cannot edit", "Only Draft and Confirmed orders can be edited."
                    )
                    return
        except AuthorizationError as error:
            QMessageBox.warning(self, "Order unavailable", str(error))
            return
        dialog = SalesOrderForm(self, order_id)
        if dialog.exec():
            self.load_orders()

    def _details(self) -> None:
        order_id = self._selected_id()
        if order_id:
            SalesOrderDetailDialog(self, order_id).exec()

    def _confirm(self) -> None:
        order_id = self._selected_id()
        if not order_id:
            return
        if QMessageBox.question(
            self, "Confirm and reserve stock",
            "Confirm this order and reserve available stock? Physical stock will not be deducted.",
        ) != QMessageBox.Yes:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                confirm_sales_order(
                    db, order_id, actor_context=actor_context
                )
        except (ValueError, TypeError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Order not confirmed", str(exc))
            return
        self.load_orders()

    def _hold_resume(self) -> None:
        order_id = self._selected_id()
        if not order_id:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                order = get_sales_order_by_id(
                    db, order_id, actor_context=actor_context
                )
                if not order:
                    return
                status = order.status
                if status == "On Hold":
                    resume_sales_order(
                        db, order_id, actor_context=actor_context
                    )
                else:
                    reason, accepted = QInputDialog.getText(
                        self, "Place order on hold", "Reason for releasing reservations:"
                    )
                    if not accepted:
                        return
                    hold_sales_order(
                        db, order_id, reason, actor_context=actor_context
                    )
        except (ValueError, TypeError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Status not changed", str(exc))
            return
        self.load_orders()

    def _cancel(self) -> None:
        order_id = self._selected_id()
        if not order_id:
            return
        if QMessageBox.question(
            self, "Cancel sales order", "Cancel this order and release any remaining reservations?"
        ) != QMessageBox.Yes:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                cancel_sales_order(
                    db, order_id, "Cancelled by user",
                    actor_context=actor_context,
                )
        except (ValueError, TypeError, AuthorizationError) as exc:
            QMessageBox.warning(self, "Order not cancelled", str(exc))
            return
        self.load_orders()

    def _fulfil(self) -> None:
        order_id = self._selected_id()
        if not order_id:
            return
        dialog = SalesOrderFulfilmentDialog(self, order_id)
        if dialog.exec():
            self.load_orders()

    def _export(self) -> None:
        path, selected = QFileDialog.getSaveFileName(
            self, "Export Sales Order Report", "sales-order-register.xlsx",
            "Excel Workbook (*.xlsx);;PDF Document (*.pdf)",
        )
        if not path:
            return
        try:
            actor_context = _trusted_actor_context()
            start = date.fromisoformat(self.start_filter.date().toString("yyyy-MM-dd")) \
                if start_filter_is_set(self.start_filter) else None
            end = date.fromisoformat(self.end_filter.date().toString("yyyy-MM-dd")) \
                if start_filter_is_set(self.end_filter) else None
            with get_db() as db:
                orders = list_sales_orders(
                    db, search=self.search.text(),
                    customer_id=self.customer_filter.currentData(),
                    status=self.status_filter.currentData(),
                    start_date=start,
                    end_date=end,
                    actor_context=actor_context,
                )
                if path.lower().endswith(".pdf") or "PDF" in selected:
                    generate_sales_order_pdf_report(
                        db, path, orders, actor_context=actor_context
                    )
                else:
                    if not path.lower().endswith(".xlsx"):
                        path += ".xlsx"
                    generate_sales_order_excel_report(
                        db, path, orders, actor_context=actor_context
                    )
            QMessageBox.information(self, "Report exported", f"Report saved to:\n{path}")
        except (AuthorizationError, OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    def _confirmation_pdf(self) -> None:
        order_id = self._selected_id()
        if not order_id:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Order Confirmation", "order-confirmation.pdf", "PDF Document (*.pdf)"
        )
        if not path:
            return
        try:
            actor_context = _trusted_actor_context()
            with get_db() as db:
                generate_order_confirmation_pdf(
                    db, order_id, path, actor_context=actor_context
                )
            QMessageBox.information(self, "PDF exported", f"Order confirmation saved to:\n{path}")
        except (AuthorizationError, OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Export failed", str(exc))


def start_filter_is_set(widget: QDateEdit) -> bool:
    return widget.date() != widget.minimumDate()
