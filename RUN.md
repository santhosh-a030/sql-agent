# How to run this project

Written for someone who has never seen this repo. Follow it top to bottom.

- **Part 1** — setup, once (≈10 minutes)
- **Part 2** — run the CLI
- **Part 3** — run the web UI
- **Part 4** — troubleshooting

Every command assumes you are inside the project folder:

```bash
cd mysql_agent
```

---

## Part 1 — Setup (once)

### 1.1 What you need

| Thing | Check it with | If missing |
|---|---|---|
| Python 3.10+ | `python3 --version` | install from python.org or `brew install python` |
| MySQL 8+ running | `mysqladmin ping` → `mysqld is alive` | `brew install mysql && brew services start mysql` |
| MySQL root password | you set it at install time | `mysql_secure_installation` |
| An Anthropic API key | — | create one at https://console.anthropic.com |

### 1.2 Create the Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

This installs: `sqlalchemy`, `pymysql`, `sqlglot`, `anthropic`, `python-dotenv`,
`streamlit`.

> Everything below calls `.venv/bin/python` and `.venv/bin/streamlit` directly, so
> you never have to remember to activate the venv. If you prefer activating it
> (`source .venv/bin/activate`), drop the `.venv/bin/` prefix from every command.

### 1.3 Create the demo databases

Two SQL files build four practice databases and the read-only user the agent
uses. You will be prompted for your **MySQL root password**.

```bash
mysql -u root -p < sql/seed.sql        # shop_demo  + creates the agent_ro user
mysql -u root -p < sql/seed_more.sql   # hr_demo, library_demo, saas_demo
```

Check it worked:

```bash
mysql -u root -p -e "
SELECT table_schema AS db, COUNT(*) AS tables
FROM information_schema.tables
WHERE table_schema LIKE '%_demo'
GROUP BY table_schema;"
```

Expected:

```
+--------------+--------+
| db           | tables |
+--------------+--------+
| hr_demo      |      3 |
| library_demo |      5 |
| saas_demo    |     15 |
| shop_demo    |      8 |
+--------------+--------+
```

**What `seed.sql` created besides tables:** a MySQL user called `agent_ro` with
`SELECT` permission only, on the demo databases only. The agent connects as this
user, so it physically cannot write to your data or reach any other database.

### 1.4 Create the `.env` file

```bash
cp .env.example .env
```

Then open `.env` and fill in two lines:

```ini
DB_URL=mysql+pymysql://agent_ro:Ag3nt_RO%212026%23sql@127.0.0.1:3306/shop_demo
ANTHROPIC_API_KEY=sk-ant-...your-key-here...
```

Notes on `DB_URL`:

- `agent_ro` / `Ag3nt_RO!2026#sql` is the read-only user from `seed.sql`. If you
  changed that password, change it here too.
- Special characters must be **URL-encoded**: `!` → `%21`, `#` → `%23`,
  `@` → `%40`.
- The database at the end (`/shop_demo`) is just the default. The CLI's `--db`
  flag and the UI's dropdown switch it at runtime.

`.env` is in `.gitignore` — never commit it.

### 1.5 Verify setup before spending any API credit

```bash
.venv/bin/python step1_extract.py
```

This talks to MySQL only — **no API call, no cost**. You should see a long
printout of every table, column, enum value and date range, ending with:

```
full card written to cache/shop_demo.json
```

If that works, the database half is correct.

---

## Part 2 — Run the CLI

### 2.1 Ask one question

```bash
.venv/bin/python step4_agent.py "which city has the highest average order value?"
```

You will see the agent's tool calls in grey as they happen, then the answer:

```
Connected to `shop_demo` - 7 tables. Model: claude-sonnet-5

> which city has the highest average order value?

   [describe_table] table=customers
   [describe_table] table=orders
   [run_sql] sql=SELECT c.city, ROUND(AVG(v.order_total), 2) AS aov ...
   [3 API calls, 12 in / 891 out, 4,210 cached, $0.0109]

**Chennai has the highest average order value at ₹24,718** ...
```

The last grey line is the real cost of that question.

### 2.2 Interactive chat

```bash
.venv/bin/python step4_agent.py
```

Type questions at the `>` prompt. History is kept, so follow-ups work:

```
> which city has the highest average order value?
...
> now show me just the enterprise customers in that city
```

Type `exit` (or press `Ctrl+C`) to quit.

### 2.3 Switch database

```bash
.venv/bin/python step4_agent.py --db hr_demo
.venv/bin/python step4_agent.py --db library_demo "what is overdue right now?"
.venv/bin/python step4_agent.py --db saas_demo "how many users never logged in?"
```

### 2.4 All CLI options

```
.venv/bin/python step4_agent.py [--db <database>] [--refresh] [question...]

  --db <name>   use this database instead of the one in .env
  --refresh     re-read the schema from MySQL instead of using cache/<db>.json
                (needed after you change tables or columns)
  question...   ask one question and exit; omit it for interactive chat
```

