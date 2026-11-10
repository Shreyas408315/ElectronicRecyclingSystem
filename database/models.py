from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Integer,
    Index, Numeric, String, Text, UniqueConstraint, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database.connection import Base

class ApplicationSetting(Base):
    __tablename__ = "application_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=True
    )


class ApplicationSettingHistory(Base):
    __tablename__ = "application_setting_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setting_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    previous_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    name_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    employees: Mapped[list["Employee"]] = relationship("Employee", back_populates="department")


class Designation(Base):
    __tablename__ = "designations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    name_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    employees: Mapped[list["Employee"]] = relationship("Employee", back_populates="designation")


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint(
            "employment_type IN ('Full-time', 'Part-time', 'Contract', 'Temporary', 'Intern', 'Other')",
            name="ck_employees_employment_type",
        ),
        CheckConstraint(
            "employment_status IN ('Active', 'On Leave', 'Inactive', 'Separated')",
            name="ck_employees_status",
        ),
        CheckConstraint(
            "date_of_leaving IS NULL OR date_of_leaving >= date_of_joining",
            name="ck_employees_leaving_after_joining",
        ),
        Index("ix_employees_department_status", "department_id", "employment_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    designation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("designations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    department_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    employment_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    alternate_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    residential_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pin_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    date_of_joining: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    employment_status: Mapped[str] = mapped_column(
        String(20), default="Active", nullable=False, index=True
    )
    date_of_leaving: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    emergency_contact_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_photo_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    department: Mapped["Department"] = relationship("Department", back_populates="employees")
    designation: Mapped["Designation"] = relationship("Designation", back_populates="employees")
    history: Mapped[list["EmployeeHistory"]] = relationship(
        "EmployeeHistory",
        back_populates="employee",
        order_by="EmployeeHistory.created_at.desc(), EmployeeHistory.id.desc()",
        cascade="all, delete-orphan",
    )


class EmployeeHistory(Base):
    __tablename__ = "employee_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    previous_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    previous_department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    new_department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    previous_designation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    new_designation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    employee: Mapped["Employee"] = relationship("Employee", back_populates="history")


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_person: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    alternate_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pin_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    opening_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    purchases: Mapped[list["Purchase"]] = relationship(
        "Purchase", back_populates="supplier", cascade="all, delete-orphan"
    )
    supplier_payments: Mapped[list["SupplierPayment"]] = relationship(
        "SupplierPayment", back_populates="supplier"
    )
    expenses: Mapped[list["Expense"]] = relationship("Expense", back_populates="supplier")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("opening_balance >= 0", name="ck_customers_opening_balance_nonnegative"),
        CheckConstraint(
            "credit_limit IS NULL OR credit_limit >= 0",
            name="ck_customers_credit_limit_nonnegative",
        ),
        CheckConstraint(
            "custom_payment_days IS NULL OR custom_payment_days >= 0",
            name="ck_customers_custom_payment_days_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    customer_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    contact_person: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    alternate_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    gstin: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)

    billing_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    shipping_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pin_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str] = mapped_column(String(100), default="India", nullable=False)

    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00"), nullable=False
    )
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    payment_terms: Mapped[str] = mapped_column(String(20), default="Immediate", nullable=False)
    custom_payment_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preferred_payment_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    sales_orders: Mapped[list["SalesOrder"]] = relationship(
        "SalesOrder", back_populates="customer"
    )
    sales_invoices: Mapped[list["SalesInvoice"]] = relationship(
        "SalesInvoice", back_populates="customer"
    )
    customer_receipts: Mapped[list["CustomerReceipt"]] = relationship(
        "CustomerReceipt", back_populates="customer"
    )


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purchase_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True)
    purchase_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False, index=True)
    
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    transportation_charges: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    loading_charges: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    unloading_charges: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    other_charges: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    balance_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    
    payment_method: Mapped[str] = mapped_column(String(50), default="Cash", nullable=False)
    payment_status: Mapped[str] = mapped_column(String(30), default="Unpaid", nullable=False, index=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    supplier: Mapped["Supplier"] = relationship("Supplier", back_populates="purchases")
    items: Mapped[list["PurchaseItem"]] = relationship(
        "PurchaseItem", back_populates="purchase", cascade="all, delete-orphan", lazy="selectin"
    )
    dismantling_entries: Mapped[list["DismantlingEntry"]] = relationship(
        "DismantlingEntry", back_populates="purchase"
    )

class PurchaseItem(Base):
    __tablename__ = "purchase_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purchase_id: Mapped[int] = mapped_column(Integer, ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False, index=True)
    appliance_category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=True)
    unit: Mapped[str] = mapped_column(String(20), default="pieces", nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Relationships
    purchase: Mapped["Purchase"] = relationship("Purchase", back_populates="items")
    dismantling_entries: Mapped[list["DismantlingEntry"]] = relationship(
        "DismantlingEntry", back_populates="purchase_item"
    )

class DismantlingEntry(Base):
    __tablename__ = "dismantling_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dismantling_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    dismantling_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False, index=True)
    purchase_id: Mapped[int] = mapped_column(Integer, ForeignKey("purchases.id", ondelete="RESTRICT"), nullable=False, index=True)
    purchase_item_id: Mapped[int] = mapped_column(Integer, ForeignKey("purchase_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    
    appliance_category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    appliances_dismantled: Mapped[int] = mapped_column(Integer, nullable=False)
    input_weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    team_leader: Mapped[str | None] = mapped_column(String(100), nullable=True)

    labour_charges: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    other_expenses: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    total_processing_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)

    total_recovered_weight: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    total_scrap_weight: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    estimated_recovery_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    recovery_percentage: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)

    status: Mapped[str] = mapped_column(String(30), default="Draft", nullable=False, index=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_inventory_updated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    purchase: Mapped["Purchase"] = relationship("Purchase", back_populates="dismantling_entries")
    purchase_item: Mapped["PurchaseItem"] = relationship("PurchaseItem", back_populates="dismantling_entries")
    recovered_components: Mapped[list["RecoveredComponent"]] = relationship(
        "RecoveredComponent", back_populates="dismantling_entry", cascade="all, delete-orphan", lazy="selectin"
    )
    scrap_materials: Mapped[list["ScrapMaterial"]] = relationship(
        "ScrapMaterial", back_populates="dismantling_entry", cascade="all, delete-orphan", lazy="selectin"
    )

class RecoveredComponent(Base):
    __tablename__ = "recovered_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dismantling_entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("dismantling_entries.id", ondelete="CASCADE"), nullable=False, index=True)
    component_name: Mapped[str] = mapped_column(String(100), nullable=False)
    component_category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("1.00"), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default="pieces", nullable=False)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=True)
    condition: Mapped[str] = mapped_column(String(50), default="Working", nullable=False)
    estimated_unit_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    estimated_total_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    storage_location: Mapped[str | None] = mapped_column(String(100), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    inventory_item_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("inventory_items.id", ondelete="SET NULL"), nullable=True, index=True)

    # Relationships
    dismantling_entry: Mapped["DismantlingEntry"] = relationship("DismantlingEntry", back_populates="recovered_components")
    inventory_item: Mapped["InventoryItem | None"] = relationship("InventoryItem", back_populates="recovered_components")

