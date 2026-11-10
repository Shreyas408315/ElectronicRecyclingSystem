# Electronic Recycling Factory Management System

A Windows desktop ERP application built using **Python**, **PySide6**, and **SQLAlchemy** with **SQLite** for managing electronic waste recovery, dismantling, procurement, and inventory operations.

---

## Setup & Execution (Windows PowerShell)

```powershell
# 1. Create and activate virtual environment
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Upgrade pip and install dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3. Run the application
python app.py
```

The SQLite database will be initialized automatically at `data/factory.db`.

---

## Running Automated Tests

Run the test suite using `pytest`:

```powershell
# Run all tests
pytest tests -v
```

For headless Windows validation, set `QT_QPA_PLATFORM=offscreen`. Tests create
temporary databases and document folders only; never run tests against
`data/factory.db`.

Automated unit and integration tests cover:
- Auto-generation of sequential `SUP-XXXXXX` and `PUR-XXXXXX` codes
- Multi-item appliance lines and automatic monetary calculation
- Additional costs: Transportation, loading, unloading, other expenses
- Payment statuses: Unpaid, Partially Paid, Paid
- Input validators (Email, Phone, GSTIN, PIN code, Opening Balance, Quantities, Rates)
- Supplier active constraint and relationship linking
- Database transactions, rollbacks, and persistence
- PDF purchase receipt generation using ReportLab
- UI search filtering across IDs, dates, categories, statuses, and sorting
- Supplier details ledger integration and live outstanding calculation
- Dismantling recovery, stock approval/reversal, reports, and inventory audit movements
- Expense draft/post/void lifecycle, itemized decimal totals, supplier linking, payment history, dashboard metrics, and PDF/Excel reports
- Settings validation and audit history, managed-document checksum/path safeguards, verified backup/restore workflows, and Settings UI smoke coverage
- Employee profile, department/designation, employment history, managed-document, privacy-aware report, and navigation coverage
- UI tests use isolated temporary SQLite databases and never write fixture records to `data/factory.db`

---

## Implemented Modules

### 1. Purchase Management
Accessible from the **Purchases** navigation item on the dark navy sidebar:
- **Purchase Dashboard & Real-Time KPIs**: Displays Total Purchases, Purchases Today, Purchases This Month, Total Purchase Expenditure, Total Amount Paid, and Total Outstanding Supplier Balance.
- **Inward Purchase Entry**: Comprehensive modal form with auto-generated Purchase ID (`PUR-000001`), date selection, active supplier picker, and auto-populated vendor details.
- **Multi-Appliance Line Items**: Supports recording multiple appliance categories in a single inward lot (e.g. Refrigerators, Washing Machines, Microwaves, ACs) with unit types (pieces/kg), weight, rate, and automatic line-total math.
- **Freight & Additional Expenses**: Tracks transportation, loading, unloading, and other expenses with automatic grand-total and balance calculations.
- **Decimal-Safe Precision**: Uses SQLAlchemy `Numeric(12, 2)` and Python `Decimal` with `ROUND_HALF_UP` to prevent floating-point inaccuracies.
- **PDF Purchase Receipts**: Generates clean, printable, formal purchase vouchers using ReportLab with factory details, line-item tables, and signature blocks.
- **Strict Inventory Separation**: Incoming appliances remain recorded as raw scrap; dismantled components only enter inventory upon subsequent Dismantling & Recovery certification.

