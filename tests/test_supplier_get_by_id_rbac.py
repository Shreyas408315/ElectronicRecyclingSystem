import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import Supplier, UserAuditEvent
from services.security_service import AuthorizationError, authenticate, create_first_administrator, create_user, logout, _seed_security_catalogue
from services.supplier_service import get_supplier_by_code, get_supplier_by_id, get_supplier_metrics, list_suppliers, reactivate_supplier


def test_get_supplier_by_id_requires_trusted_view_actor_across_sessions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    auth_session = Session()
    _seed_security_catalogue(auth_session)
    admin = create_first_administrator(auth_session, username="admin", password="Vertical-password-2026!", full_name="Admin")
    supplier = Supplier(supplier_code="SUP-000001", business_name="Protected Supplier", phone="9876543210")
    auth_session.add(supplier); auth_session.commit()
    admin_context = authenticate(auth_session, "admin", "Vertical-password-2026!")
    business_session = Session()
    assert get_supplier_by_id(business_session, supplier.id, actor_context=admin_context).business_name == "Protected Supplier"
    assert get_supplier_by_code(business_session, "SUP-000001", actor_context=admin_context).id == supplier.id
    assert get_supplier_metrics(business_session, actor_context=admin_context)["total"] == 1
    assert len(list_suppliers(business_session, actor_context=admin_context)) == 1
    supplier.is_active = False; business_session.flush()
    assert reactivate_supplier(business_session, supplier.id, actor_context=admin_context).is_active
    with pytest.raises(TypeError):
        get_supplier_by_id(business_session, supplier.id)
    with pytest.raises(TypeError):
        get_supplier_by_code(business_session, "SUP-000001")
    with pytest.raises(TypeError):
        get_supplier_metrics(business_session)
    with pytest.raises(TypeError):
        list_suppliers(business_session)
    with pytest.raises(TypeError):
        reactivate_supplier(business_session, supplier.id)
    clerk = create_user(auth_session, actor_context=admin_context, username="sales", password="Vertical-password-2026!", full_name="Sales", role_names=["Sales Operator"])
    clerk_context = authenticate(auth_session, "sales", "Vertical-password-2026!")
    with pytest.raises(AuthorizationError):
        get_supplier_by_id(business_session, supplier.id, actor_context=clerk_context)
    with pytest.raises(AuthorizationError):
        get_supplier_by_code(business_session, "SUP-000001", actor_context=clerk_context)
    with pytest.raises(AuthorizationError):
        get_supplier_metrics(business_session, actor_context=clerk_context)
    with pytest.raises(AuthorizationError):
        list_suppliers(business_session, actor_context=clerk_context)
    with pytest.raises(AuthorizationError):
        reactivate_supplier(business_session, supplier.id, actor_context=clerk_context)
    assert business_session.query(UserAuditEvent).filter_by(user_id=clerk.id, action="authorization_denied").count() == 5
    logout(auth_session, actor_context=admin_context)
    with pytest.raises(AuthorizationError):
        get_supplier_by_id(business_session, supplier.id, actor_context=admin_context)
