from datetime import date, timedelta
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
from services.purchase_service import get_recent_purchases
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_first_administrator,
    create_user,
    logout,
)


def _create_recent_purchase_rows(session):
    supplier = Supplier(
        supplier_code="SUP-RECENT-0001",
        business_name="Recent Purchases Supplier",
        phone="9000000201",
    )
    session.add(supplier)
    session.flush()
    purchases = []
    for index in range(7):
        purchase = Purchase(
            purchase_code=f"PUR-RECENT-{index + 1:06d}",
            supplier_id=supplier.id,
            purchase_date=date.today() - timedelta(days=index // 2),
            subtotal=Decimal("10.00"),
            total_amount=Decimal("10.00"),
            amount_paid=Decimal("0.00"),
            balance_amount=Decimal("10.00"),
            payment_status="Unpaid",
        )
        if index == 0:
            purchase.items.append(
                PurchaseItem(
                    appliance_category="Refrigerators",
                    brand="Test Brand",
                    model_number="R-1",
                    quantity=1,
                    weight=Decimal("10.00"),
                    unit="pieces",
                    rate=Decimal("10.00"),
                    line_total=Decimal("10.00"),
                )
            )
        purchases.append(purchase)
    session.add_all(purchases)
    session.commit()
    return purchases


def test_procurement_can_list_recent_purchases_with_existing_limit_and_order(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        purchases = _create_recent_purchase_rows(session)
        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-recent-procurement",
            password="Purchase-recent-procurement-2026!",
            full_name="Purchase Recent Procurement",
            role_names=["Procurement"],
        )
        session.commit()
        actor = authenticate(
            session,
            procurement.username,
            "Purchase-recent-procurement-2026!",
        )

        assert (
            signature(get_recent_purchases).parameters["actor_context"].kind
            is Parameter.KEYWORD_ONLY
        )
        recent = get_recent_purchases(session, actor_context=actor)
        assert len(recent) == 5
        assert [purchase.id for purchase in recent] == [
            purchase.id for purchase in sorted(
                purchases,
                key=lambda row: (row.purchase_date, row.id),
                reverse=True,
            )[:5]
        ]
        assert all(
            purchase.supplier.business_name == "Recent Purchases Supplier"
            for purchase in recent
        )
        item_purchase = next(purchase for purchase in recent if purchase.items)
        assert item_purchase.items[0].appliance_category == "Refrigerators"
        assert get_recent_purchases(session, 2, actor_context=actor) == recent[:2]
        assert get_recent_purchases(session, 0, actor_context=actor) == []
        logout(session, actor_context=actor)


def test_recent_purchases_empty_result_remains_empty(authenticated_actor):
    with database_connection.get_db() as session:
        assert get_recent_purchases(session, actor_context=authenticated_actor) == []


def test_recent_purchases_authorizes_before_query_and_records_denial(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        _create_recent_purchase_rows(session)
        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-recent-clerk",
            password="Purchase-recent-clerk-2026!",
            full_name="Purchase Recent Clerk",
            role_names=["HR/Employee Manager"],
        )
        session.commit()
        denied_actor = authenticate(
            session, clerk.username, "Purchase-recent-clerk-2026!"
        )
        fabricated_actor = ActorContext(
            denied_actor.user_id,
            denied_actor.username,
            "fabricated-purchase-recent-context",
            denied_actor.issued_at,
            denied_actor.database_identity,
        )
        expired_actor = authenticate(
            session,
            "fixture-admin",
            "Fixture-admin-password-2026!",
            timeout_minutes=0,
        )
        logged_out_actor = authenticate(
            session, "fixture-admin", "Fixture-admin-password-2026!"
        )
        logout(session, actor_context=logged_out_actor)

        inactive_user = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-recent-inactive",
            password="Purchase-recent-inactive-2026!",
            full_name="Purchase Recent Inactive",
            role_names=["Procurement"],
        )
        session.commit()
        inactive_actor = authenticate(
            session, inactive_user.username, "Purchase-recent-inactive-2026!"
        )
        session.get(User, inactive_user.id).is_active = False
        session.commit()

        revoked_user = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-recent-revoked",
            password="Purchase-recent-revoked-2026!",
            full_name="Purchase Recent Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        revoked_actor = authenticate(
            session, revoked_user.username, "Purchase-recent-revoked-2026!"
        )
        view_permission_id = session.scalar(
            select(Permission.id).where(Permission.code == "purchases.view")
        )
        for role_id in session.scalars(
            select(UserRole.role_id).where(UserRole.user_id == revoked_user.id)
        ):
            session.query(RolePermission).filter_by(
                role_id=role_id, permission_id=view_permission_id
            ).delete()
        session.commit()

        with pytest.raises(TypeError):
            signature(get_recent_purchases).bind(session)

        purchase_queries = []

        def record_purchase_query(conn, cursor, statement, parameters, context, executemany):
            if " from purchases " in statement.lower():
                purchase_queries.append(statement)

        event.listen(session.get_bind(), "before_cursor_execute", record_purchase_query)
        try:
            for actor in (
                fabricated_actor,
                denied_actor,
                expired_actor,
                logged_out_actor,
                inactive_actor,
                revoked_actor,
            ):
                with pytest.raises(AuthorizationError):
                    get_recent_purchases(session, actor_context=actor)
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", record_purchase_query)

        assert purchase_queries == []
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == clerk.id,
                UserAuditEvent.action == "authorization_denied",
                UserAuditEvent.module == "Security",
            )
        ) is not None

    for actor in (
        denied_actor,
        expired_actor,
        logged_out_actor,
        inactive_actor,
        revoked_actor,
    ):
        logout(actor_context=actor)


def test_recent_purchases_rejects_cross_database_actor_with_colliding_user_id(
    authenticated_actor, tmp_path
):
    other_engine = create_engine(
        f"sqlite:///{(tmp_path / 'other-purchase-recent.db').as_posix()}"
    )
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(
        bind=other_engine, autoflush=False, expire_on_commit=False
    )
    try:
        with OtherSession() as session:
            other_user = create_first_administrator(
                session,
                username="other-purchase-recent-admin",
                password="Other-purchase-recent-admin-2026!",
                full_name="Other Purchase Recent Admin",
            )
            session.flush()
            supplier = Supplier(
                supplier_code="SUP-OTHER-RECENT-0001",
                business_name="Other Recent Supplier",
                phone="9000000202",
            )
            session.add(supplier)
            session.flush()
            session.add(
                Purchase(
                    purchase_code="PUR-OTHER-RECENT-000001",
                    supplier_id=supplier.id,
                    total_amount=Decimal("10.00"),
                    amount_paid=Decimal("0.00"),
                    balance_amount=Decimal("10.00"),
                )
            )
            session.commit()
            assert other_user.id == authenticated_actor.user_id
            with pytest.raises(AuthorizationError):
                get_recent_purchases(session, actor_context=authenticated_actor)
    finally:
        other_engine.dispose()


def test_recent_purchases_supports_independent_same_database_sessions(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        purchases = _create_recent_purchase_rows(session)
    with database_connection.get_db() as independent_session:
        results = get_recent_purchases(
            independent_session, actor_context=authenticated_actor
        )
        assert len(results) == 5
        expected = sorted(
            purchases,
            key=lambda row: (row.purchase_date, row.id),
            reverse=True,
        )[:5]
        assert [purchase.id for purchase in results] == [
            purchase.id for purchase in expected
        ]
