import calendar
from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from database.models import (
    Account,
    AccountMapping,
    AccountingPeriod,
    AccountingPeriodHistory,
    ApplicationSettingHistory,
    ApplicationSetting,
    JournalLine,
)
from services.accounting_common import (
    ACCOUNT_TYPES,
    AccountingValidationError,
    _require_active_account,
)
from services.security_service import ActorContext, require_permission

DEFAULT_ACCOUNT_SPECS = (
    ("1000", "Assets", "Asset", "Debit", "assets", True),
    ("1010", "Cash in Hand", "Cash", "Debit", "cash", False),
    ("1020", "Bank Accounts", "Bank", "Debit", "bank", False),
    ("1100", "Accounts Receivable", "Control", "Debit", "accounts_receivable", False),
    ("1200", "Inventory — Materials Awaiting Dismantling", "Inventory", "Debit", "inventory_raw", False),
    ("1210", "Inventory — Recovered Components", "Inventory", "Debit", "inventory_recovered", False),
    ("1300", "Other Current Assets", "Current Asset", "Debit", "other_current_assets", False),
    ("1310", "Input CGST", "GST Input", "Debit", "input_cgst", False),
    ("1320", "Input SGST", "GST Input", "Debit", "input_sgst", False),
    ("1330", "Input IGST", "GST Input", "Debit", "input_igst", False),
    ("2000", "Liabilities", "Liability", "Credit", "liabilities", True),
    ("2010", "Accounts Payable", "Control", "Credit", "accounts_payable", False),
    ("2020", "Expense Payables", "Current Liability", "Credit", "expense_payables", False),
    ("2030", "Customer Advances", "Current Liability", "Credit", "customer_advances", False),
    ("2040", "Output CGST", "GST Output", "Credit", "output_cgst", False),
    ("2050", "Output SGST", "GST Output", "Credit", "output_sgst", False),
    ("2060", "Output IGST", "GST Output", "Credit", "output_igst", False),
    ("2070", "Other Current Liabilities", "Current Liability", "Credit", "other_current_liabilities", False),
    ("3000", "Equity", "Equity", "Credit", "equity", True),
    ("3010", "Owner's Capital", "Capital", "Credit", "owners_capital", False),
    ("3020", "Retained Earnings", "Retained Earnings", "Credit", "retained_earnings", False),
    ("3030", "Opening Balance Equity", "Opening Balance Equity", "Credit", "opening_balance_equity", False),
    ("4000", "Income", "Income", "Credit", "income", True),
    ("4010", "Sales Revenue", "Sales", "Credit", "sales_revenue", False),
    ("4020", "Other Operating Income", "Operating Income", "Credit", "other_operating_income", False),
    ("4030", "Other Income", "Other Income", "Credit", "other_income", False),
    ("5000", "Expenses", "Expense", "Debit", "expenses", True),
    ("5010", "Cost of Goods Sold", "Cost of Goods Sold", "Debit", "cost_of_goods_sold", False),
    ("5110", "Electricity and Utilities", "Operating Expense", "Debit", "electricity_utilities", False),
    ("5120", "Factory Rent", "Operating Expense", "Debit", "factory_rent", False),
    ("5130", "Repairs and Maintenance", "Operating Expense", "Debit", "repairs_maintenance", False),
    ("5140", "Transportation and Logistics", "Operating Expense", "Debit", "transportation_logistics", False),
    ("5150", "Fuel", "Operating Expense", "Debit", "fuel", False),
    ("5160", "Tools and Consumables", "Operating Expense", "Debit", "tools_consumables", False),
    ("5170", "Packaging", "Operating Expense", "Debit", "packaging", False),
    ("5180", "Waste Disposal", "Operating Expense", "Debit", "waste_disposal", False),
    ("5190", "Office and Administration", "Operating Expense", "Debit", "office_administration", False),
    ("5200", "Communication and Internet", "Operating Expense", "Debit", "communication_internet", False),
    ("5210", "Insurance", "Operating Expense", "Debit", "insurance", False),
    ("5220", "Professional Fees", "Operating Expense", "Debit", "professional_fees", False),
    ("5230", "Employee Welfare", "Operating Expense", "Debit", "employee_welfare", False),
    ("5290", "Other Expenses", "Operating Expense", "Debit", "other_expenses", False),
    ("1400", "Supplier Advances", "Current Asset", "Debit", "supplier_advances", False),
)

