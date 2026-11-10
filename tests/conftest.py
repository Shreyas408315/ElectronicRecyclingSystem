import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import database.connection as database_connection


@pytest.fixture(autouse=True)
def isolate_application_database(tmp_path, monkeypatch):
    """Keep UI integration tests and application DB access out of factory.db."""
    test_engine = create_engine(
        f"sqlite:///{(tmp_path / 'factory-test.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(test_engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    monkeypatch.setattr(database_connection, "engine", test_engine)
    monkeypatch.setattr(
        database_connection,
        "SessionLocal",
        sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False),
    )
    database_connection.initialize_database()
    try:
        yield
    finally:
        # Dispose before pytest removes tmp_path so SQLite has no live handles
        # (including WAL/journal sidecars) on Windows.
        test_engine.dispose()


@pytest.fixture
def authenticated_actor():
    """Issue a real, database-scoped Administrator context for RBAC callers."""
    from services.security_service import authenticate, create_first_administrator, logout

    with database_connection.get_db() as session:
        create_first_administrator(session, username="fixture-admin", password="Fixture-admin-password-2026!", full_name="Fixture Administrator")
    with database_connection.get_db() as session:
        context = authenticate(session, "fixture-admin", "Fixture-admin-password-2026!")
    try:
        yield context
    finally:
        with database_connection.get_db() as session:
            logout(session, actor_context=context)


@pytest.fixture
def authenticated_actor_factory():
    """Authenticate real administrators separately for custom isolated databases."""
    from services.security_service import authenticate, create_first_administrator, logout

    password = "Isolated-test-admin-2026!"
    administrators = {}
    contexts = []

    def authenticate_for(session):
        database_key = id(session.get_bind())
        credentials = administrators.get(database_key)
        if credentials is None:
            username = f"isolated-admin-{len(administrators) + 1}"
            create_first_administrator(
                session,
                username=username,
                password=password,
                full_name="Isolated Test Administrator",
            )
            session.flush()
            credentials = (username, password)
            administrators[database_key] = credentials
        context = authenticate(session, *credentials)
        contexts.append(context)
        return context

    yield authenticate_for

    for context in contexts:
        logout(actor_context=context)
