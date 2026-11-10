from datetime import date, timedelta
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
from services.purchase_service import list_purchases
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_first_administrator,
    create_user,
    logout,
)
from services.supplier_service import create_supplier


def _create_listing_fixtures(session, actor_context):
    supplier_a = create_supplier(
        session,
        {
            "business_name": "Alpha Purchase Supplier",
            "phone": "9000000001",
        },
        actor_context=actor_context,
    )
    supplier_b = create_supplier(
        session,
        {
            "business_name": "Beta Purchase Supplier",
            "phone": "9000000002",
        },
        actor_context=actor_context,
    )
    older_paid = Purchase(
        purchase_code="PUR-LIST-000001",
        supplier_id=supplier_a.id,
        purchase_date=date.today() - timedelta(days=5),
        subtotal=Decimal("100.00"),
        total_amount=Decimal("100.00"),
        amount_paid=Decimal("100.00"),
        balance_amount=Decimal("0.00"),
        payment_status="Paid",
    )
    older_paid.items.append(PurchaseItem(
        appliance_category="Refrigerators",
        brand="Brand A",
        model_number="A-1",
        quantity=2,
        weight=Decimal("20.00"),
        unit="pieces",
        rate=Decimal("50.00"),
        line_total=Decimal("100.00"),
    ))
    newer_unpaid = Purchase(
        purchase_code="PUR-LIST-000002",
        supplier_id=supplier_b.id,
        purchase_date=date.today(),
        subtotal=Decimal("60.00"),
        total_amount=Decimal("60.00"),
        amount_paid=Decimal("0.00"),
        balance_amount=Decimal("60.00"),
        payment_status="Unpaid",
    )
    newer_unpaid.items.append(PurchaseItem(
        appliance_category="Air Conditioners",
        brand="Brand B",
        model_number="B-1",
        quantity=1,
        weight=Decimal("15.00"),
        unit="pieces",
        rate=Decimal("60.00"),
        line_total=Decimal("60.00"),
    ))
    partial = Purchase(
        purchase_code="PUR-LIST-000003",
        supplier_id=supplier_a.id,
        purchase_date=date.today() - timedelta(days=2),
        subtotal=Decimal("80.00"),
        total_amount=Decimal("80.00"),
        amount_paid=Decimal("30.00"),
        balance_amount=Decimal("50.00"),
        payment_status="Partially Paid",
    )
    partial.items.append(PurchaseItem(
        appliance_category="Refrigerators",
        brand="Brand C",
        model_number="C-1",
        quantity=4,
        weight=Decimal("40.00"),
        unit="pieces",
        rate=Decimal("20.00"),
        line_total=Decimal("80.00"),
    ))
    session.add_all([older_paid, newer_unpaid, partial])
    session.commit()
    return older_paid.id, newer_unpaid.id, partial.id


def test_procurement_listing_preserves_filters_ordering_and_eager_relationships(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        _create_listing_fixtures(session, authenticated_actor)
        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-list-procurement",
            password="Purchase-list-procurement-password-2026!",
            full_name="Purchase List Procurement",
            role_names=["Procurement"],
        )
        session.commit()
        actor = authenticate(
            session,
            "purchase-list-procurement",
            "Purchase-list-procurement-password-2026!",
        )

        assert signature(list_purchases).parameters["actor_context"].kind is Parameter.KEYWORD_ONLY
        recent = list_purchases(session, actor_context=actor)
        assert [row.purchase_code for row in recent] == [
            "PUR-LIST-000002", "PUR-LIST-000003", "PUR-LIST-000001"
        ]
        assert all(row.supplier is not None and row.items for row in recent)

        assert [row.purchase_code for row in list_purchases(
            session, search="Alpha Purchase Supplier", actor_context=actor
        )] == ["PUR-LIST-000003", "PUR-LIST-000001"]
        assert [row.purchase_code for row in list_purchases(
            session, search="PUR-LIST-000002", actor_context=actor
        )] == ["PUR-LIST-000002"]
        assert [row.purchase_code for row in list_purchases(
            session,
            start_date=date.today() - timedelta(days=3),
            end_date=date.today() - timedelta(days=1),
            actor_context=actor,
        )] == ["PUR-LIST-000003"]
        assert [row.purchase_code for row in list_purchases(
            session, category_filter="Air Conditioners", actor_context=actor
        )] == ["PUR-LIST-000002"]
        assert [row.purchase_code for row in list_purchases(
            session, status_filter="PAID", actor_context=actor
        )] == ["PUR-LIST-000001"]
        assert [row.purchase_code for row in list_purchases(
            session, status_filter="PARTIALLY PAID", actor_context=actor
        )] == ["PUR-LIST-000003"]
        assert [row.purchase_code for row in list_purchases(
            session, sort_by="date_asc", actor_context=actor
        )] == ["PUR-LIST-000001", "PUR-LIST-000003", "PUR-LIST-000002"]
        assert [row.purchase_code for row in list_purchases(
            session, sort_by="amount_desc", actor_context=actor
        )] == ["PUR-LIST-000001", "PUR-LIST-000003", "PUR-LIST-000002"]
        assert list_purchases(
            session, search="does-not-exist", actor_context=actor
        ) == []
        logout(session, actor_context=actor)


