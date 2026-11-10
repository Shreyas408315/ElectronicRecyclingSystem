from contextlib import contextmanager
from datetime import datetime
import hashlib
from pathlib import Path
import sqlite3
from sqlalchemy import create_engine, event
from sqlalchemy import inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "factory.db"

engine = create_engine(
    f"sqlite:///{DB_PATH.as_posix()}",
    connect_args={"check_same_thread": False, "timeout": 30},
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

@contextmanager
def get_db():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_verified_sqlite_backup(source_path: Path, backup_directory: Path) -> Path:
    """Create a non-overwriting, integrity-checked backup using SQLite's online backup API."""
    source_path = source_path.resolve()
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_stem = f"{source_path.stem}-pre-migration-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    backup_path = backup_directory / f"{backup_stem}.db"
    suffix = 1
    while True:
        try:
            with backup_path.open("xb"):
                pass
            break
        except FileExistsError:
            backup_path = backup_directory / f"{backup_stem}-{suffix}.db"
            suffix += 1

    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    destination = None
    try:
        destination = sqlite3.connect(str(backup_path))
        source.backup(destination)
        destination.commit()
        source_integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        backup_integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if source_integrity != "ok" or backup_integrity != "ok":
            raise RuntimeError(
                f"SQLite backup verification failed (source={source_integrity}, backup={backup_integrity})."
            )

        def dump_hash(connection: sqlite3.Connection) -> str:
            digest = hashlib.sha256()
            for line in connection.iterdump():
                digest.update(line.encode("utf-8"))
                digest.update(b"\n")
            return digest.hexdigest()

        if dump_hash(source) != dump_hash(destination):
            raise RuntimeError("SQLite backup verification failed: logical database contents differ.")
    finally:
        if destination is not None:
            destination.close()
        source.close()
    return backup_path


def _production_schema_changes_required(database_engine) -> bool:
    database_path = database_engine.url.database
    if not database_path or Path(database_path).resolve() != DB_PATH.resolve():
        return False
    if not DB_PATH.exists():
        return False
    inspector = inspect(database_engine)
    existing_tables = set(inspector.get_table_names())
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            return True
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if any(column.name not in existing_columns for column in table.columns):
            return True
    if "sales_invoice_items" in existing_tables and not _has_invoice_source_unique_index(inspector):
        return True
    for table_name in ("customer_receipts", "supplier_payments"):
        if table_name not in existing_tables:
            continue
        if any(
            index.get("unique")
            and tuple(index.get("column_names") or ()) == ("transaction_reference",)
            for index in inspector.get_indexes(table_name)
        ):
            return True
    return False


def _has_invoice_source_unique_index(database_inspector) -> bool:
    source_columns = ("invoice_id", "fulfilment_item_id")
    return any(
        index.get("unique")
        and tuple(index.get("column_names") or ()) == source_columns
        for index in database_inspector.get_indexes("sales_invoice_items")
    ) or any(
        tuple(constraint.get("column_names") or ()) == source_columns
        for constraint in database_inspector.get_unique_constraints("sales_invoice_items")
    )


def initialize_database():
    from database import models  # register model metadata
    migration_backup = None
    if _production_schema_changes_required(engine):
        migration_backup = create_verified_sqlite_backup(DB_PATH, ROOT / "reports" / "backups")

    Base.metadata.create_all(bind=engine)
    
    # Safe SQLite column migration: ensure existing tables get newly added columns
    from sqlalchemy import text
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if table_name in inspector.get_table_names():
                existing_cols = {col["name"] for col in inspector.get_columns(table_name)}
                for col in table.columns:
                    if col.name not in existing_cols:
                        col_type = col.type.compile(engine.dialect)
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}"))

        if "sales_invoice_items" in inspect(conn).get_table_names():
            invoice_item_inspector = inspect(conn)
            if not _has_invoice_source_unique_index(invoice_item_inspector):
                conn.execute(text(
                    "CREATE UNIQUE INDEX uq_sales_invoice_items_invoice_fulfilment_idx "
                    "ON sales_invoice_items (invoice_id, fulfilment_item_id)"
                ))

        if "sales_order_fulfilments" in inspect(conn).get_table_names():
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_sales_order_fulfilments_idempotency_key "
                "ON sales_order_fulfilments (idempotency_key)"
            ))

        _migrate_payment_reference_scopes(conn)

    if migration_backup is not None and "backup_records" in inspect(engine).get_table_names():
        from database.models import BackupRecord

        digest = hashlib.sha256()
        with migration_backup.open("rb") as backup_file:
            for chunk in iter(lambda: backup_file.read(1024 * 1024), b""):
                digest.update(chunk)
        with SessionLocal() as session:
            session.add(
                BackupRecord(
                    filename=migration_backup.name,
                    storage_path=str(migration_backup.resolve()),
                    backup_type="Pre-Migration",
                    file_size=migration_backup.stat().st_size,
                    checksum_sha256=digest.hexdigest(),
                    integrity_result="ok",
                    schema_compatible=True,
                    verification_status="Verified",
                    verification_details="SQLite online backup passed integrity and logical-content verification before additive migration.",
                )
            )
            session.commit()

    # Roles and permissions are reference data only.  No account, password, or
    # historical business record is created or changed at startup.
    from services.security_service import _seed_security_catalogue
    with SessionLocal() as session:
        _seed_security_catalogue(session)
        session.commit()