ACCOUNT_TYPE_BY_KEY = {
    "assets": "Assets",
    "cash": "Assets",
    "bank": "Assets",
    "accounts_receivable": "Assets",
    "inventory_raw": "Assets",
    "inventory_recovered": "Assets",
    "other_current_assets": "Assets",
    "input_cgst": "Assets",
    "input_sgst": "Assets",
    "input_igst": "Assets",
    "liabilities": "Liabilities",
    "accounts_payable": "Liabilities",
    "expense_payables": "Liabilities",
    "customer_advances": "Liabilities",
    "output_cgst": "Liabilities",
    "output_sgst": "Liabilities",
    "output_igst": "Liabilities",
    "other_current_liabilities": "Liabilities",
    "equity": "Equity",
    "owners_capital": "Equity",
    "retained_earnings": "Equity",
    "opening_balance_equity": "Equity",
    "income": "Income",
    "sales_revenue": "Income",
    "other_operating_income": "Income",
    "other_income": "Income",
    "expenses": "Expenses",
    "cost_of_goods_sold": "Expenses",
    "electricity_utilities": "Expenses",
    "factory_rent": "Expenses",
    "repairs_maintenance": "Expenses",
    "transportation_logistics": "Expenses",
    "fuel": "Expenses",
    "tools_consumables": "Expenses",
    "packaging": "Expenses",
    "waste_disposal": "Expenses",
    "office_administration": "Expenses",
    "communication_internet": "Expenses",
    "insurance": "Expenses",
    "professional_fees": "Expenses",
    "employee_welfare": "Expenses",
    "other_expenses": "Expenses",
    "supplier_advances": "Assets",
}


def _period_month_start(year: int, month: int, day: int) -> date:
    bounded_day = min(day, calendar.monthrange(year, month)[1])
    return date(year, month, bounded_day)


def create_account(
    session: Session,
    *,
    actor_context: ActorContext,
    code: str,
    name: str,
    account_type: str,
    subtype: str | None = None,
    normal_balance: str | None = None,
    parent_id: int | None = None,
    description: str | None = None,
) -> Account:
    require_permission(actor_context, "accounting.configure", db_session=session)
    code, name = str(code).strip(), str(name).strip()
    if not code or not name:
        raise AccountingValidationError("Account code and name are required.")
    if account_type not in ACCOUNT_TYPES:
        raise AccountingValidationError("Select a valid account type.")
    expected_normal = "Debit" if account_type in {"Assets", "Expenses"} else "Credit"
    if normal_balance and normal_balance != expected_normal:
        raise AccountingValidationError(
            f"{account_type} accounts must have a {expected_normal.lower()} normal balance."
        )
    existing = session.scalar(select(Account).where(Account.code == code))
    if existing is not None:
        raise AccountingValidationError(f"Account code '{code}' already exists.")
    parent = session.get(Account, parent_id) if parent_id else None
    if parent_id and parent is None:
        raise AccountingValidationError("The selected parent account does not exist.")
    if parent is not None and parent.account_type != account_type:
        raise AccountingValidationError("Parent and child accounts must have the same account type.")
    account = Account(
        code=code,
        name=name,
        account_type=account_type,
        subtype=str(subtype).strip() if subtype else None,
        normal_balance=expected_normal,
        parent_id=parent.id if parent else None,
        description=str(description).strip() if description else None,
        is_system=False,
        is_active=True,
    )
    session.add(account)
    session.flush()
    return account


