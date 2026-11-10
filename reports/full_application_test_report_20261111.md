# Full Application Stabilization Test Report

Date: 2026-11-11

Baseline: `f474731dc4eb8f57e3225ed513288522eb887713`

## Test isolation and evidence

The application test fixture replaces the application SQLAlchemy engine and
session factory with temporary SQLite databases. Tests that create their own
databases, document storage, backup directories, and exports use `tmp_path`.
The successful run placed pytest temporary files on `D:` because the `C:`
volume ran out of free space during the first attempt. No production database
or production document directory was used.

Successful-run evidence is outside the project:

- Pytest log: `D:\ERS-Stabilization-20261111_024500\pytest.log`
- Run metadata: `D:\ERS-Stabilization-20261111_024500\run-metadata.txt`
- Pytest temporary root: `D:\ERS-Stabilization-20261111_024500\pytest-temp`

## Collection

Command:

```powershell
& .\.venv\Scripts\python.exe -m pytest --collect-only -q tests
```

Result: 298 tests collected, 0 collection errors, exit code 0, 1.55 seconds.

## Full-suite result

Command:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -v --durations=20 -p no:cacheprovider
```

Environment: Python 3.14.3, `QT_QPA_PLATFORM=offscreen`,
`PYTEST_DEBUG_TEMPROOT=D:\ERS-Stabilization-20261111_024500\pytest-temp`,
`PYTHONDONTWRITEBYTECODE=1`.

| Collected | Passed | Failed | Skipped | Errors | Process exit | Wall duration |
|---:|---:|---:|---:|---:|---:|---:|
| 298 | 298 | 0 | 0 | 0 | 0 | 00:08:12.709 |

Pytest reported 486.91 seconds (8:06.91) of test execution. The successful run
completed the entire suite.

## Initial resource failure and diagnosis

The initial full-suite attempt used the default pytest temporary location on
`C:`. It stopped before completing all collected tests; the partial log showed
91 passed, 1 failed, and 204 errors, with process exit code 1 and no final
pytest summary. A stop-at-first-error diagnostic identified the failure as
`sqlite3.OperationalError: database or disk is full` while creating the
isolated test database index `ix_employees_phone`. This was a local disk-space
and test-fixture setup failure, not an application behavior failure.

The suite was rerun on `D:` with pytest's cache provider disabled to avoid
writing temporary/cache data to the full `C:` volume. The complete rerun passed
all 298 tests. No tests were skipped or weakened.

## Other checks

- Python compilation:
  `& .\.venv\Scripts\python.exe -m compileall -q app.py database services tests ui utils`
  — exit code 0.
- `git diff --check` — exit code 0 before this report was added; rerun before
  commit.
- Secret-pattern scan found no matches. No tracked `.env`, SQLite database,
  backup, document, PDF, or Excel business export files were found.
- `data\factory.db` and its WAL/SHM sidecars are ignored by `.gitignore`.
