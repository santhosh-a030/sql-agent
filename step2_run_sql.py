"""
STEP 2 - The one tool that actually touches data.

The LLM hands us a string it believes is SQL. Before that string reaches MySQL
it passes four gates, and whatever comes back is shaped so the LLM can react:

  GATE 1  parse it      - only a plain SELECT survives (sqlglot, not regex)
  GATE 2  force a LIMIT - the LLM writes "SELECT * FROM orders"; we will not
                          put 10,000 rows into the prompt
  GATE 3  timeout       - MySQL kills the query server-side after N seconds
  GATE 4  rollback      - we open a transaction and never commit it

Errors are returned as a dict, never raised. The 'hint' field is the sentence
that lets the agent fix its own SQL on the next turn.

Run:  .venv/bin/python step2_run_sql.py
"""
import datetime as dt
import decimal
import os
import time

import sqlglot
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp

MAX_ROWS = 100          # never return more rows than this to the LLM
TIMEOUT_SECONDS = 15    # never let a query run longer than this

# Only these statement types may run at all.
READ_ONLY_STATEMENTS = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)

# Statement types that write. Blocked even when buried inside a CTE.
WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
               exp.Alter, exp.TruncateTable, exp.Merge, exp.Command)

# MySQL functions that read the filesystem or burn CPU on purpose.
BANNED_FUNCTIONS = {"load_file", "sleep", "benchmark", "get_lock", "master_pos_wait"}


class UnsafeSQL(Exception):
    """The SQL is not a plain read. The message is written for the LLM to read."""


# ------------------------------------------------------------ GATE 1 and GATE 2
def check_and_limit(sql: str, max_rows: int = MAX_ROWS) -> str:
    """Validate the SQL and return it rewritten with a LIMIT. Raises UnsafeSQL."""
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="mysql") if s is not None]
    except Exception as e:
        raise UnsafeSQL(f"Could not parse the SQL: {e}") from e

    if not statements:
        raise UnsafeSQL("Empty SQL.")
    if len(statements) > 1:
        raise UnsafeSQL("Multiple statements are not allowed. Send exactly one SELECT.")

    stmt = statements[0]

    # GATE 1a - the top-level statement must be a read
    if not isinstance(stmt, READ_ONLY_STATEMENTS):
        kind = type(stmt).__name__.upper()
        raise UnsafeSQL(f"Only read queries are allowed; got a {kind} statement. Rewrite it as a SELECT.")

    # GATE 1b - walk the whole tree. A write can hide inside a CTE:
    #   WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone
    for node in stmt.walk():
        if isinstance(node, WRITE_NODES):
            raise UnsafeSQL(
                f"The query contains a write operation ({type(node).__name__.upper()}). Read-only queries only."
            )
        if isinstance(node, exp.Anonymous) and str(node.this).lower() in BANNED_FUNCTIONS:
            raise UnsafeSQL(f"Function {node.this} is not allowed.")
        if isinstance(node, exp.Func) and node.sql_name().lower() in BANNED_FUNCTIONS:
            raise UnsafeSQL(f"Function {node.sql_name()} is not allowed.")

    # GATE 2
    return _force_limit(stmt, max_rows).sql(dialect="mysql")


def _force_limit(stmt, max_rows: int):
    """Add LIMIT max_rows, or tighten an existing LIMIT that asks for more."""
    existing = stmt.args.get("limit")
    if existing is not None:
        try:
            wanted = int(existing.expression.this)
        except (AttributeError, TypeError, ValueError):
            return stmt                      # LIMIT ? or LIMIT expr - leave it alone
        if wanted <= max_rows:
            return stmt                      # already modest enough
    try:
        return stmt.limit(max_rows)
    except Exception:                        # UNION on some dialects
        return exp.select("*").from_(stmt.subquery(alias="q")).limit(max_rows)


# ------------------------------------------------------------------ GATE 3
def _set_timeout(conn, seconds: int) -> None:
    """MySQL kills SELECTs that exceed MAX_EXECUTION_TIME (milliseconds)."""
    conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME = {int(seconds * 1000)}"))


# --------------------------------------------------------- error -> hint for LLM
def classify(message: str) -> tuple[str, str]:
    m = message.lower()
    if "doesn't exist" in m or "unknown table" in m:
        return "unknown_table", "That table does not exist. Use list_tables and copy the exact name."
    if "unknown column" in m:
        return "unknown_column", "That column does not exist. Call describe_table for the exact column names."
    if "error in your sql syntax" in m:
        return "syntax_error", "Fix the MySQL syntax and resend exactly one SELECT."
    if "is ambiguous" in m:
        return "ambiguous_column", "Two joined tables share that column name. Qualify it with a table alias."
    if "maximum statement execution time exceeded" in m or "query execution was interrupted" in m:
        return "timeout", "The query was too slow. Add a WHERE filter, aggregate, or narrow the date range."
    if "only_full_group_by" in m or "not in group by" in m:
        return "aggregation_error", "Every selected column must be aggregated or listed in GROUP BY."
    if "command denied" in m or "access denied" in m:
        return "permission_denied", "The database user may only run SELECT. Do not attempt writes."
    if "truncated incorrect" in m or "incorrect date" in m:
        return "type_mismatch", "You compared a column to a value of the wrong type. Check the column type."
    return "database_error", "Re-read the schema and simplify the query."


def _jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    return str(v)


