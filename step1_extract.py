"""
STEP 1 - Extract everything an LLM needs to write SQL against a MySQL database.

The agent itself does no thinking here. This is pure fact-gathering.
Each function below collects exactly one item from the checklist:

  1 dialect              -> which SQL flavour to write
  2 table names          -> so it never invents a table
  3 columns + types      -> so it never invents a column
  4 primary keys         -> how to identify one row
  5 foreign keys         -> HOW TO JOIN (the biggest failure source)
  6 row counts           -> filter vs aggregate, and "is this table huge?"
  7 sample rows          -> what the data actually looks like
  8 allowed values       -> the vocabulary: status is 'pending', not 'Pending'
  9 min / max            -> the real date range and number range
 10 comments             -> documentation the DBA already wrote
 11 views                -> pre-built answers

Run:  .venv/bin/python step1_extract.py
"""
import json
import os
import re

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# how many distinct values still counts as "a short list worth showing the LLM"
MAX_DISTINCT = 25
# don't profile values/ranges on tables bigger than this (keeps extraction fast)
PROFILE_ROW_LIMIT = 5_000_000


# --------------------------------------------------------------- 2, 6, 10, 11
def fetch_tables(conn, db):
    """Table names + the DBA's table comment + an approximate row count."""
    rows = conn.execute(text("""
        SELECT table_name    AS table_name,
               table_type    AS table_type,
               table_comment AS table_comment,
               table_rows    AS table_rows
        FROM information_schema.tables
        WHERE table_schema = :db
        ORDER BY table_type, table_name
    """), {"db": db}).mappings().all()

    tables, views = {}, {}
    for r in rows:
        entry = {
            "comment": (r["table_comment"] or "").strip(),
            "approx_rows": int(r["table_rows"] or 0),
            "columns": {},
            "primary_key": [],
            "foreign_keys": [],
            "indexes": [],
            "sample_rows": [],
        }
        (views if r["table_type"] == "VIEW" else tables)[r["table_name"]] = entry
    return tables, views


# ------------------------------------------------------------------ 3, 4, 10
def fetch_columns(conn, db, tables, views):
    """Column name, full type, nullability, default, comment, and PK membership."""
    rows = conn.execute(text("""
        SELECT table_name       AS table_name,
               column_name      AS column_name,
               ordinal_position AS ordinal_position,
               column_type      AS column_type,
               data_type        AS data_type,
               is_nullable      AS is_nullable,
               column_default   AS column_default,
               column_comment   AS column_comment,
               column_key       AS column_key,
               extra            AS extra
        FROM information_schema.columns
        WHERE table_schema = :db
        ORDER BY table_name, ordinal_position
    """), {"db": db}).mappings().all()

    for r in rows:
        target = tables.get(r["table_name"]) or views.get(r["table_name"])
        if target is None:
            continue
        target["columns"][r["column_name"]] = {
            "type": r["column_type"],              # e.g. enum('web','mobile') or decimal(10,2)
            "base_type": r["data_type"],           # e.g. enum / decimal / int
            "nullable": r["is_nullable"] == "YES",
            "default": r["column_default"],
            "comment": (r["column_comment"] or "").strip(),
            "extra": r["extra"] or "",             # auto_increment, etc.
            "declared_values": parse_enum(r["column_type"]),   # item 8, for ENUMs
        }
        if r["column_key"] == "PRI":
            target["primary_key"].append(r["column_name"])


def parse_enum(column_type: str):
    """enum('web','mobile','store') -> ['web','mobile','store']. Free, no query needed."""
    if not column_type.lower().startswith("enum("):
        return None
    return re.findall(r"'((?:[^']|'')*)'", column_type[5:-1])


# ---------------------------------------------------------------------- 5
def fetch_foreign_keys(conn, db, tables):
    """child.column -> parent.column. This is what lets the LLM write a JOIN."""
    rows = conn.execute(text("""
        SELECT k.table_name             AS table_name,
               k.constraint_name        AS constraint_name,
               k.column_name            AS column_name,
               k.referenced_table_name  AS referenced_table_name,
               k.referenced_column_name AS referenced_column_name
        FROM information_schema.key_column_usage k
        WHERE k.table_schema = :db AND k.referenced_table_name IS NOT NULL
        ORDER BY k.table_name, k.constraint_name, k.ordinal_position
    """), {"db": db}).mappings().all()

    for r in rows:
        t = tables.get(r["table_name"])
        if t is None:
            continue
        t["foreign_keys"].append({
            "column": r["column_name"],
            "references_table": r["referenced_table_name"],
            "references_column": r["referenced_column_name"],
        })


def fetch_indexes(conn, db, tables):
    """Which columns are indexed - a hint about what is cheap to filter on."""
    rows = conn.execute(text("""
        SELECT table_name   AS table_name,
               index_name   AS index_name,
               column_name  AS column_name,
               non_unique   AS non_unique,
               seq_in_index AS seq_in_index
        FROM information_schema.statistics
        WHERE table_schema = :db
        ORDER BY table_name, index_name, seq_in_index
    """), {"db": db}).mappings().all()

    for r in rows:
        t = tables.get(r["table_name"])
        if t is None:
            continue
        existing = next((i for i in t["indexes"] if i["name"] == r["index_name"]), None)
        if existing is None:
            t["indexes"].append({"name": r["index_name"],
                                 "columns": [r["column_name"]],
                                 "unique": not r["non_unique"]})
        else:
            existing["columns"].append(r["column_name"])


