from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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
)
import database.connection as db_connection
from database.models import ApplicationSetting
from services.backup_management_service import (
    BackupValidationError,
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from services.document_service import (
    DOCUMENT_CATEGORIES,
    MAX_ABSOLUTE_SIZE,
    RELATED_ENTITY_TYPES,
    archive_document,
    get_document_content,
    list_documents,
    upload_document,
)
from services.settings_service import (
    SETTING_RULES,
    SettingsValidationError,
    deactivate_payment_account,
    get_setting,
    get_settings,
    get_payment_accounts,
    payment_account_mappings,
    save_payment_account,
    update_settings,
)
from services.security_service import (
    ActorContext,
    AuthorizationError,
    current_actor_context,
    require_permission,
)


FACTORY_FIELDS = [
    ("Factory name", "factory_name"),
    ("Registered / operating address", "factory_address"),
    ("Phone", "factory_phone"),
    ("Email", "factory_email"),
    ("State / UT", "factory_state"),
    ("Country", "factory_country"),
    ("PIN code", "factory_pin_code"),
    ("GSTIN (if registered)", "factory_gstin"),
    ("PAN (optional)", "factory_pan"),
    ("CIN (optional)", "factory_cin"),
]
PREFERENCE_FIELDS = [
    ("Currency ISO code", "default_currency"),
    ("Currency display symbol", "currency_symbol"),
    ("Date format", "date_format"),
    ("Timezone", "timezone"),
    ("Default invoice tax treatment", "default_tax_treatment"),
    ("Default payment terms", "default_payment_terms"),
    ("Maximum document size (MB)", "max_document_size_mb"),
    ("Dashboard refresh interval (seconds)", "dashboard_refresh_seconds"),
    ("Document storage directory", "document_storage_path"),
    ("Backup directory", "backup_directory"),
    ("Backup retention target (no automatic deletion)", "backup_retention_count"),
    ("Application language", "application_language"),
    ("Accounting fiscal year start month", "accounting_fiscal_start_month"),
    ("Accounting fiscal year start day", "accounting_fiscal_start_day"),
]


def _db_path() -> Path:
    database = db_connection.engine.url.database
    if not database:
        raise RuntimeError("The active database does not have a local file path.")
    return Path(database).resolve()


def _actor_context() -> ActorContext:
    actor_context = current_actor_context()
    if actor_context is None:
        raise AuthorizationError("Sign in is required.")
    return actor_context


def _has_permission(permission_code: str) -> bool:
    try:
        with db_connection.get_db() as session:
            require_permission(
                _actor_context(), permission_code, db_session=session
            )
        return True
    except AuthorizationError:
        return False


def _backup_directory(session, permission: str, actor_context: ActorContext) -> Path:
    require_permission(actor_context, permission, db_session=session)
    setting = session.get(ApplicationSetting, "backup_directory")
    configured = setting.value if setting is not None else None
    return Path(configured).expanduser().resolve() if configured else (
        db_connection.ROOT / "reports" / "backups"
    ).resolve()


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        heading = QLabel("Settings & Document Management")
        heading.setObjectName("heading")
        intro = QLabel(
            "Configure verified factory details and local application data. Missing values remain unset; "
            "invoice snapshots are not changed by later settings edits."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        layout.addWidget(heading)
        layout.addWidget(intro)
        self.tabs = QTabWidget()
        self.configuration_tab = ConfigurationTab()
        self.payment_accounts_tab = PaymentAccountsTab()
        self.documents_tab = DocumentsTab()
        self.backups_tab = BackupsTab()
        self.tabs.addTab(self.configuration_tab, "Factory & Preferences")
        self.tabs.addTab(self.payment_accounts_tab, "Payment Accounts")
        self.tabs.addTab(self.documents_tab, "Documents")
        self.tabs.addTab(self.backups_tab, "Backup & Restore")
        layout.addWidget(self.tabs)

    def load_data(self):
        self.configuration_tab.load_data()
        self.payment_accounts_tab.load_data()
        self.documents_tab.load_data()
        self.backups_tab.load_data()

    def clear_protected_data(self):
        self.configuration_tab.clear_protected_data()
        self.payment_accounts_tab.clear_protected_data()
        self.documents_tab.clear_protected_data()
        self.backups_tab.clear_protected_data()


class ConfigurationTab(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        self.fields: dict[str, QLineEdit] = {}
        self.storage_buttons: list[QPushButton] = []
        self.access_note = QLabel()
        self.access_note.setObjectName("muted")
        root.addWidget(self.access_note)
        form = QFormLayout()
        for label, key in FACTORY_FIELDS + PREFERENCE_FIELDS:
            row = QHBoxLayout()
            field = QLineEdit()
            field.setMaxLength(SETTING_RULES[key][0])
            self.fields[key] = field
            row.addWidget(field)
            if key in {"document_storage_path", "backup_directory"}:
                browse = QPushButton("Browse…")
                browse.clicked.connect(lambda _checked=False, target=field: self._browse(target))
                row.addWidget(browse)
                self.storage_buttons.append(browse)
            form.addRow(label, row)
        root.addLayout(form)
        self.save_button = QPushButton("Save settings")
        self.save_button.clicked.connect(self.save)
        self.save_button.setVisible(False)
        root.addWidget(self.save_button)
        root.addStretch()
        for field in self.fields.values():
            field.setReadOnly(True)
        for button in self.storage_buttons:
            button.setVisible(False)

    @staticmethod
    def _browse(field: QLineEdit):
        directory = QFileDialog.getExistingDirectory(None, "Select directory", field.text())
        if directory:
            field.setText(directory)

    def load_data(self):
        try:
            with db_connection.get_db() as session:
                values = get_settings(
                    session, actor_context=_actor_context()
                )
            for key, field in self.fields.items():
                field.setText(values.get(key) or "")
            can_manage = _has_permission("settings.manage")
            self.save_button.setVisible(can_manage)
            for field in self.fields.values():
                field.setReadOnly(not can_manage)
            for button in self.storage_buttons:
                button.setVisible(can_manage)
            self.access_note.setText(
                "" if can_manage else "Settings are read-only for this account."
            )
        except AuthorizationError as exc:
            self.clear_protected_data()
            self.access_note.setText(str(exc))
        except Exception as exc:
            self.clear_protected_data()
            QMessageBox.critical(self, "Unable to load settings", str(exc))

    def clear_protected_data(self):
        for field in self.fields.values():
            field.clear()
            field.setReadOnly(True)
        self.save_button.setVisible(False)
        for button in self.storage_buttons:
            button.setVisible(False)

    def save(self):
        values = {key: field.text() for key, field in self.fields.items()}
        try:
            with db_connection.get_db() as session:
                update_settings(
                    session,
                    values,
                    actor_context=_actor_context(),
                    reason="Updated from Settings page",
                )
            QMessageBox.information(self, "Settings saved", "Configuration was saved and audited.")
        except (SettingsValidationError, OSError) as exc:
            QMessageBox.warning(self, "Invalid settings", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Could not save settings", str(exc))


class PaymentAccountDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Payment Account")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.kind = QComboBox()
        self.kind.addItems(["Cash", "Bank", "UPI Settlement", "Cheque Clearing", "Other"])
        self.bank = QLineEdit()
        self.holder = QLineEdit()
        self.identifier = QLineEdit()
        self.identifier.setMaxLength(20)
        self.identifier.setPlaceholderText("Optional; enter only masked value or last 4 digits")
        self.ifsc = QLineEdit()
        self.branch = QLineEdit()
        self.mapping = QComboBox()
        self.mapping.addItem("No accounting mapping", None)
        with db_connection.get_db() as session:
            self._mappings = payment_account_mappings(
                session, actor_context=_actor_context()
            )
        for mapping in self._mappings:
            self.mapping.addItem(
                f"{mapping.mapping_key} (account #{mapping.account_id})",
                mapping.id,
            )
        form.addRow("Display name *", self.name)
        form.addRow("Account type *", self.kind)
        form.addRow("Bank name", self.bank)
        form.addRow("Account holder", self.holder)
        form.addRow("Account reference (masked)", self.identifier)
        form.addRow("IFSC", self.ifsc)
        form.addRow("Branch", self.branch)
        form.addRow("Accounting mapping", self.mapping)
        note = QLabel("Do not enter or store full bank account numbers in this local configuration.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return {
            "display_name": self.name.text(),
            "account_type": self.kind.currentText(),
            "bank_name": self.bank.text(),
            "account_holder": self.holder.text(),
            "account_identifier": self.identifier.text(),
            "ifsc": self.ifsc.text(),
            "branch": self.branch.text(),
            "account_mapping_id": self.mapping.currentData(),
        }


class PaymentAccountsTab(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        description = QLabel(
            "Maintain the bank/cash account labels used by payment screens. Full account numbers are not stored."
        )
        description.setWordWrap(True)
        root.addWidget(description)
        actions = QHBoxLayout()
        self.add_button = QPushButton("Add payment account")
        self.add_button.clicked.connect(self.add_account)
        self.add_button.setVisible(False)
        self.deactivate_button = QPushButton("Deactivate selected")
        self.deactivate_button.clicked.connect(self.deactivate_selected)
        self.deactivate_button.setVisible(False)
        actions.addWidget(self.add_button)
        actions.addWidget(self.deactivate_button)
        actions.addStretch()
        root.addLayout(actions)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Name", "Type", "Bank", "Holder", "Masked reference", "IFSC", "Accounting mapping",
        ])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        root.addWidget(self.table)
        self.access_note = QLabel()
        self.access_note.setObjectName("muted")
        root.addWidget(self.access_note)

    def load_data(self):
        try:
            if not _has_permission("settings.view"):
                raise AuthorizationError("Settings view permission is required.")
            with db_connection.get_db() as session:
                accounts = get_payment_accounts(
                    session, actor_context=_actor_context()
                )
            can_manage = _has_permission("settings.manage")
            self.add_button.setVisible(can_manage)
            self.deactivate_button.setVisible(can_manage)
            self.access_note.setText(
                "" if can_manage else "Payment-account settings are read-only."
            )
        except AuthorizationError as exc:
            self.clear_protected_data()
            self.access_note.setText(str(exc))
            return
        except Exception as exc:
            self.clear_protected_data()
            QMessageBox.warning(self, "Unable to load payment accounts", str(exc))
            return
        self.table.setRowCount(len(accounts))
        for row, account in enumerate(accounts):
            mapping_name = str(account.account_mapping_id or "")
            values = (
                account.display_name, account.account_type, account.bank_name or "",
                account.account_holder or "", account.account_identifier or "",
                account.ifsc or "", mapping_name,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
            self.table.item(row, 0).setData(256, account.id)
            if not account.is_active:
                for column in range(self.table.columnCount()):
                    self.table.item(row, column).setText(
                        self.table.item(row, column).text() + (" (inactive)" if column == 0 else "")
                    )

    def clear_protected_data(self):
        self.table.setRowCount(0)
        self.add_button.setVisible(False)
        self.deactivate_button.setVisible(False)

    def add_account(self):
        try:
            dialog = PaymentAccountDialog(self)
            if dialog.exec() != QDialog.Accepted:
                return
            with db_connection.get_db() as session:
                save_payment_account(
                    session, **dialog.values(), actor_context=_actor_context()
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not add payment account", str(exc))

    def deactivate_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        account_id = self.table.item(row, 0).data(256)
        if QMessageBox.question(
            self, "Deactivate payment account",
            "Deactivate this account for future payment entry? Historical payments are unchanged.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with db_connection.get_db() as session:
                deactivate_payment_account(
                    session, int(account_id), actor_context=_actor_context()
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not deactivate account", str(exc))


class DocumentsTab(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        actions = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search file name, document ID, or description")
        self.search.textChanged.connect(self.load_data)
        self.include_archived = QComboBox()
        self.include_archived.addItem("Active documents only", False)
        self.include_archived.addItem("Include archived", True)
        self.include_archived.currentIndexChanged.connect(self.load_data)
        self.upload_button = QPushButton("Upload document")
        self.upload_button.clicked.connect(self.upload)
        self.upload_button.setVisible(False)
        self.open_button = QPushButton("Open / save selected")
        self.open_button.clicked.connect(self.open_selected)
        self.open_button.setVisible(False)
        self.archive_button = QPushButton("Archive selected")
        self.archive_button.clicked.connect(self.archive_selected)
        self.archive_button.setVisible(False)
        for widget in (
            self.search, self.include_archived, self.upload_button,
            self.open_button, self.archive_button,
        ):
            actions.addWidget(widget)
        root.addLayout(actions)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Document ID", "File name", "Category", "Related record",
            "Uploaded", "Size", "Checksum", "Status",
        ])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        root.addWidget(self.table)
        self.documents = []
        self.access_note = QLabel()
        self.access_note.setObjectName("muted")
        root.addWidget(self.access_note)

    def load_data(self, *_args):
        try:
            if not _has_permission("documents.view"):
                raise AuthorizationError("Document view permission is required.")
            with db_connection.get_db() as session:
                self.documents = list_documents(
                    session,
                    actor_context=_actor_context(),
                    include_archived=bool(self.include_archived.currentData()),
                    search=self.search.text(),
                )
            can_manage = _has_permission("documents.manage")
            self.upload_button.setVisible(can_manage)
            self.open_button.setVisible(True)
            self.archive_button.setVisible(can_manage)
            self.access_note.clear()
        except AuthorizationError as exc:
            self.clear_protected_data()
            self.access_note.setText(str(exc))
            return
        except Exception as exc:
            self.clear_protected_data()
            QMessageBox.warning(self, "Unable to load documents", str(exc))
            return
        self.table.setRowCount(len(self.documents))
        for row, document in enumerate(self.documents):
            values = (
                document.document_code,
                document.original_filename,
                document.category,
                f"{document.related_entity_type} {document.related_entity_id or ''}".strip(),
                document.uploaded_at.strftime("%Y-%m-%d %H:%M"),
                f"{document.file_size:,} bytes",
                document.checksum_sha256[:16],
                document.status,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
            self.table.item(row, 0).setData(256, document.id)

    def clear_protected_data(self):
        self.documents = []
        self.table.setRowCount(0)
        self.upload_button.setVisible(False)
        self.open_button.setVisible(False)
        self.archive_button.setVisible(False)

    def upload(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Select document", "", "Supported documents (*.pdf *.png *.jpg *.jpeg *.webp *.txt *.csv *.xlsx *.docx)"
        )
        if not filename:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Document details")
        form = QFormLayout(dialog)
        category = QComboBox()
        category.addItems(sorted(DOCUMENT_CATEGORIES))
        related_type = QComboBox()
        related_type.addItems(sorted(RELATED_ENTITY_TYPES))
        related_id = QLineEdit()
        description = QLineEdit()
        notes = QLineEdit()
        form.addRow("Category", category)
        form.addRow("Related record type", related_type)
        form.addRow("Related record ID (optional)", related_id)
        form.addRow("Description", description)
        form.addRow("Notes", notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            entity_id = int(related_id.text()) if related_id.text().strip() else None
            with db_connection.get_db() as session:
                configured_size = int(
                    get_setting(
                        session,
                        "max_document_size_mb",
                        "25",
                        actor_context=_actor_context(),
                    )
                    or "25"
                )
            maximum_size = min(configured_size * 1024 * 1024, MAX_ABSOLUTE_SIZE)
            if Path(filename).stat().st_size > maximum_size:
                raise ValueError("The selected document exceeds the configured file-size limit.")
            with open(filename, "rb") as file:
                content = file.read(maximum_size + 1)
            if len(content) > maximum_size:
                raise ValueError("The selected document exceeds the configured file-size limit.")
            with db_connection.get_db() as session:
                upload_document(
                    session,
                    actor_context=_actor_context(),
                    filename=Path(filename).name,
                    content=content,
                    category=category.currentText(),
                    related_entity_type=related_type.currentText(),
                    related_entity_id=entity_id,
                    description=description.text(),
                    notes=notes.text(),
                )
            self.load_data()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Could not upload document", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Could not upload document", str(exc))

    def _selected_id(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).data(256) if row >= 0 else None

    def open_selected(self):
        document_id = self._selected_id()
        if document_id is None:
            return
        try:
            with db_connection.get_db() as session:
                document, content = get_document_content(
                    session, int(document_id), actor_context=_actor_context()
                )
                suggested = document.original_filename
            target, _ = QFileDialog.getSaveFileName(self, "Save verified document", suggested)
            if target:
                Path(target).write_bytes(content)
                QDesktopServices.openUrl(QUrl.fromLocalFile(target))
        except Exception as exc:
            QMessageBox.warning(self, "Could not open document", str(exc))

    def archive_selected(self):
        document_id = self._selected_id()
        if document_id is None:
            return
        reason, accepted = QInputDialog.getText(self, "Archive document", "Reason for archiving:")
        if not accepted or not reason.strip():
            return
        if QMessageBox.question(
            self, "Confirm archive",
            "Archive this document? The stored file will be retained and not physically deleted.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with db_connection.get_db() as session:
                archive_document(
                    session,
                    int(document_id),
                    reason=reason,
                    actor_context=_actor_context(),
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not archive document", str(exc))


class BackupsTab(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        note = QLabel(
            "Restore replaces the active SQLite database. A fresh verified safety backup is created first; "
            "the confirmation is explicit, and existing backup files are never overwritten."
        )
        note.setWordWrap(True)
        root.addWidget(note)
        buttons = QHBoxLayout()
        self.create_button = QPushButton("Create verified backup")
        self.create_button.clicked.connect(self.create)
        self.create_button.setVisible(False)
        self.verify_button = QPushButton("Verify selected")
        self.verify_button.clicked.connect(self.verify_selected)
        self.verify_button.setVisible(False)
        self.restore_button = QPushButton("Restore selected backup…")
        self.restore_button.clicked.connect(self.restore_selected)
        self.restore_button.setVisible(False)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.load_data)
        for button in (
            self.create_button, self.verify_button, self.restore_button, refresh
        ):
            buttons.addWidget(button)
        buttons.addStretch()
        root.addLayout(buttons)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Filename", "Size", "SHA-256", "Integrity", "Path"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        root.addWidget(self.table)
        self.records: list[dict[str, object]] = []
        self.access_note = QLabel()
        self.access_note.setObjectName("muted")
        root.addWidget(self.access_note)

    def load_data(self):
        try:
            actor_context = _actor_context()
            if not _has_permission("backups.view"):
                raise AuthorizationError("Backup view permission is required.")
            with db_connection.get_db() as session:
                directory = _backup_directory(
                    session, "backups.view", actor_context
                )
                self.records = list_backups(
                    session, directory, actor_context=actor_context
                )
            self.create_button.setVisible(
                _has_permission("backups.create")
            )
            self.verify_button.setVisible(True)
            self.restore_button.setVisible(
                _has_permission("backups.restore")
            )
            self.access_note.clear()
            self.table.setRowCount(len(self.records))
            for row, record in enumerate(self.records):
                values = (
                    record["filename"],
                    f'{record["size"]:,} bytes',
                    record["sha256"],
                    record["integrity"],
                    record["path"],
                )
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QTableWidgetItem(str(value)))
                self.table.item(row, 0).setData(256, record["path"])
        except AuthorizationError as exc:
            self.clear_protected_data()
            self.access_note.setText(str(exc))
        except Exception as exc:
            self.clear_protected_data()
            QMessageBox.critical(self, "Unable to list backups", str(exc))

    def clear_protected_data(self):
        self.records = []
        self.table.setRowCount(0)
        self.create_button.setVisible(False)
        self.verify_button.setVisible(False)
        self.restore_button.setVisible(False)

    def create(self):
        try:
            actor_context = _actor_context()
            with db_connection.get_db() as session:
                directory = _backup_directory(
                    session, "backups.create", actor_context
                )
                record = create_backup(
                    session,
                    _db_path(),
                    directory,
                    actor_context=actor_context,
                )
            QMessageBox.information(
                self, "Backup verified",
                f'File: {record["path"]}\nSize: {record["size"]:,} bytes\n'
                f'SHA-256: {record["sha256"]}\nIntegrity: {record["integrity"]}',
            )
            self.load_data()
        except Exception as exc:
            QMessageBox.critical(self, "Backup failed", str(exc))

    def _selected_path(self) -> Path | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        return Path(str(self.table.item(row, 0).data(256)))

    def verify_selected(self):
        path = self._selected_path()
        if not path:
            return
        try:
            actor_context = _actor_context()
            with db_connection.get_db() as session:
                result = verify_backup(
                    session, path, actor_context=actor_context
                )
            QMessageBox.information(
                self,
                "Backup verification",
                f'Integrity: {result["integrity"]}\nSchema: {result["details"]}\n'
                f'SHA-256: {result["sha256"]}',
            )
        except Exception as exc:
            QMessageBox.critical(self, "Verification failed", str(exc))

    def restore_selected(self):
        path = self._selected_path()
        if not path:
            return
        confirmation = QMessageBox.question(
            self,
            "Confirm database restore",
            f"Replace the active database with:\n{path}\n\n"
            "The application will create a fresh verified safety backup first. "
            "This cannot be undone from this screen. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmation != QMessageBox.Yes:
            return
        try:
            actor_context = _actor_context()
            with db_connection.get_db() as session:
                directory = _backup_directory(
                    session, "backups.restore", actor_context
                )
                result = restore_backup(
                    session,
                    _db_path(),
                    path,
                    actor_context=actor_context,
                    confirmed=True,
                    engine=db_connection.engine,
                    backup_directory=directory,
                )
            QMessageBox.information(
                self,
                "Restore completed",
                f'Integrity: {result["integrity"]}\n'
                f'Safety backup: {result["safety_backup"]}\n'
                "Restart the application before continuing work.",
            )
            self.window().close()
        except (BackupValidationError, OSError) as exc:
            QMessageBox.critical(self, "Restore failed", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Restore failed", str(exc))
