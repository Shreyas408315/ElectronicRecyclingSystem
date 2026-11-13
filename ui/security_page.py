from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import database.connection as db_connection
from database.models import Role, User, UserRole
from services.security_service import (
    AuthenticationError,
    AuthorizationError,
    ActorContext,
    create_user,
    current_actor_context,
    require_permission,
)


class CreateUserDialog(QDialog):
    def __init__(self, actor_context: ActorContext, parent=None):
        super().__init__(parent)
        self.actor_context = actor_context
        self.setWindowTitle("Create User")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.username = QLineEdit()
        self.username.setMaxLength(80)
        self.username.setPlaceholderText("3-80 letters, digits, dots, underscores, or hyphens")
        self.full_name = QLineEdit()
        self.full_name.setMaxLength(200)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_password = QLineEdit()
        self.confirm_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.role = QComboBox()

        with db_connection.get_db() as session:
            require_permission(
                self.actor_context, "users.manage", db_session=session
            )
            roles = list(
                session.scalars(
                    select(Role)
                    .where(Role.is_system.is_(True))
                    .order_by(Role.name)
                )
            )
        for role in roles:
            self.role.addItem(role.name, role.name)

        self.show_passwords = QCheckBox("Show passwords")
        self.show_passwords.toggled.connect(self._set_password_visibility)
        form.addRow("Username", self.username)
        form.addRow("Full name", self.full_name)
        form.addRow("Password", self.password)
        form.addRow("Confirm password", self.confirm_password)
        form.addRow("Role", self.role)
        layout.addLayout(form)
        layout.addWidget(self.show_passwords)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("errorBanner")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setObjectName("primaryButton")
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setObjectName("secondaryButton")
        buttons.accepted.connect(self.create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setTabOrder(self.username, self.full_name)
        self.setTabOrder(self.full_name, self.password)
        self.setTabOrder(self.password, self.confirm_password)
        self.setTabOrder(self.confirm_password, self.role)
        self.setTabOrder(self.role, self.show_passwords)
        self.setTabOrder(self.show_passwords, self.save_button)

    def _set_password_visibility(self, visible: bool):
        mode = (
            QLineEdit.EchoMode.Normal
            if visible
            else QLineEdit.EchoMode.Password
        )
        self.password.setEchoMode(mode)
        self.confirm_password.setEchoMode(mode)

    def create(self):
        password = self.password.text()
        confirmation = self.confirm_password.text()
        self.password.clear()
        self.confirm_password.clear()
        self.show_passwords.setChecked(False)
        self.error_label.hide()
        try:
            if password != confirmation:
                raise AuthenticationError("The passwords do not match.")
            if not self.role.currentData():
                raise AuthenticationError("Select an existing built-in role.")
            current_actor = current_actor_context()
            if current_actor is None or current_actor != self.actor_context:
                raise AuthorizationError(
                    "The authenticated session changed or expired. Reopen this dialog."
                )
            with db_connection.get_db() as session:
                create_user(
                    session,
                    actor_context=current_actor,
                    username=self.username.text(),
                    password=password,
                    full_name=self.full_name.text(),
                    role_names=[self.role.currentData()],
                )
        except (AuthenticationError, AuthorizationError, ValueError) as error:
            self.error_label.setText(str(error))
            self.error_label.show()
            return
        except SQLAlchemyError:
            self.error_label.setText(
                "The user could not be created. No changes were saved."
            )
            self.error_label.show()
            return
        finally:
            password = ""
            confirmation = ""

        self.accept()

    def done(self, result: int):
        self.password.clear()
        self.confirm_password.clear()
        super().done(result)


class SecurityPage(QWidget):
    """Authorized user directory and audited user-creation workflow."""

    def __init__(self, actor_context: ActorContext | None = None):
        super().__init__()
        self.actor_context = actor_context or current_actor_context()
        layout = QVBoxLayout(self)
        title = QLabel("Security: Users and Roles")
        title.setObjectName("heading")
        layout.addWidget(title)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        controls = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh Users")
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.clicked.connect(self.load_data)
        controls.addWidget(self.refresh_button)
        controls.addStretch()
        self.create_button = QPushButton("Create User")
        self.create_button.setObjectName("primaryButton")
        self.create_button.clicked.connect(self.open_create_user)
        controls.addWidget(self.create_button)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Username", "Full name", "Status", "Roles"]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setMinimumSectionSize(90)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 240)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 300)
        layout.addWidget(self.table, 1)
        self.load_data()

    def _authorized_actor(self, session) -> ActorContext:
        current_actor = current_actor_context()
        if current_actor is None or current_actor != self.actor_context:
            raise AuthorizationError(
                "The authenticated session changed or expired. Reopen Security."
            )
        require_permission(
            current_actor, "users.manage", db_session=session
        )
        return current_actor

    def load_data(self):
        self.table.setRowCount(0)
        self.create_button.setVisible(False)
        try:
            with db_connection.get_db() as session:
                self._authorized_actor(session)
                users = list(
                    session.scalars(select(User).order_by(User.username))
                )
                user_rows = []
                for user in users:
                    roles = list(
                        session.scalars(
                            select(Role.name)
                            .join(UserRole, UserRole.role_id == Role.id)
                            .where(UserRole.user_id == user.id)
                            .order_by(Role.name)
                        )
                    )
                    user_rows.append(
                        (
                            user.username,
                            user.full_name,
                            "Active" if user.is_active else "Inactive",
                            ", ".join(roles),
                        )
                    )
        except AuthorizationError as error:
            self.note.setText(
                f"User management is unavailable: {error}"
            )
            return
        except SQLAlchemyError:
            self.note.setText("Could not load the user directory.")
            return

        self.create_button.setVisible(True)
        self.note.setText(
            "User creation is audited and requires the Users: Manage permission."
        )
        for values in user_rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))

    def open_create_user(self):
        try:
            with db_connection.get_db() as session:
                actor = self._authorized_actor(session)
            dialog = CreateUserDialog(actor, self)
        except AuthorizationError as error:
            self.note.setText(f"User creation is unavailable: {error}")
            self.create_button.setVisible(False)
            return
        except SQLAlchemyError:
            self.note.setText("Could not load the user creation form.")
            return

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
