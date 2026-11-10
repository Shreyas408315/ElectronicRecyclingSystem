"""Offline authentication, RBAC, and authenticated audit helpers.

No password, recovery secret, or session token is persisted in the database.
The desktop client keeps only the authenticated user id in memory.
"""
from __future__ import annotations

import json
import re
import secrets
from functools import wraps
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from database.models import Permission, Role, RolePermission, User, UserAuditEvent, UserRole


class AuthenticationError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


# Argon2id is provided by argon2-cffi.  These parameters are intentionally
# explicit and can be reviewed/raised during a future hardware assessment.
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
SESSION_TIMEOUT_MINUTES = 15

PERMISSIONS: dict[str, tuple[str, str, str]] = {
    "suppliers.view": ("Suppliers", "View", "View suppliers"),
    "suppliers.manage": ("Suppliers", "Manage", "Create or edit suppliers"),
    "customers.view": ("Customers", "View", "View customer records and financial terms"),
    "customers.manage": ("Customers", "Manage", "Create or edit customer records"),
    "receipts.view": ("Customer Receipts", "View", "View customer receipts, allocations, and statements"),
    "receipts.manage": ("Customer Receipts", "Manage Drafts", "Create or edit draft customer receipts and allocations"),
    "purchases.view": ("Purchases", "View", "View purchases"),
    "purchases.post": ("Purchases", "Post", "Create or post purchases"),
    "dismantling.view": ("Dismantling", "View", "View dismantling"),
    "dismantling.manage": ("Dismantling", "Manage", "Create, edit, or submit dismantling entries"),
    "dismantling.approve": ("Dismantling", "Approve", "Approve dismantling"),
    "inventory.view": ("Inventory", "View", "View inventory"),
    "inventory.manage": ("Inventory", "Manage", "Create, edit, or deactivate inventory catalogue items"),
    "inventory.adjust": ("Inventory", "Adjust", "Adjust or transfer inventory"),
    "inventory.dispatch": ("Inventory", "Sales Dispatch", "Record inventory changes for authorized sales dispatches"),
    "sales.view": ("Sales", "View", "View sales"),
    "sales.manage": ("Sales", "Manage", "Create, edit, or cancel sales orders"),
    "sales.confirm": ("Sales", "Confirm", "Confirm orders"),
    "sales.dispatch": ("Sales", "Dispatch", "Dispatch reserved sales-order stock"),
    "invoices.view": ("Sales Invoices", "View", "View sales invoices"),
    "invoices.manage": ("Sales Invoices", "Manage Drafts", "Create, edit, or cancel draft sales invoices"),
    "invoices.issue": ("Invoices", "Issue", "Issue invoices"),
    "invoices.void": ("Sales Invoices", "Void", "Void issued sales invoices"),
    "receipts.post": ("Receipts", "Post", "Post customer receipts"),
    "receipts.void": ("Customer Receipts", "Void", "Void posted customer receipts"),
    "supplier_payments.view": ("Supplier Payments", "View", "View supplier payments, allocations, and statements"),
    "supplier_payments.manage": ("Supplier Payments", "Manage Drafts", "Create or edit draft supplier payments and allocations"),
    "supplier_payments.post": ("Supplier Payments", "Post", "Post supplier payments"),
    "supplier_payments.void": ("Supplier Payments", "Void", "Void posted supplier payments"),
    "expenses.view": ("Expense Payments", "View", "View expense payments and history"),
    "expenses.manage": ("Expense Payments", "Manage Drafts", "Create or edit draft expense payments"),
    "expenses.post": ("Expenses", "Post", "Create or post expenses"),
    "expenses.void": ("Expense Payments", "Void", "Void posted expense payments"),
    "accounting.view": ("Accounting", "View", "View financial reports"),
    "accounting.manage": ("Accounting", "Manage Drafts", "Create or edit draft accounting journals"),
    "accounting.configure": ("Accounting", "Configure", "Manage the chart of accounts and accounting mappings"),
    "accounting.periods.manage": ("Accounting", "Manage Periods", "Configure, close, or reopen accounting periods"),
    "accounting.post": ("Accounting", "Post", "Post accounting journals"),
    "accounting.reverse": ("Accounting", "Reverse", "Reverse posted accounting journals"),
    "accounting.exceptions.manage": ("Accounting", "Manage Exceptions", "Retry or administer accounting exceptions"),
    "employees.view": ("Employees", "View", "View standard employee records"),
    "employees.manage": ("Employees", "Manage", "Manage employees"),
    "employees.private.view": ("Employees", "View Private Data", "View sensitive employee contact and emergency details"),
    "employees.export_private": ("Employees", "Export Private Data", "Export sensitive employee information"),
    "settings.view": ("Settings", "View", "View application settings"),
    "settings.manage": ("Settings", "Manage", "Manage settings"),
    "users.manage": ("Security", "Manage Users", "Manage users and roles"),
    "backups.view": ("Backups", "View", "List and verify database backups"),
    "backups.create": ("Backups", "Create", "Create verified database backups"),
    "backups.restore": ("Backups", "Restore", "Restore backups"),
    "documents.view": ("Documents", "View", "View and download managed documents"),
    "documents.manage": ("Documents", "Manage", "Upload or archive documents"),
}

