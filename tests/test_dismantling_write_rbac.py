from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import (
    AccountingException,
    DismantlingEntry,
    JournalEntry,
    Permission,
    Role,
    RolePermission,
    StockMovement,
    UserAuditEvent,
    User,
)
from services.dismantling_service import (
    DismantlingValidationError,
    approve_dismantling_entry,
    create_dismantling_entry,
    reject_dismantling_entry,
    reverse_approved_dismantling_entry,
    submit_dismantling_for_approval,
    update_dismantling_entry,
)
from services.purchase_service import create_purchase
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_user,
    logout,
    _seed_security_catalogue,
)
from services.supplier_service import create_supplier


@pytest.fixture
def dismantling_write_context(tmp_path, authenticated_actor_factory):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'dismantling-write.db').as_posix()}"
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        _seed_security_catalogue(session)
        admin = authenticated_actor_factory(session)
        supplier = create_supplier(
            session,
            {
                "supplier_code": "SUP-WRITE-0001",
                "business_name": "Write RBAC Test Supplier",
                "phone": "9000000002",
                "is_active": True,
            },
            actor_context=admin,
        )
        purchase = create_purchase(
            session,
            {"supplier_id": supplier.id, "purchase_date": date.today()},
            [{
                "appliance_category": "Refrigerators",
                "brand": "RBAC Test",
                "quantity": 3,
                "rate": Decimal("100.00"),
            }],
            actor_context=admin,
        )
        entry = create_dismantling_entry(
            session,
            {
                "purchase_id": purchase.id,
                "purchase_item_id": purchase.items[0].id,
                "appliances_dismantled": 1,
                "input_weight": Decimal("30.00"),
                "status": "Draft",
            },
            [{
                "component_name": "RBAC Test Motor",
                "quantity": 1,
                "weight": Decimal("5.00"),
                "condition": "Working",
            }],
            [],
            actor_context=admin,
        )
        session.commit()
        yield {
            "engine": engine,
            "session": session,
            "session_factory": session_factory,
            "admin": admin,
            "entry": entry,
        }
    finally:
        session.close()
        engine.dispose()


def _role_actor(session, admin, username, role_name):
    create_user(
        session,
        actor_context=admin,
        username=username,
        password="Dismantling-write-test-2026!",
        full_name=username,
        role_names=[role_name],
    )
    session.commit()
    return authenticate(session, username, "Dismantling-write-test-2026!")


def test_write_permission_boundaries_reject_inventory_operator_before_business_reads(
    dismantling_write_context,
):
    ctx = dismantling_write_context
    session = ctx["session"]
    actor = _role_actor(
        session, ctx["admin"], "dismantling-inventory-only", "Inventory Operator"
    )
    statements = []

    def capture_sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(ctx["engine"], "before_cursor_execute", capture_sql)
    calls = [
        lambda: create_dismantling_entry(session, {}, [], [], actor_context=actor),
        lambda: update_dismantling_entry(
            session, ctx["entry"].id, {}, [], [], actor_context=actor
        ),
        lambda: submit_dismantling_for_approval(
            session, ctx["entry"].id, actor_context=actor
        ),
        lambda: approve_dismantling_entry(
            session, ctx["entry"].id, actor_context=actor
        ),
        lambda: reject_dismantling_entry(
            session, ctx["entry"].id, "Not authorized", actor_context=actor
        ),
        lambda: reverse_approved_dismantling_entry(
            session, ctx["entry"].id, "Not authorized", actor_context=actor
        ),
    ]
    try:
        for call in calls:
            with pytest.raises(AuthorizationError):
                call()
    finally:
        event.remove(ctx["engine"], "before_cursor_execute", capture_sql)

    assert not any(
        "dismantling_entries" in statement
        or "stock_movements" in statement
        or "inventory_items" in statement
        for statement in statements
    )
    assert session.get(DismantlingEntry, ctx["entry"].id).status == "Draft"
    assert session.scalar(select(StockMovement.id)) is None
    assert session.query(UserAuditEvent).filter_by(
        user_id=actor.user_id, action="authorization_denied", module="Security"
    ).count() == len(calls)


def test_approval_requires_inventory_adjust_even_for_dismantling_approver(
    dismantling_write_context,
):
    ctx = dismantling_write_context
    session = ctx["session"]
    actor = _role_actor(
        session, ctx["admin"], "dismantling-approver-no-stock", "Factory Manager"
    )
    role = session.scalar(
        select(Role).where(Role.name == "Factory Manager")
    )
    permission = session.scalar(
        select(Permission).where(Permission.code == "inventory.adjust")
    )
    session.query(RolePermission).filter_by(
        role_id=role.id, permission_id=permission.id
    ).delete()
    session.commit()

    with pytest.raises(AuthorizationError, match="inventory.adjust"):
        approve_dismantling_entry(
            session, ctx["entry"].id, actor_context=actor
        )

    assert session.get(DismantlingEntry, ctx["entry"].id).status == "Draft"
    assert session.scalar(select(StockMovement.id)) is None
    assert session.query(UserAuditEvent).filter_by(
        user_id=actor.user_id, action="authorization_denied"
    ).count() == 1


