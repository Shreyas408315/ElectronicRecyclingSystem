from decimal import Decimal
from inspect import Parameter, signature

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

import database.connection as database_connection
from database.connection import Base
from database.models import (
    AccountingException,
    DismantlingEntry,
    InventoryItem,
    JournalEntry,
    Permission,
    Purchase,
    PurchaseItem,
    RolePermission,
    StockMovement,
    Supplier,
    UserAuditEvent,
    User,
    UserRole,
)
from services.purchase_service import create_purchase
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_first_administrator,
    create_user,
    logout,
)
from services.supplier_service import create_supplier


PURCHASE_ITEMS = [
    {
        "appliance_category": "Refrigerators",
        "brand": "RBAC Test Brand",
        "model_number": "R-100",
        "quantity": 2,
        "weight": Decimal("40.00"),
        "unit": "pieces",
        "rate": Decimal("100.00"),
    }
]


def _create_active_supplier(session, actor_context):
    return create_supplier(
        session,
        {
            "business_name": "Purchase Creation RBAC Supplier",
            "phone": "9000000401",
            "is_active": True,
            "opening_balance": Decimal("25.00"),
        },
        actor_context=actor_context,
    )


def test_procurement_creates_purchase_with_existing_calculations_and_dependencies(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier = _create_active_supplier(session, authenticated_actor)
        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-create-procurement",
            password="Purchase-create-procurement-2026!",
            full_name="Purchase Create Procurement",
            role_names=["Procurement"],
        )
        session.commit()
        actor = authenticate(
            session, procurement.username, "Purchase-create-procurement-2026!"
        )

        assert (
            signature(create_purchase).parameters["actor_context"].kind
            is Parameter.KEYWORD_ONLY
        )
        purchase = create_purchase(
            session,
            {
                "supplier_id": supplier.id,
                "transportation_charges": Decimal("10.00"),
                "loading_charges": Decimal("5.00"),
                "unloading_charges": Decimal("2.00"),
                "other_charges": Decimal("3.00"),
                "amount_paid": Decimal("50.00"),
                "payment_method": "UPI",
            },
            PURCHASE_ITEMS,
            actor_context=actor,
        )
        session.flush()

        assert purchase.purchase_code == "PUR-000001"
        assert purchase.supplier_id == supplier.id
        assert purchase.subtotal == Decimal("200.00")
        assert purchase.total_amount == Decimal("220.00")
        assert purchase.amount_paid == Decimal("50.00")
        assert purchase.balance_amount == Decimal("170.00")
        assert purchase.payment_status == "Partially Paid"
        assert len(purchase.items) == 1
        assert purchase.items[0].quantity == 2
        assert purchase.items[0].line_total == Decimal("200.00")

        assert session.scalar(select(InventoryItem.id)) is None
        assert session.scalar(select(StockMovement.id)) is None
        journal = session.scalar(
            select(JournalEntry.id).where(
                JournalEntry.source_module == "Purchase",
                JournalEntry.source_event_type == "CREATED",
                JournalEntry.source_transaction_id == purchase.id,
            )
        )
        accounting_exception = session.scalar(
            select(AccountingException.id).where(
                AccountingException.source_module == "Purchase",
                AccountingException.source_event_type == "CREATED",
                AccountingException.source_transaction_id == purchase.id,
            )
        )
        assert (journal is None) != (accounting_exception is None)
        logout(session, actor_context=actor)


