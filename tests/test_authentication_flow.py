import pytest
from unittest.mock import Mock
from PySide6.QtWidgets import QApplication, QDialog
from sqlalchemy import select

from database.connection import get_db
from database.models import RolePermission, User, UserRole
from services.security_service import (
    AuthenticationError,
    PASSWORD_HASHER,
    authenticate,
    create_first_administrator,
    create_user,
    current_actor_context,
    logout,
    needs_first_run_setup,
)


PASSWORD = "Strong-password-2026!"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_case_insensitive_login_preserves_existing_user_and_permissions():
    with get_db() as session:
        assert needs_first_run_setup(session)
        user = create_first_administrator(
            session, username="Ram123", password=PASSWORD, full_name="Test Administrator"
        )
        session.flush()
        user_id = user.id
        assert not needs_first_run_setup(session)
        original_username = user.username
        original_user_roles = set(
            session.scalars(select(UserRole.role_id).where(UserRole.user_id == user_id))
        )
        original_role_permissions = set(
            session.scalars(select(RolePermission.permission_id))
        )
        stored_hash = session.get(User, user_id).password_hash
        assert not hasattr(user, "password_hash")

    assert stored_hash.startswith("$argon2id$")
    assert PASSWORD_HASHER.verify(stored_hash, PASSWORD)

    logout()
    with get_db() as session:
        actor = authenticate(session, "RAM123", PASSWORD)
        assert actor.user_id == user_id
        assert actor.username == "Ram123"
        assert current_actor_context() == actor
        assert session.get(User, user_id).username == original_username
        assert set(session.scalars(select(UserRole.role_id).where(UserRole.user_id == user_id))) == original_user_roles
        assert set(session.scalars(select(RolePermission.permission_id))) == original_role_permissions
        logout(session, actor_context=actor)

    assert current_actor_context() is None
    with get_db() as session:
        for username, password in (("RAM123", "Incorrect-password-2026!"), ("unknown", PASSWORD)):
            with pytest.raises(AuthenticationError, match="Invalid username or password"):
                authenticate(session, username, password)
        assert current_actor_context() is None


def test_inactive_user_cannot_authenticate():
    with get_db() as session:
        user = create_first_administrator(
            session, username="inactive-admin", password=PASSWORD, full_name="Inactive Administrator"
        )
        session.get(User, user.id).is_active = False

    logout()
    with get_db() as session:
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            authenticate(session, "INACTIVE-ADMIN", PASSWORD)
        assert current_actor_context() is None


def test_ambiguous_case_insensitive_usernames_are_rejected():
    with get_db() as session:
        create_first_administrator(
            session, username="Ram123", password=PASSWORD, full_name="First Administrator"
        )
        session.flush()
        actor = authenticate(session, "Ram123", PASSWORD)
        with pytest.raises(AuthenticationError, match="Username already exists"):
            create_user(
                session,
                actor_context=actor,
                username="RAM123",
                password=PASSWORD,
                full_name="Duplicate Administrator",
                role_names=[],
            )
        logout(session, actor_context=actor)
        session.add(
            User(
                username="RAM123",
                password_hash=PASSWORD_HASHER.hash(PASSWORD),
                full_name="Second Administrator",
            )
        )

    logout()
    with get_db() as session:
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            authenticate(session, "ram123", PASSWORD)
        assert current_actor_context() is None


def test_first_run_and_sign_in_dialogs_smoke(qapp, monkeypatch):
    from ui.auth_dialogs import FirstRunAdministratorDialog, LoginDialog

    assert QApplication.instance() is qapp
    monkeypatch.setattr("ui.auth_dialogs.QMessageBox.warning", Mock(return_value=None))
    with get_db() as session:
        assert needs_first_run_setup(session)

    setup_dialog = FirstRunAdministratorDialog()
    setup_dialog.full_name.setText("UI Test Administrator")
    setup_dialog.username.setText("UiAdmin")
    setup_dialog.password.setText(PASSWORD)
    setup_dialog.confirm.setText(PASSWORD)
    setup_dialog.create()
    assert setup_dialog.result() == QDialog.DialogCode.Accepted

    with get_db() as session:
        assert not needs_first_run_setup(session)

    login_dialog = LoginDialog()
    login_dialog.username.setText("UIADMIN")
    login_dialog.password.setText(PASSWORD)
    login_dialog.login()
    assert login_dialog.result() == QDialog.DialogCode.Accepted
    assert login_dialog.authenticated_session is not None
    logout(actor_context=login_dialog.authenticated_session)