# ---------------------------------------------------------------------- 7
def fetch_sample_rows(conn, table, columns, limit=2):
    """A couple of real rows. Nothing explains a column better than its contents."""
    cols = ", ".join(f"`{c}`" for c in columns)
    rows = conn.execute(text(f"SELECT {cols} FROM `{table}` LIMIT {limit}")).mappings().all()
    return [{k: jsonable(v) for k, v in row.items()} for row in rows]


# -------------------------------------------------------------------- 8, 9
VALUE_TYPES = {"enum", "varchar", "char", "tinyint", "set"}
RANGE_TYPES = {"date", "datetime", "timestamp", "int", "bigint", "smallint",
               "mediumint", "decimal", "float", "double"}


def profile_column(conn, table, column, info):
    """
    Item 8: if the column holds few distinct values, list them with counts.
             That gives the LLM the exact literals to put in a WHERE clause.
    Item 9: if it is a date or a number, get MIN and MAX so the LLM knows the
             real range instead of guessing.
    """
    base = info["base_type"]
    out = {}

    if base in VALUE_TYPES:
        n = conn.execute(text(f"SELECT COUNT(DISTINCT `{column}`) FROM `{table}`")).scalar_one()
        if n is not None and n <= MAX_DISTINCT:
            rows = conn.execute(text(f"""
                SELECT `{column}` AS v, COUNT(*) AS c
                FROM `{table}` GROUP BY `{column}` ORDER BY c DESC
            """)).all()
            out["actual_values"] = [{"value": jsonable(v), "count": int(c)} for v, c in rows]
        else:
            out["distinct_count"] = int(n) if n is not None else None

    if base in RANGE_TYPES:
        lo, hi = conn.execute(
            text(f"SELECT MIN(`{column}`), MAX(`{column}`) FROM `{table}`")
        ).one()
        if lo is not None or hi is not None:
            out["min"] = jsonable(lo)
            out["max"] = jsonable(hi)

    nulls = conn.execute(
        text(f"SELECT COUNT(*) FROM `{table}` WHERE `{column}` IS NULL")
    ).scalar_one()
    if nulls:
        out["null_count"] = int(nulls)

    return out


def jsonable(v):
    import datetime as dt
    import decimal
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    return str(v)


# ---------------------------------------------------------------- put it together
def extract(url):
    engine = create_engine(url)
    with engine.connect() as conn:
        db = conn.execute(text("SELECT DATABASE()")).scalar_one()
        dialect = engine.dialect.name                                   # item 1

        tables, views = fetch_tables(conn, db)                          # 2, 6, 10, 11
        fetch_columns(conn, db, tables, views)                          # 3, 4, 10
        fetch_foreign_keys(conn, db, tables)                            # 5
        fetch_indexes(conn, db, tables)

        for name, t in tables.items():
            t["sample_rows"] = fetch_sample_rows(conn, name, t["columns"])   # 7
            if t["approx_rows"] <= PROFILE_ROW_LIMIT:
                for col, info in t["columns"].items():
                    info["profile"] = profile_column(conn, name, col, info)  # 8, 9

        for name, v in views.items():
            v["sample_rows"] = fetch_sample_rows(conn, name, v["columns"])

    return {"dialect": dialect, "database": db, "tables": tables, "views": views}


# ------------------------------------------------------------------- printing
def render(card):
    L = [f"DIALECT: {card['dialect']}   DATABASE: {card['database']}",
         f"TABLES: {len(card['tables'])}   VIEWS: {len(card['views'])}", ""]

    for name, t in card["tables"].items():
        L.append("=" * 78)
        head = f"TABLE {name}   (~{t['approx_rows']:,} rows)"
        L.append(head + (f"   -- {t['comment']}" if t["comment"] else ""))
        for col, info in t["columns"].items():
            flags = []
            if col in t["primary_key"]:
                flags.append("PK")
            if not info["nullable"]:
                flags.append("NOT NULL")
            if info["extra"]:
                flags.append(info["extra"])
            L.append(f"   {col:<14} {info['type']:<42} {' '.join(flags)}")
            if info["comment"]:
                L.append(f"       note    : {info['comment']}")

            p = info.get("profile") or {}
            if info["declared_values"]:
                L.append(f"       declared: {info['declared_values']}")
            if "actual_values" in p:
                shown = ", ".join(f"{a['value']}({a['count']})" for a in p["actual_values"][:8])
                L.append(f"       actual  : {shown}")
            elif "distinct_count" in p:
                L.append(f"       distinct: {p['distinct_count']} values (too many to list)")
            if "min" in p:
                L.append(f"       range   : {p['min']}  ..  {p['max']}")
            if "null_count" in p:
                L.append(f"       nulls   : {p['null_count']}")

        for fk in t["foreign_keys"]:
            L.append(f"   JOIN  {name}.{fk['column']} -> {fk['references_table']}.{fk['references_column']}")
        if t["sample_rows"]:
            L.append(f"   SAMPLE {t['sample_rows'][0]}")
        L.append("")

    for name, v in card["views"].items():
        L.append("=" * 78)
        L.append(f"VIEW {name}   cols: {', '.join(v['columns'])}")
        if v["sample_rows"]:
            L.append(f"   SAMPLE {v['sample_rows'][0]}")
    return "\n".join(L)


if __name__ == "__main__":
    load_dotenv()
    card = extract(os.environ["DB_URL"])
    print(render(card))
 
    os.makedirs("cache", exist_ok=True)
    path = f"cache/{card['database']}.json"
    with open(path, "w") as f:
        json.dump(card, f, indent=2, default=str)
    print(f"\n\nfull card written to {path}")