def test_role_without_purchase_post_is_denied_before_reads_or_mutations(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier = _create_active_supplier(session, authenticated_actor)
        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-create-clerk",
            password="Purchase-create-clerk-2026!",
            full_name="Purchase Create Clerk",
            role_names=["HR/Employee Manager"],
        )
        finance_user = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-create-finance",
            password="Purchase-create-finance-2026!",
            full_name="Purchase Create Finance",
            role_names=["Accounts & Finance"],
        )
        session.commit()
        actor = authenticate(
            session, clerk.username, "Purchase-create-clerk-2026!"
        )
        finance_actor = authenticate(
            session, finance_user.username, "Purchase-create-finance-2026!"
        )
        supplier_before = (supplier.business_name, supplier.opening_balance, supplier.is_active)
        initial_counts = {
            model: session.scalar(select(func.count(model.id)))
            for model in (
                Purchase,
                PurchaseItem,
                JournalEntry,
                AccountingException,
                InventoryItem,
                StockMovement,
                DismantlingEntry,
            )
        }

        protected_reads = []

        def capture_business_reads(connection, cursor, statement, parameters, context, executemany):
            sql = statement.lower()
            if any(f" from {table} " in sql for table in ("suppliers", "purchases")):
                protected_reads.append(sql)

        event.listen(
            session.get_bind(), "before_cursor_execute", capture_business_reads
        )
        try:
            for denied_actor in (actor, finance_actor):
                with pytest.raises(AuthorizationError):
                    create_purchase(
                        session,
                        {"supplier_id": supplier.id},
                        PURCHASE_ITEMS,
                        actor_context=denied_actor,
                    )
        finally:
            event.remove(
                session.get_bind(), "before_cursor_execute", capture_business_reads
            )

        assert protected_reads == []
        assert (supplier.business_name, supplier.opening_balance, supplier.is_active) == supplier_before
        assert all(
            session.scalar(select(func.count(model.id))) == initial_counts[model]
            for model in initial_counts
        )
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == clerk.id,
                UserAuditEvent.action == "authorization_denied",
                UserAuditEvent.module == "Security",
            )
        ) is not None
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == finance_user.id,
                UserAuditEvent.action == "authorization_denied",
                UserAuditEvent.module == "Security",
            )
        ) is not None
        logout(session, actor_context=actor)
        logout(session, actor_context=finance_actor)


def test_create_purchase_requires_trusted_actor_and_protected_supplier_permission(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier = _create_active_supplier(session, authenticated_actor)
        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-create-no-supplier-view",
            password="Purchase-create-no-supplier-view-2026!",
            full_name="Purchase Create No Supplier View",
            role_names=["Procurement"],
        )
        session.commit()
        actor = authenticate(
            session,
            procurement.username,
            "Purchase-create-no-supplier-view-2026!",
        )
        with pytest.raises(TypeError):
            signature(create_purchase).bind(session, {"supplier_id": supplier.id}, PURCHASE_ITEMS)

        fabricated = ActorContext(
            actor.user_id,
            actor.username,
            "fabricated-purchase-create-context",
            actor.issued_at,
            actor.database_identity,
        )
        with pytest.raises(AuthorizationError):
            create_purchase(
                session,
                {"supplier_id": supplier.id},
                PURCHASE_ITEMS,
                actor_context=fabricated,
            )

        supplier_view_id = session.scalar(
            select(Permission.id).where(Permission.code == "suppliers.view")
        )
        role_ids = session.scalars(
            select(UserRole.role_id).where(UserRole.user_id == procurement.id)
        )
        for role_id in role_ids:
            session.query(RolePermission).filter_by(
                role_id=role_id, permission_id=supplier_view_id
            ).delete()
        session.commit()

        supplier_queries = []

        def capture_supplier_reads(connection, cursor, statement, parameters, context, executemany):
            if " from suppliers " in statement.lower():
                supplier_queries.append(statement)

        event.listen(session.get_bind(), "before_cursor_execute", capture_supplier_reads)
        try:
            with pytest.raises(AuthorizationError):
                create_purchase(
                    session,
                    {"supplier_id": supplier.id},
                    PURCHASE_ITEMS,
                    actor_context=actor,
                )
        finally:
            event.remove(
                session.get_bind(), "before_cursor_execute", capture_supplier_reads
            )

        assert supplier_queries == []
        assert session.scalar(select(Purchase.id)) is None
        assert session.scalar(select(PurchaseItem.id)) is None
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == procurement.id,
                UserAuditEvent.action == "authorization_denied",
            )
        ) is not None
        logout(session, actor_context=actor)


