# Feature development list

Your list, plus what I'd add, with an honest take on each: what it is, why it
matters, how big it is, and what it depends on.

Effort scale: **S** = hours · **M** = 1–2 days · **L** = a week or more.

---

## Where the project is today

| Works | Doesn't |
|---|---|
| extract any MySQL schema into a facts card, cached | one database per process; swapping needs a restart or a dropdown |
| read-only Q&A with self-correcting SQL | no writes at all |
| 3 Claude models, switchable, with live cost | no open-source / local model |
| per-database agent notes that persist | chat history dies when you close the tab |
| CLI + Streamlit UI on localhost | not reachable by anyone else |
| 4 demo databases, ~120 test questions | no *scored* test set — "did this change help?" is eyeballed |

---

## Your seven items

### 1. Add a new database from the UI — **M**

**What:** a "Connect database" form — host, port, user, password, database —
that extracts the schema and adds it to the dropdown. Replaces today's
single `DB_URL` in `.env`.

**Why it matters:** this is the feature that makes the project a *product*
instead of a demo. Everything else assumes it.

**What it needs:**
- a `Registry` class holding many live connections keyed by a URL hash, not by
  database name (two servers can both have a `shop_demo`)
- credential storage — **this is the real work**. Options, worst to best:
  session-only (retype every time), encrypted file with a local key, OS keychain,
  or a secrets manager. Pick before writing code, because it's hard to retrofit.
- a connection test with a clear error before the schema extract runs

**Watch out:** the moment a user can type any connection string, they can point
the agent at a production database with a write-capable user. Add a check that
refuses a connection whose user has INSERT/UPDATE/DELETE grants, or at minimum
warn loudly.

### 2. Multiple chats + persistent history — **M**

**What:** named conversations in a sidebar, saved across restarts. Each chat
pinned to one database.

**Why it matters:** right now every browser refresh loses the thread, and the
agent's notes are the only memory that survives.

**How:** SQLite file (`chats.db`) with `conversations(id, title, db_id,
created_at)` and `messages(id, conversation_id, role, content_json, usage_json)`.
Store the raw API message list as JSON so a conversation can be resumed exactly.

**Two real problems to solve:**
- **Context growth.** A 40-turn conversation full of tool results will blow past
  a sane token budget. You need either Claude's server-side compaction, or to
  drop old tool results while keeping the assistant text.
- **Auto-titling.** Name the chat from the first question — a cheap Haiku call,
  or just the first 50 characters.

### 3. "Optimise the agent using ReAct" — **S, but not what you think**

