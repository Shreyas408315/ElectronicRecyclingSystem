import pytest
from decimal import Decimal
from PySide6.QtWidgets import QApplication
from database.connection import get_db
import database.connection as database_connection
from database.models import Supplier, Purchase, PurchaseItem, ApplicationSetting
from services.supplier_service import create_supplier
from services.purchase_service import (
    create_purchase,
    get_purchase_by_id,
    get_supplier_purchase_ledger_balance,
)

@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_e2e_purchase_lifecycle_and_persistence(qapp, authenticated_actor):
    database_connection.initialize_database()

    # 1. Register active supplier
    with get_db() as db:
        sup = create_supplier(db, {
            "business_name": "EcoSource Logistics & Recycling",
            "contact_person": "Harish Patel",
            "phone": "9820099887",
            "opening_balance": 5000.0,
            "is_active": True,
        }, actor_context=authenticated_actor)
        sup_id = sup.id

    # 2. Record purchase with multiple appliances
    with get_db() as db:
        p = create_purchase(
            db,
            {
                "supplier_id": sup_id,
                "transportation_charges": Decimal("400.00"),
                "loading_charges": Decimal("100.00"),
                "amount_paid": Decimal("3000.00"),
                "payment_method": "Bank Transfer / NEFT",
                "remarks": "E2E incoming scrap batch with refrigerators & washing machines",
            },
            [
                {"appliance_category": "Refrigerators", "brand": "Whirlpool", "quantity": 3, "rate": Decimal("1500.00")},
                {"appliance_category": "Washing Machines", "brand": "Godrej", "quantity": 2, "rate": Decimal("1000.00")},
            ],
            actor_context=authenticated_actor,
        )
        p_id = p.id
        p_code = p.purchase_code

    # 3. Simulate application restart with a fresh database session
    new_session = database_connection.SessionLocal()
    try:
        reloaded_p = get_purchase_by_id(
            new_session, p_id, actor_context=authenticated_actor
        )
        assert reloaded_p is not None
        assert reloaded_p.purchase_code == p_code
        assert reloaded_p.supplier.business_name == "EcoSource Logistics & Recycling"
        assert len(reloaded_p.items) == 2

        # Verify calculations:
        # Items: (3 * 1500) + (2 * 1000) = 4500 + 2000 = 6500.00
        # Charges: 400 + 100 = 500.00
        # Total: 7000.00
        # Paid: 3000.00
        # Balance: 4000.00
        assert reloaded_p.subtotal == Decimal("6500.00")
        assert reloaded_p.total_amount == Decimal("7000.00")
        assert reloaded_p.amount_paid == Decimal("3000.00")
        assert reloaded_p.balance_amount == Decimal("4000.00")
        assert reloaded_p.payment_status == "Partially Paid"

        # Verify Supplier Ledger Outstanding Calculation:
        # Opening balance (5000.00) + Unpaid purchase balance (4000.00) = 9000.00
        out_bal, p_list = get_supplier_purchase_ledger_balance(
            new_session, sup_id, actor_context=authenticated_actor
        )
        assert out_bal == Decimal("9000.00")
        assert len(p_list) == 1
        assert p_list[0].id == p_id

        # Verify Critical Inventory Separation:
        # Purchasing raw appliances does NOT create or touch dismantled component inventory tables
        # The database only contains incoming raw appliances in purchases and purchase_items
        from sqlalchemy import inspect
        from database.connection import engine
        table_names = inspect(engine).get_table_names()
        assert "purchases" in table_names
        assert "purchase_items" in table_names
        assert "inventory" not in table_names  # Inventory separation verified!

    finally:
        new_session.close()