ROLE_MATRIX: dict[str, set[str]] = {
    "System Administrator": set(PERMISSIONS),
    "Factory Manager": {
        "suppliers.view", "suppliers.manage", "customers.view", "customers.manage",
        "receipts.view", "receipts.manage", "purchases.view", "purchases.post",
        "dismantling.view", "dismantling.manage", "dismantling.approve",
        "inventory.view", "inventory.manage", "inventory.adjust", "inventory.dispatch",
        "sales.view", "sales.manage", "sales.confirm", "sales.dispatch",
        "invoices.view", "invoices.manage", "invoices.issue", "invoices.void",
        "receipts.post", "receipts.void", "supplier_payments.view",
        "supplier_payments.manage", "supplier_payments.post", "supplier_payments.void",
        "expenses.view", "expenses.manage", "expenses.post", "expenses.void",
        "accounting.view", "accounting.post", "accounting.manage",
        "employees.view", "employees.manage", "settings.view", "settings.manage",
        "documents.view", "documents.manage",
    },
    "Procurement": {
        "suppliers.view", "suppliers.manage", "purchases.view", "purchases.post",
        "documents.view", "documents.manage",
    },
    "Inventory Operator": {"dismantling.view", "inventory.view", "inventory.adjust"},
    "Sales Operator": {
        "customers.view", "customers.manage", "receipts.view", "inventory.view",
        "inventory.dispatch", "sales.view", "sales.manage", "sales.confirm",
        "sales.dispatch", "invoices.view", "invoices.manage", "invoices.issue",
        "documents.view", "documents.manage",
    },
    "Accounts & Finance": {
        "customers.view", "purchases.view", "sales.view", "invoices.view",
        "invoices.void", "receipts.view", "receipts.manage", "receipts.post",
        "receipts.void", "supplier_payments.view", "supplier_payments.manage",
        "supplier_payments.post", "supplier_payments.void", "expenses.view",
        "expenses.manage", "expenses.post", "expenses.void", "accounting.view",
        "accounting.manage", "accounting.configure", "accounting.periods.manage",
        "accounting.post", "accounting.reverse", "accounting.exceptions.manage",
        "settings.view", "documents.view", "documents.manage",
    },
    "HR/Employee Manager": {
        "employees.view", "employees.manage", "employees.private.view",
        "employees.export_private", "documents.view", "documents.manage",
    },
    "Read-only Auditor": {
        "suppliers.view", "customers.view", "receipts.view", "purchases.view",
        "dismantling.view", "inventory.view", "sales.view", "invoices.view",
        "supplier_payments.view", "expenses.view", "accounting.view",
    },
}


@dataclass(frozen=True)
class ActorContext:
    """Opaque, immutable principal issued only by authenticate()."""
    user_id: int
    username: str
    session_id: str
    issued_at: datetime
    database_identity: str
    timeout_minutes: int = SESSION_TIMEOUT_MINUTES


