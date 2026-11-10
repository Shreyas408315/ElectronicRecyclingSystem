from datetime import date
from decimal import Decimal
from PySide6.QtCore import Qt, Signal, QDate
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QTextEdit, QDoubleSpinBox, QSpinBox, QComboBox, QDateEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QGroupBox, QScrollArea, QWidget, QMessageBox, QFrame, QGridLayout,
    QTabWidget, QFileDialog
)
from database.connection import get_db
from database.models import DismantlingEntry
from services.dismantling_service import (
    COMPONENT_CATEGORIES,
    COMPONENT_CONDITIONS,
    SCRAP_CLASSIFICATIONS,
    DismantlingValidationError,
    generate_next_dismantling_code,
    get_purchases_with_undismantled_items,
    calculate_dismantling_recovery_math,
    create_dismantling_entry,
    update_dismantling_entry,
)
from services.security_service import AuthorizationError, current_actor_context
from services.security_service import require_permission
from utils.validators import to_decimal, format_currency


class DismantlingFormDialog(QDialog):
    """Dialog for creating or editing a dismantling batch entry with recovered components and scrap."""
    entry_saved = Signal(int)

    def __init__(self, parent=None, entry: DismantlingEntry | None = None):
        super().__init__(parent)
        self.entry = entry
        self.is_edit_mode = entry is not None

        title = f"Edit Dismantling Batch — {entry.dismantling_code}" if self.is_edit_mode else "New Appliance Dismantling Entry"
        self.setWindowTitle(title)
        self.resize(980, 800)
        self.setMinimumSize(880, 680)
        self.setModal(True)

        self.available_purchases: list[dict] = []
        self.current_purchase_items: list[dict] = []
        self.components_data: list[dict] = []
        self.scrap_data: list[dict] = []
        self.attachment_paths: list[str] = []
        self._editing_component_index: int | None = None
        self._editing_scrap_index: int | None = None
        self._loading = False

        self._init_ui()
        self._load_purchases_data()

        if self.is_edit_mode:
            self._load_existing_entry()
        else:
            self._load_auto_code()
        self._update_write_permissions()

    def _init_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Dialog Header
        header = QFrame()
        header.setObjectName("dialogHeader")
        h_layout = QVBoxLayout(header)
        h_layout.setContentsMargins(24, 18, 24, 18)

        title = QLabel("Edit Dismantling Batch" if self.is_edit_mode else "Record Appliance Dismantling & Recovery")
        title.setObjectName("dialogTitle")
        desc = QLabel(
            "Select incoming purchased appliances, record recovered recyclable components, scrap materials, and processing expenses."
        )
        desc.setObjectName("dialogDesc")
        desc.setWordWrap(True)

        h_layout.addWidget(title)
        h_layout.addWidget(desc)
        root_layout.addWidget(header)

        # 2. Scrollable Body
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("formScrollArea")

        container = QWidget()
        container.setObjectName("formContainer")
        form_layout = QVBoxLayout(container)
        form_layout.setContentsMargins(24, 16, 24, 16)
        form_layout.setSpacing(14)

        # Section A: Header & Inward Purchase Selection
        group_purchase = QGroupBox("Inward Batch Selection & Appliance Source")
        grid_p = QGridLayout(group_purchase)
        grid_p.setContentsMargins(16, 14, 16, 14)
        grid_p.setSpacing(10)

        grid_p.addWidget(QLabel("Dismantling ID:"), 0, 0)
        self.txt_code = QLineEdit()
        self.txt_code.setReadOnly(True)
        self.txt_code.setObjectName("readOnlyField")
        grid_p.addWidget(self.txt_code, 0, 1)

        grid_p.addWidget(QLabel("Date:"), 0, 2)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setDisplayFormat("dd-MM-yyyy")
        grid_p.addWidget(self.date_edit, 0, 3)

        grid_p.addWidget(QLabel("Select Purchase Order *:"), 1, 0)
        self.combo_purchase = QComboBox()
        self.combo_purchase.currentIndexChanged.connect(self._on_purchase_selected)
        grid_p.addWidget(self.combo_purchase, 1, 1, 1, 3)

        grid_p.addWidget(QLabel("Supplier Name:"), 2, 0)
        self.txt_supplier = QLineEdit()
        self.txt_supplier.setReadOnly(True)
        self.txt_supplier.setObjectName("readOnlyField")
        self.txt_supplier.setPlaceholderText("Auto-filled from selected purchase")
        grid_p.addWidget(self.txt_supplier, 2, 1)

        grid_p.addWidget(QLabel("Purchase Date:"), 2, 2)
        self.txt_purchase_date = QLineEdit()
        self.txt_purchase_date.setReadOnly(True)
        self.txt_purchase_date.setObjectName("readOnlyField")
        grid_p.addWidget(self.txt_purchase_date, 2, 3)

        grid_p.addWidget(QLabel("Select Appliance Item *:"), 3, 0)
        self.combo_item = QComboBox()
        self.combo_item.currentIndexChanged.connect(self._on_item_selected)
        grid_p.addWidget(self.combo_item, 3, 1, 1, 3)

        grid_p.addWidget(QLabel("Appliance Category:"), 4, 0)
        self.txt_category = QLineEdit()
        self.txt_category.setReadOnly(True)
        self.txt_category.setObjectName("readOnlyField")
        grid_p.addWidget(self.txt_category, 4, 1)

        grid_p.addWidget(QLabel("Brand:"), 4, 2)
        self.txt_brand = QLineEdit()
        self.txt_brand.setReadOnly(True)
        self.txt_brand.setObjectName("readOnlyField")
        grid_p.addWidget(self.txt_brand, 4, 3)

        grid_p.addWidget(QLabel("Model:"), 5, 0)
        self.txt_model = QLineEdit()
        self.txt_model.setReadOnly(True)
        self.txt_model.setObjectName("readOnlyField")
        grid_p.addWidget(self.txt_model, 5, 1, 1, 3)

        # Inward lot summary box
        self.box_item_summary = QFrame()
        self.box_item_summary.setObjectName("noticeBox")
        box_l = QVBoxLayout(self.box_item_summary)
        box_l.setContentsMargins(12, 8, 12, 8)
        self.lbl_item_details = QLabel("Select an inward purchase and appliance lot above to view available quantities.")
        self.lbl_item_details.setObjectName("noticeText")
        self.lbl_item_details.setWordWrap(True)
        box_l.addWidget(self.lbl_item_details)
        grid_p.addWidget(self.box_item_summary, 6, 0, 1, 4)

        form_layout.addWidget(group_purchase)

        # Section B: Batch Processing & Labour Costs
        group_proc = QGroupBox("Batch Dismantling Details & Processing Costs")
        grid_proc = QGridLayout(group_proc)
        grid_proc.setContentsMargins(16, 14, 16, 14)
        grid_proc.setSpacing(10)

        grid_proc.addWidget(QLabel("Number of Appliances Dismantled *:"), 0, 0)
        self.spin_dismantle_qty = QSpinBox()
        self.spin_dismantle_qty.setRange(1, 10000)
        self.spin_dismantle_qty.setValue(1)
        grid_proc.addWidget(self.spin_dismantle_qty, 0, 1)

        grid_proc.addWidget(QLabel("Total Input Batch Weight (kg):"), 0, 2)
        self.spin_input_weight = QDoubleSpinBox()
        self.spin_input_weight.setRange(0.0, 1000000.0)
        self.spin_input_weight.setDecimals(2)
        self.spin_input_weight.setValue(0.0)
        self.spin_input_weight.setSuffix(" kg")
        self.spin_input_weight.valueChanged.connect(self._recalculate_recovery_math)
        grid_proc.addWidget(self.spin_input_weight, 0, 3)

        grid_proc.addWidget(QLabel("Supervisor / Team Responsible:"), 1, 0)
        self.txt_team = QLineEdit()
        self.txt_team.setPlaceholderText("e.g. Dismantling Line A / Lead Vikram")
        grid_proc.addWidget(self.txt_team, 1, 1)

        grid_proc.addWidget(QLabel("Labour Charges (₹):"), 1, 2)
        self.spin_labour = self._create_currency_spinbox()
        self.spin_labour.valueChanged.connect(self._recalculate_recovery_math)
        grid_proc.addWidget(self.spin_labour, 1, 3)

        grid_proc.addWidget(QLabel("Other Processing Expenses (₹):"), 2, 0)
        self.spin_other_exp = self._create_currency_spinbox()
        self.spin_other_exp.valueChanged.connect(self._recalculate_recovery_math)
        grid_proc.addWidget(self.spin_other_exp, 2, 1)

        grid_proc.addWidget(QLabel("Total Processing Cost:"), 2, 2)
        self.lbl_total_proc_cost = QLabel("₹ 0.00")
        self.lbl_total_proc_cost.setObjectName("metricValue")
        grid_proc.addWidget(self.lbl_total_proc_cost, 2, 3)

        form_layout.addWidget(group_proc)

        # Section C: Tabs for Recovered Components and Scrap Materials
        self.tabs_recovery = QTabWidget()
        self.tabs_recovery.setObjectName("detailTabs")

        # Tab 1: Recovered Components
        tab_comp = QWidget()
        l_comp = QVBoxLayout(tab_comp)
        l_comp.setContentsMargins(14, 12, 14, 12)
        l_comp.setSpacing(10)

        # Component inline adder
        frame_add_c = QFrame()
        frame_add_c.setStyleSheet("background-color: #f1f5f9; border-radius: 6px; padding: 6px;")
        grid_c = QGridLayout(frame_add_c)
        grid_c.setContentsMargins(8, 6, 8, 6)
        grid_c.setSpacing(8)

        grid_c.addWidget(QLabel("Component Name *:"), 0, 0)
        self.combo_c_name = QComboBox()
        self.combo_c_name.setEditable(True)
        common_parts = [
            "Rotary Compressor", "Reciprocating Compressor", "Copper Wiring Harness",
            "Electric Motor", "Aluminium Condenser Coil", "Aluminium Evaporator",
            "Main Printed Circuit Board (PCB)", "Control Panel PCB", "Steel Chassis / Cabinet",
            "Drum / Tub Assembly", "Cooling Fan Motor", "Transformer Coil",
            "Magnetron (Microwave)", "Blower Fan & Impeller", "Plastic Housing (ABS/HIPS)",
        ]
        for p in common_parts:
            self.combo_c_name.addItem(p)
        grid_c.addWidget(self.combo_c_name, 0, 1)

        grid_c.addWidget(QLabel("Category:"), 0, 2)
        self.combo_c_cat = QComboBox()
        for cat in COMPONENT_CATEGORIES:
            self.combo_c_cat.addItem(cat)
        grid_c.addWidget(self.combo_c_cat, 0, 3)

        grid_c.addWidget(QLabel("Quantity:"), 1, 0)
        self.spin_c_qty = QDoubleSpinBox()
        self.spin_c_qty.setRange(0.01, 100000.0)
        self.spin_c_qty.setValue(1.0)
        self.spin_c_qty.setDecimals(2)
        grid_c.addWidget(self.spin_c_qty, 1, 1)

        grid_c.addWidget(QLabel("Unit:"), 1, 2)
        self.combo_c_unit = QComboBox()
        for u in ["pieces", "kg", "grams", "meters"]:
            self.combo_c_unit.addItem(u)
        grid_c.addWidget(self.combo_c_unit, 1, 3)

        grid_c.addWidget(QLabel("Weight (kg):"), 2, 0)
        self.spin_c_weight = QDoubleSpinBox()
        self.spin_c_weight.setRange(0.0, 100000.0)
        self.spin_c_weight.setDecimals(2)
        self.spin_c_weight.setValue(0.0)
        grid_c.addWidget(self.spin_c_weight, 2, 1)

        grid_c.addWidget(QLabel("Condition:"), 2, 2)
        self.combo_c_cond = QComboBox()
        for cond in COMPONENT_CONDITIONS:
            self.combo_c_cond.addItem(cond)
        grid_c.addWidget(self.combo_c_cond, 2, 3)

        grid_c.addWidget(QLabel("Est. Unit Value (₹):"), 3, 0)
        self.spin_c_val = self._create_currency_spinbox()
        grid_c.addWidget(self.spin_c_val, 3, 1)

        grid_c.addWidget(QLabel("Storage Location:"), 3, 2)
        self.txt_c_loc = QLineEdit()
        self.txt_c_loc.setPlaceholderText("e.g. Warehouse Bay 3 / Bin C-12")
        grid_c.addWidget(self.txt_c_loc, 3, 3)

        grid_c.addWidget(QLabel("Component Remarks:"), 4, 0)
        self.txt_c_remarks = QLineEdit()
        self.txt_c_remarks.setPlaceholderText("Optional notes for this recovered part")
        grid_c.addWidget(self.txt_c_remarks, 4, 1, 1, 3)

        self.btn_add_comp = QPushButton("+ Add Recovered Component")
        self.btn_add_comp.setObjectName("primaryButton")
        self.btn_add_comp.clicked.connect(self._add_component_row)
        grid_c.addWidget(self.btn_add_comp, 5, 2, 1, 2)

        l_comp.addWidget(frame_add_c)

        # Table of components
        self.table_comp = QTableWidget(0, 8)
        self.table_comp.setHorizontalHeaderLabels([
            "Component", "Category", "Qty", "Unit", "Weight (kg)", "Condition", "Est. Total (₹)", "Storage Location"
        ])
        h_c = self.table_comp.horizontalHeader()
        h_c.setSectionResizeMode(0, QHeaderView.Stretch)
        h_c.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self.table_comp.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_comp.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_comp.setFixedHeight(170)
        self.table_comp.setObjectName("styledTable")
        l_comp.addWidget(self.table_comp)

        component_actions = QHBoxLayout()
        self.btn_edit_comp = QPushButton("Edit Selected Component")
        self.btn_edit_comp.setObjectName("secondaryButton")
        self.btn_edit_comp.clicked.connect(self._edit_selected_component)
        component_actions.addWidget(self.btn_edit_comp)
        btn_del_comp = QPushButton("🗑 Remove Selected Component")
        btn_del_comp.setObjectName("secondaryButton")
        btn_del_comp.clicked.connect(self._remove_selected_component)
        component_actions.addWidget(btn_del_comp)
        component_actions.addStretch()
        l_comp.addLayout(component_actions)

        self.tabs_recovery.addTab(tab_comp, "Recovered Components (0)")

        # Tab 2: Scrap & Unusable Material
        tab_scrap = QWidget()
        l_scrap = QVBoxLayout(tab_scrap)
        l_scrap.setContentsMargins(14, 12, 14, 12)
        l_scrap.setSpacing(10)

        # Scrap inline adder
        frame_add_s = QFrame()
        frame_add_s.setStyleSheet("background-color: #f1f5f9; border-radius: 6px; padding: 6px;")
        grid_s = QGridLayout(frame_add_s)
        grid_s.setContentsMargins(8, 6, 8, 6)
        grid_s.setSpacing(8)

        grid_s.addWidget(QLabel("Scrap Material Type *:"), 0, 0)
        self.combo_s_type = QComboBox()
        self.combo_s_type.setEditable(True)
        common_scrap = [
            "Crushed Plastic Shell (Mixed)", "PUF Insulation Foam", "Glass Wool & Fibers",
            "Contaminated Iron Rust Slag", "Rubber Gaskets & Seals", "Hazardous E-Waste Residue",
            "Shattered Screen Glass", "General Non-Recyclable Fluff",
        ]
        for sc in common_scrap:
            self.combo_s_type.addItem(sc)
        grid_s.addWidget(self.combo_s_type, 0, 1)

        grid_s.addWidget(QLabel("Classification:"), 0, 2)
        self.combo_s_class = QComboBox()
        for cl in SCRAP_CLASSIFICATIONS:
            self.combo_s_class.addItem(cl)
        grid_s.addWidget(self.combo_s_class, 0, 3)

        grid_s.addWidget(QLabel("Quantity:"), 1, 0)
        self.spin_s_qty = QDoubleSpinBox()
        self.spin_s_qty.setRange(0.01, 100000.0)
        self.spin_s_qty.setValue(1.0)
        grid_s.addWidget(self.spin_s_qty, 1, 1)

        grid_s.addWidget(QLabel("Weight (kg) *:"), 1, 2)
        self.spin_s_weight = QDoubleSpinBox()
        self.spin_s_weight.setRange(0.0, 100000.0)
        self.spin_s_weight.setDecimals(2)
        self.spin_s_weight.setValue(1.0)
        grid_s.addWidget(self.spin_s_weight, 1, 3)

        grid_s.addWidget(QLabel("Unit:"), 2, 0)
        self.combo_s_unit = QComboBox()
        for u in ["kg", "pieces", "grams"]:
            self.combo_s_unit.addItem(u)
        grid_s.addWidget(self.combo_s_unit, 2, 1)

        grid_s.addWidget(QLabel("Est. Scrap Value (₹):"), 2, 2)
        self.spin_s_val = self._create_currency_spinbox()
        grid_s.addWidget(self.spin_s_val, 2, 3)

        grid_s.addWidget(QLabel("Disposal Remarks:"), 3, 0)
        self.txt_s_disp = QLineEdit()
        self.txt_s_disp.setPlaceholderText("e.g. Authorized Hazardous Landfill, Secondary Smelter")
        grid_s.addWidget(self.txt_s_disp, 3, 1, 1, 3)

        self.btn_add_scrap = QPushButton("+ Add Scrap Material")
        self.btn_add_scrap.setObjectName("primaryButton")
        self.btn_add_scrap.clicked.connect(self._add_scrap_row)
        grid_s.addWidget(self.btn_add_scrap, 4, 2, 1, 2)

        l_scrap.addWidget(frame_add_s)

        # Table of scrap items
        self.table_scrap = QTableWidget(0, 6)
        self.table_scrap.setHorizontalHeaderLabels([
            "Scrap Type", "Classification", "Quantity", "Weight (kg)", "Est. Value (₹)", "Disposal Method"
        ])
        h_s = self.table_scrap.horizontalHeader()
        h_s.setSectionResizeMode(0, QHeaderView.Stretch)
        h_s.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h_s.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h_s.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h_s.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        h_s.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table_scrap.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_scrap.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_scrap.setFixedHeight(170)
        self.table_scrap.setObjectName("styledTable")
        l_scrap.addWidget(self.table_scrap)

        scrap_actions = QHBoxLayout()
        self.btn_edit_scrap = QPushButton("Edit Selected Scrap")
        self.btn_edit_scrap.setObjectName("secondaryButton")
        self.btn_edit_scrap.clicked.connect(self._edit_selected_scrap)
        scrap_actions.addWidget(self.btn_edit_scrap)
        btn_del_scrap = QPushButton("🗑 Remove Selected Scrap")
        btn_del_scrap.setObjectName("secondaryButton")
        btn_del_scrap.clicked.connect(self._remove_selected_scrap)
        scrap_actions.addWidget(btn_del_scrap)
        scrap_actions.addStretch()
        l_scrap.addLayout(scrap_actions)

        self.tabs_recovery.addTab(tab_scrap, "Scrap & Waste (0)")

        form_layout.addWidget(self.tabs_recovery)

        # Section D: Recovery Analytics Summary Card
        card_metrics = QFrame()
        card_metrics.setObjectName("metricsContainer")
        c_layout = QGridLayout(card_metrics)
        c_layout.setContentsMargins(16, 12, 16, 12)
        c_layout.setSpacing(12)

        c_layout.addWidget(QLabel("Total Recovered Weight:"), 0, 0)
        self.lbl_rec_weight = QLabel("0.00 kg")
        self.lbl_rec_weight.setObjectName("metricValue")
        c_layout.addWidget(self.lbl_rec_weight, 0, 1)

        c_layout.addWidget(QLabel("Total Scrap Weight:"), 0, 2)
        self.lbl_scrap_weight = QLabel("0.00 kg")
        self.lbl_scrap_weight.setObjectName("metricValue")
        c_layout.addWidget(self.lbl_scrap_weight, 0, 3)

        c_layout.addWidget(QLabel("Estimated Recovery Value:"), 1, 0)
        self.lbl_est_value = QLabel("₹ 0.00")
        self.lbl_est_value.setObjectName("metricValuePrimary")
        c_layout.addWidget(self.lbl_est_value, 1, 1)

        c_layout.addWidget(QLabel("Recovery Percentage by Weight:"), 1, 2)
        self.lbl_rec_pct = QLabel("N/A")
        self.lbl_rec_pct.setObjectName("metricValue")
        c_layout.addWidget(self.lbl_rec_pct, 1, 3)

        form_layout.addWidget(card_metrics)

        # Section E: Remarks and optional photographs / attachments
        group_rem = QGroupBox("Remarks, Photographs & Attachments")
        v_rem = QVBoxLayout(group_rem)
        v_rem.setContentsMargins(16, 10, 16, 12)
        self.txt_remarks = QTextEdit()
        self.txt_remarks.setPlaceholderText("Enter shift notes, inspection observation, hazardous material precautions...")
        self.txt_remarks.setFixedHeight(60)
        v_rem.addWidget(self.txt_remarks)

        attach_row = QHBoxLayout()
        self.txt_attachments = QLineEdit()
        self.txt_attachments.setReadOnly(True)
        self.txt_attachments.setPlaceholderText("Optional photographs and documents — not required")
        self.txt_attachments.setObjectName("readOnlyField")
        btn_attach = QPushButton("Attach Files")
        btn_attach.setObjectName("secondaryButton")
        btn_attach.clicked.connect(self._choose_attachments)
        attach_row.addWidget(self.txt_attachments)
        attach_row.addWidget(btn_attach)
        v_rem.addLayout(attach_row)
        form_layout.addWidget(group_rem)

        scroll.setWidget(container)
        root_layout.addWidget(scroll)

        # Error notification banner
        self.lbl_error = QLabel()
        self.lbl_error.setObjectName("errorBanner")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setVisible(False)
        root_layout.addWidget(self.lbl_error)

        # Footer Actions
        footer = QFrame()
        footer.setObjectName("dialogFooter")
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(24, 14, 24, 14)
        f_layout.setSpacing(12)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("secondaryButton")
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_save_draft = QPushButton("Save as Draft")
        self.btn_save_draft.setObjectName("secondaryButton")
        self.btn_save_draft.clicked.connect(lambda: self._save_entry("Draft"))

        self.btn_submit = QPushButton("Submit for Approval")
        self.btn_submit.setObjectName("primaryButton")
        self.btn_submit.clicked.connect(lambda: self._save_entry("Pending Approval"))

        f_layout.addStretch()
        f_layout.addWidget(self.btn_cancel)
        f_layout.addWidget(self.btn_save_draft)
        f_layout.addWidget(self.btn_submit)
        root_layout.addWidget(footer)

    def _create_currency_spinbox(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 10000000.0)
        spin.setDecimals(2)
        spin.setPrefix("₹ ")
        spin.setValue(0.0)
        return spin

    def _load_auto_code(self):
        try:
            actor_context = current_actor_context()
            if actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                self.txt_code.setText(
                    generate_next_dismantling_code(
                        db, actor_context=actor_context
                    )
                )
        except AuthorizationError as exc:
            self._fail_closed_for_read_permission(exc)

    def _fail_closed_for_read_permission(self, error: AuthorizationError):
        self.available_purchases = []
        self.current_purchase_items = []
        self.combo_purchase.clear()
        self.combo_item.clear()
        self.txt_code.clear()
        self.btn_save_draft.setEnabled(False)
        self.btn_submit.setEnabled(False)
        self._show_error(
            f"Authentication or permission required to load dismantling data: {error}"
        )

    def _update_write_permissions(self):
        actor_context = current_actor_context()
        allowed = False
        if actor_context is not None:
            try:
                with get_db() as db:
                    require_permission(
                        actor_context, "dismantling.manage", db_session=db
                    )
                allowed = True
            except AuthorizationError:
                allowed = False
        self.btn_save_draft.setEnabled(allowed)
        self.btn_submit.setEnabled(allowed)
        if not allowed and self.lbl_error.isHidden():
            self._show_error(
                "Dismantling management permission is required to save or submit entries."
            )

    def _choose_attachments(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Photographs or Attachments",
            "",
            "Images and Documents (*.png *.jpg *.jpeg *.pdf *.webp);;All Files (*.*)",
        )
        if files:
            self.attachment_paths = files
            self.txt_attachments.setText(" | ".join(files))

    def _load_purchases_data(self):
        exclude_id = self.entry.id if self.is_edit_mode and self.entry else None
        try:
            actor_context = current_actor_context()
            if actor_context is None:
                raise AuthorizationError("Sign in is required.")
            with get_db() as db:
                self.available_purchases = get_purchases_with_undismantled_items(
                    db,
                    exclude_dismantling_id=exclude_id,
                    actor_context=actor_context,
                )
                self.combo_purchase.clear()
                self.combo_purchase.addItem("— Select Purchase Order with Undismantled Stock —", None)
                for p in self.available_purchases:
                    tot_avail = sum(it["remaining_quantity"] for it in p["available_items"])
                    self.combo_purchase.addItem(
                        f"{p['purchase_code']} — {p['supplier_name']} ({tot_avail} appliances pending)",
                        p["purchase_id"]
                    )
        except AuthorizationError as exc:
            self._fail_closed_for_read_permission(exc)

    def _on_purchase_selected(self):
        p_id = self.combo_purchase.currentData()
        self.combo_item.clear()
        self.current_purchase_items = []
        if not p_id:
            self.txt_supplier.clear()
            self.txt_purchase_date.clear()
            self.txt_category.clear()
            self.txt_brand.clear()
            self.txt_model.clear()
            self.lbl_item_details.setText("Select an inward purchase above to view available appliance lots.")
            return

        for p in self.available_purchases:
            if p["purchase_id"] == p_id:
                self.txt_supplier.setText(p["supplier_name"])
                p_date = p["purchase_date"]
                self.txt_purchase_date.setText(p_date.strftime("%d-%b-%Y") if p_date else "—")
                self.current_purchase_items = p["available_items"]
                for it in self.current_purchase_items:
                    self.combo_item.addItem(
                        f"{it['category']} ({it['brand']} {it['model']}) — {it['remaining_quantity']} available",
                        it["item_id"]
                    )
                break

    def _on_item_selected(self):
        item_id = self.combo_item.currentData()
        if not item_id:
            return

        for it in self.current_purchase_items:
            if it["item_id"] == item_id:
                self.txt_category.setText(it["category"])
                self.txt_brand.setText(it["brand"])
                self.txt_model.setText(it["model"])
                self.spin_dismantle_qty.setMaximum(max(1, it["remaining_quantity"]))
                if not self._loading:
                    self.spin_dismantle_qty.setValue(min(1, it["remaining_quantity"]) or 1)
                    if it["weight"] and it["weight"] > Decimal("0.00") and it["total_purchased"]:
                        weight_per_unit = it["weight"] / Decimal(str(it["total_purchased"]))
                        suggested = float(weight_per_unit * Decimal(str(self.spin_dismantle_qty.value())))
                        self.spin_input_weight.setValue(round(suggested, 2))

                details = (
                    f"<b>Category:</b> {it['category']}  |  <b>Brand/Model:</b> {it['brand']} {it['model']}<br/>"
                    f"<b>Originally Purchased:</b> {it['total_purchased']} {it['unit']}  |  "
                    f"<b>Available Undismantled:</b> <font color='#15803d'><b>{it['remaining_quantity']} {it['unit']}</b></font>"
                )
                self.lbl_item_details.setText(details)
                break

    def _add_component_row(self):
        name = self.combo_c_name.currentText().strip()
        if not name:
            self._show_error("Please specify a component name.")
            return

        cat = self.combo_c_cat.currentText()
        qty = self.spin_c_qty.value()
        unit = self.combo_c_unit.currentText()
        weight = self.spin_c_weight.value()
        cond = self.combo_c_cond.currentText()
        unit_val = self.spin_c_val.value()

        tot_val = (to_decimal(weight) * to_decimal(unit_val)) if (unit == "kg" and weight > 0) else (to_decimal(qty) * to_decimal(unit_val))

        comp = {
            "component_name": name,
            "component_category": cat,
            "quantity": to_decimal(qty),
            "unit": unit,
            "weight": to_decimal(weight),
            "condition": cond,
            "estimated_unit_value": to_decimal(unit_val),
            "estimated_total_value": tot_val,
            "storage_location": self.txt_c_loc.text().strip() or None,
            "remarks": self.txt_c_remarks.text().strip() or None,
        }
        if self._editing_component_index is None:
            self.components_data.append(comp)
        else:
            self.components_data[self._editing_component_index] = comp
        self._reset_component_editor()
        self._clear_error()

        self._refresh_components_table()
        self._recalculate_recovery_math()

    def _edit_selected_component(self):
        selected = self.table_comp.selectionModel().selectedRows()
        if not selected:
            return
        row = selected[0].row()
        if not 0 <= row < len(self.components_data):
            return

        component = self.components_data[row]
        self._editing_component_index = row
        self.combo_c_name.setEditText(component["component_name"])
        self.combo_c_cat.setCurrentText(component["component_category"])
        self.spin_c_qty.setValue(float(component["quantity"]))
        self.combo_c_unit.setCurrentText(component["unit"])
        self.spin_c_weight.setValue(float(component["weight"] or 0))
        self.combo_c_cond.setCurrentText(component["condition"])
        self.spin_c_val.setValue(float(component["estimated_unit_value"]))
        self.txt_c_loc.setText(component["storage_location"] or "")
        self.txt_c_remarks.setText(component["remarks"] or "")
        self.btn_add_comp.setText("Update Recovered Component")

    def _reset_component_editor(self):
        self._editing_component_index = None
        self.combo_c_name.setEditText("")
        self.spin_c_qty.setValue(1.0)
        self.spin_c_weight.setValue(0.0)
        self.spin_c_val.setValue(0.0)
        self.txt_c_loc.clear()
        self.txt_c_remarks.clear()
        self.btn_add_comp.setText("+ Add Recovered Component")

    def _remove_selected_component(self):
        sel = self.table_comp.selectionModel().selectedRows()
        if sel:
            row = sel[0].row()
            if 0 <= row < len(self.components_data):
                self.components_data.pop(row)
                if self._editing_component_index == row:
                    self._reset_component_editor()
                elif self._editing_component_index is not None and row < self._editing_component_index:
                    self._editing_component_index -= 1
                self._refresh_components_table()
                self._recalculate_recovery_math()

    def _refresh_components_table(self):
        self.table_comp.setRowCount(len(self.components_data))
        for r, c in enumerate(self.components_data):
            self.table_comp.setItem(r, 0, QTableWidgetItem(c["component_name"]))
            self.table_comp.setItem(r, 1, QTableWidgetItem(c["component_category"]))
            self.table_comp.setItem(r, 2, QTableWidgetItem(f"{c['quantity']:.2f}"))
            self.table_comp.setItem(r, 3, QTableWidgetItem(c["unit"]))
            self.table_comp.setItem(r, 4, QTableWidgetItem(f"{c['weight']:.2f}" if c['weight'] else "—"))
            self.table_comp.setItem(r, 5, QTableWidgetItem(c["condition"]))
            self.table_comp.setItem(r, 6, QTableWidgetItem(f"₹ {c['estimated_total_value']:.2f}"))
            self.table_comp.setItem(r, 7, QTableWidgetItem(c["storage_location"] or "—"))
        self.tabs_recovery.setTabText(0, f"Recovered Components ({len(self.components_data)})")

    def _add_scrap_row(self):
        s_type = self.combo_s_type.currentText().strip()
        if not s_type:
            self._show_error("Please specify a scrap material type.")
            return

        s_class = self.combo_s_class.currentText()
        qty = self.spin_s_qty.value()
        weight = self.spin_s_weight.value()
        val = self.spin_s_val.value()
        disp = self.txt_s_disp.text().strip() or None

        scrap = {
            "scrap_type": s_type,
            "material_classification": s_class,
            "quantity": to_decimal(qty),
            "weight": to_decimal(weight),
            "unit": self.combo_s_unit.currentText() or "kg",
            "estimated_value": to_decimal(val),
            "disposal_method": disp,
            "remarks": None,
        }
        if self._editing_scrap_index is None:
            self.scrap_data.append(scrap)
        else:
            self.scrap_data[self._editing_scrap_index] = scrap
        self._reset_scrap_editor()
        self._clear_error()

        self._refresh_scrap_table()
        self._recalculate_recovery_math()

    def _edit_selected_scrap(self):
        selected = self.table_scrap.selectionModel().selectedRows()
        if not selected:
            return
        row = selected[0].row()
        if not 0 <= row < len(self.scrap_data):
            return

        scrap = self.scrap_data[row]
        self._editing_scrap_index = row
        self.combo_s_type.setEditText(scrap["scrap_type"])
        self.combo_s_class.setCurrentText(scrap["material_classification"])
        self.spin_s_qty.setValue(float(scrap["quantity"]))
        self.spin_s_weight.setValue(float(scrap["weight"]))
        self.combo_s_unit.setCurrentText(scrap["unit"])
        self.spin_s_val.setValue(float(scrap["estimated_value"]))
        self.txt_s_disp.setText(scrap["disposal_method"] or "")
        self.btn_add_scrap.setText("Update Scrap Material")

    def _reset_scrap_editor(self):
        self._editing_scrap_index = None
        self.combo_s_type.setEditText("")
        self.spin_s_qty.setValue(1.0)
        self.spin_s_weight.setValue(1.0)
        self.spin_s_val.setValue(0.0)
        self.txt_s_disp.clear()
        self.btn_add_scrap.setText("+ Add Scrap Material")

    def _remove_selected_scrap(self):
        sel = self.table_scrap.selectionModel().selectedRows()
        if sel:
            row = sel[0].row()
            if 0 <= row < len(self.scrap_data):
                self.scrap_data.pop(row)
                if self._editing_scrap_index == row:
                    self._reset_scrap_editor()
                elif self._editing_scrap_index is not None and row < self._editing_scrap_index:
                    self._editing_scrap_index -= 1
                self._refresh_scrap_table()
                self._recalculate_recovery_math()

    def _refresh_scrap_table(self):
        self.table_scrap.setRowCount(len(self.scrap_data))
        for r, s in enumerate(self.scrap_data):
            self.table_scrap.setItem(r, 0, QTableWidgetItem(s["scrap_type"]))
            self.table_scrap.setItem(r, 1, QTableWidgetItem(s["material_classification"]))
            self.table_scrap.setItem(r, 2, QTableWidgetItem(f"{s['quantity']:.2f}"))
            self.table_scrap.setItem(r, 3, QTableWidgetItem(f"{s['weight']:.2f} kg"))
            self.table_scrap.setItem(r, 4, QTableWidgetItem(f"₹ {s['estimated_value']:.2f}"))
            self.table_scrap.setItem(r, 5, QTableWidgetItem(s["disposal_method"] or "—"))
        self.tabs_recovery.setTabText(1, f"Scrap & Waste ({len(self.scrap_data)})")

    def _recalculate_recovery_math(self):
        labour = to_decimal(self.spin_labour.value())
        other = to_decimal(self.spin_other_exp.value())
        input_w = to_decimal(self.spin_input_weight.value()) if self.spin_input_weight.value() > 0 else None

        calc = calculate_dismantling_recovery_math(
            self.components_data, self.scrap_data, labour, other, input_w
        )

        self.lbl_total_proc_cost.setText(format_currency(calc["total_processing_cost"]))
        self.lbl_rec_weight.setText(f"{calc['total_recovered_weight']:.2f} kg")
        self.lbl_scrap_weight.setText(f"{calc['total_scrap_weight']:.2f} kg")
        self.lbl_est_value.setText(format_currency(calc["estimated_recovery_value"]))

        if calc["recovery_percentage"] is not None:
            self.lbl_rec_pct.setText(f"{calc['recovery_percentage']:.1f}%")
        else:
            self.lbl_rec_pct.setText("N/A (Input weight not set)")

    def _load_existing_entry(self):
        e = self.entry
        self._loading = True
        self.txt_code.setText(e.dismantling_code)
        self.date_edit.setDate(QDate(e.dismantling_date.year, e.dismantling_date.month, e.dismantling_date.day))
        self.spin_dismantle_qty.setValue(e.appliances_dismantled)
        self.spin_input_weight.setValue(float(e.input_weight or 0.0))
        self.txt_team.setText(e.team_leader or "")
        self.spin_labour.setValue(float(e.labour_charges))
        self.spin_other_exp.setValue(float(e.other_expenses))
        self.txt_remarks.setPlainText(e.remarks or "")
        if e.attachments:
            self.attachment_paths = [p for p in e.attachments.split("|") if p.strip()]
            self.txt_attachments.setText(" | ".join(self.attachment_paths))

        # Select purchase and item
        idx_p = self.combo_purchase.findData(e.purchase_id)
        if idx_p >= 0:
            self.combo_purchase.setCurrentIndex(idx_p)
            idx_it = self.combo_item.findData(e.purchase_item_id)
            if idx_it >= 0:
                self.combo_item.setCurrentIndex(idx_it)

        # Load components
        self.components_data = [
            {
                "component_name": c.component_name,
                "component_category": c.component_category,
                "quantity": c.quantity,
                "unit": c.unit,
                "weight": c.weight,
                "condition": c.condition,
                "estimated_unit_value": c.estimated_unit_value,
                "estimated_total_value": c.estimated_total_value,
                "storage_location": c.storage_location,
                "remarks": c.remarks,
            }
            for c in e.recovered_components
        ]
        self._refresh_components_table()

        # Load scrap
        self.scrap_data = [
            {
                "scrap_type": s.scrap_type,
                "material_classification": s.material_classification,
                "quantity": s.quantity,
                "weight": s.weight,
                "unit": s.unit,
                "estimated_value": s.estimated_value,
                "disposal_method": s.disposal_method,
                "remarks": s.remarks,
            }
            for s in e.scrap_materials
        ]
        self._refresh_scrap_table()
        self._recalculate_recovery_math()
        self._loading = False

    def _show_error(self, message: str):
        self.lbl_error.setText(f"⚠ {message}")
        self.lbl_error.setVisible(True)

    def _clear_error(self):
        self.lbl_error.setText("")
        self.lbl_error.setVisible(False)

    def _save_entry(self, status: str):
        self._clear_error()
        actor_context = current_actor_context()
        if actor_context is None:
            self._fail_closed_for_read_permission(
                AuthorizationError("Sign in is required.")
            )
            return

        p_id = self.combo_purchase.currentData()
        p_item_id = self.combo_item.currentData()

        if not p_id or not p_item_id:
            self._show_error("Please select an inward purchase order and appliance item.")
            return

        if len(self.components_data) == 0 and len(self.scrap_data) == 0:
            self._show_error("Please record at least one recovered component or scrap material item.")
            return

        header_payload = {
            "dismantling_code": self.txt_code.text().strip(),
            "dismantling_date": self.date_edit.date().toPython(),
            "purchase_id": p_id,
            "purchase_item_id": p_item_id,
            "appliances_dismantled": self.spin_dismantle_qty.value(),
            "input_weight": self.spin_input_weight.value(),
            "team_leader": self.txt_team.text().strip(),
            "labour_charges": self.spin_labour.value(),
            "other_expenses": self.spin_other_exp.value(),
            "status": status,
            "remarks": self.txt_remarks.toPlainText().strip(),
            "attachments": "|".join(self.attachment_paths) if self.attachment_paths else None,
        }

        try:
            self.btn_save_draft.setEnabled(False)
            self.btn_submit.setEnabled(False)
            with get_db() as db:
                if self.is_edit_mode:
                    entry = update_dismantling_entry(
                        db, self.entry.id, header_payload, self.components_data,
                        self.scrap_data, actor_context=actor_context,
                    )
                    entry_id = entry.id
                    QMessageBox.information(
                        self,
                        "Batch Updated",
                        f"Dismantling batch '{entry.dismantling_code}' has been updated."
                    )
                else:
                    entry = create_dismantling_entry(
                        db, header_payload, self.components_data, self.scrap_data,
                        actor_context=actor_context,
                    )
                    entry_id = entry.id
                    status_note = "saved as Draft" if status == "Draft" else "submitted for supervisor approval"
                    QMessageBox.information(
                        self,
                        "Dismantling Recorded",
                        f"Dismantling batch '{entry.dismantling_code}' successfully {status_note}."
                    )

            self.entry_saved.emit(entry_id)
            self.accept()

        except AuthorizationError as e:
            self._show_error(f"Permission denied: {e}")
            self._update_write_permissions()
        except DismantlingValidationError as e:
            self._show_error(str(e))
            self._update_write_permissions()
        except Exception as e:
            self._show_error(f"Database error: {str(e)}")
            self._update_write_permissions()
