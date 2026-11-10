import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import (
    AccountMapping,
    AccountingPeriod,
    ApplicationSetting,
    ApplicationSettingHistory,
    PaymentAccount,
)
from services.security_service import ActorContext, require_permission
from utils.validators import clean_string, validate_email, validate_gstin, validate_phone


class SettingsValidationError(ValueError):
    pass


SETTING_RULES: dict[str, tuple[int, str]] = {
    "factory_name": (200, "text"),
    "factory_address": (500, "text"),
    "factory_phone": (20, "phone"),
    "factory_email": (120, "email"),
    "factory_gstin": (15, "gstin"),
    "factory_state": (100, "text"),
    "factory_country": (100, "text"),
    "factory_pin_code": (20, "text"),
    "factory_pan": (10, "pan"),
    "factory_cin": (30, "text"),
    "default_currency": (3, "currency"),
    "currency_symbol": (8, "text"),
    "date_format": (20, "date_format"),
    "timezone": (80, "text"),
    "default_tax_treatment": (30, "tax_treatment"),
    "document_storage_path": (1000, "path"),
    "max_document_size_mb": (5, "positive_integer"),
    "backup_directory": (1000, "path"),
    "backup_retention_count": (5, "positive_integer"),
    "default_payment_terms": (100, "text"),
    "application_language": (20, "text"),
    "dashboard_refresh_seconds": (5, "positive_integer"),
    "session_timeout_minutes": (3, "positive_integer"),
    "accounting_fiscal_start_month": (2, "fiscal_month"),
    "accounting_fiscal_start_day": (2, "fiscal_day"),
}


def _validate(key: str, raw_value: object) -> str:
    if key not in SETTING_RULES:
        raise SettingsValidationError(f"Unknown or unsupported setting: {key}.")
    if raw_value is not None and not isinstance(raw_value, str):
        raise SettingsValidationError(f"{key} must be provided as text.")
    maximum, kind = SETTING_RULES[key]
    value = clean_string(raw_value)
    if value is None:
        return ""
    if len(value) > maximum:
        raise SettingsValidationError(f"{key} must be no more than {maximum} characters.")
    if kind == "email":
        valid, error = validate_email(value)
        if not valid:
            raise SettingsValidationError(error)
    elif kind == "phone":
        valid, error = validate_phone(value)
        if not valid:
            raise SettingsValidationError(error)
    elif kind == "gstin":
        valid, error = validate_gstin(value)
        if not valid:
            raise SettingsValidationError(error)
        value = value.upper()
    elif kind == "pan" and not re.fullmatch(r"[A-Za-z]{5}\d{4}[A-Za-z]", value):
        raise SettingsValidationError("PAN must contain 5 letters, 4 digits, and a final letter.")
    elif kind == "currency" and not re.fullmatch(r"[A-Za-z]{3}", value):
        raise SettingsValidationError("Currency must be a three-letter ISO currency code.")
    elif kind == "date_format" and value not in {"dd/MM/yyyy", "MM/dd/yyyy", "yyyy-MM-dd"}:
        raise SettingsValidationError("Choose a supported date format.")
    elif kind == "tax_treatment" and value not in {"NO_GST", "INTRA_STATE_GST", "INTER_STATE_GST"}:
        raise SettingsValidationError("Choose a supported tax treatment.")
    elif kind == "positive_integer":
        try:
            parsed = int(value)
        except ValueError as exc:
            raise SettingsValidationError(f"{key} must be a positive whole number.") from exc
        if parsed <= 0 or str(parsed) != value:
            raise SettingsValidationError(f"{key} must be a positive whole number.")
    elif kind in {"fiscal_month", "fiscal_day"}:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise SettingsValidationError(f"{key} must be a whole number.") from exc
        upper_bound = 12 if kind == "fiscal_month" else 31
        if not 1 <= parsed <= upper_bound or str(parsed) != value:
            raise SettingsValidationError(f"{key} must be between 1 and {upper_bound}.")
    elif kind == "path":
        if "\x00" in value:
            raise SettingsValidationError(f"{key} contains an invalid path.")
        if not Path(value).expanduser().is_absolute():
            raise SettingsValidationError(f"{key} must be an absolute directory path.")
    return value


def _get_setting(session: Session, key: str, default: str | None = None) -> str | None:
    setting = session.get(ApplicationSetting, key)
    return setting.value if setting is not None else default


def get_setting(
    session: Session,
    key: str,
    default: str | None = None,
    *,
    actor_context: ActorContext,
) -> str | None:
    require_permission(actor_context, "settings.view", db_session=session)
    return _get_setting(session, key, default)


def get_settings(
    session: Session, *, actor_context: ActorContext
) -> dict[str, str | None]:
    require_permission(actor_context, "settings.view", db_session=session)
    return {
        key: _get_setting(session, key)
        for key in SETTING_RULES
    }


