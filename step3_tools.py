"""
STEP 3 - Turn steps 1 and 2 into tools the LLM can call.

A tool is three things:
  1. a JSON schema    - so the model knows the name and arguments
  2. a description    - so the model knows WHEN to use it
  3. a python function - what actually runs

Design choice that matters: we do NOT dump the whole schema card into the
prompt. The model gets a cheap table list, then pulls detail for the two or
three tables it actually needs. That is the only version that survives a
63-table database.

Run:  .venv/bin/python step3_tools.py
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine

from step1_extract import extract
from step2_run_sql import run_sql, to_markdown

CACHE = Path(__file__).parent / "cache"


# ============================================================ the session object
class DBSession:
    """Everything the tools need: a connection, the facts card, and the notes."""

    def __init__(self, url: str, refresh: bool = False):
        self.engine = create_engine(url, pool_pre_ping=True)
        self.card = self._load_card(url, refresh)
        self.db = self.card["database"]
        self.notes_path = CACHE / f"{self.db}.notes.json"
        self.notes = json.loads(self.notes_path.read_text()) if self.notes_path.exists() else []

    def _load_card(self, url, refresh):
        """Read the cached card from step 1, or build it if this DB is new."""
        CACHE.mkdir(exist_ok=True)
        # peek at the db name without a query: it is the last path segment of the URL
        name = url.rsplit("/", 1)[-1].split("?")[0]
        path = CACHE / f"{name}.json"
        if path.exists() and not refresh:
            print(f"[cache hit]  {path}")
            return json.loads(path.read_text())
        print(f"[extracting] {name} ... (first load, this queries information_schema)")
        card = extract(url)
        (CACHE / f"{card['database']}.json").write_text(json.dumps(card, indent=2, default=str))
        return card

    # ----------------------------------------------------------- for the prompt
    def overview(self) -> str:
        """Small enough to sit in the system prompt even for a big database."""
        lines = [f"Database `{self.db}` ({self.card['dialect']}). Tables:"]
        for name, t in self.card["tables"].items():
            note = f" - {t['comment']}" if t["comment"] else ""
            lines.append(f"  {name} (~{t['approx_rows']:,} rows){note}")
        for name in self.card["views"]:
            lines.append(f"  {name} (VIEW)")
        if self.notes:
            lines.append("\nNotes learned earlier:")
            lines += [f"  - {n['note']}" for n in self.notes]
        return "\n".join(lines)


# ==================================================================== the tools
def tool_list_tables(s: DBSession) -> str:
    """Cheap overview. Same text as the prompt slot."""
    return s.overview()


def tool_describe_table(s: DBSession, table: str) -> str:
    """Full detail for ONE table - the expensive context, fetched on demand."""
    t = s.card["tables"].get(table) or s.card["views"].get(table)
    if t is None:
        available = ", ".join(list(s.card["tables"]) + list(s.card["views"]))
        return f"No table named '{table}'. Available: {available}"

    out = [f"TABLE {table}  (~{t.get('approx_rows', 0):,} rows)"]
    if t.get("comment"):
        out.append(f"purpose: {t['comment']}")

    for col, info in t["columns"].items():
        flags = []
        if col in t.get("primary_key", []):
            flags.append("PRIMARY KEY")
        if not info["nullable"]:
            flags.append("NOT NULL")
        out.append(f"  {col}  {info['type']}  {' '.join(flags)}")
        if info["comment"]:
            out.append(f"      meaning: {info['comment']}")

        p = info.get("profile") or {}
        if "actual_values" in p:
            vals = ", ".join(f"'{a['value']}' ({a['count']})" for a in p["actual_values"][:25])
            out.append(f"      values present: {vals}")
            declared = info.get("declared_values")
            if declared:
                unused = [d for d in declared if d not in {a["value"] for a in p["actual_values"]}]
                if unused:
                    out.append(f"      declared but NEVER used: {unused}")
        elif "distinct_count" in p:
            out.append(f"      {p['distinct_count']} distinct values (too many to list)")
        if "min" in p:
            out.append(f"      range: {p['min']} .. {p['max']}")
        if "null_count" in p:
            out.append(f"      NULLs: {p['null_count']}")

    for fk in t.get("foreign_keys", []):
        out.append(f"  JOIN: {table}.{fk['column']} = {fk['references_table']}.{fk['references_column']}")
    if t.get("sample_rows"):
        out.append(f"  example row: {t['sample_rows'][0]}")
    return "\n".join(out)


def tool_run_sql(s: DBSession, sql: str) -> str:
    """The only tool that touches data. All four gates from step 2 apply."""
    result = run_sql(s.engine, sql)
    return to_markdown(result)


def tool_save_note(s: DBSession, note: str) -> str:
    """Agent memory: a fact worth keeping for the next question about this DB."""
    s.notes.append({"note": note})
    s.notes_path.write_text(json.dumps(s.notes, indent=2))
    return f"Saved. {len(s.notes)} note(s) stored for `{s.db}`."


def tool_read_notes(s: DBSession) -> str:
    if not s.notes:
        return "No notes saved yet for this database."
    return "\n".join(f"{i+1}. {n['note']}" for i, n in enumerate(s.notes))


# ====================================================== schemas the LLM receives
TOOLS = [
    {
        "name": "list_tables",
        "description": (
            "List every table in the database with its approximate row count and purpose. "
            "Call this first when you do not yet know what the database contains."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": [],
                         "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "describe_table",
        "description": (
            "Get full detail for one table: every column with its exact type, the values "
            "actually present in low-cardinality columns, min/max for dates and numbers, "
            "foreign keys for JOINs, and an example row. "
            "ALWAYS call this for every table you intend to query, before writing SQL. "
            "The 'values present' list is authoritative - use those exact literals in WHERE clauses."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "Exact table name"}},
            "required": ["table"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "run_sql",
        "description": (
            "Run one read-only MySQL SELECT and get the rows back. "
            "Rejected: anything that is not a single SELECT, multiple statements, writes "
            "hidden in a CTE. A LIMIT is added automatically and queries time out after 15s. "
            "If it returns an error, read the 'Hint' line and fix the query."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "One MySQL SELECT statement"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "save_note",
        "description": (
            "Save a durable fact about this database for future questions - a business "
            "definition you worked out, a join that is not obvious, a table to avoid. "
            "Do not save the answer to a one-off question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"note": {"type": "string", "description": "One sentence"}},
            "required": ["note"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "read_notes",
        "description": "Read back the facts saved earlier about this database.",
        "input_schema": {"type": "object", "properties": {}, "required": [],
                         "additionalProperties": False},
        "strict": True,
    },
]

HANDLERS = {
    "list_tables": tool_list_tables,
    "describe_table": tool_describe_table,
    "run_sql": tool_run_sql,
    "save_note": tool_save_note,
    "read_notes": tool_read_notes,
}


def dispatch(session: DBSession, name: str, args: dict) -> str:
    """One entry point. Step 4's loop calls only this."""
    handler = HANDLERS.get(name)
    if handler is None:
        return f"No such tool: {name}. Available: {', '.join(HANDLERS)}"
    try:
        return handler(session, **args)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"


