import hashlib
from io import BytesIO
import mimetypes
import re
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select
from sqlalchemy import event
from sqlalchemy.orm import Session

from database.connection import ROOT
from database import models
from database.models import ApplicationSetting, ApplicationSettingHistory, ManagedDocument
from services.security_service import ActorContext, require_permission


ALLOWED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".csv", ".xlsx", ".docx"
}
DOCUMENT_CATEGORIES = {
    "Invoice", "Receipt", "Purchase", "Supplier", "Dismantling", "Inventory",
    "Customer", "Expense", "Accounting", "Employee Profile Photograph",
    "Appointment Letter", "Employment Agreement", "Training Certificate",
    "Qualification Certificate", "Employment Record", "General", "Other",
}
RELATED_ENTITY_TYPES = {
    "None", "Purchase", "Supplier", "Dismantling", "InventoryItem", "Customer",
    "SalesOrder", "SalesInvoice", "CustomerReceipt", "SupplierPayment", "Expense",
    "AccountingEntry", "Employee",
}
MAX_ABSOLUTE_SIZE = 100 * 1024 * 1024
RELATED_MODELS = {
    "Purchase": models.Purchase,
    "Supplier": models.Supplier,
    "Dismantling": models.DismantlingEntry,
    "InventoryItem": models.InventoryItem,
    "Customer": models.Customer,
    "SalesOrder": models.SalesOrder,
    "SalesInvoice": models.SalesInvoice,
    "CustomerReceipt": models.CustomerReceipt,
    "SupplierPayment": models.SupplierPayment,
    "Expense": models.Expense,
    "AccountingEntry": models.JournalEntry,
    "Employee": models.Employee,
}
RELATED_PERMISSIONS = {
    "Purchase": ("purchases.view", "purchases.post"),
    "Supplier": ("suppliers.view", "suppliers.manage"),
    "Dismantling": ("dismantling.view", "dismantling.manage"),
    "InventoryItem": ("inventory.view", "inventory.manage"),
    "Customer": ("customers.view", "customers.manage"),
    "SalesOrder": ("sales.view", "sales.manage"),
    "SalesInvoice": ("invoices.view", "invoices.manage"),
    "CustomerReceipt": ("receipts.view", "receipts.manage"),
    "SupplierPayment": ("supplier_payments.view", "supplier_payments.manage"),
    "Expense": ("expenses.view", "expenses.manage"),
    "AccountingEntry": ("accounting.view", "accounting.manage"),
    "Employee": ("employees.view", "employees.manage"),
}
CATEGORY_PERMISSIONS = {
    "Invoice": ("invoices.view", "invoices.manage"),
    "Receipt": ("receipts.view", "receipts.manage"),
    "Purchase": ("purchases.view", "purchases.post"),
    "Supplier": ("suppliers.view", "suppliers.manage"),
    "Dismantling": ("dismantling.view", "dismantling.manage"),
    "Inventory": ("inventory.view", "inventory.manage"),
    "Customer": ("customers.view", "customers.manage"),
    "Expense": ("expenses.view", "expenses.manage"),
    "Accounting": ("accounting.view", "accounting.manage"),
    "Employee Profile Photograph": ("employees.view", "employees.manage"),
    "Appointment Letter": ("employees.view", "employees.manage"),
    "Employment Agreement": ("employees.view", "employees.manage"),
    "Training Certificate": ("employees.view", "employees.manage"),
    "Qualification Certificate": ("employees.view", "employees.manage"),
    "Employment Record": ("employees.view", "employees.manage"),
}
PRIVATE_EMPLOYEE_CATEGORIES = {
    "Employee Profile Photograph",
    "Appointment Letter",
    "Employment Agreement",
    "Training Certificate",
    "Qualification Certificate",
    "Employment Record",
}


class DocumentValidationError(ValueError):
    pass


@event.listens_for(Session, "after_commit")
def _clear_pending_document_files(session: Session) -> None:
    session.info.pop("managed_document_pending_files", None)


@event.listens_for(Session, "after_rollback")
def _remove_pending_document_files(session: Session) -> None:
    for path in session.info.pop("managed_document_pending_files", set()):
        Path(path).unlink(missing_ok=True)


def _configured_setting(session: Session, key: str) -> str | None:
    setting = session.get(ApplicationSetting, key)
    return setting.value if setting is not None else None


def _get_storage_root(session: Session) -> Path:
    configured = _configured_setting(session, "document_storage_path")
    root = Path(configured).expanduser() if configured else ROOT / "data" / "documents"
    return root.resolve()


def get_storage_root(session: Session, *, actor_context: ActorContext) -> Path:
    require_permission(actor_context, "documents.view", db_session=session)
    return _get_storage_root(session)


