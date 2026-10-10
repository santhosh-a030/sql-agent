# MySQL Agent — ask your database questions in English

A text-to-SQL agent built from scratch, one step at a time. Give it a MySQL
connection and it learns the schema, then answers questions by writing and
running SQL itself.

```
you: which payment method brings in the most revenue from enterprise customers?

  ⟳ describe_table  table=payments
  ⟳ describe_table  table=customers
  ⟳ run_sql         SELECT p.method, SUM(p.amount) ... GROUP BY p.method

COD brings in the most enterprise revenue: ₹29.36M across 572 successful payments.
COD leads on total value despite UPI having more transactions (680 vs 572),
meaning COD enterprise orders have a higher average ticket (~₹51.3k vs ~₹32.7k).
```

---

## The core idea

An LLM cannot "read your database". It can only read **text you give it**.

So the whole job splits in two:

| Half | Who does it | What it is |
|---|---|---|
| **Extraction** | plain Python, no LLM | turn a connection into a text description of the database |
| **Reasoning** | the LLM | read that text, write SQL, read the rows, answer |

Almost every text-to-SQL failure is an extraction failure, not a reasoning
failure. The model writes `WHERE status = 'Pending'` and gets zero rows because
nobody told it the column actually holds `'pending'`. That is why step 1 is the
biggest file in this project.

---

## The loop

```
             ┌─────────────────────────────────────────────┐
             │  you ask a question                         │
             └──────────────────┬──────────────────────────┘
                                ▼
             ┌─────────────────────────────────────────────┐
             │  system prompt = rules + table list + notes │
             └──────────────────┬──────────────────────────┘
                                ▼
                      ┌───────────────────┐
                      │  Claude decides   │◀───────────────┐
                      └─────────┬─────────┘                │
                                │                          │
            wants a tool? ──────┴──── no ──▶ final answer   │
                   │                                        │
                   ▼                                        │
      ┌────────────────────────────┐                        │
      │ describe_table / run_sql / │   result text ─────────┘
      │ save_note / read_notes     │
      └────────────────────────────┘
```

The model never sees the connection URL or the password. It only ever sees text
returned by tools.

---

## How we got here, step by step

### Step 1 — Extraction (`step1_extract.py`)

**Question it answers:** what does an LLM actually need in order to write correct SQL?

Eleven things, and the file has one function per group:

| # | Detail | Why SQL fails without it |
|---|---|---|
| 1 | dialect | `LIMIT` vs `TOP`, different date functions |
| 2 | table names | `custmers` → error |
| 3 | columns + types | guessing `cust_name` vs `name` |
| 4 | primary keys | cannot identify or dedupe a row |
| 5 | **foreign keys** | **cannot JOIN** — the single biggest failure source |
| 6 | row counts | filter-vs-aggregate, "is this table huge?" |
| 7 | sample rows | shows what a column really holds |
| 8 | **values present** | `'pending'`, not `'Pending'` or `3` |
| 9 | min / max | so "last quarter" means something real |
| 10 | comments | documentation the DBA already wrote |
| 11 | views | pre-built answers |

It runs in two phases:

```
PHASE A  the catalogue — 4 queries, no table scans
         information_schema.tables       → names, comments, row estimates
         information_schema.columns      → types, nullability, comments, PKs
         information_schema.key_column_usage → the JOIN map
         information_schema.statistics   → indexes

PHASE B  the data itself — loops over tables and columns
         SELECT … LIMIT 2                → sample rows
         COUNT(DISTINCT col) ≤ 25        → GROUP BY to list the actual values
         MIN(col), MAX(col)              → real ranges
         COUNT(*) WHERE col IS NULL      → how empty is it
```

Phase A gives you structure. **Phase B gives you vocabulary**, and vocabulary is
what makes text-to-SQL work. Example of what only Phase B can tell you:

```
orders.status  enum('pending','paid','shipped','delivered','cancelled','refunded')
    values present: 'delivered'(3332) 'pending'(1667) 'paid'(1667)
                    'shipped'(1667) 'cancelled'(1667)
    declared but NEVER used: ['refunded']
```

That last line is the difference between *"you had 0 refunds last quarter"*
(wrong, and confidently wrong) and *"refunds are not tracked in this schema at
all"* (right).

Output goes to `cache/<database>.json` — extract once, reuse forever.

### Step 2 — Guarded execution (`step2_run_sql.py`)

**Question it answers:** how do I let an LLM run SQL without it being able to
break anything?

Four gates, in order:

```
LLM's SQL string
      │
   ┌──▼──────────────────────────────────────────────────────┐
   │ GATE 1  parse with sqlglot, allow ONLY a read statement │
   │         - no INSERT/UPDATE/DELETE/DROP/TRUNCATE         │
   │         - no stacked statements (SELECT 1; DROP …)      │
   │         - no write hidden in a CTE                      │
   │         - no load_file() / SLEEP() / BENCHMARK()        │
   ├─────────────────────────────────────────────────────────┤
   │ GATE 2  force a LIMIT (rewrites LIMIT 5000 → LIMIT 100) │
   ├─────────────────────────────────────────────────────────┤
   │ GATE 3  SET SESSION MAX_EXECUTION_TIME — 15s kill       │
   ├─────────────────────────────────────────────────────────┤
   │ GATE 4  open a transaction, never commit, always roll   │
   └──▼──────────────────────────────────────────────────────┘
      │
    MySQL
```