def update_settings(
    session: Session,
    values: dict[str, object],
    *,
    actor_context: ActorContext,
    reason: str | None = None,
    responsible_user: str | None = None,
) -> dict[str, str | None]:
    require_permission(actor_context, "settings.manage", db_session=session)
    if not values:
        return {}
    if {"accounting_fiscal_start_month", "accounting_fiscal_start_day"} & values.keys():
        current_month = _get_setting(session, "accounting_fiscal_start_month", "4") or "4"
        current_day = _get_setting(session, "accounting_fiscal_start_day", "1") or "1"
        proposed_month = values.get("accounting_fiscal_start_month", current_month) or "4"
        proposed_day = values.get("accounting_fiscal_start_day", current_day) or "1"
        proposed_month = _validate("accounting_fiscal_start_month", proposed_month)
        proposed_day = _validate("accounting_fiscal_start_day", proposed_day)
        if (proposed_month, proposed_day) != (current_month, current_day) and session.scalar(
            select(AccountingPeriod.id).limit(1)
        ):
            raise SettingsValidationError(
                "Fiscal year start cannot be changed after accounting periods have been created."
            )
    with session.begin_nested():
        for key, raw_value in values.items():
            new_value = _validate(key, raw_value)
            setting = session.get(ApplicationSetting, key)
            previous_value = setting.value if setting else None
            stored_value = new_value or None
            if previous_value == stored_value:
                continue
            if stored_value is None:
                if setting:
                    session.delete(setting)
            elif setting:
                setting.value = stored_value
            else:
                session.add(ApplicationSetting(key=key, value=stored_value))
            session.add(
                ApplicationSettingHistory(
                    setting_key=key,
                    previous_value=previous_value,
                    new_value=stored_value,
                    reason=clean_string(reason),
                    responsible_user=clean_string(responsible_user),
                )
            )
        session.flush()
    return {key: _get_setting(session, key) for key in values}


def get_factory_profile(
    session: Session, *, actor_context: ActorContext
) -> dict[str, str | None]:
    require_permission(actor_context, "settings.view", db_session=session)
    keys = (
        "factory_name", "factory_address", "factory_phone", "factory_email",
        "factory_gstin", "factory_state", "factory_country", "factory_pin_code",
        "factory_pan", "factory_cin",
    )
    return {key: _get_setting(session, key) for key in keys}


def get_active_payment_accounts(
    session: Session, *, actor_context: ActorContext
) -> list[PaymentAccount]:
    require_permission(actor_context, "settings.view", db_session=session)
    return list(
        session.scalars(
            select(PaymentAccount)
            .where(PaymentAccount.is_active.is_(True))
            .order_by(PaymentAccount.display_name)
        )
    )


def get_payment_accounts(
    session: Session, *, actor_context: ActorContext
) -> list[PaymentAccount]:
    require_permission(actor_context, "settings.view", db_session=session)
    return list(
        session.scalars(
            select(PaymentAccount).order_by(PaymentAccount.display_name)
        )
    )


def save_payment_account(
    session: Session,
    *,
    display_name: str,
    account_type: str,
    bank_name: str | None = None,
    account_holder: str | None = None,
    account_identifier: str | None = None,
    ifsc: str | None = None,
    branch: str | None = None,
    account_mapping_id: int | None = None,
    actor_context: ActorContext,
) -> PaymentAccount:
    require_permission(actor_context, "settings.manage", db_session=session)
    name = clean_string(display_name)
    if not name:
        raise SettingsValidationError("Payment account name is required.")
    if account_type not in {"Cash", "Bank", "UPI Settlement", "Cheque Clearing", "Other"}:
        raise SettingsValidationError("Choose a supported payment account type.")
    if account_type == "Bank" and not clean_string(bank_name):
        raise SettingsValidationError("Bank name is required for a bank account.")
    identifier = clean_string(account_identifier)
    if identifier and len(re.sub(r"\D", "", identifier)) > 4:
        raise SettingsValidationError("Store only a masked account reference or its last four digits.")
    if account_type == "Bank" and ifsc and not re.fullmatch(r"[A-Za-z]{4}0[A-Za-z0-9]{6}", ifsc.strip()):
        raise SettingsValidationError("IFSC format is invalid.")
    if account_mapping_id is not None and session.get(AccountMapping, account_mapping_id) is None:
        raise SettingsValidationError("The selected accounting mapping does not exist.")
    duplicate = session.scalar(
        select(PaymentAccount.id).where(PaymentAccount.display_name == name)
    )
    if duplicate is not None:
        raise SettingsValidationError("A payment account with this name already exists.")
    account = PaymentAccount(
        display_name=name,
        account_type=account_type,
        bank_name=clean_string(bank_name),
        account_holder=clean_string(account_holder),
        account_identifier=identifier,
        ifsc=clean_string(ifsc).upper() if ifsc else None,
        branch=clean_string(branch),
        account_mapping_id=account_mapping_id,
    )
    session.add(account)
    session.flush()
    return account


def deactivate_payment_account(
    session: Session, account_id: int, *, actor_context: ActorContext
) -> None:
    require_permission(actor_context, "settings.manage", db_session=session)
    account = session.get(PaymentAccount, account_id)
    if account is None:
        raise SettingsValidationError("Payment account not found.")
    account.is_active = False


def payment_account_mappings(
    session: Session, *, actor_context: ActorContext
) -> list[AccountMapping]:
    require_permission(actor_context, "settings.view", db_session=session)
    return list(session.scalars(select(AccountMapping).order_by(AccountMapping.mapping_key)))
