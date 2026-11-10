import inspect
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base, get_db
from database.models import AccountingException, CustomerReceipt, JournalEntry, User
from services.customer_receipt_service import (
    create_customer_receipt,
    get_customer_receipt_dashboard,
    post_customer_receipt,
)
from services.customer_service import create_customer
from services.expense_service import get_expense_dashboard, void_expense
from services.payment_report_service import (
    generate_payment_excel_report,
    generate_payment_pdf_report,
)
from services.security_service import (
    AuthorizationError,
    PERMISSIONS,
    ROLE_MATRIX,
    authenticate,
    create_user,
    logout,
)


def _protected_public_apis():
    from services import (
        accounting_report_service,
        customer_receipt_service,
        expense_payment_service,
        expense_report_service,
        expense_service,
        payment_report_service,
        sales_invoice_service,
        supplier_payment_service,
    )

    modules = (
        customer_receipt_service,
        supplier_payment_service,
        expense_payment_service,
        payment_report_service,
        expense_report_service,
    )
    api_names = {
        function.__name__
        for module in modules
        for _, function in inspect.getmembers(module, inspect.isfunction)
        if function.__module__ == module.__name__ and not function.__name__.startswith("_")
    }
    api_names.update({
        "get_reconciliation_report",
        "generate_accounting_excel_report",
        "generate_accounting_pdf_report",
    })
    api_names.update({
        "void_sales_invoice",
        "get_sales_invoice_summary",
        "get_sales_invoice_metrics",
    })
    api_names.update({
        "get_expense_by_id",
        "void_expense",
        "list_expenses",
        "calculate_category_summary",
        "calculate_expense_summary",
        "calculate_dashboard_expense_summary",
        "calculate_monthly_summary",
        "get_expense_statement",
        "get_expense_dashboard",
    })
    candidates = {
        function.__name__: function
        for module in (
            *modules, accounting_report_service, sales_invoice_service,
            expense_service,
        )
        for _, function in inspect.getmembers(module, inspect.isfunction)
        if function.__module__ == module.__name__
    }
    return {name: candidates[name] for name in sorted(api_names)}


def test_every_protected_payment_api_requires_keyword_only_actor_context():
    apis = _protected_public_apis()
    assert len(apis) == 61
    for name, function in apis.items():
        parameter = inspect.signature(function).parameters.get("actor_context")
        assert parameter is not None, name
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is inspect.Parameter.empty, name

        args = []
        kwargs = {}
        for current in inspect.signature(function).parameters.values():
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


def test_payment_role_matrix_keeps_read_draft_post_and_void_separate():
    payment_permissions = {
        "receipts.view", "receipts.manage", "receipts.post", "receipts.void",
        "supplier_payments.view", "supplier_payments.manage",
        "supplier_payments.post", "supplier_payments.void",
        "expenses.view", "expenses.manage", "expenses.post", "expenses.void",
    }
    assert payment_permissions <= PERMISSIONS.keys()
    assert payment_permissions <= ROLE_MATRIX["System Administrator"]
    assert payment_permissions <= ROLE_MATRIX["Factory Manager"]
    assert payment_permissions <= ROLE_MATRIX["Accounts & Finance"]
    assert {
        "receipts.view", "supplier_payments.view", "expenses.view",
    } <= ROLE_MATRIX["Read-only Auditor"]
    assert not payment_permissions.intersection(
        ROLE_MATRIX["Read-only Auditor"] - {
            "receipts.view", "supplier_payments.view", "expenses.view",
        }
    )
    assert ROLE_MATRIX["Sales Operator"] & payment_permissions == {
        "receipts.view",
    }
    assert not payment_permissions.intersection(ROLE_MATRIX["Procurement"])


def test_receipt_permissions_keep_view_manage_post_boundaries(authenticated_actor):
    with get_db() as session:
        operator = create_user(
            session,
            actor_context=authenticated_actor,
            username=f"payments-sales-{uuid4().hex[:8]}",
            password="Sales-operator-password-2026!",
            full_name="Payments Sales Operator",
            role_names=["Sales Operator"],
        )
        operator_id = operator.id
        admin_customer = create_customer(
            session,
            {
                "customer_name": "RBAC Receipt Customer",
                "customer_type": "Retailer",
            },
            actor_context=authenticated_actor,
        )
        receipt = create_customer_receipt(
            session,
            {
                "customer_id": admin_customer.id,
                "receipt_date": datetime.today().date(),
                "amount_received": "125.00",
                "payment_method": "Cash",
            },
            actor_context=authenticated_actor,
        )
        receipt_id = receipt.id
        actor = authenticate(
            session, operator.username, "Sales-operator-password-2026!"
        )

    try:
        with get_db() as session:
            assert get_customer_receipt_dashboard(
                session, actor_context=actor
            )["receipt_count"] == 0
            with pytest.raises(AuthorizationError, match="expenses.view"):
                get_expense_dashboard(
                    session, actor_context=actor
                )
            with pytest.raises(AuthorizationError, match="expenses.void"):
                void_expense(
                    session, 999, "Unauthorized", actor_context=actor
                )
            with pytest.raises(AuthorizationError, match="receipts.post"):
                post_customer_receipt(
                    session, receipt_id, actor_context=actor
                )
            receipt = session.get(CustomerReceipt, receipt_id)
            assert receipt.status == "Draft"
            assert receipt.allocations == []
            assert session.scalar(
                select(JournalEntry.id).where(
                    JournalEntry.source_module == "Customer Receipt",
                    JournalEntry.source_transaction_id == receipt_id,
                )
            ) is None
    finally:
        logout(actor_context=actor)


