from datetime import date
from decimal import Decimal

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QDoubleSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from sqlalchemy import select

from database.connection import get_db
from database.models import Account, ApplicationSetting, Customer, ExpenseCategory, Supplier
from services.account_service import (
    close_accounting_period, configure_account_mapping, create_account,
    deactivate_account, update_account,
    ensure_accounting_period, list_account_mappings, list_accounting_periods,
    list_accounts, reopen_accounting_period, seed_default_accounts,
    set_fiscal_year_start,
)
from services.accounting_posting_service import (
    list_accounting_exceptions, retry_accounting_exception,
)
from services.accounting_report_service import (
    get_accounting_coverage, get_balance_sheet, get_cash_bank_book,
    get_general_ledger, get_opening_balance_preview, get_party_ledger,
    get_profit_and_loss, get_reconciliation_report, get_trial_balance,
    generate_accounting_excel_report, generate_accounting_pdf_report,
)
from services.journal_service import (
    create_draft_journal, get_journal_by_id, list_journals, post_journal,
    reverse_journal, update_draft_journal,
)
from services.payment_common import PAYMENT_METHODS
from services.security_service import (
    AuthorizationError, current_actor_context, require_permission,
)


_REPORT_TYPES = (
    "Trial Balance", "Profit & Loss", "Balance Sheet", "Journal Register",
    "General Ledger", "Cash/Bank Book", "Customer Ledger", "Supplier Ledger",
    "Accounting Coverage", "Opening Balance Preview", "Reconciliation",
)


def _date(value: QDate) -> date:
    return value.toPython()


def _actor_context():
    actor_context = current_actor_context()
    if actor_context is None:
        raise AuthorizationError("Sign in is required.")
    return actor_context


def _has_permission(permission_code: str) -> bool:
    try:
        with get_db() as db:
            require_permission(_actor_context(), permission_code, db_session=db)
        return True
    except AuthorizationError:
        return False


class ManualJournalDialog(QDialog):
    def __init__(self, accounts: list[Account], parent=None, journal=None):
        super().__init__(parent)
        self.journal = journal
        self.setWindowTitle("Edit Manual Journal Draft" if journal else "New Manual Journal Draft")
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.journal_date = QDateEdit()
        self.journal_date.setCalendarPopup(True)
        self.journal_date.setDate(
            QDate(journal.journal_date.year, journal.journal_date.month, journal.journal_date.day)
            if journal else QDate.currentDate()
        )
        form.addRow("Journal date:", self.journal_date)
        self.description = QLineEdit(journal.description if journal else "")
        form.addRow("Description:", self.description)
        self.reference = QLineEdit(journal.reference_number or "" if journal else "")
        form.addRow("Reference (optional):", self.reference)
        layout.addLayout(form)
        self.lines: list[tuple[QComboBox, QDoubleSpinBox, QDoubleSpinBox]] = []
        grid = QTableWidget(6, 3)
        grid.setHorizontalHeaderLabels(["Account", "Debit", "Credit"])
        grid.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for row in range(6):
            account_box = QComboBox()
            account_box.addItem("Select account", None)
            for account in accounts:
                account_box.addItem(f"{account.code} — {account.name}", account.id)
            debit, credit = QDoubleSpinBox(), QDoubleSpinBox()
            for control in (debit, credit):
                control.setRange(0, 1_000_000_000)
                control.setDecimals(2)
                control.setSingleStep(100)
            grid.setCellWidget(row, 0, account_box)
            grid.setCellWidget(row, 1, debit)
            grid.setCellWidget(row, 2, credit)
            self.lines.append((account_box, debit, credit))
            if journal and row < len(journal.lines):
                line = journal.lines[row]
                account_index = account_box.findData(line.account_id)
                if account_index >= 0:
                    account_box.setCurrentIndex(account_index)
                debit.setValue(float(line.debit))
                credit.setValue(float(line.credit))
        layout.addWidget(grid)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def payload(self) -> dict:
        rows = []
        for account, debit, credit in self.lines:
            account_id = account.currentData()
            if account_id is None and not debit.value() and not credit.value():
                continue
            rows.append({
                "account_id": account_id,
                "debit": Decimal(str(debit.value())),
                "credit": Decimal(str(credit.value())),
            })
        return {
            "journal_date": _date(self.journal_date.date()),
            "description": self.description.text(),
            "reference_number": self.reference.text(),
            "lines": rows,
        }


class AccountingPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("Accounting & Financial Reporting")
        title.setObjectName("heading")
        header.addWidget(title)
        header.addStretch()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.load_data)
        header.addWidget(refresh)
        root.addLayout(header)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            "background: white; border: 1px solid #dbe3ef; border-radius: 8px; padding: 12px;"
        )
        root.addWidget(self.summary)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self._build_accounts_tab()
        self._build_journals_tab()
        self._build_exceptions_tab()
        self._build_periods_tab()
        self._build_reports_tab()
        self.load_data()

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        return table

    def _build_accounts_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        seed = QPushButton("Initialize default chart of accounts")
        seed.clicked.connect(self._seed_accounts)
        add = QPushButton("Add account")
        add.clicked.connect(self._add_account)
        edit = QPushButton("Edit selected")
        edit.clicked.connect(self._edit_account)
        deactivate = QPushButton("Deactivate selected")
        deactivate.clicked.connect(self._deactivate_account)
        actions.addWidget(seed)
        actions.addWidget(add)
        actions.addWidget(edit)
        actions.addWidget(deactivate)
        self.account_manage_buttons = (seed, add, edit, deactivate)
        actions.addStretch()
        self.mapping_key = QComboBox()
        self.mapping_key.setEditable(True)
        self.mapping_key.setMinimumWidth(210)
        self.mapping_key.addItems([
            "control:accounts_receivable",
            "control:accounts_payable",
            "control:customer_advances",
            "control:expense_payables",
            "asset:supplier_advances",
            "inventory:materials_awaiting_dismantling",
            "inventory:recovered_components",
            "income:sales_revenue",
            "expense:cost_of_goods_sold",
            "tax:output_cgst",
            "tax:output_sgst",
            "tax:output_igst",
            "payment:cash:cash in hand",
            *(
                f"payment:{method.casefold()}:<bank/account name>"
                for method in PAYMENT_METHODS if method != "Cash"
            ),
        ])
        self.mapping_account = QComboBox()
        save_mapping = QPushButton("Save mapping")
        save_mapping.clicked.connect(self._save_mapping)
        self.mapping_save_button = save_mapping
        actions.addWidget(self.mapping_key)
        actions.addWidget(self.mapping_account)
        actions.addWidget(save_mapping)
        layout.addLayout(actions)
        self.account_table = self._table(
            ["Code", "Account", "Type", "Subtype", "Parent", "Status", "System key"]
        )
        layout.addWidget(self.account_table)
        self.mapping_table = self._table(["Mapping key", "Account", "Description", "Status"])
        layout.addWidget(self.mapping_table)
        self.tabs.addTab(page, "Accounts & Mappings")

    def _build_journals_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        add = QPushButton("New manual journal draft")
        add.clicked.connect(self._new_journal)
        edit = QPushButton("Edit selected draft")
        edit.clicked.connect(self._edit_journal)
        post = QPushButton("Post selected draft")
        post.clicked.connect(self._post_journal)
        reverse = QPushButton("Reverse selected posted journal")
        reverse.clicked.connect(self._reverse_journal)
        for button in (add, edit, post, reverse):
            actions.addWidget(button)
        self.journal_manage_buttons = (add, edit)
        self.journal_post_button = post
        self.journal_reverse_button = reverse
        actions.addStretch()
        layout.addLayout(actions)
        self.journal_table = self._table(
            ["Journal", "Date", "Description", "Reference", "Source", "Debits", "Credits", "Status"]
        )
        layout.addWidget(self.journal_table)
        self.tabs.addTab(page, "Journal Entries")

    def _build_exceptions_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        retry = QPushButton("Retry selected source event")
        retry.clicked.connect(self._retry_exception)
        self.retry_exception_button = retry
        actions.addWidget(retry)
        actions.addStretch()
        layout.addLayout(actions)
        self.exception_table = self._table(
            ["Source", "ID", "Event", "Reference", "Reason", "Status"]
        )
        layout.addWidget(self.exception_table)
        self.tabs.addTab(page, "Accounting Review Queue")

    def _build_periods_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        self.fiscal_month = QSpinBox()
        self.fiscal_month.setRange(1, 12)
        self.fiscal_month.setValue(4)
        self.fiscal_day = QSpinBox()
        self.fiscal_day.setRange(1, 31)
        self.fiscal_day.setValue(1)
        save_fiscal = QPushButton("Save fiscal year start")
        save_fiscal.clicked.connect(self._save_fiscal_start)
        ensure = QPushButton("Create current year periods")
        ensure.clicked.connect(self._ensure_periods)
        close = QPushButton("Close selected period")
        close.clicked.connect(self._close_period)
        reopen = QPushButton("Reopen selected period")
        reopen.clicked.connect(self._reopen_period)
        actions.addWidget(QLabel("Fiscal year starts:"))
        actions.addWidget(self.fiscal_month)
        actions.addWidget(QLabel("month /"))
        actions.addWidget(self.fiscal_day)
        for button in (save_fiscal, ensure, close, reopen):
            actions.addWidget(button)
        self.fiscal_configure_button = save_fiscal
        self.period_manage_buttons = (ensure, close, reopen)
        actions.addStretch()
        layout.addLayout(actions)
        self.period_table = self._table(
            ["Period", "Name", "Start", "End", "Status", "Closed", "Reopened"]
        )
        layout.addWidget(self.period_table)
        self.tabs.addTab(page, "Periods")

    def _build_reports_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.report_type = QComboBox()
        self.report_type.addItems(_REPORT_TYPES)
        self.report_account = QComboBox()
        self.report_account.setMinimumWidth(180)
        self.report_party = QComboBox()
        self.report_party.setMinimumWidth(180)
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setSpecialValueText("All dates")
        self.start_date.setDate(QDate(2000, 1, 1))
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(QDate.currentDate())
        preview = QPushButton("Preview report")
        preview.clicked.connect(self._preview_report)
        pdf = QPushButton("Export PDF")
        pdf.clicked.connect(lambda: self._export_report("pdf"))
        excel = QPushButton("Export Excel")
        excel.clicked.connect(lambda: self._export_report("xlsx"))
        opening = QPushButton("Opening-balance preview")
        opening.clicked.connect(self._show_opening_preview)
        reconcile = QPushButton("Subledger reconciliation")
        reconcile.clicked.connect(self._show_reconciliation)
        for control in (
            self.report_type, self.report_account, self.report_party,
            self.start_date, self.end_date, preview, pdf, excel,
            opening, reconcile,
        ):
            controls.addWidget(control)
        layout.addLayout(controls)
        self.report_table = self._table(["Account / Section", "Details", "Debit / Amount", "Credit"])
        layout.addWidget(self.report_table)
        self.report_note = QLabel("Reports use posted journals only and are marked provisional when source events remain unposted.")
        self.report_note.setWordWrap(True)
        layout.addWidget(self.report_note)
        self.tabs.addTab(page, "Reports & Reconciliation")

    def _clear_protected_data(self, message: str) -> None:
        self.summary.setText(message)
        for table in (
            self.account_table, self.mapping_table, self.journal_table,
            self.exception_table, self.period_table, self.report_table,
        ):
            table.setRowCount(0)
        for control in (self.mapping_account, self.report_account, self.report_party):
            control.clear()
        self.fiscal_month.setValue(4)
        self.fiscal_day.setValue(1)
        self.report_note.setText(message)
        for button in (
            *self.account_manage_buttons,
            *self.journal_manage_buttons,
            *self.period_manage_buttons,
            self.mapping_save_button,
            self.journal_post_button,
            self.journal_reverse_button,
            self.retry_exception_button,
            self.fiscal_configure_button,
        ):
            button.setEnabled(False)

    def load_data(self):
        actor_context = current_actor_context()
        if actor_context is None:
            self._clear_protected_data("Sign in to view accounting data.")
            return
        for button in self.account_manage_buttons:
            button.setEnabled(_has_permission("accounting.configure"))
        self.mapping_save_button.setEnabled(
            _has_permission("accounting.configure")
        )
        for button in self.journal_manage_buttons:
            button.setEnabled(_has_permission("accounting.manage"))
        self.journal_post_button.setEnabled(_has_permission("accounting.post"))
        self.journal_reverse_button.setEnabled(
            _has_permission("accounting.reverse")
        )
        self.retry_exception_button.setEnabled(
            _has_permission("accounting.exceptions.manage")
        )
        self.fiscal_configure_button.setEnabled(
            _has_permission("accounting.configure")
        )
        for button in self.period_manage_buttons:
            button.setEnabled(_has_permission("accounting.periods.manage"))
        if not _has_permission("accounting.view"):
            self._clear_protected_data(
                "Accounting access is not available for this account."
            )
            return
        try:
            with get_db() as db:
                accounts = list_accounts(
                    db, active_only=False, actor_context=actor_context
                )
                mappings = list_account_mappings(db, actor_context=actor_context)
                journals = list_journals(
                    db, limit=500, actor_context=actor_context
                )
                exceptions = (
                    list_accounting_exceptions(
                        db, status="ALL", actor_context=actor_context
                    )
                    if _has_permission("accounting.exceptions.manage") else []
                )
                periods = list_accounting_periods(db, actor_context=actor_context)
                coverage = get_accounting_coverage(
                    db, actor_context=actor_context
                )
                trial = get_trial_balance(db, actor_context=actor_context)
                customers = list(db.scalars(select(Customer).order_by(Customer.customer_name)))
                suppliers = list(db.scalars(select(Supplier).order_by(Supplier.business_name)))
                expense_categories = list(db.scalars(select(ExpenseCategory).order_by(ExpenseCategory.name)))
                fiscal_month = db.get(ApplicationSetting, "accounting_fiscal_start_month")
                fiscal_day = db.get(ApplicationSetting, "accounting_fiscal_start_day")
        except AuthorizationError as exc:
            self._clear_protected_data(str(exc))
            return
        self.summary.setText(
            f"Trial balance: debits {trial['total_debit']:.2f} / credits "
            f"{trial['total_credit']:.2f} (difference {trial['difference']:.2f})  |  "
            f"Source events: {coverage['posted_source_events']}/"
            f"{coverage['eligible_source_events']} posted  |  "
            f"{coverage['open_exceptions']} open review items"
        )
        self.fiscal_month.setValue(int(fiscal_month.value) if fiscal_month else 4)
        self.fiscal_day.setValue(int(fiscal_day.value) if fiscal_day else 1)
        for category in expense_categories:
            category_key = f"expense_category:{category.name.casefold()}"
            if self.mapping_key.findText(category_key) < 0:
                self.mapping_key.addItem(category_key)
        self.mapping_account.clear()
        self.mapping_account.addItem("Select account", None)
        for account in accounts:
            if account.is_active:
                self.mapping_account.addItem(f"{account.code} — {account.name}", account.id)
        self.report_account.clear()
        self.report_account.addItem("Select account", None)
        for account in accounts:
            if account.is_active:
                self.report_account.addItem(f"{account.code} — {account.name}", account.id)
        self.report_party.clear()
        self.report_party.addItem("Select party", None)
        for party in customers:
            self.report_party.addItem(
                f"Customer: {party.customer_code} — {party.customer_name}",
                ("Customer", party.id),
            )
        for party in suppliers:
            self.report_party.addItem(
                f"Supplier: {party.supplier_code} — {party.business_name}",
                ("Supplier", party.id),
            )
        self.account_table.setRowCount(len(accounts))
        for row, account in enumerate(accounts):
            values = (
                account.code, account.name, account.account_type, account.subtype or "",
                account.parent.name if account.parent else "",
                "Active" if account.is_active else "Inactive", account.system_key or "",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.UserRole, account.id)
                self.account_table.setItem(row, column, cell)
        self.mapping_table.setRowCount(len(mappings))
        for row, mapping in enumerate(mappings):
            values = (
                mapping.mapping_key,
                f"{mapping.account.code} — {mapping.account.name}",
                mapping.description or "", "Active" if mapping.is_active else "Inactive",
            )
            for column, value in enumerate(values):
                self.mapping_table.setItem(row, column, QTableWidgetItem(str(value)))
        self.journal_table.setRowCount(len(journals))
        for row, journal in enumerate(journals):
            values = (
                journal.journal_number, journal.journal_date, journal.description,
                journal.reference_number or "", journal.source_module,
                journal.total_debit, journal.total_credit, journal.status,
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.UserRole, journal.id)
                self.journal_table.setItem(row, column, cell)
        self.exception_table.setRowCount(len(exceptions))
        for row, exception in enumerate(exceptions):
            values = (
                exception.source_module, exception.source_transaction_id,
                exception.source_event_type, exception.source_reference or "",
                exception.reason, exception.status,
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.UserRole, exception.id)
                self.exception_table.setItem(row, column, cell)
        self.period_table.setRowCount(len(periods))
        for row, period in enumerate(periods):
            values = (
                period.period_code, period.period_name, period.start_date,
                period.end_date, period.status, period.closed_at or "",
                period.reopened_at or "",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.UserRole, period.id)
                self.period_table.setItem(row, column, cell)
        try:
            self._preview_report()
        except AuthorizationError as exc:
            self._clear_protected_data(str(exc))

    def _selected_id(self, table: QTableWidget) -> int | None:
        row = table.currentRow()
        return table.item(row, 0).data(Qt.UserRole) if row >= 0 else None

    def _seed_accounts(self):
        try:
            with get_db() as db:
                seed_default_accounts(db, actor_context=_actor_context())
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Chart of accounts", str(exc))

    def _add_account(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Add account")
        form = QFormLayout(dialog)
        code, name, subtype = QLineEdit(), QLineEdit(), QLineEdit()
        kind = QComboBox()
        kind.addItems(["Assets", "Liabilities", "Equity", "Income", "Expenses"])
        form.addRow("Code:", code)
        form.addRow("Name:", name)
        form.addRow("Type:", kind)
        form.addRow("Subtype:", subtype)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            with get_db() as db:
                create_account(
                    db, actor_context=_actor_context(),
                    code=code.text(), name=name.text(),
                    account_type=kind.currentText(), subtype=subtype.text(),
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not add account", str(exc))

    def _edit_account(self):
        account_id = self._selected_id(self.account_table)
        if account_id is None:
            return
        with get_db() as db:
            account = db.get(Account, account_id)
            if account is None:
                return
            name, description = account.name, account.description or ""
        new_name, ok = QInputDialog.getText(self, "Edit account", "Account name:", text=name)
        if not ok:
            return
        new_description, ok = QInputDialog.getText(
            self, "Edit account", "Description:", text=description,
        )
        if not ok:
            return
        try:
            with get_db() as db:
                update_account(
                    db, account_id, actor_context=_actor_context(),
                    name=new_name, description=new_description,
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not edit account", str(exc))

    def _deactivate_account(self):
        account_id = self._selected_id(self.account_table)
        if account_id is None:
            return
        if QMessageBox.question(
            self, "Deactivate account",
            "The account will remain in historical journals. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                deactivate_account(
                    db, account_id, actor_context=_actor_context()
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not deactivate account", str(exc))

    def _save_mapping(self):
        account_id = self.mapping_account.currentData()
        if account_id is None:
            QMessageBox.information(self, "Account mapping", "Select an active account.")
            return
        try:
            with get_db() as db:
                configure_account_mapping(
                    db, self.mapping_key.currentText(), int(account_id),
                    "Explicit accounting source mapping",
                    actor_context=_actor_context(),
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not save mapping", str(exc))

    def _new_journal(self):
        with get_db() as db:
            actor_context = _actor_context()
            accounts = list_accounts(
                db, active_only=True, actor_context=actor_context
            )
        dialog = ManualJournalDialog(accounts, self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            with get_db() as db:
                payload = dialog.payload()
                payload["actor_context"] = _actor_context()
                create_draft_journal(db, **payload)
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not create journal", str(exc))

    def _edit_journal(self):
        journal_id = self._selected_id(self.journal_table)
        if journal_id is None:
            return
        with get_db() as db:
            actor_context = _actor_context()
            journal = get_journal_by_id(
                db, journal_id, actor_context=actor_context
            )
            accounts = list_accounts(
                db, active_only=True, actor_context=actor_context
            )
        if journal is None or journal.status != "Draft" or journal.source_module != "Manual":
            QMessageBox.information(self, "Edit journal", "Select a manual draft journal.")
            return
        dialog = ManualJournalDialog(accounts, self, journal)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            with get_db() as db:
                update_draft_journal(
                    db, journal_id, actor_context=_actor_context(),
                    **dialog.payload(),
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not edit journal", str(exc))

    def _post_journal(self):
        journal_id = self._selected_id(self.journal_table)
        if journal_id is None:
            return
        if QMessageBox.question(
            self, "Post journal", "Post the selected balanced journal?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as db:
                post_journal(
                    db, journal_id, actor_context=_actor_context()
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not post journal", str(exc))

    def _reverse_journal(self):
        journal_id = self._selected_id(self.journal_table)
        if journal_id is None:
            return
        reason, ok = QInputDialog.getText(self, "Reverse journal", "Reason for reversal:")
        if not ok or not reason.strip():
            return
        try:
            with get_db() as db:
                reverse_journal(
                    db, journal_id, reason, actor_context=_actor_context()
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not reverse journal", str(exc))

    def _retry_exception(self):
        exception_id = self._selected_id(self.exception_table)
        if exception_id is None:
            return
        try:
            with get_db() as db:
                retry_accounting_exception(
                    db, exception_id, actor_context=_actor_context()
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not retry source event", str(exc))

    def _save_fiscal_start(self):
        try:
            with get_db() as db:
                set_fiscal_year_start(
                    db, self.fiscal_month.value(), self.fiscal_day.value(),
                    actor_context=_actor_context(),
                )
            QMessageBox.information(self, "Fiscal year", "Fiscal year start saved.")
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not save fiscal year", str(exc))

    def _ensure_periods(self):
        try:
            with get_db() as db:
                ensure_accounting_period(
                    db, date.today(), actor_context=_actor_context()
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not create periods", str(exc))

    def _change_period(self, close: bool):
        period_id = self._selected_id(self.period_table)
        if period_id is None:
            return
        reason, ok = QInputDialog.getText(
            self, "Accounting period", "Reason (required):"
        )
        if not ok or not reason.strip():
            return
        try:
            with get_db() as db:
                (close_accounting_period if close else reopen_accounting_period)(
                    db, period_id, reason, actor_context=_actor_context(),
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not change period", str(exc))

    def _close_period(self):
        self._change_period(True)

    def _reopen_period(self):
        self._change_period(False)

    def _date_range(self) -> tuple[date | None, date | None]:
        start = _date(self.start_date.date())
        return (None if start == date(2000, 1, 1) else start, _date(self.end_date.date()))

    def _report_filters(self) -> dict:
        party = self.report_party.currentData()
        return {
            "account_id": self.report_account.currentData(),
            "party_type": party[0] if party else None,
            "party_id": party[1] if party else None,
        }

    def _preview_report(self):
        start, end = self._date_range()
        report_type = self.report_type.currentText()
        try:
            with get_db() as db:
                actor_context = _actor_context()
                coverage = get_accounting_coverage(
                    db, actor_context=actor_context,
                    start_date=start, end_date=end
                )
                if report_type == "Trial Balance":
                    data = get_trial_balance(
                        db, actor_context=actor_context, as_of=end
                    )
                    rows = [
                        (row["account"].code, row["account"].name,
                         row["debit_balance"], row["credit_balance"])
                        for row in data["rows"]
                    ]
                elif report_type == "Profit & Loss":
                    data = get_profit_and_loss(
                        db, actor_context=actor_context,
                        start_date=start, end_date=end
                    )
                    rows = [
                        (row["account"].code, row["account"].name, row["amount"], "")
                        for row in data["income"] + data["expenses"]
                    ]
                elif report_type == "Balance Sheet":
                    data = get_balance_sheet(
                        db, actor_context=actor_context, as_of=end
                    )
                    rows = [
                        (kind, row.get("account").name if row.get("account") else row.get("name"),
                         row["amount"], "")
                        for kind, group in data["groups"].items() for row in group
                    ]
                elif report_type in {"General Ledger", "Cash/Bank Book"}:
                    account_id = self.report_account.currentData()
                    data = (
                        get_cash_bank_book(
                            db, account_id, start_date=start, end_date=end,
                            actor_context=actor_context,
                        )
                        if report_type == "Cash/Bank Book"
                        else get_general_ledger(
                            db, account_id, start_date=start, end_date=end,
                            actor_context=actor_context,
                        )
                    )
                    rows = [
                        (row["date"], row["journal_number"], row["description"],
                         row["running_balance"])
                        for row in data["rows"]
                    ]
                elif report_type in {"Customer Ledger", "Supplier Ledger"}:
                    party = self.report_party.currentData()
                    expected_type = "Customer" if report_type == "Customer Ledger" else "Supplier"
                    if party is None or party[0] != expected_type:
                        raise ValueError(f"Select a {expected_type.lower()} for this report.")
                    data = get_party_ledger(
                        db, party[0], party[1], start_date=start, end_date=end,
                        actor_context=actor_context,
                    )
                    rows = [
                        (row["date"], row["journal_number"], row["description"],
                         f"Dr {row['debit']} / Cr {row['credit']}")
                        for row in data["rows"]
                    ]
                elif report_type == "Accounting Coverage":
                    rows = [
                        (row["source_module"], row["source_id"], row["event_type"], row["date"])
                        for row in coverage["missing"]
                    ]
                elif report_type == "Opening Balance Preview":
                    preview_data = get_opening_balance_preview(
                        db, actor_context=actor_context
                    )
                    rows = [
                        ("Customer", row["code"], row["name"], row["debit"])
                        for row in preview_data["customers"]
                    ] + [
                        ("Supplier", row["code"], row["name"], row["credit"])
                        for row in preview_data["suppliers"]
                    ]
                elif report_type == "Reconciliation":
                    data = get_reconciliation_report(
                        db, actor_context=actor_context
                    )
                    rows = [
                        (key.replace("_", " ").title(), value["subledger"],
                         value["control_account"], value["difference"])
                        for key, value in data.items() if key != "inventory"
                    ]
                    rows.append(("Inventory", "Review required", "", data["inventory"]["message"]))
                else:
                    rows = [
                        (journal.journal_number, journal.description,
                         journal.total_debit, journal.total_credit)
                        for journal in list_journals(
                            db, start_date=start, end_date=end,
                            actor_context=actor_context,
                        )
                    ]
            self.report_table.setRowCount(len(rows))
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    self.report_table.setItem(row, column, QTableWidgetItem(str(value)))
            self.report_note.setText(
                f"{report_type}: {'COMPLETE' if coverage['complete'] else 'PROVISIONAL'} — "
                f"{coverage['unposted_source_events']} unposted source events; "
                f"{coverage['open_exceptions']} open exceptions. "
                "Only posted journals are included; historical transactions are not auto-posted."
            )
        except Exception as exc:
            self.report_table.setRowCount(0)
            self.report_note.clear()
            QMessageBox.warning(self, "Could not preview report", str(exc))

    def _export_report(self, format_name: str):
        extension = "pdf" if format_name == "pdf" else "xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export accounting report", f"accounting-report.{extension}",
            "PDF (*.pdf)" if extension == "pdf" else "Excel workbook (*.xlsx)",
        )
        if not path:
            return
        start, end = self._date_range()
        try:
            with get_db() as db:
                if extension == "pdf":
                    generate_accounting_pdf_report(
                        db, path, actor_context=_actor_context(),
                        report_type=self.report_type.currentText(),
                        start_date=start, end_date=end, **self._report_filters(),
                    )
                else:
                    generate_accounting_excel_report(
                        db, path, actor_context=_actor_context(),
                        report_type=self.report_type.currentText(),
                        start_date=start, end_date=end, **self._report_filters(),
                    )
            QMessageBox.information(self, "Export complete", f"Report saved to:\n{path}")
        except Exception as exc:
            QMessageBox.warning(self, "Could not export report", str(exc))

    def _show_opening_preview(self):
        with get_db() as db:
            preview = get_opening_balance_preview(
                db, actor_context=_actor_context()
            )
        lines = [
            preview["mode"],
            f"Customer receivables: {preview['customer_receivable_total']:.2f}",
            f"Supplier payables: {preview['supplier_payable_total']:.2f}",
            f"Opening equity difference (preview only): debit "
            f"{preview['opening_equity_debit']:.2f}; credit {preview['opening_equity_credit']:.2f}",
            "No opening journal was created. Verify source records and supporting documents before any migration.",
        ]
        QMessageBox.information(self, "Opening balance preview", "\n".join(lines))

    def _show_reconciliation(self):
        try:
            with get_db() as db:
                report = get_reconciliation_report(
                    db, actor_context=_actor_context()
                )
        except Exception as exc:
            QMessageBox.warning(
                self, "Reconciliation unavailable", str(exc)
            )
            return
        lines = []
        for name, result in report.items():
            if name == "inventory":
                lines.append(f"Inventory: {result['message']}")
                continue
            control = result["control_account"]
            lines.append(
                f"{name.replace('_', ' ').title()}: subledger {result['subledger']:.2f}; "
                f"control {control if control is not None else 'Not configured'}; "
                f"difference {result['difference'] if result['difference'] is not None else 'N/A'}"
            )
        QMessageBox.information(self, "Subledger reconciliation", "\n".join(lines))
