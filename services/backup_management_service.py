import hashlib
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from database.connection import ROOT, create_verified_sqlite_backup
from services.security_service import ActorContext, require_permission
from sqlalchemy.orm import Session


class BackupValidationError(ValueError):
    pass


REQUIRED_RESTORE_TABLES = {
    "application_settings",
    "customers",
    "suppliers",
    "purchases",
    "purchase_items",
    "inventory_items",
    "sales_orders",
    "sales_invoices",
}


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_sqlite_sidecars(database_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)


def _schema_compatibility(path: Path) -> tuple[bool, str]:
    try:
        connection = sqlite3.connect(path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                return False, f"SQLite integrity check failed: {integrity}"
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = REQUIRED_RESTORE_TABLES - tables
            if missing:
                return False, f"Missing required application tables: {', '.join(sorted(missing))}"
            foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_violations:
                return False, f"Foreign-key check found {len(foreign_key_violations)} violation(s)."
            return True, "Integrity, foreign keys, and required core schema verified."
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return False, f"Unable to inspect SQLite backup: {exc}"


def _record_backup(
    database_path: Path,
    backup_path: Path,
    backup_type: str,
    compatible: bool,
    details: str,
) -> None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                "INSERT INTO backup_records "
                "(filename, storage_path, backup_type, file_size, checksum_sha256, "
                "integrity_result, schema_compatible, verification_status, "
                "verification_details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    backup_path.name,
                    str(backup_path.resolve()),
                    backup_type,
                    backup_path.stat().st_size,
                    _checksum(backup_path),
                    "ok" if compatible else "failed",
                    int(compatible),
                    "Verified" if compatible else "Failed",
                    details,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
    except sqlite3.Error:
        raise


def _release_authorization_transaction(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise BackupValidationError(
            "Backup operations require a session without pending ORM changes."
        )
    session.rollback()


def create_backup(
    session: Session,
    database_path: Path,
    backup_directory: Path | None = None,
    *,
    actor_context: ActorContext,
    backup_type: str = "Manual",
) -> dict[str, object]:
    require_permission(actor_context, "backups.create", db_session=session)
    _release_authorization_transaction(session)
    database_path = database_path.resolve()
    directory = (backup_directory or ROOT / "reports" / "backups").resolve()
    if not database_path.is_file():
        raise BackupValidationError("The active database file does not exist.")
    backup = create_verified_sqlite_backup(database_path, directory)
    compatible, details = _schema_compatibility(backup)
    if not compatible:
        raise BackupValidationError(f"Created backup failed verification: {details}")
    try:
        _record_backup(database_path, backup, backup_type, compatible, details)
    except sqlite3.Error as exc:
        raise BackupValidationError(f"Backup is verified, but its history could not be recorded: {exc}") from exc
    return {
        "path": str(backup),
        "filename": backup.name,
        "size": backup.stat().st_size,
        "sha256": _checksum(backup),
        "integrity": "ok",
        "schema_compatible": True,
        "details": details,
    }


def _verify_backup(path: Path) -> dict[str, object]:
    path = path.resolve()
    if not path.is_file():
        raise BackupValidationError("Backup file does not exist.")
    compatible, details = _schema_compatibility(path)
    with closing(sqlite3.connect(path)) as connection:
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_violations:
        details += f" Foreign-key violations: {len(foreign_key_violations)}."
        compatible = False
    return {
        "path": str(path),
        "filename": path.name,
        "size": path.stat().st_size,
        "sha256": _checksum(path),
        "integrity": "ok" if compatible else "failed",
        "schema_compatible": compatible,
        "details": details,
    }


def verify_backup(
    session: Session, path: Path, *, actor_context: ActorContext
) -> dict[str, object]:
    require_permission(actor_context, "backups.view", db_session=session)
    _release_authorization_transaction(session)
    return _verify_backup(path)


def list_backups(
    session: Session, backup_directory: Path, *, actor_context: ActorContext
) -> list[dict[str, object]]:
    require_permission(actor_context, "backups.view", db_session=session)
    _release_authorization_transaction(session)
    if not backup_directory.exists():
        return []
    backup_directory = backup_directory.resolve()
    records: list[dict[str, object]] = []
    for path in sorted(backup_directory.glob("*.db"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.resolve().parent != backup_directory or not path.is_file():
            records.append({
                "path": str(path),
                "filename": path.name,
                "size": path.stat().st_size if path.exists() else 0,
                "sha256": "",
                "integrity": "failed",
                "schema_compatible": False,
                "details": "Backup path is not a regular file contained in the configured backup directory.",
            })
            continue
        try:
            records.append(_verify_backup(path))
        except (OSError, BackupValidationError) as exc:
            records.append({
                "path": str(path),
                "filename": path.name,
                "size": path.stat().st_size if path.exists() else 0,
                "sha256": "",
                "integrity": "failed",
                "schema_compatible": False,
                "details": str(exc),
            })
    return records


def restore_backup(
    session: Session,
    database_path: Path,
    backup_path: Path,
    *,
    actor_context: ActorContext,
    confirmed: bool,
    engine=None,
    backup_directory: Path | None = None,
) -> dict[str, object]:
    require_permission(actor_context, "backups.restore", db_session=session)
    _release_authorization_transaction(session)
    if not confirmed:
        raise BackupValidationError("Restore requires explicit confirmation.")
    database_path = database_path.resolve()
    backup_path = backup_path.resolve()
    if database_path == backup_path:
        raise BackupValidationError("The active database cannot be used as its own restore source.")
    allowed_backup_directory = (
        backup_directory or ROOT / "reports" / "backups"
    ).resolve()
    if backup_path.parent != allowed_backup_directory:
        raise BackupValidationError("Restore source must be inside the configured backup directory.")
    verification = _verify_backup(backup_path)
    if not verification["schema_compatible"]:
        raise BackupValidationError(f"Restore source failed compatibility checks: {verification['details']}")

    safety_copy = create_verified_sqlite_backup(database_path, allowed_backup_directory)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    stage_fd, stage_name = tempfile.mkstemp(
        prefix=f".{database_path.stem}-restore-", suffix=".db", dir=database_path.parent
    )
    os.close(stage_fd)
    stage_path = Path(stage_name)
    replaced_database = False
    try:
        with closing(sqlite3.connect(backup_path)) as source, closing(
            sqlite3.connect(stage_path)
        ) as destination:
            source.backup(destination)
            destination.commit()
        staged = _verify_backup(stage_path)
        if not staged["schema_compatible"]:
            raise BackupValidationError(f"Staged restore failed verification: {staged['details']}")

        if engine is not None:
            engine.dispose()
        with closing(sqlite3.connect(database_path)) as current:
            checkpoint = current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0] != 0:
                raise BackupValidationError("The active database could not be safely checkpointed for replacement.")
        os.replace(stage_path, database_path)
        replaced_database = True

        with closing(sqlite3.connect(database_path)) as restored:
            restored.execute("PRAGMA journal_mode=DELETE")
            restored.execute("PRAGMA foreign_keys=ON")
            violations = restored.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise BackupValidationError(
                    f"Restored database has {len(violations)} foreign-key violations."
                )
            restored_tables = {
                row[0]
                for row in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "backup_records" in restored_tables:
                restored.execute(
                    "INSERT INTO backup_records "
                    "(filename, storage_path, backup_type, file_size, checksum_sha256, "
                    "integrity_result, schema_compatible, verification_status, "
                    "verification_details, created_at) VALUES (?, ?, 'Restore', ?, ?, "
                    "'ok', 1, 'Verified', ?, ?)",
                    (
                        backup_path.name,
                        str(backup_path),
                        backup_path.stat().st_size,
                        _checksum(backup_path),
                        f"Restored after verified safety copy: {safety_copy.name}",
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            restored.commit()
        return {
            "restored_from": str(backup_path),
            "safety_backup": str(safety_copy),
            "integrity": "ok",
            "foreign_key_violations": 0,
        }
    except Exception:
        if replaced_database and safety_copy.exists():
            if engine is not None:
                engine.dispose()
            _remove_sqlite_sidecars(database_path)
            shutil.copy2(safety_copy, database_path)
        raise
    finally:
        stage_path.unlink(missing_ok=True)
