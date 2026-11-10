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
    RolePermission,
    Supplier,
    User,
    UserAuditEvent,
    UserRole,
)
from services.purchase_service import get_purchase_metrics
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_first_administrator,
    create_user,
    logout,
)


PURCHASE_LIST_PASSWORD = "Purchase-metrics-procurement-2026!"


def _add_metric_purchases(session):
    supplier = Supplier(
        supplier_code="SUP-METRICS-0001",
        business_name="Purchase Metrics Supplier",
        phone="9000000101",
    )
    session.add(supplier)
    session.flush()
    today = date.today()
    purchases = [
        Purchase(
            purchase_code="PUR-METRICS-000001",
            supplier_id=supplier.id,
            purchase_date=today,
            subtotal=Decimal("10.00"),
            total_amount=Decimal("10.00"),
            amount_paid=Decimal("1.00"),
            balance_amount=Decimal("9.00"),
            payment_status="Partially Paid",
        ),
        Purchase(
            purchase_code="PUR-METRICS-000002",
            supplier_id=supplier.id,
            purchase_date=today - timedelta(days=2),
            subtotal=Decimal("20.00"),
            total_amount=Decimal("20.00"),
            amount_paid=Decimal("2.00"),
            balance_amount=Decimal("18.00"),
            payment_status="Unpaid",
        ),
        Purchase(
            purchase_code="PUR-METRICS-000003",
            supplier_id=supplier.id,
            purchase_date=today - timedelta(days=40),
            subtotal=Decimal("30.00"),
            total_amount=Decimal("30.00"),
            amount_paid=Decimal("3.00"),
            balance_amount=Decimal("27.00"),
            payment_status="Paid",
        ),
    ]
    session.add_all(purchases)
    session.commit()


def test_metrics_preserve_values_for_procurement_and_independent_session(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        _add_metric_purchases(session)
        create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-metrics-procurement",
            password=PURCHASE_LIST_PASSWORD,
            full_name="Purchase Metrics Procurement",
            role_names=["Procurement"],
        )
        session.commit()
        procurement_actor = authenticate(
            session, "purchase-metrics-procurement", PURCHASE_LIST_PASSWORD
        )

        assert (
            signature(get_purchase_metrics).parameters["actor_context"].kind
            is Parameter.KEYWORD_ONLY
        )
        expected = {
            "total_purchases": 3,
            "purchases_today": 1,
            "purchases_this_month": 2,
            "total_expenditure": Decimal("60.00"),
            "total_paid": Decimal("6.00"),
            "total_outstanding": Decimal("54.00"),
        }
        assert get_purchase_metrics(
            session, actor_context=procurement_actor
        ) == expected

    with database_connection.get_db() as independent_session:
        assert get_purchase_metrics(
            independent_session, actor_context=procurement_actor
        ) == expected
    logout(actor_context=procurement_actor)


def test_empty_metrics_and_missing_actor_behavior(authenticated_actor):
    with database_connection.get_db() as session:
        with pytest.raises(TypeError):
            signature(get_purchase_metrics).bind(session)
        assert get_purchase_metrics(session, actor_context=authenticated_actor) == {
            "total_purchases": 0,
            "purchases_today": 0,
            "purchases_this_month": 0,
            "total_expenditure": Decimal("0.00"),
            "total_paid": Decimal("0.00"),
            "total_outstanding": Decimal("0.00"),
        }


def test_denied_actors_are_rejected_before_purchase_queries_and_audited(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        _add_metric_purchases(session)
        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-metrics-clerk",
            password="Purchase-metrics-clerk-2026!",
            full_name="Purchase Metrics Clerk",
            role_names=["HR/Employee Manager"],
        )
        session.commit()
        clerk_actor = authenticate(
            session, "purchase-metrics-clerk", "Purchase-metrics-clerk-2026!"
        )
        fabricated_actor = ActorContext(
            clerk_actor.user_id,
            clerk_actor.username,
            "fabricated-purchase-metrics-context",
            clerk_actor.issued_at,
            clerk_actor.database_identity,
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
            username="purchase-metrics-inactive",
            password="Purchase-metrics-inactive-2026!",
            full_name="Purchase Metrics Inactive",
            role_names=["Procurement"],
        )
        session.commit()
        inactive_actor = authenticate(
            session,
            "purchase-metrics-inactive",
            "Purchase-metrics-inactive-2026!",
        )
        session.get(User, inactive_user.id).is_active = False
        session.commit()

        revoked_user = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-metrics-revoked",
            password="Purchase-metrics-revoked-2026!",
            full_name="Purchase Metrics Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        revoked_actor = authenticate(
            session,
            "purchase-metrics-revoked",
            "Purchase-metrics-revoked-2026!",
        )
        view_permission_id = session.scalar(
            select(Permission.id).where(Permission.code == "purchases.view")
        )
        revoked_role_ids = session.scalars(
            select(UserRole.role_id).where(UserRole.user_id == revoked_user.id)
        )
        for role_id in revoked_role_ids:
            session.query(RolePermission).filter_by(
                role_id=role_id, permission_id=view_permission_id
            ).delete()
        session.commit()

        purchase_queries = []

        def record_purchase_query(conn, cursor, statement, parameters, context, executemany):
            if " from purchases " in statement.lower():
                purchase_queries.append(statement)

        event.listen(session.get_bind(), "before_cursor_execute", record_purchase_query)
        try:
            for actor in (
                fabricated_actor,
                clerk_actor,
                expired_actor,
                logged_out_actor,
                inactive_actor,
                revoked_actor,
            ):
                with pytest.raises(AuthorizationError):
                    get_purchase_metrics(session, actor_context=actor)
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
        clerk_actor,
        expired_actor,
        logged_out_actor,
        inactive_actor,
        revoked_actor,
    ):
        logout(actor_context=actor)


def test_metrics_reject_cross_database_actor_even_when_user_ids_collide(
    authenticated_actor, tmp_path
):
    other_engine = create_engine(
        f"sqlite:///{(tmp_path / 'other-purchase-metrics.db').as_posix()}"
    )
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(
        bind=other_engine, autoflush=False, expire_on_commit=False
    )
    try:
        with OtherSession() as session:
            other_user = create_first_administrator(
                session,
                username="other-purchase-metrics-admin",
                password="Other-purchase-metrics-admin-2026!",
                full_name="Other Purchase Metrics Admin",
            )
            session.flush()
            supplier = Supplier(
                supplier_code="SUP-OTHER-METRICS-0001",
                business_name="Other Metrics Supplier",
                phone="9000000102",
            )
            session.add(supplier)
            session.flush()
            session.add(
                Purchase(
                    purchase_code="PUR-OTHER-METRICS-000001",
                    supplier_id=supplier.id,
                    total_amount=Decimal("900.00"),
                    amount_paid=Decimal("0.00"),
                    balance_amount=Decimal("900.00"),
                )
            )
            session.commit()
            assert other_user.id == authenticated_actor.user_id
            with pytest.raises(AuthorizationError):
                get_purchase_metrics(session, actor_context=authenticated_actor)
    finally:
        other_engine.dispose()