@dataclass(frozen=True)
class UserSummary:
    id: int
    username: str
    full_name: str
    employee_id: int | None
    is_active: bool


def _user_summary(user: User) -> UserSummary:
    return UserSummary(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        employee_id=user.employee_id,
        is_active=user.is_active,
    )


@dataclass
class _TrustedSession:
    context: ActorContext
    last_activity: datetime
    logged_out: bool = False


# Memory-only registry.  A fabricated ActorContext cannot pass validation
# because its opaque session_id will not reference this registry entry.
_trusted_sessions: dict[str, _TrustedSession] = {}


# Backwards-compatible exported name for UI code; it is now an immutable actor
# context, not a database-session-bound identity.
AuthenticatedSession = ActorContext


def _trusted_actor(context: ActorContext | None) -> ActorContext | None:
    if not isinstance(context, ActorContext):
        return None
    state = _trusted_sessions.get(context.session_id)
    if state is None or state.logged_out or state.context != context:
        return None
    if datetime.now() - state.last_activity >= timedelta(minutes=context.timeout_minutes):
        return None
    state.last_activity = datetime.now()
    return context


def _database_identity(session: Session) -> str:
    """Stable environment identity, not SQLAlchemy session-object identity."""
    engine = session.get_bind()
    identity = str(engine.url.render_as_string(hide_password=True))
    if engine.dialect.name == "sqlite" and engine.url.database in (None, "", ":memory:"):
        token = engine.__dict__.setdefault(
            "_trusted_database_identity", secrets.token_urlsafe(24)
        )
        return f"{identity}#{token}"
    return identity

_current_session: ContextVar[ActorContext | None] = ContextVar("current_security_session", default=None)


def validate_password_strength(password: str) -> None:
    if not isinstance(password, str) or len(password) < 12:
        raise AuthenticationError("Password must contain at least 12 characters.")
    if not re.search(r"[a-z]", password) or not re.search(r"[A-Z]", password):
        raise AuthenticationError("Password must include upper- and lower-case letters.")
    if not re.search(r"\d", password) or not re.search(r"[^A-Za-z0-9]", password):
        raise AuthenticationError("Password must include a number and a symbol.")


def _username(value: str) -> str:
    name = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", name):
        raise AuthenticationError("Username must be 3-80 letters, digits, dots, underscores, or hyphens.")
    return name


def _seed_security_catalogue(session: Session) -> None:
    """Idempotently create roles/permissions, never a user or password."""
    permission_ids: dict[str, int] = {}
    for code, (module, action, description) in PERMISSIONS.items():
        permission = session.scalar(select(Permission).where(Permission.code == code))
        if permission is None:
            permission = Permission(code=code, module=module, action=action, description=description)
            session.add(permission)
            session.flush()
        permission_ids[code] = permission.id
    for role_name, codes in ROLE_MATRIX.items():
        role = session.scalar(select(Role).where(Role.name == role_name))
        if role is None:
            role = Role(name=role_name, description=f"Built-in {role_name} role.", is_system=True)
            session.add(role)
            session.flush()
        existing = set(session.scalars(select(RolePermission.permission_id).where(RolePermission.role_id == role.id)))
        for code in codes:
            if permission_ids[code] not in existing:
                session.add(RolePermission(role_id=role.id, permission_id=permission_ids[code]))
        if role_name == "Factory Manager":
            legacy_sensitive_grants = {
                permission_ids[code]
                for code in (
                    "backups.view", "backups.create", "backups.restore",
                    "employees.private.view", "employees.export_private",
                )
            }
            session.execute(
                delete(RolePermission).where(
                    RolePermission.role_id == role.id,
                    RolePermission.permission_id.in_(legacy_sensitive_grants),
                )
            )
    session.flush()


def needs_first_run_setup(session: Session) -> bool:
    return session.scalar(select(User.id).limit(1)) is None


