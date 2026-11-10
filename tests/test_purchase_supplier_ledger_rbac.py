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
    RolePermission,
    Supplier,
    User,
    UserAuditEvent,
    UserRole,
)
from services.purchase_service import get_supplier_purchase_ledger_balance
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_first_administrator,
    create_user,
    logout,
)


def _create_supplier_ledger(session, *, opening_balance=Decimal("5000.00")):
    supplier = Supplier(
        supplier_code="SUP-PUR-LEDGER-0001",
        business_name="Purchase Ledger Supplier",
        phone="9000000301",
        opening_balance=opening_balance,
    )
    session.add(supplier)
    session.flush()
    partially_paid = Purchase(
        purchase_code="PUR-LEDGER-000001",
        supplier_id=supplier.id,
        purchase_date=date.today(),
        subtotal=Decimal("6500.00"),
        total_amount=Decimal("7000.00"),
        amount_paid=Decimal("3000.00"),
        balance_amount=Decimal("4000.00"),
        payment_status="Partially Paid",
    )
    fully_paid = Purchase(
        purchase_code="PUR-LEDGER-000002",
        supplier_id=supplier.id,
        purchase_date=date.today(),
        subtotal=Decimal("1000.00"),
        total_amount=Decimal("1000.00"),
        amount_paid=Decimal("1000.00"),
        balance_amount=Decimal("0.00"),
        payment_status="Paid",
    )
    unpaid = Purchase(
        purchase_code="PUR-LEDGER-000003",
        supplier_id=supplier.id,
        purchase_date=date.today(),
        subtotal=Decimal("1000.00"),
        total_amount=Decimal("1000.00"),
        amount_paid=Decimal("0.00"),
        balance_amount=Decimal("1000.00"),
        payment_status="Unpaid",
    )
    session.add_all([partially_paid, fully_paid, unpaid])
    session.commit()
    return supplier, partially_paid, fully_paid, unpaid


def test_procurement_reads_purchase_ledger_and_existing_financial_calculation(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier, partially_paid, fully_paid, unpaid = _create_supplier_ledger(
            session
        )
        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-ledger-procurement",
            password="Purchase-ledger-procurement-2026!",
            full_name="Purchase Ledger Procurement",
            role_names=["Procurement"],
        )
        session.commit()
        actor = authenticate(
            session, procurement.username, "Purchase-ledger-procurement-2026!"
        )

        assert (
            signature(get_supplier_purchase_ledger_balance)
            .parameters["actor_context"]
            .kind
            is Parameter.KEYWORD_ONLY
        )
        outstanding, purchases = get_supplier_purchase_ledger_balance(
            session, supplier.id, actor_context=actor
        )
        assert outstanding == Decimal("10000.00")
        assert [purchase.id for purchase in purchases] == [
            unpaid.id,
            fully_paid.id,
            partially_paid.id,
        ]
        assert sum(
            (purchase.balance_amount for purchase in purchases),
            Decimal("0.00"),
        ) == Decimal("5000.00")
        assert sum(
            (purchase.amount_paid for purchase in purchases),
            Decimal("0.00"),
        ) == Decimal("4000.00")
        logout(session, actor_context=actor)


def test_ledger_preserves_missing_supplier_and_no_purchase_behavior(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier = Supplier(
            supplier_code="SUP-PUR-LEDGER-EMPTY",
            business_name="No Purchases Supplier",
            phone="9000000302",
            opening_balance=Decimal("17.50"),
        )
        session.add(supplier)
        session.commit()
        assert get_supplier_purchase_ledger_balance(
            session, supplier.id, actor_context=authenticated_actor
        ) == (Decimal("17.50"), [])
        assert get_supplier_purchase_ledger_balance(
            session, -1, actor_context=authenticated_actor
        ) == (Decimal("0.00"), [])


def test_ledger_rejects_untrusted_or_unauthorized_actors_before_data_queries(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier, _, _, _ = _create_supplier_ledger(session)
        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-ledger-clerk",
            password="Purchase-ledger-clerk-2026!",
            full_name="Purchase Ledger Clerk",
            role_names=["HR/Employee Manager"],
        )
        session.commit()
        denied_actor = authenticate(
            session, clerk.username, "Purchase-ledger-clerk-2026!"
        )
        fabricated_actor = ActorContext(
            denied_actor.user_id,
            denied_actor.username,
            "fabricated-purchase-ledger-context",
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
            username="purchase-ledger-inactive",
            password="Purchase-ledger-inactive-2026!",
            full_name="Purchase Ledger Inactive",
            role_names=["Procurement"],
        )
        session.commit()
        inactive_actor = authenticate(
            session, inactive_user.username, "Purchase-ledger-inactive-2026!"
        )
        session.get(User, inactive_user.id).is_active = False
        session.commit()

        revoked_user = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-ledger-revoked",
            password="Purchase-ledger-revoked-2026!",
            full_name="Purchase Ledger Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        revoked_actor = authenticate(
            session, revoked_user.username, "Purchase-ledger-revoked-2026!"
        )
        purchase_view_id = session.scalar(
            select(Permission.id).where(Permission.code == "purchases.view")
        )
        for role_id in session.scalars(
            select(UserRole.role_id).where(UserRole.user_id == revoked_user.id)
        ):
            session.query(RolePermission).filter_by(
                role_id=role_id, permission_id=purchase_view_id
            ).delete()
        session.commit()

        with pytest.raises(TypeError):
            signature(get_supplier_purchase_ledger_balance).bind(
                session, supplier.id
            )

        protected_selects = []

        def capture_ledger_data_queries(
            connection, cursor, statement, parameters, context, executemany
        ):
            sql = statement.lower()
            if " from suppliers " in sql or " from purchases " in sql:
                protected_selects.append(sql)

        event.listen(
            session.get_bind(), "before_cursor_execute", capture_ledger_data_queries
        )
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
                    get_supplier_purchase_ledger_balance(
                        session, supplier.id, actor_context=actor
                    )
        finally:
            event.remove(
                session.get_bind(),
                "before_cursor_execute",
                capture_ledger_data_queries,
            )

        assert protected_selects == []
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


def test_ledger_rejects_cross_database_actor_when_user_ids_collide(
    authenticated_actor, tmp_path
):
    other_engine = create_engine(
        f"sqlite:///{(tmp_path / 'other-purchase-ledger.db').as_posix()}"
    )
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(
        bind=other_engine, autoflush=False, expire_on_commit=False
    )
    try:
        with OtherSession() as session:
            other_user = create_first_administrator(
                session,
                username="other-purchase-ledger-admin",
                password="Other-purchase-ledger-admin-2026!",
                full_name="Other Purchase Ledger Admin",
            )
            session.flush()
            supplier, _, _, _ = _create_supplier_ledger(session)
            assert other_user.id == authenticated_actor.user_id
            with pytest.raises(AuthorizationError):
                get_supplier_purchase_ledger_balance(
                    session, supplier.id, actor_context=authenticated_actor
                )
    finally:
        other_engine.dispose()


def test_ledger_supports_independent_sessions_on_same_database(authenticated_actor):
    with database_connection.get_db() as session:
        supplier, _, _, _ = _create_supplier_ledger(session)
        supplier_id = supplier.id
    with database_connection.get_db() as independent_session:
        outstanding, purchases = get_supplier_purchase_ledger_balance(
            independent_session,
            supplier_id,
            actor_context=authenticated_actor,
        )
        assert outstanding == Decimal("10000.00")
        assert len(purchases) == 3
