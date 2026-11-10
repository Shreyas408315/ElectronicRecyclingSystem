import sqlite3

from sqlalchemy import create_engine, inspect

import database.connection as database_connection


def test_invoice_migration_is_additive_backed_up_and_preserves_existing_rows(
    tmp_path, monkeypatch
):
    root = tmp_path / "migration-root"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    database_path = data_dir / "factory.db"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE sales_invoice_items "
            "(id INTEGER PRIMARY KEY, invoice_id INTEGER, fulfilment_item_id INTEGER)"
        )
        connection.exec_driver_sql(
            "INSERT INTO sales_invoice_items (id, invoice_id, fulfilment_item_id) "
            "VALUES (41, 12, 9)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE sales_order_fulfilment_items "
            "(id INTEGER PRIMARY KEY, fulfilment_id INTEGER, sales_order_item_id INTEGER, "
            "inventory_item_id INTEGER, quantity NUMERIC(12, 2))"
        )
        connection.exec_driver_sql(
            "INSERT INTO sales_order_fulfilment_items "
            "(id, fulfilment_id, sales_order_item_id, inventory_item_id, quantity) "
            "VALUES (9, 3, 5, 7, 2)"
        )

    monkeypatch.setattr(database_connection, "ROOT", root)
    monkeypatch.setattr(database_connection, "DB_PATH", database_path)
    monkeypatch.setattr(database_connection, "engine", engine)
    database_connection.initialize_database()

    inspector = inspect(engine)
    invoice_columns = {column["name"] for column in inspector.get_columns("sales_invoice_items")}
    fulfilment_columns = {
        column["name"] for column in inspector.get_columns("sales_order_fulfilment_items")
    }
    assert "order_discount_amount" in invoice_columns
    assert "invoiced_quantity" in fulfilment_columns
    assert any(
        index.get("unique")
        and tuple(index.get("column_names") or ()) == ("invoice_id", "fulfilment_item_id")
        for index in inspector.get_indexes("sales_invoice_items")
    )
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT id, invoice_id, fulfilment_item_id FROM sales_invoice_items"
        ).one() == (41, 12, 9)
        assert connection.exec_driver_sql(
            "SELECT id, quantity, invoiced_quantity FROM sales_order_fulfilment_items"
        ).one() == (9, 2, None)

    backups = list((root / "reports" / "backups").glob("*.db"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT id, invoice_id, fulfilment_item_id FROM sales_invoice_items"
        ).fetchone() == (41, 12, 9)
    finally:
        backup.close()
        engine.dispose()