### 2. Supplier Management
Accessible from the **Suppliers** navigation item on the dark navy sidebar:
- **Supplier List & Real-time Metrics**: Displays total registered suppliers, active suppliers, inactive suppliers, and total opening balance with responsive metric cards.
- **Auto-generated Supplier ID**: Automatically computes unique sequential codes formatted as `SUP-000001`.
- **Search & Filter**: Real-time multi-field search across ID, Business Name, Contact Person, Phone, City, and Email with Active/Inactive dropdown filters.
- **Sorting**: Multi-parameter sorting by Business Name (A-Z / Z-A), Registration Date (Newest / Oldest), and Supplier ID.
- **Add / Edit Supplier Form**: Comprehensive modal form with field validation (required business name, 10–13 digit phone numbers, email regex, 15-character uppercase GSTIN, 6-digit PIN codes, and numeric opening balance).
- **Soft Deactivation & Reactivation**: Safeguards historical ledger records by deactivating suppliers rather than permanently deleting them, with confirmation dialogs.
- **Supplier Details & Live Ledger**: Complete profile breakdown showing contact, location, registration timestamps, initial opening balance, live outstanding balance (Opening Balance + Unpaid Purchases), and linked Purchase History table.
- **Sidebar Brand Visibility**: Refined dark navy sidebar styling (`#0f172a`) ensuring crisp white typography for the brand header.
- **Auto-generated Supplier ID**: Automatically computes unique sequential codes formatted as `SUP-000001`.
- **Search & Filter**: Real-time multi-field search across ID, Business Name, Contact Person, Phone, City, and Email with Active/Inactive dropdown filters.
- **Sorting**: Multi-parameter sorting by Business Name (A-Z / Z-A), Registration Date (Newest / Oldest), and Supplier ID.
- **Add / Edit Supplier Form**: Comprehensive modal dialog with field validation (required business name, 10–13 digit phone numbers, email regex, 15-character uppercase GSTIN, 6-digit PIN codes, and numeric opening balance).
- **Soft Deactivation & Reactivation**: Safeguards historical ledger records by deactivating suppliers rather than permanently deleting them, with confirmation dialogs.
- **Supplier Details View**: Complete profile breakdown showing contact, location, registration timestamps, initial opening balance, outstanding balance, and an integrated purchase history tab ready for Purchase Management module integration.
- **Sidebar Brand Visibility**: Refined dark navy sidebar styling (`#0f172a`) ensuring crisp white typography for the brand header.

### 3. Dismantling & Recovery Management
Accessible from the **Dismantling & Recovery** navigation item:
- **Purchase-linked batches**: Select an existing purchase and purchase item; supplier, appliance details, purchased quantity, and remaining quantity are derived from those records.
- **Partial processing**: Draft, pending, and approved batches reserve the dismantled appliance quantity so repeat batches cannot exceed the purchase-item total; rejected/reversed batches release that quantity.
- **Recovery and scrap details**: Record multiple reusable/recyclable components, damaged/non-working items, recyclable scrap, and unusable waste with decimal-safe quantity, weight, and valuation fields. Component and scrap lines can be edited or removed before saving.
- **Dashboard and filters**: Track purchased/dismantled/pending quantities, batches, component quantity, recovered/scrap weight, and labour expenses, with date, appliance category, status, search, and sort filters.
- **Approval-controlled inventory**: Draft/pending/rejected batches do not post stock. Approval adds only working/recyclable components and explicitly recyclable scrap, in the same transaction as the approval and source-linked `StockMovement` records. Duplicate approvals are rejected.
- **Correction history**: Approved entries cannot be edited; authorized reversals create negative audit movements and restore the appliance quantity.
- **Reports**: PDF and Excel exports include batch, appliance-wise, component, scrap, daily/monthly, labour, recovery-percentage, and estimated-valuation data. Estimated recovery value is not recorded as sales revenue.
- **Inventory management**: The Inventory navigation page provides recovered-material stock, reservations, availability, movements, adjustments, transfers, low-stock monitoring, valuation, and PDF/Excel reporting.

### 4. Expenses Management
Accessible from the **Expenses** navigation item:
- **Operating expense register**: Create and edit draft expenses, post them for reporting, and void posted records with a required reason and retained audit history. Posted expense details are immutable.
- **Supplier integration**: Optionally link an expense to an existing supplier without treating it as a purchase or changing supplier purchase balances.
- **Itemized expenses**: Record multiple expense lines with decimal-safe quantities, discounts, tax rates, and calculated totals.
- **Separate expense payments**: Record draft payments and post or void them with audit history. Payment totals are derived from posted payment records and do not use supplier-payment or customer-receipt tables.
- **Dashboard and reports**: Show current-month posted expenses, current-month posted payments by payment date, and all outstanding posted expenses. Export the expense register, category/payee summaries, monthly/yearly summaries, payment register, and outstanding expenses to PDF or Excel.
- **Database safety**: Expense tables are additive. Automated expense tests use isolated in-memory SQLite databases; startup migration and backup behavior remains managed by the existing database initialization code.

