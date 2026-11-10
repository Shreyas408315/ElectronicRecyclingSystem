from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import (
    AccountingException,
    InventoryItem,
    JournalEntry,
    Permission,
    Role,
    RolePermission,
    StockMovement,
    User,
    UserAuditEvent,
)
from services.inventory_report_service import (
    generate_inventory_excel_report,
    generate_inventory_pdf_report,
)
from services.inventory_service import (
    _apply_stock_change,
    InventoryValidationError,
    create_inventory_item,
    deactivate_inventory_item,
    generate_next_part_code,
    get_inventory_categories,
    get_inventory_item,
    get_inventory_metrics,
    get_inventory_movements,
    get_inventory_source_references,
    list_inventory_items,
    record_stock_movement,
    transfer_inventory_location,
    update_inventory_item,
)
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_user,
    logout,
    _seed_security_catalogue,
)


@pytest.fixture
def inventory_rbac_context(tmp_path, authenticated_actor_factory):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'inventory-rbac.db').as_posix()}"
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        _seed_security_catalogue(session)
        admin = authenticated_actor_factory(session)
        item = create_inventory_item(
            session,
            {
                "part_name": "RBAC Copper Coil",
                "category": "Recyclable Raw Materials",
                "unit": "kg",
                "condition": "Recyclable Raw Material",
                "storage_warehouse": "Main",
            },
            actor_context=admin,
        )
        session.commit()
        yield {
            "engine": engine,
            "session": session,
            "session_factory": session_factory,
            "admin": admin,
            "item": item,
        }
    finally:
        session.close()
        engine.dispose()


def _role_actor(session, admin, username, role_name):
    create_user(
        session,
        actor_context=admin,
        username=username,
        password="Inventory-rbac-test-2026!",
        full_name=username,
        role_names=[role_name],
    )
    session.commit()
    return authenticate(
        session, username, "Inventory-rbac-test-2026!"
    )


def test_inventory_permissions_separate_view_manage_and_adjust(inventory_rbac_context):
    ctx = inventory_rbac_context
    session = ctx["session"]
    operator = _role_actor(
        session,
        ctx["admin"],
        "inventory-rbac-operator",
        "Inventory Operator",
    )
    auditor = _role_actor(
        session,
        ctx["admin"],
        "inventory-rbac-auditor",
        "Read-only Auditor",
    )

    assert get_inventory_item(
        session, ctx["item"].id, actor_context=operator
    ).id == ctx["item"].id
    assert get_inventory_item(
        session, ctx["item"].id, actor_context=auditor
    ).id == ctx["item"].id

    movement = record_stock_movement(
        session,
        ctx["item"].id,
        "ADJUSTMENT_IN",
        "4",
        "8",
        reason="Authorized stock count",
        user_responsible="Inventory Operator",
        actor_context=operator,
    )
    assert movement.new_quantity == Decimal("4.00")
    assert transfer_inventory_location(
        session,
        ctx["item"].id,
        {"storage_warehouse": "Annex"},
        reason="Authorized location transfer",
        user_responsible="Inventory Operator",
        actor_context=operator,
    ).movement_type == "TRANSFER"

    with pytest.raises(AuthorizationError, match="inventory.adjust"):
        record_stock_movement(
            session,
            ctx["item"].id,
            "ISSUE",
            "1",
            reason="Unauthorized stock change",
            user_responsible="Auditor",
            actor_context=auditor,
        )
    with pytest.raises(AuthorizationError, match="inventory.adjust"):
        transfer_inventory_location(
            session,
            ctx["item"].id,
            {"storage_warehouse": "Unauthorized"},
            reason="Unauthorized location transfer",
            user_responsible="Auditor",
            actor_context=auditor,
        )
    with pytest.raises(AuthorizationError, match="inventory.manage"):
        create_inventory_item(
            session,
            {"part_name": "Unauthorized item", "category": "Components"},
            actor_context=operator,
        )
    with pytest.raises(AuthorizationError, match="inventory.manage"):
        generate_next_part_code(session, actor_context=operator)
    with pytest.raises(AuthorizationError, match="inventory.manage"):
        update_inventory_item(
            session,
            ctx["item"].id,
            {"part_name": "Unauthorized edit"},
            actor_context=operator,
        )
    with pytest.raises(AuthorizationError, match="inventory.manage"):
        deactivate_inventory_item(
            session, ctx["item"].id, actor_context=operator
        )

    manager = get_inventory_item(
        session, ctx["item"].id, actor_context=ctx["admin"]
    )
    update_inventory_item(
        session,
        manager.id,
        {"part_name": "Managed Copper Coil"},
        actor_context=ctx["admin"],
    )
    assert get_inventory_item(
        session, manager.id, actor_context=ctx["admin"]
    ).part_name == "Managed Copper Coil"

    # A distinct zero-stock catalogue record can be deactivated by a manager.
    empty_item = create_inventory_item(
        session,
        {"part_name": "Empty managed item", "category": "Components"},
        actor_context=ctx["admin"],
    )
    deactivate_inventory_item(
        session, empty_item.id, actor_context=ctx["admin"]
    )
    assert session.get(InventoryItem, empty_item.id).is_active is False


