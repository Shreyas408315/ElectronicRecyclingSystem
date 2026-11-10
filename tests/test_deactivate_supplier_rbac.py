from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database.connection as database_connection
from database.connection import Base
from database.models import Purchase, Supplier, User, UserAuditEvent, UserRole
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_user,
    logout,
)
from services.supplier_service import deactivate_supplier


def _supplier_with_history(session):
    sequence = session.query(Supplier).count() + 1
    supplier = Supplier(
        supplier_code=f"SUP-DEACTIVATE-{sequence:03d}",
        business_name="History-preserving supplier",
        phone="9876543210",
        opening_balance=1250.0,
        remarks="Existing supplier history",
    )
    session.add(supplier)
    session.flush()
    purchase = Purchase(
        purchase_code=f"PUR-DEACTIVATE-{sequence:03d}",
        supplier_id=supplier.id,
        subtotal=Decimal("125.00"),
        total_amount=Decimal("125.00"),
        balance_amount=Decimal("125.00"),
    )
    session.add(purchase)
    session.commit()
    return supplier.id, purchase.id


def test_deactivate_supplier_authorizes_before_mutation_and_preserves_history(authenticated_actor):
    with database_connection.get_db() as session:
        supplier_id, purchase_id = _supplier_with_history(session)
        before = session.get(Supplier, supplier_id)
        history = (before.supplier_code, before.business_name, before.opening_balance, before.remarks, before.created_at)

        with pytest.raises(TypeError):
            deactivate_supplier(session, supplier_id)
        with pytest.raises(AuthorizationError):
            deactivate_supplier(
                session,
                supplier_id,
                actor_context=ActorContext(
                    authenticated_actor.user_id,
                    authenticated_actor.username,
                    "fabricated-session",
                    authenticated_actor.issued_at,
                    authenticated_actor.database_identity,
                ),
            )
        assert session.get(Supplier, supplier_id).is_active is True
        assert session.get(Purchase, purchase_id).supplier_id == supplier_id

        deactivated = deactivate_supplier(session, supplier_id, actor_context=authenticated_actor)
        session.commit()
        assert deactivated.is_active is False
        assert (
            deactivated.supplier_code,
            deactivated.business_name,
            deactivated.opening_balance,
            deactivated.remarks,
            deactivated.created_at,
        ) == history
        assert session.get(Purchase, purchase_id).total_amount == Decimal("125.00")

        procurement = create_user(
            session,
            actor_context=authenticated_actor,
            username="deactivate-procurement",
            password="Deactivate-procurement-password-2026!",
            full_name="Deactivate Procurement",
            role_names=["Procurement"],
        )
        session.commit()
        procurement_context = authenticate(
            session, "deactivate-procurement", "Deactivate-procurement-password-2026!"
        )
        second_supplier_id, _ = _supplier_with_history(session)
        assert deactivate_supplier(
            session, second_supplier_id, actor_context=procurement_context
        ).is_active is False


def test_deactivate_supplier_denials_audit_and_reject_lifecycle_contexts(authenticated_actor):
    with database_connection.get_db() as session:
        supplier_id, purchase_id = _supplier_with_history(session)
        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="deactivate-clerk",
            password="Deactivate-clerk-password-2026!",
            full_name="Deactivate Clerk",
            role_names=["Sales Operator"],
        )
        session.commit()
        clerk_context = authenticate(session, "deactivate-clerk", "Deactivate-clerk-password-2026!")
        with pytest.raises(AuthorizationError):
            deactivate_supplier(session, supplier_id, actor_context=clerk_context)
        assert session.query(UserAuditEvent).filter_by(
            user_id=clerk.id, action="authorization_denied", module="Security"
        ).count() == 1

        expired_context = authenticate(session, "fixture-admin", "Fixture-admin-password-2026!", timeout_minutes=0)
        with pytest.raises(AuthorizationError):
            deactivate_supplier(session, supplier_id, actor_context=expired_context)
        session.get(User, clerk.id).is_active = False
        session.commit()
        with pytest.raises(AuthorizationError):
            deactivate_supplier(session, supplier_id, actor_context=clerk_context)

        revoked = create_user(
            session,
            actor_context=authenticated_actor,
            username="deactivate-revoked",
            password="Deactivate-revoked-password-2026!",
            full_name="Deactivate Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        revoked_context = authenticate(session, "deactivate-revoked", "Deactivate-revoked-password-2026!")
        session.query(UserRole).filter_by(user_id=revoked.id).delete()
        session.commit()
        with pytest.raises(AuthorizationError):
            deactivate_supplier(session, supplier_id, actor_context=revoked_context)
        assert session.query(UserAuditEvent).filter_by(
            user_id=revoked.id, action="authorization_denied", module="Security"
        ).count() == 1
        logout(session, actor_context=authenticated_actor)
        with pytest.raises(AuthorizationError):
            deactivate_supplier(session, supplier_id, actor_context=authenticated_actor)
        assert session.get(Supplier, supplier_id).is_active is True
        assert session.get(Purchase, purchase_id).supplier_id == supplier_id


def test_deactivate_supplier_supports_independent_session_and_rejects_cross_database_actor(authenticated_actor):
    with database_connection.get_db() as session:
        supplier_id, _ = _supplier_with_history(session)

    with database_connection.get_db() as independent_session:
        assert deactivate_supplier(
            independent_session, supplier_id, actor_context=authenticated_actor
        ).is_active is False
        independent_session.commit()

    other_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(bind=other_engine)
    other_session = OtherSession()
    try:
        with pytest.raises(AuthorizationError):
            deactivate_supplier(other_session, 1, actor_context=authenticated_actor)
    finally:
        other_session.close()
        other_engine.dispose()