def test_create_purchase_rejects_expired_logged_out_inactive_and_revoked_actors(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier = _create_active_supplier(session, authenticated_actor)
        expired = authenticate(
            session,
            "fixture-admin",
            "Fixture-admin-password-2026!",
            timeout_minutes=0,
        )
        logged_out = authenticate(
            session, "fixture-admin", "Fixture-admin-password-2026!"
        )
        logout(session, actor_context=logged_out)

        inactive_user = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-create-inactive",
            password="Purchase-create-inactive-2026!",
            full_name="Purchase Create Inactive",
            role_names=["Procurement"],
        )
        session.commit()
        inactive = authenticate(
            session,
            inactive_user.username,
            "Purchase-create-inactive-2026!",
        )
        session.get(User, inactive_user.id).is_active = False
        session.commit()

        revoked_user = create_user(
            session,
            actor_context=authenticated_actor,
            username="purchase-create-revoked",
            password="Purchase-create-revoked-2026!",
            full_name="Purchase Create Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        revoked = authenticate(
            session, revoked_user.username, "Purchase-create-revoked-2026!"
        )
        post_permission_id = session.scalar(
            select(Permission.id).where(Permission.code == "purchases.post")
        )
        for role_id in session.scalars(
            select(UserRole.role_id).where(UserRole.user_id == revoked_user.id)
        ):
            session.query(RolePermission).filter_by(
                role_id=role_id, permission_id=post_permission_id
            ).delete()
        session.commit()

        for actor in (expired, logged_out, inactive, revoked):
            with pytest.raises(AuthorizationError):
                create_purchase(
                    session,
                    {"supplier_id": supplier.id},
                    PURCHASE_ITEMS,
                    actor_context=actor,
                )
        assert session.scalar(select(Purchase.id)) is None

    for actor in (expired, logged_out, inactive, revoked):
        logout(actor_context=actor)


def test_create_purchase_rejects_cross_database_actor_with_colliding_user_id(
    authenticated_actor, tmp_path
):
    other_engine = create_engine(
        f"sqlite:///{(tmp_path / 'other-create-purchase.db').as_posix()}"
    )
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(
        bind=other_engine, autoflush=False, expire_on_commit=False
    )
    try:
        with OtherSession() as session:
            other_admin = create_first_administrator(
                session,
                username="other-create-purchase-admin",
                password="Other-create-purchase-admin-2026!",
                full_name="Other Create Purchase Admin",
            )
            session.flush()
            supplier = Supplier(
                supplier_code="SUP-OTHER-CREATE-0001",
                business_name="Other Create Supplier",
                phone="9000000402",
            )
            session.add(supplier)
            session.commit()
            assert other_admin.id == authenticated_actor.user_id
            with pytest.raises(AuthorizationError):
                create_purchase(
                    session,
                    {"supplier_id": supplier.id},
                    PURCHASE_ITEMS,
                    actor_context=authenticated_actor,
                )
            assert session.scalar(select(Purchase.id)) is None
            assert session.scalar(select(PurchaseItem.id)) is None
    finally:
        other_engine.dispose()


def test_create_purchase_accepts_independent_same_database_session(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier = _create_active_supplier(session, authenticated_actor)
        supplier_id = supplier.id
    with database_connection.get_db() as independent_session:
        purchase = create_purchase(
            independent_session,
            {"supplier_id": supplier_id},
            PURCHASE_ITEMS,
            actor_context=authenticated_actor,
        )
        assert purchase.purchase_code == "PUR-000001"
        assert purchase.supplier_id == supplier_id


def test_post_flush_failure_rolls_back_purchase_and_items(
    authenticated_actor, monkeypatch
):
    with database_connection.get_db() as session:
        supplier = _create_active_supplier(session, authenticated_actor)
        supplier_id = supplier.id

    def fail_accounting_posting(*args, **kwargs):
        raise RuntimeError("Simulated accounting posting failure")

    monkeypatch.setattr(
        "services.accounting_posting_service.capture_source_event",
        fail_accounting_posting,
    )
    with pytest.raises(RuntimeError, match="Simulated accounting posting failure"):
        with database_connection.get_db() as session:
            create_purchase(
                session,
                {"supplier_id": supplier_id},
                PURCHASE_ITEMS,
                actor_context=authenticated_actor,
            )

    with database_connection.get_db() as session:
        assert session.scalar(select(Purchase.id)) is None
        assert session.scalar(select(PurchaseItem.id)) is None
        supplier = session.get(Supplier, supplier_id)
        assert supplier is not None
        assert supplier.opening_balance == Decimal("25.00")
        assert session.scalar(select(InventoryItem.id)) is None
        assert session.scalar(select(StockMovement.id)) is None
