from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QDialog,
)

from database.connection import get_db
from database.models import Customer
from services.security_service import AuthorizationError, current_actor_context
from services.customer_service import (
    CUSTOMER_TYPES,
    PAYMENT_METHODS,
    PAYMENT_TERMS,
    CustomerDuplicateWarning,
    CustomerValidationError,
    create_customer,
    generate_next_customer_code,
    update_customer,
)


class CustomerFormDialog(QDialog):
    customer_saved = Signal(int)

    def __init__(self, parent=None, customer: Customer | None = None):
        super().__init__(parent)
        self.customer = customer
        self.setWindowTitle("Edit Customer" if customer else "Add Customer")
        self.setModal(True)
        self.resize(760, 760)
        self.setMinimumSize(640, 620)
        self._init_ui()
        if customer:
            self._load_customer()
        else:
            actor_context = current_actor_context()
            if actor_context is None:
                self.lbl_error.setText("Customer management unavailable: Sign in is required.")
                self.lbl_error.setVisible(True)
                self.btn_save.setEnabled(False)
            else:
                try:
                    with get_db() as db:
                        self.txt_code.setText(generate_next_customer_code(
                            db, actor_context=actor_context
                        ))
                except AuthorizationError as error:
                    self.lbl_error.setText(f"Customer management unavailable: {error}")
                    self.lbl_error.setVisible(True)
                    self.btn_save.setEnabled(False)

    def _init_ui(self):
        root = QVBoxLayout(self)
        header = QFrame()
        header.setObjectName("dialogHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(22, 16, 22, 16)
        title = QLabel("Customer & Distributor Details")
        title.setObjectName("dialogTitle")
        description = QLabel("Maintain customer information and opening balance; sales receivables are not managed here.")
        description.setObjectName("dialogDesc")
        description.setWordWrap(True)
        header_layout.addWidget(title)
        header_layout.addWidget(description)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(12)

        basic = QGroupBox("Basic Information")
        grid = QGridLayout(basic)
        self.txt_code = QLineEdit()
        self.txt_code.setReadOnly(True)
        self.txt_code.setObjectName("readOnlyField")
        grid.addWidget(QLabel("Customer ID:"), 0, 0)
        grid.addWidget(self.txt_code, 0, 1)
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("Customer or business name")
        grid.addWidget(QLabel("Customer name *:"), 1, 0)
        grid.addWidget(self.txt_name, 1, 1)
        self.combo_type = QComboBox()
        self.combo_type.addItems(CUSTOMER_TYPES)
        grid.addWidget(QLabel("Customer type:"), 2, 0)
        grid.addWidget(self.combo_type, 2, 1)
        self.txt_contact = QLineEdit()
        grid.addWidget(QLabel("Contact person:"), 3, 0)
        grid.addWidget(self.txt_contact, 3, 1)
        layout.addWidget(basic)

        contact = QGroupBox("Contact Information")
        contact_form = QFormLayout(contact)
        self.txt_phone = QLineEdit()
        self.txt_phone.setPlaceholderText("e.g. +91 9876543210")
        contact_form.addRow("Primary phone:", self.txt_phone)
        self.txt_alt_phone = QLineEdit()
        contact_form.addRow("Alternate phone:", self.txt_alt_phone)
        self.txt_email = QLineEdit()
        self.txt_email.setPlaceholderText("name@example.com")
        contact_form.addRow("Email:", self.txt_email)
        self.txt_gstin = QLineEdit()
        self.txt_gstin.setMaxLength(15)
        self.txt_gstin.textChanged.connect(
            lambda text: self.txt_gstin.setText(text.upper()) if text != text.upper() else None
        )
        contact_form.addRow("GSTIN:", self.txt_gstin)
        self.txt_pan = QLineEdit()
        self.txt_pan.setMaxLength(10)
        self.txt_pan.textChanged.connect(
            lambda text: self.txt_pan.setText(text.upper()) if text != text.upper() else None
        )
        contact_form.addRow("PAN:", self.txt_pan)
        layout.addWidget(contact)

        address = QGroupBox("Address")
        address_form = QFormLayout(address)
        self.txt_billing = QTextEdit()
        self.txt_billing.setMaximumHeight(70)
        self.txt_billing.textChanged.connect(self._sync_shipping_address)
        address_form.addRow("Billing address:", self.txt_billing)
        self.chk_copy_address = QCheckBox("Use billing address as shipping address")
        self.chk_copy_address.toggled.connect(self._copy_billing_to_shipping)
        address_form.addRow("", self.chk_copy_address)
        self.txt_shipping = QTextEdit()
        self.txt_shipping.setMaximumHeight(70)
        address_form.addRow("Shipping address:", self.txt_shipping)
        self.txt_city = QLineEdit()
        self.txt_state = QLineEdit()
        self.txt_pin = QLineEdit()
        self.txt_pin.setMaxLength(10)
        self.txt_country = QLineEdit("India")
        address_form.addRow("City:", self.txt_city)
        address_form.addRow("State:", self.txt_state)
        address_form.addRow("PIN code:", self.txt_pin)
        address_form.addRow("Country:", self.txt_country)
        layout.addWidget(address)

        financial = QGroupBox("Financial Information")
        financial_form = QFormLayout(financial)
        self.spin_opening = self._money_box()
        financial_form.addRow("Opening balance (₹):", self.spin_opening)
        self.chk_credit_limit = QCheckBox("Set a credit limit")
        self.spin_credit = self._money_box()
        self.spin_credit.setEnabled(False)
        self.chk_credit_limit.toggled.connect(self.spin_credit.setEnabled)
        financial_form.addRow("Credit limit:", self.chk_credit_limit)
        financial_form.addRow("Credit limit amount (₹):", self.spin_credit)
        self.combo_terms = QComboBox()
        self.combo_terms.addItems(PAYMENT_TERMS)
        self.combo_terms.currentTextChanged.connect(self._toggle_custom_days)
        financial_form.addRow("Payment terms:", self.combo_terms)
        self.spin_days = QSpinBox()
        self.spin_days.setRange(0, 3650)
        self.spin_days.setEnabled(False)
        financial_form.addRow("Custom payment days:", self.spin_days)
        self.combo_payment = QComboBox()
        self.combo_payment.addItems(PAYMENT_METHODS)
        self.combo_payment.setEditable(True)
        financial_form.addRow("Preferred payment method:", self.combo_payment)
        self.chk_active = QCheckBox("Customer is active")
        self.chk_active.setChecked(True)
        financial_form.addRow("Status:", self.chk_active)
        self.txt_notes = QTextEdit()
        self.txt_notes.setMaximumHeight(75)
        financial_form.addRow("Notes:", self.txt_notes)
        layout.addWidget(financial)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self.lbl_error = QLabel()
        self.lbl_error.setObjectName("errorBanner")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setVisible(False)
        root.addWidget(self.lbl_error)
        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setObjectName("secondaryButton")
        cancel.clicked.connect(self.reject)
        self.btn_save = QPushButton("Update Customer" if self.customer else "Save Customer")
        self.btn_save.setObjectName("primaryButton")
        self.btn_save.clicked.connect(self._save)
        footer.addWidget(cancel)
        footer.addWidget(self.btn_save)
        root.addLayout(footer)

    @staticmethod
    def _money_box() -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0, 999999999999.99)
        box.setDecimals(2)
        box.setPrefix("₹ ")
        return box

    def _toggle_custom_days(self, term: str):
        self.spin_days.setEnabled(term == "Custom")

    def _copy_billing_to_shipping(self, checked: bool):
        self.txt_shipping.setReadOnly(checked)
        if checked:
            self.txt_shipping.setPlainText(self.txt_billing.toPlainText())

    def _sync_shipping_address(self):
        if self.chk_copy_address.isChecked():
            self.txt_shipping.setPlainText(self.txt_billing.toPlainText())

    def _load_customer(self):
        customer = self.customer
        self.txt_code.setText(customer.customer_code)
        self.txt_name.setText(customer.customer_name or "")
        self.combo_type.setCurrentText(customer.customer_type or "Other")
        self.txt_contact.setText(customer.contact_person or "")
        self.txt_phone.setText(customer.phone or "")
        self.txt_alt_phone.setText(customer.alternate_phone or "")
        self.txt_email.setText(customer.email or "")
        self.txt_gstin.setText(customer.gstin or "")
        self.txt_pan.setText(customer.pan or "")
        self.txt_billing.setPlainText(customer.billing_address or "")
        self.txt_shipping.setPlainText(customer.shipping_address or "")
        self.chk_copy_address.setChecked(
            bool(customer.billing_address and customer.billing_address == customer.shipping_address)
        )
        for field in ("city", "state", "pin_code", "country"):
            getattr(self, f"txt_{'pin' if field == 'pin_code' else field}").setText(
                getattr(customer, field) or ""
            )
        self.spin_opening.setValue(float(customer.opening_balance or 0))
        if customer.credit_limit is not None:
            self.chk_credit_limit.setChecked(True)
            self.spin_credit.setValue(float(customer.credit_limit))
        self.combo_terms.setCurrentText(customer.payment_terms or "Immediate")
        self.spin_days.setValue(customer.custom_payment_days or 0)
        self.combo_payment.setCurrentText(customer.preferred_payment_method or "Cash")
        self.chk_active.setChecked(customer.is_active)
        self.txt_notes.setPlainText(customer.notes or "")

    def _payload(self) -> dict:
        return {
            "customer_name": self.txt_name.text(),
            "customer_type": self.combo_type.currentText(),
            "contact_person": self.txt_contact.text(),
            "phone": self.txt_phone.text(),
            "alternate_phone": self.txt_alt_phone.text(),
            "email": self.txt_email.text(),
            "gstin": self.txt_gstin.text(),
            "pan": self.txt_pan.text(),
            "billing_address": self.txt_billing.toPlainText(),
            "shipping_address": self.txt_shipping.toPlainText(),
            "city": self.txt_city.text(),
            "state": self.txt_state.text(),
            "pin_code": self.txt_pin.text(),
            "country": self.txt_country.text(),
            "opening_balance": self.spin_opening.value(),
            "credit_limit": self.spin_credit.value() if self.chk_credit_limit.isChecked() else None,
            "payment_terms": self.combo_terms.currentText(),
            "custom_payment_days": self.spin_days.value() if self.combo_terms.currentText() == "Custom" else None,
            "preferred_payment_method": self.combo_payment.currentText(),
            "notes": self.txt_notes.toPlainText(),
            "is_active": self.chk_active.isChecked(),
        }

    def _save(self, allow_potential_duplicate: bool = False):
        self.lbl_error.clear()
        self.lbl_error.setVisible(False)
        self.btn_save.setEnabled(False)
        payload = self._payload()
        actor_context = current_actor_context()
        if actor_context is None:
            self.lbl_error.setText("Customer management unavailable: Sign in is required.")
            self.lbl_error.setVisible(True)
            self.btn_save.setEnabled(False)
            return
        try:
            with get_db() as db:
                saved = (
                    update_customer(
                        db, self.customer.id, payload,
                        actor_context=actor_context,
                        allow_potential_duplicate=allow_potential_duplicate,
                    )
                    if self.customer else
                    create_customer(
                        db,
                        payload,
                        actor_context=actor_context,
                        allow_potential_duplicate=allow_potential_duplicate,
                    )
                )
                customer_id = saved.id
            self.customer_saved.emit(customer_id)
            self.accept()
        except CustomerDuplicateWarning as warning:
            self.btn_save.setEnabled(True)
            answer = QMessageBox.warning(
                self,
                "Possible Duplicate Customer",
                f"{warning}\n\nDo you want to save this as a separate customer record?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                self._save(allow_potential_duplicate=True)
            return
        except (CustomerValidationError, ValueError) as error:
            self.lbl_error.setText(str(error))
            self.lbl_error.setVisible(True)
        except Exception as error:
            self.lbl_error.setText(f"Could not save customer: {error}")
            self.lbl_error.setVisible(True)
        finally:
            if self.isVisible():
                self.btn_save.setEnabled(True)
