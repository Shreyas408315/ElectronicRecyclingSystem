from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
from sqlalchemy import select

import database.connection as db_connection
from database.models import Role, User, UserRole
from services.security_service import AuthorizationError, require_permission


class SecurityPage(QWidget):
    """Read-only user/role directory; privileged creation remains service-gated."""
    def __init__(self, actor_context=None):
        super().__init__()
        self.actor_context = actor_context
        layout = QVBoxLayout(self)
        title = QLabel("Security: Users and Roles")
        title.setObjectName("heading")
        layout.addWidget(title)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        controls = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.load_data)
        controls.addWidget(refresh)
        controls.addStretch()
        layout.addLayout(controls)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Username", "Full name", "Status", "Roles"])
        layout.addWidget(self.table)
        self.load_data()

    def load_data(self):
        self.table.setRowCount(0)
        with db_connection.get_db() as session:
            try:
                require_permission(self.actor_context, "users.manage", db_session=session)
            except AuthorizationError:
                self.note.setText("You do not have permission to manage users. Access is denied by the service layer.")
                return
            users = list(session.scalars(select(User).order_by(User.username)))
            self.note.setText("Role assignment and user creation are audited and require the Users: Manage permission.")
            for user in users:
                roles = list(session.scalars(
                    select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id)
                ))
                row = self.table.rowCount()
                self.table.insertRow(row)
                for column, value in enumerate((user.username, user.full_name, "Active" if user.is_active else "Inactive", ", ".join(roles))):
                    self.table.setItem(row, column, QTableWidgetItem(value))
