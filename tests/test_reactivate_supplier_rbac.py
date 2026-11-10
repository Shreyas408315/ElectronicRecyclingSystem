import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database.connection as database_connection
from database.connection import Base
from database.models import Supplier, UserAuditEvent
from services.security_service import (
    ActorContext,
    AuthorizationError,
    authenticate,
    create_user,
    logout,
)
from services.supplier_service import deactivate_supplier, reactivate_supplier


def _inactive_supplier(session, actor_context):
    supplier = Supplier(
        supplier_code="SUP-REACTIVATE-001",
        business_name="History-preserving supplier",
        phone="9876543210",
        remarks="Existing supplier history",
    )
    session.add(supplier)
    session.flush()
    deactivate_supplier(session, supplier.id, actor_context=actor_context)
    session.commit()
    return supplier.id


def test_reactivate_supplier_requires_trusted_manage_actor_and_preserves_record(authenticated_actor):
    """Every denied call precedes mutation; an allowed call changes active status only."""
    with database_connection.get_db() as session:
        supplier_id = _inactive_supplier(session, authenticated_actor)
        before = session.get(Supplier, supplier_id)
        history = (before.supplier_code, before.business_name, before.phone, before.remarks, before.created_at)

        with pytest.raises(TypeError):
            reactivate_supplier(session, supplier_id)
        with pytest.raises(AuthorizationError):
            reactivate_supplier(
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
        assert session.get(Supplier, supplier_id).is_active is False

        reactivated = reactivate_supplier(session, supplier_id, actor_context=authenticated_actor)
        session.commit()
        assert reactivated.is_active is True
        assert (
            reactivated.supplier_code,
            reactivated.business_name,
            reactivated.phone,
            reactivated.remarks,
            reactivated.created_at,
        ) == history


def test_reactivate_supplier_denials_audit_and_reject_invalid_contexts(authenticated_actor):
    with database_connection.get_db() as session:
        supplier_id = _inactive_supplier(session, authenticated_actor)
        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="reactivate-clerk",
            password="Reactivate-clerk-password-2026!",
            full_name="Reactivate Clerk",
            role_names=["Sales Operator"],
        )
        session.commit()
        clerk_context = authenticate(session, "reactivate-clerk", "Reactivate-clerk-password-2026!")

        with pytest.raises(AuthorizationError):
            reactivate_supplier(session, supplier_id, actor_context=clerk_context)
        assert session.get(Supplier, supplier_id).is_active is False
        assert session.query(UserAuditEvent).filter_by(
            user_id=clerk.id, action="authorization_denied", module="Security"
        ).count() == 1

        expired_context = authenticate(
            session,
            "fixture-admin",
            "Fixture-admin-password-2026!",
            timeout_minutes=0,
        )
        with pytest.raises(AuthorizationError):
            reactivate_supplier(session, supplier_id, actor_context=expired_context)
        logout(session, actor_context=authenticated_actor)
        with pytest.raises(AuthorizationError):
            reactivate_supplier(session, supplier_id, actor_context=authenticated_actor)
        assert session.get(Supplier, supplier_id).is_active is False


def test_reactivate_supplier_supports_independent_session_and_rejects_cross_database_actor(authenticated_actor):
    with database_connection.get_db() as session:
        supplier_id = _inactive_supplier(session, authenticated_actor)

    with database_connection.get_db() as independent_session:
        assert reactivate_supplier(
            independent_session, supplier_id, actor_context=authenticated_actor
        ).is_active is True
        independent_session.commit()

    other_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(bind=other_engine)
    other_session = OtherSession()
    try:
        with pytest.raises(AuthorizationError):
            reactivate_supplier(other_session, 1, actor_context=authenticated_actor)
    finally:
        other_session.close()
        other_engine.dispose()
