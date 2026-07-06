"""Guard the PG search_vector expression against drift and regression.

Two schema files must produce the identical generated expression: 001 (the
CREATE TABLE fresh installs get) and 004 (the ALTER that upgrades existing
databases). If someone edits one and not the other, fresh and migrated
databases silently diverge — 004's guard would even skip the rebuild on a
new install whose 001 expression already contains ``translate``.

Also pins the fix itself: angle brackets must be neutralized before
``to_tsvector``, because PG's parser drops ``<Word>`` as an XML *tag* token
(msbuild element names, C# generic params were unsearchable — found live on
the jellyfin index, 2026-07-06). No live PG needed: this parses the SQL text.
"""

from __future__ import annotations

import re
from pathlib import Path

SCHEMA_DIR = Path(__file__).parent.parent / "knowledge" / "schema" / "postgres"

_EXPR_RE = re.compile(
    r"GENERATED ALWAYS AS \((.*?)\) STORED", re.DOTALL | re.IGNORECASE
)


def _search_vector_expr(sql_name: str) -> str:
    """Extract the normalized generated expression from a schema file."""
    sql = (SCHEMA_DIR / sql_name).read_text(encoding="utf-8")
    match = _EXPR_RE.search(sql)
    assert match, f"no GENERATED ALWAYS AS (...) STORED block in {sql_name}"
    return " ".join(match.group(1).split())


def test_init_and_migration_expressions_identical():
    assert _search_vector_expr("001_init.sql") == _search_vector_expr(
        "004_fts_xml_tags.sql"
    )


def test_expression_neutralizes_angle_brackets():
    expr = _search_vector_expr("001_init.sql")
    assert "translate(" in expr
    assert "'<>'" in expr
    # translate must wrap the text fed to to_tsvector, not sit beside it.
    assert expr.index("to_tsvector") < expr.index("translate(")


def test_migration_guard_checks_live_expression():
    # 004 must stay a no-op on already-migrated databases: the DO block
    # keys off the live column expression, not a migrations table.
    sql = (SCHEMA_DIR / "004_fts_xml_tags.sql").read_text(encoding="utf-8")
    assert "pg_get_expr" in sql
    assert "NOT LIKE '%translate%'" in sql
