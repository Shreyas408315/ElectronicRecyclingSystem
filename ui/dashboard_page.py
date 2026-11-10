from datetime import date

from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from database.connection import get_db
from services.expense_service import calculate_dashboard_expense_summary
from services.employee_service import get_employee_summary
from services.security_service import AuthorizationError, current_actor_context
from utils.validators import format_currency


class DashboardPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        header = QGridLayout()
        title = QLabel("Factory Dashboard")
        title.setObjectName("heading")
        subtitle = QLabel(
            "Operating expenses incurred, payments made and remaining expense payables "
            "are shown separately."
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.load_data)
        header.addWidget(title, 0, 0)
        header.addWidget(subtitle, 1, 0)
        header.addWidget(refresh, 0, 1)
        layout.addLayout(header)
        self.values: dict[str, QLabel] = {}
        cards = QGridLayout()
        for index, (key, caption) in enumerate((
            ("total_expenses", "Current-month posted expenses"),
            ("total_payments", "Current-month expense payments"),
            ("outstanding", "Current expense payables"),
            ("active_employees", "Active employees"),
            ("employees_on_leave", "Employees on leave"),
        )):
            card = QWidget()
            box = QVBoxLayout(card)
            label = QLabel(caption)
            label.setObjectName("muted")
            value = QLabel("₹ 0.00" if key in {
                "total_expenses", "total_payments", "outstanding"
            } else "0")
            value.setObjectName("metricBigValue")
            box.addWidget(label)
            box.addWidget(value)
            cards.addWidget(card, 0, index)
            self.values[key] = value
        layout.addLayout(cards)
        self.expense_access_warning = QLabel()
        self.expense_access_warning.setObjectName("muted")
        self.expense_access_warning.setWordWrap(True)
        layout.addWidget(self.expense_access_warning)
        self.employee_access_warning = QLabel()
        self.employee_access_warning.setObjectName("muted")
        self.employee_access_warning.setWordWrap(True)
        layout.addWidget(self.employee_access_warning)
        layout.addStretch()
        self.load_data()

    def load_data(self):
        today = date.today()
        month_start = today.replace(day=1)
        summary = {}
        employee_summary = {}
        actor_context = current_actor_context()
        try:
            with get_db() as db:
                summary = calculate_dashboard_expense_summary(
                    db, actor_context=actor_context,
                    start_date=month_start, end_date=today,
                )
            self.expense_access_warning.clear()
        except AuthorizationError as exc:
            self.expense_access_warning.setText(
                f"Expense payment metrics unavailable: {exc}"
            )
        try:
            with get_db() as db:
                employee_summary = get_employee_summary(
                    db, actor_context=actor_context
                )
            self.employee_access_warning.clear()
        except AuthorizationError as exc:
            self.employee_access_warning.setText(
                f"Employee metrics unavailable: {exc}"
            )
        for key in ("total_expenses", "total_payments", "outstanding"):
            if key in summary:
                self.values[key].setText(format_currency(summary[key]))
            else:
                self.values[key].clear()
        for key in ("active_employees", "employees_on_leave"):
            if employee_summary:
                employee_key = "active" if key == "active_employees" else "on_leave"
                self.values[key].setText(str(employee_summary[employee_key]))
            else:
                self.values[key].clear()
