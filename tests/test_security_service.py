import pytest

from database.connection import get_db
from database.models import Permission, Role, RolePermission, User, UserAuditEvent
from services.security_service import (
    AuthenticationError,
    AuthorizationError,
    ActorContext,
    authenticate,
    change_password,
    create_first_administrator,
    create_user,
    logout,
    require_permission,
    PERMISSIONS,
    ROLE_MATRIX,
    _seed_security_catalogue,
    _trusted_sessions,
)
from sqlalchemy import select
from datetime import datetime, timedelta


PASSWORD = "Strong-password-2026!"


def setup_admin():
    with get_db() as session:
        return create_first_administrator(
            session, username="admin", password=PASSWORD, full_name="System Admin"
        ).id


def test_first_run_admin_is_argon2_hashed_and_authenticates():
    admin_id = setup_admin()
    with get_db() as session:
        state = authenticate(session, "admin", PASSWORD)
        assert state.user_id == admin_id
        assert PASSWORD not in session.get(User, admin_id).password_hash
        with pytest.raises(AuthenticationError):
            create_first_administrator(session, username="other", password=PASSWORD, full_name="Other")
        assert session.get(UserAuditEvent, 1).action == "first_administrator_created"


def test_user_creation_returns_no_password_hash_or_credential():
    admin_id = setup_admin()
    with get_db() as session:
        admin_context = authenticate(session, "admin", PASSWORD)
        created = create_user(
            session,
            actor_context=admin_context,
            username="credential-safe-user",
            password="User-password-2026!",
            full_name="Credential Safe User",
            role_names=["Procurement"],
        )
        assert created.username == "credential-safe-user"
        assert not hasattr(created, "password_hash")
        assert not hasattr(created, "password")
        stored_user = session.get(User, created.id)
        assert stored_user.password_hash.startswith("$argon2id$")
        assert stored_user.password_hash != "User-password-2026!"


def test_password_change_and_logout_invalidate_context():
    admin_id = setup_admin()
    with get_db() as session:
        context = authenticate(session, "admin", PASSWORD)
        with pytest.raises(TypeError, match="actor_context"):
            change_password(
                session,
                user_id=admin_id,
                current_password=PASSWORD,
                new_password="Changed-password-2026!",
            )
        change_password(
            session,
            actor_context=context,
            user_id=admin_id,
            current_password=PASSWORD,
            new_password="Changed-password-2026!",
        )
        logout(session, actor_context=context)
        with pytest.raises(AuthorizationError):
            require_permission(context, "users.manage", db_session=session)


def test_password_change_cannot_target_another_user_without_user_management():
    setup_admin()
    with get_db() as session:
        admin_context = authenticate(session, "admin", PASSWORD)
        operator = create_user(
            session,
            actor_context=admin_context,
            username="operator",
            password=PASSWORD,
            full_name="Operator",
            role_names=["Sales Operator"],
        )
        operator_context = authenticate(session, "operator", PASSWORD)
        with pytest.raises(AuthorizationError):
            change_password(
                session,
                actor_context=operator_context,
                user_id=admin_context.user_id,
                current_password=PASSWORD,
                new_password="Changed-password-2026!",
            )
        assert authenticate(session, "admin", PASSWORD).user_id == admin_context.user_id

        change_password(
            session,
            actor_context=operator_context,
            user_id=operator.id,
            current_password=PASSWORD,
            new_password="Changed-password-2026!",
        )
        change_password(
            session,
            actor_context=admin_context,
            user_id=operator.id,
            current_password=PASSWORD,
            new_password="Admin-reset-password-2026!",
        )
        with pytest.raises(AuthenticationError):
            authenticate(session, "operator", "Changed-password-2026!")
        assert authenticate(
            session, "operator", "Admin-reset-password-2026!"
        ).user_id == operator.id
        with pytest.raises(AuthorizationError):
            require_permission(
                operator_context, "sales.view", db_session=session
            )


def test_new_permissions_use_explicit_least_privilege_role_assignments():
    expected = {
        "accounting.manage", "accounting.configure", "accounting.periods.manage",
        "accounting.reverse", "accounting.exceptions.manage", "employees.view",
        "employees.private.view", "employees.export_private", "settings.view",
        "documents.view", "backups.view", "backups.create",
    }
    assert expected <= PERMISSIONS.keys()
    assert ROLE_MATRIX["System Administrator"] == set(PERMISSIONS)

    factory = ROLE_MATRIX["Factory Manager"]
    assert {"employees.view", "settings.view", "documents.view"} <= factory
    assert not {
        "backups.view", "backups.create", "backups.restore",
        "employees.private.view", "employees.export_private",
    } & factory

    finance = ROLE_MATRIX["Accounts & Finance"]
    assert {
        "accounting.manage", "accounting.configure",
        "accounting.periods.manage", "accounting.reverse",
        "accounting.exceptions.manage", "settings.view",
    } <= finance
    assert "settings.manage" not in finance

    hr = ROLE_MATRIX["HR/Employee Manager"]
    assert {
        "employees.view", "employees.manage", "employees.private.view",
        "employees.export_private", "documents.view",
    } <= hr

    assert not {
        "employees.view", "employees.private.view", "employees.export_private",
        "backups.view", "backups.create", "backups.restore",
    } & ROLE_MATRIX["Read-only Auditor"]


