from datetime import date
from decimal import Decimal
from inspect import Parameter, signature

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

import database.connection as database_connection
from database.connection import Base
from database.models import (
    Permission,
    Purchase,
    PurchaseItem,
    RolePermission,
    Supplier,
    User,
    UserAuditEvent,
    UserRole,
)
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_user,
    logout,
)
from services.supplier_service import create_supplier
from services.purchase_service import get_purchase_by_id


def _create_purchase(session, actor_context):
    supplier = create_supplier(
        session,
        {"business_name": "Purchase RBAC supplier", "phone": "9876543210"},
        actor_context=actor_context,
    )
    purchase = Purchase(
        purchase_code="PUR-RBAC-000001",
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 6),
        subtotal=Decimal("100.00"),
        total_amount=Decimal("100.00"),
        amount_paid=Decimal("25.00"),
        balance_amount=Decimal("75.00"),
        payment_status="Partially Paid",
    )
    purchase.items.append(
        PurchaseItem(
            appliance_category="Refrigerators",
            brand="Test Brand",
            model_number="RBAC-1",
            quantity=2,
            weight=Decimal("30.00"),
            unit="pieces",
            rate=Decimal("50.00"),
            line_total=Decimal("100.00"),
        )
    )
    session.add(purchase)
    session.commit()
    return purchase.id


def test_authorized_procurement_can_read_purchase_and_relationships(authenticated_actor):
    with database_connection.get_db() as session:
        purchase_id = _create_purchase(session, authenticated_actor)
        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-read-procurement",
            password="Purchase-read-procurement-password-2026!",
            full_name="Purchase Read Procurement",
            role_names=["Procurement"],
        )
        session.commit()
        procurement_context = authenticate(
            session,
            "purchase-read-procurement",
            "Purchase-read-procurement-password-2026!",
        )
        purchase = get_purchase_by_id(
            session, purchase_id, actor_context=procurement_context
        )
        assert purchase is not None
        assert purchase.purchase_code == "PUR-RBAC-000001"
        assert purchase.supplier.business_name == "Purchase RBAC supplier"
        assert [(item.appliance_category, item.quantity) for item in purchase.items] == [
            ("Refrigerators", 2)
        ]
        logout(session, actor_context=procurement_context)


def test_purchase_read_requires_keyword_actor_and_rejects_missing_or_fabricated_context(
    authenticated_actor, monkeypatch
):
    assert signature(get_purchase_by_id).parameters["actor_context"].kind is Parameter.KEYWORD_ONLY
    with database_connection.get_db() as session:
        purchase_id = _create_purchase(session, authenticated_actor)
        purchase_reads = []
        original_scalar = Session.scalar

        def track_purchase_scalar(bound_session, statement, *args, **kwargs):
            if "purchases" in str(statement).lower():
                purchase_reads.append(str(statement))
            return original_scalar(bound_session, statement, *args, **kwargs)

        monkeypatch.setattr(Session, "scalar", track_purchase_scalar)
        with pytest.raises(TypeError):
            signature(get_purchase_by_id).bind(session, purchase_id)
        fabricated = ActorContext(
            authenticated_actor.user_id,
            authenticated_actor.username,
            "fabricated-purchase-reader",
            authenticated_actor.issued_at,
            authenticated_actor.database_identity,
        )
        with pytest.raises(AuthorizationError):
            get_purchase_by_id(session, purchase_id, actor_context=fabricated)
        assert purchase_reads == []


