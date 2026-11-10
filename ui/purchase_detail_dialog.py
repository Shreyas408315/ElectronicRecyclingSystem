import os
from pathlib import Path
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox,
    QWidget, QFrame, QMessageBox, QFileDialog, QScrollArea, QGridLayout
)
from database.connection import get_db
from database.models import Purchase
from services.purchase_service import get_purchase_by_id
from services.security_service import AuthorizationError, current_actor_context
from services.pdf_receipt_service import generate_purchase_receipt_pdf
from utils.validators import format_currency


class PurchaseDetailDialog(QDialog):
    """Dialog showing detailed inward purchase voucher, items, freight breakdown, and PDF export."""

    def __init__(self, parent=None, purchase_id: int = 0):
        super().__init__(parent)
        self.purchase_id = purchase_id
        self.purchase_data: dict | None = None

        self.resize(880, 720)
        self.setMinimumSize(780, 600)
        self.setModal(True)

        self._load_purchase_data()
        self._init_ui()
        self._populate_data()

    def _load_purchase_data(self):
        try:
            with get_db() as db:
                p = get_purchase_by_id(
                    db, self.purchase_id, actor_context=current_actor_context()
                )
                if not p:
                    return
                sup = p.supplier
                self.purchase_data = {
                    "id": p.id,
                    "purchase_code": p.purchase_code,
                    "purchase_date": p.purchase_date,
                    "subtotal": p.subtotal,
                    "transportation_charges": p.transportation_charges,
                    "loading_charges": p.loading_charges,
                    "unloading_charges": p.unloading_charges,
                    "other_charges": p.other_charges,
                    "total_amount": p.total_amount,
                    "amount_paid": p.amount_paid,
                    "balance_amount": p.balance_amount,
                    "payment_method": p.payment_method,
                    "payment_status": p.payment_status,
                    "remarks": p.remarks,
                    "created_at": p.created_at,
                    "supplier": {
                        "id": sup.id,
                        "code": sup.supplier_code,
                        "name": sup.business_name,
                        "contact_person": sup.contact_person,
                        "phone": sup.phone,
                        "email": sup.email,
                        "gstin": sup.gstin,
                        "address": sup.address,
                        "city": sup.city,
                        "state": sup.state,
                        "pin_code": sup.pin_code,
                    },
                    "items": [
                        {
                            "category": item.appliance_category,
                            "brand": item.brand,
                            "model": item.model_number,
                            "quantity": item.quantity,
                            "unit": item.unit,
                            "weight": item.weight,
                            "rate": item.rate,
                            "line_total": item.line_total,
                        }
                        for item in p.items
                    ],
                }
        except AuthorizationError:
            self.purchase_data = None
            QMessageBox.warning(
                self,
                "Authentication Required",
                "Sign in with purchase-view permission to view this purchase.",
            )

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

        # Supplier Details Section
        group_sup = QGroupBox("Supplier & Inward Party")
        g_sup = QGridLayout(group_sup)
        g_sup.setContentsMargins(16, 14, 16, 14)
        g_sup.setSpacing(10)

        g_sup.addWidget(QLabel("Supplier Business:"), 0, 0)
        self.val_sup_name = QLabel("—")
        self.val_sup_name.setObjectName("fieldVal")
        g_sup.addWidget(self.val_sup_name, 0, 1)

        g_sup.addWidget(QLabel("Contact Person:"), 0, 2)
        self.val_sup_contact = QLabel("—")
        self.val_sup_contact.setObjectName("fieldVal")
        g_sup.addWidget(self.val_sup_contact, 0, 3)

        g_sup.addWidget(QLabel("Phone Number:"), 1, 0)
        self.val_sup_phone = QLabel("—")
        self.val_sup_phone.setObjectName("fieldVal")
        g_sup.addWidget(self.val_sup_phone, 1, 1)

        g_sup.addWidget(QLabel("GSTIN:"), 1, 2)
        self.val_sup_gstin = QLabel("—")
        self.val_sup_gstin.setObjectName("fieldVal")
        g_sup.addWidget(self.val_sup_gstin, 1, 3)

        g_sup.addWidget(QLabel("Address:"), 2, 0)
        self.val_sup_address = QLabel("—")
        self.val_sup_address.setObjectName("fieldVal")
        self.val_sup_address.setWordWrap(True)
        g_sup.addWidget(self.val_sup_address, 2, 1, 1, 3)

        body_layout.addWidget(group_sup)

        # Line Items Table
        group_items = QGroupBox("Appliance Line Items")
        i_layout = QVBoxLayout(group_items)
        i_layout.setContentsMargins(16, 14, 16, 14)

        self.table_items = QTableWidget(0, 8)
        self.table_items.setHorizontalHeaderLabels([
            "#", "Appliance Category", "Brand & Model", "Qty", "Unit", "Weight (kg)", "Rate (₹)", "Line Total (₹)"
        ])
        h = self.table_items.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self.table_items.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_items.setFixedHeight(180)
        self.table_items.setObjectName("styledTable")
        i_layout.addWidget(self.table_items)

        body_layout.addWidget(group_items)

        # Financial Summary & Additional Costs
        group_fin = QGroupBox("Financial Summary & Additional Expenses")
        g_fin = QGridLayout(group_fin)
        g_fin.setContentsMargins(16, 14, 16, 14)
        g_fin.setSpacing(10)

        g_fin.addWidget(QLabel("Appliance Subtotal:"), 0, 0)
        self.val_subtotal = QLabel("₹ 0.00")
        self.val_subtotal.setObjectName("fieldVal")
        g_fin.addWidget(self.val_subtotal, 0, 1)

        g_fin.addWidget(QLabel("Transportation (+):"), 0, 2)
        self.val_trans = QLabel("₹ 0.00")
        self.val_trans.setObjectName("fieldVal")
        g_fin.addWidget(self.val_trans, 0, 3)

        g_fin.addWidget(QLabel("Loading Charges (+):"), 1, 0)
        self.val_load = QLabel("₹ 0.00")
        self.val_load.setObjectName("fieldVal")
        g_fin.addWidget(self.val_load, 1, 1)

        g_fin.addWidget(QLabel("Unloading Charges (+):"), 1, 2)
        self.val_unload = QLabel("₹ 0.00")
        self.val_unload.setObjectName("fieldVal")
        g_fin.addWidget(self.val_unload, 1, 3)

        g_fin.addWidget(QLabel("Other Expenses (+):"), 2, 0)
        self.val_other = QLabel("₹ 0.00")
        self.val_other.setObjectName("fieldVal")
        g_fin.addWidget(self.val_other, 2, 1)

        g_fin.addWidget(QLabel("<b>TOTAL PURCHASE COST:</b>"), 2, 2)
        self.val_total = QLabel("₹ 0.00")
        self.val_total.setObjectName("metricValuePrimary")
        g_fin.addWidget(self.val_total, 2, 3)

        g_fin.addWidget(QLabel("Amount Paid:"), 3, 0)
        self.val_paid = QLabel("₹ 0.00")
        self.val_paid.setObjectName("fieldVal")
        g_fin.addWidget(self.val_paid, 3, 1)

        g_fin.addWidget(QLabel("<b>Remaining Balance:</b>"), 3, 2)
        self.val_balance = QLabel("₹ 0.00")
        self.val_balance.setObjectName("metricValue")
        g_fin.addWidget(self.val_balance, 3, 3)

        g_fin.addWidget(QLabel("Payment Method:"), 4, 0)
        self.val_method = QLabel("—")
        self.val_method.setObjectName("fieldVal")
        g_fin.addWidget(self.val_method, 4, 1)

        body_layout.addWidget(group_fin)

        # Remarks
        group_rem = QGroupBox("Remarks & Notes")
        r_layout = QVBoxLayout(group_rem)
        r_layout.setContentsMargins(16, 12, 16, 14)
        self.val_remarks = QLabel("No remarks recorded.")
        self.val_remarks.setObjectName("fieldVal")
        self.val_remarks.setWordWrap(True)
        r_layout.addWidget(self.val_remarks)
        body_layout.addWidget(group_rem)

        scroll.setWidget(container)
        root_layout.addWidget(scroll)

        # Footer Actions
        footer = QFrame()
        footer.setObjectName("dialogFooter")
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(24, 16, 24, 16)
        f_layout.setSpacing(12)

        self.btn_pdf = QPushButton("📄 Print Receipt (PDF)")
        self.btn_pdf.setObjectName("primaryButton")
        self.btn_pdf.clicked.connect(self._export_pdf_receipt)

        self.btn_close = QPushButton("Close")
        self.btn_close.setObjectName("secondaryButton")
        self.btn_close.clicked.connect(self.accept)

        f_layout.addWidget(self.btn_pdf)
        f_layout.addStretch()
        f_layout.addWidget(self.btn_close)
        root_layout.addWidget(footer)

    def _populate_data(self):
        if not self.purchase_data:
            return

        p = self.purchase_data
        self.setWindowTitle(f"Purchase Voucher — {p['purchase_code']}")
        self.lbl_title.setText(f"Inward Purchase {p['purchase_code']}")
        self.lbl_code_badge.setText(f" {p['purchase_code']} ")

        status = p["payment_status"]
        if status == "Paid":
            self.lbl_status_badge.setText(" PAID ")
            self.lbl_status_badge.setStyleSheet(
                "background: #dcfce7; color: #15803d; border: 1px solid #86efac; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )
        elif status == "Partially Paid":
            self.lbl_status_badge.setText(" PARTIALLY PAID ")
            self.lbl_status_badge.setStyleSheet(
                "background: #fef3c7; color: #b45309; border: 1px solid #fde68a; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )
        else:
            self.lbl_status_badge.setText(" UNPAID ")
            self.lbl_status_badge.setStyleSheet(
                "background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; border-radius: 4px; font-weight: 700; padding: 2px 8px;"
            )

        p_date_str = p["purchase_date"].strftime("%d %b %Y") if p["purchase_date"] else "—"
        self.lbl_sub.setText(f"Purchase Date: {p_date_str}  |  Recorded On: {p['created_at'].strftime('%d %b %Y, %I:%M %p')}")

        sup = p["supplier"]
        self.val_sup_name.setText(f"{sup['name']} ({sup['code']})")
        self.val_sup_contact.setText(sup["contact_person"] or "—")
        self.val_sup_phone.setText(sup["phone"] or "—")
        self.val_sup_gstin.setText(sup["gstin"] or "—")
        loc_parts = [part for part in [sup["address"], sup["city"], sup["state"], sup["pin_code"]] if part]
        self.val_sup_address.setText(", ".join(loc_parts) if loc_parts else "—")

        # Line items
        items = p["items"]
        self.table_items.setRowCount(len(items))
        for r, item in enumerate(items):
            bm = f"{item['brand'] or ''} {item['model'] or ''}".strip() or "—"
            self.table_items.setItem(r, 0, QTableWidgetItem(str(r + 1)))
            self.table_items.setItem(r, 1, QTableWidgetItem(item["category"]))
            self.table_items.setItem(r, 2, QTableWidgetItem(bm))
            self.table_items.setItem(r, 3, QTableWidgetItem(str(item["quantity"])))
            self.table_items.setItem(r, 4, QTableWidgetItem(item["unit"]))
            self.table_items.setItem(r, 5, QTableWidgetItem(f"{item['weight']:.2f}" if item["weight"] else "—"))
            self.table_items.setItem(r, 6, QTableWidgetItem(f"₹ {item['rate']:.2f}"))
            self.table_items.setItem(r, 7, QTableWidgetItem(f"₹ {item['line_total']:.2f}"))

        self.val_subtotal.setText(format_currency(p["subtotal"]))
        self.val_trans.setText(format_currency(p["transportation_charges"]))
        self.val_load.setText(format_currency(p["loading_charges"]))
        self.val_unload.setText(format_currency(p["unloading_charges"]))
        self.val_other.setText(format_currency(p["other_charges"]))
        self.val_total.setText(format_currency(p["total_amount"]))
        self.val_paid.setText(format_currency(p["amount_paid"]))
        self.val_balance.setText(format_currency(p["balance_amount"]))
        self.val_method.setText(p["payment_method"])
        self.val_remarks.setText(p["remarks"] or "No remarks provided.")

    def _export_pdf_receipt(self):
        if not self.purchase_data:
            return

        code = self.purchase_data["purchase_code"]
        suggested_filename = f"Purchase_Receipt_{code}.pdf"

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Purchase Receipt PDF",
            suggested_filename,
            "PDF Files (*.pdf)"
        )
        if not file_path:
            return

        try:
            with get_db() as db:
                p = get_purchase_by_id(
                    db, self.purchase_id, actor_context=current_actor_context()
                )
                if not p:
                    QMessageBox.warning(self, "Error", "Purchase record not found.")
                    return
                saved_path = generate_purchase_receipt_pdf(
                    db,
                    self.purchase_id,
                    file_path,
                    actor_context=current_actor_context(),
                )

            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Information)
            msg.setWindowTitle("PDF Generated")
            msg.setText(f"Purchase receipt has been saved successfully:\n\n{saved_path}")
            btn_open = msg.addButton("Open PDF", QMessageBox.ActionRole)
            btn_ok = msg.addButton(QMessageBox.Ok)
            msg.exec()

            if msg.clickedButton() == btn_open:
                QDesktopServices.openUrl(QUrl.fromLocalFile(saved_path))

        except AuthorizationError:
            QMessageBox.warning(
                self,
                "Authentication Required",
                "Sign in with purchase-view permission to access this purchase.",
            )
        except Exception as e:
            QMessageBox.critical(self, "PDF Generation Failed", f"Failed to generate receipt: {str(e)}")
