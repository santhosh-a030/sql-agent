"""
The agent loop, as a generator so both the CLI and the UI can use it.

step4_agent.py was the teaching version. This is the same loop, but it YIELDS
events as they happen instead of printing them, which is what a UI needs:

    for event in run_agent(...):
        event["type"] == "tool_call"   -> a tool is about to run
        event["type"] == "tool_result" -> what it returned
        event["type"] == "answer"      -> the final text
        event["type"] == "usage"       -> tokens and cost for this question
"""
import datetime as dt

from step3_tools import TOOLS, dispatch

# ---------------------------------------------------------------- model choice
# Price per 1M tokens, for the cost readout.
MODELS = {
    "claude-sonnet-5": {"label": "Sonnet 5  (recommended)", "in": 2.00, "out": 10.00,
                        "cache_write": 2.50, "cache_read": 0.20, "effort": True},
    "claude-opus-5":   {"label": "Opus 5  (most capable)",  "in": 5.00, "out": 25.00,
                        "cache_write": 6.25, "cache_read": 0.50, "effort": True},
    "claude-haiku-4-5": {"label": "Haiku 4.5  (cheapest)",  "in": 1.00, "out": 5.00,
                         "cache_write": 1.25, "cache_read": 0.10, "effort": False},
}
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "medium"
MAX_TOOL_TURNS = 12

SYSTEM_TEMPLATE = """You are a MySQL analyst answering questions about one database.

Today's date is {today}.

{overview}

How to work:
1. Before writing SQL against a table, call describe_table on it. Never guess a
   column name or a value.
2. The "values present" list from describe_table is authoritative. Use those exact
   literals in WHERE clauses - do not invent casing or synonyms.
3. Write one MySQL SELECT and run it with run_sql. A LIMIT is added for you.
4. If run_sql returns an error, read the Hint line, fix the query, and try again.
   Give up after three failed attempts and explain what blocked you.
5. When you learn something durable about this database - a business definition, a
   non-obvious join, a table to avoid - call save_note once. Do not save answers to
   one-off questions.

How to answer:
- Lead with the number or the finding, in one sentence.
- Then the supporting rows if there are several.
- State the SQL you ran, in a code block, at the end.
- If the question is ambiguous (e.g. "revenue" could mean captured payments or
  ordered value), say which definition you used and why.
- Never present a guess as a fact. If the data cannot answer the question, say so.
"""


def build_system(session) -> str:
    return SYSTEM_TEMPLATE.format(today=dt.date.today().isoformat(),
                                  overview=session.overview())


def cost_of(model: str, usage) -> float:
    p = MODELS.get(model)
    if p is None:
        return 0.0
    m = 1_000_000
    return (
        (usage.input_tokens or 0) * p["in"] / m
        + (usage.output_tokens or 0) * p["out"] / m
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p["cache_write"] / m
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p["cache_read"] / m
    )


def run_agent(client, session, messages, question, *,
              model=DEFAULT_MODEL, effort=DEFAULT_EFFORT):
    """Run one question to completion. Mutates `messages`. Yields events."""
    messages.append({"role": "user", "content": question})
    system = build_system(session)

    totals = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
              "cost": 0.0, "api_calls": 0}

    for _ in range(MAX_TOOL_TURNS):
        kwargs = {
            "model": model,
            "max_tokens": 16000,
            "system": system,
            "tools": TOOLS,
            "messages": messages,
            # Caches the longest stable prefix. In a tool loop the history is
            # re-sent every turn, so this is where the real saving is.
            "cache_control": {"type": "ephemeral"},
        }
        if MODELS.get(model, {}).get("effort") and effort:
            kwargs["output_config"] = {"effort": effort}

        response = client.messages.create(**kwargs)

        u = response.usage
        totals["input"] += u.input_tokens or 0
        totals["output"] += u.output_tokens or 0
        totals["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        totals["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        totals["cost"] += cost_of(model, u)
        totals["api_calls"] += 1

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            yield {"type": "answer", "text": "_The model declined this request._"}
            yield {"type": "usage", **totals}
            return

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text")
            yield {"type": "answer", "text": text}
            yield {"type": "usage", **totals}
            return

        results = []
        for block in tool_uses:
            yield {"type": "tool_call", "name": block.name, "args": block.input}

            output = dispatch(session, block.name, block.input)
            is_error = output.startswith(("ERROR", "No table named", "No such tool"))
            yield {"type": "tool_result", "name": block.name,
                   "output": output, "is_error": is_error}

            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
                **({"is_error": True} if is_error else {}),
            })

        # every result from one assistant turn goes back in ONE user message
        messages.append({"role": "user", "content": results})

    yield {"type": "answer",
           "text": f"_Stopped after {MAX_TOOL_TURNS} tool turns without reaching an answer._"}
    yield {"type": "usage", **totals}