### 5. Accounting & Financial Reporting
Accessible from the **Accounting** navigation item:
- **Double-entry journals**: Decimal-safe, balanced manual drafts and source-generated postings use unique journal numbers, immutable posted lines, status history, reversals, and open/closed fiscal periods.
- **Explicit account mappings**: Initialize the standard chart of accounts, create additional accounts, and map each supported source to a compatible account type. Missing mappings create review exceptions rather than blocking purchases, invoices, receipts, supplier payments, expenses, expense payments, or dispatches.
- **Initial mapping keys**: `control:accounts_receivable`, `control:accounts_payable`, `control:customer_advances`, `control:expense_payables`, `asset:supplier_advances`, `inventory:materials_awaiting_dismantling`, `inventory:recovered_components`, `income:sales_revenue`, `expense:cost_of_goods_sold`, and `tax:output_cgst` / `tax:output_sgst` / `tax:output_igst`. Cash/bank mappings use `payment:<method>:<bank-or-account-name>`; expense categories use `expense_category:<casefolded category name>`.
- **New-event integration only**: New purchase creation, invoice issue/void, posted receipts and supplier payments, advance applications, posted expenses/payments, expense voids, dismantling approval review, and sales dispatch are connected to accounting. Historical transactions are never automatically backfilled or posted.
- **Inventory costing**: Inventory cost must be explicitly marked as verified before dispatch for COGS recognition. Dispatch records preserve the unit-cost snapshot; missing or unverified costing remains in the review queue. Estimated recovery value and selling prices are not treated as cost or revenue.
- **Statements and reports**: Trial balance, profit and loss, balance sheet, journal register, general ledger, cash/bank book, customer/supplier ledgers, source-event coverage, opening-balance preview, and subledger reconciliation are available with PDF and Excel exports where applicable. Reports are labeled provisional when source events remain unposted.
- **Opening balances and migration safety**: Opening balances are preview-only and are not posted automatically. The application uses additive SQLite schema initialization; before a production schema migration, create and verify a fresh backup and confirm integrity and historical-row preservation. Accounting tests use isolated databases.

### 6. Settings & Document Management
Accessible from the **Settings** navigation item:
- **Central factory profile**: Store factory identity, address, contact, GSTIN and other optional registration fields. Invoice and payment configuration screens share these setting keys; issued invoice snapshots are not modified.
- **Regional and application preferences**: Store currency/date/timezone, tax-treatment, fiscal-year, payment-term, document-size, storage and backup preferences. No registration or tax value is assumed when left unset.
- **Payment accounts**: Maintain reusable cash/bank/settlement account labels and optional accounting mappings. Only a masked account reference or last four digits may be stored; payment entry screens can select configured accounts or enter an unconfigured reference.
- **Managed documents**: Upload supported PDF, image, text/CSV and Office documents; files receive generated internal names, SHA-256 checksums, size/type checks and generic links to existing business records. Archiving requires a reason and retains the file; files are not physically deleted.
- **Backups and restore**: Create non-overwriting SQLite online backups, verify integrity/schema/foreign keys and checksum, and keep backup metadata. Restore requires explicit UI confirmation, validates the source and creates a fresh safety backup before replacement. Restart the application after restore.
- **Schema safety**: New metadata tables are additive. Startup records the verified pre-migration backup after the schema upgrade. Tests use temporary databases and do not exercise restore against production.

