"""Vignette assembly and storage.

The vignette is what a matched therapist reads before a first conversation. It is never shown
to the hero.

One deliberate split: Claude generates only the two fields that require reading the
conversation — `topic_summaries` and `key_signals`. Everything else (session id, timestamp,
zip, community context, crisis flag, turn count) is filled in from state the system already
knows for certain. Asking the model to echo facts it was handed invites quiet drift in exactly
the fields a provider would trust most, and a hallucinated turn count in a clinical handoff is
worse than no turn count.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from .conversation import LISTEN_FOR, MODEL, TOPIC_KEYS, ConversationState

log = logging.getLogger(__name__)

VIGNETTE_MAX_TOKENS = 16000

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "daylight.db"

NOT_COVERED = "Not covered in this conversation."

GENERATION_PROMPT = """You have just completed a mental health intake conversation.

Summarize what the hero shared in each topic area in 2-3 sentences each. Then identify the 3-5 \
most clinically significant signals from the conversation.

Be specific. Use neutral, non-diagnostic language. Quote or closely paraphrase the hero's own \
words where they are more precise than a summary would be.

Rules:
- Do not name a condition or suggest a diagnosis. Describe what was said, not what it means.
- If a topic was never reached, write exactly: "Not covered in this conversation."
- A signal is something a therapist would want to know before the first session. Sleep loss \
with a specific onset is a signal. "Seemed sad" is not.
- Where the hero gave onset, duration, frequency, or impact on daily life, include it — those \
are what a clinician maps to.
- Distinguish "the hero said no" from "this was not asked." Both are useful; conflating them \
is not.

What a clinician would want each topic to have touched:
""" + "\n".join(
    f"{topic}: " + "; ".join(items) for topic, items in LISTEN_FOR.items()
)

#: The model fills these two. The rest of the vignette is assembled from known state.
GENERATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "topic_summaries": {
            "type": "object",
            "properties": {key: {"type": "string"} for key in TOPIC_KEYS},
            "required": list(TOPIC_KEYS),
            "additionalProperties": False,
        },
        # No minItems/maxItems — the API rejects array length constraints in
        # output_config schemas. The 3-5 range is enforced in the prompt instead, and
        # truncated below as a backstop.
        "key_signals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The 3-5 most clinically significant signals. Never more than 5.",
        },
    },
    "required": ["topic_summaries", "key_signals"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _empty_summaries() -> dict[str, str]:
    return {key: NOT_COVERED for key in TOPIC_KEYS}


def _transcript(state: ConversationState) -> str:
    """Flatten history into a labelled transcript.

    Passed as a single user message rather than replaying the history as alternating turns:
    this is an analysis task, not a continuation of the conversation, and framing it as
    replay tempts the model into answering the hero instead of describing them.
    """
    lines = []
    for message in state.history:
        speaker = "HERO" if message["role"] == "user" else "COMPANION"
        lines.append(f"{speaker}: {message['content']}")
    return "\n\n".join(lines)


def generate(
    state: ConversationState, client: anthropic.Anthropic | None = None
) -> dict:
    """Build the vignette for a finished conversation.

    Never raises. If the generation call fails, the vignette is still returned with the
    deterministic fields intact and summaries marked not-covered — a provider sees a thin
    record rather than no record, and the crisis flag in particular always survives.
    """
    client = client or anthropic.Anthropic()

    generated: dict = {"topic_summaries": _empty_summaries(), "key_signals": []}
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=VIGNETTE_MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=GENERATION_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Topics the conversation actually reached: "
                        f"{', '.join(state.topics_covered) or 'none'}\n\n"
                        f"--- transcript ---\n{_transcript(state)}"
                    ),
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": GENERATION_SCHEMA}},
        )
        for block in response.content:
            if getattr(block, "type", None) == "text":
                payload = json.loads(block.text)
                generated["topic_summaries"] = {
                    key: str(payload["topic_summaries"].get(key) or NOT_COVERED)
                    for key in TOPIC_KEYS
                }
                # Backstop for the range the schema can no longer express.
                generated["key_signals"] = [
                    str(s) for s in payload.get("key_signals", [])
                ][:5]
                break
    except (anthropic.APIError, ValueError, KeyError):
        log.exception("vignette generation failed for %s", state.session_id)

    return {
        "session_id": state.session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "zip_code": state.zip_code,
        "community_context": dict(state.community_context),
        "topic_summaries": generated["topic_summaries"],
        "key_signals": generated["key_signals"],
        "crisis_flag": state.crisis_flag,
        "turn_count": state.turn_count,
        # Not in the spec's schema. Added because SPEC.md principle 5 is "visible reasoning
        # for providers" — this is what the deterministic screen saw, including the
        # reflective disclosures it deliberately chose not to escalate.
        "safety_events": [dict(event) for event in state.safety_events],
    }


# ---------------------------------------------------------------------------
# Storage
#
# SQLite, keyed on the session token. The hot fields are stored as real columns so a provider
# queue can be built with a WHERE clause; the full vignette rides along as JSON so the schema
# can grow without a migration in a prototype.
# ---------------------------------------------------------------------------


def db_path() -> Path:
    return Path(os.getenv("DAYLIGHT_DB_PATH") or DEFAULT_DB_PATH)


def _connect(path: Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(path or db_path())
    connection.row_factory = sqlite3.Row
    return connection


def init_db(path: Path | None = None) -> None:
    with _connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vignettes (
                session_id  TEXT PRIMARY KEY,
                timestamp   TEXT NOT NULL,
                zip_code    TEXT,
                crisis_flag INTEGER NOT NULL DEFAULT 0,
                turn_count  INTEGER NOT NULL DEFAULT 0,
                payload     TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_vignettes_crisis "
            "ON vignettes (crisis_flag, timestamp DESC)"
        )


def save(vignette: dict, path: Path | None = None) -> None:
    """Upsert a vignette. Keyed on session token, so a re-close overwrites rather than dupes."""
    init_db(path)
    with _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO vignettes
                (session_id, timestamp, zip_code, crisis_flag, turn_count, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                timestamp   = excluded.timestamp,
                zip_code    = excluded.zip_code,
                crisis_flag = excluded.crisis_flag,
                turn_count  = excluded.turn_count,
                payload     = excluded.payload
            """,
            (
                vignette["session_id"],
                vignette["timestamp"],
                vignette.get("zip_code"),
                int(bool(vignette.get("crisis_flag"))),
                int(vignette.get("turn_count") or 0),
                json.dumps(vignette),
            ),
        )
    log.info("saved vignette %s (crisis=%s)", vignette["session_id"], vignette["crisis_flag"])


def load(session_id: str, path: Path | None = None) -> dict | None:
    init_db(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT payload FROM vignettes WHERE session_id = ?", (session_id,)
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_sessions(limit: int = 50, path: Path | None = None) -> list[dict]:
    """Session index for the Provider View, newest first, crisis sessions surfaced first."""
    init_db(path)
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT session_id, timestamp, zip_code, crisis_flag, turn_count
            FROM vignettes
            ORDER BY crisis_flag DESC, timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