def _audit(session: Session, user_id: int | None, action: str, module: str, *, source_type: str | None = None, source_id: int | None = None, before: object = None, after: object = None) -> None:
    session.add(UserAuditEvent(
        user_id=user_id, action=action, module=module, source_type=source_type, source_id=source_id,
        before_values=json.dumps(before, default=str, sort_keys=True) if before is not None else None,
        after_values=json.dumps(after, default=str, sort_keys=True) if after is not None else None,
    ))


def create_first_administrator(session: Session, *, username: str, password: str, full_name: str, employee_id: int | None = None) -> UserSummary:
    if not needs_first_run_setup(session):
        raise AuthenticationError("An administrator already exists; use authorized user management.")
    _seed_security_catalogue(session)
    validate_password_strength(password)
    user = User(username=_username(username), password_hash=PASSWORD_HASHER.hash(password), full_name=(full_name or "").strip(), employee_id=employee_id)
    if not user.full_name:
        raise AuthenticationError("Full name is required.")
    session.add(user)
    session.flush()
    role = session.scalar(select(Role).where(Role.name == "System Administrator"))
    session.add(UserRole(user_id=user.id, role_id=role.id))
    _audit(session, user.id, "first_administrator_created", "Security", source_type="User", source_id=user.id)
    return _user_summary(user)


