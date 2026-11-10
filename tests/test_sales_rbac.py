import inspect
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import (
    AccountingException,
    InventoryItem,
    JournalEntry,
    Permission,
    Role,
    RolePermission,
    SalesInvoice,
    SalesInvoiceHistory,
    SalesOrder,
    SalesOrderFulfilment,
    SalesOrderFulfilmentItem,
    StockMovement,
    User,
)
from services.customer_service import create_customer
from services.inventory_service import create_inventory_item, record_stock_movement
from services.sales_invoice_service import (
    create_invoice_from_fulfilled_order,
    get_sales_invoice_summary,
    issue_sales_invoice,
    update_invoice_factory_settings,
    void_sales_invoice,
)
from services.sales_invoice_report_service import generate_sales_invoice_excel_report
from services.sales_order_service import (
    confirm_sales_order,
    create_draft_order,
    fulfil_sales_order,
    get_sales_order_summary,
)
from services.sales_order_report_service import generate_sales_order_excel_report
from services.security_service import (
    AuthorizationError,
    PERMISSIONS,
    ROLE_MATRIX,
    authenticate,
    create_user,
    logout,
)


@pytest.fixture
def sales_context(tmp_path, authenticated_actor_factory):
    engine = create_engine(f"sqlite:///{(tmp_path / 'sales-rbac.db').as_posix()}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        admin = authenticated_actor_factory(session)
        customer = create_customer(
            session,
            {
                "customer_name": "Sales RBAC Customer",
                "customer_type": "Retailer",
                "state": "Maharashtra",
            },
            actor_context=admin,
        )
        item = create_inventory_item(
            session,
            {
                "part_name": "Sales RBAC Component",
                "category": "Components",
                "unit": "pieces",
                "condition": "Working",
                "cost_price": "5.00",
                "estimated_selling_price": "12.00",
            },
            actor_context=admin,
        )
        record_stock_movement(
            session,
            item.id,
            "ADJUSTMENT_IN",
            "3",
            reason="Test opening balance",
            user_responsible="RBAC test",
            actor_context=admin,
        )
        order = create_draft_order(
            session,
            {"customer_id": customer.id},
            [{
                "inventory_item_id": item.id,
                "quantity_ordered": "2",
                "unit_selling_price": "12.00",
            }],
            actor_context=admin,
        )
        confirm_sales_order(session, order.id, actor_context=admin)
        update_invoice_factory_settings(
            session,
            {
                "name": "RBAC Test Factory",
                "address": "Test Industrial Estate",
                "gstin": "",
                "state": "Maharashtra",
            },
            actor_context=admin,
        )
        fulfilment = fulfil_sales_order(
            session,
            order.id,
            {order.items[0].id: "1"},
            idempotency_key=uuid4().hex,
            actor_context=admin,
        )
        invoice = create_invoice_from_fulfilled_order(
            session, order.id, actor_context=admin
        )
        issue_sales_invoice(session, invoice.id, actor_context=admin)
        session.commit()
        yield {
            "engine": engine,
            "session": session,
            "session_factory": session_factory,
            "admin": admin,
            "customer": customer,
            "item": item,
            "order": order,
            "fulfilment": fulfilment,
            "invoice": invoice,
        }
    finally:
        session.close()
        engine.dispose()


def _role_actor(session, admin, role_name):
    username = f"sales-rbac-{uuid4().hex[:10]}"
    password = "Sales-rbac-test-2026!"
    create_user(
        session,
        actor_context=admin,
        username=username,
        password=password,
        full_name=username,
        role_names=[role_name],
    )
    session.flush()
    return authenticate(session, username, password)


def test_sales_permissions_keep_view_manage_issue_void_and_dispatch_separate():
    assert {
        "sales.view",
        "sales.manage",
        "sales.confirm",
        "sales.dispatch",
        "invoices.view",
        "invoices.manage",
        "invoices.issue",
        "invoices.void",
        "inventory.dispatch",
    } <= PERMISSIONS.keys()
    operator = ROLE_MATRIX["Sales Operator"]
    assert {
        "sales.view",
        "sales.manage",
        "sales.confirm",
        "sales.dispatch",
        "invoices.view",
        "invoices.manage",
        "invoices.issue",
        "inventory.dispatch",
    } <= operator
    assert "invoices.void" not in operator
    assert "inventory.adjust" not in operator

    finance = ROLE_MATRIX["Accounts & Finance"]
    assert {"invoices.view", "invoices.void", "sales.view", "receipts.view"} <= finance
    assert not {"sales.manage", "sales.dispatch", "invoices.manage", "invoices.issue"} & finance
    auditor = ROLE_MATRIX["Read-only Auditor"]
    assert {"sales.view", "invoices.view"} <= auditor
    assert not {
        "sales.manage", "sales.confirm", "sales.dispatch", "invoices.manage",
        "invoices.issue", "invoices.void", "inventory.dispatch",
    } & auditor


def _sales_database_apis():
    from services.inventory_service import record_sales_dispatch_movement
    from services import (
        sales_invoice_report_service,
        sales_invoice_service,
        sales_order_report_service,
        sales_order_service,
    )

    modules = (
        sales_order_service,
        sales_invoice_service,
        sales_order_report_service,
        sales_invoice_report_service,
    )
    apis = {}
    for module in modules:
        for name, function in inspect.getmembers(module, inspect.isfunction):
            if function.__module__ != module.__name__ or name.startswith("_"):
                continue
            parameters = inspect.signature(function).parameters
            if (
                any(name in parameters for name in ("session", "db_session", "_session"))
                or name == "get_sales_invoice_summary"
            ):
                apis[name] = function
    apis["record_sales_dispatch_movement"] = record_sales_dispatch_movement
    return apis


def test_every_sales_database_api_requires_keyword_only_actor_context():
    apis = _sales_database_apis()
    assert len(apis) == 40
    for name, function in apis.items():
        signature = inspect.signature(function)
        parameter = signature.parameters.get("actor_context")
        assert parameter is not None, name
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is inspect.Parameter.empty, name

        args = []
        kwargs = {}
        for current in signature.parameters.values():
            if current.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                if current.default is inspect.Parameter.empty:
                    args.append(object())
            elif (
                current.kind is inspect.Parameter.KEYWORD_ONLY
                and current.name != "actor_context"
                and current.default is inspect.Parameter.empty
            ):
                kwargs[current.name] = object()
        with pytest.raises(TypeError, match="actor_context"):
            function(*args, **kwargs)


def test_invoice_void_requires_invoice_void_even_for_receipt_and_invoice_viewers(
    sales_context, monkeypatch
):
    ctx = sales_context
    session = ctx["session"]
    actor = _role_actor(session, ctx["admin"], "Sales Operator")
    invoice_id = ctx["invoice"].id
    before = (
        session.scalar(select(SalesInvoice.status).where(SalesInvoice.id == invoice_id)),
        session.scalar(select(func.count(SalesInvoiceHistory.id)).where(
            SalesInvoiceHistory.invoice_id == invoice_id
        )),
        session.scalar(select(func.count(JournalEntry.id)).where(
            JournalEntry.source_module == "Sales Invoice",
            JournalEntry.source_transaction_id == invoice_id,
        )),
        session.scalar(select(func.count(AccountingException.id)).where(
            AccountingException.source_module == "Sales Invoice",
            AccountingException.source_transaction_id == invoice_id,
        )),
    )

    def receipt_balance_must_not_be_read(*_args, **_kwargs):
        raise AssertionError("Receipt data must not be read before invoices.void is authorized.")

    monkeypatch.setattr(
        "services.customer_receipt_service.calculate_invoice_balance",
        receipt_balance_must_not_be_read,
    )
    with pytest.raises(AuthorizationError, match="invoices.void"):
        void_sales_invoice(
            session,
            invoice_id,
            "Unauthorized void attempt",
            actor_context=actor,
        )
    after = (
        session.scalar(select(SalesInvoice.status).where(SalesInvoice.id == invoice_id)),
        session.scalar(select(func.count(SalesInvoiceHistory.id)).where(
            SalesInvoiceHistory.invoice_id == invoice_id
        )),
        session.scalar(select(func.count(JournalEntry.id)).where(
            JournalEntry.source_module == "Sales Invoice",
            JournalEntry.source_transaction_id == invoice_id,
        )),
        session.scalar(select(func.count(AccountingException.id)).where(
            AccountingException.source_module == "Sales Invoice",
            AccountingException.source_transaction_id == invoice_id,
        )),
    )
    assert after == before
    logout(actor_context=actor)


def test_accounts_finance_can_void_invoice_without_invoice_issue_permission(
    sales_context,
):
    ctx = sales_context
    session = ctx["session"]
    actor = _role_actor(session, ctx["admin"], "Accounts & Finance")
    assert "invoices.issue" not in ROLE_MATRIX["Accounts & Finance"]
    invoice = void_sales_invoice(
        session,
        ctx["invoice"].id,
        "Authorized finance correction",
        actor_context=actor,
    )
    assert invoice.status == "Void"
    assert invoice.void_reason == "Authorized finance correction"
    logout(actor_context=actor)


def test_order_invoice_summaries_and_exports_use_the_authorized_session_records(
    sales_context, tmp_path
):
    ctx = sales_context
    session = ctx["session"]
    admin = ctx["admin"]
    foreign_order = SalesOrder(
        id=ctx["order"].id,
        order_code="FORGED-ORDER",
    )
    foreign_invoice = SalesInvoice(
        id=ctx["invoice"].id,
        invoice_code="FORGED-INVOICE",
    )
    assert get_sales_order_summary(
        session, foreign_order, actor_context=admin
    )["order_code"] == ctx["order"].order_code
    assert get_sales_invoice_summary(
        foreign_invoice, session, actor_context=admin
    )["invoice_code"] == ctx["invoice"].invoice_code

    order_path = tmp_path / "authorized-orders.xlsx"
    invoice_path = tmp_path / "authorized-invoices.xlsx"
    generate_sales_order_excel_report(
        session,
        str(order_path),
        [foreign_order],
        actor_context=admin,
    )
    generate_sales_invoice_excel_report(
        session,
        str(invoice_path),
        [foreign_invoice],
        actor_context=admin,
    )
    assert load_workbook(order_path).active["A2"].value == ctx["order"].order_code
    assert load_workbook(invoice_path).active["A2"].value == ctx["invoice"].invoice_code


def test_sales_dispatch_requires_inventory_dispatch_and_denial_changes_nothing(
    sales_context,
):
    ctx = sales_context
    session = ctx["session"]
    role = Role(
        name=f"Sales Dispatch Only {uuid4().hex[:8]}",
        description="Narrow test role",
        is_system=False,
    )
    session.add(role)
    session.flush()
    dispatch_permission = session.scalar(
        select(Permission).where(Permission.code == "sales.dispatch")
    )
    session.add(
        RolePermission(role_id=role.id, permission_id=dispatch_permission.id)
    )
    session.flush()
    username = f"sales-dispatch-only-{uuid4().hex[:8]}"
    password = "Sales-dispatch-test-2026!"
    create_user(
        session,
        actor_context=ctx["admin"],
        username=username,
        password=password,
        full_name=username,
        role_names=[role.name],
    )
    actor = authenticate(session, username, password)
    item = session.get(InventoryItem, ctx["item"].id)
    before = (
        item.quantity_available,
        item.reserved_quantity,
        session.scalar(select(func.count(SalesOrderFulfilment.id))),
        session.scalar(select(func.count(SalesOrderFulfilmentItem.id))),
        session.scalar(select(func.count(StockMovement.id)).where(
            StockMovement.movement_type == "SALE_DISPATCH"
        )),
    )
    order = session.get(SalesOrder, ctx["order"].id)
    with pytest.raises(AuthorizationError, match="inventory.dispatch"):
        fulfil_sales_order(
            session,
            order.id,
            {order.items[0].id: "1"},
            idempotency_key=uuid4().hex,
            actor_context=actor,
        )
    after = (
        item.quantity_available,
        item.reserved_quantity,
        session.scalar(select(func.count(SalesOrderFulfilment.id))),
        session.scalar(select(func.count(SalesOrderFulfilmentItem.id))),
        session.scalar(select(func.count(StockMovement.id)).where(
            StockMovement.movement_type == "SALE_DISPATCH"
        )),
    )
    assert after == before
    logout(actor_context=actor)


def test_sales_read_rejects_missing_fabricated_expired_inactive_logged_out_and_cross_database_contexts(
    sales_context,
):
    ctx = sales_context
    session = ctx["session"]
    admin = ctx["admin"]
    from services.sales_order_service import get_sales_order_metrics
    from services import security_service

    independent_session = ctx["session_factory"]()
    try:
        assert get_sales_order_metrics(
            independent_session, actor_context=admin
        )["total_orders"] == 1
    finally:
        independent_session.close()

    with pytest.raises(AuthorizationError, match="Sign in"):
        get_sales_order_metrics(session, actor_context=None)
    with pytest.raises(AuthorizationError, match="Sign in"):
        get_sales_order_metrics(
            session,
            actor_context=replace(admin, session_id=uuid4().hex),
        )

    trusted = security_service._trusted_sessions[admin.session_id]
    previous_activity = trusted.last_activity
    trusted.last_activity = datetime.now() - timedelta(
        minutes=admin.timeout_minutes + 1
    )
    try:
        with pytest.raises(AuthorizationError, match="Sign in"):
            get_sales_order_metrics(session, actor_context=admin)
    finally:
        trusted.last_activity = previous_activity

    user = session.get(User, admin.user_id)
    assert user is not None
    user.is_active = False
    session.flush()
    with pytest.raises(AuthorizationError, match="inactive"):
        get_sales_order_metrics(session, actor_context=admin)
    user.is_active = True
    session.flush()

    other_engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(bind=other_engine)
        other_session = sessionmaker(bind=other_engine)()
        try:
            with pytest.raises(AuthorizationError, match="different database"):
                get_sales_order_metrics(other_session, actor_context=admin)
        finally:
            other_session.close()
    finally:
        other_engine.dispose()

    logout(actor_context=admin)
    with pytest.raises(AuthorizationError, match="Sign in"):
        get_sales_order_metrics(session, actor_context=admin)


def test_sales_operator_dispatch_idempotency_does_not_consume_stock_twice(
    sales_context,
):
    ctx = sales_context
    session = ctx["session"]
    actor = _role_actor(session, ctx["admin"], "Sales Operator")
    order = session.get(SalesOrder, ctx["order"].id)
    item = session.get(InventoryItem, ctx["item"].id)
    key = uuid4().hex

    first = fulfil_sales_order(
        session,
        order.id,
        {order.items[0].id: "1"},
        idempotency_key=key,
        actor_context=actor,
    )
    movement_count = session.scalar(
        select(func.count(StockMovement.id)).where(
            StockMovement.movement_type == "SALE_DISPATCH"
        )
    )
    repeated = fulfil_sales_order(
        session,
        order.id,
        {order.items[0].id: "1"},
        idempotency_key=key,
        actor_context=actor,
    )
    assert repeated.id == first.id
    assert item.quantity_available == 1
    assert item.reserved_quantity == 0
    assert session.scalar(select(func.count(SalesOrderFulfilment.id))) == 2
    assert movement_count == 2
    assert session.scalar(
        select(func.count(StockMovement.id)).where(
            StockMovement.movement_type == "SALE_DISPATCH"
        )
    ) == movement_count
    logout(actor_context=actor)
