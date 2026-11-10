from inspect import signature

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database.connection as database_connection
from database.connection import Base
from database.models import Supplier, User, UserAuditEvent, UserRole
from services.security_service import ActorContext, AuthorizationError, authenticate, create_user, logout
from services.supplier_service import SupplierValidationError, create_supplier


def _payload(name="Authorized supplier"):
    return {"business_name": name, "phone": "9876543210"}


def test_create_supplier_authorizes_before_validation_and_preserves_creation_behavior(authenticated_actor):
    with database_connection.get_db() as session:
        supplier = create_supplier(session, _payload(), actor_context=authenticated_actor)
        session.commit()
        assert supplier.supplier_code == "SUP-000001"
        assert supplier.is_active is True
        with pytest.raises(TypeError):
            signature(create_supplier).bind(session, _payload("Missing actor"))
        with pytest.raises(AuthorizationError):
            create_supplier(
                session, _payload("Fabricated"), actor_context=ActorContext(
                    authenticated_actor.user_id, authenticated_actor.username, "fabricated",
                    authenticated_actor.issued_at, authenticated_actor.database_identity,
                )
            )
        assert session.query(Supplier).count() == 1
        with pytest.raises(SupplierValidationError):
            create_supplier(session, {"business_name": "Bad", "phone": "123"}, actor_context=authenticated_actor)
        assert session.query(Supplier).count() == 1


def test_create_supplier_denials_are_audited_and_do_not_write(authenticated_actor):
    with database_connection.get_db() as session:
        clerk = create_user(session, actor_context=authenticated_actor, username="create-clerk",
            password="Create-clerk-password-2026!", full_name="Create Clerk", role_names=["Sales Operator"])
        session.commit()
        clerk_context = authenticate(session, "create-clerk", "Create-clerk-password-2026!")
        with pytest.raises(AuthorizationError):
            create_supplier(session, _payload(), actor_context=clerk_context)
        assert session.query(Supplier).count() == 0
        assert session.query(UserAuditEvent).filter_by(user_id=clerk.id, action="authorization_denied").count() == 1
        expired = authenticate(session, "fixture-admin", "Fixture-admin-password-2026!", timeout_minutes=0)
        with pytest.raises(AuthorizationError):
            create_supplier(session, _payload(), actor_context=expired)
        session.get(User, clerk.id).is_active = False; session.commit()
        with pytest.raises(AuthorizationError):
            create_supplier(session, _payload(), actor_context=clerk_context)
        revoked = create_user(session, actor_context=authenticated_actor, username="create-revoked",
            password="Create-revoked-password-2026!", full_name="Create Revoked", role_names=["Procurement"])
        session.commit()
        revoked_context = authenticate(session, "create-revoked", "Create-revoked-password-2026!")
        session.query(UserRole).filter_by(user_id=revoked.id).delete(); session.commit()
        with pytest.raises(AuthorizationError):
            create_supplier(session, _payload(), actor_context=revoked_context)
        assert session.query(UserAuditEvent).filter_by(user_id=revoked.id, action="authorization_denied").count() == 1
        logout(session, actor_context=authenticated_actor)
        with pytest.raises(AuthorizationError):
            create_supplier(session, _payload(), actor_context=authenticated_actor)


def test_create_supplier_supports_procurement_and_independent_session_but_rejects_other_database(authenticated_actor):
    with database_connection.get_db() as session:
        procurement = create_user(session, actor_context=authenticated_actor, username="create-procurement",
            password="Create-procurement-password-2026!", full_name="Create Procurement", role_names=["Procurement"])
        session.commit()
        procurement_context = authenticate(session, "create-procurement", "Create-procurement-password-2026!")
    with database_connection.get_db() as independent_session:
        assert create_supplier(independent_session, _payload(), actor_context=procurement_context).supplier_code == "SUP-000001"
        independent_session.commit()
    other_engine = create_engine("sqlite:///:memory:"); Base.metadata.create_all(other_engine)
    other_session = sessionmaker(bind=other_engine)()
    try:
        with pytest.raises(AuthorizationError):
            create_supplier(other_session, _payload(), actor_context=procurement_context)
    finally:
        other_session.close(); other_engine.dispose()
