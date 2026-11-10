from collections import Counter
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from database.models import (
    JournalEntry,
    JournalHistory,
    JournalLine,
)
from services.accounting_common import (
    AccountingValidationError,
    accounting_date,
    accounting_money,
    _get_open_period,
    _require_active_account,
)
from services.security_service import ActorContext, require_permission


def _normalized_lines(session: Session, rows: list[dict]) -> tuple[list[dict], Decimal, Decimal]:
    if len(rows) < 2:
        raise AccountingValidationError("A journal requires at least two lines.")
    normalized: list[dict] = []
    debit_total = Decimal("0.00")
    credit_total = Decimal("0.00")
    for index, row in enumerate(rows, start=1):
        account_id = row.get("account_id")
        try:
            account_id = int(account_id)
        except (TypeError, ValueError) as exc:
            raise AccountingValidationError(f"Line {index} requires an account.") from exc
        account = _require_active_account(session, account_id)
        debit = accounting_money(row.get("debit", 0), f"Line {index} debit")
        credit = accounting_money(row.get("credit", 0), f"Line {index} credit")
        if (debit > 0) == (credit > 0):
            raise AccountingValidationError(
                f"Line {index} must have exactly one positive debit or credit."
            )
        debit_total += debit
        credit_total += credit
        normalized.append({
            "account_id": account.id,
            "description": str(row.get("description") or "").strip() or None,
            "debit": debit,
            "credit": credit,
            "party_type": str(row.get("party_type") or "").strip() or None,
            "party_id": row.get("party_id"),
            "source_reference": str(row.get("source_reference") or "").strip() or None,
        })
    if debit_total != credit_total:
        raise AccountingValidationError(
            f"Journal is not balanced (debits {debit_total:.2f}, credits {credit_total:.2f})."
        )
    if debit_total <= 0:
        raise AccountingValidationError("Journal total must be greater than zero.")
    return normalized, debit_total, credit_total


def _append_history(
    session: Session, journal: JournalEntry, event_type: str,
    previous_status: str | None, new_status: str | None,
    reason: str | None = None, responsible_user: str | None = None,
) -> None:
    session.add(JournalHistory(
        journal_id=journal.id,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        reason=reason,
        responsible_user=responsible_user,
    ))


def create_draft_journal(
    session: Session,
    *,
    actor_context: ActorContext,
    journal_date: date,
    description: str,
    lines: list[dict],
    reference_number: str | None = None,
    responsible_user: str | None = None,
) -> JournalEntry:
    require_permission(actor_context, "accounting.manage", db_session=session)
    day = accounting_date(journal_date, "Journal date")
    _get_open_period(session, day)
    text = str(description or "").strip()
    if not text:
        raise AccountingValidationError("Journal description is required.")
    normalized, debit_total, credit_total = _normalized_lines(session, lines)
    journal = JournalEntry(
        journal_number="JRN-PENDING",
        journal_date=day,
        description=text,
        reference_number=str(reference_number or "").strip() or None,
        source_module="Manual",
        source_transaction_id=None,
        source_event_type="MANUAL",
        status="Draft",
        total_debit=debit_total,
        total_credit=credit_total,
        responsible_user=str(responsible_user or "").strip() or None,
    )
    with session.begin_nested():
        session.add(journal)
        session.flush()
        journal.journal_number = f"JRN-{journal.id:06d}"
        for line in normalized:
            session.add(JournalLine(journal_id=journal.id, **line))
        session.flush()
        _append_history(session, journal, "CREATED", None, "Draft", responsible_user=responsible_user)
        session.flush()
        session.expire(journal, ["history"])
    return journal


def get_journal_by_id(
    session: Session, journal_id: int, *, actor_context: ActorContext
) -> JournalEntry | None:
    require_permission(actor_context, "accounting.view", db_session=session)
    return session.scalar(
        select(JournalEntry).options(
            selectinload(JournalEntry.lines).selectinload(JournalLine.account),
            selectinload(JournalEntry.history),
        ).where(JournalEntry.id == journal_id)
    )


