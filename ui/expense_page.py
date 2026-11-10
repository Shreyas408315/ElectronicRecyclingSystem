from datetime import date
from decimal import Decimal

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QDoubleValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from sqlalchemy import select

from database.connection import get_db
from database.models import ExpenseCategory, Supplier
from services.expense_common import (
    ExpenseValidationError,
    PAYMENT_METHODS,
)
from services.expense_payment_service import (
    create_draft_expense_payment,
    get_expense_payment_by_id,
    get_expense_payment_history,
    get_expense_payment_summary,
    post_expense_payment,
    void_expense_payment,
)
from services.expense_report_service import (
    generate_expense_excel_report,
    generate_expense_pdf,
    generate_expense_pdf_report,
)
from services.expense_service import (
    calculate_expense_summary,
    create_draft_expense,
    create_expense_category,
    deactivate_expense_category,
    get_expense_by_id,
    list_expense_categories,
    list_expenses,
    post_expense,
    seed_default_expense_categories,
    update_draft_expense,
    void_expense,
)
from services.settings_service import get_active_payment_accounts
from services.security_service import (
    AuthorizationError,
    current_actor_context,
    require_permission,
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


def _optional_date(control: QDateEdit) -> date | None:
    value = control.date().toPython()
    return None if value == _NO_DATE.toPython() else value


class ExpenseCategoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Expense Categories")
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Category", "Description", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        add = QPushButton("Add")
        rename = QPushButton("Edit selected")
        deactivate = QPushButton("Deactivate selected")
        close = QPushButton("Close")
        for button in (add, rename, deactivate):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(close)
        layout.addLayout(actions)
        add.clicked.connect(self._add)
        rename.clicked.connect(self._rename)
        deactivate.clicked.connect(self._deactivate)
        can_manage = _has_permission("expenses.manage")
        add.setEnabled(can_manage)
        rename.setEnabled(can_manage)
        deactivate.setEnabled(can_manage)
        close.clicked.connect(self.accept)
        self.load_data()

    def load_data(self):
        with get_db() as db:
            categories = list_expense_categories(
                db, active_only=False, actor_context=_actor_context()
            )
        self.table.setRowCount(len(categories))
        for row, category in enumerate(categories):
            for col, value in enumerate((
                category.name, category.description or "",
                "Active" if category.is_active else "Inactive",
            )):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, category.id)
                self.table.setItem(row, col, item)

    def _selected(self) -> int | None:
        row = self.table.currentRow()
        return self.table.item(row, 0).data(Qt.UserRole) if row >= 0 else None

    def _add(self):
        name, ok = QInputDialog.getText(self, "Add category", "Category name:")
        if not ok:
            return
        description, _ = QInputDialog.getText(self, "Add category", "Description (optional):")
        try:
            with get_db() as db:
                create_expense_category(
                    db, name, description, actor_context=_actor_context()
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not add category", str(exc))

    def _rename(self):
        category_id = self._selected()
        if category_id is None:
            return
        with get_db() as db:
            category = db.get(ExpenseCategory, category_id)
            current_name, current_description = category.name, category.description or ""
        name, ok = QInputDialog.getText(self, "Edit category", "Category name:", text=current_name)
        if not ok:
            return
        description, ok = QInputDialog.getText(
            self, "Edit category", "Description:", text=current_description
        )
        if not ok:
            return
        try:
            from services.expense_service import update_expense_category
            with get_db() as db:
                update_expense_category(
                    db, category_id, name=name, description=description,
                    actor_context=_actor_context(),
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not edit category", str(exc))

    def _deactivate(self):
        category_id = self._selected()
        if category_id is None:
            return
        if QMessageBox.question(
            self, "Deactivate category",
            "The category will remain attached to historical expenses. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                deactivate_expense_category(
                    db, category_id, actor_context=_actor_context()
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not deactivate category", str(exc))


class ExpenseFormDialog(QDialog):
    def __init__(self, parent=None, expense_id: int | None = None):
        super().__init__(parent)
        self.expense_id = expense_id
        self._submitting = False
        self.setWindowTitle("Edit Draft Expense" if expense_id else "New Expense")
        self.resize(840, 620)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.expense_date = QDateEdit()
        self.expense_date.setCalendarPopup(True)
        self.expense_date.setDate(QDate.currentDate())
        self.due_date = QDateEdit()
        self.due_date.setCalendarPopup(True)
        self.due_date.setSpecialValueText("No due date")
        self.due_date.setDate(_NO_DATE)
        self.due_enabled = QCheckBox("Set due date")
        self.due_enabled.toggled.connect(self.due_date.setEnabled)
        self.due_date.setEnabled(False)
        due_row = QHBoxLayout()
        due_row.addWidget(self.due_enabled)
        due_row.addWidget(self.due_date)
        self.title = QLineEdit()
        self.category = QComboBox()
        self.payee = QLineEdit()
        self.supplier = QComboBox()
        self.supplier.addItem("No supplier link", None)
        self.reference = QLineEdit()
        self.description = QLineEdit()
        self.department = QLineEdit()
        self.notes = QLineEdit()
        self.attachment = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        attach_row = QHBoxLayout()
        attach_row.addWidget(self.attachment)
        attach_row.addWidget(browse)
        form.addRow("Expense date *", self.expense_date)
        form.addRow("Due date", due_row)
        form.addRow("Title *", self.title)
        form.addRow("Category *", self.category)
        form.addRow("Payee/vendor *", self.payee)
        form.addRow("Link supplier", self.supplier)
        form.addRow("Vendor reference", self.reference)
        form.addRow("Description", self.description)
        form.addRow("Department / cost centre", self.department)
        form.addRow("Notes", self.notes)
        form.addRow("Supporting document", attach_row)
        root.addLayout(form)

        self.items = QTableWidget(0, 7)
        self.items.setHorizontalHeaderLabels([
            "Line description", "Quantity", "Unit", "Unit price",
            "Discount", "Tax %", "Category",
        ])
        self.items.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 7):
            self.items.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        root.addWidget(QLabel("Optional itemization (leave empty for a single-amount expense):"))
        root.addWidget(self.items, 1)
        line_actions = QHBoxLayout()
        add_line = QPushButton("Add line")
        remove_line = QPushButton("Remove selected line")
        add_line.clicked.connect(self._add_line)
        remove_line.clicked.connect(lambda: self.items.removeRow(self.items.currentRow()))
        line_actions.addWidget(add_line)
        line_actions.addWidget(remove_line)
        line_actions.addStretch()
        root.addLayout(line_actions)

        totals = QFormLayout()
        self.subtotal = QLineEdit()
        self.subtotal.setValidator(QDoubleValidator(0, 999999999999.99, 2, self))
        self.subtotal.setPlaceholderText("Required when no item lines are entered")
        self.discount = QLineEdit("0")
        self.charges = QLineEdit("0")
        self.tax = QLineEdit("0")
        for field in (self.discount, self.charges, self.tax):
            field.setValidator(QDoubleValidator(0, 999999999999.99, 2, self))
        totals.addRow("Single-amount subtotal", self.subtotal)
        totals.addRow("Additional discount", self.discount)
        totals.addRow("Additional charges", self.charges)
        totals.addRow("Tax (single-amount expenses)", self.tax)
        root.addLayout(totals)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Save Draft")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._load_choices()
        if expense_id:
            self._load_expense()

    def _load_choices(self):
        with get_db() as db:
            categories = list_expense_categories(
                db, actor_context=_actor_context()
            )
            suppliers = db.scalars(
                select(Supplier).where(Supplier.is_active.is_(True))
                .order_by(Supplier.business_name, Supplier.id)
            ).all()
        for category in categories:
            self.category.addItem(category.name, category.id)
        for supplier in suppliers:
            self.supplier.addItem(
                f"{supplier.supplier_code} — {supplier.business_name}", supplier.id
            )

    def _load_expense(self):
        with get_db() as db:
            expense = get_expense_by_id(
                db, self.expense_id, actor_context=_actor_context()
            )
            if expense is None or expense.status != "Draft":
                raise ExpenseValidationError("Only existing draft expenses can be edited.")
            data = {
                "date": expense.expense_date,
                "due": expense.due_date,
                "title": expense.title,
                "category_id": expense.category_id,
                "payee": expense.payee_name,
                "supplier_id": expense.supplier_id,
                "reference": expense.vendor_reference,
                "description": expense.description,
                "department": expense.department,
                "notes": expense.notes,
                "attachment": expense.attachment_path,
                "subtotal": expense.subtotal,
                "discount": expense.discount,
                "charges": expense.additional_charges,
                "tax": expense.tax_amount,
                "items": [{
                    "description": item.description, "quantity": item.quantity,
                    "unit": item.unit, "unit_price": item.unit_price,
                    "discount": item.discount, "tax_rate": item.tax_rate,
                    "category_name": item.category_name,
                } for item in expense.items],
            }
        self.expense_date.setDate(QDate(data["date"].year, data["date"].month, data["date"].day))
        if data["due"]:
            self.due_enabled.setChecked(True)
            self.due_date.setDate(QDate(data["due"].year, data["due"].month, data["due"].day))
        self.title.setText(data["title"])
        self.category.setCurrentIndex(self.category.findData(data["category_id"]))
        self.payee.setText(data["payee"])
        self.supplier.setCurrentIndex(max(0, self.supplier.findData(data["supplier_id"])))
        self.reference.setText(data["reference"] or "")
        self.description.setText(data["description"] or "")
        self.department.setText(data["department"] or "")
        self.notes.setText(data["notes"] or "")
        self.attachment.setText(data["attachment"] or "")
        self.subtotal.setText(str(data["subtotal"]))
        self.discount.setText(str(data["discount"]))
        self.charges.setText(str(data["charges"]))
        self.tax.setText(str(data["tax"]))
        for item in data["items"]:
            self._add_line(item)

    def _add_line(self, values: dict | None = None):
        row = self.items.rowCount()
        self.items.insertRow(row)
        defaults = values or {
            "description": "", "quantity": "1", "unit": "unit",
            "unit_price": "0", "discount": "0", "tax_rate": "0",
            "category_name": "",
        }
        for col, key in enumerate((
            "description", "quantity", "unit", "unit_price", "discount",
            "tax_rate", "category_name",
        )):
            self.items.setItem(row, col, QTableWidgetItem(str(defaults[key])))

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select supporting document")
        if path:
            self.attachment.setText(path)

    def _payload(self) -> tuple[dict, list[dict]]:
        payload = {
            "expense_date": self.expense_date.date().toPython(),
            "due_date": self.due_date.date().toPython() if self.due_enabled.isChecked() else None,
            "title": self.title.text(),
            "category_id": self.category.currentData(),
            "payee_name": self.payee.text(),
            "supplier_id": self.supplier.currentData(),
            "vendor_reference": self.reference.text(),
            "description": self.description.text(),
            "department": self.department.text(),
            "notes": self.notes.text(),
            "attachment_path": self.attachment.text(),
            "subtotal": self.subtotal.text() or "0",
            "discount": self.discount.text() or "0",
            "additional_charges": self.charges.text() or "0",
            "tax_amount": self.tax.text() or "0",
        }
        items = []
        for row in range(self.items.rowCount()):
            values = [self.items.item(row, col).text().strip() if self.items.item(row, col) else "" for col in range(7)]
            items.append(dict(zip((
                "description", "quantity", "unit", "unit_price", "discount",
                "tax_rate", "category_name",
            ), values)))
        return payload, items

    def _save(self):
        if self._submitting:
            return
        self._submitting = True
        try:
            payload, items = self._payload()
            with get_db() as db:
                if self.expense_id:
                    update_draft_expense(
                        db, self.expense_id, payload, items,
                        actor_context=_actor_context(),
                    )
                else:
                    create_draft_expense(
                        db, payload, items, actor_context=_actor_context()
                    )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Could not save expense draft", str(exc))
        finally:
            self._submitting = False


class ExpensePaymentDialog(QDialog):
    def __init__(self, parent, expense_id: int):
        super().__init__(parent)
        self.expense_id = expense_id
        self.setWindowTitle("Record Expense Payment")
        layout = QFormLayout(self)
        self.day = QDateEdit()
        self.day.setCalendarPopup(True)
        self.day.setDate(QDate.currentDate())
        self.amount = QLineEdit()
        self.amount.setValidator(QDoubleValidator(0, 999999999999.99, 2, self))
        self.method = QComboBox()
        self.method.addItems(PAYMENT_METHODS)
        self.reference = QLineEdit()
        self.account = QComboBox()
        self.account.setEditable(True)
        self.account.setInsertPolicy(QComboBox.NoInsert)
        with get_db() as db:
            self.account.addItems(
                account.display_name for account in get_active_payment_accounts(
                    db, actor_context=_actor_context()
                )
            )
        self.account.setCurrentIndex(-1)
        self.account.setEditText("")
        self.cheque = QLineEdit()
        self.notes = QLineEdit()
        layout.addRow("Payment date", self.day)
        layout.addRow("Amount paid *", self.amount)
        layout.addRow("Payment method", self.method)
        layout.addRow("Transaction reference", self.reference)
        layout.addRow("Bank/account name", self.account)
        layout.addRow("Cheque number", self.cheque)
        layout.addRow("Notes", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save_button = buttons.button(QDialogButtonBox.Save)
        save_button.setText("Save and Post")
        save_button.setEnabled(
            _has_permission("expenses.manage")
            and _has_permission("expenses.post")
        )
        buttons.accepted.connect(self._save_and_post)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _save_and_post(self):
        try:
            payload = {
                "expense_id": self.expense_id,
                "payment_date": self.day.date().toPython(),
                "amount_paid": self.amount.text(),
                "payment_method": self.method.currentText(),
                "transaction_reference": self.reference.text(),
                "bank_account_name": self.account.currentText(),
                "cheque_number": self.cheque.text(),
                "notes": self.notes.text(),
            }
            if QMessageBox.question(
                self, "Confirm payment posting",
                "This records an actual payment and changes the outstanding balance. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
            with get_db() as db:
                actor_context = _actor_context()
                payment = create_draft_expense_payment(
                    db, payload, actor_context=actor_context
                )
                post_expense_payment(
                    db, payment.id, actor_context=actor_context
                )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Could not record payment", str(exc))


class ExpenseDetailDialog(QDialog):
    def __init__(self, parent, expense_id: int):
        super().__init__(parent)
        self.expense_id = expense_id
        self.setWindowTitle("Expense Details")
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        with get_db() as db:
            expense = get_expense_by_id(
                db, expense_id, actor_context=_actor_context()
            )
            if expense is None:
                raise ExpenseValidationError("Expense record no longer exists.")
            payment_summary = get_expense_payment_summary(
                db, expense.id, actor_context=_actor_context()
            )
            paid = payment_summary["total_paid"]
            balance = (
                payment_summary["outstanding"]
                if expense.status == "Posted" else expense.total_amount
            )
            text = (
                f"<h2>{expense.expense_code} — {expense.title}</h2>"
                f"<b>{expense.expense_date}</b> · {expense.category.name} · "
                f"{expense.payee_name} · <b>{expense.status}</b><br>"
                f"Total {format_currency(expense.total_amount)} · "
                f"Paid {format_currency(paid)} · Outstanding {format_currency(balance)} · "
                f"{payment_summary['payment_status']}<br>"
                f"Supplier: {expense.supplier.business_name if expense.supplier else '—'} · "
                f"Vendor reference: {expense.vendor_reference or '—'} · "
                f"Due: {expense.due_date or '—'}<br>"
                f"Subtotal {format_currency(expense.subtotal)} · "
                f"Discount {format_currency(expense.discount)} · "
                f"Tax {format_currency(expense.tax_amount)} · "
                f"Additional charges {format_currency(expense.additional_charges)}"
            )
            detail_label = QLabel(text)
            detail_label.setWordWrap(True)
            layout.addWidget(detail_label)
            if expense.description:
                layout.addWidget(QLabel(expense.description))
            self.attachment_path = expense.attachment_path
            self.status = expense.status
            self.items_data = [
                (item.description, item.quantity, item.unit, item.unit_price,
                 item.discount, item.tax_amount, item.line_total)
                for item in expense.items
            ]
            payments = get_expense_payment_history(
                db, expense.id, actor_context=_actor_context()
            )
            expense_history = [(h.event_type, h.from_status, h.to_status, h.reason, h.created_at) for h in expense.history]
        if self.items_data:
            lines = QTableWidget(len(self.items_data), 7)
            lines.setHorizontalHeaderLabels([
                "Description", "Qty", "Unit", "Rate", "Discount", "Tax", "Total",
            ])
            for row, values in enumerate(self.items_data):
                for col, value in enumerate(values):
                    lines.setItem(row, col, QTableWidgetItem(str(value)))
            lines.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            layout.addWidget(lines)
        self.payments_table = QTableWidget(len(payments), 6)
        self.payments_table.setHorizontalHeaderLabels([
            "Payment ID", "Date", "Amount", "Method", "Reference", "Status",
        ])
        for row, payment in enumerate(payments):
            values = (
                payment.payment_code, payment.payment_date, format_currency(payment.amount_paid),
                payment.payment_method, payment.transaction_reference or "", payment.status,
            )
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.UserRole, payment.id)
                self.payments_table.setItem(row, col, cell)
        self.payments_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.payments_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(QLabel("Expense payment history"))
        layout.addWidget(self.payments_table)
        self.history_table = QTableWidget(len(expense_history), 5)
        self.history_table.setHorizontalHeaderLabels(["Event", "From", "To", "Reason", "Date"])
        for row, values in enumerate(expense_history):
            for col, value in enumerate(values):
                self.history_table.setItem(row, col, QTableWidgetItem(str(value or "")))
        layout.addWidget(QLabel("Expense status history"))
        layout.addWidget(self.history_table)
        actions = QHBoxLayout()
        self.payment_button = QPushButton("Record Payment")
        self.payment_button.setEnabled(
            self.status == "Posted" and balance > 0
            and _has_permission("expenses.manage")
            and _has_permission("expenses.post")
        )
        self.payment_button.clicked.connect(self._record_payment)
        self.void_payment_button = QPushButton("Void Selected Payment")
        self.void_payment_button.setEnabled(_has_permission("expenses.void"))
        self.void_payment_button.clicked.connect(self._void_payment)
        self.view_payment_button = QPushButton("View Payment History")
        self.view_payment_button.setEnabled(_has_permission("expenses.view"))
        self.view_payment_button.clicked.connect(self._view_payment)
        self.pdf_button = QPushButton("Expense PDF")
        self.pdf_button.clicked.connect(self._pdf)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        for button in (
            self.payment_button, self.void_payment_button, self.pdf_button,
            self.view_payment_button, self.close_button,
        ):
            actions.addWidget(button)
        layout.addLayout(actions)

    def _record_payment(self):
        dialog = ExpensePaymentDialog(self, self.expense_id)
        if dialog.exec() == QDialog.Accepted:
            self.accept()

    def _void_payment(self):
        row = self.payments_table.currentRow()
        payment_id = (
            self.payments_table.item(row, 0).data(Qt.UserRole)
            if row >= 0 else None
        )
        if payment_id is None:
            QMessageBox.information(self, "Select payment", "Select a payment to void.")
            return
        reason, accepted = QInputDialog.getText(
            self, "Void expense payment", "Mandatory void reason:"
        )
        if not accepted or not reason.strip():
            return
        if QMessageBox.question(
            self, "Confirm void", "Void this posted expense payment?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                void_expense_payment(
                    db, payment_id, reason, actor_context=_actor_context()
                )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Could not void payment", str(exc))

    def _view_payment(self):
        row = self.payments_table.currentRow()
        payment_id = (
            self.payments_table.item(row, 0).data(Qt.UserRole)
            if row >= 0 else None
        )
        if payment_id is None:
            QMessageBox.information(self, "Select payment", "Select a payment to view.")
            return
        try:
            with get_db() as db:
                payment = get_expense_payment_by_id(
                    db, payment_id, actor_context=_actor_context()
                )
                if payment is None:
                    raise ExpenseValidationError("Expense payment no longer exists.")
                history = list(payment.history)
                summary = "\n".join(
                    f"{entry.created_at}: {entry.event_type} "
                    f"({entry.from_status or '—'} → {entry.to_status or '—'})"
                    f"{': ' + entry.reason if entry.reason else ''}"
                    for entry in history
                )
                detail = (
                    f"{payment.payment_code}\nDate: {payment.payment_date}\n"
                    f"Amount: {format_currency(payment.amount_paid)}\n"
                    f"Method: {payment.payment_method}\n"
                    f"Reference: {payment.transaction_reference or '—'}\n"
                    f"Status: {payment.status}\n"
                    f"Notes: {payment.notes or '—'}\n"
                    f"Void reason: {payment.void_reason or '—'}\n\n"
                    f"Payment audit history:\n{summary or 'No history recorded.'}"
                )
            QMessageBox.information(self, "Expense payment detail", detail)
        except Exception as exc:
            QMessageBox.warning(self, "Could not view payment", str(exc))

    def _pdf(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save expense PDF", f"expense-{self.expense_id}.pdf",
            "PDF files (*.pdf)",
        )
        if not path:
            return
        try:
            with get_db() as db:
                generate_expense_pdf(
                    db, self.expense_id, path,
                    actor_context=_actor_context(),
                )
            QMessageBox.information(self, "PDF created", f"Saved to:\n{path}")
        except Exception as exc:
            QMessageBox.warning(self, "Could not create PDF", str(exc))


class ExpensePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        if _has_permission("expenses.manage"):
            with get_db() as db:
                seed_default_expense_categories(
                    db, actor_context=_actor_context()
                )
        self._init_ui()
        self.load_expenses()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Expenses Management")
        title.setObjectName("supplierPageTitle")
        subtitle = QLabel("Track factory operating costs separately from purchases and supplier payments.")
        subtitle.setObjectName("supplierPageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        header_buttons = {}
        for label, callback in (
            ("Categories", self._categories),
            ("PDF / Excel Reports", self._reports),
            ("+ Add Expense", self._create),
        ):
            button = QPushButton(label)
            if label == "+ Add Expense":
                button.setObjectName("primaryButton")
            button.clicked.connect(callback)
            header.addWidget(button)
            header_buttons[label] = button
        self.category_button = header_buttons["Categories"]
        self.report_button = header_buttons["PDF / Excel Reports"]
        self.add_expense_button = header_buttons["+ Add Expense"]
        layout.addLayout(header)
        self.access_warning = QLabel()
        self.access_warning.setObjectName("muted")
        self.access_warning.setWordWrap(True)
        layout.addWidget(self.access_warning)

        self.metrics: dict[str, QLabel] = {}
        cards = QGridLayout()
        for index, (key, caption) in enumerate((
            ("total_expenses", "Posted expenses"),
            ("total_payments", "Payments made"),
            ("outstanding", "Expense payables"),
            ("unpaid_count", "Unpaid"),
            ("partially_paid_count", "Partially paid"),
            ("overdue_count", "Overdue"),
        )):
            frame = QFrame()
            frame.setObjectName("metricsContainer")
            box = QVBoxLayout(frame)
            box.addWidget(QLabel(caption))
            value = QLabel("0")
            value.setObjectName("metricBigValue")
            box.addWidget(value)
            cards.addWidget(frame, index // 3, index % 3)
            self.metrics[key] = value
        layout.addLayout(cards)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search ID, title, payee or reference")
        self.search.textChanged.connect(self.load_expenses)
        filters.addWidget(self.search, 2)
        self.category_filter = QComboBox()
        self.category_filter.addItem("All categories", None)
        self.category_filter.currentIndexChanged.connect(self.load_expenses)
        filters.addWidget(self.category_filter)
        self.payee_filter = QLineEdit()
        self.payee_filter.setPlaceholderText("Payee")
        self.payee_filter.textChanged.connect(self.load_expenses)
        filters.addWidget(self.payee_filter)
        self.status_filter = QComboBox()
        self.status_filter.addItems(["ALL", "Draft", "Posted", "Voided"])
        self.status_filter.currentIndexChanged.connect(self.load_expenses)
        filters.addWidget(self.status_filter)
        self.payment_filter = QComboBox()
        self.payment_filter.addItems([
            "ALL", "Unpaid", "Partially Paid", "Paid", "Overdue", "Not Payable",
        ])
        self.payment_filter.currentIndexChanged.connect(self.load_expenses)
        filters.addWidget(self.payment_filter)
        layout.addLayout(filters)
        date_row = QHBoxLayout()
        self.date_from = QDateEdit()
        self.date_to = QDateEdit()
        for control, prompt, default in (
            (self.date_from, "From", _NO_DATE),
            (self.date_to, "To", _NO_DATE),
        ):
            control.setCalendarPopup(True)
            control.setSpecialValueText(prompt)
            control.setDate(default)
            control.dateChanged.connect(self.load_expenses)
            date_row.addWidget(control)
        date_row.addStretch()
        layout.addLayout(date_row)

        actions = QHBoxLayout()
        self.count_label = QLabel("Showing 0 expenses")
        actions.addWidget(self.count_label)
        actions.addStretch()
        action_buttons = {}
        for label, callback in (
            ("Details / Payments", self._details),
            ("Edit Draft", self._edit),
            ("Post", self._post),
            ("Void", self._void),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
            action_buttons[label] = button
        self.details_button = action_buttons["Details / Payments"]
        self.edit_expense_button = action_buttons["Edit Draft"]
        self.post_expense_button = action_buttons["Post"]
        self.void_expense_button = action_buttons["Void"]
        layout.addLayout(actions)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels([
            "Expense ID", "Date", "Title", "Category", "Payee",
            "Total", "Paid", "Outstanding", "Payment status", "Status",
        ])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.doubleClicked.connect(lambda *_: self._details())
        layout.addWidget(self.table, 1)

    def _load_categories(self):
        selected = self.category_filter.currentData()
        with get_db() as db:
            categories = list_expense_categories(
                db, actor_context=_actor_context()
            )
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("All categories", None)
        for category in categories:
            self.category_filter.addItem(category.name, category.id)
        if selected is not None:
            self.category_filter.setCurrentIndex(
                max(0, self.category_filter.findData(selected))
            )
        self.category_filter.blockSignals(False)

    def load_expenses(self, *_):
        if self._loading or not hasattr(self, "table"):
            return
        self._loading = True
        try:
            has_view = _has_permission("expenses.view")
            self.category_button.setEnabled(_has_permission("expenses.manage"))
            self.report_button.setEnabled(has_view)
            self.details_button.setEnabled(has_view)
            self.add_expense_button.setEnabled(_has_permission("expenses.manage"))
            self.edit_expense_button.setEnabled(_has_permission("expenses.manage"))
            self.post_expense_button.setEnabled(_has_permission("expenses.post"))
            self.void_expense_button.setEnabled(_has_permission("expenses.void"))
            if not has_view:
                self.table.setRowCount(0)
                for label in self.metrics.values():
                    label.clear()
                self.count_label.setText("Expense data unavailable")
                message = (
                    "Sign in is required."
                    if current_actor_context() is None
                    else "Expense view permission is required."
                )
                self.access_warning.setText(message)
                return
            self._load_categories()
            start, end = _optional_date(self.date_from), _optional_date(self.date_to)
            with get_db() as db:
                actor_context = _actor_context()
                metrics = calculate_expense_summary(
                    db, actor_context=actor_context,
                    start_date=start, end_date=end,
                )
                expenses = list_expenses(
                    db,
                    actor_context=actor_context,
                    search=self.search.text() or None,
                    category_id=self.category_filter.currentData(),
                    payee=self.payee_filter.text() or None,
                    status=self.status_filter.currentText(),
                    payment_status=self.payment_filter.currentText(),
                    start_date=start, end_date=end,
                )
                for key, label in self.metrics.items():
                    value = metrics[key]
                    label.setText(
                        format_currency(value)
                        if key in ("total_expenses", "total_payments", "outstanding")
                        else str(value)
                    )
                self.table.setRowCount(len(expenses))
                for row, expense in enumerate(expenses):
                    payment_summary = get_expense_payment_summary(
                        db, expense.id, actor_context=actor_context
                    )
                    paid = payment_summary["total_paid"]
                    balance = (
                        payment_summary["outstanding"]
                        if expense.status == "Posted" else expense.total_amount
                    )
                    values = (
                        expense.expense_code, expense.expense_date, expense.title,
                        expense.category.name, expense.payee_name,
                        format_currency(expense.total_amount), format_currency(paid),
                        format_currency(balance), payment_summary["payment_status"],
                        expense.status,
                    )
                    for column, value in enumerate(values):
                        cell = QTableWidgetItem(str(value))
                        cell.setData(Qt.UserRole, expense.id)
                        if column == 8:
                            color = {
                                "Unpaid": "#b91c1c",
                                "Partially Paid": "#b45309",
                                "Paid": "#15803d",
                                "Overdue": "#7f1d1d",
                            }.get(str(value))
                            if color:
                                cell.setForeground(QColor(color))
                        self.table.setItem(row, column, cell)
            self.count_label.setText(f"Showing {len(expenses)} expenses")
        except AuthorizationError as exc:
            self.table.setRowCount(0)
            for label in self.metrics.values():
                label.clear()
            self.count_label.setText("Expense data unavailable")
            self.access_warning.setText(
                f"Expense and payment data unavailable: {exc}"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Could not load expenses", str(exc))
        finally:
            self._loading = False

    def _selected_id(self) -> int | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def _create(self):
        dialog = ExpenseFormDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.load_expenses()

    def _edit(self):
        expense_id = self._selected_id()
        if expense_id is None:
            return
        try:
            dialog = ExpenseFormDialog(self, expense_id)
            if dialog.exec() == QDialog.Accepted:
                self.load_expenses()
        except Exception as exc:
            QMessageBox.warning(self, "Could not edit expense", str(exc))

    def _details(self):
        expense_id = self._selected_id()
        if expense_id is None:
            QMessageBox.information(self, "Select an expense", "Select an expense to view.")
            return
        try:
            ExpenseDetailDialog(self, expense_id).exec()
            self.load_expenses()
        except AuthorizationError as exc:
            QMessageBox.warning(
                self, "Expense payments unavailable", str(exc)
            )

    def _post(self):
        expense_id = self._selected_id()
        if expense_id is None:
            return
        if QMessageBox.question(
            self, "Post expense",
            "Posting makes this expense part of operating-expense totals. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                post_expense(
                    db, expense_id, actor_context=_actor_context()
                )
            self.load_expenses()
        except Exception as exc:
            QMessageBox.warning(self, "Could not post expense", str(exc))

    def _void(self):
        expense_id = self._selected_id()
        if expense_id is None:
            return
        reason, accepted = QInputDialog.getText(self, "Void expense", "Mandatory void reason:")
        if not accepted or not reason.strip():
            return
        if QMessageBox.question(
            self, "Confirm void",
            "Voiding preserves the expense history. Any posted payments must be voided first.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                void_expense(
                    db, expense_id, reason,
                    actor_context=_actor_context(),
                )
            self.load_expenses()
        except Exception as exc:
            QMessageBox.warning(self, "Could not void expense", str(exc))

    def _categories(self):
        ExpenseCategoryDialog(self).exec()
        self.load_expenses()

    def _reports(self):
        path, selected = QFileDialog.getSaveFileName(
            self, "Export expense report", "expense-register.xlsx",
            "Excel workbook (*.xlsx);;PDF report (*.pdf)",
        )
        if not path:
            return
        try:
            with get_db() as db:
                if path.lower().endswith(".pdf") or "PDF" in selected:
                    if not path.lower().endswith(".pdf"):
                        path += ".pdf"
                    generate_expense_pdf_report(
                        db, path, actor_context=_actor_context(),
                        start_date=_optional_date(self.date_from),
                        end_date=_optional_date(self.date_to),
                    )
                else:
                    if not path.lower().endswith(".xlsx"):
                        path += ".xlsx"
                    generate_expense_excel_report(
                        db, path, actor_context=_actor_context(),
                        start_date=_optional_date(self.date_from),
                        end_date=_optional_date(self.date_to),
                    )
            QMessageBox.information(self, "Report created", f"Saved to:\n{path}")
        except Exception as exc:
            QMessageBox.warning(self, "Could not export expense report", str(exc))
