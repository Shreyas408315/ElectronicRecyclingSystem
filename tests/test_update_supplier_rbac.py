from datetime import date
from decimal import Decimal
from inspect import Parameter, signature

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import database.connection as database_connection
from database.connection import Base
from database.models import (
    Purchase,
    Supplier,
    SupplierPayment,
    UserAuditEvent,
    User,
    UserRole,
)
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_first_administrator,
    create_user,
    logout,
)
from services.supplier_service import (
    create_supplier,
    deactivate_supplier,
    generate_next_supplier_code,
    get_supplier_by_code,
    get_supplier_by_id,
    get_supplier_metrics,
    list_suppliers,
    reactivate_supplier,
    update_supplier,
)


def _payload(name="Updated supplier", **overrides):
    payload = {
        "business_name": name,
        "phone": "9876543210",
        "opening_balance": "1250.00",
        "remarks": "Updated by authorized user",
    }
    payload.update(overrides)
    return payload


def _supplier_with_history(session, actor_context):
    supplier = create_supplier(
        session,
        {
            "business_name": "Supplier with history",
            "phone": "9876543210",
            "opening_balance": "1250.00",
        },
        actor_context=actor_context,
    )
    purchase = Purchase(
        purchase_code="PUR-UPDATE-RBAC-1",
        supplier_id=supplier.id,
        subtotal=Decimal("250.00"),
        total_amount=Decimal("250.00"),
        amount_paid=Decimal("50.00"),
        balance_amount=Decimal("200.00"),
    )
    payment = SupplierPayment(
        payment_code="SPAY-UPDATE-RBAC-1",
        supplier_id=supplier.id,
        payment_date=date(2026, 11, 6),
        amount_paid=Decimal("50.00"),
        payment_method="NEFT",
        status="Draft",
    )
    session.add_all([purchase, payment])
    session.commit()
    return supplier.id, purchase.id, payment.id


def _snapshot(session, supplier_id):
    supplier = session.get(Supplier, supplier_id)
    return (
        supplier.supplier_code,
        supplier.business_name,
        supplier.phone,
        supplier.opening_balance,
        supplier.remarks,
        supplier.updated_at,
    )


def test_update_supplier_is_mandatory_keyword_actor_and_all_nine_supplier_apis_require_it():
    supplier_apis = (
        create_supplier,
        update_supplier,
        deactivate_supplier,
        reactivate_supplier,
        get_supplier_by_id,
        get_supplier_by_code,
        get_supplier_metrics,
        list_suppliers,
        generate_next_supplier_code,
    )
    for api in supplier_apis:
        actor_parameter = signature(api).parameters["actor_context"]
        assert actor_parameter.kind is Parameter.KEYWORD_ONLY
        assert actor_parameter.default is Parameter.empty
        assert actor_parameter.annotation is ActorContext


def test_administrator_and_procurement_can_update_supplier(authenticated_actor):
    with database_connection.get_db() as session:
        supplier = create_supplier(
            session,
            {"business_name": "First supplier", "phone": "9876543210"},
            actor_context=authenticated_actor,
        )
        supplier_id = supplier.id
        updated = update_supplier(
            session, supplier_id, _payload(), actor_context=authenticated_actor
        )
        assert updated.business_name == "Updated supplier"
        assert updated.supplier_code == "SUP-000001"

        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="update-procurement",
            password="Update-procurement-password-2026!",
            full_name="Update Procurement",
            role_names=["Procurement"],
        )
        session.commit()
        procurement_context = authenticate(
            session, "update-procurement", "Update-procurement-password-2026!"
        )
        updated = update_supplier(
            session,
            supplier_id,
            _payload("Procurement update", opening_balance="2000.00"),
            actor_context=procurement_context,
        )
        assert updated.business_name == "Procurement update"
        assert updated.opening_balance == Decimal("2000.00")


def test_update_supplier_rejects_missing_and_fabricated_context_before_supplier_read(
    authenticated_actor, monkeypatch
):
    with database_connection.get_db() as session:
        supplier_id, purchase_id, payment_id = _supplier_with_history(
            session, authenticated_actor
        )
        before = _snapshot(session, supplier_id)
        supplier_gets = []
        original_get = Session.get

        def track_supplier_get(bound_session, entity, ident, *args, **kwargs):
            if entity is Supplier:
                supplier_gets.append(ident)
            return original_get(bound_session, entity, ident, *args, **kwargs)

        monkeypatch.setattr(Session, "get", track_supplier_get)
        with pytest.raises(TypeError):
            signature(update_supplier).bind(session, supplier_id, _payload())
        with pytest.raises(AuthorizationError):
            update_supplier(
                session,
                supplier_id,
                _payload(),
                actor_context=ActorContext(
                    authenticated_actor.user_id,
                    authenticated_actor.username,
                    "fabricated-update-session",
                    authenticated_actor.issued_at,
                    authenticated_actor.database_identity,
                ),
            )

        assert supplier_gets == []
        assert _snapshot(session, supplier_id) == before
        assert session.get(Purchase, purchase_id).supplier_id == supplier_id
        assert session.get(SupplierPayment, payment_id).supplier_id == supplier_id


