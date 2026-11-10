from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)
from sqlalchemy import select

from database.connection import get_db
from database.models import Customer, Supplier
from services.customer_receipt_service import (
    PaymentValidationError,
    apply_customer_advance,
    create_customer_receipt,
    get_customer_receipt_by_id,
    get_customer_receipt_dashboard,
    get_customer_receipt_summary,
    get_customer_receipt_targets,
    list_customer_receipts,
    post_customer_receipt,
    update_customer_receipt,
    void_customer_receipt,
)
from services.payment_common import PAYMENT_METHODS, ZERO
from services.security_service import (
    AuthorizationError,
    current_actor_context,
    require_permission,
)
from services.settings_service import get_active_payment_accounts
from services.payment_report_service import (
    generate_customer_receipt_pdf,
    generate_payment_excel_report,
    generate_payment_pdf_report,
    generate_supplier_payment_voucher_pdf,
)
from services.supplier_payment_service import (
    apply_supplier_advance,
    create_supplier_payment,
    get_supplier_payment_by_id,
    get_supplier_payment_dashboard,
    get_supplier_payment_summary,
    get_supplier_payment_targets,
    list_supplier_payments,
    post_supplier_payment,
    update_supplier_payment,
    void_supplier_payment,
)
from utils.validators import format_currency


_NO_DATE = QDate(2000, 1, 1)


def _actor_context():
    actor_context = current_actor_context()
    if actor_context is None:
        raise AuthorizationError("Sign in is required.")
    return actor_context


def _has_permission(permission_code: str) -> bool:
    try:
        with get_db() as db:
            require_permission(
                _actor_context(), permission_code, db_session=db
            )
        return True
    except AuthorizationError:
        return False


def _qt_date(value: date | None) -> str:
    return value.isoformat() if value else "—"


def _decimal_text(value: Decimal | None) -> str:
    return f"{value or Decimal('0.00'):.2f}"


class PaymentEntryDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        customer_side: bool,
        existing: dict | None = None,
    ):
        super().__init__(parent)
        self.customer_side = customer_side
        self.existing = existing
        self.targets: list[dict] = []
        self._loading_table = False
        self._submitting = False
        self.setWindowTitle(
            ("Edit Draft Customer Receipt" if existing else "New Customer Receipt")
            if customer_side
            else ("Edit Draft Supplier Payment" if existing else "New Supplier Payment")
        )
        self.setModal(True)
        self.resize(930, 650)

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.party = QComboBox()
        self.party.setMinimumWidth(360)
        self.day = QDateEdit()
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("dd-MMM-yyyy")
        self.amount = QLineEdit()
        self.amount.setValidator(QDoubleValidator(0, 999999999999.99, 2, self))
        self.amount.setPlaceholderText("0.00")
        self.method = QComboBox()
        self.method.addItems(PAYMENT_METHODS)
        self.reference = QLineEdit()
        self.bank = QComboBox()
        self.bank.setEditable(True)
        self.bank.setInsertPolicy(QComboBox.NoInsert)
        self.cheque = QLineEdit()
        self.notes = QLineEdit()
        form.addRow("Customer" if customer_side else "Supplier", self.party)
        form.addRow("Receipt date" if customer_side else "Payment date", self.day)
        form.addRow("Amount received (₹)" if customer_side else "Amount paid (₹)", self.amount)
        form.addRow("Payment method", self.method)
        form.addRow("Transaction reference", self.reference)
        form.addRow("Bank / account name", self.bank)
        form.addRow("Cheque number", self.cheque)
        form.addRow("Notes", self.notes)
        root.addLayout(form)

        self.allocations = QTableWidget(0, 7)
        self.allocations.setHorizontalHeaderLabels([
            "Target", "Date", "Due date", "Total (₹)", "Previously paid (₹)",
            "Outstanding (₹)", "Allocate (₹)",
        ])
        self.allocations.setSelectionBehavior(QTableWidget.SelectRows)
        self.allocations.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.allocations.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.allocations.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for column in range(3, 7):
            self.allocations.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        root.addWidget(QLabel(
            "Leave allocation at 0.00 to retain the amount as an explicit customer advance "
            "or supplier advance. Existing advances are never applied automatically."
        ))
        root.addWidget(self.allocations)
        self.totals = QLabel("Allocated: ₹ 0.00 | Unallocated advance: ₹ 0.00")
        root.addWidget(self.totals)
        self.access_warning = QLabel()
        self.access_warning.setObjectName("muted")
        self.access_warning.setWordWrap(True)
        root.addWidget(self.access_warning)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Save Draft")
        permission_prefix = "receipts" if customer_side else "supplier_payments"
        self.save_button.setEnabled(
            _has_permission(f"{permission_prefix}.manage")
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.amount.textChanged.connect(self._update_totals)
        self.allocations.itemChanged.connect(self._update_totals)
        self.party.currentIndexChanged.connect(self._load_targets)

        actor_context = _actor_context()
        with get_db() as db:
            require_permission(
                actor_context,
                "receipts.view" if customer_side else "supplier_payments.view",
                db_session=db,
            )
            parties = db.scalars(
                select(Customer if customer_side else Supplier).order_by(
                    Customer.customer_name if customer_side else Supplier.business_name
                )
            ).all()
            payment_accounts = get_active_payment_accounts(
                db, actor_context=actor_context
            )
        self.bank.addItems([account.display_name for account in payment_accounts])
        self.bank.setCurrentIndex(-1)
        self.bank.setEditText("")
        for party in parties:
            display = (
                f"{party.customer_code} — {party.customer_name}"
                if customer_side
                else f"{party.supplier_code} — {party.business_name}"
            )
            self.party.addItem(display, party.id)

        if existing:
            self.party.setCurrentIndex(max(0, self.party.findData(existing["party_id"])))
            self.day.setDate(QDate.fromString(existing["date"], "yyyy-MM-dd"))
            self.amount.setText(existing["amount"])
            self.method.setCurrentText(existing["method"])
            self.reference.setText(existing["reference"] or "")
            self.bank.setEditText(existing["bank"] or "")
            self.cheque.setText(existing["cheque"] or "")
            self.notes.setText(existing["notes"] or "")
        else:
            self.day.setDate(QDate.currentDate())
        self._load_targets()

    def _load_targets(self, *_args):
        try:
            self._load_targets_authorized()
        except AuthorizationError as exc:
            self.targets = []
            self.allocations.setRowCount(0)
            self.save_button.setEnabled(False)
            self.access_warning.setText(
                f"Payment allocation data is unavailable: {exc}"
            )
            self._update_totals()

    def _load_targets_authorized(self):
        party_id = self.party.currentData()
        if not party_id:
            self.targets = []
            self.allocations.setRowCount(0)
            self._update_totals()
            return
        with get_db() as db:
            targets = (
                get_customer_receipt_targets(
                    db, int(party_id), actor_context=_actor_context()
                )
                if self.customer_side
                else get_supplier_payment_targets(
                    db, int(party_id), actor_context=_actor_context()
                )
            )
            if self.existing:
                transaction = (
                    get_customer_receipt_by_id(
                        db, self.existing["id"], actor_context=_actor_context()
                    )
                    if self.customer_side
                    else get_supplier_payment_by_id(
                        db, self.existing["id"], actor_context=_actor_context()
                    )
                )
                previous = {}
                if transaction:
                    for allocation in transaction.allocations:
                        key = (
                            allocation.allocation_type,
                            allocation.invoice_id if self.customer_side else allocation.purchase_id,
                        )
                        previous[key] = previous.get(key, Decimal("0.00")) + allocation.amount
                for target in targets:
                    key = (target["allocation_type"], target.get("invoice_id", target.get("purchase_id")))
                    target["existing_allocation"] = previous.get(key, Decimal("0.00"))
                for key, amount in previous.items():
                    if not any(
                        (row["allocation_type"], row.get("invoice_id", row.get("purchase_id"))) == key
                        for row in targets
                    ):
                        targets.append({
                            "allocation_type": key[0],
                            "invoice_id" if self.customer_side else "purchase_id": key[1],
                            "code": "Existing allocation",
                            "invoice_date" if self.customer_side else "purchase_date": None,
                            "due_date": None,
                            "total": amount,
                            "previously_received" if self.customer_side else "previously_allocated": ZERO,
                            "historical_amount_paid": ZERO,
                            "outstanding": amount,
                            "existing_allocation": amount,
                        })
        self.targets = targets
        self._loading_table = True
        self.allocations.setRowCount(len(targets))
        for row, target in enumerate(targets):
            date_value = target.get("invoice_date", target.get("purchase_date"))
            previously_paid = target.get(
                "previously_received",
                target.get("historical_amount_paid", Decimal("0.00"))
                + target.get("previously_allocated", Decimal("0.00")),
            )
            values = [
                target["code"],
                _qt_date(date_value),
                _qt_date(target.get("due_date")),
                _decimal_text(target.get("total")),
                _decimal_text(previously_paid),
                _decimal_text(target.get("outstanding")),
                _decimal_text(target.get("existing_allocation")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setData(Qt.UserRole, target)
                if column == 6:
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                self.allocations.setItem(row, column, item)
        self._loading_table = False
        self._update_totals()

    def _allocation_payload(self) -> list[dict]:
        allocations = []
        for row, target in enumerate(self.targets):
            item = self.allocations.item(row, 6)
            raw_amount = item.text().strip() if item else "0"
            try:
                amount = Decimal(raw_amount or "0").quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                raise PaymentValidationError(f"Allocation for {target['code']} must be a valid amount.")
            if amount < 0:
                raise PaymentValidationError(f"Allocation for {target['code']} cannot be negative.")
            if amount == 0:
                continue
            if amount > target["outstanding"]:
                raise PaymentValidationError(
                    f"Allocation to {target['code']} exceeds its displayed outstanding balance."
                )
            key = "invoice_id" if self.customer_side else "purchase_id"
            allocations.append({
                "allocation_type": target["allocation_type"],
                key: target.get(key),
                "amount": amount,
                "allocation_date": self.day.date().toPython(),
            })
        return allocations

    def _update_totals(self, *_args):
        if self._loading_table:
            return
        allocated = Decimal("0.00")
        for row in range(self.allocations.rowCount()):
            item = self.allocations.item(row, 6)
            if item:
                try:
                    allocated += Decimal(item.text().strip() or "0")
                except InvalidOperation:
                    pass
        try:
            total = Decimal(self.amount.text() or "0")
        except InvalidOperation:
            total = Decimal("0")
        self.totals.setText(
            f"Allocated: {format_currency(allocated)} | "
            f"Unallocated advance: {format_currency(max(Decimal('0'), total - allocated))}"
        )

    def _validate_and_accept(self):
        try:
            amount = Decimal(self.amount.text() or "0")
            if amount <= 0:
                raise PaymentValidationError("Payment amount must be greater than zero.")
            allocations = self._allocation_payload()
            if sum((row["amount"] for row in allocations), Decimal("0")) > amount:
                raise PaymentValidationError("Allocations cannot exceed the transaction amount.")
        except (InvalidOperation, PaymentValidationError) as exc:
            QMessageBox.warning(self, "Invalid payment", str(exc))
            return
        self._submitting = True
        self.save_button.setEnabled(False)
        self.accept()

    def payload(self) -> tuple[dict, list[dict]]:
        return ({
            "customer_id" if self.customer_side else "supplier_id": self.party.currentData(),
            "receipt_date" if self.customer_side else "payment_date": self.day.date().toPython(),
            "amount_received" if self.customer_side else "amount_paid": self.amount.text(),
            "payment_method": self.method.currentText(),
            "transaction_reference": self.reference.text(),
            "bank_account_name": self.bank.currentText(),
            "cheque_number": self.cheque.text(),
            "notes": self.notes.text(),
        }, self._allocation_payload())


class PaymentDetailDialog(QDialog):
    def __init__(self, parent, *, customer_side: bool, transaction_id: int):
        super().__init__(parent)
        self.customer_side = customer_side
        self.transaction_id = transaction_id
        self.setWindowTitle("Payment Details")
        self.resize(700, 500)
        layout = QVBoxLayout(self)
        with get_db() as db:
            transaction = (
                get_customer_receipt_by_id(
                    db, transaction_id, actor_context=_actor_context()
                )
                if customer_side else get_supplier_payment_by_id(
                    db, transaction_id, actor_context=_actor_context()
                )
            )
            if transaction is None:
                layout.addWidget(QLabel("Payment record was not found."))
                return
            summary = (
                get_customer_receipt_summary(
                    db, transaction, actor_context=_actor_context()
                )
                if customer_side else get_supplier_payment_summary(
                    db, transaction, actor_context=_actor_context()
                )
            )
            self.unallocated_amount = summary["unallocated_amount"]
            code = transaction.receipt_code if customer_side else transaction.payment_code
            party = (
                transaction.customer.customer_name
                if customer_side else transaction.supplier.business_name
            )
            day = transaction.receipt_date if customer_side else transaction.payment_date
            amount = transaction.amount_received if customer_side else transaction.amount_paid
            heading = QLabel(
                f"<h2>{code}</h2><b>{party}</b> · {day} · "
                f"{transaction.payment_method} · {transaction.status}<br>"
                f"Amount {format_currency(amount)} · Allocated {format_currency(summary['allocated_amount'])} · "
                f"Advance {format_currency(summary['unallocated_amount'])}"
            )
            heading.setWordWrap(True)
            layout.addWidget(heading)
            table = QTableWidget(len(transaction.allocations), 5)
            table.setHorizontalHeaderLabels(["Target", "Type", "Date", "Amount", "Source"])
            for row, allocation in enumerate(transaction.allocations):
                if customer_side and allocation.invoice_id:
                    from database.models import SalesInvoice
                    target = db.get(SalesInvoice, allocation.invoice_id)
                    target_label = target.invoice_code if target else "Invoice"
                elif not customer_side and allocation.purchase_id:
                    from database.models import Purchase
                    target = db.get(Purchase, allocation.purchase_id)
                    target_label = target.purchase_code if target else "Purchase"
                else:
                    target_label = "Opening Balance"
                values = [
                    target_label, allocation.allocation_type, str(allocation.allocation_date),
                    format_currency(allocation.amount), allocation.allocation_source,
                ]
                for column, value in enumerate(values):
                    table.setItem(row, column, QTableWidgetItem(value))
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            layout.addWidget(table)
            if transaction.void_reason:
                layout.addWidget(QLabel(f"Void reason: {transaction.void_reason}"))
            self.status = transaction.status
            self.code = code

        buttons = QHBoxLayout()
        self.post_button = QPushButton("Post")
        self.void_button = QPushButton("Void")
        self.advance_button = QPushButton("Apply Advance")
        self.pdf_button = QPushButton("Export PDF")
        self.close_button = QPushButton("Close")
        self.post_button.setVisible(self.status == "Draft")
        self.void_button.setVisible(self.status == "Posted")
        self.advance_button.setVisible(
            self.status == "Posted" and self.unallocated_amount > Decimal("0.00")
        )
        permission_prefix = "receipts" if customer_side else "supplier_payments"
        self.post_button.setVisible(
            self.status == "Draft" and _has_permission(f"{permission_prefix}.post")
        )
        self.void_button.setVisible(
            self.status == "Posted" and _has_permission(f"{permission_prefix}.void")
        )
        self.advance_button.setVisible(
            self.status == "Posted"
            and self.unallocated_amount > Decimal("0.00")
            and _has_permission(f"{permission_prefix}.post")
        )
        self.post_button.clicked.connect(self._post)
        self.void_button.clicked.connect(self._void)
        self.advance_button.clicked.connect(self._apply_advance)
        self.pdf_button.clicked.connect(self._export_pdf)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.post_button)
        buttons.addWidget(self.void_button)
        buttons.addWidget(self.advance_button)
        buttons.addWidget(self.pdf_button)
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def _post(self):
        title = "Post receipt" if self.customer_side else "Post supplier payment"
        if QMessageBox.question(
            self, title, "Posting makes this financial transaction affect balances. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                if self.customer_side:
                    post_customer_receipt(
                        db, self.transaction_id, actor_context=_actor_context()
                    )
                else:
                    post_supplier_payment(
                        db, self.transaction_id, actor_context=_actor_context()
                    )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Could not post transaction", str(exc))

    def _void(self):
        reason, accepted = QInputDialog.getText(self, "Void transaction", "Mandatory void reason:")
        if not accepted or not reason.strip():
            return
        if QMessageBox.question(
            self, "Confirm void",
            "Voiding preserves the transaction history but releases its allocations. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                if self.customer_side:
                    void_customer_receipt(
                        db, self.transaction_id, reason,
                        actor_context=_actor_context(),
                    )
                else:
                    void_supplier_payment(
                        db, self.transaction_id, reason,
                        actor_context=_actor_context(),
                    )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Could not void transaction", str(exc))

    def _export_pdf(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF", f"{self.code}.pdf", "PDF files (*.pdf)"
        )
        if not path:
            return
        try:
            with get_db() as db:
                if self.customer_side:
                    generate_customer_receipt_pdf(
                        db, self.transaction_id, path,
                        actor_context=_actor_context(),
                    )
                else:
                    generate_supplier_payment_voucher_pdf(
                        db, self.transaction_id, path,
                        actor_context=_actor_context(),
                    )
            QMessageBox.information(self, "PDF created", f"Saved to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Could not create PDF", str(exc))

    def _apply_advance(self):
        try:
            dialog = AdvanceAllocationDialog(
                self, customer_side=self.customer_side,
                transaction_id=self.transaction_id,
                advance_amount=self.unallocated_amount,
            )
        except AuthorizationError as exc:
            QMessageBox.warning(self, "Payment data unavailable", str(exc))
            return
        if dialog.exec() != QDialog.Accepted:
            return
        allocation = dialog.payload()
        try:
            with get_db() as db:
                if self.customer_side:
                    apply_customer_advance(
                        db, self.transaction_id, allocation,
                        actor_context=_actor_context(),
                    )
                else:
                    apply_supplier_advance(
                        db, self.transaction_id, allocation,
                        actor_context=_actor_context(),
                    )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Could not apply advance", str(exc))


class AdvanceAllocationDialog(QDialog):
    def __init__(
        self,
        parent,
        *,
        customer_side: bool,
        transaction_id: int,
        advance_amount: Decimal,
    ):
        super().__init__(parent)
        self.customer_side = customer_side
        self.advance_amount = advance_amount
        self.setWindowTitle("Apply Customer Advance" if customer_side else "Apply Supplier Advance")
        layout = QFormLayout(self)
        self.target = QComboBox()
        self.amount = QLineEdit()
        self.amount.setValidator(QDoubleValidator(0, 999999999999.99, 2, self))
        self.amount.setPlaceholderText(f"Available advance: {advance_amount:.2f}")
        self.day = QDateEdit()
        self.day.setCalendarPopup(True)
        self.day.setDate(QDate.currentDate())
        self.reference = QLineEdit()
        layout.addRow("Outstanding invoice / purchase", self.target)
        layout.addRow("Amount to allocate (₹)", self.amount)
        layout.addRow("Allocation date", self.day)
        layout.addRow("Reference / note", self.reference)
        party_id = None
        with get_db() as db:
            if customer_side:
                transaction = get_customer_receipt_by_id(
                    db, transaction_id, actor_context=_actor_context()
                )
                party_id = transaction.customer_id if transaction else None
                targets = get_customer_receipt_targets(
                    db, party_id, actor_context=_actor_context()
                ) if party_id else []
            else:
                transaction = get_supplier_payment_by_id(
                    db, transaction_id, actor_context=_actor_context()
                )
                party_id = transaction.supplier_id if transaction else None
                targets = get_supplier_payment_targets(
                    db, party_id, actor_context=_actor_context()
                ) if party_id else []
        self.targets = targets
        for target in targets:
            amount_due = target["outstanding"]
            self.target.addItem(
                f"{target['code']} — Outstanding {amount_due:.2f}",
                target,
            )
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        if not targets:
            buttons.button(QDialogButtonBox.Ok).setEnabled(False)
            layout.addRow(QLabel("No open invoice or purchase balance is available."))

    def _validate(self):
        try:
            amount = Decimal(self.amount.text() or "0")
            if amount <= 0:
                raise PaymentValidationError("Allocation amount must be greater than zero.")
            if amount > self.advance_amount:
                raise PaymentValidationError("Allocation cannot exceed the available advance.")
            target = self.target.currentData()
            if target and amount > target["outstanding"]:
                raise PaymentValidationError("Allocation cannot exceed the target outstanding balance.")
        except (InvalidOperation, PaymentValidationError) as exc:
            QMessageBox.warning(self, "Invalid allocation", str(exc))
            return
        self.accept()

    def payload(self) -> dict:
        target = self.target.currentData()
        key = "invoice_id" if self.customer_side else "purchase_id"
        return {
            "allocation_type": target["allocation_type"],
            key: target.get(key),
            "amount": self.amount.text(),
            "allocation_date": self.day.date().toPython(),
            "reference": self.reference.text(),
        }


class PaymentRegisterTab(QWidget):
    def __init__(self, parent, *, customer_side: bool):
        super().__init__(parent)
        self.customer_side = customer_side
        self._loading = False
        layout = QVBoxLayout(self)
        dashboard = QGridLayout()
        self.metric_labels: dict[str, QLabel] = {}
        self.metric_definitions = (
            [
                ("total_receipts", "Receipts in period"),
                ("receipt_count", "Receipt count"),
                ("unallocated_advances", "Unallocated customer advances"),
                ("unpaid_invoices", "Unpaid invoices"),
                ("partially_paid_invoices", "Partially paid invoices"),
                ("overdue_invoices", "Overdue invoices"),
            ]
            if customer_side
            else [
                ("total_payments", "Payments in period"),
                ("payment_count", "Payment count"),
                ("unallocated_advances", "Unallocated supplier advances"),
                ("outstanding_purchases", "Outstanding purchases"),
            ]
        )
        for index, (key, label) in enumerate(self.metric_definitions):
            card = QFrame()
            card.setObjectName("paymentMetricCard")
            card_layout = QVBoxLayout(card)
            caption = QLabel(label)
            caption.setObjectName("muted")
            value = QLabel("—")
            value.setObjectName("paymentMetricValue")
            card_layout.addWidget(caption)
            card_layout.addWidget(value)
            dashboard.addWidget(card, index // 3, index % 3)
            self.metric_labels[key] = value
        layout.addLayout(dashboard)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search ID, party or transaction reference")
        self.party_filter = QComboBox()
        self.party_filter.addItem(
            "All customers" if customer_side else "All suppliers", None
        )
        self.access_error = None
        try:
            actor_context = _actor_context()
            with get_db() as db:
                require_permission(
                    actor_context,
                    "receipts.view" if customer_side else "supplier_payments.view",
                    db_session=db,
                )
                parties = db.scalars(
                    select(Customer if customer_side else Supplier).order_by(
                        Customer.customer_name if customer_side else Supplier.business_name
                    )
                ).all()
        except AuthorizationError as exc:
            self.access_error = str(exc)
            parties = []
        for party in parties:
            self.party_filter.addItem(
                f"{party.customer_name}" if customer_side else party.business_name,
                party.id,
            )
        self.party_filter.currentIndexChanged.connect(self.load_data)
        self.status_filter = QComboBox()
        self.status_filter.addItems(["ALL", "Draft", "Posted", "Voided"])
        self.method_filter = QComboBox()
        self.method_filter.addItem("All methods", "")
        for method in PAYMENT_METHODS:
            self.method_filter.addItem(method, method)
        self.start_date = QDateEdit()
        self.end_date = QDateEdit()
        for control in (self.start_date, self.end_date):
            control.setCalendarPopup(True)
            control.setDisplayFormat("dd-MMM-yyyy")
        self.start_date.setDate(QDate(2000, 1, 1))
        self.end_date.setDate(QDate.currentDate())
        for control in (
            self.search, self.party_filter, self.status_filter, self.method_filter,
            self.start_date, self.end_date
        ):
            filters.addWidget(control)
        self.search.returnPressed.connect(self.load_data)
        self.status_filter.currentIndexChanged.connect(self.load_data)
        self.method_filter.currentIndexChanged.connect(self.load_data)
        self.start_date.dateChanged.connect(self.load_data)
        self.end_date.dateChanged.connect(self.load_data)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "ID", "Customer" if customer_side else "Supplier", "Date", "Amount (₹)",
            "Allocated (₹)", "Advance (₹)", "Method", "Reference", "Status",
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self.table)
        self.access_warning = QLabel()
        self.access_warning.setObjectName("muted")
        self.access_warning.setWordWrap(True)
        layout.addWidget(self.access_warning)

        actions = QHBoxLayout()
        self.create_button = QPushButton(
            "New Receipt" if customer_side else "New Supplier Payment"
        )
        self.edit_button = QPushButton("Edit Draft")
        self.view_button = QPushButton("View / Post / Void")
        self.export_pdf_button = QPushButton("Export PDF")
        self.export_excel_button = QPushButton("Export Excel")
        for button in (
            self.create_button, self.edit_button, self.view_button,
            self.export_pdf_button, self.export_excel_button,
        ):
            actions.addWidget(button)
        if self.access_error:
            for button in (
                self.create_button, self.edit_button, self.view_button,
                self.export_pdf_button, self.export_excel_button,
            ):
                button.setEnabled(False)
            self.access_warning.setText(
                f"Payment data is unavailable: {self.access_error}"
            )
        actions.addStretch()
        layout.addLayout(actions)
        self.create_button.clicked.connect(self._create)
        self.edit_button.clicked.connect(self._edit)
        self.view_button.clicked.connect(self._view)
        self.export_pdf_button.clicked.connect(self._export_pdf)
        self.export_excel_button.clicked.connect(self._export_excel)
        permission_prefix = "receipts" if customer_side else "supplier_payments"
        self.create_button.setEnabled(
            not self.access_error
            and _has_permission(f"{permission_prefix}.manage")
        )
        self.edit_button.setEnabled(
            not self.access_error
            and _has_permission(f"{permission_prefix}.manage")
        )
        self.export_pdf_button.setEnabled(
            not self.access_error
            and _has_permission("receipts.view")
            and _has_permission("supplier_payments.view")
        )
        self.export_excel_button.setEnabled(
            not self.access_error
            and _has_permission("receipts.view")
            and _has_permission("supplier_payments.view")
        )
        self.load_data()

    def _date(self, control: QDateEdit) -> date | None:
        value = control.date().toPython()
        return None if value == _NO_DATE.toPython() else value

    def load_data(self, *_args):
        if self._loading:
            return
        if self.access_error:
            self.table.setRowCount(0)
            for label in self.metric_labels.values():
                label.clear()
            self.access_warning.setText(
                f"Payment data is unavailable: {self.access_error}"
            )
            return
        self._loading = True
        try:
            start_date, end_date = self._date(self.start_date), self._date(self.end_date)
            with get_db() as db:
                if self.customer_side:
                    dashboard = get_customer_receipt_dashboard(
                        db, actor_context=_actor_context(),
                        start_date=start_date, end_date=end_date
                    )
                    rows = list_customer_receipts(
                        db,
                        actor_context=_actor_context(),
                        customer_id=self.party_filter.currentData(),
                        payment_method_filter=self.method_filter.currentData() or None,
                        status=self.status_filter.currentText(),
                        start_date=start_date,
                        end_date=end_date,
                        search=self.search.text() or None,
                    )
                else:
                    dashboard = get_supplier_payment_dashboard(
                        db, actor_context=_actor_context(),
                        start_date=start_date, end_date=end_date
                    )
                    rows = list_supplier_payments(
                        db,
                        actor_context=_actor_context(),
                        supplier_id=self.party_filter.currentData(),
                        payment_method_filter=self.method_filter.currentData() or None,
                        status=self.status_filter.currentText(),
                        start_date=start_date,
                        end_date=end_date,
                        search=self.search.text() or None,
                    )
                for key, _label in self.metric_definitions:
                    value = dashboard[key]
                    self.metric_labels[key].setText(
                        format_currency(value)
                        if isinstance(value, Decimal) else str(value)
                    )
                self.table.setRowCount(len(rows))
                for row_index, transaction in enumerate(rows):
                    summary = (
                        get_customer_receipt_summary(
                            db, transaction, actor_context=_actor_context()
                        )
                        if self.customer_side else get_supplier_payment_summary(
                            db, transaction, actor_context=_actor_context()
                        )
                    )
                    cells = [
                        transaction.receipt_code if self.customer_side else transaction.payment_code,
                        transaction.customer.customer_name if self.customer_side else transaction.supplier.business_name,
                        transaction.receipt_date if self.customer_side else transaction.payment_date,
                        transaction.amount_received if self.customer_side else transaction.amount_paid,
                        summary["allocated_amount"], summary["unallocated_amount"],
                        transaction.payment_method, transaction.transaction_reference or "",
                        transaction.status,
                    ]
                    for column, value in enumerate(cells):
                        rendered = (
                            format_currency(value)
                            if column in (3, 4, 5)
                            else str(value)
                        )
                        item = QTableWidgetItem(rendered)
                        if column == 0:
                            item.setData(Qt.UserRole, transaction.id)
                        self.table.setItem(row_index, column, item)
        except AuthorizationError as exc:
            self.table.setRowCount(0)
            for label in self.metric_labels.values():
                label.clear()
            self.access_error = str(exc)
            for button in (
                self.create_button, self.edit_button, self.view_button,
                self.export_pdf_button, self.export_excel_button,
            ):
                button.setEnabled(False)
            self.access_warning.setText(
                f"Payment data is unavailable: {exc}"
            )
            QMessageBox.warning(self, "Payment register unavailable", str(exc))
        except Exception as exc:
            self.table.setRowCount(0)
            for label in self.metric_labels.values():
                label.clear()
            self.access_warning.setText(
                f"Payment data could not be loaded: {exc}"
            )
            QMessageBox.critical(self, "Could not load payment register", str(exc))
        finally:
            self._loading = False

    def _selected_id(self) -> int | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def _create(self):
        dialog = PaymentEntryDialog(self, customer_side=self.customer_side)
        if dialog.exec() != QDialog.Accepted:
            return
        payload, allocations = dialog.payload()
        try:
            with get_db() as db:
                if self.customer_side:
                    create_customer_receipt(
                        db, payload, allocations, actor_context=_actor_context()
                    )
                else:
                    create_supplier_payment(
                        db, payload, allocations, actor_context=_actor_context()
                    )
            self.load_data()
        except Exception as exc:
            QMessageBox.critical(self, "Could not save draft", str(exc))

    def _edit(self):
        transaction_id = self._selected_id()
        if transaction_id is None:
            QMessageBox.information(self, "Select a record", "Select a draft transaction to edit.")
            return
        try:
            with get_db() as db:
                transaction = (
                    get_customer_receipt_by_id(
                        db, transaction_id, actor_context=_actor_context()
                    )
                    if self.customer_side else get_supplier_payment_by_id(
                        db, transaction_id, actor_context=_actor_context()
                    )
                )
                if transaction is None or transaction.status != "Draft":
                    raise PaymentValidationError("Only draft transactions can be edited.")
                existing = {
                    "id": transaction.id,
                    "party_id": transaction.customer_id if self.customer_side else transaction.supplier_id,
                    "date": (transaction.receipt_date if self.customer_side else transaction.payment_date).isoformat(),
                    "amount": _decimal_text(transaction.amount_received if self.customer_side else transaction.amount_paid),
                    "method": transaction.payment_method,
                    "reference": transaction.transaction_reference,
                    "bank": transaction.bank_account_name,
                    "cheque": transaction.cheque_number,
                    "notes": transaction.notes,
                }
            dialog = PaymentEntryDialog(
                self, customer_side=self.customer_side, existing=existing
            )
            if dialog.exec() != QDialog.Accepted:
                return
            payload, allocations = dialog.payload()
            with get_db() as db:
                if self.customer_side:
                    update_customer_receipt(
                        db, transaction_id, payload, allocations,
                        actor_context=_actor_context(),
                    )
                else:
                    update_supplier_payment(
                        db, transaction_id, payload, allocations,
                        actor_context=_actor_context(),
                    )
            self.load_data()
        except Exception as exc:
            QMessageBox.critical(self, "Could not edit draft", str(exc))

    def _view(self):
        transaction_id = self._selected_id()
        if transaction_id is None:
            QMessageBox.information(self, "Select a record", "Select a transaction to view.")
            return
        try:
            dialog = PaymentDetailDialog(
                self, customer_side=self.customer_side, transaction_id=transaction_id
            )
        except AuthorizationError as exc:
            QMessageBox.warning(self, "Payment data unavailable", str(exc))
            return
        dialog.exec()
        self.load_data()

    def _export_pdf(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save payment register", "payments-register.pdf", "PDF files (*.pdf)"
        )
        if not path:
            return
        try:
            with get_db() as db:
                generate_payment_pdf_report(
                    db, path, actor_context=_actor_context(),
                    start_date=self._date(self.start_date), end_date=self._date(self.end_date)
                )
            QMessageBox.information(self, "Report created", f"Saved to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Could not export PDF", str(exc))

    def _export_excel(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save payment reports", "payments-register.xlsx", "Excel files (*.xlsx)"
        )
        if not path:
            return
        try:
            with get_db() as db:
                generate_payment_excel_report(
                    db, path, actor_context=_actor_context(),
                    start_date=self._date(self.start_date), end_date=self._date(self.end_date)
                )
            QMessageBox.information(self, "Report created", f"Saved to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Could not export Excel", str(exc))


class PaymentsReceiptPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        heading = QLabel("Payments & Receipts")
        heading.setObjectName("heading")
        note = QLabel(
            "Record customer cash receipts and supplier disbursements. Drafts do not affect balances; "
            "posted allocations are auditable. This is not a General Ledger."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(note)
        self.tabs = QTabWidget()
        self.customer_tab = PaymentRegisterTab(self, customer_side=True)
        self.supplier_tab = PaymentRegisterTab(self, customer_side=False)
        self.tabs.addTab(self.customer_tab, "Customer Receipts")
        self.tabs.addTab(self.supplier_tab, "Supplier Payments")
        layout.addWidget(self.tabs)

    def load_data(self):
        self.customer_tab.load_data()
        self.supplier_tab.load_data()
