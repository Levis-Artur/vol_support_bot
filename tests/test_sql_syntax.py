"""Grammar checks only; real PostgreSQL tests also validate names, types and locking."""
import ast

from pglast import parse_plpgsql, parse_sql

from config import BASE_DIR


def test_schema_sql_and_migration_body():
    sql = (BASE_DIR / "schema.sql").read_text(encoding="utf-8")
    assert parse_sql(sql)
    assert parse_plpgsql(sql[sql.index("DO $$"):])
    migration = (BASE_DIR / "migrations" / "001_open_access.sql").read_text(encoding="utf-8")
    assert parse_sql(migration)
    assert parse_plpgsql(migration[migration.index("CREATE OR REPLACE FUNCTION"):])


def test_all_literal_queries_parse():
    count = 0
    for filename in ("bot.py", "healthcheck.py"):
        module = ast.parse((BASE_DIR / filename).read_text(encoding="utf-8"))
        for node in ast.walk(module):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"execute", "fetch", "fetchrow", "fetchval"}
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                assert parse_sql(node.args[0].value), f"{filename}:{node.lineno}"
                count += 1
    assert count > 25
