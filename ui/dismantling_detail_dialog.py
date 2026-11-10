from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox,
    QWidget, QFrame, QMessageBox, QTabWidget, QScrollArea, QGridLayout, QInputDialog
)
from database.connection import get_db
from database.models import DismantlingEntry
from services.dismantling_service import (
    get_dismantling_by_id,
    approve_dismantling_entry,
    reject_dismantling_entry,
    reverse_approved_dismantling_entry,
)
from services.security_service import (
    AuthorizationError,
    current_actor_context,
    require_permission,
)
from utils.validators import format_currency


class DismantlingDetailDialog(QDialog):
    """Detailed view for a dismantling batch showing recovery stats, components, scrap, and inventory audit trail."""
    entry_updated = Signal(int)

    def __init__(self, parent=None, entry_id: int = 0):
        super().__init__(parent)
        self.entry_id = entry_id
        self.entry_data: dict | None = None

        self.resize(920, 750)
        self.setMinimumSize(800, 620)
        self.setModal(True)

        self._load_entry_data()
        self._init_ui()
        self._populate_data()

    def _load_entry_data(self):
        with get_db() as db:
            try:
                actor_context = current_actor_context()
                if actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                e = get_dismantling_by_id(
                    db,
                    self.entry_id,
                    actor_context=actor_context,
                )
            except AuthorizationError:
                self.entry_data = None
                QMessageBox.warning(
                    self,
                    "Authentication Required",
                    "Sign in with dismantling-view permission to view this batch.",
                )
                return
            if not e:
                return

            sup_name = e.purchase.supplier.business_name if e.purchase and e.purchase.supplier else "—"
            p_code = e.purchase.purchase_code if e.purchase else "—"

            self.entry_data = {
                "id": e.id,
                "dismantling_code": e.dismantling_code,
                "dismantling_date": e.dismantling_date,
                "purchase_code": p_code,
                "supplier_name": sup_name,
                "appliance_category": e.appliance_category,
                "brand": e.brand,
                "model_number": e.model_number,
                "appliances_dismantled": e.appliances_dismantled,
                "input_weight": e.input_weight,
                "team_leader": e.team_leader,
                "labour_charges": e.labour_charges,
                "other_expenses": e.other_expenses,
                "total_processing_cost": e.total_processing_cost,
                "total_recovered_weight": e.total_recovered_weight,
                "total_scrap_weight": e.total_scrap_weight,
                "estimated_recovery_value": e.estimated_recovery_value,
                "recovery_percentage": e.recovery_percentage,
                "status": e.status,
                "remarks": e.remarks,
                "approved_by": e.approved_by,
                "approved_at": e.approved_at,
                "is_inventory_updated": e.is_inventory_updated,
                "created_at": e.created_at,
                "components": [
                    {
                        "name": c.component_name,
                        "category": c.component_category,
                        "quantity": c.quantity,
                        "unit": c.unit,
                        "weight": c.weight,
                        "condition": c.condition,
                        "estimated_unit_value": c.estimated_unit_value,
                        "estimated_total_value": c.estimated_total_value,
                        "storage_location": c.storage_location,
                        "inventory_item_id": c.inventory_item_id,
                    }
                    for c in e.recovered_components
                ],
                "scrap": [
                    {
                        "type": s.scrap_type,
                        "classification": s.material_classification,
                        "quantity": s.quantity,
                        "weight": s.weight,
                        "unit": s.unit,
                        "estimated_value": s.estimated_value,
                        "disposal_method": s.disposal_method,
                    }
                    for s in e.scrap_materials
                ],
            }

    def _init_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Header Profile Banner
        self.header_card = QFrame()
        self.header_card.setObjectName("detailHeaderCard")
        h_layout = QVBoxLayout(self.header_card)
        h_layout.setContentsMargins(24, 18, 24, 18)

        top_row = QHBoxLayout()
        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("detailSupplierName")
        self.lbl_code_badge = QLabel()
        self.lbl_code_badge.setObjectName("codeBadge")
        self.lbl_status_badge = QLabel()
        self.lbl_status_badge.setObjectName("statusBadge")

        top_row.addWidget(self.lbl_title)
        top_row.addWidget(self.lbl_code_badge)
        top_row.addWidget(self.lbl_status_badge)
        top_row.addStretch()
        h_layout.addLayout(top_row)

        self.lbl_sub = QLabel()
        self.lbl_sub.setObjectName("detailRegDate")
        h_layout.addWidget(self.lbl_sub)
        root_layout.addWidget(self.header_card)

        # Scrollable Area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        body_layout = QVBoxLayout(container)
        body_layout.setContentsMargins(24, 16, 24, 16)
        body_layout.setSpacing(14)

        # Metric Cards Summary Bar
        metrics_box = QFrame()
        metrics_box.setObjectName("metricsContainer")
        m_layout = QGridLayout(metrics_box)
        m_layout.setContentsMargins(16, 12, 16, 12)
        m_layout.setSpacing(12)

        m_layout.addWidget(QLabel("Appliances Dismantled:"), 0, 0)
        self.val_qty = QLabel("0")
        self.val_qty.setObjectName("metricValue")
        m_layout.addWidget(self.val_qty, 0, 1)

        m_layout.addWidget(QLabel("Input Batch Weight:"), 0, 2)
        self.val_input_wt = QLabel("0.00 kg")
        self.val_input_wt.setObjectName("metricValue")
        m_layout.addWidget(self.val_input_wt, 0, 3)

        m_layout.addWidget(QLabel("Recovered Weight:"), 1, 0)
        self.val_rec_wt = QLabel("0.00 kg")
        self.val_rec_wt.setObjectName("metricValue")
        m_layout.addWidget(self.val_rec_wt, 1, 1)

        m_layout.addWidget(QLabel("Scrap Weight:"), 1, 2)
        self.val_scrap_wt = QLabel("0.00 kg")
        self.val_scrap_wt.setObjectName("metricValue")
        m_layout.addWidget(self.val_scrap_wt, 1, 3)

        m_layout.addWidget(QLabel("Recovery % by Weight:"), 2, 0)
        self.val_rec_pct = QLabel("N/A")
        self.val_rec_pct.setObjectName("metricValuePrimary")
        m_layout.addWidget(self.val_rec_pct, 2, 1)

        m_layout.addWidget(QLabel("Est. Recovery Valuation:"), 2, 2)
        self.val_est_val = QLabel("₹ 0.00")
        self.val_est_val.setObjectName("metricValuePrimary")
        m_layout.addWidget(self.val_est_val, 2, 3)

        body_layout.addWidget(metrics_box)

        # Inward Batch Meta Information
        group_meta = QGroupBox("Inward Lot & Processing Costs")
        g_meta = QGridLayout(group_meta)
        g_meta.setContentsMargins(16, 14, 16, 14)
        g_meta.setSpacing(10)

        g_meta.addWidget(QLabel("Source Purchase Order:"), 0, 0)
        self.val_po = QLabel("—")
        self.val_po.setObjectName("fieldVal")
        g_meta.addWidget(self.val_po, 0, 1)

        g_meta.addWidget(QLabel("Supplier Business:"), 0, 2)
        self.val_sup = QLabel("—")
        self.val_sup.setObjectName("fieldVal")
        g_meta.addWidget(self.val_sup, 0, 3)

        g_meta.addWidget(QLabel("Appliance Model:"), 1, 0)
        self.val_model = QLabel("—")
        self.val_model.setObjectName("fieldVal")
        g_meta.addWidget(self.val_model, 1, 1)

        g_meta.addWidget(QLabel("Team / Supervisor:"), 1, 2)
        self.val_team = QLabel("—")
        self.val_team.setObjectName("fieldVal")
        g_meta.addWidget(self.val_team, 1, 3)

        g_meta.addWidget(QLabel("Labour Expenses:"), 2, 0)
        self.val_labour = QLabel("₹ 0.00")
        self.val_labour.setObjectName("fieldVal")
        g_meta.addWidget(self.val_labour, 2, 1)

        g_meta.addWidget(QLabel("Other Expenses:"), 2, 2)
        self.val_other = QLabel("₹ 0.00")
        self.val_other.setObjectName("fieldVal")
        g_meta.addWidget(self.val_other, 2, 3)

        body_layout.addWidget(group_meta)

        # Tabs for Components, Scrap, and Inventory Audit Trail
        self.tabs = QTabWidget()
        self.tabs.setObjectName("detailTabs")

        # Tab 1: Recovered Components
        tab_comp = QWidget()
        l_comp = QVBoxLayout(tab_comp)
        l_comp.setContentsMargins(14, 12, 14, 12)

        self.tbl_comp = QTableWidget(0, 7)
        self.tbl_comp.setHorizontalHeaderLabels([
            "Component", "Category", "Qty", "Unit", "Weight (kg)", "Condition", "Est. Total (₹)"
        ])
        h_c = self.tbl_comp.horizontalHeader()
        h_c.setSectionResizeMode(0, QHeaderView.Stretch)
        h_c.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        h_c.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.tbl_comp.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_comp.setFixedHeight(180)
        self.tbl_comp.setObjectName("styledTable")
        l_comp.addWidget(self.tbl_comp)
        self.tabs.addTab(tab_comp, "Recovered Components")

        # Tab 2: Scrap & Waste
        tab_scrap = QWidget()
        l_scrap = QVBoxLayout(tab_scrap)
        l_scrap.setContentsMargins(14, 12, 14, 12)

        self.tbl_scrap = QTableWidget(0, 5)
        self.tbl_scrap.setHorizontalHeaderLabels([
            "Scrap Type", "Classification", "Weight (kg)", "Est. Value (₹)", "Disposal Method"
        ])
        h_s = self.tbl_scrap.horizontalHeader()
        h_s.setSectionResizeMode(0, QHeaderView.Stretch)
        h_s.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h_s.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h_s.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h_s.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tbl_scrap.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_scrap.setFixedHeight(180)
        self.tbl_scrap.setObjectName("styledTable")
        l_scrap.addWidget(self.tbl_scrap)
        self.tabs.addTab(tab_scrap, "Scrap & Waste")

        # Tab 3: Inventory Integration & Stock Movement Audit
        tab_audit = QWidget()
        l_audit = QVBoxLayout(tab_audit)
        l_audit.setContentsMargins(14, 12, 14, 12)
        l_audit.setSpacing(10)

        self.box_audit_notice = QFrame()
        self.box_audit_notice.setObjectName("noticeBox")
        box_aud_l = QVBoxLayout(self.box_audit_notice)
        box_aud_l.setContentsMargins(12, 8, 12, 8)
        self.lbl_audit_status = QLabel()
        self.lbl_audit_status.setObjectName("noticeText")
        self.lbl_audit_status.setWordWrap(True)
        box_aud_l.addWidget(self.lbl_audit_status)
        l_audit.addWidget(self.box_audit_notice)

        self.tbl_movements = QTableWidget(0, 5)
        self.tbl_movements.setHorizontalHeaderLabels([
            "Movement Date", "Movement Type", "Part Name", "Quantity", "Weight (kg)"
        ])
        h_m = self.tbl_movements.horizontalHeader()
        h_m.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h_m.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h_m.setSectionResizeMode(2, QHeaderView.Stretch)
        h_m.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h_m.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tbl_movements.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_movements.setFixedHeight(140)
        self.tbl_movements.setObjectName("styledTable")
        l_audit.addWidget(self.tbl_movements)

        self.tabs.addTab(tab_audit, "Inventory & Stock Audit")
        body_layout.addWidget(self.tabs)

        # Remarks
        group_rem = QGroupBox("Remarks & Notes")
        r_l = QVBoxLayout(group_rem)
        r_l.setContentsMargins(16, 10, 16, 12)
        self.val_remarks = QLabel("No remarks recorded.")
        self.val_remarks.setObjectName("fieldVal")
        self.val_remarks.setWordWrap(True)
        r_l.addWidget(self.val_remarks)
        body_layout.addWidget(group_rem)

        scroll.setWidget(container)
        root_layout.addWidget(scroll)

        # Footer Actions
        footer = QFrame()
        footer.setObjectName("dialogFooter")
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(24, 14, 24, 14)
        f_layout.setSpacing(12)

        self.btn_edit = QPushButton("Edit Batch")
        self.btn_edit.setObjectName("secondaryButton")
        self.btn_edit.clicked.connect(self._edit_entry)

        self.btn_reject = QPushButton("Reject Batch")
        self.btn_reject.setObjectName("dangerButton")
        self.btn_reject.clicked.connect(self._reject_entry)

        self.btn_reverse = QPushButton("Reverse Batch (Correction)")
        self.btn_reverse.setObjectName("dangerButton")
        self.btn_reverse.clicked.connect(self._reverse_entry)

        self.btn_approve = QPushButton("✔ Approve & Update Inventory")
        self.btn_approve.setObjectName("successButton")
        self.btn_approve.clicked.connect(self._approve_entry)

        self.btn_close = QPushButton("Close")
        self.btn_close.setObjectName("secondaryButton")
        self.btn_close.clicked.connect(self.accept)

        f_layout.addWidget(self.btn_edit)
        f_layout.addWidget(self.btn_reject)
        f_layout.addWidget(self.btn_reverse)
        f_layout.addStretch()
        f_layout.addWidget(self.btn_approve)
        f_layout.addWidget(self.btn_close)
        root_layout.addWidget(footer)

    def _populate_data(self):
        if not self.entry_data:
            for button in (self.btn_edit, self.btn_reject, self.btn_reverse, self.btn_approve):
                button.setVisible(False)
            return

        d = self.entry_data
        self.setWindowTitle(f"Dismantling Batch — {d['dismantling_code']} ({d['appliance_category']})")
        self.lbl_title.setText(f"Batch {d['dismantling_code']} — {d['appliance_category']}")
        self.lbl_code_badge.setText(f" {d['dismantling_code']} ")

        # Status badge
        st = d["status"]
        if st == "Approved":
            self.lbl_status_badge.setText(" APPROVED ")
            self.lbl_status_badge.setStyleSheet(
                "background: #dcfce7; color: #15803d; border: 1px solid #86efac; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )
        elif st == "Pending Approval":
            self.lbl_status_badge.setText(" PENDING APPROVAL ")
            self.lbl_status_badge.setStyleSheet(
                "background: #fef3c7; color: #b45309; border: 1px solid #fde68a; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )
        elif st == "Rejected":
            self.lbl_status_badge.setText(" REJECTED ")
            self.lbl_status_badge.setStyleSheet(
                "background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )
        elif st == "Reversed":
            self.lbl_status_badge.setText(" REVERSED ")
            self.lbl_status_badge.setStyleSheet(
                "background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )
        else:
            self.lbl_status_badge.setText(" DRAFT ")
            self.lbl_status_badge.setStyleSheet(
                "background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )

        self.lbl_sub.setText(f"Dismantling Date: {d['dismantling_date'].strftime('%d %b %Y')}  |  Recorded On: {d['created_at'].strftime('%d %b %Y, %I:%M %p')}")

        self.val_qty.setText(str(d["appliances_dismantled"]))
        self.val_input_wt.setText(f"{d['input_weight']:.2f} kg" if d['input_weight'] else "N/A")
        self.val_rec_wt.setText(f"{d['total_recovered_weight']:.2f} kg")
        self.val_scrap_wt.setText(f"{d['total_scrap_weight']:.2f} kg")

        if d["recovery_percentage"] is not None:
            self.val_rec_pct.setText(f"{d['recovery_percentage']:.1f}%")
        else:
            self.val_rec_pct.setText("N/A")

        self.val_est_val.setText(format_currency(d["estimated_recovery_value"]))

        self.val_po.setText(d["purchase_code"])
        self.val_sup.setText(d["supplier_name"])
        bm = f"{d['brand'] or ''} {d['model_number'] or ''}".strip() or "—"
        self.val_model.setText(bm)
        self.val_team.setText(d["team_leader"] or "—")
        self.val_labour.setText(format_currency(d["labour_charges"]))
        self.val_other.setText(format_currency(d["other_expenses"]))
        self.val_remarks.setText(d["remarks"] or "No remarks recorded.")

        # Populate Components
        comps = d["components"]
        self.tabs.setTabText(0, f"Recovered Components ({len(comps)})")
        self.tbl_comp.setRowCount(len(comps))
        for r, c in enumerate(comps):
            self.tbl_comp.setItem(r, 0, QTableWidgetItem(c["name"]))
            self.tbl_comp.setItem(r, 1, QTableWidgetItem(c["category"]))
            self.tbl_comp.setItem(r, 2, QTableWidgetItem(f"{c['quantity']:.2f}"))
            self.tbl_comp.setItem(r, 3, QTableWidgetItem(c["unit"]))
            self.tbl_comp.setItem(r, 4, QTableWidgetItem(f"{c['weight']:.2f}" if c['weight'] else "—"))
            self.tbl_comp.setItem(r, 5, QTableWidgetItem(c["condition"]))
            self.tbl_comp.setItem(r, 6, QTableWidgetItem(format_currency(c["estimated_total_value"])))

        # Populate Scrap
        scraps = d["scrap"]
        self.tabs.setTabText(1, f"Scrap & Waste ({len(scraps)})")
        self.tbl_scrap.setRowCount(len(scraps))
        for r, s in enumerate(scraps):
            self.tbl_scrap.setItem(r, 0, QTableWidgetItem(s["type"]))
            self.tbl_scrap.setItem(r, 1, QTableWidgetItem(s["classification"]))
            self.tbl_scrap.setItem(r, 2, QTableWidgetItem(f"{s['weight']:.2f} kg"))
            self.tbl_scrap.setItem(r, 3, QTableWidgetItem(format_currency(s["estimated_value"])))
            self.tbl_scrap.setItem(r, 4, QTableWidgetItem(s["disposal_method"] or "—"))

        # Inventory Audit Box
        if d["is_inventory_updated"] and d["status"] == "Approved":
            appr_dt = d["approved_at"].strftime("%d %b %Y, %I:%M %p") if d["approved_at"] else "—"
            self.lbl_audit_status.setText(
                f"✔ Approved by {d['approved_by'] or 'Supervisor'} on {appr_dt}. "
                "Recovered saleable components and recyclable scrap have been credited to inventory with StockMovement audit trails."
            )
            self._load_stock_movements()
        elif d["status"] == "Reversed":
            self.lbl_audit_status.setText(
                "⛔ Batch has been formally REVERSED. Reversing negative stock movements were executed and inventory counts restored."
            )
            self._load_stock_movements()
        else:
            self.lbl_audit_status.setText(
                "⏳ Status: " + d["status"] + ". Working inventory is NOT modified until formal supervisor approval."
            )
            self.tbl_movements.setRowCount(0)

        can_manage = self._has_permission("dismantling.manage")
        can_approve = self._has_permission("dismantling.approve")
        can_adjust_inventory = self._has_permission("inventory.adjust")
        can_review = st in ("Draft", "Pending Approval")
        self.btn_edit.setVisible(st == "Draft" and can_manage)
        self.btn_reject.setVisible(can_review and can_approve)
        self.btn_approve.setVisible(
            can_review and can_approve and can_adjust_inventory
        )
        self.btn_reverse.setVisible(
            st == "Approved" and can_approve and can_adjust_inventory
        )

    def _has_permission(self, permission_code: str) -> bool:
        actor_context = current_actor_context()
        if actor_context is None:
            return False
        try:
            with get_db() as db:
                require_permission(
                    actor_context, permission_code, db_session=db
                )
            return True
        except AuthorizationError:
            return False

    def _load_stock_movements(self):
        with get_db() as db:
            from database.models import StockMovement, InventoryItem
            from sqlalchemy import select
            movements = list(
                db.scalars(
                    select(StockMovement)
                    .where(StockMovement.reference_type == "DISMANTLING_ENTRY")
                    .where(StockMovement.reference_id == self.entry_id)
                    .order_by(StockMovement.id.asc())
                ).all()
            )
            self.tbl_movements.setRowCount(len(movements))
            for r, m in enumerate(movements):
                inv = db.get(InventoryItem, m.inventory_item_id)
                p_name = inv.part_name if inv else "Item"
                d_str = m.movement_date.strftime("%d-%b-%Y %I:%M") if m.movement_date else "—"
                self.tbl_movements.setItem(r, 0, QTableWidgetItem(d_str))
                self.tbl_movements.setItem(r, 1, QTableWidgetItem(m.movement_type))
                self.tbl_movements.setItem(r, 2, QTableWidgetItem(p_name))
                self.tbl_movements.setItem(r, 3, QTableWidgetItem(f"{m.quantity:.2f}"))
                self.tbl_movements.setItem(r, 4, QTableWidgetItem(f"{m.weight:.2f} kg"))

    def _edit_entry(self):
        from ui.dismantling_form_dialog import DismantlingFormDialog
        with get_db() as db:
            try:
                actor_context = current_actor_context()
                if actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                e = get_dismantling_by_id(
                    db,
                    self.entry_id,
                    actor_context=actor_context,
                )
            except AuthorizationError:
                QMessageBox.warning(
                    self,
                    "Authentication Required",
                    "Sign in with dismantling-view permission to edit this batch.",
                )
                return
            if not e:
                return
            dialog = DismantlingFormDialog(self, entry=e)
            dialog.entry_saved.connect(self._on_saved_after_edit)
            dialog.exec()

    def _on_saved_after_edit(self, entry_id: int):
        self.entry_updated.emit(entry_id)
        self._load_entry_data()
        self._populate_data()

    def _approve_entry(self):
        if not self._has_permission("dismantling.approve") or not self._has_permission(
            "inventory.adjust"
        ):
            QMessageBox.warning(
                self, "Permission Denied",
                "Dismantling approval and inventory-adjust permissions are required.",
            )
            return
        confirm = QMessageBox.question(
            self,
            "Confirm Batch Approval",
            f"Are you sure you want to approve Dismantling Batch '{self.entry_data['dismantling_code']}'?\n\n"
            "This will permanently add all recovered components and recyclable scrap into active inventory "
            "and create immutable StockMovement audit records.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if confirm == QMessageBox.Yes:
            try:
                actor_context = current_actor_context()
                if actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                self.btn_approve.setEnabled(False)
                with get_db() as db:
                    approve_dismantling_entry(
                        db,
                        self.entry_id,
                        approved_by="Factory Supervisor",
                        actor_context=actor_context,
                    )
                QMessageBox.information(
                    self,
                    "Batch Approved",
                    f"Dismantling batch '{self.entry_data['dismantling_code']}' has been approved and inventory credited."
                )
                self.entry_updated.emit(self.entry_id)
                self._load_entry_data()
                self._populate_data()
            except AuthorizationError as e:
                QMessageBox.warning(self, "Permission Denied", str(e))
                self._load_entry_data()
                self._populate_data()
            except Exception as e:
                QMessageBox.critical(self, "Approval Failed", str(e))
                self.btn_approve.setEnabled(True)

    def _reject_entry(self):
        if not self._has_permission("dismantling.approve"):
            QMessageBox.warning(
                self, "Permission Denied",
                "Dismantling approval permission is required to reject batches.",
            )
            return
        reason, ok = QInputDialog.getText(
            self,
            "Reject Dismantling Batch",
            "Please provide a reason for rejecting this dismantling entry:"
        )
        if ok and reason.strip():
            try:
                actor_context = current_actor_context()
                if actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                self.btn_reject.setEnabled(False)
                with get_db() as db:
                    reject_dismantling_entry(
                        db,
                        self.entry_id,
                        reason=reason.strip(),
                        actor_context=actor_context,
                    )
                QMessageBox.information(
                    self,
                    "Batch Rejected",
                    f"Dismantling batch '{self.entry_data['dismantling_code']}' has been rejected."
                )
                self.entry_updated.emit(self.entry_id)
                self._load_entry_data()
                self._populate_data()
            except AuthorizationError as e:
                QMessageBox.warning(self, "Permission Denied", str(e))
                self._load_entry_data()
                self._populate_data()
            except Exception as e:
                QMessageBox.critical(self, "Rejection Failed", str(e))
                self.btn_reject.setEnabled(True)

    def _reverse_entry(self):
        if not self._has_permission("dismantling.approve") or not self._has_permission(
            "inventory.adjust"
        ):
            QMessageBox.warning(
                self, "Permission Denied",
                "Dismantling approval and inventory-adjust permissions are required.",
            )
            return
        reason, ok = QInputDialog.getText(
            self,
            "Auditable Reversal Confirmation",
            "Warning: Reversing this approved batch will deduct credited components from inventory\n"
            "and restore undismantled appliance counts.\n\nEnter correction/reversal justification:"
        )
        if ok and reason.strip():
            try:
                actor_context = current_actor_context()
                if actor_context is None:
                    raise AuthorizationError("Sign in is required.")
                self.btn_reverse.setEnabled(False)
                with get_db() as db:
                    reverse_approved_dismantling_entry(
                        db,
                        self.entry_id,
                        reason=reason.strip(),
                        actor_context=actor_context,
                    )
                QMessageBox.information(
                    self,
                    "Batch Reversed",
                    f"Batch '{self.entry_data['dismantling_code']}' has been reversed and stock corrected."
                )
                self.entry_updated.emit(self.entry_id)
                self._load_entry_data()
                self._populate_data()
            except AuthorizationError as e:
                QMessageBox.warning(self, "Permission Denied", str(e))
                self._load_entry_data()
                self._populate_data()
            except Exception as e:
                QMessageBox.critical(self, "Reversal Failed", str(e))
                self.btn_reverse.setEnabled(True)