**Correction worth making:** this agent is *already* ReAct. Reason → Act →
Observe → repeat is exactly what the `tool_use` loop does. Adopting a ReAct
*framework* (LangChain's `ReActAgent` and friends) would make it **worse**: those
frameworks make the model emit `Thought: ... Action: ... Action Input: ...` as
plain text, which then has to be parsed with regex. Native `tool_use` gives you
structured, schema-validated calls with none of that fragility.

So skip the framework. What actually improves answer quality, in order of
payoff:

| Upgrade | Effort | What it does |
|---|---|---|
| **Few-shot from past success** | M | store every question→SQL pair that worked; inject the 3 most similar into the prompt. The single biggest accuracy win available. |
| **A verification pass** | S | after answering, run one cross-check query (a different formulation) and report if they disagree. The agent already does this sometimes — make it a rule. |
| **A plan step on hard questions** | S | for multi-table questions, have it state the join path before writing SQL. Catches wrong-grain joins (the fan-out double-counting bug). |
| **Tighter retry policy** | S | today it retries up to 3 times on any error. Different error types deserve different responses: `unknown_column` → re-describe the table; `timeout` → add a filter, don't just retry. |
| **Prompt-level grain warnings** | S | tell it explicitly that joining `orders` to `order_items` multiplies rows, so `SUM(orders.shipping_fee)` over that join is wrong. This is the #1 silent error in text-to-SQL. |

### 4. Write mode — create / modify records — **L, and the riskiest thing here**

**What:** let the agent INSERT / UPDATE / DELETE.

**Be clear-eyed about this.** Every protection in the project today exists
because the agent cannot write. Turning that off removes the safety net, and
"the LLM wrote a DELETE without a WHERE clause" is a real failure mode, not a
hypothetical.

If you build it, build it in this order and don't skip steps:

```
1. A SECOND connection          agent_rw, granted only on specific tables,
                                never the same connection as reads
2. Explicit intent              a separate propose_write tool. The read path
                                stays SELECT-only forever.
3. Dry run, always              run inside a transaction, show affected row
                                count and a before/after sample, then ROLLBACK
4. Human approval               render the SQL + the dry-run result, require a
                                click. No auto-apply. Ever, at first.
5. Hard rules in code           - no UPDATE/DELETE without a WHERE
                                - refuse if dry run affects > N rows
                                - table allowlist
                                - no DDL, no TRUNCATE, no multi-table updates
6. Audit log                    who asked, what SQL, how many rows, when,
                                approved by whom — in your own table
7. Undo path                    for UPDATE, store the before-image so a
                                reversal statement can be generated
```

**Start narrower than you think.** One table, one operation — e.g. "update an
order's status" — with step 3–6 fully in place. A general-purpose write agent is
a much larger project than a read agent.

### 5. Open-source model (Gemma / Llama / Qwen) — **M**

**Why you'd want it:** zero per-question cost, and the schema never leaves your
machine. For a database agent the privacy argument is genuinely strong.

**What to expect, honestly:** a noticeable quality drop on the things this
project is actually about — multi-table joins, NULL semantics, choosing between
two plausible definitions. Warm-up and single-table questions will be fine.

**Practical route:**
- **Ollama** locally; it exposes an OpenAI-compatible endpoint with tool calling.
  `gemma3`, `qwen2.5-coder`, and `llama3.1` are the candidates — `qwen2.5-coder`
  is usually the best of them at SQL specifically.
- Refactor `agent_core.run_agent()` behind a small provider interface
  (`send(messages, tools) -> (text, tool_calls, usage)`), with an Anthropic
  implementation and an Ollama one. The tool *schemas* are already provider-neutral
  JSON Schema, so this is less work than it sounds.
- Expect to need a tool-call repair layer: smaller models emit malformed
  arguments. Validate against the schema and hand back the error.

**Do item 10 (evals) first.** Otherwise you cannot tell whether Gemma is
"good enough" — you'll be comparing vibes on different questions.

**A middle option worth considering:** keep Claude Haiku 4.5 ($1/$5) instead. At
roughly $0.002 a question it is cheap enough that "free" rarely justifies the
quality loss — unless the driver is privacy, in which case local is the only
answer.

### 6. Expose over HTTP and share with Siva — **M, with a hard prerequisite**

**What:** a URL Siva can open.

**The prerequisite is authentication, and it is not optional.** An unauthenticated
URL is an open query console on your database. Before sharing anything:

| Need | Why |
|---|---|
| login | otherwise anyone who finds the URL can read your data |
| per-user DB credentials, or one read-only replica | so you can revoke one person without touching the rest |
| rate limit + per-user cost cap | your API key pays for every question asked |
| audit log of question → SQL → user | you will want this the first time an answer looks wrong |

**Architecture:** Streamlit is the wrong tool for multi-user. Split it:

```
FastAPI backend          /chat (SSE stream), /databases, /conversations
  - owns connections, credentials, the agent loop
  - one session per user, no shared mutable state
React/HTMX frontend      or keep Streamlit as an internal-only admin view
```

**For "just show Siva tomorrow":** Tailscale or `cloudflared tunnel` to your
localhost, plus a shared password. That's hours, not days — and nothing is on
the public internet. Do the real backend when more than two people need it.

### 7. Other features worth building

Ordered by value for effort.

| # | Feature | Effort | Why |
|---|---|---|---|
| 8 | **Charts** | S | "revenue by month" wants a line, not a markdown table. Have the agent return a chart spec; render with `st.line_chart` / Vega. |
| 9 | **Export results to CSV** | S | the first thing anyone asks for after seeing an answer. |
| 10 | **Evals** | M | **do this early.** Turn `QUESTIONS.md` into a scored set: question → expected SQL result (not expected wording). Then every prompt change, model swap, and retry-policy tweak becomes a measurement. Without it, items 3 and 5 are guesswork. |
| 11 | **Query history, re-run, pin** | S | a tab listing every SQL the agent ever ran, with a re-run button. Doubles as your debugging tool. |
| 12 | **Schema drift detection** | S | hash the card; if `information_schema` changes, auto-refresh. Today a dropped column means wrong answers until someone passes `--refresh`. |
| 13 | **Column-level masking** | M | `employees.salary`, emails, phone numbers. A deny-list the extractor and `run_sql` both respect, so the agent can aggregate salary without seeing individual rows. Needed before anyone outside your team uses it. |
| 14 | **Table retrieval for wide schemas** | M | at 15 tables, listing them all is fine. At 300, embed the table descriptions and retrieve the relevant handful per question. Only build when you hit a real schema that big. |
| 15 | **Postgres + SQLite support** | M | `step1_extract.py` is MySQL-specific (`information_schema` with MySQL quirks); `step2` is nearly dialect-neutral already thanks to sqlglot. Worth it the first time someone has a Postgres database. |
| 16 | **Scheduled reports** | M | "email me weekly revenue every Monday" — the agent writes the SQL once, cron runs it. High perceived value, low complexity, since it reuses everything. |
| 17 | **Observability** | S | log question, tool calls, SQL, row counts, tokens, cost, latency to a table. You cannot improve what you cannot see. |
| 18 | **"Explain this query" mode** | S | paste SQL, get an explanation. Reuses the schema card, no new machinery, genuinely useful for learning a database. |
| 19 | **Multi-database questions** | L | "compare order volume in shop_demo against tickets in saas_demo" — needs two connections and client-side joining. Niche; defer. |

---

## Suggested order

Each phase is shippable on its own.

```
PHASE 1  make it usable                              ~1 week
  1   add a database from the UI            M
  2   multiple chats + persistent history   M
  9   CSV export                            S
  8   charts                                S

PHASE 2  make it trustworthy                         ~1 week
  10  evals from QUESTIONS.md               M   <- unblocks everything after
  17  observability                         S
  12  schema drift detection                S
  3   few-shot + verification + grain rules M

PHASE 3  make it shareable                           ~1 week
  6   FastAPI backend + auth                M
  13  column masking                        M
      per-user credentials + cost caps      M

PHASE 4  pick based on what you actually need
  5   local model via Ollama                M   (needs phase 2 evals)
  15  Postgres support                      M
  16  scheduled reports                     M
  4   write mode, one table, fully gated    L   (do last, deliberately)
```

**Why evals sit in phase 2 rather than later:** items 3, 4, and 5 are all
"change the agent's behaviour and hope it got better". With a scored set you
measure instead. It's the difference between engineering and tinkering, and it
gets cheaper the earlier you build it — `QUESTIONS.md` is already most of the
input.

**Why write mode is last:** not because it's hard to make work, but because it's
hard to make *safe*, and every other feature is more valuable per hour spent.
Also, by then you'll have the audit log (17), auth (6), and evals (10) that a
write path actually requires.

---

## Things I'd push back on

| Idea | Why not |
|---|---|
| a ReAct / LangChain agent framework | you'd replace structured `tool_use` with regex-parsed text. Strictly worse. See item 3. |
| letting the model hold the connection URL | the credential would have to pass through the conversation. Keep URL handling in the app layer. |
| one giant "do anything" SQL tool with writes enabled | the separation between read and write paths is the safety property. Don't collapse it. |
| caching answers | cache the *schema* and the *question→SQL mapping*, never the answer — the data changes underneath you. |
| vector-embedding every row for "semantic search" | this is a SQL problem. SQL is already the right retrieval engine over structured data. |
| fine-tuning a model on your schema | few-shot examples (item 3) get most of the benefit for a fraction of the work, and don't go stale when the schema changes. |

---

## Open questions for you

These change the plan materially:

1. **Who are the users?** Just you and Siva, or a team? Two people means a
   tunnel and a shared password. Ten means phase 3 properly.
2. **Which real database is the target?** The demos are synthetic. A real schema
   will expose extraction gaps (composite keys, no FK constraints declared,
   `varchar` columns holding JSON) that `shop_demo` never will.
3. **Is write mode actually needed, or is it "would be cool"?** If a human is
   going to approve every write anyway, a generated SQL statement they paste
   themselves may be 90% of the value at 10% of the risk.
4. **Is the Gemma interest about cost or privacy?** Cost → Haiku is already
   ~$0.002/question. Privacy → local is the only option and the quality tradeoff
   is worth accepting.