class ScrapMaterial(Base):
    __tablename__ = "scrap_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dismantling_entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("dismantling_entries.id", ondelete="CASCADE"), nullable=False, index=True)
    scrap_type: Mapped[str] = mapped_column(String(100), nullable=False)
    material_classification: Mapped[str] = mapped_column(String(50), default="Unusable Waste", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("1.00"), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default="kg", nullable=False)
    estimated_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    disposal_method: Mapped[str | None] = mapped_column(String(100), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationship
    dismantling_entry: Mapped["DismantlingEntry"] = relationship("DismantlingEntry", back_populates="scrap_materials")

class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    part_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    part_name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(20), default="pieces", nullable=False)
    quantity_available: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    reserved_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    total_weight: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    condition: Mapped[str] = mapped_column(String(50), default="Working", nullable=False)
    cost_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    cost_price_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    estimated_selling_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    storage_location: Mapped[str | None] = mapped_column(String(100), nullable=True)
    appliance_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    storage_warehouse: Mapped[str | None] = mapped_column(String(100), nullable=True)
    storage_area: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rack_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    shelf_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    bin_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    minimum_stock_level: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    weight_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    photographs: Mapped[str | None] = mapped_column(Text, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    movements: Mapped[list["StockMovement"]] = relationship(
        "StockMovement", back_populates="inventory_item", cascade="all, delete-orphan"
    )
    recovered_components: Mapped[list["RecoveredComponent"]] = relationship(
        "RecoveredComponent", back_populates="inventory_item"
    )
    sales_order_items: Mapped[list["SalesOrderItem"]] = relationship(
        "SalesOrderItem", back_populates="inventory_item"
    )
    reservations: Mapped[list["SalesOrderReservation"]] = relationship(
        "SalesOrderReservation", back_populates="inventory_item"
    )

class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inventory_item_id: Mapped[int] = mapped_column(Integer, ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True)
    movement_type: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    reference_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reference_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    movement_date: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_responsible: Mapped[str | None] = mapped_column(String(100), nullable=True)
    previous_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    new_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    previous_weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    new_weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    weight_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    from_location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    to_location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Relationship
    inventory_item: Mapped["InventoryItem"] = relationship("InventoryItem", back_populates="movements")


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('Draft', 'Confirmed', 'Partially Fulfilled', 'Fulfilled', 'Cancelled', 'On Hold')",
            name="ck_sales_orders_valid_status",
        ),
        CheckConstraint("subtotal >= 0", name="ck_sales_orders_subtotal_nonnegative"),
        CheckConstraint("discount_amount >= 0", name="ck_sales_orders_discount_nonnegative"),
        CheckConstraint("additional_charges >= 0", name="ck_sales_orders_charges_nonnegative"),
        CheckConstraint("tax_amount >= 0", name="ck_sales_orders_tax_nonnegative"),
        CheckConstraint("grand_total >= 0", name="ck_sales_orders_total_nonnegative"),
        CheckConstraint("amount_paid IS NULL OR amount_paid >= 0", name="ck_sales_orders_amount_paid_nonnegative"),
        CheckConstraint("balance_due IS NULL OR balance_due >= 0", name="ck_sales_orders_balance_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False, index=True)
    expected_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="Draft", nullable=False, index=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    additional_charges: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    grand_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    amount_paid: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    balance_due: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    customer: Mapped["Customer"] = relationship("Customer", back_populates="sales_orders")
    items: Mapped[list["SalesOrderItem"]] = relationship(
        "SalesOrderItem", back_populates="sales_order", cascade="all, delete-orphan",
        lazy="selectin",
    )
    reservations: Mapped[list["SalesOrderReservation"]] = relationship(
        "SalesOrderReservation", back_populates="sales_order"
    )
    fulfilments: Mapped[list["SalesOrderFulfilment"]] = relationship(
        "SalesOrderFulfilment", back_populates="sales_order", lazy="selectin",
    )
    status_history: Mapped[list["SalesOrderStatusHistory"]] = relationship(
        "SalesOrderStatusHistory", back_populates="sales_order"
    )
    sales_invoices: Mapped[list["SalesInvoice"]] = relationship(
        "SalesInvoice", back_populates="sales_order"
    )


class SalesOrderItem(Base):
    __tablename__ = "sales_order_items"
    __table_args__ = (
        CheckConstraint("quantity_ordered > 0", name="ck_sales_order_items_quantity_positive"),
        CheckConstraint("quantity_fulfilled >= 0", name="ck_sales_order_items_fulfilled_nonnegative"),
        CheckConstraint(
            "quantity_fulfilled <= quantity_ordered",
            name="ck_sales_order_items_fulfilled_lte_ordered",
        ),
        CheckConstraint("unit_selling_price >= 0", name="ck_sales_order_items_price_nonnegative"),
        CheckConstraint("discount >= 0", name="ck_sales_order_items_discount_nonnegative"),
        CheckConstraint("tax_rate >= 0", name="ck_sales_order_items_tax_rate_nonnegative"),
        CheckConstraint("line_subtotal >= 0", name="ck_sales_order_items_subtotal_nonnegative"),
        CheckConstraint("line_total >= 0", name="ck_sales_order_items_total_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sales_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inventory_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("inventory_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_name_snapshot: Mapped[str] = mapped_column(String(150), nullable=False)
    part_code_snapshot: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    quantity_ordered: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity_fulfilled: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    unit_selling_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0.000"), nullable=False)
    line_subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sales_order: Mapped["SalesOrder"] = relationship("SalesOrder", back_populates="items")
    inventory_item: Mapped["InventoryItem"] = relationship("InventoryItem", back_populates="sales_order_items")
    reservations: Mapped[list["SalesOrderReservation"]] = relationship(
        "SalesOrderReservation", back_populates="sales_order_item"
    )
    fulfilment_items: Mapped[list["SalesOrderFulfilmentItem"]] = relationship(
        "SalesOrderFulfilmentItem", back_populates="sales_order_item"
    )


class SalesOrderReservation(Base):
    __tablename__ = "sales_order_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_sales_order_reservations_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sales_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sales_order_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    inventory_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("inventory_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    product_name_snapshot: Mapped[str] = mapped_column(String(150), nullable=False)
    part_code_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    unit_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    fulfilment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sales_order_fulfilments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    sales_order: Mapped["SalesOrder"] = relationship("SalesOrder", back_populates="reservations")
    sales_order_item: Mapped["SalesOrderItem | None"] = relationship(
        "SalesOrderItem", back_populates="reservations"
    )
    inventory_item: Mapped["InventoryItem"] = relationship("InventoryItem", back_populates="reservations")


class SalesOrderFulfilment(Base):
    __tablename__ = "sales_order_fulfilments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fulfilment_code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    sales_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    fulfilment_date: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False, index=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    sales_order: Mapped["SalesOrder"] = relationship("SalesOrder", back_populates="fulfilments")
    items: Mapped[list["SalesOrderFulfilmentItem"]] = relationship(
        "SalesOrderFulfilmentItem", back_populates="fulfilment", cascade="all, delete-orphan",
        lazy="selectin",
    )


class SalesOrderFulfilmentItem(Base):
    __tablename__ = "sales_order_fulfilment_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_sales_order_fulfilment_items_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fulfilment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_order_fulfilments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sales_order_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    inventory_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("inventory_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    invoiced_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=True
    )
    unit_cost_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    accounting_cost_status: Mapped[str | None] = mapped_column(
        String(30), default="Pending Review", nullable=True
    )
    fulfilment: Mapped["SalesOrderFulfilment"] = relationship(
        "SalesOrderFulfilment", back_populates="items"
    )
    sales_order_item: Mapped["SalesOrderItem"] = relationship(
        "SalesOrderItem", back_populates="fulfilment_items"
    )
    inventory_item: Mapped["InventoryItem"] = relationship("InventoryItem")


class SalesOrderStatusHistory(Base):
    __tablename__ = "sales_order_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sales_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    previous_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    sales_order: Mapped["SalesOrder"] = relationship("SalesOrder", back_populates="status_history")


class SalesInvoice(Base):
    __tablename__ = "sales_invoices"
    __table_args__ = (
        CheckConstraint(
            "status IN ('Draft', 'Issued', 'Partially Paid', 'Paid', 'Overdue', 'Cancelled', 'Void')",
            name="ck_sales_invoices_valid_status",
        ),
        CheckConstraint("subtotal >= 0", name="ck_sales_invoices_subtotal_nonnegative"),
        CheckConstraint("discount_amount >= 0", name="ck_sales_invoices_discount_nonnegative"),
        CheckConstraint("additional_charges >= 0", name="ck_sales_invoices_charges_nonnegative"),
        CheckConstraint("taxable_amount >= 0", name="ck_sales_invoices_taxable_nonnegative"),
        CheckConstraint("tax_amount >= 0", name="ck_sales_invoices_tax_nonnegative"),
        CheckConstraint("grand_total >= 0", name="ck_sales_invoices_total_nonnegative"),
        CheckConstraint(
            "amount_paid IS NULL OR amount_paid >= 0",
            name="ck_sales_invoices_paid_nonnegative",
        ),
        CheckConstraint(
            "balance_due IS NULL OR balance_due >= 0",
            name="ck_sales_invoices_balance_nonnegative",
        ),
        CheckConstraint(
            "tax_treatment IN ('NO_GST', 'INTRA_STATE_GST', 'INTER_STATE_GST')",
            name="ck_sales_invoices_tax_treatment",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sales_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="Draft", nullable=False, index=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    additional_charges: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    taxable_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    cgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    sgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    igst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    grand_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    amount_paid: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    balance_due: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    tax_treatment: Mapped[str] = mapped_column(String(30), default="NO_GST", nullable=False)
    factory_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    factory_address_snapshot: Mapped[str | None] = mapped_column(String(500), nullable=True)
    factory_gstin_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    factory_state_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    customer_code_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    customer_contact_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_phone_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    customer_gstin_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    billing_address_snapshot: Mapped[str | None] = mapped_column(String(500), nullable=True)
    shipping_address_snapshot: Mapped[str | None] = mapped_column(String(500), nullable=True)
    customer_state_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_pin_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    customer_country_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms_and_conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    customer: Mapped["Customer"] = relationship("Customer", back_populates="sales_invoices")
    sales_order: Mapped["SalesOrder"] = relationship("SalesOrder", back_populates="sales_invoices")
    items: Mapped[list["SalesInvoiceItem"]] = relationship(
        "SalesInvoiceItem", back_populates="invoice", lazy="selectin"
    )
    history: Mapped[list["SalesInvoiceHistory"]] = relationship(
        "SalesInvoiceHistory", back_populates="invoice", lazy="selectin"
    )


class SalesInvoiceItem(Base):
    __tablename__ = "sales_invoice_items"
    __table_args__ = (
        UniqueConstraint(
            "invoice_id", "fulfilment_item_id",
            name="uq_sales_invoice_items_invoice_fulfilment",
        ),
        CheckConstraint("quantity > 0", name="ck_sales_invoice_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_sales_invoice_items_price_nonnegative"),
        CheckConstraint("discount >= 0", name="ck_sales_invoice_items_discount_nonnegative"),
        CheckConstraint(
            "order_discount_amount >= 0",
            name="ck_sales_invoice_items_order_discount_nonnegative",
        ),
        CheckConstraint("tax_rate >= 0", name="ck_sales_invoice_items_tax_rate_nonnegative"),
        CheckConstraint("taxable_amount >= 0", name="ck_sales_invoice_items_taxable_nonnegative"),
        CheckConstraint("tax_amount >= 0", name="ck_sales_invoice_items_tax_nonnegative"),
        CheckConstraint("line_total >= 0", name="ck_sales_invoice_items_total_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    inventory_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("inventory_items.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    sales_order_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    fulfilment_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_order_fulfilment_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_name_snapshot: Mapped[str] = mapped_column(String(150), nullable=False)
    part_code_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hsn_sac_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    order_discount_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00"), nullable=True
    )
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0.000"), nullable=False)
    taxable_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    cgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    sgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    igst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    invoice: Mapped["SalesInvoice"] = relationship("SalesInvoice", back_populates="items")
    inventory_item: Mapped["InventoryItem | None"] = relationship("InventoryItem")
    sales_order_item: Mapped["SalesOrderItem"] = relationship("SalesOrderItem")
    fulfilment_item: Mapped["SalesOrderFulfilmentItem"] = relationship("SalesOrderFulfilmentItem")


class SalesInvoiceHistory(Base):
    __tablename__ = "sales_invoice_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sales_invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    invoice: Mapped["SalesInvoice"] = relationship("SalesInvoice", back_populates="history")


PAYMENT_METHOD_VALUES = (
    "Cash", "Bank Transfer", "UPI", "Cheque", "NEFT", "RTGS", "IMPS", "Other"
)


class CustomerReceipt(Base):
    __tablename__ = "customer_receipts"
    __table_args__ = (
        CheckConstraint("amount_received > 0", name="ck_customer_receipts_amount_positive"),
        CheckConstraint(
            "payment_method IN ('Cash', 'Bank Transfer', 'UPI', 'Cheque', 'NEFT', 'RTGS', 'IMPS', 'Other')",
            name="ck_customer_receipts_payment_method",
        ),
        CheckConstraint(
            "status IN ('Draft', 'Posted', 'Voided')",
            name="ck_customer_receipts_status",
        ),
        CheckConstraint(
            "status != 'Voided' OR (void_reason IS NOT NULL AND voided_at IS NOT NULL)",
            name="ck_customer_receipts_void_audit",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    receipt_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount_received: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False)
    transaction_reference: Mapped[str | None] = mapped_column(
        String(100), index=True, nullable=True
    )
    bank_account_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    cheque_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Draft", nullable=False, index=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    customer: Mapped["Customer"] = relationship("Customer", back_populates="customer_receipts")
    allocations: Mapped[list["CustomerReceiptAllocation"]] = relationship(
        "CustomerReceiptAllocation", back_populates="receipt",
        order_by="CustomerReceiptAllocation.id",
    )


class CustomerReceiptAllocation(Base):
    __tablename__ = "customer_receipt_allocations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_customer_receipt_allocations_amount_positive"),
        CheckConstraint(
            "(allocation_type = 'Invoice' AND invoice_id IS NOT NULL) OR "
            "(allocation_type = 'Opening Balance' AND invoice_id IS NULL)",
            name="ck_customer_receipt_allocations_target",
        ),
        CheckConstraint(
            "allocation_source IN ('Posting', 'Advance Application')",
            name="ck_customer_receipt_allocations_source",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    receipt_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customer_receipts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invoice_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sales_invoices.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    allocation_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    allocation_source: Mapped[str] = mapped_column(String(30), default="Posting", nullable=False)
    allocation_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    receipt: Mapped["CustomerReceipt"] = relationship(
        "CustomerReceipt", back_populates="allocations"
    )


class SupplierPayment(Base):
    __tablename__ = "supplier_payments"
    __table_args__ = (
        CheckConstraint("amount_paid > 0", name="ck_supplier_payments_amount_positive"),
        CheckConstraint(
            "payment_method IN ('Cash', 'Bank Transfer', 'UPI', 'Cheque', 'NEFT', 'RTGS', 'IMPS', 'Other')",
            name="ck_supplier_payments_payment_method",
        ),
        CheckConstraint(
            "status IN ('Draft', 'Posted', 'Voided')",
            name="ck_supplier_payments_status",
        ),
        CheckConstraint(
            "status != 'Voided' OR (void_reason IS NOT NULL AND voided_at IS NOT NULL)",
            name="ck_supplier_payments_void_audit",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False)
    transaction_reference: Mapped[str | None] = mapped_column(
        String(100), index=True, nullable=True
    )
    bank_account_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    cheque_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Draft", nullable=False, index=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    supplier: Mapped["Supplier"] = relationship("Supplier", back_populates="supplier_payments")
    allocations: Mapped[list["SupplierPaymentAllocation"]] = relationship(
        "SupplierPaymentAllocation", back_populates="payment",
        order_by="SupplierPaymentAllocation.id",
    )


class SupplierPaymentAllocation(Base):
    __tablename__ = "supplier_payment_allocations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_supplier_payment_allocations_amount_positive"),
        CheckConstraint(
            "(allocation_type = 'Purchase' AND purchase_id IS NOT NULL) OR "
            "(allocation_type = 'Opening Balance' AND purchase_id IS NULL)",
            name="ck_supplier_payment_allocations_target",
        ),
        CheckConstraint(
            "allocation_source IN ('Posting', 'Advance Application')",
            name="ck_supplier_payment_allocations_source",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("supplier_payments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    purchase_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("purchases.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    allocation_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    allocation_source: Mapped[str] = mapped_column(String(30), default="Posting", nullable=False)
    allocation_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    payment: Mapped["SupplierPayment"] = relationship(
        "SupplierPayment", back_populates="allocations"
    )


class CustomerReceiptHistory(Base):
    __tablename__ = "customer_receipt_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    receipt_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customer_receipts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class SupplierPaymentHistory(Base):
    __tablename__ = "supplier_payment_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("supplier_payments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class PaymentTransactionReferenceScope(Base):
    __tablename__ = "payment_transaction_reference_scopes"
    __table_args__ = (
        CheckConstraint(
            "transaction_kind IN ('Customer Receipt', 'Supplier Payment')",
            name="ck_payment_reference_scopes_kind",
        ),
        CheckConstraint(
            "payment_method IN ('Cash', 'Bank Transfer', 'UPI', 'Cheque', 'NEFT', 'RTGS', 'IMPS', 'Other')",
            name="ck_payment_reference_scopes_method",
        ),
    )

    transaction_reference: Mapped[str] = mapped_column(String(100), primary_key=True)
    transaction_kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    payment_method: Mapped[str] = mapped_column(String(30), primary_key=True)
    account_scope: Mapped[str] = mapped_column(String(150), primary_key=True, default="")
    transaction_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    expenses: Mapped[list["Expense"]] = relationship("Expense", back_populates="category")


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("subtotal >= 0", name="ck_expenses_subtotal_nonnegative"),
        CheckConstraint("discount >= 0", name="ck_expenses_discount_nonnegative"),
        CheckConstraint("additional_charges >= 0", name="ck_expenses_charges_nonnegative"),
        CheckConstraint("tax_amount >= 0", name="ck_expenses_tax_nonnegative"),
        CheckConstraint("total_amount >= 0", name="ck_expenses_total_nonnegative"),
        CheckConstraint("taxable_amount >= 0", name="ck_expenses_taxable_nonnegative"),
        CheckConstraint(
            "status IN ('Draft', 'Posted', 'Voided')", name="ck_expenses_status"
        ),
        CheckConstraint(
            "status != 'Voided' OR (void_reason IS NOT NULL AND voided_at IS NOT NULL)",
            name="ck_expenses_void_audit",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    expense_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_categories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payee_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    supplier_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    vendor_reference: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    additional_charges: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00"), nullable=False
    )
    taxable_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attachment_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Draft", nullable=False, index=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    category: Mapped["ExpenseCategory"] = relationship("ExpenseCategory", back_populates="expenses")
    supplier: Mapped["Supplier | None"] = relationship("Supplier", back_populates="expenses")
    items: Mapped[list["ExpenseItem"]] = relationship(
        "ExpenseItem", back_populates="expense", cascade="all, delete-orphan",
        order_by="ExpenseItem.id",
    )
    payments: Mapped[list["ExpensePayment"]] = relationship(
        "ExpensePayment", back_populates="expense", order_by="ExpensePayment.id",
    )
    history: Mapped[list["ExpenseHistory"]] = relationship(
        "ExpenseHistory", back_populates="expense", order_by="ExpenseHistory.id",
    )


class ExpenseItem(Base):
    __tablename__ = "expense_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_expense_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_expense_items_unit_price_nonnegative"),
        CheckConstraint("discount >= 0", name="ck_expense_items_discount_nonnegative"),
        CheckConstraint("tax_rate >= 0", name="ck_expense_items_tax_rate_nonnegative"),
        CheckConstraint("line_subtotal >= 0", name="ck_expense_items_subtotal_nonnegative"),
        CheckConstraint("line_total >= 0", name="ck_expense_items_total_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    expense_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    category_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), default="unit", nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0.0000"), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"), nullable=False)
    line_subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    expense: Mapped["Expense"] = relationship("Expense", back_populates="items")


class ExpenseHistory(Base):
    __tablename__ = "expense_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    expense_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expenses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    expense: Mapped["Expense"] = relationship("Expense", back_populates="history")


class ExpensePayment(Base):
    __tablename__ = "expense_payments"
    __table_args__ = (
        CheckConstraint("amount_paid > 0", name="ck_expense_payments_amount_positive"),
        CheckConstraint(
            "payment_method IN ('Cash', 'Bank Transfer', 'UPI', 'Cheque', 'NEFT', 'RTGS', 'IMPS', 'Other')",
            name="ck_expense_payments_method",
        ),
        CheckConstraint(
            "status IN ('Draft', 'Posted', 'Voided')", name="ck_expense_payments_status"
        ),
        CheckConstraint(
            "status != 'Voided' OR (void_reason IS NOT NULL AND voided_at IS NOT NULL)",
            name="ck_expense_payments_void_audit",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    expense_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expenses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False)
    transaction_reference: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    bank_account_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    cheque_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Draft", nullable=False, index=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    expense: Mapped["Expense"] = relationship("Expense", back_populates="payments")
    history: Mapped[list["ExpensePaymentHistory"]] = relationship(
        "ExpensePaymentHistory", back_populates="payment", order_by="ExpensePaymentHistory.id",
    )


class ExpensePaymentHistory(Base):
    __tablename__ = "expense_payment_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("expense_payments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    payment: Mapped["ExpensePayment"] = relationship("ExpensePayment", back_populates="history")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('Assets', 'Liabilities', 'Equity', 'Income', 'Expenses')",
            name="ck_accounts_type",
        ),
        CheckConstraint(
            "normal_balance IN ('Debit', 'Credit')", name="ck_accounts_normal_balance"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    account_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    subtype: Mapped[str | None] = mapped_column(String(60), nullable=True)
    normal_balance: Mapped[str] = mapped_column(String(10), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    system_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    parent: Mapped["Account | None"] = relationship(
        "Account", remote_side="Account.id", back_populates="children"
    )
    children: Mapped[list["Account"]] = relationship("Account", back_populates="parent")


class AccountMapping(Base):
    __tablename__ = "account_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mapping_key: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    account: Mapped["Account"] = relationship("Account")


class AccountingPeriod(Base):
    __tablename__ = "accounting_periods"
    __table_args__ = (
        CheckConstraint("status IN ('Open', 'Closed')", name="ck_accounting_periods_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    period_name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="Open", nullable=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )


class AccountingPeriodHistory(Base):
    __tablename__ = "accounting_period_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounting_periods.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    previous_status: Mapped[str] = mapped_column(String(10), nullable=False)
    new_status: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('Draft', 'Posted', 'Reversed')", name="ck_journal_entries_status"
        ),
        CheckConstraint("total_debit >= 0", name="ck_journal_entries_debit_nonnegative"),
        CheckConstraint("total_credit >= 0", name="ck_journal_entries_credit_nonnegative"),
        UniqueConstraint(
            "source_module", "source_transaction_id", "source_event_type",
            name="uq_journal_entries_source_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_number: Mapped[str] = mapped_column(
        String(30), unique=True, nullable=False, index=True
    )
    journal_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reference_number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    source_module: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_transaction_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="Draft", nullable=False, index=True)
    total_debit: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0.00"), nullable=False)
    total_credit: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0.00"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reversal_of_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("journal_entries.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    lines: Mapped[list["JournalLine"]] = relationship(
        "JournalLine", back_populates="journal", cascade="all, delete-orphan",
        order_by="JournalLine.id",
    )
    history: Mapped[list["JournalHistory"]] = relationship(
        "JournalHistory", back_populates="journal", order_by="JournalHistory.id",
    )


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0", name="ck_journal_lines_debit_nonnegative"),
        CheckConstraint("credit >= 0", name="ck_journal_lines_credit_nonnegative"),
        CheckConstraint(
            "(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)",
            name="ck_journal_lines_one_sided_positive",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    debit: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0.00"), nullable=False)
    credit: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0.00"), nullable=False)
    party_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    party_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    journal: Mapped["JournalEntry"] = relationship("JournalEntry", back_populates="lines")
    account: Mapped["Account"] = relationship("Account")


class JournalHistory(Base):
    __tablename__ = "journal_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("journal_entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    previous_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    journal: Mapped["JournalEntry"] = relationship("JournalEntry", back_populates="history")


class AccountingException(Base):
    __tablename__ = "accounting_exceptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('Open', 'Resolved')", name="ck_accounting_exceptions_status"
        ),
        UniqueConstraint(
            "source_module", "source_transaction_id", "source_event_type",
            name="uq_accounting_exceptions_source_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_module: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_transaction_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="Open", nullable=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PaymentAccount(Base):
    __tablename__ = "payment_accounts"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('Cash', 'Bank', 'UPI Settlement', 'Cheque Clearing', 'Other')",
            name="ck_payment_accounts_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    account_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    bank_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    account_holder: Mapped[str | None] = mapped_column(String(120), nullable=True)
    account_identifier: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ifsc: Mapped[str | None] = mapped_column(String(20), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(100), nullable=True)
    account_mapping_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("account_mappings.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
    account_mapping: Mapped["AccountMapping | None"] = relationship("AccountMapping")


class ManagedDocument(Base):
    __tablename__ = "managed_documents"
    __table_args__ = (
        CheckConstraint("file_size >= 0", name="ck_managed_documents_size_nonnegative"),
        CheckConstraint(
            "status IN ('Active', 'Archived')", name="ck_managed_documents_status"
        ),
        Index("ix_managed_documents_entity", "related_entity_type", "related_entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    file_extension: Mapped[str] = mapped_column(String(15), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    related_entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    related_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="Active", nullable=False, index=True)
    archive_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BackupRecord(Base):
    __tablename__ = "backup_records"
    __table_args__ = (
        CheckConstraint(
            "verification_status IN ('Verified', 'Failed')",
            name="ck_backup_records_verification_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    backup_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    integrity_result: Mapped[str] = mapped_column(String(20), nullable=False)
    schema_compatible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    verification_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False, index=True)


# Security tables are deliberately additive.  Historical business records keep
# their existing responsible_user text; new audit events reference a real user.
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_permission"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True)
    permission_id: Mapped[int] = mapped_column(Integer, ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False, index=True)


class UserAuditEvent(Base):
    __tablename__ = "user_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    source_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    before_values: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_values: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False, index=True)