if __name__ == "__main__":
    load_dotenv()
    s = DBSession(os.environ["DB_URL"])

    print("\n" + "=" * 78)
    print("WHAT THE MODEL SEES IN THE SYSTEM PROMPT (cheap - fits any database)")
    print("=" * 78)
    print(s.overview())

    print("\n" + "=" * 78)
    print("TOOL SCHEMAS SENT TO THE MODEL")
    print("=" * 78)
    for t in TOOLS:
        args = ", ".join(t["input_schema"]["properties"]) or "-"
        print(f"  {t['name']:16} args: {args}")

    print("\n" + "=" * 78)
    print("SIMULATING THE CALLS AN LLM WOULD MAKE FOR:")
    print("  'Which payment method brings in the most revenue from enterprise customers?'")
    print("=" * 78)

    script = [
        ("describe_table", {"table": "payments"}),
        ("describe_table", {"table": "customers"}),
        ("run_sql", {"sql": """
            SELECT p.method, ROUND(SUM(p.amount), 2) AS revenue, COUNT(*) AS payments
            FROM payments p
            JOIN orders o    ON o.id = p.order_id
            JOIN customers c ON c.id = o.customer_id
            WHERE p.status = 'success' AND c.segment = 'enterprise'
            GROUP BY p.method
            ORDER BY revenue DESC"""}),
        ("save_note", {"note": "Revenue = SUM(payments.amount) WHERE payments.status='success'. "
                               "Never count payments rows blindly - failed attempts get a retry row."}),
        ("describe_table", {"table": "custmers"}),      # typo -> graceful
        ("run_sql", {"sql": "DELETE FROM orders"}),     # blocked by step 2
    ]

    for name, args in script:
        shown = {k: " ".join(str(v).split())[:70] for k, v in args.items()}
        print(f"\n>>> {name}({shown})")
        print(dispatch(s, name, args))