def update_draft_journal(
    session: Session,
    journal_id: int,
    *,
    actor_context: ActorContext,
    journal_date: date,
    description: str,
    lines: list[dict],
    reference_number: str | None = None,
    responsible_user: str | None = None,
) -> JournalEntry:
    require_permission(actor_context, "accounting.manage", db_session=session)
    journal = get_journal_by_id(session, journal_id, actor_context=actor_context)
    if journal is None:
        raise AccountingValidationError(f"Journal #{journal_id} does not exist.")
    if journal.status != "Draft" or journal.source_module != "Manual":
        raise AccountingValidationError("Only a manual draft journal can be edited.")
    day = accounting_date(journal_date, "Journal date")
    _get_open_period(session, day)
    text = str(description or "").strip()
    if not text:
        raise AccountingValidationError("Journal description is required.")
    normalized, debit_total, credit_total = _normalized_lines(session, lines)
    with session.begin_nested():
        for line in list(journal.lines):
            session.delete(line)
        session.flush()
        journal.journal_date = day
        journal.description = text
        journal.reference_number = str(reference_number or "").strip() or None
        journal.total_debit = debit_total
        journal.total_credit = credit_total
        journal.responsible_user = str(responsible_user or "").strip() or journal.responsible_user
        for line in normalized:
            session.add(JournalLine(journal_id=journal.id, **line))
        _append_history(
            session, journal, "UPDATED", "Draft", "Draft",
            "Draft journal edited.", responsible_user,
        )
        session.flush()
        session.expire(journal, ["lines", "history"])
    return journal


def validate_journal(
    session: Session, journal_id: int, *, actor_context: ActorContext
) -> dict:
    require_permission(actor_context, "accounting.view", db_session=session)
    journal = get_journal_by_id(session, journal_id, actor_context=actor_context)
    if journal is None:
        raise AccountingValidationError(f"Journal #{journal_id} does not exist.")
    normalized, debit_total, credit_total = _normalized_lines(
        session,
        [{
            "account_id": line.account_id, "debit": line.debit, "credit": line.credit,
            "description": line.description, "party_type": line.party_type,
            "party_id": line.party_id, "source_reference": line.source_reference,
        } for line in journal.lines],
    )
    return {
        "balanced": debit_total == credit_total,
        "debit_total": debit_total,
        "credit_total": credit_total,
        "line_count": len(normalized),
        "period": _get_open_period(session, journal.journal_date),
    }


def post_journal(
    session: Session, journal_id: int, *, actor_context: ActorContext,
    responsible_user: str | None = None
) -> JournalEntry:
    require_permission(actor_context, "accounting.post", db_session=session)
    journal = get_journal_by_id(session, journal_id, actor_context=actor_context)
    if journal is None:
        raise AccountingValidationError(f"Journal #{journal_id} does not exist.")
    if journal.status != "Draft":
        raise AccountingValidationError("Only a draft journal can be posted.")
    _get_open_period(session, journal.journal_date)
    _normalized_lines(session, [{
        "account_id": line.account_id, "debit": line.debit, "credit": line.credit,
        "description": line.description, "party_type": line.party_type,
        "party_id": line.party_id, "source_reference": line.source_reference,
    } for line in journal.lines])
    with session.begin_nested():
        previous = journal.status
        journal.status = "Posted"
        journal.posted_at = datetime.now()
        journal.responsible_user = str(responsible_user or "").strip() or journal.responsible_user
        if journal.source_module == "Manual" and journal.source_transaction_id is None:
            journal.source_transaction_id = journal.id
        _append_history(
            session, journal, "POSTED", previous, "Posted",
            responsible_user=journal.responsible_user,
        )
        session.flush()
        session.expire(journal, ["history"])
    return journal


