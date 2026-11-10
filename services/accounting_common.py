from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Account, AccountMapping, AccountingPeriod, ApplicationSetting

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
ACCOUNT_TYPES = ("Assets", "Liabilities", "Equity", "Income", "Expenses")
NORMAL_BALANCES = ("Debit", "Credit")


class AccountingValidationError(ValueError):
    pass


def accounting_money(value: object, label: str = "Amount", *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
        if not result.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AccountingValidationError(f"{label} must be a valid decimal amount.") from exc
    if result < ZERO or (positive and result <= ZERO):
        requirement = "greater than zero" if positive else "nonnegative"
        raise AccountingValidationError(f"{label} must be {requirement}.")
    return result


def accounting_date(value: object, label: str = "Date") -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise AccountingValidationError(f"{label} must be a valid date.") from exc


def _require_active_account(
    session: Session, account_id: int, *, posting: bool = True
) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise AccountingValidationError(f"Account #{account_id} does not exist.")
    if posting and not account.is_active:
        raise AccountingValidationError(f"Account {account.code} is inactive.")
    if posting and session.scalar(
        select(Account.id).where(Account.parent_id == account.id).limit(1)
    ):
        raise AccountingValidationError(
            f"Account {account.code} is a parent account and cannot receive journal postings."
        )
    return account


def _get_mapped_account(session: Session, mapping_key: str) -> Account:
    mapping = session.scalar(
        select(AccountMapping).where(
            AccountMapping.mapping_key == mapping_key,
            AccountMapping.is_active.is_(True),
        )
    )
    if mapping is None:
        raise AccountingValidationError(
            f"Required accounting mapping '{mapping_key}' is not configured."
        )
    return _require_active_account(session, mapping.account_id)


def _get_fiscal_setting(session: Session, key: str, default: str) -> str:
    setting = session.get(ApplicationSetting, key)
    return setting.value if setting is not None else default


def _get_open_period(session: Session, target_date: date) -> AccountingPeriod:
    from services.account_service import _ensure_accounting_period

    period = _ensure_accounting_period(session, target_date)
    if period.status != "Open":
        raise AccountingValidationError(
            f"Accounting period {period.period_name} is closed."
        )
    if not (period.start_date <= target_date <= period.end_date):
        raise AccountingValidationError("Accounting date is outside its accounting period.")
    return period