def update_account(
    session: Session, account_id: int, *, actor_context: ActorContext, **changes
) -> Account:
    require_permission(actor_context, "accounting.configure", db_session=session)
    account = session.get(Account, account_id)
    if account is None:
        raise AccountingValidationError(f"Account #{account_id} does not exist.")
    if account.is_system:
        forbidden = {"code", "account_type", "normal_balance", "system_key"} & changes.keys()
        if forbidden:
            raise AccountingValidationError("System account identity and type are protected.")
    if "name" in changes:
        name = str(changes["name"]).strip()
        if not name:
            raise AccountingValidationError("Account name is required.")
        account.name = name
    if "description" in changes:
        account.description = str(changes["description"]).strip() or None
    if "parent_id" in changes:
        parent_id = changes["parent_id"]
        parent = session.get(Account, parent_id) if parent_id else None
        if parent_id and parent is None:
            raise AccountingValidationError("The selected parent account does not exist.")
        if parent and (parent.id == account.id or parent.account_type != account.account_type):
            raise AccountingValidationError("Invalid parent account.")
        account.parent_id = parent.id if parent else None
    deactivate_requested = changes.pop("is_active", None) is False
    if "is_active" in changes:
        account.is_active = bool(changes["is_active"])
    account.updated_at = datetime.now()
    session.flush()
    return deactivate_account(
        session, account.id, actor_context=actor_context
    ) if deactivate_requested else account


def seed_default_accounts(
    session: Session, *, actor_context: ActorContext
) -> list[Account]:
    require_permission(actor_context, "accounting.configure", db_session=session)
    existing_by_key = {
        row.system_key: row
        for row in session.scalars(select(Account).where(Account.system_key.is_not(None)))
    }
    accounts: list[Account] = []
    for code, name, subtype, normal, key, is_group in DEFAULT_ACCOUNT_SPECS:
        account = existing_by_key.get(key)
        if account is None:
            account = session.scalar(select(Account).where(Account.code == code))
        account_type = ACCOUNT_TYPE_BY_KEY[key]
        if account is None:
            account = Account(
                code=code,
                name=name,
                account_type=account_type,
                subtype=subtype,
                normal_balance=normal,
                system_key=key,
                is_system=True,
                is_active=True,
                description="System chart-of-accounts entry.",
            )
            session.add(account)
            session.flush()
        elif account.system_key is None:
            account.system_key = key
            account.is_system = True
            session.flush()
        accounts.append(account)

    by_key = {account.system_key: account for account in accounts}
    for account, spec in zip(accounts, DEFAULT_ACCOUNT_SPECS):
        key, is_group = spec[4], spec[5]
        if is_group:
            continue
        parent_key = {
            "cash": "assets", "bank": "assets", "accounts_receivable": "assets",
            "inventory_raw": "assets", "inventory_recovered": "assets",
            "other_current_assets": "assets", "input_cgst": "assets",
            "input_sgst": "assets", "input_igst": "assets",
            "accounts_payable": "liabilities", "expense_payables": "liabilities",
            "customer_advances": "liabilities", "output_cgst": "liabilities",
            "output_sgst": "liabilities", "output_igst": "liabilities",
            "other_current_liabilities": "liabilities",
            "owners_capital": "equity", "retained_earnings": "equity",
            "opening_balance_equity": "equity",
            "sales_revenue": "income", "other_operating_income": "income",
            "other_income": "income",
            "cost_of_goods_sold": "expenses", "electricity_utilities": "expenses",
            "factory_rent": "expenses", "repairs_maintenance": "expenses",
            "transportation_logistics": "expenses", "fuel": "expenses",
            "tools_consumables": "expenses", "packaging": "expenses",
            "waste_disposal": "expenses", "office_administration": "expenses",
            "communication_internet": "expenses", "insurance": "expenses",
            "professional_fees": "expenses", "employee_welfare": "expenses",
            "other_expenses": "expenses", "supplier_advances": "assets",
        }.get(key)
        if parent_key and account.parent_id is None:
            account.parent_id = by_key[parent_key].id
    session.flush()
    return accounts


def list_accounts(
    session: Session, *, search: str | None = None, account_type: str | None = None,
    active_only: bool = False, actor_context: ActorContext,
) -> list[Account]:
    require_permission(actor_context, "accounting.view", db_session=session)
    stmt = select(Account).options(selectinload(Account.parent)).order_by(Account.code)
    if search:
        term = f"%{search.strip()}%"
        stmt = stmt.where(or_(Account.code.ilike(term), Account.name.ilike(term)))
    if account_type and account_type != "ALL":
        stmt = stmt.where(Account.account_type == account_type)
    if active_only:
        stmt = stmt.where(Account.is_active.is_(True))
    return list(session.scalars(stmt))


