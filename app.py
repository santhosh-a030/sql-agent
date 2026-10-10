"""
Streamlit UI for the SQL agent.

Run:  cd mysql_agent && .venv/bin/streamlit run app.py

Layout: database selector + model selector on top, chat below. Every tool call
the agent makes is shown as it happens, and each answer carries its token cost
so you can compare models on real questions instead of guessing.
"""
import os

import anthropic
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from agent_core import DEFAULT_EFFORT, DEFAULT_MODEL, MODELS, run_agent
from step3_tools import DBSession

load_dotenv()

st.set_page_config(page_title="SQL Agent", page_icon="🗄", layout="wide")


# ------------------------------------------------------------------ plumbing
def base_url() -> str:
    """The DB_URL from .env with the database name stripped off."""
    return os.environ["DB_URL"].rpartition("/")[0]


@st.cache_data(ttl=300)
def list_databases() -> list[str]:
    """Only the databases agent_ro is actually granted SELECT on."""
    engine = create_engine(f"{base_url()}/information_schema")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT schema_name AS schema_name
            FROM information_schema.schemata
            WHERE schema_name NOT IN
                  ('mysql','information_schema','performance_schema','sys')
            ORDER BY schema_name
        """)).scalars().all()
    return list(rows)


@st.cache_resource(show_spinner="Loading schema...")
def get_session(db_name: str) -> DBSession:
    """One DBSession per database, cached across reruns."""
    return DBSession(f"{base_url()}/{db_name}")


@st.cache_resource
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def chat_key(db: str) -> str:
    return f"messages::{db}"


# -------------------------------------------------------------------- header
if not os.environ.get("ANTHROPIC_API_KEY"):
    st.error("ANTHROPIC_API_KEY is not set. Add it to mysql_agent/.env and restart.")
    st.stop()

try:
    databases = list_databases()
except Exception as e:
    st.error(f"Could not connect to MySQL with DB_URL from .env:\n\n{e}")
    st.stop()

if not databases:
    st.error("The agent_ro user has no databases granted to it.")
    st.stop()

top = st.columns([3, 3, 2, 2])

with top[0]:
    default_db = os.environ["DB_URL"].rpartition("/")[2]
    db_name = st.selectbox(
        "Database", databases,
        index=databases.index(default_db) if default_db in databases else 0,
    )

with top[1]:
    model = st.selectbox(
        "Model", list(MODELS),
        index=list(MODELS).index(DEFAULT_MODEL),
        format_func=lambda m: MODELS[m]["label"],
    )

with top[2]:
    supports_effort = MODELS[model]["effort"]
    effort = st.selectbox(
        "Effort", ["low", "medium", "high"],
        index=["low", "medium", "high"].index(DEFAULT_EFFORT),
        disabled=not supports_effort,
        help="Lower effort = fewer thinking tokens = cheaper."
             if supports_effort else "Not supported on this model.",
    )

with top[3]:
    st.caption(
        f"${MODELS[model]['in']:.2f} in / ${MODELS[model]['out']:.2f} out per 1M"
    )
    if st.button("Clear chat", use_container_width=True):
        st.session_state.pop(chat_key(db_name), None)
        st.rerun()

session = get_session(db_name)
client = get_client()

with st.expander(
    f"`{session.db}` — {len(session.card['tables'])} tables, "
    f"{len(session.card['views'])} view(s), {len(session.notes)} saved note(s)"
):
    st.code(session.overview(), language="text")

st.session_state.setdefault(chat_key(db_name), [])
history = st.session_state[chat_key(db_name)]   # [{role, content, tools?, usage?}]


# ---------------------------------------------------------------- transcript
def render_tools(tools: list[dict]) -> None:
    for t in tools:
        args = ", ".join(f"{k}={v}" for k, v in t["args"].items()) or "—"
        icon = "⚠️" if t.get("is_error") else "✅"
        with st.expander(f"{icon}  `{t['name']}`  {args[:110]}", expanded=False):
            if t["name"] == "run_sql" and "sql" in t["args"]:
                st.code(t["args"]["sql"], language="sql")
            st.code(t.get("output", ""), language="text")


for turn in history:
    with st.chat_message(turn["role"]):
        if turn.get("tools"):
            render_tools(turn["tools"])
        st.markdown(turn["content"])
        if turn.get("usage"):
            u = turn["usage"]
            st.caption(
                f"{u['api_calls']} API calls · {u['input']:,} in / {u['output']:,} out"
                f" · {u['cache_read']:,} cached · **${u['cost']:.4f}**"
            )


# ------------------------------------------------------------------ new turn
question = st.chat_input(f"Ask about {session.db}...")

if question:
    history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    # the model needs the raw API message list, which we keep separately
    api_key_name = f"api_messages::{db_name}"
    st.session_state.setdefault(api_key_name, [])
    api_messages = st.session_state[api_key_name]

    with st.chat_message("assistant"):
        tool_log: list[dict] = []
        tools_area = st.container()
        answer_area = st.empty()
        usage_area = st.empty()
        answer, usage = "", None

        try:
            for event in run_agent(client, session, api_messages, question,
                                   model=model, effort=effort):
                if event["type"] == "tool_call":
                    tool_log.append({"name": event["name"], "args": event["args"]})
                    args = ", ".join(f"{k}={v}" for k, v in event["args"].items()) or "—"
                    with tools_area:
                        st.caption(f"⟳ `{event['name']}` {args[:110]}")

                elif event["type"] == "tool_result":
                    tool_log[-1]["output"] = event["output"]
                    tool_log[-1]["is_error"] = event["is_error"]
                    if event["is_error"]:
                        with tools_area:
                            st.caption(f"↳ {event['output'].splitlines()[0]}")

                elif event["type"] == "answer":
                    answer = event["text"]
                    answer_area.markdown(answer)

                elif event["type"] == "usage":
                    usage = {k: v for k, v in event.items() if k != "type"}
                    usage_area.caption(
                        f"{usage['api_calls']} API calls · "
                        f"{usage['input']:,} in / {usage['output']:,} out · "
                        f"{usage['cache_read']:,} cached · **${usage['cost']:.4f}**"
                    )
        except anthropic.APIStatusError as e:
            answer = f"**API error {e.status_code}:** {e.message}"
            answer_area.error(answer)
        except Exception as e:                      # keep the UI alive
            answer = f"**Failed:** {e}"
            answer_area.error(answer)

    history.append({"role": "assistant", "content": answer,
                    "tools": tool_log, "usage": usage})
    st.rerun()
