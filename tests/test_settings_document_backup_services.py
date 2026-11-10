import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.connection import get_db
from database.models import (
    AccountingPeriod,
    ApplicationSetting,
    ApplicationSettingHistory,
    Customer,
    ManagedDocument,
    PaymentAccount,
)
from services.backup_management_service import (
    BackupValidationError,
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from services.document_service import (
    DocumentValidationError,
    archive_document,
    get_document_content,
    list_documents,
    upload_document,
)
from services.security_service import (
    AuthorizationError,
    authenticate,
    create_user,
    logout,
)
from services.settings_service import (
    SettingsValidationError,
    get_settings,
    save_payment_account,
    update_settings,
)


@pytest.fixture
def isolated_db(tmp_path):
    path = tmp_path / "settings-test.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield path, engine, factory
    engine.dispose()


@pytest.fixture
def admin_context(isolated_db, authenticated_actor_factory):
    _, _, factory = isolated_db
    with factory.begin() as session:
        return authenticated_actor_factory(session)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_settings_are_validated_persisted_and_audited(isolated_db, admin_context):
    _, _, factory = isolated_db
    with factory.begin() as session:
        update_settings(
            session,
            {"factory_name": "North Plant", "factory_gstin": "27ABCDE1234F1Z5"},
            actor_context=admin_context,
            reason="Initial setup",
        )
        history = list(session.scalars(select(ApplicationSettingHistory)))
        assert len(history) == 2
        assert session.get(ApplicationSetting, "factory_name").value == "North Plant"

    with factory.begin() as session:
        with pytest.raises(SettingsValidationError, match="valid"):
            update_settings(
                session,
                {"factory_email": "not-an-email"},
                actor_context=admin_context,
            )
        assert session.get(ApplicationSetting, "factory_email") is None


def test_scoped_public_apis_require_keyword_only_actor_context(isolated_db, tmp_path):
    _, _, factory = isolated_db
    with factory() as session:
        with pytest.raises(TypeError):
            get_settings(session)
        with pytest.raises(TypeError):
            update_settings(session, {"factory_name": "No context"})
        with pytest.raises(TypeError):
            upload_document(session, filename="file.txt", content=b"content")
        with pytest.raises(TypeError):
            list_documents(session)
        with pytest.raises(TypeError):
            create_backup(session, tmp_path / "active.db", tmp_path / "backups")
        assert not (tmp_path / "backups").exists()


def test_backup_permission_is_checked_before_filesystem_access(
    isolated_db, authenticated_actor_factory, tmp_path
):
    _, _, factory = isolated_db
    backup_directory = tmp_path / "backups"
    with factory.begin() as session:
        admin_context = authenticated_actor_factory(session)
        create_user(
            session,
            actor_context=admin_context,
            username="backup-viewer",
            password="Backup-viewer-2026!",
            full_name="Backup Viewer",
            role_names=["Read-only Auditor"],
        )
        viewer = authenticate(session, "backup-viewer", "Backup-viewer-2026!")
        with pytest.raises(AuthorizationError, match="backups.view"):
            list_backups(
                session, backup_directory, actor_context=viewer
            )
        with pytest.raises(AuthorizationError, match="backups.create"):
            create_backup(
                session,
                tmp_path / "missing-active.db",
                backup_directory,
                actor_context=viewer,
            )
        assert not backup_directory.exists()


def test_payment_accounts_mask_sensitive_identifiers_and_use_mapping_fk(isolated_db, admin_context):
    _, _, factory = isolated_db
    with factory.begin() as session:
        account = save_payment_account(
            session,
            display_name="Main Current Account",
            account_type="Bank",
            bank_name="Example Bank",
            account_identifier="1234",
            ifsc="ABCD0123456",
            actor_context=admin_context,
        )
        assert account.account_identifier == "1234"
        with pytest.raises(SettingsValidationError, match="last four"):
            save_payment_account(
                session,
                display_name="Second Account",
                account_type="Bank",
                bank_name="Example Bank",
                account_identifier="12345678",
                actor_context=admin_context,
            )
        assert len(list(session.scalars(select(PaymentAccount)))) == 1


def test_fiscal_year_settings_cannot_change_after_periods_exist(isolated_db, admin_context):
    _, _, factory = isolated_db
    with factory.begin() as session:
        session.add(AccountingPeriod(
            period_code="FY2026-04",
            period_name="April 2026",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
            fiscal_year_start_month=4,
        ))
    with factory.begin() as session:
        with pytest.raises(SettingsValidationError, match="cannot be changed"):
            update_settings(
                session,
                {
                    "accounting_fiscal_start_month": "5",
                    "accounting_fiscal_start_day": "1",
                },
                actor_context=admin_context,
            )
        assert session.get(ApplicationSetting, "accounting_fiscal_start_month") is None


def test_settings_ui_clears_protected_values_after_logout(qapp, authenticated_actor):
    from ui.settings_page import SettingsPage

    with get_db() as session:
        update_settings(
            session,
            {"factory_name": "Visible Factory"},
            actor_context=authenticated_actor,
        )

    page = SettingsPage()
    page.load_data()
    assert page.configuration_tab.fields["factory_name"].text() == "Visible Factory"
    assert not page.configuration_tab.save_button.isHidden()
    assert not page.documents_tab.upload_button.isHidden()
    assert not page.backups_tab.create_button.isHidden()

    logout(actor_context=authenticated_actor)
    page.load_data()

    assert page.configuration_tab.fields["factory_name"].text() == ""
    assert page.configuration_tab.save_button.isHidden()
    assert page.payment_accounts_tab.table.rowCount() == 0
    assert page.documents_tab.table.rowCount() == 0
    assert page.documents_tab.upload_button.isHidden()
    assert page.backups_tab.table.rowCount() == 0
    assert page.backups_tab.create_button.isHidden()
    page.close()
    qapp.processEvents()


def test_document_upload_checksum_archive_and_storage_path_safety(isolated_db, tmp_path, admin_context):
    _, _, factory = isolated_db
    storage = tmp_path / "managed-documents"
    with factory.begin() as session:
        update_settings(
            session,
            {"document_storage_path": str(storage.resolve())},
            actor_context=admin_context,
        )
        customer = Customer(
            customer_code="CUS-000001",
            customer_name="Test Customer",
            customer_type="Business",
        )
        session.add(customer)
        session.flush()
        doc = upload_document(
            session,
            filename=r"..\..\invoice.pdf",
            content=b"%PDF-1.7\nverified test document",
            category="Invoice",
            related_entity_type="Customer",
            related_entity_id=customer.id,
            description="Invoice attachment",
            actor_context=admin_context,
        )
        doc_id = doc.id
        assert Path(doc.storage_path).parent == storage.resolve()
        assert doc.checksum_sha256 == hashlib.sha256(b"%PDF-1.7\nverified test document").hexdigest()

    with factory.begin() as session:
        loaded, content = get_document_content(
            session, doc_id, actor_context=admin_context
        )
        assert loaded.original_filename == "invoice.pdf"
        assert content.startswith(b"%PDF-")
        Path(loaded.storage_path).write_bytes(b"tampered")
        with pytest.raises(DocumentValidationError, match="checksum"):
            get_document_content(session, doc_id, actor_context=admin_context)
        Path(loaded.storage_path).write_bytes(content)
        archive_document(
            session,
            doc_id,
            reason="Superseded by corrected attachment",
            actor_context=admin_context,
        )
        with pytest.raises(DocumentValidationError, match="[Aa]rchived"):
            get_document_content(session, doc_id, actor_context=admin_context)


def test_document_rejects_bad_file_signatures_and_unsafe_types(isolated_db, tmp_path, admin_context):
    _, _, factory = isolated_db
    storage = tmp_path / "managed-documents"
    with factory.begin() as session:
        update_settings(
            session,
            {"document_storage_path": str(storage.resolve())},
            actor_context=admin_context,
        )
        with pytest.raises(DocumentValidationError, match="contents"):
            upload_document(
                session, filename="fake.pdf", content=b"not a PDF",
                actor_context=admin_context,
            )
        with pytest.raises(DocumentValidationError, match="extension"):
            upload_document(
                session, filename="payload.exe", content=b"MZ",
                actor_context=admin_context,
            )
        with pytest.raises(DocumentValidationError, match="valid DOCX"):
            upload_document(
                session,
                filename="fake.docx",
                content=b"PK\x03\x04not-a-zip",
                actor_context=admin_context,
            )
        assert session.scalar(select(ManagedDocument.id)) is None


def test_document_file_is_removed_when_database_transaction_rolls_back(isolated_db, tmp_path, admin_context):
    _, _, factory = isolated_db
    storage = tmp_path / "managed-documents"
    session = factory()
    update_settings(
        session,
        {"document_storage_path": str(storage.resolve())},
        actor_context=admin_context,
    )
    document = upload_document(
        session, filename="rollback.txt", content=b"temporary attachment",
        actor_context=admin_context,
    )
    stored_path = Path(document.storage_path)
    assert stored_path.is_file()
    session.rollback()
    assert not stored_path.exists()
    assert session.scalar(select(ManagedDocument.id)) is None
    session.close()


def test_backup_is_verified_and_restore_creates_safety_copy(isolated_db, tmp_path, admin_context):
    path, engine, factory = isolated_db
    backup_dir = tmp_path / "backups"
    with factory.begin() as session:
        update_settings(
            session, {"factory_name": "Before Restore"},
            actor_context=admin_context,
        )

    result = create_backup(
        factory(), path, backup_dir, actor_context=admin_context
    )
    backup_path = Path(result["path"])
    assert result["integrity"] == "ok"
    assert result["sha256"] == hashlib.sha256(backup_path.read_bytes()).hexdigest()
    with factory() as session:
        assert verify_backup(
            session, backup_path, actor_context=admin_context
        )["schema_compatible"]

    with factory.begin() as session:
        update_settings(
            session, {"factory_name": "After Backup"},
            actor_context=admin_context,
        )
    restore = restore_backup(
        factory(),
        path,
        backup_path,
        actor_context=admin_context,
        confirmed=True,
        engine=engine,
        backup_directory=backup_dir,
    )
    assert restore["integrity"] == "ok"
    assert Path(restore["safety_backup"]).is_file()
    with sqlite3.connect(path) as connection:
        name = connection.execute(
            "SELECT value FROM application_settings WHERE key='factory_name'"
        ).fetchone()[0]
        history_count = connection.execute("SELECT COUNT(*) FROM backup_records").fetchone()[0]
        assert name == "Before Restore"
        assert history_count == 1


def test_restore_requires_confirmation_and_rejects_incompatible_database(isolated_db, tmp_path, admin_context):
    path, _, factory = isolated_db
    with pytest.raises(BackupValidationError, match="confirmation"):
        restore_backup(
            factory(), path, path, actor_context=admin_context, confirmed=False
        )

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    invalid = backup_dir / "invalid.db"
    with sqlite3.connect(invalid) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    with pytest.raises(BackupValidationError, match="compatibility"):
        restore_backup(
            factory(),
            path,
            invalid,
            actor_context=admin_context,
            confirmed=True,
            backup_directory=backup_dir,
        )