def configure_account_mapping(
    session: Session, mapping_key: str, account_id: int, description: str | None = None,
    *, actor_context: ActorContext,
) -> AccountMapping:
    require_permission(actor_context, "accounting.configure", db_session=session)
    key = str(mapping_key).strip()
    if not key:
        raise AccountingValidationError("Mapping key is required.")
    account = _require_active_account(session, account_id)
    if key in {
        "control:accounts_receivable", "asset:supplier_advances",
        "inventory:materials_awaiting_dismantling", "inventory:recovered_components",
    } or key.startswith("payment:"):
        expected_type = "Assets"
    elif key == "income:sales_revenue":
        expected_type = "Income"
    elif key == "expense:cost_of_goods_sold" or key.startswith("expense_category:"):
        expected_type = "Expenses"
    elif key in {
        "control:accounts_payable", "control:customer_advances",
        "control:expense_payables", "tax:output_cgst", "tax:output_sgst",
        "tax:output_igst",
    }:
        expected_type = "Liabilities"
    else:
        raise AccountingValidationError(f"Unsupported accounting mapping key '{key}'.")
    if account.account_type != expected_type:
        raise AccountingValidationError(
            f"Mapping '{key}' requires an {expected_type.lower()} account."
        )
    mapping = session.scalar(
        select(AccountMapping).where(AccountMapping.mapping_key == key)
    )
    if mapping is None:
        mapping = AccountMapping(mapping_key=key, account_id=account.id)
        session.add(mapping)
    mapping.account_id = account.id
    mapping.description = str(description).strip() if description else None
    mapping.is_active = True
    mapping.updated_at = datetime.now()
    session.flush()
    return mapping


def list_account_mappings(
    session: Session, *, actor_context: ActorContext
) -> list[AccountMapping]:
    require_permission(actor_context, "accounting.view", db_session=session)
    return list(session.scalars(
        select(AccountMapping).options(selectinload(AccountMapping.account))
        .order_by(AccountMapping.mapping_key)
    ))


def set_fiscal_year_start(
    session: Session, month: int, day: int = 1, *, actor_context: ActorContext
) -> None:
    require_permission(actor_context, "accounting.configure", db_session=session)
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise AccountingValidationError("Fiscal year start must be a valid month and day.")
    month_setting = session.get(ApplicationSetting, "accounting_fiscal_start_month")
    day_setting = session.get(ApplicationSetting, "accounting_fiscal_start_day")
    current_month = int(month_setting.value if month_setting else "4")
    current_day = int(day_setting.value if day_setting else "1")
    if (month, day) != (current_month, current_day) and session.scalar(
        select(AccountingPeriod.id).limit(1)
    ):
        raise AccountingValidationError(
            "Fiscal year start cannot be changed after accounting periods have been created."
        )
    with session.begin_nested():
        for key, value in (
            ("accounting_fiscal_start_month", str(month)),
            ("accounting_fiscal_start_day", str(day)),
        ):
            setting = session.get(ApplicationSetting, key)
            previous = setting.value if setting else None
            if previous == value:
                continue
            if setting is None:
                session.add(ApplicationSetting(key=key, value=value))
            else:
                setting.value = value
            session.add(ApplicationSettingHistory(
                setting_key=key,
                previous_value=previous,
                new_value=value,
                reason="Updated accounting fiscal year start",
            ))
        session.flush()


def _ensure_accounting_period(session: Session, target_date: date) -> AccountingPeriod:
    start_month = int(
        (session.get(ApplicationSetting, "accounting_fiscal_start_month").value
         if session.get(ApplicationSetting, "accounting_fiscal_start_month") else "4")
    )
    start_day = int(
        (session.get(ApplicationSetting, "accounting_fiscal_start_day").value
         if session.get(ApplicationSetting, "accounting_fiscal_start_day") else "1")
    )
    if not 1 <= start_month <= 12 or not 1 <= start_day <= 31:
        raise AccountingValidationError("Configured fiscal year start is invalid.")
    fy_start = _period_month_start(target_date.year, start_month, start_day)
    if target_date < fy_start:
        fy_start = _period_month_start(target_date.year - 1, start_month, start_day)
    def period_start_at(index: int) -> date:
        absolute_month = (fy_start.year * 12 + fy_start.month - 1) + index
        year, month0 = divmod(absolute_month, 12)
        return _period_month_start(year, month0 + 1, start_day)

    for index in range(12):
        period_start = period_start_at(index)
        period_end = period_start_at(index + 1)
        period_end = date.fromordinal(period_end.toordinal() - 1)
        period_code = f"FY{fy_start.year}-{(fy_start.year + 1) % 100:02d}-P{index + 1:02d}"
        period = session.scalar(
            select(AccountingPeriod).where(AccountingPeriod.period_code == period_code)
        )
        if period is None:
            period = AccountingPeriod(
                period_code=period_code,
                period_name=f"{period_start:%b %Y}",
                start_date=period_start,
                end_date=period_end,
                fiscal_year_start_month=start_month,
                status="Open",
            )
            session.add(period)
            session.flush()
        if period.start_date <= target_date <= period.end_date:
            return period
    raise AccountingValidationError("Could not locate an accounting period for this date.")