def test_purchase_read_denies_expired_logged_out_inactive_and_revoked_actors(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        purchase_id = _create_purchase(session, authenticated_actor)

        expired = authenticate(
            session,
            "fixture-admin",
            "Fixture-admin-password-2026!",
            timeout_minutes=0,
        )
        with pytest.raises(AuthorizationError):
            get_purchase_by_id(session, purchase_id, actor_context=expired)

        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-read-clerk",
            password="Purchase-read-clerk-password-2026!",
            full_name="Purchase Read Clerk",
            role_names=["Sales Operator"],
        )
        session.commit()
        clerk_context = authenticate(
            session, "purchase-read-clerk", "Purchase-read-clerk-password-2026!"
        )
        with pytest.raises(AuthorizationError):
            get_purchase_by_id(session, purchase_id, actor_context=clerk_context)
        denied = session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == clerk.id,
                UserAuditEvent.action == "authorization_denied",
                UserAuditEvent.module == "Security",
            )
        )
        assert denied is not None

        revoked = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-read-revoked",
            password="Purchase-read-revoked-password-2026!",
            full_name="Purchase Read Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        revoked_context = authenticate(
            session,
            "purchase-read-revoked",
            "Purchase-read-revoked-password-2026!",
        )
        view_permission = session.scalar(
            select(Permission.id).where(Permission.code == "purchases.view")
        )
        for role_id in session.scalars(
            select(UserRole.role_id).where(UserRole.user_id == revoked.id)
        ):
            session.query(RolePermission).filter_by(
                role_id=role_id, permission_id=view_permission
            ).delete()
        session.commit()
        with pytest.raises(AuthorizationError):
            get_purchase_by_id(session, purchase_id, actor_context=revoked_context)
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == revoked.id,
                UserAuditEvent.action == "authorization_denied",
            )
        ) is not None

        inactive = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-read-inactive",
            password="Purchase-read-inactive-password-2026!",
            full_name="Purchase Read Inactive",
            role_names=["Procurement"],
        )
        session.commit()
        inactive_context = authenticate(
            session,
            "purchase-read-inactive",
            "Purchase-read-inactive-password-2026!",
        )
        session.get(User, inactive.id).is_active = False
        session.commit()
        with pytest.raises(AuthorizationError):
            get_purchase_by_id(session, purchase_id, actor_context=inactive_context)

        logged_out = authenticate(
            session, "fixture-admin", "Fixture-admin-password-2026!"
        )
        logout(session, actor_context=logged_out)
        with pytest.raises(AuthorizationError):
            get_purchase_by_id(session, purchase_id, actor_context=logged_out)


def test_purchase_read_rejects_cross_database_actor_with_colliding_user_id(
    authenticated_actor, tmp_path
):
    with database_connection.get_db() as session:
        _create_purchase(session, authenticated_actor)

    other_engine = create_engine(f"sqlite:///{(tmp_path / 'other.db').as_posix()}")
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(bind=other_engine, autoflush=False, expire_on_commit=False)
    try:
        with OtherSession() as other_session:
            from services.security_service import create_first_administrator

            other_user = create_first_administrator(
                other_session,
                username="other-purchase-admin",
                password="Other-purchase-admin-password-2026!",
                full_name="Other Purchase Administrator",
            )
            other_session.flush()
            supplier = Supplier(
                supplier_code="SUP-000001",
                business_name="Other database supplier",
                phone="9876543210",
            )
            other_session.add(supplier)
            other_session.flush()
            other_purchase = Purchase(
                id=1,
                purchase_code="PUR-OTHER-000001",
                supplier_id=supplier.id,
                subtotal=Decimal("1.00"),
                total_amount=Decimal("1.00"),
                amount_paid=Decimal("0.00"),
                balance_amount=Decimal("1.00"),
            )
            other_session.add(other_purchase)
            other_session.commit()
            assert other_user.id == authenticated_actor.user_id

            with pytest.raises(AuthorizationError):
                get_purchase_by_id(
                    other_session, 1, actor_context=authenticated_actor
                )
            assert other_session.get(Purchase, 1).purchase_code == "PUR-OTHER-000001"
    finally:
        other_engine.dispose()


def test_purchase_read_supports_independent_sessions_on_same_database(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        purchase_id = _create_purchase(session, authenticated_actor)
    with database_connection.get_db() as independent_session:
        purchase = get_purchase_by_id(
            independent_session,
            purchase_id,
            actor_context=authenticated_actor,
        )
        assert purchase is not None
        assert purchase.purchase_code == "PUR-RBAC-000001"


def test_purchase_read_authorizes_before_purchase_select_and_returns_no_denied_data(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        purchase_id = _create_purchase(session, authenticated_actor)
        sales_user = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-read-no-access",
            password="Purchase-read-no-access-password-2026!",
            full_name="Purchase Read No Access",
            role_names=["HR/Employee Manager"],
        )
        session.commit()
        denied_actor = authenticate(
            session,
            "purchase-read-no-access",
            "Purchase-read-no-access-password-2026!",
        )
        statements = []

        def record_sql(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.lower())

        event.listen(session.get_bind(), "before_cursor_execute", record_sql)
        try:
            with pytest.raises(AuthorizationError):
                get_purchase_by_id(session, purchase_id, actor_context=denied_actor)
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", record_sql)

        assert not any(" from purchases " in sql for sql in statements)
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == sales_user.id,
                UserAuditEvent.action == "authorization_denied",
            )
        ) is not None
