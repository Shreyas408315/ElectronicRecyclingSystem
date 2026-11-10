from datetime import date
from decimal import Decimal
from inspect import Parameter, signature

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

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
from services.purchase_service import get_purchase_by_code
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_first_administrator,
    create_user,
    logout,
)
from services.supplier_service import create_supplier


def _create_purchase(session, actor_context, code="PUR-CODE-RBAC-000001"):
    supplier = create_supplier(
        session,
        {"business_name": "Purchase code supplier", "phone": "9876543210"},
        actor_context=actor_context,
    )
    purchase = Purchase(
        purchase_code=code,
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
            brand="RBAC Brand",
            model_number="CODE-1",
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


def test_procurement_reads_purchase_by_code_and_existing_lookup_behavior(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        _create_purchase(session, authenticated_actor)
        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-code-procurement",
            password="Purchase-code-procurement-password-2026!",
            full_name="Purchase Code Procurement",
            role_names=["Procurement"],
        )
        session.commit()
        procurement_context = authenticate(
            session,
            "purchase-code-procurement",
            "Purchase-code-procurement-password-2026!",
        )

        purchase = get_purchase_by_code(
            session,
            "  PUR-CODE-RBAC-000001  ",
            actor_context=procurement_context,
        )
        assert purchase is not None
        assert purchase.purchase_code == "PUR-CODE-RBAC-000001"
        assert purchase.supplier.business_name == "Purchase code supplier"
        assert [(item.appliance_category, item.quantity) for item in purchase.items] == [
            ("Refrigerators", 2)
        ]
        assert get_purchase_by_code(
            session, "PUR-NOT-FOUND", actor_context=procurement_context
        ) is None


def test_purchase_by_code_requires_actor_and_denies_before_purchase_read(
    authenticated_actor,
):
    assert signature(get_purchase_by_code).parameters["actor_context"].kind is Parameter.KEYWORD_ONLY
    with database_connection.get_db() as session:
        _create_purchase(session, authenticated_actor)
        user = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-code-no-access",
            password="Purchase-code-no-access-password-2026!",
            full_name="Purchase Code No Access",
            role_names=["HR/Employee Manager"],
        )
        session.commit()
        denied_actor = authenticate(
            session,
            "purchase-code-no-access",
            "Purchase-code-no-access-password-2026!",
        )
        with pytest.raises(TypeError):
            signature(get_purchase_by_code).bind(session, "PUR-CODE-RBAC-000001")

        fabricated = ActorContext(
            denied_actor.user_id,
            denied_actor.username,
            "fabricated-purchase-code-context",
            denied_actor.issued_at,
            denied_actor.database_identity,
        )
        statements = []

        def record_sql(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.lower())

        event.listen(session.get_bind(), "before_cursor_execute", record_sql)
        try:
            with pytest.raises(AuthorizationError):
                get_purchase_by_code(
                    session, "PUR-CODE-RBAC-000001", actor_context=fabricated
                )
            with pytest.raises(AuthorizationError):
                get_purchase_by_code(
                    session,
                    "PUR-CODE-RBAC-000001",
                    actor_context=denied_actor,
                )
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", record_sql)

        assert not any(" from purchases " in statement for statement in statements)
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == user.id,
                UserAuditEvent.action == "authorization_denied",
                UserAuditEvent.module == "Security",
            )
        ) is not None


def test_purchase_by_code_rejects_expired_logged_out_inactive_and_revoked_actors(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        _create_purchase(session, authenticated_actor)
        expired = authenticate(
            session,
            "fixture-admin",
            "Fixture-admin-password-2026!",
            timeout_minutes=0,
        )
        with pytest.raises(AuthorizationError):
            get_purchase_by_code(
                session, "PUR-CODE-RBAC-000001", actor_context=expired
            )

        revoked = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-code-revoked",
            password="Purchase-code-revoked-password-2026!",
            full_name="Purchase Code Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        revoked_context = authenticate(
            session, "purchase-code-revoked", "Purchase-code-revoked-password-2026!"
        )
        permission_id = session.scalar(
            select(Permission.id).where(Permission.code == "purchases.view")
        )
        for role_id in session.scalars(
            select(UserRole.role_id).where(UserRole.user_id == revoked.id)
        ):
            session.query(RolePermission).filter_by(
                role_id=role_id, permission_id=permission_id
            ).delete()
        session.commit()
        with pytest.raises(AuthorizationError):
            get_purchase_by_code(
                session,
                "PUR-CODE-RBAC-000001",
                actor_context=revoked_context,
            )

        inactive = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-code-inactive",
            password="Purchase-code-inactive-password-2026!",
            full_name="Purchase Code Inactive",
            role_names=["Procurement"],
        )
        session.commit()
        inactive_context = authenticate(
            session, "purchase-code-inactive", "Purchase-code-inactive-password-2026!"
        )
        session.get(User, inactive.id).is_active = False
        session.commit()
        with pytest.raises(AuthorizationError):
            get_purchase_by_code(
                session,
                "PUR-CODE-RBAC-000001",
                actor_context=inactive_context,
            )

        logged_out = authenticate(
            session, "fixture-admin", "Fixture-admin-password-2026!"
        )
        logout(session, actor_context=logged_out)
        with pytest.raises(AuthorizationError):
            get_purchase_by_code(
                session, "PUR-CODE-RBAC-000001", actor_context=logged_out
            )


def test_purchase_by_code_rejects_cross_database_actor_with_colliding_user_id(
    authenticated_actor, tmp_path
):
    other_engine = create_engine(f"sqlite:///{(tmp_path / 'other-purchase-code.db').as_posix()}")
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(bind=other_engine, autoflush=False, expire_on_commit=False)
    try:
        with OtherSession() as session:
            user = create_first_administrator(
                session,
                username="other-purchase-code-admin",
                password="Other-purchase-code-admin-password-2026!",
                full_name="Other Purchase Code Administrator",
            )
            session.flush()
            supplier = Supplier(
                supplier_code="SUP-000001",
                business_name="Other database supplier",
                phone="9876543210",
            )
            session.add(supplier)
            session.flush()
            session.add(
                Purchase(
                    id=1,
                    purchase_code="PUR-OTHER-000001",
                    supplier_id=supplier.id,
                    subtotal=Decimal("10.00"),
                    total_amount=Decimal("10.00"),
                    amount_paid=Decimal("0.00"),
                    balance_amount=Decimal("10.00"),
                )
            )
            session.commit()
            assert user.id == authenticated_actor.user_id
            with pytest.raises(AuthorizationError):
                get_purchase_by_code(
                    session,
                    "PUR-OTHER-000001",
                    actor_context=authenticated_actor,
                )
    finally:
        other_engine.dispose()


def test_purchase_by_code_supports_independent_same_database_sessions(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        _create_purchase(session, authenticated_actor)
    with database_connection.get_db() as independent_session:
        purchase = get_purchase_by_code(
            independent_session,
            "PUR-CODE-RBAC-000001",
            actor_context=authenticated_actor,
        )
        assert purchase is not None
        assert purchase.purchase_code == "PUR-CODE-RBAC-000001"