def test_list_requires_trusted_actor_and_authorizes_before_purchase_query(
    authenticated_actor, monkeypatch
):
    with database_connection.get_db() as session:
        _create_listing_fixtures(session, authenticated_actor)
        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-list-clerk",
            password="Purchase-list-clerk-password-2026!",
            full_name="Purchase List Clerk",
            role_names=["HR/Employee Manager"],
        )
        session.commit()
        denied_actor = authenticate(
            session, "purchase-list-clerk", "Purchase-list-clerk-password-2026!"
        )
        with pytest.raises(TypeError):
            signature(list_purchases).bind(session)
        fabricated = ActorContext(
            denied_actor.user_id,
            denied_actor.username,
            "fabricated-purchase-list-context",
            denied_actor.issued_at,
            denied_actor.database_identity,
        )

        purchase_selects = []

        def record_sql(conn, cursor, statement, parameters, context, executemany):
            sql = statement.lower()
            if " from purchases " in sql or " join suppliers " in sql:
                purchase_selects.append(sql)

        event.listen(session.get_bind(), "before_cursor_execute", record_sql)
        try:
            for actor in (fabricated, denied_actor):
                with pytest.raises(AuthorizationError):
                    list_purchases(session, actor_context=actor)
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", record_sql)

        assert purchase_selects == []
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == clerk.id,
                UserAuditEvent.action == "authorization_denied",
                UserAuditEvent.module == "Security",
            )
        ) is not None


def test_list_rejects_expired_logged_out_inactive_and_revoked_actors(authenticated_actor):
    with database_connection.get_db() as session:
        _create_listing_fixtures(session, authenticated_actor)
        expired = authenticate(
            session,
            "fixture-admin",
            "Fixture-admin-password-2026!",
            timeout_minutes=0,
        )
        with pytest.raises(AuthorizationError):
            list_purchases(session, actor_context=expired)

        revoked = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-list-revoked",
            password="Purchase-list-revoked-password-2026!",
            full_name="Purchase List Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        revoked_actor = authenticate(
            session, "purchase-list-revoked", "Purchase-list-revoked-password-2026!"
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
            list_purchases(session, actor_context=revoked_actor)
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == revoked.id,
                UserAuditEvent.action == "authorization_denied",
            )
        ) is not None

        inactive = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-list-inactive",
            password="Purchase-list-inactive-password-2026!",
            full_name="Purchase List Inactive",
            role_names=["Procurement"],
        )
        session.commit()
        inactive_actor = authenticate(
            session, "purchase-list-inactive", "Purchase-list-inactive-password-2026!"
        )
        session.get(User, inactive.id).is_active = False
        session.commit()
        with pytest.raises(AuthorizationError):
            list_purchases(session, actor_context=inactive_actor)

        logged_out = authenticate(
            session, "fixture-admin", "Fixture-admin-password-2026!"
        )
        logout(session, actor_context=logged_out)
        with pytest.raises(AuthorizationError):
            list_purchases(session, actor_context=logged_out)


def test_list_rejects_cross_database_actor_even_when_user_ids_collide(
    authenticated_actor, tmp_path
):
    other_engine = create_engine(f"sqlite:///{(tmp_path / 'other-purchase-list.db').as_posix()}")
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(bind=other_engine, autoflush=False, expire_on_commit=False)
    try:
        with OtherSession() as session:
            other_user = create_first_administrator(
                session,
                username="other-purchase-list-admin",
                password="Other-purchase-list-admin-password-2026!",
                full_name="Other Purchase List Admin",
            )
            session.flush()
            supplier = Supplier(
                supplier_code="SUP-000001",
                business_name="Other database supplier",
                phone="9000000001",
            )
            session.add(supplier)
            session.flush()
            session.add(Purchase(
                id=1,
                purchase_code="PUR-OTHER-LIST-000001",
                supplier_id=supplier.id,
                subtotal=Decimal("1.00"),
                total_amount=Decimal("1.00"),
                amount_paid=Decimal("0.00"),
                balance_amount=Decimal("1.00"),
            ))
            session.commit()
            assert other_user.id == authenticated_actor.user_id
            with pytest.raises(AuthorizationError):
                list_purchases(session, actor_context=authenticated_actor)
    finally:
        other_engine.dispose()


def test_list_supports_independent_sessions_on_same_database(authenticated_actor):
    with database_connection.get_db() as session:
        _create_listing_fixtures(session, authenticated_actor)
    with database_connection.get_db() as independent_session:
        results = list_purchases(
            independent_session, actor_context=authenticated_actor
        )
        assert len(results) == 3
