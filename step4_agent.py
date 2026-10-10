"""
STEP 4 - The agent loop.

This is the while loop you described at the start, written out properly:

    you ask a question
      -> Claude decides which tool to call
      -> we run it and hand back the result
      -> Claude decides again ... (repeat)
      -> Claude stops calling tools and writes the answer

Everything Claude knows about your database arrives through the tools from
step 3. The database URL and password never enter the conversation.

Run:  .venv/bin/python step4_agent.py                            (interactive, DB from .env)
      .venv/bin/python step4_agent.py "how many orders?"          (one question)
      .venv/bin/python step4_agent.py --db hr_demo                (a different database)
      .venv/bin/python step4_agent.py --db saas_demo "..."        (both)
      add --refresh to re-extract the schema instead of using the cache

For the web UI instead:  .venv/bin/streamlit run app.py
"""
import datetime as dt
import json
import os
import sys

import anthropic
from dotenv import load_dotenv

from agent_core import DEFAULT_EFFORT, DEFAULT_MODEL, run_agent
from step3_tools import DBSession

MODEL = DEFAULT_MODEL    # claude-sonnet-5 - see agent_core.MODELS
EFFORT = DEFAULT_EFFORT

def ask(client, session, messages, question):
    """One question. Prints each tool call, returns the final answer.

    The loop itself lives in agent_core.run_agent() so the CLI and the Streamlit
    UI share one implementation. The system prompt is there too.
    """
    answer = ""
    for event in run_agent(client, session, messages, question,
                           model=MODEL, effort=EFFORT):
        if event["type"] == "tool_call":
            args = {k: " ".join(str(v).split()) for k, v in event["args"].items()}
            preview = ", ".join(f"{k}={v[:90]}" for k, v in args.items()) or "-"
            print(f"   \033[90m[{event['name']}] {preview}\033[0m")
        elif event["type"] == "tool_result" and event["is_error"]:
            print(f"   \033[91m   -> {event['output'].splitlines()[0]}\033[0m")
        elif event["type"] == "answer":
            answer = event["text"]
        elif event["type"] == "usage":
            print(f"   \033[90m[{event['api_calls']} API calls, "
                  f"{event['input']:,} in / {event['output']:,} out, "
                  f"{event['cache_read']:,} cached, ${event['cost']:.4f}]\033[0m\n")
    return answer


def resolve_url(db_name: str | None) -> str:
    """DB_URL from .env, with its database swapped out if --db was given.

    This is the whole trick behind 'point the agent at a different database':
    the app layer builds the URL. The model is never involved.
    """
    url = os.environ["DB_URL"]
    if not db_name:
        return url
    base, _, _ = url.rpartition("/")
    return f"{base}/{db_name}"


def main():
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to mysql_agent/.env")

    args = sys.argv[1:]
    db_name = None
    if args and args[0] == "--db":
        if len(args) < 2:
            sys.exit("usage: step4_agent.py [--db <database>] [question]")
        db_name = args[1]
        args = args[2:]
    refresh = "--refresh" in args
    args = [a for a in args if a != "--refresh"]

    session = DBSession(resolve_url(db_name), refresh=refresh)
    client = anthropic.Anthropic()
    messages = []

    print(f"\nConnected to `{session.db}` - {len(session.card['tables'])} tables. "
          f"Model: {MODEL}")

    if args:
        question = " ".join(args)
        print(f"\n> {question}\n")
        print(ask(client, session, messages, question))
        return

    print("Ask a question, or 'exit'.\n")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in {"exit", "quit", ""}:
            break
        print()
        print(ask(client, session, messages, question))
        print()


if __name__ == "__main__":
    main()