def test_update_supplier_denials_are_audited_and_leave_related_financial_records_unchanged(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier_id, purchase_id, payment_id = _supplier_with_history(
            session, authenticated_actor
        )
        before = _snapshot(session, supplier_id)
        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="update-clerk",
            password="Update-clerk-password-2026!",
            full_name="Update Clerk",
            role_names=["Sales Operator"],
        )
        session.commit()
        clerk_context = authenticate(
            session, "update-clerk", "Update-clerk-password-2026!"
        )

        with pytest.raises(AuthorizationError):
            update_supplier(
                session, supplier_id, _payload(), actor_context=clerk_context
            )
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == clerk.id,
                UserAuditEvent.action == "authorization_denied",
                UserAuditEvent.module == "Security",
            )
        ) is not None
        assert _snapshot(session, supplier_id) == before
        assert session.get(Purchase, purchase_id).total_amount == Decimal("250.00")
        assert session.get(Purchase, purchase_id).balance_amount == Decimal("200.00")
        payment = session.get(SupplierPayment, payment_id)
        assert payment.supplier_id == supplier_id
        assert payment.amount_paid == Decimal("50.00")


def test_update_supplier_rejects_expired_logged_out_inactive_and_revoked_actors(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier_id, _, _ = _supplier_with_history(session, authenticated_actor)
        before = _snapshot(session, supplier_id)

        expired = authenticate(
            session,
            "fixture-admin",
            "Fixture-admin-password-2026!",
            timeout_minutes=0,
        )
        with pytest.raises(AuthorizationError):
            update_supplier(session, supplier_id, _payload(), actor_context=expired)

        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="update-revoked",
            password="Update-revoked-password-2026!",
            full_name="Update Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        procurement_context = authenticate(
            session, "update-revoked", "Update-revoked-password-2026!"
        )
        session.query(UserRole).filter_by(user_id=procurement.id).delete()
        session.commit()
        with pytest.raises(AuthorizationError):
            update_supplier(
                session,
                supplier_id,
                _payload(),
                actor_context=procurement_context,
            )
        assert session.scalar(
            select(UserAuditEvent.id).where(
                UserAuditEvent.user_id == procurement.id,
                UserAuditEvent.action == "authorization_denied",
            )
        ) is not None

        inactive = create_user(
            session,
            actor_context=authenticated_actor,
            username="update-inactive",
            password="Update-inactive-password-2026!",
            full_name="Update Inactive",
            role_names=["Procurement"],
        )
        session.commit()
        inactive_context = authenticate(
            session, "update-inactive", "Update-inactive-password-2026!"
        )
        session.get(User, inactive.id).is_active = False
        session.commit()
        with pytest.raises(AuthorizationError):
            update_supplier(
                session, supplier_id, _payload(), actor_context=inactive_context
            )

        logged_out = authenticate(
            session, "fixture-admin", "Fixture-admin-password-2026!"
        )
        logout(session, actor_context=logged_out)
        with pytest.raises(AuthorizationError):
            update_supplier(session, supplier_id, _payload(), actor_context=logged_out)
        assert _snapshot(session, supplier_id) == before


def test_update_supplier_supports_independent_sessions_and_rejects_cross_database_actor(
    authenticated_actor,
):
    with database_connection.get_db() as session:
        supplier_id, _, _ = _supplier_with_history(session, authenticated_actor)
    with database_connection.get_db() as independent_session:
        updated = update_supplier(
            independent_session,
            supplier_id,
            _payload("Independent session update"),
            actor_context=authenticated_actor,
        )
        assert updated.business_name == "Independent session update"
        independent_session.commit()

    other_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(other_engine)
    other_session = sessionmaker(bind=other_engine)()
    try:
        create_first_administrator(
            other_session,
            username="other-database-admin",
            password="Other-database-admin-2026!",
            full_name="Other Database Administrator",
        )
        other_session.flush()
        assert other_session.query(User).one().id == authenticated_actor.user_id
        other_supplier = Supplier(
            id=1,
            supplier_code="SUP-000001",
            business_name="Collision supplier",
            phone="9876543210",
        )
        other_session.add(other_supplier)
        other_session.commit()
        with pytest.raises(AuthorizationError):
            update_supplier(
                other_session,
                1,
                _payload("Must not update"),
                actor_context=authenticated_actor,
            )
        assert other_session.get(Supplier, 1).business_name == "Collision supplier"
    finally:
        other_session.close()
        other_engine.dispose()


def test_update_supplier_validation_and_flush_failure_do_not_leave_partial_changes(
    authenticated_actor, monkeypatch
):
    with database_connection.get_db() as session:
        supplier_id, _, _ = _supplier_with_history(session, authenticated_actor)
        before = _snapshot(session, supplier_id)
        with pytest.raises(ValueError):
            update_supplier(
                session,
                supplier_id,
                _payload("Invalid phone", phone="123"),
                actor_context=authenticated_actor,
            )
        assert _snapshot(session, supplier_id) == before

        def fail_supplier_flush(bound_session, flush_context, instances):
            if any(
                isinstance(instance, Supplier) and instance in bound_session.dirty
                for instance in bound_session.dirty
            ):
                raise RuntimeError("forced supplier update flush failure")

        from sqlalchemy import event

        event.listen(session, "before_flush", fail_supplier_flush)
        with pytest.raises(RuntimeError, match="forced supplier update"):
            update_supplier(
                session,
                supplier_id,
                _payload("Partial update must roll back"),
                actor_context=authenticated_actor,
            )
        event.remove(session, "before_flush", fail_supplier_flush)
        session.rollback()
        session.expire_all()
        assert _snapshot(session, supplier_id) == before
        assert session.scalar(
            select(Supplier.supplier_code).where(Supplier.id == supplier_id)
        ) == "SUP-000001"
        assert generate_next_supplier_code(
            session, actor_context=authenticated_actor
        ) == "SUP-000002"