Why **parse** instead of keyword-matching: a column named `drop_reason` or a
string `'delete me'` would trip a blocklist. sqlglot looks at the real statement
type.

The other half of this step is error shaping. Errors come back as **data, never
exceptions**:

```python
{"ok": False,
 "error_type": "unknown_column",
 "message": "Unknown column 'nmae' in 'field list'",
 "hint": "Call describe_table for the exact column names."}
```

That `hint` is handed straight back to the model, and it is what makes the agent
fix its own SQL instead of giving up. All five MySQL error families are
classified: unknown table, unknown column, syntax, ambiguous column, aggregation
(`ONLY_FULL_GROUP_BY`), plus timeout and permission denied.

### Step 3 — Tools (`step3_tools.py`)

**Question it answers:** which decisions belong to the model, and which belong to
my code?

The rule: **a tool exists for a decision the LLM has to make.**

| Action | Who triggers it | Why |
|---|---|---|
| extract a URL's schema | the app | deterministic; and the credential must not enter the conversation |
| pick which table to inspect | **LLM tool** | a real choice |
| write and run SQL | **LLM tool** | the core choice |
| write down a learned fact | **LLM tool** | its own judgement |

Five tools:

```
list_tables()          cheap overview: names, row counts, purposes
describe_table(table)  the expensive detail — fetched only when needed
run_sql(sql)           the only tool that touches data
save_note(note)        durable memory for this database
read_notes()           read it back
```

The important design choice is in `DBSession.overview()`: the system prompt gets
only the **table list**, not the whole schema card.

```
Database `shop_demo` (mysql). Tables:
  customers (~2,000 rows) - One row per registered customer account
  orders (~9,921 rows) - Order header. One row per checkout
  tmp_import_2019 (~0 rows) - DEPRECATED one-off import dump. Do not use
  ...
```

Eight lines instead of 120. Detail arrives per-table on demand. This is the only
version that survives a 60-table database — which is why `saas_demo` (15 tables)
exists as a test.

### Step 4 — The agent loop (`agent_core.py`, `step4_agent.py`)

**Question it answers:** how do the pieces become an agent?

```python
messages.append({"role": "user", "content": question})

while True:
    response = client.messages.create(model=..., system=..., tools=TOOLS,
                                      messages=messages)
    messages.append({"role": "assistant", "content": response.content})

    tool_uses = [b for b in response.content if b.type == "tool_use"]
    if not tool_uses:
        return the text answer            # Claude is done

    results = [run(b) for b in tool_uses]
    messages.append({"role": "user", "content": results})   # ALL in one message
```

Details that matter:

| Detail | Why |
|---|---|
| all tool results in **one** user message | splitting them teaches the model to stop making parallel calls |
| `is_error: True` on failures | marks it as something to recover from, not as data |
| `MAX_TOOL_TURNS = 12` | a question needing more than that is in a retry spiral |
| `messages` persists between questions | follow-ups work: *"now break that down by city"* |
| today's date in the system prompt | so "last quarter" resolves against real dates |
| `cache_control` on every request | the history is re-sent every turn; caching makes repeats ~10% of the cost |

The loop lives in `agent_core.py` as a **generator** that yields events
(`tool_call`, `tool_result`, `answer`, `usage`), so the CLI and the UI share one
implementation. `step4_agent.py` is now a thin terminal front-end.

### Step 5 — Memory (fell out of step 3)

`save_note` was supposed to be its own step. It wasn't needed — the tool plus one
line in the system prompt was enough. The agent writes its own notes to
`cache/<db>.notes.json`, and they are injected into the prompt on every later
session:

```
1. Captured revenue = SUM(payments.amount) WHERE payments.status='success';
   successful payments only ever attach to orders with status paid/shipped/
   delivered, so the order-status filter is redundant for payment-based revenue.
2. This database cannot answer refund questions: orders.status declares
   'refunded' but zero rows ever use it, and payments.status only has
   'success'/'failed' with no negative amounts.
```

It also **corrects itself**. After a data fix, its next run wrote:
*"CORRECTION to earlier note: loans.returned_on DOES contain NULLs (599 of 3000)…"*

### Step 6 — The UI (`app.py`)

Streamlit. Database selector, model selector, effort selector, chat.

```
┌──────────────────────────────────────────────────────────┐
│ Database ▾    Model ▾       Effort ▾   $2/$10 per 1M     │
│ shop_demo     Sonnet 5      medium     [Clear chat]      │
├──────────────────────────────────────────────────────────┤
│ ▸ shop_demo — 7 tables, 1 view, 2 saved notes            │
├──────────────────────────────────────────────────────────┤
│ you: which payment method earns most from enterprise?    │
│                                                          │
│ ⟳ describe_table table=payments                          │
│ ⟳ run_sql SELECT p.method, SUM(p.amount)...              │
│ **COD brings in the most: ₹29.36M…**                     │
│ 2 API calls · 4 in / 424 out · 1,926 cached · $0.0104    │
├──────────────────────────────────────────────────────────┤
│ Ask about shop_demo...                                   │
└──────────────────────────────────────────────────────────┘
```