def _require_related_permission(
    session: Session,
    entity_type: str,
    category: str,
    *,
    actor_context: ActorContext,
    manage: bool,
) -> None:
    permissions = []
    for mapping in (RELATED_PERMISSIONS.get(entity_type), CATEGORY_PERMISSIONS.get(category)):
        if mapping is not None:
            permission = mapping[1 if manage else 0]
            if permission not in permissions:
                permissions.append(permission)
    for permission in permissions:
        require_permission(actor_context, permission, db_session=session)
    if not manage and (
        entity_type == "Employee" or category in PRIVATE_EMPLOYEE_CATEGORIES
    ):
        require_permission(
            actor_context, "employees.private.view", db_session=session
        )


def _actor_permissions(session: Session, actor_context: ActorContext) -> set[str]:
    from database.models import Permission, RolePermission, UserRole

    return set(
        session.scalars(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == actor_context.user_id)
        )
    )


def _safe_original_name(filename: str) -> str:
    name = PureWindowsPath(filename).name
    name = PurePosixPath(name).name
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise DocumentValidationError("A valid document filename is required.")
    return name[:255]


def _validate_content(extension: str, content: bytes) -> str:
    if not content:
        raise DocumentValidationError("The selected document is empty.")
    signatures = {
        ".pdf": content.startswith(b"%PDF-"),
        ".png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": content.startswith(b"\xff\xd8\xff"),
        ".jpeg": content.startswith(b"\xff\xd8\xff"),
        ".webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
        ".xlsx": content.startswith(b"PK\x03\x04"),
        ".docx": content.startswith(b"PK\x03\x04"),
    }
    if extension in signatures and not signatures[extension]:
        raise DocumentValidationError("The file contents do not match the selected file type.")
    if extension in {".xlsx", ".docx"}:
        expected_entries = (
            {"[Content_Types].xml", "xl/workbook.xml"}
            if extension == ".xlsx"
            else {"[Content_Types].xml", "word/document.xml"}
        )
        try:
            with ZipFile(BytesIO(content)) as archive:
                if not expected_entries.issubset(archive.namelist()):
                    raise DocumentValidationError(
                        f"The selected file is not a valid {extension[1:].upper()} document."
                    )
        except BadZipFile as exc:
            raise DocumentValidationError(
                f"The selected file is not a valid {extension[1:].upper()} archive."
            ) from exc
    if extension in {".txt", ".csv"}:
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentValidationError("Text and CSV documents must be valid UTF-8.") from exc
    return mimetypes.guess_type(f"document{extension}")[0] or "application/octet-stream"


def upload_document(
    session: Session,
    *,
    filename: str,
    content: bytes,
    category: str = "General",
    related_entity_type: str = "None",
    related_entity_id: int | None = None,
    description: str | None = None,
    notes: str | None = None,
    responsible_user: str | None = None,
    actor_context: ActorContext,
) -> ManagedDocument:
    require_permission(actor_context, "documents.manage", db_session=session)
    original_name = _safe_original_name(filename)
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise DocumentValidationError("This file extension is not allowed.")
    if category not in DOCUMENT_CATEGORIES:
        raise DocumentValidationError("Choose a supported document category.")
    if related_entity_type not in RELATED_ENTITY_TYPES:
        raise DocumentValidationError("Choose a supported related record type.")
    _require_related_permission(
        session,
        related_entity_type,
        category,
        actor_context=actor_context,
        manage=True,
    )
    if related_entity_type == "None" and related_entity_id is not None:
        raise DocumentValidationError("Select a related record type for the supplied record ID.")
    if related_entity_type != "None":
        if related_entity_id is None or related_entity_id <= 0:
            raise DocumentValidationError("A valid related record ID is required.")
        if related_entity_type not in RELATED_MODELS or session.get(
            RELATED_MODELS[related_entity_type], related_entity_id
        ) is None:
            raise DocumentValidationError("The related business record does not exist.")
    max_size = int(_configured_setting(session, "max_document_size_mb") or "25") * 1024 * 1024
    if len(content) > min(max_size, MAX_ABSOLUTE_SIZE):
        raise DocumentValidationError("The document exceeds the configured file-size limit.")
    mime_type = _validate_content(extension, content)

    root = _get_storage_root(session)
    root.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{extension}"
    target = (root / stored_name).resolve()
    if target.parent != root:
        raise DocumentValidationError("The resolved document path is outside the storage directory.")
    checksum = hashlib.sha256(content).hexdigest()
    try:
        with target.open("xb") as file:
            file.write(content)
        document = ManagedDocument(
            document_code=f"DOC-{uuid.uuid4().hex[:12].upper()}",
            original_filename=original_name,
            stored_filename=stored_name,
            file_extension=extension,
            mime_type=mime_type,
            file_size=len(content),
            storage_path=str(target),
            category=category,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
            description=(description or "").strip() or None,
            notes=(notes or "").strip() or None,
            responsible_user=(responsible_user or "").strip() or None,
            checksum_sha256=checksum,
        )
        session.add(document)
        session.flush()
        session.info.setdefault("managed_document_pending_files", set()).add(str(target))
        return document
    except Exception:
        target.unlink(missing_ok=True)
        raise