def test_catalogue_seed_removes_legacy_sensitive_factory_manager_grants():
    setup_admin()
    forbidden = {
        "backups.view", "backups.create", "backups.restore",
        "employees.private.view", "employees.export_private",
    }
    with get_db() as session:
        factory = session.scalar(select(Role).where(Role.name == "Factory Manager"))
        permissions = list(session.scalars(
            select(Permission).where(Permission.code.in_(forbidden))
        ))
        session.add_all(
            RolePermission(role_id=factory.id, permission_id=permission.id)
            for permission in permissions
        )
        session.flush()
        _seed_security_catalogue(session)
        remaining = set(session.scalars(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == factory.id)
        ))
    assert not forbidden & remaining


def test_roles_authorize_admin_and_deny_unprivileged_user():
    admin_id = setup_admin()
    with get_db() as session:
        admin_context = authenticate(session, "admin", PASSWORD)
        clerk = create_user(
            session, actor_context=admin_context, username="sales", password=PASSWORD,
            full_name="Sales User", role_names=["Sales Operator"],
        )
        clerk_context = authenticate(session, "sales", PASSWORD)
    with get_db() as session:
        assert require_permission(clerk_context, "sales.manage", db_session=session).id == clerk.id
        with pytest.raises(AuthorizationError):
            require_permission(clerk_context, "users.manage", db_session=session)
        fake = ActorContext(admin_id, "admin", "fabricated", admin_context.issued_at, admin_context.database_identity)
        with pytest.raises(AuthorizationError):
            require_permission(fake, "users.manage", db_session=session)
        assert session.query(UserAuditEvent).filter_by(action="authorization_denied").count() == 1


def test_create_user_rejects_missing_fabricated_and_expired_actors():
    setup_admin()
    with get_db() as session:
        with pytest.raises(AuthorizationError):
            create_user(
                session,
                actor_context=None,
                username="missing-actor",
                password=PASSWORD,
                full_name="Missing Actor",
                role_names=["Factory Manager"],
            )

        admin_context = authenticate(session, "admin", PASSWORD)
        fabricated = ActorContext(
            user_id=admin_context.user_id,
            username=admin_context.username,
            session_id="not-a-trusted-session",
            issued_at=admin_context.issued_at,
            database_identity=admin_context.database_identity,
        )
        with pytest.raises(AuthorizationError):
            create_user(
                session,
                actor_context=fabricated,
                username="fabricated-actor",
                password=PASSWORD,
                full_name="Fabricated Actor",
                role_names=["Factory Manager"],
            )

    trusted = _trusted_sessions[admin_context.session_id]
    trusted.last_activity = datetime.now() - timedelta(
        minutes=admin_context.timeout_minutes + 1
    )
    with get_db() as session:
        with pytest.raises(AuthorizationError):
            create_user(
                session,
                actor_context=admin_context,
                username="expired-actor",
                password=PASSWORD,
                full_name="Expired Actor",
                role_names=["Factory Manager"],
            )


def test_create_user_rejects_inactive_and_unprivileged_actors():
    admin_id = setup_admin()
    with get_db() as session:
        admin_context = authenticate(session, "admin", PASSWORD)
        operator = create_user(
            session,
            actor_context=admin_context,
            username="user-creation-operator",
            password=PASSWORD,
            full_name="User Creation Operator",
            role_names=["Procurement"],
        )
        operator_context = authenticate(
            session, operator.username, PASSWORD
        )
        with pytest.raises(AuthorizationError):
            create_user(
                session,
                actor_context=operator_context,
                username="unauthorized-create",
                password=PASSWORD,
                full_name="Unauthorized Create",
                role_names=["Factory Manager"],
            )

    with get_db() as session:
        session.get(User, admin_id).is_active = False
    with get_db() as session:
        with pytest.raises(AuthorizationError):
            create_user(
                session,
                actor_context=admin_context,
                username="inactive-admin-create",
                password=PASSWORD,
                full_name="Inactive Admin Create",
                role_names=["Factory Manager"],
            )


def test_create_user_duplicate_password_and_role_failures_roll_back():
    setup_admin()
    with get_db() as session:
        admin_context = authenticate(session, "admin", PASSWORD)
        create_user(
            session,
            actor_context=admin_context,
            username="duplicate-user",
            password=PASSWORD,
            full_name="Duplicate User",
            role_names=["Factory Manager"],
        )
        with pytest.raises(AuthenticationError, match="already exists"):
            create_user(
                session,
                actor_context=admin_context,
                username="duplicate-user",
                password=PASSWORD,
                full_name="Duplicate User",
                role_names=["Factory Manager"],
            )
        with pytest.raises(AuthenticationError, match="at least 12"):
            create_user(
                session,
                actor_context=admin_context,
                username="short-password",
                password="short",
                full_name="Short Password",
                role_names=["Factory Manager"],
            )

    with pytest.raises(AuthenticationError, match="roles do not exist"):
        with get_db() as session:
            create_user(
                session,
                actor_context=admin_context,
                username="role-rollback-user",
                password=PASSWORD,
                full_name="Role Rollback User",
                role_names=["Factory Manager", "Missing Built-in Role"],
            )

    with get_db() as session:
        assert session.scalar(
            select(User).where(User.username == "role-rollback-user")
        ) is None
        assert session.scalar(
            select(UserAuditEvent).where(
                UserAuditEvent.action == "user_created",
                UserAuditEvent.after_values.contains("role-rollback-user"),
            )
        ) is None