def test_inventory_api_missing_actor_context_is_rejected(
    inventory_rbac_context, tmp_path
):
    ctx = inventory_rbac_context
    session = ctx["session"]
    item_id = ctx["item"].id
    no_actor_calls = [
        lambda: generate_next_part_code(session),
        lambda: create_inventory_item(session, {"part_name": "x", "category": "x"}),
        lambda: update_inventory_item(session, item_id, {"part_name": "x"}),
        lambda: deactivate_inventory_item(session, item_id),
        lambda: record_stock_movement(
            session, item_id, "ISSUE", "1", reason="Missing actor", user_responsible="x"
        ),
        lambda: transfer_inventory_location(
            session, item_id, {"storage_location": "x"},
            reason="Missing actor", user_responsible="x",
        ),
        lambda: list_inventory_items(session),
        lambda: get_inventory_item(session, item_id),
        lambda: get_inventory_movements(session),
        lambda: get_inventory_metrics(session),
        lambda: get_inventory_source_references(session, item_id),
        lambda: get_inventory_categories(session),
        lambda: generate_inventory_pdf_report(
            session, str(tmp_path / "missing.pdf")
        ),
        lambda: generate_inventory_excel_report(
            session, str(tmp_path / "missing.xlsx")
        ),
        lambda: _apply_stock_change(
            session,
            ctx["item"],
            movement_type="ADJUSTMENT_IN",
            quantity_delta=Decimal("1"),
            weight_delta=Decimal("0"),
            reference_type="TEST",
            reference_id=None,
            reason="Missing actor",
            user_responsible="Test",
            weight_verified=False,
        ),
    ]
    for call in no_actor_calls:
        with pytest.raises(TypeError):
            call()


