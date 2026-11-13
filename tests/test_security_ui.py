from datetime import datetime, timedelta

import pytest
from PySide6.QtWidgets import QApplication, QDialog
from sqlalchemy import select

from database.connection import get_db
from database.models import Role, User, UserAuditEvent, UserRole
from services.security_service import (
    AuthorizationError,
    ActorContext,
    authenticate,
    create_user,
    logout,
)
from ui.security_page import CreateUserDialog, SecurityPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_security_page_user_creation_refreshes_and_audits(
    qapp, authenticated_actor
):
    page = SecurityPage(authenticated_actor)
    assert not page.create_button.isHidden()
    assert page.table.rowCount() == 1

    dialog = CreateUserDialog(authenticated_actor, page)
    dialog.username.setText("accept_factory")
    dialog.full_name.setText("Acceptance Factory Manager")
    dialog.password.setText("Factory-acceptance-2026!")
    dialog.confirm_password.setText("Factory-acceptance-2026!")
    role_index = dialog.role.findData("Factory Manager")
    assert role_index >= 0
    dialog.role.setCurrentIndex(role_index)
    dialog.create()

    assert dialog.result() == int(QDialog.DialogCode.Accepted)
    assert dialog.password.text() == ""
    assert dialog.confirm_password.text() == ""
    page.load_data()
    assert page.table.rowCount() == 2
    rows = {
        tuple(
            page.table.item(row, column).text()
            for column in range(page.table.columnCount())
        )
        for row in range(page.table.rowCount())
    }
    assert (
        "accept_factory",
        "Acceptance Factory Manager",
        "Active",
        "Factory Manager",
    ) in rows

    with get_db() as session:
        user = session.scalar(
            select(User).where(User.username == "accept_factory")
        )
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert "Factory-acceptance-2026!" not in user.password_hash
        assigned_roles = list(
            session.scalars(
                select(Role.name)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user.id)
            )
        )
        assert assigned_roles == ["Factory Manager"]
        event = session.scalar(
            select(UserAuditEvent).where(
                UserAuditEvent.action == "user_created",
                UserAuditEvent.source_id == user.id,
            )
        )
        assert event is not None
        assert "Factory-acceptance-2026!" not in (
            event.before_values or ""
        ) + (event.after_values or "")
        created_id = user.id

    with get_db() as session:
        authenticated = authenticate(
            session, "accept_factory", "Factory-acceptance-2026!"
        )
        assert authenticated.user_id == created_id
        logout(session, actor_context=authenticated)
    page.close()


def test_security_page_hides_creation_without_users_manage(
    qapp, authenticated_actor
):
    with get_db() as session:
        user = create_user(
            session,
            actor_context=authenticated_actor,
            username="security-page-procurement",
            password="Security-page-user-2026!",
            full_name="Security Page Procurement",
            role_names=["Procurement"],
        )
        actor = authenticate(
            session, user.username, "Security-page-user-2026!"
        )

    page = SecurityPage(actor)
    assert page.create_button.isHidden()
    assert page.table.rowCount() == 0
    assert "users.manage" in page.note.text()
    page.close()
    logout(actor_context=actor)


def test_create_user_dialog_rejects_mismatched_passwords(
    qapp, authenticated_actor
):
    dialog = CreateUserDialog(authenticated_actor)
    dialog.username.setText("mismatch-user")
    dialog.full_name.setText("Mismatch User")
    dialog.password.setText("Strong-password-2026!")
    dialog.confirm_password.setText("Different-password-2026!")
    dialog.create()

    assert "do not match" in dialog.error_label.text()
    with get_db() as session:
        assert session.scalar(
            select(User).where(User.username == "mismatch-user")
        ) is None
    dialog.close()


def test_open_dialog_cannot_create_after_logout(
    qapp, authenticated_actor
):
    dialog = CreateUserDialog(authenticated_actor)
    dialog.username.setText("after-logout-user")
    dialog.full_name.setText("After Logout User")
    dialog.password.setText("Strong-password-2026!")
    dialog.confirm_password.setText("Strong-password-2026!")
    with get_db() as session:
        logout(session, actor_context=authenticated_actor)

    dialog.create()
    assert "changed or expired" in dialog.error_label.text()
    assert dialog.password.text() == ""
    with get_db() as session:
        assert session.scalar(
            select(User).where(User.username == "after-logout-user")
        ) is None
    dialog.close()


def test_open_dialog_cannot_create_after_context_expiry(
    qapp, authenticated_actor, monkeypatch
):
    from services import security_service

    dialog = CreateUserDialog(authenticated_actor)
    dialog.username.setText("expired-context-user")
    dialog.full_name.setText("Expired Context User")
    dialog.password.setText("Strong-password-2026!")
    dialog.confirm_password.setText("Strong-password-2026!")
    state = security_service._trusted_sessions[authenticated_actor.session_id]
    monkeypatch.setattr(
        state,
        "last_activity",
        datetime.now() - timedelta(minutes=authenticated_actor.timeout_minutes + 1),
    )

    dialog.create()
    assert "changed or expired" in dialog.error_label.text()
    with get_db() as session:
        assert session.scalar(
            select(User).where(User.username == "expired-context-user")
        ) is None
    dialog.close()


def test_fabricated_actor_cannot_open_creation_dialog(
    qapp, authenticated_actor
):
    fabricated = ActorContext(
        user_id=authenticated_actor.user_id,
        username=authenticated_actor.username,
        session_id="fabricated-security-ui-context",
        issued_at=authenticated_actor.issued_at,
        database_identity=authenticated_actor.database_identity,
    )
    with pytest.raises(AuthorizationError):
        CreateUserDialog(fabricated)


def test_creation_service_errors_are_not_silently_accepted(
    qapp, authenticated_actor
):
    dialog = CreateUserDialog(authenticated_actor)
    dialog.username.setText("invalid-password-user")
    dialog.full_name.setText("Invalid Password User")
    dialog.password.setText("short")
    dialog.confirm_password.setText("short")
    dialog.create()
    assert "at least 12 characters" in dialog.error_label.text()
    assert dialog.result() != int(QDialog.DialogCode.Accepted)
    with get_db() as session:
        assert session.scalar(
            select(User).where(User.username == "invalid-password-user")
        ) is None
    dialog.close()