Every answer carries its real token cost, so model choice is a measurement, not
an opinion. The database dropdown reads `information_schema.schemata` **through
the read-only user**, so it can only ever list databases the agent is granted on.

---

## Security model

Three independent layers have to fail before a write can land:

```
1. the system prompt only offers read tools
2. check_and_limit() parses the SQL and rejects anything but one SELECT
3. the agent_ro MySQL user has no INSERT/UPDATE/DELETE grant at all
```

Layer 3 is the one that matters, because it holds even if the Python is buggy:

```bash
mysql -u agent_ro -p shop_demo -e "DELETE FROM orders WHERE id=1;"
# ERROR 1142: DELETE command denied to user 'agent_ro'@'localhost'
```

The credential lives in `.env` and is read by the app layer only. It is never
placed in a prompt, a tool argument, or a model response.

---

## Files

```
step1_extract.py     connection → schema card (facts only, no LLM)
step2_run_sql.py     the four gates + error classification
step3_tools.py       5 tool schemas, DBSession, dispatch()
agent_core.py        the agent loop as an event generator + model/price table
step4_agent.py       terminal front-end
app.py               Streamlit front-end

sql/seed.sql         shop_demo + the agent_ro read-only user
sql/seed_more.sql    hr_demo, library_demo, saas_demo

cache/<db>.json          extracted schema, reused on every run
cache/<db>.notes.json    facts the agent wrote down itself

.env                 DB_URL (agent_ro) + ANTHROPIC_API_KEY   [not committed]
TESTING.md           how to run it, and ~30 test questions
```

Each `stepN_*.py` is independently runnable — the fastest way to see what a layer
does on its own:

```bash
.venv/bin/python step1_extract.py    # print the schema card
.venv/bin/python step2_run_sql.py    # prove the guard blocks writes
.venv/bin/python step3_tools.py      # tool schemas + simulated LLM calls
```

---

## Quick start

> Full step-by-step setup, CLI and UI instructions, and troubleshooting are in
> **[RUN.md](RUN.md)**. The short version:

```bash
cd mysql_agent

# 1. build the demo databases (needs MySQL root once)
mysql -u root -p < sql/seed.sql
mysql -u root -p < sql/seed_more.sql

# 2. .env needs two lines
#    DB_URL=mysql+pymysql://agent_ro:<pw>@127.0.0.1:3306/shop_demo
#    ANTHROPIC_API_KEY=sk-ant-...

# 3. run it
.venv/bin/streamlit run app.py                     # web UI
.venv/bin/python step4_agent.py --db hr_demo       # terminal
```

Dependencies: `sqlalchemy`, `pymysql`, `sqlglot`, `anthropic`, `python-dotenv`,
`streamlit` (see `requirements.txt`).

---

## The four test databases

Each one breaks something different, on purpose.

| database | tables | what it stresses |
|---|---|---|
| `shop_demo` | 7 + view | the normal case: enums, comments, FKs, a deprecated junk table |
| `hr_demo` | 3 | **no comments at all** — only names, types, and profiled values |
| `library_demo` | 5 | **NULL carries meaning** (`returned_on IS NULL` = still out), many-to-many |
| `saas_demo` | 15 | **schema breadth** — too big to dump, must navigate by tool calls |

Trap questions and what a good answer looks like are in `TESTING.md`.

---

## Model choice

| Model | per 1M in / out | Notes |
|---|---|---|
| `claude-sonnet-5` | $2 / $10 | **default** — plenty for SQL |
| `claude-opus-5` | $5 / $25 | best at ambiguous, many-table questions |
| `claude-haiku-4-5` | $1 / $5 | cheapest; weaker at multi-table JOIN reasoning |

Three cost levers, all switchable in the UI:

1. **model** — Sonnet is 2.5× cheaper than Opus
2. **effort** — `medium` instead of the default `high` cuts thinking tokens
3. **prompt caching** — already on; the biggest single saving in a tool loop

Measured on a single-table question: Haiku ~$0.002, Sonnet ~$0.006, Opus ~$0.02.
Multi-table questions run 5–10× that.

---

## What this project is missing

Honest list, in the order it would matter:

- **Many databases at once.** Today the app layer swaps one `DB_URL`. A real
  registry would hold several connections live and give the model a
  `use_database` tool.
- **Table retrieval for very wide schemas.** At 15 tables, listing them all in
  the prompt is fine. At 300, you need to embed table descriptions and retrieve
  the relevant handful per question.
- **Row-level access control.** `agent_ro` is all-or-nothing per database. There
  is no "this user may not see salaries".
- **Result charting.** Answers are markdown tables; numbers over time want a chart.
- **Evals.** There is no scored question set, so "did that prompt change help?"
  is currently answered by eyeballing.
```
