import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database.connection as database_connection
from database.connection import Base
from database.models import Supplier, User, UserAuditEvent, UserRole
from services.security_service import ActorContext, AuthorizationError, authenticate, create_user, logout
from services.supplier_service import generate_next_supplier_code


def test_generate_supplier_code_authorizes_before_reads_and_preserves_sequence(authenticated_actor):
    with database_connection.get_db() as session:
        assert generate_next_supplier_code(session, actor_context=authenticated_actor) == "SUP-000001"
        session.add(Supplier(supplier_code="SUP-000007", business_name="Existing supplier", phone="9876543210"))
        session.commit()
        assert generate_next_supplier_code(session, actor_context=authenticated_actor) == "SUP-000008"

        with pytest.raises(TypeError):
            generate_next_supplier_code(session)
        with pytest.raises(AuthorizationError):
            generate_next_supplier_code(
                session,
                actor_context=ActorContext(
                    authenticated_actor.user_id,
                    authenticated_actor.username,
                    "fabricated-session",
                    authenticated_actor.issued_at,
                    authenticated_actor.database_identity,
                ),
            )
        assert session.query(Supplier).count() == 1


def test_generate_supplier_code_rejects_lifecycle_and_permission_denials_with_audit(authenticated_actor):
    with database_connection.get_db() as session:
        clerk = create_user(
            session,
            actor_context=authenticated_actor,
            username="code-clerk",
            password="Code-clerk-password-2026!",
            full_name="Code Clerk",
            role_names=["Sales Operator"],
        )
        session.commit()
        clerk_context = authenticate(session, "code-clerk", "Code-clerk-password-2026!")
        with pytest.raises(AuthorizationError):
            generate_next_supplier_code(session, actor_context=clerk_context)
        assert session.query(UserAuditEvent).filter_by(
            user_id=clerk.id, action="authorization_denied", module="Security"
        ).count() == 1

        expired = authenticate(session, "fixture-admin", "Fixture-admin-password-2026!", timeout_minutes=0)
        with pytest.raises(AuthorizationError):
            generate_next_supplier_code(session, actor_context=expired)
        session.get(User, clerk.id).is_active = False
        session.commit()
        with pytest.raises(AuthorizationError):
            generate_next_supplier_code(session, actor_context=clerk_context)

        revoked = create_user(
            session,
            actor_context=authenticated_actor,
            username="code-revoked",
            password="Code-revoked-password-2026!",
            full_name="Code Revoked",
            role_names=["Procurement"],
        )
        session.commit()
        revoked_context = authenticate(session, "code-revoked", "Code-revoked-password-2026!")
        session.query(UserRole).filter_by(user_id=revoked.id).delete()
        session.commit()
        with pytest.raises(AuthorizationError):
            generate_next_supplier_code(session, actor_context=revoked_context)
        assert session.query(UserAuditEvent).filter_by(
            user_id=revoked.id, action="authorization_denied", module="Security"
        ).count() == 1
        logout(session, actor_context=authenticated_actor)
        with pytest.raises(AuthorizationError):
            generate_next_supplier_code(session, actor_context=authenticated_actor)


def test_generate_supplier_code_supports_independent_sessions_and_rejects_other_database(authenticated_actor):
    with database_connection.get_db() as independent_session:
        assert generate_next_supplier_code(independent_session, actor_context=authenticated_actor) == "SUP-000001"

    other_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(other_engine)
    OtherSession = sessionmaker(bind=other_engine)
    other_session = OtherSession()
    try:
        with pytest.raises(AuthorizationError):
            generate_next_supplier_code(other_session, actor_context=authenticated_actor)
    finally:
        other_session.close()
        other_engine.dispose()
