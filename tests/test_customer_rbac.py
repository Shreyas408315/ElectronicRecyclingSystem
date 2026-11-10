import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import (
    Customer,
    Permission,
    RolePermission,
    User,
    UserAuditEvent,
    UserRole,
)
from services import customer_service
from services.customer_report_service import (
    generate_customer_excel_report,
    generate_customer_pdf_report,
)
from services.customer_service import (
    create_customer,
    deactivate_customer,
    find_potential_customer_duplicates,
    generate_next_customer_code,
    get_customer_by_code,
    get_customer_by_id,
    get_customer_metrics,
    list_customers,
    reactivate_customer,
    set_customer_active,
    update_customer,
)
from services.security_service import (
    ActorContext,
    AuthorizationError,
    ROLE_MATRIX,
    authenticate,
    create_user,
    logout,
)


@pytest.fixture
def customer_db(authenticated_actor_factory):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    actor = authenticated_actor_factory(session)
    session.info["actor_context"] = actor
    try:
        yield session, factory, actor, authenticated_actor_factory
    finally:
        session.close()
        engine.dispose()


def _payload(name="RBAC Customer"):
    return {
        "customer_name": name,
        "customer_type": "Distributor",
        "phone": "9000000001",
        "email": f"{name.lower().replace(' ', '.')}@example.com",
        "opening_balance": "10.00",
    }


def test_customer_public_database_apis_reject_missing_actor_context(customer_db, tmp_path):
    session, _, _, _ = customer_db
    payload = _payload()
    customer = create_customer(
        session, payload, actor_context=session.info["actor_context"]
    )
    calls = [
        lambda: generate_next_customer_code(session),
        lambda: find_potential_customer_duplicates(session, payload),
        lambda: create_customer(session, _payload("No Context")),
        lambda: update_customer(session, customer.id, payload),
        lambda: get_customer_by_id(session, customer.id),
        lambda: get_customer_by_code(session, customer.customer_code),
        lambda: list_customers(session),
        lambda: set_customer_active(session, customer.id, False),
        lambda: deactivate_customer(session, customer.id),
        lambda: reactivate_customer(session, customer.id),
        lambda: get_customer_metrics(session),
        lambda: generate_customer_excel_report(
            session, str(tmp_path / "missing-actor.xlsx")
        ),
        lambda: generate_customer_pdf_report(
            session, str(tmp_path / "missing-actor.pdf")
        ),
    ]
    for call in calls:
        with pytest.raises(TypeError):
            call()


def test_customer_role_mapping_and_accounts_view_only_enforcement(customer_db):
    session, _, admin_context, _ = customer_db
    assert "customers.view" in ROLE_MATRIX["Sales Operator"]
    assert "customers.manage" in ROLE_MATRIX["Sales Operator"]
    assert "customers.view" in ROLE_MATRIX["Accounts & Finance"]
    assert "customers.manage" not in ROLE_MATRIX["Accounts & Finance"]
    assert "customers.view" in ROLE_MATRIX["Read-only Auditor"]

    account_user = create_user(
        session,
        actor_context=admin_context,
        username="customer-account-user",
        password="Customer-account-2026!",
        full_name="Customer Accounts",
        role_names=["Accounts & Finance"],
    )
    account_context = authenticate(
        session, "customer-account-user", "Customer-account-2026!"
    )
    assert list_customers(session, actor_context=account_context) == []

    with pytest.raises(AuthorizationError, match="customers.manage"):
        create_customer(session, _payload(), actor_context=account_context)
    assert session.scalar(select(Customer.id)) is None
    assert session.scalar(
        select(UserAuditEvent.id).where(
            UserAuditEvent.action == "authorization_denied"
        )
    ) is not None
    assert session.get(User, account_user.id).is_active


def test_customer_context_trust_scope_expiry_logout_inactive_and_revocation(customer_db):
    session, factory, admin_context, authenticate_for = customer_db
    payload = _payload()

    fabricated = ActorContext(
        admin_context.user_id,
        admin_context.username,
        "not-a-trusted-session",
        admin_context.issued_at,
        admin_context.database_identity,
    )
    with pytest.raises(AuthorizationError):
        list_customers(session, actor_context=fabricated)

    expired = authenticate(
        session, "isolated-admin-1", "Isolated-test-admin-2026!", timeout_minutes=-1
    )
    with pytest.raises(AuthorizationError):
        list_customers(session, actor_context=expired)

    logged_out = authenticate(
        session, "isolated-admin-1", "Isolated-test-admin-2026!"
    )
    logout(session, actor_context=logged_out)
    with pytest.raises(AuthorizationError):
        list_customers(session, actor_context=logged_out)

    session.commit()
    independent_session = factory()
    try:
        assert list_customers(
            independent_session, actor_context=admin_context
        ) == []
    finally:
        independent_session.close()

    other_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=other_engine)
    other_session = sessionmaker(bind=other_engine)()
    try:
        other_context = authenticate_for(other_session)
        assert other_context.user_id == admin_context.user_id
        with pytest.raises(AuthorizationError, match="different database"):
            list_customers(session, actor_context=other_context)
    finally:
        other_session.close()
        other_engine.dispose()

    user = session.get(User, admin_context.user_id)
    user.is_active = False
    session.flush()
    with pytest.raises(AuthorizationError, match="inactive"):
        list_customers(session, actor_context=admin_context)
    user.is_active = True
    session.flush()

    permission = session.scalar(
        select(Permission).where(Permission.code == "customers.manage")
    )
    role_ids = session.scalars(
        select(UserRole.role_id).where(UserRole.user_id == admin_context.user_id)
    ).all()
    for role_id in role_ids:
        grant = session.scalar(
            select(RolePermission).where(
                RolePermission.role_id == role_id,
                RolePermission.permission_id == permission.id,
            )
        )
        if grant is not None:
            session.delete(grant)
    session.flush()
    with pytest.raises(AuthorizationError, match="customers.manage"):
        create_customer(session, payload, actor_context=admin_context)
    assert session.scalar(select(Customer.id)) is None


def test_customer_permission_denial_precedes_customer_mutation(customer_db, monkeypatch):
    session, _, admin_context, _ = customer_db
    customer = create_customer(
        session, _payload(), actor_context=admin_context
    )
    permission = session.scalar(
        select(Permission).where(Permission.code == "customers.manage")
    )
    role_id = session.scalar(
        select(UserRole.role_id).where(UserRole.user_id == admin_context.user_id)
    )
    grant = session.scalar(
        select(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == permission.id,
        )
    )
    session.delete(grant)
    session.flush()

    original_get = customer_service.Session.get
    customer_reads = []

    def tracked_get(self, entity, ident, **kwargs):
        if entity is Customer:
            customer_reads.append(ident)
        return original_get(self, entity, ident, **kwargs)

    monkeypatch.setattr(customer_service.Session, "get", tracked_get)
    with pytest.raises(AuthorizationError):
        update_customer(
            session, customer.id, _payload("Changed"), actor_context=admin_context
        )
    assert customer_reads == []
    assert session.get(Customer, customer.id).customer_name == "RBAC Customer"
    assert isinstance(customer, Customer)