def _migrate_payment_reference_scopes(connection):
    from sqlalchemy import text

    inspector = inspect(connection)
    for table_name in ("customer_receipts", "supplier_payments"):
        if table_name not in inspector.get_table_names():
            continue
        for index in inspector.get_indexes(table_name):
            if (
                index.get("unique")
                and tuple(index.get("column_names") or ()) == ("transaction_reference",)
            ):
                index_name = index["name"].replace('"', '""')
                connection.execute(text(f'DROP INDEX "{index_name}"'))
        connection.execute(text(
            f"CREATE INDEX IF NOT EXISTS ix_{table_name}_transaction_reference "
            f"ON {table_name} (transaction_reference)"
        ))

    if "payment_transaction_reference_scopes" not in inspect(connection).get_table_names():
        return

    legacy_sources = (
        ("customer_receipts", "Customer Receipt"),
        ("supplier_payments", "Supplier Payment"),
    )
    for table_name, transaction_kind in legacy_sources:
        if table_name not in inspect(connection).get_table_names():
            continue
        rows = connection.execute(text(
            f"SELECT id, transaction_reference, payment_method, bank_account_name "
            f"FROM {table_name} "
            "WHERE status IN ('Posted', 'Voided') AND transaction_reference IS NOT NULL"
        )).mappings()
        for row in rows:
            account_scope = " ".join((row["bank_account_name"] or "").casefold().split())
            existing = connection.execute(text(
                "SELECT transaction_id FROM payment_transaction_reference_scopes "
                "WHERE transaction_reference = :reference "
                "AND transaction_kind = :kind "
                "AND payment_method = :method "
                "AND account_scope = :account"
            ), {
                "reference": row["transaction_reference"],
                "kind": transaction_kind,
                "method": row["payment_method"],
                "account": account_scope,
            }).scalar_one_or_none()
            if existing is not None:
                if existing != row["id"]:
                    raise RuntimeError(
                        "Payment reference scope migration found duplicate posted references; "
                        f"review {table_name} record #{row['id']} before continuing."
                    )
                continue
            connection.execute(text(
                "INSERT INTO payment_transaction_reference_scopes "
                "(transaction_reference, transaction_kind, payment_method, account_scope, "
                "transaction_id, created_at) "
                "VALUES (:reference, :kind, :method, :account, :transaction_id, CURRENT_TIMESTAMP)"
            ), {
                "reference": row["transaction_reference"],
                "kind": transaction_kind,
                "method": row["payment_method"],
                "account": account_scope,
                "transaction_id": row["id"],
            })