### 7. Employee Management
Accessible from the **Employees** navigation item:
- **Employee directory**: Maintain sequential employee IDs, contact and employment details, active/on-leave/inactive/separated statuses, and filters for department, designation, employment type, status, and joining date.
- **Department and designation catalogues**: Manage reusable entries; deactivation preserves employee and history records.
- **Employment history**: Status and organizational changes are retained as history records. Operational modules' existing responsible-user text is not automatically reassigned to employee records.
- **Managed documents**: Employee documents and profile photos use the existing managed-document storage, checksum, validation, and archive services.
- **Reports**: Export employee directory, status, department/designation, joining, and separation reports to PDF or Excel. Contact and emergency-contact fields are excluded unless the user explicitly confirms a confidential export.
- **Privacy controls**: Ordinary employee reads filter private contact and emergency details unless the signed-in user has `employees.private.view`. Confidential PDF/Excel exports require both `employees.private.view` and `employees.export_private`. Employee documents also require permission to access the linked employee record.
- **Database safety**: Employee tables are additive and reference departments/designations with restrictive foreign keys. Automated employee tests use isolated SQLite databases.

## Reporting Centre

The sidebar Reporting Centre is a directory, not a separate reporting engine.
Operational reports are exported from the relevant module so report parameters
remain tied to their source data. Financial statements and reconciliations are
available in **Accounting**.

## Data Safety, Backup, and Restore

- The active development database is `data/factory.db`. Do not delete, reset,
  or use it as a test target.
- Managed documents default to `data/documents` unless changed in Settings.
- Backups default to `reports/backups` unless changed in Settings. Each backup
  uses SQLite's online backup API and is checked for integrity, required schema,
  foreign keys, and SHA-256 metadata.
- Restore requires an explicit confirmation and creates a verified safety backup
  before replacing the selected database. Restart the app after a restore.

## Authentication, authorization, and privacy

- **Sign-in and credentials**: First launch requires creating the first
  administrator through the setup dialog. There are no default credentials or
  password-recovery backdoors. Passwords are stored as Argon2id hashes using
  `argon2-cffi`, with a minimum 12-character password policy.
- **Trusted sessions**: Successful local sign-in issues an immutable
  `ActorContext`. Authorization validates its in-memory trusted-session entry,
  active account, expiry, and database identity against the same business
  database session used by the protected operation. Public database-facing
  service APIs require the actor explicitly; missing or invalid context fails
  closed. Authentication is local to the application and does not use an
  external identity provider or network authentication.
- **Permission model**: The application seeds a permission catalogue and
  explicit least-privilege assignments for its built-in roles. Services enforce
  permissions independently of UI visibility; hiding or disabling an action is
  not the authorization boundary.
- **Built-in roles**:

  | Role | Intended responsibilities |
  | --- | --- |
  | **System Administrator** | Explicit full access to the permission catalogue, including user administration, backups, and private employee data. |
  | **Factory Manager** | Broad day-to-day operations across procurement, dismantling, inventory, sales, expenses, settings, and standard employee records. It does not receive user administration, backup, or private employee-data permissions. |
  | **Procurement** | Supplier and purchase operations, plus related document access. |
  | **Inventory Operator** | Inventory viewing and adjustment/transfer, and dismantling viewing; no inventory-catalogue management or dismantling approval. |
  | **Sales Operator** | Customer and sales-order operations, dispatch and invoice issue, receipt viewing, and related document access; no receipt posting or voiding. |
  | **Accounts & Finance** | Accounting configuration, periods, journals and exceptions; receipt, supplier-payment and expense workflows; and selected customer, purchase and sales access, including invoice viewing and voiding. |
  | **HR/Employee Manager** | Employee administration, private employee-data viewing and confidential exports, and employee document access. |
  | **Read-only Auditor** | Read-only access to selected supplier, customer, receipt, purchase, dismantling, inventory, sales, invoice, expense, supplier-payment and accounting information. |
- **Employee privacy**: Standard employee reads redact sensitive contact,
  emergency-contact and notes fields unless the actor has
  `employees.private.view`. Private PDF/Excel exports require both
  `employees.private.view` and `employees.export_private`. The HR/Employee
  Manager and System Administrator receive these grants; Factory Manager does
  not.
- **Documents and backups**: `documents.view` / `documents.manage` do not
  bypass authorization on the linked business record. Employee documents have
  additional employee permission checks. Backup listing and verification,
  creation, and restore require `backups.view`, `backups.create`, and
  `backups.restore`, respectively; restore also requires explicit confirmation
  and creates a safety copy before replacing the selected database.
