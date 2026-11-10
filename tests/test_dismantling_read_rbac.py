from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import (
    DismantlingEntry,
    InventoryItem,
    Permission,
    Role,
    RolePermission,
    StockMovement,
    User,
    UserAuditEvent,
)
from services.dismantling_report_service import (
    generate_dismantling_excel_report,
    generate_dismantling_pdf_report,
)
from services.dismantling_service import (
    create_dismantling_entry,
    generate_next_dismantling_code,
    generate_next_part_code,
    get_dismantling_by_code,
    get_dismantling_metrics,
    get_purchases_with_undismantled_items,
    get_recent_dismantling_entries,
    get_remaining_undismantled_quantity,
    list_dismantling_entries,
)
from services.purchase_service import create_purchase
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_first_administrator,
    create_user,
    logout,
    _seed_security_catalogue,
)
from services.supplier_service import create_supplier


@pytest.fixture
def dismantling_read_context(tmp_path, authenticated_actor_factory):
    engine = create_engine(f"sqlite:///{(tmp_path / 'dismantling-read.db').as_posix()}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        _seed_security_catalogue(session)
        actor = authenticated_actor_factory(session)
        supplier = create_supplier(
            session,
            {
                "supplier_code": "SUP-READ-0001",
                "business_name": "RBAC Read Test Supplier",
                "phone": "9000000001",
                "is_active": True,
            },
            actor_context=actor,
        )
        purchase = create_purchase(
            session,
            {"supplier_id": supplier.id},
            [{
                "appliance_category": "Refrigerators",
                "brand": "Test Brand",
                "quantity": 3,
                "rate": Decimal("100.00"),
            }],
            actor_context=actor,
        )
        entry = create_dismantling_entry(
            session,
            {
                "purchase_id": purchase.id,
                "purchase_item_id": purchase.items[0].id,
                "appliances_dismantled": 2,
                "input_weight": Decimal("60.00"),
                "status": "Draft",
            },
            [{
                "component_name": "Recovered Test Motor",
                "quantity": 2,
                "weight": Decimal("12.00"),
                "condition": "Working",
            }],
            [],
            actor_context=actor,
        )
        session.commit()
        yield {
            "engine": engine,
            "session": session,
            "session_factory": session_factory,
            "actor": actor,
            "purchase": purchase,
            "entry": entry,
        }
    finally:
        session.close()
        engine.dispose()


def _create_role_actor(session, admin_actor, username, role_name):
    user = create_user(
        session,
        actor_context=admin_actor,
        username=username,
        password="Read-test-password-2026!",
        full_name=username,
        role_names=[role_name],
    )
    session.commit()
    return user, authenticate(session, username, "Read-test-password-2026!")


def test_authorized_dismantling_and_inventory_read_apis_preserve_results(
    dismantling_read_context, tmp_path
):
    ctx = dismantling_read_context
    session = ctx["session"]
    _, actor = _create_role_actor(
        session, ctx["actor"], "dismantling-inventory-reader", "Inventory Operator"
    )
    entry = ctx["entry"]

    assert generate_next_dismantling_code(session, actor_context=actor) == "DIS-000002"
    assert generate_next_part_code(session, actor_context=actor) == "PART-000001"
    assert get_remaining_undismantled_quantity(
        session, ctx["purchase"].items[0].id, actor_context=actor
    ) == 1
    available = get_purchases_with_undismantled_items(
        session, actor_context=actor
    )
    assert len(available) == 1
    assert available[0]["purchase_code"] == ctx["purchase"].purchase_code
    assert available[0]["available_items"][0]["remaining_quantity"] == 1

    assert get_dismantling_by_code(
        session, entry.dismantling_code, actor_context=actor
    ).id == entry.id
    assert get_dismantling_by_code(
        session, "DIS-DOES-NOT-EXIST", actor_context=actor
    ) is None
    listed = list_dismantling_entries(
        session,
        search=ctx["purchase"].purchase_code,
        start_date=entry.dismantling_date,
        end_date=entry.dismantling_date,
        category_filter="Refrigerators",
        status_filter="Draft",
        sort_by="date_desc",
        actor_context=actor,
    )
    assert [row.id for row in listed] == [entry.id]
    assert list_dismantling_entries(
        session,
        start_date=date(2099, 1, 1),
        actor_context=actor,
    ) == []
    assert get_recent_dismantling_entries(
        session, limit=1, actor_context=actor
    ) == [entry]
    assert get_recent_dismantling_entries(
        session, limit=0, actor_context=actor
    ) == []

    metrics = get_dismantling_metrics(session, actor_context=actor)
    assert metrics["total_appliances_purchased"] == 3
    assert metrics["total_dismantling_batches"] == 1
    assert metrics["total_recovered_components"] == Decimal("0.00")
    assert get_dismantling_metrics(
        session,
        start_date=date(2099, 1, 1),
        end_date=date(2099, 12, 31),
        actor_context=actor,
    )["total_dismantling_batches"] == 0

    pdf_path = tmp_path / "authorized-report.pdf"
    excel_path = tmp_path / "authorized-report.xlsx"
    assert generate_dismantling_pdf_report(
        session, str(pdf_path), actor_context=actor
    ) == str(pdf_path.resolve())
    assert generate_dismantling_excel_report(
        session, str(excel_path), actor_context=actor
    ) == str(excel_path.resolve())
    assert pdf_path.exists() and excel_path.exists()

    session.expire_all()
    persisted = session.get(DismantlingEntry, entry.id)
    assert persisted.status == "Draft"
    assert persisted.is_inventory_updated is False
    assert session.scalars(select(InventoryItem)).all() == []
    assert session.scalars(select(StockMovement)).all() == []


def test_all_public_dismantling_read_apis_require_actor_context(
    dismantling_read_context, tmp_path
):
    session = dismantling_read_context["session"]
    entry = dismantling_read_context["entry"]
    calls = [
        lambda: generate_next_dismantling_code(session),
        lambda: generate_next_part_code(session),
        lambda: get_remaining_undismantled_quantity(
            session, dismantling_read_context["purchase"].items[0].id
        ),
        lambda: get_purchases_with_undismantled_items(session),
        lambda: get_dismantling_by_code(session, entry.dismantling_code),
        lambda: list_dismantling_entries(session),
        lambda: get_dismantling_metrics(session),
        lambda: get_recent_dismantling_entries(session),
        lambda: generate_dismantling_pdf_report(
            session, str(tmp_path / "missing-actor.pdf")
        ),
        lambda: generate_dismantling_excel_report(
            session, str(tmp_path / "missing-actor.xlsx")
        ),
    ]
    for call in calls:
        with pytest.raises(TypeError):
            call()


def test_denied_read_apis_audit_and_query_no_protected_business_data(
    dismantling_read_context, tmp_path
):
    ctx = dismantling_read_context
    session = ctx["session"]
    _, denied_actor = _create_role_actor(
        session, ctx["actor"], "dismantling-hr-reader", "HR/Employee Manager"
    )
    entry = ctx["entry"]
    item_id = ctx["purchase"].items[0].id
    outputs = [
        lambda: generate_next_dismantling_code(session, actor_context=denied_actor),
        lambda: generate_next_part_code(session, actor_context=denied_actor),
        lambda: get_remaining_undismantled_quantity(
            session, item_id, actor_context=denied_actor
        ),
        lambda: get_purchases_with_undismantled_items(
            session, actor_context=denied_actor
        ),
        lambda: get_dismantling_by_code(
            session, entry.dismantling_code, actor_context=denied_actor
        ),
        lambda: list_dismantling_entries(session, actor_context=denied_actor),
        lambda: get_dismantling_metrics(session, actor_context=denied_actor),
        lambda: get_recent_dismantling_entries(
            session, actor_context=denied_actor
        ),
        lambda: generate_dismantling_pdf_report(
            session, str(tmp_path / "denied.pdf"), actor_context=denied_actor
        ),
        lambda: generate_dismantling_excel_report(
            session, str(tmp_path / "denied.xlsx"), actor_context=denied_actor
        ),
    ]
    statements = []

    def record_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(ctx["engine"], "before_cursor_execute", record_statement)
    try:
        for call in outputs:
            with pytest.raises(AuthorizationError):
                call()
    finally:
        event.remove(ctx["engine"], "before_cursor_execute", record_statement)

    protected_tables = (
        "dismantling_entries",
        "recovered_components",
        "scrap_materials",
        "purchase_items",
        "purchases",
        "inventory_items",
    )
    assert not any(
        "select" in statement and any(table in statement for table in protected_tables)
        for statement in statements
    )
    assert session.query(UserAuditEvent).filter_by(
        action="authorization_denied"
    ).count() == len(outputs)
    assert not (tmp_path / "denied.pdf").exists()
    assert not (tmp_path / "denied.xlsx").exists()


def test_dismantling_reads_reject_invalid_actor_contexts(
    dismantling_read_context, tmp_path
):
    ctx = dismantling_read_context
    session = ctx["session"]
    actor = ctx["actor"]
    entry = ctx["entry"]

    fabricated = ActorContext(
        actor.user_id,
        actor.username,
        "fabricated-dismantling-session",
        actor.issued_at,
        actor.database_identity,
    )
    with pytest.raises(AuthorizationError):
        get_dismantling_by_code(
            session, entry.dismantling_code, actor_context=fabricated
        )

    expired = authenticate(
        session, "isolated-admin-1", "Isolated-test-admin-2026!",
        timeout_minutes=0,
    )
    with pytest.raises(AuthorizationError):
        list_dismantling_entries(session, actor_context=expired)

    logged_out = authenticate(session, "isolated-admin-1", "Isolated-test-admin-2026!")
    logout(session, actor_context=logged_out)
    with pytest.raises(AuthorizationError):
        get_dismantling_metrics(session, actor_context=logged_out)

    session.get(User, actor.user_id).is_active = False
    session.commit()
    with pytest.raises(AuthorizationError):
        get_recent_dismantling_entries(session, actor_context=actor)

    other_engine = create_engine(
        f"sqlite:///{(tmp_path / 'another-dismantling-db.db').as_posix()}"
    )
    Base.metadata.create_all(bind=other_engine)
    other_session = sessionmaker(bind=other_engine)()
    try:
        other_admin = create_first_administrator(
            other_session,
            username="other-dismantling-admin",
            password="Other-admin-password-2026!",
            full_name="Other Database Admin",
        )
        other_session.commit()
        assert other_admin.id == actor.user_id
        with pytest.raises(AuthorizationError):
            get_purchases_with_undismantled_items(
                other_session, actor_context=actor
            )
    finally:
        other_session.close()
        other_engine.dispose()


def test_dismantling_read_apis_accept_independent_same_database_sessions(
    dismantling_read_context,
):
    ctx = dismantling_read_context
    independent_session = ctx["session_factory"]()
    try:
        entry = get_dismantling_by_code(
            independent_session,
            ctx["entry"].dismantling_code,
            actor_context=ctx["actor"],
        )
        assert entry is not None
        assert entry.id == ctx["entry"].id
        assert get_dismantling_metrics(
            independent_session, actor_context=ctx["actor"]
        )["total_dismantling_batches"] == 1
    finally:
        independent_session.close()


def test_revoked_dismantling_view_permission_is_denied_and_audited(
    dismantling_read_context,
):
    ctx = dismantling_read_context
    session = ctx["session"]
    _, actor = _create_role_actor(
        session, ctx["actor"], "dismantling-revoked-reader", "Inventory Operator"
    )
    role = session.scalar(select(Role).where(Role.name == "Inventory Operator"))
    permission = session.scalar(
        select(Permission).where(Permission.code == "dismantling.view")
    )
    grant = session.scalar(
        select(RolePermission).where(
            RolePermission.role_id == role.id,
            RolePermission.permission_id == permission.id,
        )
    )
    assert grant is not None
    session.delete(grant)
    session.commit()

    with pytest.raises(AuthorizationError):
        list_dismantling_entries(session, actor_context=actor)
    assert session.query(UserAuditEvent).filter_by(
        action="authorization_denied"
    ).count() == 1