def ensure_accounting_period(
    session: Session, target_date: date, *, actor_context: ActorContext
) -> AccountingPeriod:
    require_permission(actor_context, "accounting.periods.manage", db_session=session)
    return _ensure_accounting_period(session, target_date)


def list_accounting_periods(
    session: Session, *, actor_context: ActorContext
) -> list[AccountingPeriod]:
    require_permission(actor_context, "accounting.view", db_session=session)
    return list(session.scalars(select(AccountingPeriod).order_by(AccountingPeriod.start_date.desc())))


def close_accounting_period(
    session: Session, period_id: int, reason: str, responsible_user: str | None = None,
    *, actor_context: ActorContext,
) -> AccountingPeriod:
    require_permission(actor_context, "accounting.periods.manage", db_session=session)
    period = session.get(AccountingPeriod, period_id)
    if period is None:
        raise AccountingValidationError(f"Accounting period #{period_id} does not exist.")
    reason = str(reason or "").strip()
    if not reason:
        raise AccountingValidationError("A reason is required to close an accounting period.")
    if period.status != "Open":
        raise AccountingValidationError("Only an open accounting period can be closed.")
    period.status = "Closed"
    period.closed_at = datetime.now()
    period.updated_at = datetime.now()
    session.add(AccountingPeriodHistory(
        period_id=period.id, event_type="CLOSED", previous_status="Open",
        new_status="Closed", reason=reason, responsible_user=responsible_user,
    ))
    session.flush()
    return period


def reopen_accounting_period(
    session: Session, period_id: int, reason: str, responsible_user: str | None = None,
    *, actor_context: ActorContext,
) -> AccountingPeriod:
    require_permission(actor_context, "accounting.periods.manage", db_session=session)
    period = session.get(AccountingPeriod, period_id)
    if period is None:
        raise AccountingValidationError(f"Accounting period #{period_id} does not exist.")
    reason = str(reason or "").strip()
    if not reason:
        raise AccountingValidationError("A reason is required to reopen an accounting period.")
    if period.status != "Closed":
        raise AccountingValidationError("Only a closed accounting period can be reopened.")
    period.status = "Open"
    period.reopened_at = datetime.now()
    period.updated_at = datetime.now()
    session.add(AccountingPeriodHistory(
        period_id=period.id, event_type="REOPENED", previous_status="Closed",
        new_status="Open", reason=reason, responsible_user=responsible_user,
    ))
    session.flush()
    return period


def deactivate_account(
    session: Session, account_id: int, *, actor_context: ActorContext
) -> Account:
    require_permission(actor_context, "accounting.configure", db_session=session)
    account = session.get(Account, account_id)
    if account is None:
        raise AccountingValidationError(f"Account #{account_id} does not exist.")
    if account.is_system:
        raise AccountingValidationError("System accounts cannot be deactivated.")
    if session.scalar(select(JournalLine.id).where(JournalLine.account_id == account.id).limit(1)):
        raise AccountingValidationError("An account referenced by a journal cannot be deactivated.")
    if session.scalar(
        select(AccountMapping.id).where(
            AccountMapping.account_id == account.id,
            AccountMapping.is_active.is_(True),
        ).limit(1)
    ):
        raise AccountingValidationError("An account used by an active mapping cannot be deactivated.")
    account.is_active = False
    account.updated_at = datetime.now()
    session.flush()
    return account
