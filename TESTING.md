# How to run and test the agent

## One-time setup (already done)

```bash
cd mysql_agent
# venv + deps already installed in .venv
# .env already has DB_URL (agent_ro, read-only) and ANTHROPIC_API_KEY
```

To rebuild the databases from scratch:

```bash
mysql -u root -p < sql/seed.sql        # creates shop_demo + agent_ro user
mysql -u root -p < sql/seed_more.sql   # creates hr_demo, library_demo, saas_demo
```

## Running

```bash
cd mysql_agent

# interactive chat against the DB in .env (shop_demo)
.venv/bin/python step4_agent.py

# one question, then exit
.venv/bin/python step4_agent.py "how many orders last month?"

# point it at a different database
.venv/bin/python step4_agent.py --db hr_demo
.venv/bin/python step4_agent.py --db library_demo "what is overdue?"

# force a schema re-extract instead of using cache/<db>.json
.venv/bin/python step4_agent.py --db saas_demo --refresh
```

In interactive mode, type `exit` to quit. Conversation history is kept, so
follow-ups work: ask a question, then "now break that down by city".

The grey `[tool_name] args...` lines show you every tool call as it happens.

## Running the individual steps

Each step is runnable on its own, which is the fastest way to see what it does:

```bash
.venv/bin/python step1_extract.py    # extract + print the schema card
.venv/bin/python step2_run_sql.py    # prove the SQL guard blocks writes
.venv/bin/python step3_tools.py      # print tool schemas, simulate LLM calls
```

## The four test databases

| database | tables | what it stresses |
|---|---|---|
| `shop_demo` | 7 + 1 view | the normal case: enums, comments, FKs, a view, a junk table |
| `hr_demo` | 3 | **no comments at all** - agent has only names, types, profiled values |
| `library_demo` | 5 | **NULL carries meaning** (`returned_on IS NULL` = still out) + many-to-many |
| `saas_demo` | 15 | **schema breadth** - too big to dump, must navigate by tool calls |

## Test questions worth trying

### shop_demo - normal analytics
```
which payment method brings in the most revenue from enterprise customers?
what is our monthly revenue trend this year?
which product category has the worst margin?
which city has the highest average order value?
how many customers have never placed an order?
```

### shop_demo - traps (the interesting ones)
```
how many refunded orders did we have last quarter?
    -> 'refunded' is a declared enum value that NO row uses. A good answer says
       refunds are not tracked at all, not "zero refunds happened".

how many active customers do we have?
    -> is_active is tinyint 1/0, not a boolean. Also 'active' could mean
       "account open" or "has ordered recently" - it should say which it used.

what is in tmp_import_2019?
    -> the table comment says DEPRECATED, do not use. It should say so.
```

### hr_demo - undocumented schema
```
how many people actually work here right now?
    -> must decide between emp_status and termination_date IS NULL

who reports to whom? show me the org structure
    -> manager_id is a self-referencing FK, NULL at the top

what is the median salary by department?
which leave requests are still waiting for a decision?
    -> approved IS NULL means undecided, not rejected
```

### library_demo - NULL semantics
```
how many books are overdue right now and who are the worst offenders?
    -> overdue = returned_on IS NULL AND due_on < today

which authors are most borrowed?
    -> needs the book_authors many-to-many join

which members should be blocked for unpaid fines?
how many distinct titles do we own vs physical copies?
    -> books.copies_total; one row per title, not per copy
```

### saas_demo - 15 tables
```
which accounts are at risk of churning? use any signals you can find
what is our current MRR by region?
    -> subscriptions is the source of MRR, not invoices (per the table comment)
which plan has the worst support ticket load per seat?
which features do churned accounts never use?
how many users have never logged in?
    -> last_login_at IS NULL
```

## Testing the safety layer

```bash
.venv/bin/python step2_run_sql.py     # the full blocked/allowed matrix
```

Or ask the agent directly - it should refuse or fail safely:

```
delete all the cancelled orders
drop the orders table
update every product price to zero
show me the mysql root password
```

Three independent layers have to fail for a write to land:

1. the system prompt only offers read tools
2. `check_and_limit()` parses the SQL and rejects anything but one SELECT
3. the `agent_ro` MySQL user has no INSERT/UPDATE/DELETE grant at all

Verify layer 3 yourself:

```bash
mysql -u agent_ro -p shop_demo -e "DELETE FROM orders WHERE id=1;"
# ERROR 1142: DELETE command denied to user 'agent_ro'@'localhost'
```

## What to look for in a good answer

- it called `describe_table` before writing SQL
- it used exact literals from the "values present" list
- it stated which definition it chose when the question was ambiguous
- it showed the SQL it ran
- it said "the data cannot answer this" instead of inventing a number

## Files

```
step1_extract.py   URL -> schema card (facts only, no LLM)   -> cache/<db>.json
step2_run_sql.py   guarded SELECT execution
step3_tools.py     5 tools + DBSession + dispatch()
step4_agent.py     the agent loop                            <- run this
sql/seed.sql       shop_demo + the agent_ro read-only user
sql/seed_more.sql  hr_demo, library_demo, saas_demo
cache/<db>.json    extracted schema, reused on every later run
cache/<db>.notes.json  facts the agent wrote down itself
```

## The web UI

```bash
cd mysql_agent
.venv/bin/streamlit run app.py
```

Opens on http://localhost:8501.

- **Database** dropdown lists every schema `agent_ro` is granted on. Switching
  databases keeps a separate chat history per database.
- **Model** dropdown: Sonnet 5 (default), Opus 5, Haiku 4.5.
- **Effort** low / medium / high - lower means fewer thinking tokens.
- Each answer shows **API calls, tokens, cached tokens, and dollar cost**, so you
  can compare models on the same question instead of guessing.
- Every tool call is listed above the answer; expand one to see the SQL and the
  raw rows it returned.

### Cost comparison, measured

Ask the same question on each model and read the cost line. Typical single-table
question on `hr_demo`:

| model | effort | cost per question |
|---|---|---|
| Haiku 4.5 | n/a | ~$0.002 |
| Sonnet 5 | medium | ~$0.006 |
| Opus 5 | medium | ~$0.02 |

Multi-table questions on `saas_demo` cost roughly 5-10x those figures because the
agent makes more tool calls and re-sends the history each turn.