def reverse_journal(
    session: Session,
    journal_id: int,
    reason: str,
    *,
    actor_context: ActorContext,
    reversal_date: date | None = None,
    responsible_user: str | None = None,
) -> JournalEntry:
    require_permission(actor_context, "accounting.reverse", db_session=session)
    original = get_journal_by_id(session, journal_id, actor_context=actor_context)
    if original is None:
        raise AccountingValidationError(f"Journal #{journal_id} does not exist.")
    if original.status != "Posted":
        raise AccountingValidationError("Only a posted journal can be reversed.")
    if original.source_module != "Manual":
        raise AccountingValidationError(
            "Source-generated journals must be reversed through their source transaction lifecycle."
        )
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise AccountingValidationError("A reason is required to reverse a journal.")
    existing = session.scalar(
        select(JournalEntry).where(JournalEntry.reversal_of_id == original.id)
    )
    if existing is not None:
        raise AccountingValidationError(
            f"Journal {original.journal_number} already has reversal {existing.journal_number}."
        )
    day = accounting_date(reversal_date or date.today(), "Reversal date")
    _get_open_period(session, day)
    reverse_lines = [{
        "account_id": line.account_id,
        "description": f"Reversal: {line.description}" if line.description else reason_text,
        "debit": line.credit,
        "credit": line.debit,
        "party_type": line.party_type,
        "party_id": line.party_id,
        "source_reference": line.source_reference or original.reference_number,
    } for line in original.lines]
    with session.begin_nested():
        reversal = JournalEntry(
            journal_number="JRN-PENDING",
            journal_date=day,
            description=f"Reversal of {original.journal_number}: {reason_text}",
            reference_number=original.journal_number,
            source_module="Reversal",
            source_transaction_id=original.id,
            source_event_type="REVERSAL",
            status="Posted",
            total_debit=original.total_credit,
            total_credit=original.total_debit,
            posted_at=datetime.now(),
            responsible_user=responsible_user or original.responsible_user,
            reversal_of_id=original.id,
        )
        session.add(reversal)
        session.flush()
        reversal.journal_number = f"JRN-{reversal.id:06d}"
        for row in reverse_lines:
            session.add(JournalLine(journal_id=reversal.id, **row))
        previous = original.status
        original.status = "Reversed"
        _append_history(session, original, "REVERSED", previous, "Reversed", reason_text, responsible_user)
        _append_history(session, reversal, "REVERSAL_POSTED", None, "Posted", reason_text, responsible_user)
        session.flush()
        session.expire(original, ["history"])
    return reversal


def list_journals(
    session: Session, *, search: str | None = None, status: str | None = None,
    source_module: str | None = None, start_date: date | None = None,
    end_date: date | None = None, limit: int = 500,
    actor_context: ActorContext,
) -> list[JournalEntry]:
    require_permission(actor_context, "accounting.view", db_session=session)
    stmt = select(JournalEntry).options(
        selectinload(JournalEntry.lines).selectinload(JournalLine.account)
    )
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            JournalEntry.journal_number.ilike(pattern)
            | JournalEntry.description.ilike(pattern)
            | JournalEntry.reference_number.ilike(pattern)
        )
    if status and status != "ALL":
        stmt = stmt.where(JournalEntry.status == status)
    if source_module and source_module != "ALL":
        stmt = stmt.where(JournalEntry.source_module == source_module)
    if start_date:
        stmt = stmt.where(JournalEntry.journal_date >= start_date)
    if end_date:
        stmt = stmt.where(JournalEntry.journal_date <= end_date)
    return list(session.scalars(
        stmt.order_by(JournalEntry.journal_date.desc(), JournalEntry.id.desc())
        .limit(max(1, min(limit, 2000)))
    ).unique())