def test_write_api_rejects_missing_fabricated_and_cross_database_contexts(
    dismantling_write_context, tmp_path, authenticated_actor_factory
):
    ctx = dismantling_write_context
    session = ctx["session"]

    with pytest.raises(TypeError):
        submit_dismantling_for_approval(session, ctx["entry"].id)

    fabricated = ActorContext(
        user_id=ctx["admin"].user_id,
        username=ctx["admin"].username,
        session_id="fabricated-write-session",
        issued_at=ctx["admin"].issued_at,
        database_identity=ctx["admin"].database_identity,
    )
    with pytest.raises(AuthorizationError):
        submit_dismantling_for_approval(
            session, ctx["entry"].id, actor_context=fabricated
        )

    other_engine = create_engine(
        f"sqlite:///{(tmp_path / 'dismantling-write-other.db').as_posix()}"
    )
    Base.metadata.create_all(bind=other_engine)
    other_session = sessionmaker(
        bind=other_engine, autoflush=False, expire_on_commit=False
    )()
    try:
        _seed_security_catalogue(other_session)
        other_admin = authenticated_actor_factory(other_session)
        assert other_admin.user_id == ctx["admin"].user_id
        other_supplier = create_supplier(
            other_session,
            {
                "supplier_code": "SUP-WRITE-OTHER",
                "business_name": "Other Database Supplier",
                "phone": "9000000003",
                "is_active": True,
            },
            actor_context=other_admin,
        )
        other_purchase = create_purchase(
            other_session,
            {"supplier_id": other_supplier.id},
            [{
                "appliance_category": "Refrigerators",
                "quantity": 2,
                "rate": Decimal("50.00"),
            }],
            actor_context=other_admin,
        )
        other_entry = create_dismantling_entry(
            other_session,
            {
                "purchase_id": other_purchase.id,
                "purchase_item_id": other_purchase.items[0].id,
                "appliances_dismantled": 1,
                "status": "Draft",
            },
            [{"component_name": "Other Motor", "quantity": 1}],
            [],
            actor_context=other_admin,
        )
        other_session.commit()
        with pytest.raises(AuthorizationError, match="different database"):
            submit_dismantling_for_approval(
                other_session, other_entry.id, actor_context=ctx["admin"]
            )
        assert other_session.get(DismantlingEntry, other_entry.id).status == "Draft"
    finally:
        other_session.close()
        other_engine.dispose()


def test_write_api_accepts_independent_session_on_same_database(
    dismantling_write_context,
):
    ctx = dismantling_write_context
    independent_session = ctx["session_factory"]()
    try:
        entry = submit_dismantling_for_approval(
            independent_session,
            ctx["entry"].id,
            actor_context=ctx["admin"],
        )
        assert entry.status == "Pending Approval"
    finally:
        independent_session.close()


def test_approval_is_atomic_and_creates_only_one_accounting_source_event(
    dismantling_write_context,
):
    ctx = dismantling_write_context
    session = ctx["session"]
    approved = approve_dismantling_entry(
        session, ctx["entry"].id, actor_context=ctx["admin"]
    )
    assert approved.status == "Approved"
    event_count = (
        session.query(JournalEntry)
        .filter_by(
            source_module="Dismantling",
            source_transaction_id=ctx["entry"].id,
            source_event_type="APPROVED",
        )
        .count()
        + session.query(AccountingException)
        .filter_by(
            source_module="Dismantling",
            source_transaction_id=ctx["entry"].id,
            source_event_type="APPROVED",
        )
        .count()
    )
    assert event_count == 1

    with pytest.raises(DismantlingValidationError, match="already been approved"):
        approve_dismantling_entry(
            session, ctx["entry"].id, actor_context=ctx["admin"]
        )
    assert (
        session.query(JournalEntry)
        .filter_by(
            source_module="Dismantling",
            source_transaction_id=ctx["entry"].id,
            source_event_type="APPROVED",
        )
        .count()
        + session.query(AccountingException)
        .filter_by(
            source_module="Dismantling",
            source_transaction_id=ctx["entry"].id,
            source_event_type="APPROVED",
        )
        .count()
    ) == 1


def test_write_api_rejects_expired_logged_out_and_inactive_actors(
    dismantling_write_context,
):
    ctx = dismantling_write_context
    session = ctx["session"]
    expired = authenticate(
        session,
        ctx["admin"].username,
        "Isolated-test-admin-2026!",
        timeout_minutes=0,
    )
    with pytest.raises(AuthorizationError):
        submit_dismantling_for_approval(
            session, ctx["entry"].id, actor_context=expired
        )

    logged_out = authenticate(
        session, ctx["admin"].username, "Isolated-test-admin-2026!"
    )
    logout(session, actor_context=logged_out)
    with pytest.raises(AuthorizationError):
        submit_dismantling_for_approval(
            session, ctx["entry"].id, actor_context=logged_out
        )

    session.get(User, ctx["admin"].user_id).is_active = False
    session.commit()
    with pytest.raises(AuthorizationError):
        submit_dismantling_for_approval(
            session, ctx["entry"].id, actor_context=ctx["admin"]
        )
