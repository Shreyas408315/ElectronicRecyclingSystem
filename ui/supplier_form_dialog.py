from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QTextEdit, QDoubleSpinBox, QCheckBox, QPushButton,
    QGridLayout, QGroupBox, QScrollArea, QWidget, QMessageBox, QFrame
)
from database.connection import get_db
from database.models import Supplier
from services.supplier_service import (
    SupplierValidationError,
    generate_next_supplier_code,
    create_supplier,
    update_supplier,
)
from services.security_service import AuthorizationError, current_actor_context


class SupplierFormDialog(QDialog):
    """Dialog for creating and editing supplier records."""
    supplier_saved = Signal(int)  # Emits the supplier id when saved

    def __init__(self, parent=None, supplier: Supplier | None = None):
        super().__init__(parent)
        self.supplier = supplier
        self.is_edit_mode = supplier is not None

        title = f"Edit Supplier — {supplier.supplier_code}" if self.is_edit_mode else "Add New Supplier"
        self.setWindowTitle(title)
        self.resize(720, 680)
        self.setMinimumSize(600, 550)
        self.setModal(True)

        self._init_ui()
        if self.is_edit_mode:
            self._load_supplier_data()
        else:
            self._load_auto_code()

    def _init_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Header banner
        header = QFrame()
        header.setObjectName("dialogHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(24, 20, 24, 20)

        header_title = QLabel("Edit Supplier Details" if self.is_edit_mode else "Register New Supplier")
        header_title.setObjectName("dialogTitle")
        header_desc = QLabel(
            "Update existing supplier contact, taxation, and financial information."
            if self.is_edit_mode
            else "Enter supplier details to register them in the electronic recycling procurement ledger."
        )
        header_desc.setObjectName("dialogDesc")
        header_desc.setWordWrap(True)

        header_layout.addWidget(header_title)
        header_layout.addWidget(header_desc)
        root_layout.addWidget(header)

        # Scroll area for form fields
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("formScrollArea")

        container = QWidget()
        container.setObjectName("formContainer")
        form_layout = QVBoxLayout(container)
        form_layout.setContentsMargins(24, 16, 24, 16)
        form_layout.setSpacing(16)

        # Group 1: Identity & Primary Info
        group_identity = QGroupBox("Basic Information")
        grid1 = QGridLayout(group_identity)
        grid1.setSpacing(12)
        grid1.setContentsMargins(16, 16, 16, 16)

        lbl_code = QLabel("Supplier ID:")
        self.txt_code = QLineEdit()
        self.txt_code.setReadOnly(True)
        self.txt_code.setPlaceholderText("Auto-generated (e.g. SUP-000001)")
        self.txt_code.setObjectName("readOnlyField")
        grid1.addWidget(lbl_code, 0, 0)
        grid1.addWidget(self.txt_code, 0, 1)

        lbl_name = QLabel("Business / Supplier Name *:")
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("e.g. Apex Recyclers Pvt Ltd")
        grid1.addWidget(lbl_name, 1, 0)
        grid1.addWidget(self.txt_name, 1, 1)

        lbl_contact = QLabel("Contact Person:")
        self.txt_contact = QLineEdit()
        self.txt_contact.setPlaceholderText("e.g. Mr. Rajesh Sharma")
        grid1.addWidget(lbl_contact, 2, 0)
        grid1.addWidget(self.txt_contact, 2, 1)

        form_layout.addWidget(group_identity)

        # Group 2: Contact Information
        group_contact = QGroupBox("Contact Details")
        grid2 = QGridLayout(group_contact)
        grid2.setSpacing(12)
        grid2.setContentsMargins(16, 16, 16, 16)

        lbl_phone = QLabel("Primary Phone:")
        self.txt_phone = QLineEdit()
        self.txt_phone.setPlaceholderText("e.g. 9876543210")
        grid2.addWidget(lbl_phone, 0, 0)
        grid2.addWidget(self.txt_phone, 0, 1)

        lbl_alt_phone = QLabel("Alternate Phone:")
        self.txt_alt_phone = QLineEdit()
        self.txt_alt_phone.setPlaceholderText("e.g. 022-28765432")
        grid2.addWidget(lbl_alt_phone, 0, 2)
        grid2.addWidget(self.txt_alt_phone, 0, 3)

        lbl_email = QLabel("Email Address:")
        self.txt_email = QLineEdit()
        self.txt_email.setPlaceholderText("e.g. supplier@domain.com")
        grid2.addWidget(lbl_email, 1, 0)
        grid2.addWidget(self.txt_email, 1, 1, 1, 3)

        form_layout.addWidget(group_contact)

        # Group 3: Location & Address
        group_addr = QGroupBox("Location & Address")
        grid3 = QGridLayout(group_addr)
        grid3.setSpacing(12)
        grid3.setContentsMargins(16, 16, 16, 16)

        lbl_addr = QLabel("Complete Address:")
        self.txt_address = QLineEdit()
        self.txt_address.setPlaceholderText("Plot No, Industrial Estate, Street")
        grid3.addWidget(lbl_addr, 0, 0)
        grid3.addWidget(self.txt_address, 0, 1, 1, 3)

        lbl_city = QLabel("City:")
        self.txt_city = QLineEdit()
        self.txt_city.setPlaceholderText("e.g. Mumbai")
        grid3.addWidget(lbl_city, 1, 0)
        grid3.addWidget(self.txt_city, 1, 1)

        lbl_state = QLabel("State:")
        self.txt_state = QLineEdit()
        self.txt_state.setPlaceholderText("e.g. Maharashtra")
        grid3.addWidget(lbl_state, 1, 2)
        grid3.addWidget(self.txt_state, 1, 3)

        lbl_pin = QLabel("PIN Code:")
        self.txt_pin = QLineEdit()
        self.txt_pin.setPlaceholderText("e.g. 400001 (6 digits)")
        grid3.addWidget(lbl_pin, 2, 0)
        grid3.addWidget(self.txt_pin, 2, 1)

        form_layout.addWidget(group_addr)

        # Group 4: Tax & Financials
        group_fin = QGroupBox("Taxation & Opening Balance")
        grid4 = QGridLayout(group_fin)
        grid4.setSpacing(12)
        grid4.setContentsMargins(16, 16, 16, 16)

        lbl_gstin = QLabel("GSTIN:")
        self.txt_gstin = QLineEdit()
        self.txt_gstin.setPlaceholderText("15-char GSTIN (e.g. 27ABCDE1234F1Z5)")
        # Auto convert GSTIN to uppercase on typing
        self.txt_gstin.textChanged.connect(lambda t: self.txt_gstin.setText(t.upper()))
        grid4.addWidget(lbl_gstin, 0, 0)
        grid4.addWidget(self.txt_gstin, 0, 1)

        lbl_bal = QLabel("Opening Balance (₹):")
        self.spin_opening_balance = QDoubleSpinBox()
        self.spin_opening_balance.setRange(-999999999.0, 999999999.0)
        self.spin_opening_balance.setDecimals(2)
        self.spin_opening_balance.setPrefix("₹ ")
        self.spin_opening_balance.setValue(0.0)
        grid4.addWidget(lbl_bal, 0, 2)
        grid4.addWidget(self.spin_opening_balance, 0, 3)

        lbl_status = QLabel("Account Status:")
        self.chk_active = QCheckBox("Supplier is Active")
        self.chk_active.setChecked(True)
        grid4.addWidget(lbl_status, 1, 0)
        grid4.addWidget(self.chk_active, 1, 1)

        form_layout.addWidget(group_fin)

        # Group 5: Remarks
        group_remarks = QGroupBox("Additional Notes / Remarks")
        vbox_rem = QVBoxLayout(group_remarks)
        vbox_rem.setContentsMargins(16, 12, 16, 16)
        self.txt_remarks = QTextEdit()
        self.txt_remarks.setPlaceholderText("Enter any special terms, scrap categories supplied, payment remarks...")
        self.txt_remarks.setFixedHeight(70)
        vbox_rem.addWidget(self.txt_remarks)

        form_layout.addWidget(group_remarks)

        scroll.setWidget(container)
        root_layout.addWidget(scroll)

        # Error notification banner
        self.lbl_error = QLabel()
        self.lbl_error.setObjectName("errorBanner")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setVisible(False)
        root_layout.addWidget(self.lbl_error)

        # Action Buttons Footer
        footer = QFrame()
        footer.setObjectName("dialogFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(24, 16, 24, 16)
        footer_layout.setSpacing(12)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("secondaryButton")
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_save = QPushButton("Update Supplier" if self.is_edit_mode else "Save Supplier")
        self.btn_save.setObjectName("primaryButton")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._save_supplier)

        footer_layout.addStretch()
        footer_layout.addWidget(self.btn_cancel)
        footer_layout.addWidget(self.btn_save)
        root_layout.addWidget(footer)

    def _load_auto_code(self):
        try:
            with get_db() as db:
                next_code = generate_next_supplier_code(
                    db, actor_context=current_actor_context()
                )
                self.txt_code.setText(next_code)
        except AuthorizationError:
            self.txt_code.clear()
            self.btn_save.setEnabled(False)
            self._show_error("You do not have permission to create suppliers.")

    def _load_supplier_data(self):
        s = self.supplier
        self.txt_code.setText(s.supplier_code)
        self.txt_name.setText(s.business_name or "")
        self.txt_contact.setText(s.contact_person or "")
        self.txt_phone.setText(s.phone or "")
        self.txt_alt_phone.setText(s.alternate_phone or "")
        self.txt_email.setText(s.email or "")
        self.txt_address.setText(s.address or "")
        self.txt_city.setText(s.city or "")
        self.txt_state.setText(s.state or "")
        self.txt_pin.setText(s.pin_code or "")
        self.txt_gstin.setText(s.gstin or "")
        self.spin_opening_balance.setValue(float(s.opening_balance or 0.0))
        self.txt_remarks.setPlainText(s.remarks or "")
        self.chk_active.setChecked(s.is_active)

    def _show_error(self, message: str):
        self.lbl_error.setText(f"⚠ {message}")
        self.lbl_error.setVisible(True)

    def _clear_error(self):
        self.lbl_error.setText("")
        self.lbl_error.setVisible(False)

    def _save_supplier(self):
        self._clear_error()

        payload = {
            "supplier_code": self.txt_code.text().strip(),
            "business_name": self.txt_name.text().strip(),
            "contact_person": self.txt_contact.text().strip(),
            "phone": self.txt_phone.text().strip(),
            "alternate_phone": self.txt_alt_phone.text().strip(),
            "email": self.txt_email.text().strip(),
            "address": self.txt_address.text().strip(),
            "city": self.txt_city.text().strip(),
            "state": self.txt_state.text().strip(),
            "pin_code": self.txt_pin.text().strip(),
            "gstin": self.txt_gstin.text().strip(),
            "opening_balance": self.spin_opening_balance.value(),
            "remarks": self.txt_remarks.toPlainText().strip(),
            "is_active": self.chk_active.isChecked(),
        }

        try:
            with get_db() as db:
                if self.is_edit_mode:
                    saved_sup = update_supplier(
                        db,
                        self.supplier.id,
                        payload,
                        actor_context=current_actor_context(),
                    )
                    sup_id = saved_sup.id
                    QMessageBox.information(
                        self,
                        "Supplier Updated",
                        f"Supplier '{saved_sup.business_name}' ({saved_sup.supplier_code}) updated successfully."
                    )
                else:
                    saved_sup = create_supplier(
                        db, payload, actor_context=current_actor_context()
                    )
                    sup_id = saved_sup.id
                    QMessageBox.information(
                        self,
                        "Supplier Added",
                        f"Supplier '{saved_sup.business_name}' registered successfully with ID {saved_sup.supplier_code}."
                    )

            self.supplier_saved.emit(sup_id)
            self.accept()

        except AuthorizationError:
            self._show_error("Sign in with supplier-management permission to save supplier changes.")
        except SupplierValidationError as e:
            self._show_error(str(e))
        except Exception as e:
            self._show_error(f"Database error: {str(e)}")