# ------------------------------------------------------------------ the tool
def run_sql(engine, sql: str, *, max_rows: int = MAX_ROWS, timeout: int = TIMEOUT_SECONDS) -> dict:
    """Execute one read-only query. Always returns a dict - never raises."""
    try:
        safe = check_and_limit(sql, max_rows)
    except UnsafeSQL as e:
        return {"ok": False, "error_type": "rejected", "message": str(e),
                "hint": "Only a single read-only SELECT is permitted.", "sql": sql}

    started = time.perf_counter()
    try:
        with engine.connect() as conn:
            trans = conn.begin()                  # GATE 4 - opened, never committed
            try:
                _set_timeout(conn, timeout)       # GATE 3
                result = conn.execute(text(safe))
                columns = list(result.keys())
                fetched = result.fetchmany(max_rows + 1)   # +1 tells us if there was more
                truncated = len(fetched) > max_rows
                rows = [[_jsonable(v) for v in row] for row in fetched[:max_rows]]
            finally:
                trans.rollback()
    except SQLAlchemyError as e:
        message = str(getattr(e, "orig", e)).strip()
        error_type, hint = classify(message)
        return {"ok": False, "error_type": error_type, "message": message, "hint": hint, "sql": safe}

    return {"ok": True, "sql": safe, "columns": columns, "rows": rows,
            "row_count": len(rows), "truncated": truncated,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1)}


def to_markdown(result: dict, max_col_width: int = 40) -> str:
    """Render a result compactly - for your eyes and for the LLM's next turn."""
    if not result["ok"]:
        return f"ERROR [{result['error_type']}]: {result['message']}\nHint: {result['hint']}"
    if not result["rows"]:
        return f"(0 rows)\nSQL: {result['sql']}"

    def cell(v):
        s = "NULL" if v is None else str(v)
        return s if len(s) <= max_col_width else s[: max_col_width - 1] + "…"

    cols = result["columns"]
    grid = [cols] + [[cell(v) for v in r] for r in result["rows"]]
    w = [max(len(row[i]) for row in grid) for i in range(len(cols))]
    out = ["| " + " | ".join(c.ljust(w[i]) for i, c in enumerate(grid[0])) + " |",
           "| " + " | ".join("-" * x for x in w) + " |"]
    out += ["| " + " | ".join(c.ljust(w[i]) for i, c in enumerate(r)) + " |" for r in grid[1:]]
    note = f"{result['row_count']} rows" + (" (truncated)" if result["truncated"] else "")
    return "\n".join(out) + f"\n{note} in {result['duration_ms']}ms"


if __name__ == "__main__":
    load_dotenv()
    engine = create_engine(os.environ["DB_URL"])

    ALLOWED = [
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC",
        """SELECT c.segment, ROUND(SUM(oi.qty * oi.unit_price), 2) AS revenue
           FROM customers c
           JOIN orders o      ON o.customer_id = c.id
           JOIN order_items oi ON oi.order_id = o.id
           WHERE o.status IN ('paid','shipped','delivered')
           GROUP BY c.segment ORDER BY revenue DESC""",
        "SELECT * FROM customers LIMIT 5000",          # over-wide limit gets tightened
        "WITH big AS (SELECT * FROM products WHERE price > 10000) "
        "SELECT status, COUNT(*) FROM big GROUP BY status",
    ]

    BLOCKED = [
        "DROP TABLE orders",
        "DELETE FROM orders WHERE id = 1",
        "UPDATE products SET price = 0",
        "SELECT 1; DROP TABLE orders",
        "WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone",
        "SELECT load_file('/etc/passwd')",
        "SELECT SLEEP(60)",
        "INSERT INTO customers (name, email, signup_date) VALUES ('x','x@x.com','2026-01-01')",
        "TRUNCATE TABLE payments",
    ]

    BAD_SQL = [
        "SELECT * FROM custmers",
        "SELECT nmae FROM customers",
        "SELECT FROM customers",
        "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id WHERE id = 1",
        "SELECT city, name FROM customers GROUP BY city",
    ]

    print("######## 1. ALLOWED ########")
    for sql in ALLOWED:
        r = run_sql(engine, sql, max_rows=5)
        print(f"\n-- you sent : {' '.join(sql.split())[:72]}")
        print(f"-- we ran   : {' '.join(r['sql'].split())[:110]}")
        print(to_markdown(r))

    print("\n\n######## 2. BLOCKED BEFORE REACHING MYSQL ########")
    for sql in BLOCKED:
        r = run_sql(engine, sql)
        tag = "BLOCKED" if not r["ok"] else "!! LEAKED !!"
        print(f"{tag:11} {sql[:48]:50} -> {r.get('message','')[:58]}")

    print("\n\n######## 3. REAL MYSQL ERRORS - what the agent retries on ########")
    for sql in BAD_SQL:
        r = run_sql(engine, sql)
        if r["ok"]:
            print(f"\n{sql}\n  (accepted)")
        else:
            print(f"\n{sql}\n  type: {r['error_type']}\n  msg : {r['message'][:90]}\n  hint: {r['hint']}")

    print("\n\n######## 4. THE DB USER ITSELF REFUSES WRITES ########")
    bypass = "INSERT INTO customers (name, email, signup_date) VALUES ('h','h@h.com','2026-01-01')"
    with engine.connect() as conn:
        try:
            conn.execute(text(bypass))
            print("!! the user could write - check the grants")
        except SQLAlchemyError as e:
            print("agent_ro refused it at the database layer:")
            print("  ", str(getattr(e, "orig", e))[:100])
