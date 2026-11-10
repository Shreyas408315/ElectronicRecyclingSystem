import pytest
from PySide6.QtWidgets import QApplication
from database.connection import get_db
import database.connection as database_connection
from database.models import Supplier, ApplicationSetting
from services.supplier_service import (
    SupplierValidationError,
    generate_next_supplier_code,
    create_supplier,
    update_supplier,
    deactivate_supplier,
    reactivate_supplier,
    get_supplier_by_id,
    list_suppliers,
)
from ui.supplier_form_dialog import SupplierFormDialog
from ui.supplier_detail_dialog import SupplierDetailDialog
from ui.supplier_page import SupplierPage
from ui.main_window import MainWindow
from services.security_service import authenticate, create_first_administrator

@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_full_supplier_workflow_and_persistence(qapp):
    database_connection.initialize_database()

    with get_db() as db:
        create_first_administrator(
            db, username="e2e-admin", password="E2e-admin-password-2026!", full_name="E2E Administrator"
        )
    with get_db() as db:
        actor_context = authenticate(db, "e2e-admin", "E2e-admin-password-2026!")

    # 1. Verify existing table / settings remain intact
    with get_db() as db:
        # Check application_settings exists and can be written/read without issue
        existing_setting = db.get(ApplicationSetting, "company_name")
        if not existing_setting:
            db.add(ApplicationSetting(key="company_name", value="E-Recycle Corp"))
            db.commit()

    # 2. Add Supplier 1 with auto-generated code
    with get_db() as db:
        code1 = generate_next_supplier_code(db, actor_context=actor_context)
        s1 = create_supplier(db, {
            "supplier_code": code1,
            "business_name": "E-Scrap Solutions Pvt Ltd",
            "contact_person": "Ramesh Kumar",
            "phone": "9820012345",
            "alternate_phone": "02228881234",
            "email": "ramesh@escrapsolutions.com",
            "address": "Plot 42, MIDC Industrial Area",
            "city": "Navi Mumbai",
            "state": "Maharashtra",
            "pin_code": "400705",
            "gstin": "27AABCE1234F1Z9",
            "opening_balance": 50000.0,
            "remarks": "Supplies shredded motherboard e-waste",
        }, actor_context=actor_context)
        s1_id = s1.id
        s1_code = s1.supplier_code

    assert s1_code.startswith("SUP-")

    # 3. Add Supplier 2 with auto-generated code
    with get_db() as db:
        code2 = generate_next_supplier_code(db, actor_context=actor_context)
        assert code2 != s1_code
        s2 = create_supplier(db, {
            "supplier_code": code2,
            "business_name": "Green Circuit Recyclers",
            "contact_person": "Sita Raman",
            "phone": "9820098765",
            "email": "sita@greencircuit.in",
            "city": "Bengaluru",
            "state": "Karnataka",
            "opening_balance": 12500.0,
        }, actor_context=actor_context)
        s2_id = s2.id

    # 4. Search suppliers
    with get_db() as db:
        # By Name
        res = list_suppliers(db, search="E-Scrap", actor_context=actor_context)
        assert any(s.id == s1_id for s in res)

        # By Phone
        res = list_suppliers(db, search="9820098765", actor_context=actor_context)
        assert any(s.id == s2_id for s in res)

        # By Contact Person
        res = list_suppliers(db, search="Ramesh Kumar", actor_context=actor_context)
        assert any(s.id == s1_id for s in res)

    # 5. Edit Supplier Details
    with get_db() as db:
        update_supplier(db, s1_id, {
            "business_name": "E-Scrap Solutions International",
            "contact_person": "Ramesh Kumar (Director)",
            "phone": "9820012345",
            "opening_balance": 55000.0,
        }, actor_context=actor_context)
    with get_db() as db:
        updated_s1 = get_supplier_by_id(db, s1_id, actor_context=actor_context)
        assert updated_s1.business_name == "E-Scrap Solutions International"
        assert updated_s1.contact_person == "Ramesh Kumar (Director)"
        assert updated_s1.opening_balance == 55000.0
        assert updated_s1.supplier_code == s1_code  # Unchanged!

    # 6. Deactivate Supplier
    with get_db() as db:
        deactivate_supplier(db, s1_id, actor_context=actor_context)
    with get_db() as db:
        deactivated_s1 = get_supplier_by_id(db, s1_id, actor_context=actor_context)
        assert deactivated_s1.is_active is False

    # 7. Reactivate Supplier
    with get_db() as db:
        reactivate_supplier(db, s1_id, actor_context=actor_context)
    with get_db() as db:
        reactivated_s1 = get_supplier_by_id(db, s1_id, actor_context=actor_context)
        assert reactivated_s1.is_active is True

    # 8. Validate duplicate and invalid entries
    with get_db() as db:
        # Duplicate supplier_code
        with pytest.raises(SupplierValidationError) as exc:
            create_supplier(db, {
                "supplier_code": s1_code,
                "business_name": "Duplicate Tester",
            }, actor_context=actor_context)
        assert "already exists" in str(exc.value)

        # Empty name
        with pytest.raises(SupplierValidationError):
            create_supplier(db, {"business_name": "   "}, actor_context=actor_context)

        # Bad email
        with pytest.raises(SupplierValidationError):
            create_supplier(db, {"business_name": "Bad Email Corp", "email": "bad@"}, actor_context=actor_context)

        # Bad phone
        with pytest.raises(SupplierValidationError):
            create_supplier(db, {"business_name": "Bad Phone Corp", "phone": "123"}, actor_context=actor_context)

        # Bad GSTIN
        with pytest.raises(SupplierValidationError):
            create_supplier(db, {"business_name": "Bad GSTIN Corp", "gstin": "INVALID"}, actor_context=actor_context)

    # 9. Verify persistence across fresh database sessions (simulating application restart)
    new_session = database_connection.SessionLocal()
    try:
        reloaded_s1 = new_session.get(Supplier, s1_id)
        assert reloaded_s1 is not None
        assert reloaded_s1.business_name == "E-Scrap Solutions International"
        assert reloaded_s1.supplier_code == s1_code

        reloaded_setting = new_session.get(ApplicationSetting, "company_name")
        assert reloaded_setting is not None
        assert reloaded_setting.value == "E-Recycle Corp"
    finally:
        new_session.close()
