"""Daylight — Streamlit frontend.

Two tabs: the hero's conversation, and the therapist-facing Provider View.

The hero's journey is a small state machine held in `st.session_state.stage`:

    welcome -> conversation -> closed
                    |
                    +-------> crisis -> (back to conversation, or closed)

Streamlit reruns the whole script on every interaction, so all real state lives in
`st.session_state` and nothing expensive runs outside a cache.
"""

from __future__ import annotations

import logging

import streamlit as st
from dotenv import load_dotenv

from src import enrichment, safety, vignette
from src.conversation import (
    OPENING_MESSAGE,
    TOPIC_LABELS,
    ConversationEngine,
    ConversationState,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Daylight", page_icon="🌤️", layout="centered")

HERO_AVATAR = "🫂"
COMPANION_AVATAR = "🌤️"

#: SPEC.md -> End Screen. 988 always first.
RESOURCES = [
    (safety.CRISIS_LINE_NAME, safety.CRISIS_LINE_URL, safety.CRISIS_LINE_TEXT),
    ("NAMI — National Alliance on Mental Illness", "https://nami.org",
     "Education, local support groups, and a helpline"),
    ("Depression and Bipolar Support Alliance", "https://dbsalliance.org",
     "Peer support groups, online and in person"),
    ("Mental Health America", "https://mhanational.org",
     "Screening tools and a directory of local affiliates"),
]

DISCLAIMER = """
**Before we start, a few things that are true:**

- This is **not** therapy, and I'm not a doctor. Nothing here is a diagnosis.
- You are **anonymous**. I won't ask your name, and there's no account to create.
- Nothing you say goes to another person unless you choose to send it.
- If you are in danger right now, please use the **988** line in the sidebar — it's staffed
  by people, all day and all night.

This is just a conversation about how you've been feeling. You can stop at any point.
"""

THERAPIST_HANDOFF = """
**This part isn't built yet.**

In a finished version of Daylight, this button would hand your vignette — the picture this
conversation built, still with no name attached — to a therapist who works with what you
described, and they'd reach out to you.

Until that exists, the fastest real path to a therapist who takes your insurance is
**[Headway](https://headway.co)**. Most people there are booked within a week.

You don't have to do it today. But you've already done the hard part once.
"""


# ---------------------------------------------------------------------------
# Session bootstrap
# ---------------------------------------------------------------------------


def bootstrap() -> None:
    """Create the session's state exactly once per browser session."""
    if "stage" not in st.session_state:
        st.session_state.stage = "welcome"
        st.session_state.state = ConversationState()
        st.session_state.engine = None       # built lazily — needs an API key
        st.session_state.closing = None      # {"closing_message", "not_alone"}
        st.session_state.vignette = None
        st.session_state.resumed_from_crisis = False


def engine() -> ConversationEngine:
    """The conversation engine, built on first use so the welcome screen never needs a key."""
    if st.session_state.engine is None:
        st.session_state.engine = ConversationEngine(state=st.session_state.state)
        st.session_state.state = st.session_state.engine.state
    return st.session_state.engine


# ---------------------------------------------------------------------------
# Sidebar — 988 is visible in every stage, per spec
# ---------------------------------------------------------------------------


def render_sidebar() -> None:
    with st.sidebar:
        st.error(safety.crisis_resources_markdown())
        st.divider()
        st.caption("Your session")
        st.code(st.session_state.state.session_id, language=None)
        st.caption(
            "This code is how a therapist would find what you shared. "
            "It isn't tied to your name, and it isn't tied to you."
        )
        if st.session_state.stage in {"conversation", "crisis"}:
            state = st.session_state.state
            covered = len(state.topics_covered)
            remaining = state.remaining_topics()
            st.divider()
            # A bare "3 of 7" told the hero nothing — it read as a form with no form. Naming
            # the area they're in, and what is still ahead, is what makes the arc legible.
            st.caption("Where we are")
            st.progress(covered / 7, text=TOPIC_LABELS[state.current_topic])
            if remaining:
                nxt = [t for t in remaining if t != state.current_topic]
                st.caption(
                    "Still ahead: " + ", ".join(TOPIC_LABELS[t] for t in nxt)
                    if nxt else "Last one."
                )


# ---------------------------------------------------------------------------
# Stage: welcome
# ---------------------------------------------------------------------------


def render_welcome() -> None:
    st.title("🌤️ Daylight")
    st.subheader("A private conversation about how you've been feeling")
    st.markdown(DISCLAIMER)

    # Absorbs the one slow read of the County Health Rankings release while the hero is
    # reading the disclaimer, so the zip lookup mid-conversation is instant.
    with st.spinner("", show_time=False):
        enrichment.warm_cache()

    if st.button("I'm ready to start", type="primary", width="stretch"):
        st.session_state.stage = "conversation"
        engine()
        st.rerun()


# ---------------------------------------------------------------------------
# Stage: conversation
# ---------------------------------------------------------------------------


def render_history() -> None:
    for message in st.session_state.state.history:
        is_hero = message["role"] == "user"
        with st.chat_message(
            "user" if is_hero else "assistant",
            avatar=HERO_AVATAR if is_hero else COMPANION_AVATAR,
        ):
            st.markdown(message["content"])


def render_conversation() -> None:
    # st.chat_input only pins to the viewport bottom in the main body; inside a tab it
    # renders inline, so this turn's messages would land *below* the input box until the
    # rerun caught up. Reserving the transcript slot first keeps the order right mid-turn.
    transcript = st.container()
    with transcript:
        render_history()

        if st.session_state.resumed_from_crisis:
            st.info("We can pick up wherever you want. 988 is still there in the sidebar.")
            st.session_state.resumed_from_crisis = False

    hero_message = st.chat_input(
        "Say as much or as little as you want…",
        # A second message sent mid-turn would be answered against stale state, and the
        # hero would watch two replies race. Hold the input until this turn lands.
        submit_mode="disable",
    )
    if not hero_message:
        return

    with transcript:
        with st.chat_message("user", avatar=HERO_AVATAR):
            st.markdown(hero_message)

        with st.chat_message("assistant", avatar=COMPANION_AVATAR):
            with st.spinner("Reflecting…"):
                result = engine().respond(hero_message)
            st.markdown(result.message)

    if result.crisis_mode:
        st.session_state.stage = "crisis"
    elif result.should_close:
        close_conversation()
    st.rerun()


# ---------------------------------------------------------------------------
# Stage: crisis
#
# SPEC.md -> Mode shift behavior. The conversation stops, 988 goes front and centre, and the
# hero chooses whether to continue. Nothing here is generated by the model.
# ---------------------------------------------------------------------------


def render_crisis() -> None:
    render_history()
    st.divider()
    st.error(f"### {safety.MODE_SHIFT_MESSAGE}")
    st.markdown(
        f"**{safety.CRISIS_LINE_NAME}**\n\n"
        f"# {safety.CRISIS_LINE_TEXT}\n\n"
        f"Free, confidential, and answered by a person 24/7. "
        f"You can also chat at [{safety.CRISIS_LINE_URL}]({safety.CRISIS_LINE_URL}).\n\n"
        "If you are in immediate danger, please call **911**."
    )
    st.divider()

    left, right = st.columns(2)
    if left.button("I'd like to keep talking", width="stretch"):
        st.session_state.stage = "conversation"
        st.session_state.resumed_from_crisis = True
        st.rerun()
    if right.button("I'm going to close this now", type="primary", width="stretch"):
        close_conversation()
        st.rerun()


# ---------------------------------------------------------------------------
# Stage: closed
# ---------------------------------------------------------------------------


def close_conversation() -> None:
    """Generate the closing copy, build and store the vignette, move to the end screen.

    Runs once — the vignette is the record a therapist would receive, and regenerating it on
    a Streamlit rerun would both cost a call and produce a second version of the truth.
    """
    state = st.session_state.state

    if st.session_state.closing is None and not state.crisis_flag:
        with st.spinner("Reflecting on what you just shared…"):
            st.session_state.closing = engine().closing()

    # The vignette is a 16k-token call the hero never sees — it is written for the therapist.
    # Making them watch it generate before their own ending put a blank half-minute at the
    # most loaded moment of the experience. It is built in render_closed instead, after the
    # closing copy is already on screen.
    st.session_state.stage = "closed"


def render_closed() -> None:
    state = st.session_state.state

    if state.crisis_flag:
        # The crisis close has its own shape — no warm generated close, no "you are not
        # alone" flourish, just the line and the door.
        st.title("Please reach out")
        st.error(
            f"**{safety.CRISIS_LINE_NAME}** — # {safety.CRISIS_LINE_TEXT}\n\n"
            "Talking to a person right now matters more than anything else on this screen."
        )
    else:
        closing = st.session_state.closing or {}
        st.title("Thank you for being here")
        st.markdown(closing.get("closing_message", ""))
        if closing.get("not_alone"):
            st.info(closing["not_alone"])

    st.divider()
    st.subheader("Where to go from here")
    for name, url, blurb in RESOURCES:
        st.markdown(f"**[{name}]({url})** — {blurb}")

    st.divider()
    if st.button("Talk to someone", type="primary", width="stretch"):
        st.session_state.show_handoff = True
    if st.session_state.get("show_handoff"):
        # In production this is where the matching flow would be triggered with the stored
        # vignette; the session token is already the key it would be handed.
        st.markdown(THERAPIST_HANDOFF)

    st.caption(f"Your session code: `{state.session_id}`")

    # Deliberately the last thing in the script. The vignette is a 16k-token call written
    # for the therapist, not the hero, so every hero-facing element above paints first and
    # the wait lands on a page that already looks finished. It also resolves the promise the
    # opening message makes — that what they shared becomes something they can hand over.
    if st.session_state.vignette is None:
        with st.spinner("Putting together what you shared…"):
            record = vignette.generate(state)
        try:
            vignette.save(record)
        except Exception:
            # A storage failure must not cost the hero their end screen.
            logging.exception("could not save vignette %s", record["session_id"])
        st.session_state.vignette = record


# ---------------------------------------------------------------------------
# Provider View
# ---------------------------------------------------------------------------


def render_provider_view() -> None:
    st.subheader("Provider View")
    st.caption(
        "What a matched therapist receives before a first conversation. "
        "The hero never sees this screen."
    )

    try:
        sessions = vignette.list_sessions()
    except Exception:
        st.warning("No session store available yet.")
        return

    if not sessions:
        st.info("No completed sessions yet. Finish a conversation to generate a vignette.")
        return

    def label(row: dict) -> str:
        flag = "⚠️ " if row["crisis_flag"] else ""
        return f"{flag}{row['session_id'][:8]}… · {row['timestamp'][:16]} · {row['turn_count']} turns"

    chosen = st.selectbox("Session", sessions, format_func=label)
    record = vignette.load(chosen["session_id"])
    if not record:
        st.warning("Vignette could not be loaded.")
        return

    if record.get("crisis_flag"):
        st.error(
            "**Crisis flag raised during this session.** The conversation was interrupted "
            "and 988 was surfaced. Treat this handoff as time-sensitive."
        )

    left, right = st.columns(2)
    left.metric("Session", record["session_id"][:8] + "…")
    right.metric("Turns", record["turn_count"])
    st.caption(f"Completed {record['timestamp']}")

    context = record.get("community_context") or {}
    with st.container(border=True):
        st.markdown("**Community context**")
        if context.get("county"):
            st.markdown(
                f"{context['county']}, {context['state']} "
                f"(zip {record.get('zip_code') or '—'})"
            )
            columns = st.columns(3)
            columns[0].metric("Frequent mental distress",
                              f"{context.get('pct_frequent_distress') or '—'}%")
            columns[1].metric("MH providers / 100k",
                              context.get("mh_providers_per_100k") or "—")
            columns[2].metric("Context", context.get("context_type", "default"))
        else:
            st.caption("No community data — the hero didn't share a zip, or it wasn't found.")

    st.markdown("### Key signals")
    signals = record.get("key_signals") or []
    if signals:
        for signal in signals:
            st.markdown(f"- {signal}")
    else:
        st.caption("No signals extracted.")

    st.markdown("### Topic summaries")
    for key, summary in (record.get("topic_summaries") or {}).items():
        with st.expander(key.replace("_", " ").title(), expanded=True):
            st.write(summary)

    events = record.get("safety_events") or []
    if events:
        st.markdown("### What the safety screen saw")
        st.caption(
            "Included so the escalation decision is reviewable. A `reflective` trigger means "
            "difficult-thought language was present and deliberately held, not escalated."
        )
        st.dataframe(
            [
                {
                    "turn": event["turn"],
                    "escalated": event["escalate"],
                    "triggers": ", ".join(event["triggers"]),
                    "evidence": " | ".join(event["evidence"])[:120],
                }
                for event in events
            ],
            width="stretch",
            hide_index=True,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    bootstrap()
    render_sidebar()

    hero_tab, provider_tab = st.tabs(["Daylight", "Provider View"])

    with hero_tab:
        stage = st.session_state.stage
        if stage == "welcome":
            render_welcome()
        elif stage == "conversation":
            render_conversation()
        elif stage == "crisis":
            render_crisis()
        else:
            render_closed()

    with provider_tab:
        render_provider_view()


main()