def _create_posted_source_journal(
    session: Session,
    *,
    journal_date: date,
    description: str,
    reference_number: str | None,
    source_module: str,
    source_transaction_id: int,
    source_event_type: str,
    lines: list[dict],
    actor_context: ActorContext,
    authorization_permission: str,
    responsible_user: str | None = None,
    reversal_of_id: int | None = None,
) -> JournalEntry:
    from services.accounting_posting_service import (
        _build_source_event,
        _source_permission,
    )

    if authorization_permission == "accounting.exceptions.manage":
        try:
            _source_permission(source_module, source_event_type)
        except AccountingValidationError:
            if source_event_type != "CANCELLED":
                raise
    elif authorization_permission != _source_permission(
        source_module, source_event_type
    ):
        raise AccountingValidationError("Invalid source-journal authorization.")
    require_permission(
        actor_context, authorization_permission, db_session=session
    )
    day = accounting_date(journal_date, "Journal date")
    _get_open_period(session, day)
    normalized, debit_total, credit_total = _normalized_lines(session, lines)
    if reversal_of_id is None:
        expected_day, expected_description, expected_reference, expected_lines, _ = (
            _build_source_event(
                session, source_module, source_transaction_id, source_event_type
            )
        )
        if (
            day != expected_day
            or description != expected_description
            or reference_number != expected_reference
        ):
            raise AccountingValidationError(
                "Source journal details do not match the originating transaction."
            )
        expected_signatures = Counter(
            (
                row["account_id"], row["description"], row["debit"],
                row["credit"], row["party_type"], row["party_id"],
                row["source_reference"],
            )
            for row in expected_lines
        )
        actual_signatures = Counter(
            (
                row["account_id"], row["description"], row["debit"],
                row["credit"], row["party_type"], row["party_id"],
                row["source_reference"],
            )
            for row in normalized
        )
    else:
        source_models = {
            "Sales Invoice": SalesInvoice,
            "Customer Receipt": CustomerReceipt,
            "Supplier Payment": SupplierPayment,
            "Expense": Expense,
            "Expense Payment": ExpensePayment,
        }
        source_model = source_models.get(source_module)
        expected_status = (
            "Voided" if source_event_type == "VOIDED" else "Cancelled"
        )
        source = session.get(source_model, source_transaction_id) if source_model else None
        original = session.scalar(
            select(JournalEntry)
            .options(selectinload(JournalEntry.lines))
            .where(JournalEntry.id == reversal_of_id)
        )
        if (
            source is None
            or source.status != expected_status
            or original is None
            or original.status != "Posted"
            or original.source_module != source_module
            or original.source_transaction_id != source_transaction_id
        ):
            raise AccountingValidationError(
                "Source reversal does not match a posted journal for a voided transaction."
            )
        expected_signatures = Counter(
            (
                line.account_id, line.credit, line.debit, line.party_type,
                line.party_id, line.source_reference or original.reference_number,
            )
            for line in original.lines
        )
        actual_signatures = Counter(
            (
                row["account_id"], row["debit"], row["credit"],
                row["party_type"], row["party_id"], row["source_reference"],
            )
            for row in normalized
        )
        if (
            reference_number != original.reference_number
            or not description.startswith(f"Reversal of {original.journal_number}:")
        ):
            raise AccountingValidationError(
                "Source reversal details do not match the original journal."
            )
    if expected_signatures != actual_signatures:
        raise AccountingValidationError(
            "Source journal lines do not match the originating transaction."
        )
    if session.scalar(
        select(JournalEntry.id).where(
            JournalEntry.source_module == source_module,
            JournalEntry.source_transaction_id == source_transaction_id,
            JournalEntry.source_event_type == source_event_type,
        )
    ) is not None:
        raise AccountingValidationError(
            "An accounting entry already exists for this source event."
        )
    journal = JournalEntry(
        journal_number="JRN-PENDING",
        journal_date=day,
        description=str(description).strip(),
        reference_number=str(reference_number or "").strip() or None,
        source_module=source_module,
        source_transaction_id=source_transaction_id,
        source_event_type=source_event_type,
        status="Posted",
        total_debit=debit_total,
        total_credit=credit_total,
        posted_at=datetime.now(),
        responsible_user=responsible_user,
        reversal_of_id=reversal_of_id,
    )
    with session.begin_nested():
        session.add(journal)
        session.flush()
        journal.journal_number = f"JRN-{journal.id:06d}"
        for row in normalized:
            session.add(JournalLine(journal_id=journal.id, **row))
        _append_history(session, journal, "POSTED", None, "Posted", responsible_user=responsible_user)
        session.flush()
        session.expire(journal, ["history"])
    return journal