### 2.5 Run the layers on their own (no API cost)

Useful for understanding what each piece does:

```bash
.venv/bin/python step1_extract.py    # print the full schema card
.venv/bin/python step2_run_sql.py    # show which SQL is allowed vs blocked
.venv/bin/python step3_tools.py      # print tool definitions + simulated calls
```

---

## Part 3 — Run the web UI

### 3.1 Start it

```bash
.venv/bin/streamlit run app.py
```

It prints a URL and opens your browser:

```
  You can now view your Streamlit app in your browser.
  Local URL: http://localhost:8501
```

Press `Ctrl+C` in the terminal to stop it.

### 3.2 What you see

```
┌──────────────────────────────────────────────────────────┐
│ Database ▾    Model ▾       Effort ▾   $2/$10 per 1M     │
│ shop_demo     Sonnet 5      medium     [Clear chat]      │
├──────────────────────────────────────────────────────────┤
│ ▸ shop_demo — 7 tables, 1 view, 2 saved notes            │
├──────────────────────────────────────────────────────────┤
│                                                          │
│   (your conversation appears here)                       │
│                                                          │
├──────────────────────────────────────────────────────────┤
│ Ask about shop_demo...                                   │
└──────────────────────────────────────────────────────────┘
```

| Control | What it does |
|---|---|
| **Database** | lists every database `agent_ro` can read. Each keeps its own chat history. |
| **Model** | Sonnet 5 (default, good value), Opus 5 (strongest), Haiku 4.5 (cheapest) |
| **Effort** | `low` / `medium` / `high` — how much the model thinks before answering. Lower is cheaper. |
| **Clear chat** | forget the conversation for the selected database |
| **The `▸` bar** | click to see exactly what the model was told about this database |

### 3.3 Using it

1. Pick a database from the dropdown.
2. Type a question in the box at the bottom and press Enter.
3. Watch the `⟳ tool_name ...` lines appear — that is the agent inspecting
   tables and running SQL.
4. Read the answer. Under it is a cost line:
   `2 API calls · 4 in / 424 out · 1,926 cached · $0.0104`
5. Click any tool row to expand it and see the exact SQL and the rows returned.

To compare models, ask the **same question** on Sonnet and then on Opus and
compare the cost lines.

### 3.4 Questions to start with

```
shop_demo      which payment method brings in the most revenue from enterprise customers?
shop_demo      how many refunded orders did we have last quarter?
hr_demo        how many people actually work here right now?
library_demo   how many books are overdue right now and who are the worst offenders?
saas_demo      what is our current MRR by region?
```

About 30 more, with notes on what a good answer looks like, are in `TESTING.md`.

---

## Part 4 — Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Can't connect to MySQL server on '127.0.0.1'` | MySQL not running | `brew services start mysql` |
| `Access denied for user 'agent_ro'` | wrong password in `DB_URL`, or `seed.sql` not run | re-run `mysql -u root -p < sql/seed.sql`, check URL-encoding of the password |
| `Unknown database 'shop_demo'` | seed files not run | Part 1.3 |
| `ANTHROPIC_API_KEY is not set` | missing or empty in `.env` | put your key after the `=` with no quotes and no spaces |
| `authentication_error` from the API | key is wrong or revoked | make a new one at console.anthropic.com |
| `command not found: streamlit` | using the system Python | use `.venv/bin/streamlit`, not bare `streamlit` |
| `ModuleNotFoundError: No module named 'sqlalchemy'` | deps not installed, or wrong Python | re-run Part 1.2; always call `.venv/bin/python` |
| Agent talks about a column you just deleted | stale schema cache | add `--refresh`, or `rm cache/<db>.json` |
| `Address already in use` (port 8501) | a Streamlit instance is still running | `pkill -f "streamlit run app.py"` or `--server.port 8502` |
| Answers look wrong | — | expand the tool rows in the UI and read the actual SQL it ran |

### Resetting things

```bash
# wipe the extracted schemas (they rebuild on next run)
rm cache/*.json

# wipe the agent's saved notes for one database
rm cache/shop_demo.notes.json

# rebuild the demo databases from scratch (destroys and recreates them)
mysql -u root -p < sql/seed.sql
mysql -u root -p < sql/seed_more.sql
```

### Checking the agent really cannot write

```bash
mysql -u agent_ro -p shop_demo -e "DELETE FROM orders WHERE id=1;"
# ERROR 1142 (42000): DELETE command denied to user 'agent_ro'@'localhost'
```

That error is the point. Even if the Python guard had a bug, MySQL itself
refuses.

---

## Where to read next

| File | What's in it |
|---|---|
| `README.md` | how the project was built, step by step, and why each piece exists |
| `TESTING.md` | ~30 test questions, including deliberate traps |
| `step1_extract.py` | what the agent is told about your database |
| `agent_core.py` | the agent loop and the system prompt |