- **Logout and expiry**: Logout invalidates the trusted in-memory session and
  clears protected page data. Sessions lock after the configured inactivity
  timeout (15 minutes by default). Re-authentication creates fresh page
  instances for the newly authenticated actor rather than reusing the previous
  account's cached data. The Security page provides audited user and role
  administration to authorized users.
- **Operational limitations**: Authentication and RBAC protect application
  service operations, but the application is a local desktop system; they do
  not provide remote identity management or replace operating-system file
  permissions. Local databases, documents and backups are not encrypted by the
  application. Use appropriate Windows account permissions and encrypted
  storage where required. No compliance certification is claimed.
- **Financial and tax setup**: The production chart of accounts, mappings and
  opening balances must be configured and approved by an administrator.
  Historical transactions are intentionally not posted automatically.
  Configurable GST calculations are application calculations, not a statement
  of legal or tax compliance.
- Inventory catalogue creation, editing, and deactivation require the separate
  `inventory.manage` permission (System Administrator and Factory Manager).
  Inventory Operators retain inventory viewing and stock adjustment/transfer
  access through `inventory.view` and `inventory.adjust`; they do not manage
  catalogue records. Sales dispatches use the narrower `inventory.dispatch`
  permission and do not grant Sales Operators general `inventory.adjust`
  access.
- Customer database reads and directory exports require `customers.view`
  (System Administrator, Factory Manager, Sales Operator, Accounts & Finance,
  and Read-only Auditor). Customer creation, edits, activation changes, and
  customer-code generation require `customers.manage` (System Administrator,
  Factory Manager, and Sales Operator). The Accounts & Finance role is
  intentionally view-only for customer master records.
- Customer receipts use separate `receipts.view`, `receipts.manage`,
  `receipts.post`, and `receipts.void` permissions. Sales Operators can view
  customer receipts but cannot create, post, or void them. Accounts & Finance
  can manage drafts, post, and void receipts; Read-only Auditors can view only.
- Sales orders separate `sales.view`, `sales.manage`, `sales.confirm`, and
  `sales.dispatch`. Reservations and releases are part of the corresponding
  authorized order lifecycle; a dispatch additionally requires
  `inventory.dispatch`. Sales invoices separate `invoices.view`,
  `invoices.manage`, the existing `invoices.issue`, and `invoices.void`.
  Sales Operators can manage drafts and issue invoices but cannot void issued
  invoices. Accounts & Finance can view and void issued invoices but cannot
  manage drafts or issue invoices; Read-only Auditors can view only. Sales
  order exports use `sales.view`, and invoice exports use `invoices.view`;
  these report permissions authorize access to the same data and do not grant
  lifecycle or stock-changing operations.
- Voiding a Sales Invoice independently requires invoice-void authorization,
  invoice view, Sales order view, and receipt view for the posted-allocation
  check. Receipt viewing alone never authorizes an invoice void. Automatic
  invoice and dispatch accounting source events remain part of those
  transactions and do not grant manual `accounting.post` access.
- Supplier payments use `supplier_payments.view`, `.manage`, `.post`, and
  `.void`; expense payments use `expenses.view`, `.manage`, `.post`, and
  `.void`. System Administrators receive all catalogue permissions; Factory
  Managers receive explicit operational grants, not all catalogue permissions.
  Accounts & Finance has the payment permissions, while
  Read-only Auditors have view-only access. Other roles receive no payment
  permission unless explicitly assigned. Combined payment-register exports
  require view access to both customer receipts and supplier payments.
- Sessions are memory-only, support logout, and lock after the configured
  inactivity timeout (15 minutes by default). The Security page provides the
  audited user/role directory to authorized administrators.
- Account recovery is an operational process: an authorized Windows
  administrator must restore a verified backup or have a separate authorized
  administrator reset the account. The application does not store a recovery
  password or bypass token.

## Windows Deployment Preparation

Do not install a writable database, documents, backups, logs, or settings under
`Program Files`, and do not bundle a live `factory.db` in an installer. Package
application binaries separately and configure a writable per-user data location
(for example `%LOCALAPPDATA%\\ElectronicRecyclingFactory`) before release. An
installer has not been built or validated by this repository.
