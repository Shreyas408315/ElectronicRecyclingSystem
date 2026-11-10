from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout

import database.connection as db_connection
from services.security_service import AuthenticationError, authenticate, create_first_administrator


class FirstRunAdministratorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create first administrator")
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Create the first administrator. No default password is provided."))
        form = QFormLayout()
        self.full_name = QLineEdit()
        self.username = QLineEdit()
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm = QLineEdit(); self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Full name", self.full_name)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        form.addRow("Confirm password", self.confirm)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def create(self):
        if self.password.text() != self.confirm.text():
            QMessageBox.warning(self, "Password", "The passwords do not match.")
            return
        try:
            with db_connection.get_db() as session:
                create_first_administrator(session, username=self.username.text(), password=self.password.text(), full_name=self.full_name.text())
        except AuthenticationError as exc:
            QMessageBox.warning(self, "Administrator setup", str(exc))
            return
        self.accept()


class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sign in")
        self.setModal(True)
        self.authenticated_session = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.username = QLineEdit()
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.login)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def login(self):
        try:
            with db_connection.get_db() as session:
                self.authenticated_session = authenticate(session, self.username.text(), self.password.text())
        except AuthenticationError as exc:
            QMessageBox.warning(self, "Sign in", str(exc))
            return
        self.accept()