def test_denied_inventory_calls_are_audited_before_business_reads_and_mutations(
    inventory_rbac_context, authenticated_actor_factory, tmp_path
):
    ctx = inventory_rbac_context
    session = ctx["session"]
    denied_actor = _role_actor(
        session,
        ctx["admin"],
        "inventory-rbac-denied",
        "HR/Employee Manager",
    )
    statements = []

    def record_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(ctx["engine"], "before_cursor_execute", record_statement)
    calls = [
        lambda: get_inventory_item(
            session, ctx["item"].id, actor_context=denied_actor
        ),
        lambda: get_inventory_metrics(session, actor_context=denied_actor),
        lambda: generate_inventory_pdf_report(
            session,
            str(tmp_path / "denied-inventory.pdf"),
            actor_context=denied_actor,
        ),
        lambda: create_inventory_item(
            session,
            {"part_name": "Denied catalogue item", "category": "Components"},
            actor_context=denied_actor,
        ),
        lambda: update_inventory_item(
            session, ctx["item"].id, {"part_name": "Denied update"},
            actor_context=denied_actor,
        ),
        lambda: record_stock_movement(
            session,
            ctx["item"].id,
            "ADJUSTMENT_IN",
            "1",
            reason="Denied adjustment",
            user_responsible="Denied",
            actor_context=denied_actor,
        ),
        lambda: transfer_inventory_location(
            session,
            ctx["item"].id,
            {"storage_location": "Denied"},
            reason="Denied transfer",
            user_responsible="Denied",
            actor_context=denied_actor,
        ),
        lambda: _apply_stock_change(
            session,
            ctx["item"],
            movement_type="ADJUSTMENT_IN",
            quantity_delta=Decimal("1"),
            weight_delta=Decimal("0"),
            reference_type="TEST",
            reference_id=None,
            reason="Denied private helper call",
            user_responsible="Denied",
            weight_verified=False,
            actor_context=denied_actor,
        ),
    ]
    try:
        for call in calls:
            with pytest.raises(AuthorizationError):
                call()
    finally:
        event.remove(ctx["engine"], "before_cursor_execute", record_statement)

    assert not any(
        "inventory_items" in statement or "stock_movements" in statement
        for statement in statements
    )
    assert session.get(InventoryItem, ctx["item"].id).quantity_available == Decimal("0.00")
    assert session.get(InventoryItem, ctx["item"].id).part_name == "RBAC Copper Coil"
    assert session.scalar(select(StockMovement.id)) is None
    assert not (tmp_path / "denied-inventory.pdf").exists()
    assert session.query(UserAuditEvent).filter_by(
        user_id=denied_actor.user_id,
        action="authorization_denied",
        module="Security",
    ).count() == len(calls)


def test_inventory_actor_context_rejects_fabricated_expired_logged_out_inactive_and_revoked(
    inventory_rbac_context,
):
    ctx = inventory_rbac_context
    session = ctx["session"]
    admin = ctx["admin"]
    item_id = ctx["item"].id

    fabricated = ActorContext(
        user_id=admin.user_id,
        username=admin.username,
        session_id="fabricated-inventory-context",
        issued_at=admin.issued_at,
        database_identity=admin.database_identity,
    )
    with pytest.raises(AuthorizationError):
        get_inventory_item(session, item_id, actor_context=fabricated)

    expired = authenticate(
        session,
        admin.username,
        "Isolated-test-admin-2026!",
        timeout_minutes=0,
    )
    with pytest.raises(AuthorizationError):
        get_inventory_item(session, item_id, actor_context=expired)

    logged_out = authenticate(
        session, admin.username, "Isolated-test-admin-2026!"
    )
    logout(session, actor_context=logged_out)
    with pytest.raises(AuthorizationError):
        get_inventory_item(session, item_id, actor_context=logged_out)

    session.get(User, admin.user_id).is_active = False
    session.commit()
    with pytest.raises(AuthorizationError):
        get_inventory_item(session, item_id, actor_context=admin)


def test_inventory_actor_context_rejects_revoked_and_cross_database_contexts(
    inventory_rbac_context, tmp_path, authenticated_actor_factory
):
    ctx = inventory_rbac_context
    session = ctx["session"]
    operator = _role_actor(
        session,
        ctx["admin"],
        "inventory-rbac-revoked",
        "Inventory Operator",
    )
    role_ids = select(RolePermission.role_id).join(
        Role, Role.id == RolePermission.role_id
    ).where(Role.name == "Inventory Operator")
    view_permission = session.scalar(
        select(Permission).where(Permission.code == "inventory.view")
    )
    session.query(RolePermission).filter(
        RolePermission.role_id.in_(role_ids),
        RolePermission.permission_id == view_permission.id,
    ).delete(synchronize_session=False)
    session.commit()
    with pytest.raises(AuthorizationError):
        get_inventory_item(session, ctx["item"].id, actor_context=operator)
    assert session.query(UserAuditEvent).filter_by(
        user_id=operator.user_id, action="authorization_denied"
    ).count() == 1

    other_engine = create_engine(
        f"sqlite:///{(tmp_path / 'inventory-rbac-other.db').as_posix()}"
    )
    Base.metadata.create_all(bind=other_engine)
    other_session = sessionmaker(
        bind=other_engine, autoflush=False, expire_on_commit=False
    )()
    try:
        other_admin = authenticated_actor_factory(other_session)
        assert other_admin.user_id == ctx["admin"].user_id
        other_item = create_inventory_item(
            other_session,
            {"part_name": "Other database item", "category": "Components"},
            actor_context=other_admin,
        )
        with pytest.raises(AuthorizationError, match="different database"):
            get_inventory_item(
                other_session, other_item.id, actor_context=ctx["admin"]
            )
    finally:
        other_session.close()
        other_engine.dispose()


