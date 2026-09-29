from sqlalchemy import inspect, text

from app.db import engine, init_db


def test_missing_column_is_added_to_existing_table():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE items DROP COLUMN catchup"))
    assert "catchup" not in {c["name"] for c in inspect(engine).get_columns("items")}
    init_db()
    assert "catchup" in {c["name"] for c in inspect(engine).get_columns("items")}