@pytest.mark.parametrize(
    ("export", "suffix"),
    (
        (generate_payment_excel_report, ".xlsx"),
        (generate_payment_pdf_report, ".pdf"),
    ),
)
def test_combined_payment_exports_require_both_view_permissions(
    authenticated_actor, tmp_path, export, suffix
):
    with get_db() as session:
        user = create_user(
            session,
            actor_context=authenticated_actor,
            username=f"payments-export-{uuid4().hex[:8]}",
            password="Sales-export-password-2026!",
            full_name="Payments Export Sales Operator",
            role_names=["Sales Operator"],
        )
        username = user.username
    with get_db() as session:
        actor = authenticate(
            session, username, "Sales-export-password-2026!"
        )
    output = tmp_path / f"payments{suffix}"
    try:
        with get_db() as session:
            with pytest.raises(AuthorizationError, match="supplier_payments.view"):
                export(
                    session, str(output), actor_context=actor
                )
        assert not output.exists()
    finally:
        logout(actor_context=actor)


def test_duplicate_receipt_post_does_not_duplicate_accounting_source_event(
    authenticated_actor,
):
    with get_db() as session:
        customer = create_customer(
            session,
            {
                "customer_name": "Idempotent Receipt Customer",
                "customer_type": "Retailer",
            },
            actor_context=authenticated_actor,
        )
        receipt = create_customer_receipt(
            session,
            {
                "customer_id": customer.id,
                "receipt_date": datetime.today().date(),
                "amount_received": "50.00",
                "payment_method": "Cash",
            },
            actor_context=authenticated_actor,
        )
        post_customer_receipt(
            session, receipt.id, actor_context=authenticated_actor
        )
        source_id = receipt.id
        journal_count = len(session.scalars(select(JournalEntry).where(
            JournalEntry.source_module == "Customer Receipt",
            JournalEntry.source_transaction_id == source_id,
        )).all())
        exception_count = len(session.scalars(select(AccountingException).where(
            AccountingException.source_module == "Customer Receipt",
            AccountingException.source_transaction_id == source_id,
        )).all())
        assert journal_count + exception_count == 1

        with pytest.raises(ValueError, match="Only a draft receipt"):
            post_customer_receipt(
                session, source_id, actor_context=authenticated_actor
            )
        assert len(session.scalars(select(JournalEntry).where(
            JournalEntry.source_module == "Customer Receipt",
            JournalEntry.source_transaction_id == source_id,
        )).all()) == journal_count
        assert len(session.scalars(select(AccountingException).where(
            AccountingException.source_module == "Customer Receipt",
            AccountingException.source_transaction_id == source_id,
        )).all()) == exception_count


def test_receipt_read_rejects_untrusted_expired_and_cross_database_contexts(
    authenticated_actor,
):
    with get_db() as session:
        with pytest.raises(AuthorizationError, match="Sign in"):
            get_customer_receipt_dashboard(session, actor_context=None)
        fabricated = replace(authenticated_actor, session_id=uuid4().hex)
        with pytest.raises(AuthorizationError, match="Sign in"):
            get_customer_receipt_dashboard(
                session, actor_context=fabricated
            )

        from services import security_service

        trusted = security_service._trusted_sessions[authenticated_actor.session_id]
        previous_activity = trusted.last_activity
        trusted.last_activity = datetime.now() - timedelta(
            minutes=authenticated_actor.timeout_minutes + 1
        )
        try:
            with pytest.raises(AuthorizationError, match="Sign in"):
                get_customer_receipt_dashboard(
                    session, actor_context=authenticated_actor
                )
        finally:
            trusted.last_activity = previous_activity

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    other_session = sessionmaker(bind=engine)()
    try:
        with pytest.raises(AuthorizationError, match="different database"):
            get_customer_receipt_dashboard(
                other_session, actor_context=authenticated_actor
            )
    finally:
        other_session.close()
        engine.dispose()


def test_receipt_read_accepts_an_independent_session_to_the_same_database(
    authenticated_actor,
):
    with get_db() as first_session:
        create_customer(
            first_session,
            {
                "customer_name": "Independent Payment Session Customer",
                "customer_type": "Retailer",
            },
            actor_context=authenticated_actor,
        )
    with get_db() as independent_session:
        result = get_customer_receipt_dashboard(
            independent_session, actor_context=authenticated_actor
        )
    assert result["receipt_count"] == 0


def test_receipt_read_rejects_logged_out_and_inactive_actors(authenticated_actor):
    with get_db() as session:
        user = session.get(User, authenticated_actor.user_id)
        assert user is not None
        user.is_active = False
        with pytest.raises(AuthorizationError, match="inactive"):
            get_customer_receipt_dashboard(
                session, actor_context=authenticated_actor
            )
        user.is_active = True
        session.flush()

    logout(actor_context=authenticated_actor)
    with get_db() as session:
        with pytest.raises(AuthorizationError, match="Sign in"):
            get_customer_receipt_dashboard(
                session, actor_context=authenticated_actor
            )