def test_inventory_independent_sessions_and_report_reads_preserve_results(
    inventory_rbac_context, tmp_path
):
    ctx = inventory_rbac_context
    session = ctx["session"]
    actor = ctx["admin"]
    record_stock_movement(
        session,
        ctx["item"].id,
        "ADJUSTMENT_IN",
        "3",
        "6",
        reason="Verified initial stock",
        user_responsible="Admin",
        weight_verified=True,
        actor_context=actor,
    )
    session.commit()

    independent_session = ctx["session_factory"]()
    try:
        assert list_inventory_items(
            independent_session, actor_context=actor
        )[0].id == ctx["item"].id
        assert get_inventory_metrics(
            independent_session, actor_context=actor
        )["total_physical_quantity"] == Decimal("3.00")
        assert len(get_inventory_movements(
            independent_session, ctx["item"].id, actor_context=actor
        )) == 1
    finally:
        independent_session.close()

    pdf = tmp_path / "inventory-rbac.pdf"
    excel = tmp_path / "inventory-rbac.xlsx"
    assert generate_inventory_pdf_report(
        session, str(pdf), actor_context=actor
    ) == str(pdf.resolve())
    assert generate_inventory_excel_report(
        session, str(excel), actor_context=actor
    ) == str(excel.resolve())
    assert pdf.read_bytes().startswith(b"%PDF")
    assert session.scalar(select(AccountingException.id)) is None
    assert session.scalar(select(JournalEntry.id)) is None


def test_inventory_adjustment_and_transfer_failures_roll_back_atomically(
    inventory_rbac_context,
):
    ctx = inventory_rbac_context
    session = ctx["session"]
    item = ctx["item"]

    def fail_movement_flush(session, flush_context, instances):
        if any(isinstance(record, StockMovement) for record in session.new):
            raise RuntimeError("forced inventory movement failure")

    event.listen(session, "before_flush", fail_movement_flush)
    with pytest.raises(RuntimeError, match="forced"):
        record_stock_movement(
            session,
            item.id,
            "ADJUSTMENT_IN",
            "2",
            "4",
            reason="Atomic adjustment test",
            user_responsible="Admin",
            actor_context=ctx["admin"],
        )
    with pytest.raises(RuntimeError, match="forced"):
        transfer_inventory_location(
            session,
            item.id,
            {"storage_location": "Failed destination"},
            reason="Atomic transfer test",
            user_responsible="Admin",
            actor_context=ctx["admin"],
        )
    event.remove(session, "before_flush", fail_movement_flush)
    session.expire_all()
    persisted = session.get(InventoryItem, item.id)
    assert persisted.quantity_available == Decimal("0.00")
    assert persisted.storage_location == "Main"
    assert session.scalar(select(StockMovement.id)) is None


def test_duplicate_catalogue_part_code_is_still_rejected(inventory_rbac_context):
    ctx = inventory_rbac_context
    with pytest.raises(InventoryValidationError, match="already exists"):
        create_inventory_item(
            ctx["session"],
            {
                "part_code": ctx["item"].part_code,
                "part_name": "Duplicate code",
                "category": "Components",
            },
            actor_context=ctx["admin"],
        )
    assert ctx["session"].query(InventoryItem).count() == 1