def authenticate(session: Session, username: str, password: str, *, timeout_minutes: int = SESSION_TIMEOUT_MINUTES) -> ActorContext:
    normalized_username = _username(username).lower()
    matching_users = session.scalars(
        select(User).where(func.lower(User.username) == normalized_username).limit(2)
    ).all()
    if len(matching_users) != 1:
        raise AuthenticationError("Invalid username or password.")
    user = matching_users[0]
    if not user.is_active:
        raise AuthenticationError("Invalid username or password.")
    try:
        valid = PASSWORD_HASHER.verify(user.password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        valid = False
    if not valid:
        raise AuthenticationError("Invalid username or password.")
    if PASSWORD_HASHER.check_needs_rehash(user.password_hash):
        user.password_hash = PASSWORD_HASHER.hash(password)
    user.last_login_at = datetime.now()
    context = ActorContext(user.id, user.username, secrets.token_urlsafe(32), datetime.now(), _database_identity(session), timeout_minutes)
    _trusted_sessions[context.session_id] = _TrustedSession(context, datetime.now())
    _current_session.set(context)
    _audit(session, user.id, "login", "Security", source_type="User", source_id=user.id)
    return context


def logout(session: Session | None = None, *, actor_context: ActorContext | None = None) -> None:
    state = actor_context or _current_session.get()
    if session is not None and _trusted_actor(state) is not None:
        _audit(session, state.user_id, "logout", "Security", source_type="User", source_id=state.user_id)
    if state is not None:
        trusted = _trusted_sessions.get(state.session_id)
        if trusted:
            trusted.logged_out = True
    _current_session.set(None)


def current_user_id() -> int | None:
    state = _trusted_actor(_current_session.get())
    return state.user_id if state else None


def current_actor_context() -> ActorContext | None:
    """Return the trusted in-memory principal for an interactive caller."""
    return _trusted_actor(_current_session.get())


def is_session_locked() -> bool:
    return _trusted_actor(_current_session.get()) is None


def require_permission(actor_context: ActorContext | None, permission_code: str, *, db_session: Session) -> User:
    """Validate a trusted actor against the business operation's DB session."""
    actor = _trusted_actor(actor_context)
    if actor is None:
        raise AuthorizationError("Sign in is required.")
    if actor.database_identity != _database_identity(db_session):
        raise AuthorizationError("The authenticated session belongs to a different database environment.")
    user = db_session.get(User, actor.user_id)
    if user is None or not user.is_active:
        raise AuthorizationError("The current user is inactive or unavailable.")
    allowed = db_session.scalar(
        select(Permission.id)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(UserRole.user_id == actor.user_id, Permission.code == permission_code)
        .limit(1)
    )
    if allowed is None:
        _audit(db_session, actor.user_id, "authorization_denied", "Security", after={"permission": permission_code})
        db_session.flush()
        raise AuthorizationError(f"Permission required: {permission_code}.")
    return user


def actor_has_permission(
    actor_context: ActorContext | None, permission_code: str, *, db_session: Session
) -> bool:
    try:
        require_permission(actor_context, permission_code, db_session=db_session)
    except AuthorizationError:
        return False
    return True


def protect_service_module(namespace: dict, *, view_permission: str, change_permission: str, report_permission: str | None = None) -> None:
    """Wrap public service entry points at module load time.

    The first positional argument of all protected business services is the
    SQLAlchemy Session.  Imported helpers and private functions are excluded.
    This makes direct calls subject to the same in-memory authenticated context
    as UI calls, before any business validation or mutation is performed.
    """
    read_prefixes = ("get_", "list_", "search_", "summary", "dashboard", "calculate_", "preview_")
    report_prefixes = ("export_", "generate_", "report_")
    module_name = namespace.get("__name__")
    for name, value in list(namespace.items()):
        if name.startswith("_") or not callable(value) or getattr(value, "__module__", None) != module_name:
            continue
        if name in {"protect_service_module"}:
            continue
        permission = view_permission if name.startswith(read_prefixes) else change_permission
        if report_permission and name.startswith(report_prefixes):
            permission = report_permission
        @wraps(value)
        def guarded(*args, __function=value, __permission=permission, **kwargs):
            if not args or not isinstance(args[0], Session):
                raise AuthorizationError("Protected service calls require a database session.")
            actor_context = kwargs.get("actor_context")
            require_permission(actor_context, __permission, db_session=args[0])
            return __function(*args, **kwargs)
        namespace[name] = guarded


def change_password(
    session: Session,
    *,
    actor_context: ActorContext,
    user_id: int,
    current_password: str,
    new_password: str,
) -> None:
    actor = _trusted_actor(actor_context)
    if actor is None:
        raise AuthorizationError("Sign in is required.")
    if actor.database_identity != _database_identity(session):
        raise AuthorizationError("The authenticated session belongs to a different database environment.")
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("User is not available.")
    credential_user = user
    if actor.user_id != user_id:
        credential_user = require_permission(
            actor_context, "users.manage", db_session=session
        )
    try:
        valid = PASSWORD_HASHER.verify(
            credential_user.password_hash, current_password
        )
    except (VerifyMismatchError, InvalidHashError):
        valid = False
    if not valid:
        raise AuthenticationError("Current password is incorrect.")
    validate_password_strength(new_password)
    user.password_hash = PASSWORD_HASHER.hash(new_password)
    user.password_changed_at = datetime.now()
    action = "password_changed" if actor.user_id == user_id else "password_reset"
    _audit(
        session, actor.user_id, action, "Security",
        source_type="User", source_id=user_id,
    )
    for state in _trusted_sessions.values():
        if (
            state.context.user_id == user_id
            and state.context.session_id != actor.session_id
        ):
            state.logged_out = True


def create_user(session: Session, *, actor_context: ActorContext, username: str, password: str, full_name: str, role_names: list[str], employee_id: int | None = None) -> UserSummary:
    actor = require_permission(actor_context, "users.manage", db_session=session)
    validate_password_strength(password)
    normalized_username = _username(username)
    if session.scalar(
        select(User.id).where(func.lower(User.username) == normalized_username.lower()).limit(1)
    ) is not None:
        raise AuthenticationError("Username already exists.")
    user = User(username=normalized_username, password_hash=PASSWORD_HASHER.hash(password), full_name=(full_name or "").strip(), employee_id=employee_id)
    if not user.full_name:
        raise AuthenticationError("Full name is required.")
    session.add(user)
    session.flush()
    roles = list(session.scalars(select(Role).where(Role.name.in_(role_names))))
    if len(roles) != len(set(role_names)):
        raise AuthenticationError("One or more selected roles do not exist.")
    for role in roles:
        session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()
    _audit(session, actor.id, "user_created", "Security", source_type="User", source_id=user.id, after={"username": user.username, "roles": role_names})
    return _user_summary(user)