def list_documents(
    session: Session,
    *,
    include_archived: bool = False,
    search: str | None = None,
    related_entity_type: str | None = None,
    related_entity_id: int | None = None,
    category: str | None = None,
    actor_context: ActorContext,
) -> list[ManagedDocument]:
    require_permission(actor_context, "documents.view", db_session=session)
    statement = select(ManagedDocument).order_by(ManagedDocument.uploaded_at.desc())
    if not include_archived:
        statement = statement.where(ManagedDocument.status == "Active")
    if search and search.strip():
        term = f"%{search.strip()}%"
        statement = statement.where(
            ManagedDocument.original_filename.ilike(term)
            | ManagedDocument.document_code.ilike(term)
            | ManagedDocument.description.ilike(term)
        )
    if related_entity_type:
        statement = statement.where(ManagedDocument.related_entity_type == related_entity_type)
    if related_entity_id is not None:
        statement = statement.where(ManagedDocument.related_entity_id == related_entity_id)
    if category:
        statement = statement.where(ManagedDocument.category == category)
    documents = list(session.scalars(statement))
    permissions = _actor_permissions(session, actor_context)
    visible = []
    for document in documents:
        related_permissions = []
        for mapping in (
            RELATED_PERMISSIONS.get(document.related_entity_type),
            CATEGORY_PERMISSIONS.get(document.category),
        ):
            if mapping is not None and mapping[0] not in related_permissions:
                related_permissions.append(mapping[0])
        if (
            all(code in permissions for code in related_permissions)
            and (
                document.related_entity_type != "Employee"
                and document.category not in PRIVATE_EMPLOYEE_CATEGORIES
                or "employees.private.view" in permissions
            )
        ):
            visible.append(document)
    return visible


def update_document_metadata(
    session: Session,
    document_id: int,
    *,
    category: str | None = None,
    description: str | None = None,
    notes: str | None = None,
    actor_context: ActorContext,
) -> ManagedDocument:
    require_permission(actor_context, "documents.manage", db_session=session)
    document = session.get(ManagedDocument, document_id)
    if document is None:
        raise DocumentValidationError("Document not found.")
    _require_related_permission(
        session,
        document.related_entity_type,
        document.category,
        actor_context=actor_context,
        manage=True,
    )
    if category is not None:
        _require_related_permission(
            session,
            document.related_entity_type,
            category,
            actor_context=actor_context,
            manage=True,
        )
    if document.status != "Active":
        raise DocumentValidationError("Archived document metadata cannot be edited.")
    if category is not None:
        if category not in DOCUMENT_CATEGORIES:
            raise DocumentValidationError("Choose a supported document category.")
        document.category = category
    if description is not None:
        document.description = description.strip() or None
    if notes is not None:
        document.notes = notes.strip() or None
    session.flush()
    return document


def get_document_content(
    session: Session, document_id: int, *, actor_context: ActorContext
) -> tuple[ManagedDocument, bytes]:
    require_permission(actor_context, "documents.view", db_session=session)
    document = session.get(ManagedDocument, document_id)
    if document is None:
        raise DocumentValidationError("Document not found.")
    _require_related_permission(
        session,
        document.related_entity_type,
        document.category,
        actor_context=actor_context,
        manage=False,
    )
    if document.status != "Active":
        raise DocumentValidationError("Archived documents cannot be opened.")
    path = Path(document.storage_path).resolve()
    allowed_roots = {_get_storage_root(session), (ROOT / "data" / "documents").resolve()}
    for history in session.scalars(
        select(ApplicationSettingHistory).where(
            ApplicationSettingHistory.setting_key == "document_storage_path"
        )
    ):
        for value in (history.previous_value, history.new_value):
            if value:
                allowed_roots.add(Path(value).expanduser().resolve())
    if path.parent not in allowed_roots:
        raise DocumentValidationError("Stored document path is outside the managed storage directory.")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise DocumentValidationError(f"Unable to read stored document: {exc}") from exc
    if hashlib.sha256(content).hexdigest() != document.checksum_sha256:
        raise DocumentValidationError("Document checksum verification failed.")
    return document, content


def archive_document(
    session: Session,
    document_id: int,
    *,
    reason: str,
    responsible_user: str | None = None,
    actor_context: ActorContext,
) -> ManagedDocument:
    require_permission(actor_context, "documents.manage", db_session=session)
    document = session.get(ManagedDocument, document_id)
    if document is None:
        raise DocumentValidationError("Document not found.")
    _require_related_permission(
        session,
        document.related_entity_type,
        document.category,
        actor_context=actor_context,
        manage=True,
    )
    if document.status != "Active":
        raise DocumentValidationError("Document is already archived.")
    if not reason.strip():
        raise DocumentValidationError("An archive reason is required.")
    document.status = "Archived"
    document.archive_reason = reason.strip()
    from datetime import datetime

    document.archived_at = datetime.now()
    document.responsible_user = (responsible_user or document.responsible_user or "").strip() or None
    session.flush()
    return document
