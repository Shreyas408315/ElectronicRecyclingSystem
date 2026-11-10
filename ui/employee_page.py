from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
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
from database.connection import get_db
from database.models import Employee, ManagedDocument
from services.document_service import (
    archive_document,
    get_document_content,
    list_documents,
    update_document_metadata,
    upload_document,
)
from services.employee_report_service import (
    REPORT_TYPES,
    generate_employee_excel_report,
    generate_employee_pdf_report,
)
from services.employee_service import (
    EMPLOYMENT_STATUSES,
    EMPLOYMENT_TYPES,
    EmployeeDuplicateWarning,
    EmployeeValidationError,
    create_department,
    create_designation,
    create_employee,
    deactivate_employee,
    find_possible_duplicate_employees,
    get_employee_by_id,
    get_employee_summary,
    list_departments,
    list_designations,
    list_employees,
    reactivate_employee,
    seed_default_departments,
    separate_employee,
    set_department_active,
    set_designation_active,
    update_department,
    update_designation,
    update_employee,
)
from services.security_service import (
    ActorContext,
    AuthorizationError,
    actor_has_permission,
    current_actor_context,
)


_NO_DATE = QDate(2000, 1, 1)


def _optional_date(control: QDateEdit) -> date | None:
    value = control.date().toPython()
    return None if value == _NO_DATE.toPython() else value


def _set_combo_data(combo: QComboBox, value):
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


class EmployeeFormDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        employee_id: int | None = None,
        actor_context: ActorContext,
        can_view_private: bool,
    ):
        super().__init__(parent)
        self.employee_id = employee_id
        self.actor_context = actor_context
        self.can_view_private = can_view_private
        self._submitting = False
        self._photo_source: Path | None = None
        self.setWindowTitle("Edit Employee" if employee_id else "Add Employee")
        self.resize(690, 640)
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        personal_tab = QWidget()
        personal = QFormLayout(personal_tab)
        self.full_name = QLineEdit()
        self.phone = QLineEdit()
        self.alternate_phone = QLineEdit()
        self.email = QLineEdit()
        self.address = QLineEdit()
        self.city = QLineEdit()
        self.state = QLineEdit()
        self.pin_code = QLineEdit()
        for label, field in (
            ("Full name *", self.full_name),
            ("Phone", self.phone),
            ("Alternate phone", self.alternate_phone),
            ("Email", self.email),
            ("Residential address", self.address),
            ("City", self.city),
            ("State", self.state),
            ("PIN code", self.pin_code),
        ):
            personal.addRow(label, field)
            if label != "Full name *" and not can_view_private:
                personal.removeRow(field)
        self.tabs.addTab(personal_tab, "Personal")

        employment_tab = QWidget()
        employment = QFormLayout(employment_tab)
        self.employee_code = QLineEdit()
        self.employee_code.setReadOnly(True)
        self.employee_code.setPlaceholderText("Assigned automatically")
        self.department = QComboBox()
        self.designation = QComboBox()
        self.employment_type = QComboBox()
        self.employment_type.addItems(EMPLOYMENT_TYPES)
        self.status = QComboBox()
        self.status.addItems(EMPLOYMENT_STATUSES)
        self.joining = QDateEdit()
        self.joining.setCalendarPopup(True)
        self.joining.setDisplayFormat("dd-MMM-yyyy")
        self.joining.setDate(QDate.currentDate())
        self.leaving = QDateEdit()
        self.leaving.setCalendarPopup(True)
        self.leaving.setDisplayFormat("dd-MMM-yyyy")
        self.leaving.setSpecialValueText("Not set")
        self.leaving.setMinimumDate(_NO_DATE)
        self.leaving.setDate(_NO_DATE)
        self.leaving_check = QCheckBox("Set date of leaving")
        self.leaving_check.toggled.connect(self.leaving.setEnabled)
        self.leaving.setEnabled(False)
        self.leaving_row = QHBoxLayout()
        self.leaving_row.addWidget(self.leaving_check)
        self.leaving_row.addWidget(self.leaving)
        self._load_choices()
        employment.addRow("Employee ID", self.employee_code)
        employment.addRow("Department *", self.department)
        employment.addRow("Designation *", self.designation)
        employment.addRow("Employment type *", self.employment_type)
        employment.addRow("Date of joining *", self.joining)
        employment.addRow("Employment status *", self.status)
        employment.addRow("Date of leaving", self.leaving_row)
        self.department.currentIndexChanged.connect(self._filter_designations)
        self.tabs.addTab(employment_tab, "Employment")

        emergency_tab = QWidget()
        emergency = QFormLayout(emergency_tab)
        self.emergency_name = QLineEdit()
        self.emergency_phone = QLineEdit()
        self.notes = QLineEdit()
        self.responsible_user = QLineEdit()
        emergency.addRow("Emergency contact name (optional)", self.emergency_name)
        emergency.addRow("Emergency contact phone (optional)", self.emergency_phone)
        emergency.addRow("Notes", self.notes)
        emergency.addRow("Record maintained by (optional)", self.responsible_user)
        note = QLabel(
            "The maintained-by field is a record note, not a verified identity or electronic signature."
        )
        note.setWordWrap(True)
        emergency.addRow(note)
        self.tabs.addTab(emergency_tab, "Additional")

        photo_tab = QWidget()
        photo_layout = QVBoxLayout(photo_tab)
        self.photo_label = QLabel("No profile photograph selected.")
        self.photo_label.setWordWrap(True)
        photo_layout.addWidget(self.photo_label)
        photo_button = QPushButton("Choose profile photograph")
        photo_button.clicked.connect(self._choose_photo)
        photo_layout.addWidget(photo_button)
        photo_layout.addWidget(QLabel(
            "The photograph is saved through managed document storage. "
            "Before storing real employee documents, configure Windows storage permissions and full-disk encryption."
        ))
        photo_layout.addStretch()
        self.tabs.addTab(photo_tab, "Photograph")
        if not can_view_private:
            self.tabs.removeTab(self.tabs.indexOf(emergency_tab))
            self.tabs.removeTab(self.tabs.indexOf(photo_tab))

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        if employee_id:
            self._load_employee()

    def _load_choices(self):
        with get_db() as session:
            departments = list_departments(session, actor_context=self.actor_context)
            designations = list_designations(session, actor_context=self.actor_context)
        self._departments = departments
        self._designations = designations
        self.department.clear()
        for department in departments:
            label = department.name + ("" if department.is_active else " (inactive)")
            self.department.addItem(label, department.id)
        self._filter_designations()

    def _filter_designations(self, *_args):
        current = self.designation.currentData()
        department_id = self.department.currentData()
        self.designation.blockSignals(True)
        self.designation.clear()
        for designation in self._designations:
            if designation.is_active or designation.id == current:
                self.designation.addItem(
                    designation.name + ("" if designation.is_active else " (inactive)"),
                    designation.id,
                )
        self.designation.blockSignals(False)
        _set_combo_data(self.designation, current)

    def _load_employee(self):
        with get_db() as session:
            employee = get_employee_by_id(
                session,
                self.employee_id,
                actor_context=self.actor_context,
                include_private=self.can_view_private,
            )
            if employee is None:
                raise EmployeeValidationError("Employee no longer exists.")
            values = {
                "employee_code": employee.employee_code,
                "full_name": employee.full_name,
                "phone": employee.phone or "",
                "alternate_phone": employee.alternate_phone or "",
                "email": employee.email or "",
                "residential_address": employee.residential_address or "",
                "city": employee.city or "",
                "state": employee.state or "",
                "pin_code": employee.pin_code or "",
                "department_id": employee.department_id,
                "designation_id": employee.designation_id,
                "employment_type": employee.employment_type,
                "employment_status": employee.employment_status,
                "date_of_joining": employee.date_of_joining,
                "date_of_leaving": employee.date_of_leaving,
                "emergency_contact_name": employee.emergency_contact_name or "",
                "emergency_contact_phone": employee.emergency_contact_phone or "",
                "notes": employee.notes or "",
                "responsible_user": employee.responsible_user or "",
                "profile_photo_path": employee.profile_photo_path,
            }
        self.employee_code.setText(values["employee_code"])
        self.full_name.setText(values["full_name"])
        self.phone.setText(values["phone"])
        self.alternate_phone.setText(values["alternate_phone"])
        self.email.setText(values["email"])
        self.address.setText(values["residential_address"])
        self.city.setText(values["city"])
        self.state.setText(values["state"])
        self.pin_code.setText(values["pin_code"])
        _set_combo_data(self.department, values["department_id"])
        _set_combo_data(self.designation, values["designation_id"])
        self.employment_type.setCurrentText(values["employment_type"])
        self.status.setCurrentText(values["employment_status"])
        self.joining.setDate(QDate(
            values["date_of_joining"].year,
            values["date_of_joining"].month,
            values["date_of_joining"].day,
        ))
        if values["date_of_leaving"]:
            self.leaving_check.setChecked(True)
            leaving = values["date_of_leaving"]
            self.leaving.setDate(QDate(leaving.year, leaving.month, leaving.day))
        self.emergency_name.setText(values["emergency_contact_name"])
        self.emergency_phone.setText(values["emergency_contact_phone"])
        self.notes.setText(values["notes"])
        self.responsible_user.setText(values["responsible_user"])
        self.photo_label.setText(values["profile_photo_path"] or "No profile photograph stored.")

    def _choose_photo(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Select profile photograph", "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if filename:
            self._photo_source = Path(filename)
            self.photo_label.setText(str(self._photo_source))

    def _payload(self) -> dict:
        values = {
            "full_name": self.full_name.text(),
            "department_id": self.department.currentData(),
            "designation_id": self.designation.currentData(),
            "employment_type": self.employment_type.currentText(),
            "employment_status": self.status.currentText(),
            "date_of_joining": self.joining.date().toPython(),
            "date_of_leaving": self.leaving.date().toPython() if self.leaving_check.isChecked() else None,
        }
        if self.can_view_private:
            values.update({
                "phone": self.phone.text(),
                "alternate_phone": self.alternate_phone.text(),
                "email": self.email.text(),
                "residential_address": self.address.text(),
                "city": self.city.text(),
                "state": self.state.text(),
                "pin_code": self.pin_code.text(),
                "emergency_contact_name": self.emergency_name.text(),
                "emergency_contact_phone": self.emergency_phone.text(),
                "notes": self.notes.text(),
                "responsible_user": self.responsible_user.text(),
            })
        return values

    def _save(self):
        if self._submitting:
            return
        self._submitting = True
        self.save_button.setEnabled(False)
        values = self._payload()
        allow_duplicate = False
        try:
            with get_db() as session:
                if self.employee_id is None:
                    duplicate_values = {"full_name": values["full_name"]}
                else:
                    duplicate_values = {
                        "full_name": values["full_name"],
                        "exclude_employee_id": self.employee_id,
                    }
                if self.can_view_private:
                    duplicate_values.update({
                        "phone": values.get("phone"),
                        "email": values.get("email"),
                    })
                duplicates = find_possible_duplicate_employees(
                    session,
                    actor_context=self.actor_context,
                    **duplicate_values,
                )
            if duplicates:
                description = "\n".join(
                    f"{employee.employee_code} — {employee.full_name} ({employee.department.name})"
                    for employee in duplicates[:5]
                )
                answer = QMessageBox.question(
                    self,
                    "Possible duplicate employee",
                    f"Potentially matching employee records were found:\n{description}\n\n"
                    "No records will be merged. Continue with a separate employee record?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
                allow_duplicate = True
            with get_db() as session:
                if self.employee_id is None:
                    employee = create_employee(
                        session,
                        values,
                        actor_context=self.actor_context,
                        allow_possible_duplicate=allow_duplicate,
                    )
                else:
                    values["allow_possible_duplicate"] = allow_duplicate
                    employee = update_employee(
                        session, self.employee_id, values, actor_context=self.actor_context
                    )
                if self._photo_source:
                    from services.document_service import upload_document

                    content = self._photo_source.read_bytes()
                    document = upload_document(
                        session,
                        actor_context=self.actor_context,
                        filename=self._photo_source.name,
                        content=content,
                        category="Employee Profile Photograph",
                        related_entity_type="Employee",
                        related_entity_id=employee.id,
                        description=f"Profile photograph for {employee.employee_code}",
                    )
                    employee.profile_photo_path = document.storage_path
            self.accept()
        except EmployeeDuplicateWarning as exc:
            QMessageBox.warning(self, "Possible duplicate employee", str(exc))
        except AuthorizationError as exc:
            self.reject()
            QMessageBox.warning(self, "Access denied", str(exc))
        except (EmployeeValidationError, OSError) as exc:
            QMessageBox.warning(self, "Could not save employee", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Could not save employee", str(exc))
        finally:
            self._submitting = False
            self.save_button.setEnabled(True)


class DepartmentDesignationDialog(QDialog):
    def __init__(self, parent=None, *, actor_context: ActorContext):
        super().__init__(parent)
        self.actor_context = actor_context
        self.setWindowTitle("Departments & Designations")
        self.resize(720, 480)
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self.department_table = self._make_table(
            ["Department", "Status"], ["Departments", "Designations"]
        )
        self.designation_table = self._make_table(
            ["Designation", "Status"], ["Designations", "Departments"]
        )
        self.tabs.addTab(self._catalog_tab(
            self.department_table, add_text="Add department",
            add_callback=self._add_department,
            edit_callback=self._edit_department,
            toggle_callback=self._toggle_department,
        ), "Departments")
        self.tabs.addTab(self._catalog_tab(
            self.designation_table, add_text="Add designation",
            add_callback=self._add_designation,
            edit_callback=self._edit_designation,
            toggle_callback=self._toggle_designation,
        ), "Designations")
        self.load_data()

    @staticmethod
    def _make_table(headers, _unused):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        return table

    def _catalog_tab(self, table, *, add_text, add_callback, edit_callback, toggle_callback):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(table)
        actions = QHBoxLayout()
        for text, callback in (
            (add_text, add_callback),
            ("Rename selected", edit_callback),
            ("Activate / deactivate", toggle_callback),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        return page

    def load_data(self):
        with get_db() as session:
            departments = list_departments(session, actor_context=self.actor_context)
            designations = list_designations(session, actor_context=self.actor_context)
        self._fill_table(self.department_table, departments)
        self._fill_table(self.designation_table, designations)

    @staticmethod
    def _fill_table(table, records):
        table.setRowCount(len(records))
        for row, record in enumerate(records):
            table.setItem(row, 0, QTableWidgetItem(record.name))
            table.setItem(row, 1, QTableWidgetItem("Active" if record.is_active else "Inactive"))
            table.item(row, 0).setData(Qt.UserRole, record.id)
            table.item(row, 0).setData(Qt.UserRole + 1, record.is_active)

    def _selected(self, table):
        row = table.currentRow()
        if row < 0:
            return None, None, None
        item = table.item(row, 0)
        return item.data(Qt.UserRole), item.text(), item.data(Qt.UserRole + 1)

    def _ask_name(self, title, prompt, default=""):
        value, accepted = QInputDialog.getText(self, title, prompt, text=default)
        return value.strip() if accepted and value.strip() else None

    def _add_department(self):
        name = self._ask_name("Add department", "Department name:")
        if not name:
            return
        try:
            with get_db() as session:
                create_department(session, name, actor_context=self.actor_context)
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not add department", str(exc))

    def _edit_department(self):
        department_id, name, _active = self._selected(self.department_table)
        if department_id is None:
            return
        updated = self._ask_name("Rename department", "Department name:", name)
        if not updated:
            return
        try:
            with get_db() as session:
                update_department(
                    session, department_id, updated, actor_context=self.actor_context
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not rename department", str(exc))

    def _toggle_department(self):
        department_id, name, active = self._selected(self.department_table)
        if department_id is None:
            return
        target = not bool(active)
        prompt = (
            f"{'Deactivate' if not target else 'Reactivate'} department '{name}'? "
            "Employee history and assignments will be retained."
        )
        if QMessageBox.question(self, "Confirm department status", prompt) != QMessageBox.Yes:
            return
        try:
            with get_db() as session:
                set_department_active(
                    session, department_id, target, actor_context=self.actor_context
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not update department", str(exc))

    def _add_designation(self):
        name = self._ask_name("Add designation", "Designation name:")
        if not name:
            return
        try:
            with get_db() as session:
                create_designation(session, name, actor_context=self.actor_context)
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not add designation", str(exc))

    def _edit_designation(self):
        designation_id, name, _active = self._selected(self.designation_table)
        if designation_id is None:
            return
        updated = self._ask_name("Rename designation", "Designation name:", name)
        if not updated:
            return
        try:
            with get_db() as session:
                update_designation(
                    session, designation_id, updated, actor_context=self.actor_context
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not rename designation", str(exc))

    def _toggle_designation(self):
        designation_id, name, active = self._selected(self.designation_table)
        if designation_id is None:
            return
        target = not bool(active)
        prompt = (
            f"{'Reactivate' if target else 'Deactivate'} designation '{name}'? "
            "Employee history and assignments will be retained."
        )
        if QMessageBox.question(self, "Confirm designation status", prompt) != QMessageBox.Yes:
            return
        try:
            with get_db() as session:
                set_designation_active(
                    session, designation_id, target, actor_context=self.actor_context
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not update designation", str(exc))


class EmployeeDetailDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        employee_id: int,
        actor_context: ActorContext,
        can_view_private: bool,
        can_manage: bool,
    ):
        super().__init__(parent)
        self.employee_id = employee_id
        self.actor_context = actor_context
        self.can_view_private = can_view_private
        self.can_manage = can_manage
        self.setWindowTitle("Employee Details")
        self.resize(900, 650)
        root = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setObjectName("heading")
        root.addWidget(self.heading)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self.overview = QWidget()
        self.overview_layout = QFormLayout(self.overview)
        self.photo = QLabel()
        self.photo.setFixedSize(140, 140)
        self.photo.setScaledContents(True)
        self.tabs.addTab(self.overview, "Overview")
        self.history_table = QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels([
            "Date", "Event", "Previous status", "New status",
            "Previous department", "Department", "Details",
        ])
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        if not can_view_private:
            self.history_table.setColumnHidden(6, True)
        self.tabs.addTab(self.history_table, "Employment History")
        self.documents_tab = QWidget()
        document_layout = QVBoxLayout(self.documents_tab)
        actions = QHBoxLayout()
        upload = QPushButton("Upload document")
        upload.clicked.connect(self.upload_document)
        open_button = QPushButton("Open / save selected")
        open_button.clicked.connect(self.open_document)
        edit_button = QPushButton("Edit description")
        edit_button.clicked.connect(self.edit_document)
        archive_button = QPushButton("Archive selected")
        archive_button.clicked.connect(self.archive_document)
        for button in (upload, open_button, edit_button, archive_button):
            actions.addWidget(button)
        upload.setVisible(can_manage)
        edit_button.setVisible(can_manage)
        archive_button.setVisible(can_manage)
        actions.addStretch()
        document_layout.addLayout(actions)
        self.document_table = QTableWidget(0, 5)
        self.document_table.setHorizontalHeaderLabels([
            "Document ID", "Category", "File name", "Upload date", "Description",
        ])
        self.document_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.document_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.document_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        document_layout.addWidget(self.document_table)
        self.tabs.addTab(self.documents_tab, "Documents")
        if not can_view_private:
            self.tabs.removeTab(self.tabs.indexOf(self.documents_tab))
        responsibility = QLabel(
            "No linked operational responsibility history is available. Existing responsible-user "
            "text remains unchanged and is not automatically attributed to an employee."
        )
        responsibility.setWordWrap(True)
        self.tabs.addTab(responsibility, "Operational Responsibility")
        self.documents = []
        self.load_data()

    def load_data(self):
        try:
            with get_db() as session:
                employee = get_employee_by_id(
                    session,
                    self.employee_id,
                    actor_context=self.actor_context,
                    include_private=self.can_view_private,
                )
                if employee is None:
                    raise EmployeeValidationError("Employee no longer exists.")
                self.employee = employee
                self.documents = list_documents(
                    session,
                    actor_context=self.actor_context,
                    include_archived=True,
                    related_entity_type="Employee",
                    related_entity_id=self.employee_id,
                ) if self.can_view_private else []
                self._render_overview(employee)
                history = employee.history
        except AuthorizationError:
            self._clear_data()
            self.reject()
        self.history_table.setRowCount(len(history))
        for row, event in enumerate(history):
            values = (
                event.event_date.isoformat(),
                event.event_type.replace("_", " ").title(),
                event.previous_status or "",
                event.new_status or "",
                event.previous_department or "",
                event.new_department or "",
                event.details or "",
            )
            for col, value in enumerate(values):
                self.history_table.setItem(row, col, QTableWidgetItem(value))
        self.document_table.setRowCount(len(self.documents))
        for row, document in enumerate(self.documents):
            values = (
                document.document_code,
                document.category,
                document.original_filename,
                document.uploaded_at.strftime("%Y-%m-%d"),
                document.description or "",
            )
            for col, value in enumerate(values):
                self.document_table.setItem(row, col, QTableWidgetItem(value))
            self.document_table.item(row, 0).setData(Qt.UserRole, document.id)
            self.document_table.item(row, 0).setData(Qt.UserRole + 1, document.status)

    def _clear_data(self):
        self.documents = []
        self.history_table.setRowCount(0)
        self.document_table.setRowCount(0)
        while self.overview_layout.rowCount():
            self.overview_layout.removeRow(0)
        self.heading.clear()
        self.photo.clear()

    def _render_overview(self, employee: Employee):
        self.heading.setText(f"{employee.employee_code} — {employee.full_name}")
        while self.overview_layout.rowCount():
            self.overview_layout.removeRow(0)
        self.overview_layout.addRow("Profile photograph", self.photo)
        image_loaded = False
        if employee.profile_photo_path:
            for document in self.documents:
                if document.storage_path == employee.profile_photo_path and document.status == "Active":
                    try:
                        with get_db() as session:
                            _metadata, content = get_document_content(
                                session, document.id, actor_context=self.actor_context
                            )
                        pixmap = QPixmap()
                        if pixmap.loadFromData(content):
                            self.photo.setPixmap(pixmap)
                            image_loaded = True
                    except Exception:
                        image_loaded = False
                    break
        if not image_loaded:
            self.photo.setText("No photograph available")
        fields = [
            ("Full name", employee.full_name),
            ("Employee ID", employee.employee_code),
            ("Department", employee.department.name),
            ("Designation", employee.designation.name),
            ("Employment type", employee.employment_type),
            ("Date of joining", employee.date_of_joining.isoformat()),
            ("Status", employee.employment_status),
            ("Date of leaving", employee.date_of_leaving.isoformat() if employee.date_of_leaving else "—"),
        ]
        if self.can_view_private:
            fields.extend((
                ("Phone", employee.phone or "—"),
                ("Alternate phone", employee.alternate_phone or "—"),
                ("Email", employee.email or "—"),
                ("Residential address", employee.residential_address or "—"),
                ("City / state / PIN", " / ".join(
                    value for value in (employee.city, employee.state, employee.pin_code) if value
                ) or "—"),
                ("Emergency contact", employee.emergency_contact_name or "—"),
                ("Emergency phone", employee.emergency_contact_phone or "—"),
                ("Notes", employee.notes or "—"),
            ))
        for label, value in fields:
            text = QLabel(value)
            text.setWordWrap(True)
            self.overview_layout.addRow(label, text)

    def _selected_document_id(self):
        row = self.document_table.currentRow()
        return self.document_table.item(row, 0).data(Qt.UserRole) if row >= 0 else None

    def upload_document(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Select employee document",
            "",
            "Supported documents (*.pdf *.png *.jpg *.jpeg *.webp *.txt *.csv *.xlsx *.docx)",
        )
        if not filename:
            return
        types = (
            "Appointment Letter",
            "Employment Agreement",
            "Training Certificate",
            "Qualification Certificate",
            "Employment Record",
            "Other",
        )
        category, accepted = QInputDialog.getItem(
            self, "Document category", "Category:", types, 0, False
        )
        if not accepted:
            return
        description, accepted = QInputDialog.getText(
            self, "Document description", "Description (optional):"
        )
        if not accepted:
            return
        try:
            with open(filename, "rb") as file:
                content = file.read()
            with get_db() as session:
                upload_document(
                    session,
                    actor_context=self.actor_context,
                    filename=Path(filename).name,
                    content=content,
                    category=category,
                    related_entity_type="Employee",
                    related_entity_id=self.employee_id,
                    description=description,
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not upload employee document", str(exc))

    def open_document(self):
        document_id = self._selected_document_id()
        if document_id is None:
            return
        try:
            with get_db() as session:
                document, content = get_document_content(
                    session, int(document_id), actor_context=self.actor_context
                )
            target, _ = QFileDialog.getSaveFileName(
                self, "Save verified document", document.original_filename
            )
            if target:
                Path(target).write_bytes(content)
                QDesktopServices.openUrl(QUrl.fromLocalFile(target))
        except Exception as exc:
            QMessageBox.warning(self, "Could not open document", str(exc))

    def edit_document(self):
        document_id = self._selected_document_id()
        if document_id is None:
            return
        document = next((item for item in self.documents if item.id == document_id), None)
        if document is None or document.status != "Active":
            QMessageBox.warning(self, "Document is not editable", "Only active employee documents can be edited.")
            return
        description, accepted = QInputDialog.getText(
            self,
            "Edit document description",
            "Description:",
            text=document.description or "",
        )
        if not accepted:
            return
        try:
            with get_db() as session:
                update_document_metadata(
                    session,
                    int(document_id),
                    actor_context=self.actor_context,
                    description=description,
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not update document", str(exc))

    def archive_document(self):
        document_id = self._selected_document_id()
        if document_id is None:
            return
        reason, accepted = QInputDialog.getText(self, "Archive employee document", "Reason:")
        if not accepted or not reason.strip():
            return
        if QMessageBox.question(
            self,
            "Confirm document archive",
            "Archive this document? The file is retained and will not be physically deleted.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as session:
                archive_document(
                    session,
                    int(document_id),
                    actor_context=self.actor_context,
                    reason=reason,
                )
            self.load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Could not archive document", str(exc))


class EmployeePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._init_ui()
        self._refresh_access()
        self.load_data()

    def _refresh_access(self):
        self.actor_context = current_actor_context()
        self.can_view = False
        self.can_manage = False
        self.can_view_private = False
        self.can_export_private = False
        if self.actor_context is not None:
            with get_db() as session:
                self.can_view = actor_has_permission(
                    self.actor_context, "employees.view", db_session=session
                )
                if self.can_view:
                    self.can_manage = actor_has_permission(
                        self.actor_context, "employees.manage", db_session=session
                    )
                    self.can_view_private = actor_has_permission(
                        self.actor_context, "employees.private.view", db_session=session
                    )
                    self.can_export_private = actor_has_permission(
                        self.actor_context, "employees.export_private", db_session=session
                    )
        self.add_button.setVisible(self.can_manage)
        self.manage_button.setVisible(self.can_manage)
        self.table.setColumnHidden(5, not self.can_view_private)
        self.include_private.setVisible(
            self.can_view_private and self.can_export_private
        )
        self.private_export_controls.setVisible(self.can_view)
        self.search.setPlaceholderText(
            "Search employee ID or name"
            if not self.can_view_private
            else "Search employee ID, name, phone or email"
        )

    def _clear_data(self):
        self.table.setRowCount(0)
        self.count_label.setText("Showing 0 employees")
        for value in self.metrics.values():
            value.setText("—")
        self.department_distribution.setText("Department distribution: —")
        for button in (
            self.view_button,
            self.edit_button,
            self.status_button,
            self.separate_button,
        ):
            button.setEnabled(False)
        self.include_private.setChecked(False)

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Employee Management")
        title.setObjectName("heading")
        subtitle = QLabel("Maintain employee profiles, employment history and managed documents.")
        subtitle.setObjectName("muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.add_button = QPushButton("+ Add Employee")
        self.add_button.clicked.connect(self.add_employee)
        self.manage_button = QPushButton("Departments & Designations")
        self.manage_button.clicked.connect(self.manage_catalogues)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.load_data)
        header.addWidget(self.manage_button)
        header.addWidget(refresh)
        header.addWidget(self.add_button)
        root.addLayout(header)

        card_grid = QGridLayout()
        self.metrics: dict[str, QLabel] = {}
        for index, (key, caption) in enumerate((
            ("total", "Total employees"),
            ("active", "Active"),
            ("on_leave", "On leave"),
            ("inactive", "Inactive"),
            ("separated", "Separated"),
        )):
            card = QFrame()
            card.setObjectName("metricCard")
            box = QVBoxLayout(card)
            name = QLabel(caption)
            name.setObjectName("muted")
            value = QLabel("0")
            value.setObjectName("metricBigValue")
            box.addWidget(name)
            box.addWidget(value)
            card_grid.addWidget(card, 0, index)
            self.metrics[key] = value
        root.addLayout(card_grid)
        self.department_distribution = QLabel("Department distribution: —")
        self.department_distribution.setWordWrap(True)
        root.addWidget(self.department_distribution)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search employee ID, name, phone or email")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.load_employees)
        self.department_filter = QComboBox()
        self.designation_filter = QComboBox()
        self.type_filter = QComboBox()
        self.type_filter.addItem("All employment types", "ALL")
        self.type_filter.addItems(EMPLOYMENT_TYPES)
        self.status_filter = QComboBox()
        self.status_filter.addItem("All statuses", "ALL")
        for status in EMPLOYMENT_STATUSES:
            self.status_filter.addItem(status, status)
        self.joining_from = QDateEdit()
        self.joining_from.setCalendarPopup(True)
        self.joining_from.setDisplayFormat("yyyy-MM-dd")
        self.joining_from.setSpecialValueText("Joining from")
        self.joining_from.setMinimumDate(_NO_DATE)
        self.joining_from.setDate(_NO_DATE)
        self.joining_to = QDateEdit()
        self.joining_to.setCalendarPopup(True)
        self.joining_to.setDisplayFormat("yyyy-MM-dd")
        self.joining_to.setSpecialValueText("Joining to")
        self.joining_to.setMinimumDate(_NO_DATE)
        self.joining_to.setDate(_NO_DATE)
        self.sort_filter = QComboBox()
        for text, value in (
            ("Name A-Z", "name_asc"),
            ("Name Z-A", "name_desc"),
            ("Joining date - newest", "joining_desc"),
            ("Joining date - oldest", "joining_asc"),
            ("Employee ID", "code_asc"),
        ):
            self.sort_filter.addItem(text, value)
        for control in (
            self.department_filter, self.designation_filter, self.type_filter,
            self.status_filter, self.sort_filter,
        ):
            control.currentIndexChanged.connect(self.load_employees)
        self.joining_from.dateChanged.connect(self.load_employees)
        self.joining_to.dateChanged.connect(self.load_employees)
        filters.addWidget(self.search, 2)
        filters.addWidget(self.department_filter)
        filters.addWidget(self.designation_filter)
        filters.addWidget(self.type_filter)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.joining_from)
        filters.addWidget(self.joining_to)
        filters.addWidget(self.sort_filter)
        root.addLayout(filters)

        actions = QHBoxLayout()
        self.count_label = QLabel("Showing 0 employees")
        actions.addWidget(self.count_label)
        actions.addStretch()
        self.view_button = QPushButton("View Details")
        self.view_button.clicked.connect(self.view_selected)
        self.edit_button = QPushButton("Edit")
        self.edit_button.clicked.connect(self.edit_selected)
        self.status_button = QPushButton("Deactivate / Reactivate")
        self.status_button.clicked.connect(self.toggle_selected)
        self.separate_button = QPushButton("Mark as Separated")
        self.separate_button.clicked.connect(self.separate_selected)
        for button in (self.view_button, self.edit_button, self.status_button, self.separate_button):
            button.setEnabled(False)
            actions.addWidget(button)
        self.edit_button.setVisible(False)
        self.status_button.setVisible(False)
        self.separate_button.setVisible(False)
        self.view_button.setVisible(False)
        root.addLayout(actions)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Employee ID", "Employee name", "Designation", "Department",
            "Employment type", "Phone", "Date joined", "Status",
        ])
        header = self.table.horizontalHeader()
        for col in (0, 2, 3, 4, 5, 6, 7):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(self.view_selected)
        root.addWidget(self.table)

        report_row = QHBoxLayout()
        self.report_type = QComboBox()
        for key, title in REPORT_TYPES.items():
            self.report_type.addItem(title, key)
        self.report_from = QDateEdit()
        self.report_from.setCalendarPopup(True)
        self.report_from.setDisplayFormat("yyyy-MM-dd")
        self.report_from.setSpecialValueText("Report from")
        self.report_from.setMinimumDate(_NO_DATE)
        self.report_from.setDate(_NO_DATE)
        self.report_to = QDateEdit()
        self.report_to.setCalendarPopup(True)
        self.report_to.setDisplayFormat("yyyy-MM-dd")
        self.report_to.setSpecialValueText("Report to")
        self.report_to.setMinimumDate(_NO_DATE)
        self.report_to.setDate(_NO_DATE)
        self.include_private = QCheckBox(
            "Include private contact/address details (confidential export)"
        )
        pdf = QPushButton("Export PDF")
        pdf.clicked.connect(lambda: self.export_report("pdf"))
        excel = QPushButton("Export Excel")
        excel.clicked.connect(lambda: self.export_report("excel"))
        report_row.addWidget(QLabel("Reports:"))
        report_row.addWidget(self.report_type)
        report_row.addWidget(self.report_from)
        report_row.addWidget(self.report_to)
        report_row.addWidget(self.include_private)
        report_row.addWidget(pdf)
        report_row.addWidget(excel)
        self.private_export_controls = QWidget()
        self.private_export_controls.setLayout(report_row)
        root.addWidget(self.private_export_controls)
        disclaimer = QLabel(
            "Employee records are protected by role-based permissions. Employee selection is not "
            "identity verification or an electronic signature."
        )
        disclaimer.setWordWrap(True)
        disclaimer.setObjectName("muted")
        root.addWidget(disclaimer)
        self.access_message = QLabel("Employee view permission is required.")
        self.access_message.setObjectName("muted")
        self.access_message.setVisible(False)
        root.addWidget(self.access_message)

    def load_data(self):
        if self._loading:
            return
        self._refresh_access()
        if not self.can_view or self.actor_context is None:
            self._clear_data()
            self.access_message.setVisible(True)
            return
        self.access_message.setVisible(False)
        self._loading = True
        loaded = False
        try:
            with get_db() as session:
                if self.can_manage:
                    seed_default_departments(
                        session, actor_context=self.actor_context
                    )
                departments = list_departments(
                    session, actor_context=self.actor_context
                )
                designations = list_designations(
                    session, actor_context=self.actor_context
                )
                summary = get_employee_summary(
                    session, actor_context=self.actor_context
                )
            selected_department = self.department_filter.currentData()
            selected_designation = self.designation_filter.currentData()
            self.department_filter.blockSignals(True)
            self.department_filter.clear()
            self.department_filter.addItem("All departments", None)
            for department in departments:
                self.department_filter.addItem(
                    department.name + ("" if department.is_active else " (inactive)"),
                    department.id,
                )
            _set_combo_data(self.department_filter, selected_department)
            self.department_filter.blockSignals(False)
            self.designation_filter.blockSignals(True)
            self.designation_filter.clear()
            self.designation_filter.addItem("All designations", None)
            for designation in designations:
                self.designation_filter.addItem(
                    designation.name + ("" if designation.is_active else " (inactive)"),
                    designation.id,
                )
            _set_combo_data(self.designation_filter, selected_designation)
            self.designation_filter.blockSignals(False)
            for key, value in summary.items():
                if key in self.metrics:
                    self.metrics[key].setText(str(value))
            distribution = summary["by_department"]
            self.department_distribution.setText(
                "Department distribution: " + (
                    " | ".join(f"{name}: {count}" for name, count in distribution.items())
                    if distribution else "No employee records"
                )
            )
            loaded = True
        except AuthorizationError as exc:
            self._clear_data()
            self._refresh_access()
            self.access_message.setVisible(True)
            QMessageBox.warning(self, "Access denied", str(exc))
        except Exception as exc:
            self._clear_data()
            QMessageBox.critical(self, "Unable to load employees", str(exc))
        finally:
            self._loading = False
        if loaded:
            self.load_employees()

    def load_employees(self, *_args):
        if self._loading or not self.can_view or self.actor_context is None:
            return
        try:
            start = _optional_date(self.joining_from)
            end = _optional_date(self.joining_to)
            with get_db() as session:
                employees = list_employees(
                    session,
                    actor_context=self.actor_context,
                    search=self.search.text(),
                    department_id=self.department_filter.currentData(),
                    designation_id=self.designation_filter.currentData(),
                    employment_type=None if self.type_filter.currentData() == "ALL"
                    else self.type_filter.currentText(),
                    employment_status=self.status_filter.currentData(),
                    joining_start=start,
                    joining_end=end,
                    sort_by=self.sort_filter.currentData(),
                    offset=0,
                    limit=1000,
                    include_private=self.can_view_private,
                )
            self.table.setRowCount(len(employees))
            for row, employee in enumerate(employees):
                values = (
                    employee.employee_code,
                    employee.full_name,
                    employee.designation.name,
                    employee.department.name,
                    employee.employment_type,
                    employee.phone or "—",
                    employee.date_of_joining.isoformat(),
                    employee.employment_status,
                )
                for col, value in enumerate(values):
                    self.table.setItem(row, col, QTableWidgetItem(str(value)))
                self.table.item(row, 0).setData(Qt.UserRole, employee.id)
            self.count_label.setText(f"Showing {len(employees)} employees")
            self._selection_changed()
        except AuthorizationError as exc:
            self._clear_data()
            self._refresh_access()
            self.access_message.setVisible(True)
            QMessageBox.warning(self, "Access denied", str(exc))
        except Exception as exc:
            self._clear_data()
            QMessageBox.warning(self, "Unable to list employees", str(exc))

    def _selected_employee_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 0) is None:
            return None
        return self.table.item(row, 0).data(Qt.UserRole)

    def _selection_changed(self):
        employee_id = self._selected_employee_id()
        enabled = employee_id is not None
        self.view_button.setVisible(self.can_view)
        for button in (self.edit_button, self.status_button, self.separate_button):
            button.setVisible(self.can_manage)
        self.view_button.setEnabled(enabled and self.can_view)
        for button in (self.edit_button, self.status_button, self.separate_button):
            button.setEnabled(enabled and self.can_manage)
        if not enabled:
            return
        status = self.table.item(self.table.currentRow(), 7).text()
        self.status_button.setText("Reactivate" if status == "Inactive" else "Deactivate")
        self.status_button.setEnabled(status in {"Active", "On Leave", "Inactive"})
        self.separate_button.setEnabled(status != "Separated")
        self.edit_button.setEnabled(status != "Separated")

    def add_employee(self):
        if not self.can_manage or self.actor_context is None:
            return
        dialog = EmployeeFormDialog(
            self,
            actor_context=self.actor_context,
            can_view_private=self.can_view_private,
        )
        if dialog.exec() == QDialog.Accepted:
            self.load_data()

    def view_selected(self, *_args):
        employee_id = self._selected_employee_id()
        if employee_id is not None and self.actor_context is not None:
            EmployeeDetailDialog(
                self,
                employee_id=int(employee_id),
                actor_context=self.actor_context,
                can_view_private=self.can_view_private,
                can_manage=self.can_manage,
            ).exec()

    def edit_selected(self):
        employee_id = self._selected_employee_id()
        if employee_id is None or not self.can_manage or self.actor_context is None:
            return
        dialog = EmployeeFormDialog(
            self,
            employee_id=int(employee_id),
            actor_context=self.actor_context,
            can_view_private=self.can_view_private,
        )
        if dialog.exec() == QDialog.Accepted:
            self.load_data()

    def toggle_selected(self):
        employee_id = self._selected_employee_id()
        if employee_id is None or not self.can_manage or self.actor_context is None:
            return
        status = self.table.item(self.table.currentRow(), 7).text()
        if status == "Inactive":
            prompt = "Reactivate this employee?"
        else:
            prompt = "Deactivate this employee? Historical employee and operational records will be retained."
        if QMessageBox.question(
            self, "Confirm employee status", prompt,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as session:
                if status == "Inactive":
                    reactivate_employee(
                        session, int(employee_id), actor_context=self.actor_context
                    )
                else:
                    deactivate_employee(
                        session, int(employee_id), actor_context=self.actor_context
                    )
            self.load_data()
        except AuthorizationError as exc:
            self._clear_data()
            self._refresh_access()
            QMessageBox.warning(self, "Access denied", str(exc))
        except Exception as exc:
            self._clear_data()
            QMessageBox.warning(self, "Could not change employee status", str(exc))

    def separate_selected(self):
        employee_id = self._selected_employee_id()
        if employee_id is None or not self.can_manage or self.actor_context is None:
            return
        day_text, accepted = QInputDialog.getText(
            self, "Mark employee as separated", "Date of leaving (YYYY-MM-DD):",
            text=date.today().isoformat(),
        )
        if not accepted:
            return
        reason, reason_accepted = QInputDialog.getText(
            self, "Separation details", "Reason (optional):"
        )
        if not reason_accepted:
            return
        if QMessageBox.question(
            self,
            "Confirm separation",
            "Mark this employee as separated? This status cannot be reactivated.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with get_db() as session:
                separate_employee(
                    session,
                    int(employee_id),
                    date.fromisoformat(day_text.strip()),
                    actor_context=self.actor_context,
                    reason=reason,
                )
            self.load_data()
        except AuthorizationError as exc:
            self._clear_data()
            self._refresh_access()
            QMessageBox.warning(self, "Access denied", str(exc))
        except Exception as exc:
            self._clear_data()
            QMessageBox.warning(self, "Could not record separation", str(exc))

    def manage_catalogues(self):
        if not self.can_manage or self.actor_context is None:
            return
        dialog = DepartmentDesignationDialog(self, actor_context=self.actor_context)
        dialog.exec()
        self.load_data()

    def export_report(self, format_name: str):
        if not self.can_view or self.actor_context is None:
            return
        extension = "pdf" if format_name == "pdf" else "xlsx"
        file_filter = "PDF files (*.pdf)" if extension == "pdf" else "Excel workbooks (*.xlsx)"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export employee report", f"employee-report.{extension}", file_filter
        )
        if not path:
            return
        if self.include_private.isChecked() and QMessageBox.question(
            self,
            "Confirm confidential export",
            "This export includes private contact, residential and emergency-contact fields. "
            "Continue and store the file securely?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        report_type = self.report_type.currentData()
        start = _optional_date(self.report_from)
        end = _optional_date(self.report_to)
        try:
            with get_db() as session:
                generator = (
                    generate_employee_pdf_report
                    if extension == "pdf" else generate_employee_excel_report
                )
                generator(
                    session,
                    path,
                    report_type,
                    actor_context=self.actor_context,
                    start_date=start,
                    end_date=end,
                    include_private=self.include_private.isChecked(),
                )
            QMessageBox.information(self, "Report exported", f"Report saved to:\n{path}")
        except AuthorizationError as exc:
            self._clear_data()
            self._refresh_access()
            QMessageBox.warning(self, "Access denied", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Could not export employee report", str(exc))
