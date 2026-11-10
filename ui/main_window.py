import logging

from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget
)
from ui.supplier_page import SupplierPage
from ui.purchase_page import PurchasePage
from ui.dismantling_page import DismantlingPage
from ui.inventory_page import InventoryPage
from ui.customer_page import CustomerPage
from ui.sales_order_page import SalesOrderPage
from ui.sales_invoice_page import SalesInvoicePage
from ui.payments_page import PaymentsReceiptPage
from ui.expense_page import ExpensePage
from ui.accounting_page import AccountingPage
from ui.dashboard_page import DashboardPage
from ui.settings_page import SettingsPage
from ui.employee_page import EmployeePage
from ui.security_page import SecurityPage
from database.connection import get_db
from services.security_service import actor_has_permission, current_actor_context


logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, current_user=None):
        super().__init__()
        self.current_user = current_user or current_actor_context()
        self.setWindowTitle("Electronic Recycling Factory Management System")
        self.resize(1340, 850)
        self.setMinimumSize(1020, 680)

        central = QWidget()
        central.setObjectName("centralWidget")
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Sidebar navigation
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(240)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 16)
        side.setSpacing(0)

        brand_container = QWidget()
        brand_container.setObjectName("brandContainer")
        brand_layout = QVBoxLayout(brand_container)
        brand_layout.setContentsMargins(20, 24, 20, 20)
        
        brand = QLabel("RECYCLING\nMANAGEMENT")
        brand.setObjectName("brand")
        brand_sub = QLabel("Factory ERP v1.0")
        brand_sub.setObjectName("brandSub")
        brand_layout.addWidget(brand)
        brand_layout.addWidget(brand_sub)
        if current_user is not None:
            user_label = QLabel(f"Signed in: {current_user.username}")
            user_label.setObjectName("brandSub")
            brand_layout.addWidget(user_label)
            logout_button = QPushButton("Log out")
            logout_button.setObjectName("secondaryButton")
            logout_button.clicked.connect(self.logout)
            brand_layout.addWidget(logout_button)
        side.addWidget(brand_container)

        pages = [
            "Dashboard", "Purchases", "Suppliers", "Dismantling & Recovery",
            "Inventory", "Customers", "Sales Orders", "Sales & Invoices",
            "Payments & Receipts", "Expenses", "Accounting", "Employees",
            "Reports", "Settings", "Security"
        ]
        self.nav = QListWidget()
        self.nav.setObjectName("navigation")
        for name in pages:
            item = QListWidgetItem(f"  {name}")
            self.nav.addItem(item)
        side.addWidget(self.nav)

        content = QWidget()
        content.setObjectName("contentContainer")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.purchase_page = None
        self.supplier_page = None
        self.dismantling_page = None
        self.inventory_page = None
        self.customer_page = None
        self.sales_order_page = None
        self.sales_invoice_page = None
        self.payments_receipts_page = None
        self.expense_page = None
        self.accounting_page = None
        self.dashboard_page = None
        self.settings_page = None
        self.employee_page = None
        self.security_page = None

        for name in pages:
            if name == "Dashboard":
                self.dashboard_page = DashboardPage()
                self.stack.addWidget(self.dashboard_page)
            elif name == "Purchases":
                self.purchase_page = PurchasePage()
                self.stack.addWidget(self.purchase_page)
            elif name == "Suppliers":
                self.supplier_page = SupplierPage()
                self.stack.addWidget(self.supplier_page)
            elif name == "Dismantling & Recovery":
                self.dismantling_page = DismantlingPage()
                self.stack.addWidget(self.dismantling_page)
            elif name == "Inventory":
                self.inventory_page = InventoryPage()
                self.stack.addWidget(self.inventory_page)
            elif name == "Customers":
                self.customer_page = CustomerPage()
                self.stack.addWidget(self.customer_page)
            elif name == "Sales Orders":
                self.sales_order_page = SalesOrderPage()
                self.stack.addWidget(self.sales_order_page)
            elif name == "Sales & Invoices":
                self.sales_invoice_page = SalesInvoicePage()
                self.stack.addWidget(self.sales_invoice_page)
            elif name == "Payments & Receipts":
                self.payments_receipts_page = PaymentsReceiptPage()
                self.stack.addWidget(self.payments_receipts_page)
            elif name == "Expenses":
                self.expense_page = ExpensePage()
                self.stack.addWidget(self.expense_page)
            elif name == "Accounting":
                self.accounting_page = AccountingPage()
                self.stack.addWidget(self.accounting_page)
            elif name == "Employees":
                self.employee_page = EmployeePage()
                self.stack.addWidget(self.employee_page)
            elif name == "Reports":
                # Reports are generated in their respective operational modules.
                # Keep this navigation entry useful without duplicating report logic.
                page = QWidget()
                page.setObjectName("reportsDirectoryPage")
                page_layout = QVBoxLayout(page)
                page_layout.setContentsMargins(32, 28, 32, 28)

                heading = QLabel("Reporting Centre")
                heading.setObjectName("heading")
                note = QLabel(
                    "Use each module's PDF / Excel Reports action to export its operational "
                    "reports. Accounting contains financial statements and reconciliations; "
                    "Dismantling, Inventory, Sales, Payments, Expenses, and Employees each "
                    "provide their own source-specific reports."
                )
                note.setObjectName("muted")
                note.setWordWrap(True)
                page_layout.addWidget(heading)
                page_layout.addWidget(note)
                page_layout.addStretch()
                self.stack.addWidget(page)
            elif name == "Settings":
                self.settings_page = SettingsPage()
                self.stack.addWidget(self.settings_page)
            elif name == "Security":
                self.security_page = SecurityPage(self.current_user)
                self.stack.addWidget(self.security_page)
            else:
                page = QWidget()
                page.setObjectName("placeholderPage")
                page_layout = QVBoxLayout(page)
                page_layout.setContentsMargins(32, 28, 32, 28)
                
                heading = QLabel(name)
                heading.setObjectName("heading")
                note = QLabel("This module will be implemented in upcoming development milestones.")
                note.setObjectName("muted")
                note.setWordWrap(True)
                
                page_layout.addWidget(heading)
                page_layout.addWidget(note)
                page_layout.addStretch()
                self.stack.addWidget(page)

        content_layout.addWidget(self.stack)

        layout.addWidget(sidebar)
        layout.addWidget(content)
        self.setCentralWidget(central)
        self.nav.currentRowChanged.connect(self.change_page)
        self._apply_navigation_permissions(pages)
        first_visible = next(
            (row for row in range(self.nav.count()) if not self.nav.isRowHidden(row)),
            -1,
        )
        self.nav.setCurrentRow(first_visible)
        self.setStyleSheet(self.styles())

    def _apply_navigation_permissions(self, pages):
        permissions = {
            "Dashboard": (
                "expenses.view", "employees.view", "accounting.view",
                "inventory.view", "sales.view",
            ),
            "Purchases": ("purchases.view", "purchases.post"),
            "Suppliers": ("suppliers.view",),
            "Dismantling & Recovery": ("dismantling.view", "dismantling.manage"),
            "Inventory": ("inventory.view",),
            "Customers": ("customers.view",),
            "Sales Orders": ("sales.view", "sales.manage"),
            "Sales & Invoices": ("invoices.view",),
            "Payments & Receipts": (
                "receipts.view", "supplier_payments.view",
            ),
            "Expenses": ("expenses.view", "expenses.manage", "expenses.post"),
            "Accounting": ("accounting.view",),
            "Employees": ("employees.view", "employees.manage"),
            "Reports": (
                "accounting.view", "employees.view", "inventory.view",
                "dismantling.view", "sales.view", "receipts.view",
                "supplier_payments.view", "expenses.view",
            ),
            "Settings": (
                "settings.view", "settings.manage", "documents.view",
                "backups.view", "backups.create", "backups.restore",
            ),
            "Security": ("users.manage",),
        }
        try:
            with get_db() as session:
                for index, name in enumerate(pages):
                    allowed = any(
                        actor_has_permission(
                            self.current_user, permission, db_session=session
                        )
                        for permission in permissions.get(name, ())
                    )
                    self.nav.setRowHidden(index, not allowed)
        except Exception as exc:
            for index in range(self.nav.count()):
                self.nav.setRowHidden(index, True)
            logger.exception("Could not determine navigation permissions")
            QMessageBox.warning(
                self,
                "Navigation unavailable",
                f"Permissions could not be verified. Navigation is hidden until access can be checked.\n\n{exc}",
            )

    def change_page(self, index):
        if index < 0:
            return
        self.stack.setCurrentIndex(index)
        if index == 0 and self.dashboard_page:
            self.dashboard_page.load_data()
        # If navigating to Purchases, refresh purchase listing
        if index == 1 and self.purchase_page:
            self.purchase_page.load_purchases()
        # If navigating to Suppliers, refresh supplier listing
        elif index == 2 and self.supplier_page:
            self.supplier_page.load_suppliers()
        # If navigating to Dismantling & Recovery, refresh dismantling listing
        elif index == 3 and self.dismantling_page:
            self.dismantling_page.load_dismantling_entries()
        # If navigating to Inventory, refresh stock and movement summaries
        elif index == 4 and self.inventory_page:
            self.inventory_page.load_inventory()
        # If navigating to Customers, refresh the customer catalogue
        elif index == 5 and self.customer_page:
            self.customer_page.load_customers()
        elif index == 6 and self.sales_order_page:
            self.sales_order_page.load_orders()
        elif index == 7 and self.sales_invoice_page:
            self.sales_invoice_page.load_invoices()
        elif index == 8 and self.payments_receipts_page:
            self.payments_receipts_page.load_data()
        elif index == 9 and self.expense_page:
            self.expense_page.load_expenses()
        elif index == 10 and self.accounting_page:
            self.accounting_page.load_data()
        elif index == 11 and self.employee_page:
            self.employee_page.load_data()
        elif index == 13 and self.settings_page:
            self.settings_page.load_data()
        elif index == 14 and self.security_page:
            self.security_page.load_data()

    def logout(self):
        """End the in-memory session; app.py presents the lock/login dialog."""
        from services.security_service import logout

        with get_db() as session:
            logout(session, actor_context=self.current_user)
        self.current_user = None
        self.clear_protected_page_state()
        self.centralWidget().hide()

    def clear_protected_page_state(self):
        self.current_user = None
        while self.stack.count():
            page = self.stack.widget(0)
            self.stack.removeWidget(page)
            page.deleteLater()
        for attribute in (
            "purchase_page", "supplier_page", "dismantling_page",
            "inventory_page", "customer_page", "sales_order_page",
            "sales_invoice_page", "payments_receipts_page", "expense_page",
            "accounting_page", "dashboard_page", "settings_page",
            "employee_page", "security_page",
        ):
            setattr(self, attribute, None)

    @staticmethod
    def styles():
        return '''
        /* Global typography and background */
        QMainWindow, #centralWidget {
            background-color: #f8fafc;
            color: #0f172a;
            font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
            font-size: 13px;
        }

        /* Sidebar container */
        #sidebar {
            background-color: #0f172a;
            border-right: 1px solid #1e293b;
        }
        #sidebar QLabel, #sidebar QWidget {
            background-color: transparent;
        }

        /* Brand section: explicit background transparent and crisp white text */
        #brandContainer {
            background-color: #0b1120;
            border-bottom: 1px solid #1e293b;
        }
        #brand {
            background-color: transparent;
            color: #ffffff;
            font-size: 16px;
            font-weight: 800;
            letter-spacing: 1px;
            line-height: 1.3;
        }
        #brandSub {
            background-color: transparent;
            color: #94a3b8;
            font-size: 11px;
            font-weight: 500;
            margin-top: 4px;
        }

        /* Navigation List */
        #navigation {
            background-color: transparent;
            border: none;
            color: #94a3b8;
            font-size: 13px;
            font-weight: 500;
            padding: 10px 8px;
            outline: none;
        }
        #navigation::item {
            height: 38px;
            border-radius: 6px;
            margin-bottom: 2px;
            padding-left: 6px;
        }
        #navigation::item:hover {
            background-color: #1e293b;
            color: #f1f5f9;
        }
        #navigation::item:selected {
            background-color: #2563eb;
            color: #ffffff;
            font-weight: 600;
        }

        /* Content Area */
        #contentContainer {
            background-color: #f8fafc;
        }
        #placeholderPage {
            background-color: #f8fafc;
        }
        #heading {
            font-size: 24px;
            font-weight: 700;
            color: #0f172a;
            margin-bottom: 8px;
        }
        #muted {
            color: #64748b;
            font-size: 14px;
        }

        /* Supplier Page Specific Headers */
        #supplierPageTitle {
            font-size: 22px;
            font-weight: 700;
            color: #0f172a;
        }
        #supplierPageSubtitle {
            font-size: 13px;
            color: #64748b;
            margin-top: 2px;
        }

        /* Metric Cards */
        #metricsContainer {
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
        }
        #metricCard {
            background-color: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
        }
        #metricTitle {
            font-size: 12px;
            font-weight: 600;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        #tableCountLabel {
            font-size: 13px;
            font-weight: 600;
            color: #475569;
        }

        /* Filter bar */
        #filterBar {
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
        }
        #filterLabel {
            font-size: 12px;
            font-weight: 600;
            color: #475569;
        }

        /* Buttons */
        QPushButton {
            border-radius: 6px;
            font-size: 13px;
            font-weight: 500;
            padding: 6px 14px;
            outline: none;
        }
        #primaryButton {
            background-color: #2563eb;
            color: #ffffff;
            border: 1px solid #1d4ed8;
            font-weight: 600;
            padding: 7px 18px;
        }
        #primaryButton:hover {
            background-color: #1d4ed8;
        }
        #primaryButton:pressed {
            background-color: #1e40af;
        }
        #secondaryButton {
            background-color: #ffffff;
            color: #334155;
            border: 1px solid #cbd5e1;
        }
        #secondaryButton:hover {
            background-color: #f1f5f9;
            color: #0f172a;
            border-color: #94a3b8;
        }
        #secondaryButton:pressed {
            background-color: #e2e8f0;
        }
        #secondaryButton:disabled {
            background-color: #f8fafc;
            color: #94a3b8;
            border-color: #e2e8f0;
        }
        #dangerButton {
            background-color: #dc2626;
            color: #ffffff;
            border: 1px solid #b91c1c;
            font-weight: 600;
        }
        #dangerButton:hover {
            background-color: #b91c1c;
        }
        #successButton {
            background-color: #16a34a;
            color: #ffffff;
            border: 1px solid #15803d;
            font-weight: 600;
        }
        #successButton:hover {
            background-color: #15803d;
        }

        /* Form Inputs & Controls */
        QLineEdit, QComboBox, QDoubleSpinBox, QTextEdit {
            background-color: #ffffff;
            color: #0f172a;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            padding: 6px 10px;
            selection-background-color: #bfdbfe;
            selection-color: #1e3a8a;
        }
        QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {
            border: 1.5px solid #2563eb;
            background-color: #ffffff;
        }
        #readOnlyField {
            background-color: #f1f5f9;
            color: #475569;
            border: 1px dashed #cbd5e1;
            font-weight: 600;
        }
        QComboBox::drop-down {
            border: none;
            width: 24px;
        }
        QComboBox QAbstractItemView {
            background-color: #ffffff;
            border: 1px solid #cbd5e1;
            selection-background-color: #eff6ff;
            selection-color: #1d4ed8;
            padding: 4px;
        }

        /* Data Tables */
        QTableWidget {
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            gridline-color: #f1f5f9;
            outline: none;
        }
        QTableWidget::item {
            padding: 8px 10px;
            border-bottom: 1px solid #f1f5f9;
            color: #1e293b;
        }
        QTableWidget::item:alternate {
            background-color: #f8fafc;
        }
        QTableWidget::item:selected {
            background-color: #eff6ff;
            color: #1e40af;
            font-weight: 600;
        }
        QHeaderView::section {
            background-color: #f1f5f9;
            color: #475569;
            font-size: 12px;
            font-weight: 700;
            padding: 9px 10px;
            border: none;
            border-bottom: 2px solid #cbd5e1;
            border-right: 1px solid #e2e8f0;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        /* Dialogs and Details */
        QDialog {
            background-color: #f8fafc;
        }
        #dialogHeader {
            background-color: #0f172a;
            border-bottom: 1px solid #1e293b;
        }
        #dialogTitle {
            color: #ffffff;
            font-size: 18px;
            font-weight: 700;
        }
        #dialogDesc {
            color: #94a3b8;
            font-size: 12px;
            margin-top: 4px;
        }
        #dialogFooter {
            background-color: #ffffff;
            border-top: 1px solid #e2e8f0;
        }
        #formScrollArea, #formContainer {
            background-color: #f8fafc;
        }
        QGroupBox {
            font-weight: 700;
            font-size: 13px;
            color: #1e293b;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            margin-top: 12px;
            padding-top: 18px;
            background-color: #ffffff;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 14px;
            padding: 0 6px;
            background-color: #ffffff;
            color: #2563eb;
        }
        #errorBanner {
            background-color: #fef2f2;
            color: #991b1b;
            border: 1px solid #fecaca;
            border-radius: 6px;
            padding: 10px 14px;
            margin: 6px 24px;
            font-weight: 600;
        }

        /* Detail Dialog Elements */
        #detailHeaderCard {
            background-color: #0f172a;
            border-bottom: 1px solid #1e293b;
        }
        #detailSupplierName {
            color: #ffffff;
            font-size: 20px;
            font-weight: 700;
        }
        #codeBadge {
            background-color: #1e293b;
            color: #38bdf8;
            border: 1px solid #334155;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 700;
            padding: 3px 8px;
            margin-left: 10px;
        }
        #detailRegDate {
            color: #94a3b8;
            font-size: 12px;
            margin-top: 6px;
        }
        #detailMetricBar {
            background-color: #f8fafc;
        }
        #balanceCard {
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
        }
        #metricValue {
            font-size: 20px;
            font-weight: 700;
            color: #0f172a;
        }
        #metricValuePrimary {
            font-size: 20px;
            font-weight: 700;
            color: #2563eb;
        }
        #metricSubtext {
            font-size: 11px;
            color: #64748b;
            margin-top: 2px;
        }
        #fieldVal {
            font-weight: 600;
            color: #0f172a;
            font-size: 13px;
        }
        #noticeBox {
            background-color: #eff6ff;
            border: 1px solid #bfdbfe;
            border-radius: 8px;
        }
        #noticeText {
            color: #1e40af;
            font-size: 12px;
            line-height: 1.4;
        }
        QTabWidget::pane {
            border: 1px solid #e2e8f0;
            background-color: #ffffff;
            border-radius: 0 0 8px 8px;
        }
        QTabBar::tab {
            background-color: #f1f5f9;
            color: #475569;
            font-weight: 600;
            padding: 8px 18px;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            margin-right: 4px;
        }
        QTabBar::tab:selected {
            background-color: #ffffff;
            color: #2563eb;
            border: 1px solid #e2e8f0;
            border-bottom: 1px solid #ffffff;
        }
        '''
