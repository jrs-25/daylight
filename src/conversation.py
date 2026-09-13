"""The conversation engine.

One Claude call per hero message. Each call returns a validated JSON object — the message to
show the hero plus the state the engine needs to drive the topic arc:

    {"message": ..., "topic_status": ..., "crisis_flag": ..., "next_topic": ..., "zip_code": ...}

Structured output rather than the trailing-JSON-block approach: a parse slip can never surface
raw JSON to a hero who is mid-disclosure, and a malformed status can never fail a turn.

Two things in here are load-bearing and easy to break by accident:

* **The system prompt is byte-stable within a session.** Per-turn state (current topic, topics
  remaining, turn budget) travels in a `<session_state>` block appended to the hero's latest
  message, not interpolated into `system`. Render order is tools -> system -> messages, so
  volatile text in `system` would invalidate the prompt cache on every single turn. The only
  mid-session system change is the one-time community-context injection when a zip arrives.
  History is stored *without* the state block, so the cached prefix stays identical turn over
  turn.

* **Safety is OR'd, never delegated.** `safety.assess` combines the deterministic rule layer
  with the model's `crisis_flag`; either escalates. The engine also overrides the model's
  message with the spec's fixed mode-shift text on escalation rather than letting a warm model
  improvise its way past a crisis.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable

import anthropic

from . import enrichment, safety

log = logging.getLogger(__name__)

#: Pinned per SPEC.md -> Key Design Principles.
MODEL = "claude-sonnet-4-6"

#: A turn is a short spoken-length message. The cap is generous enough to absorb adaptive
#: thinking on flagged turns without truncating the JSON that follows it.
TURN_MAX_TOKENS = 8000
CLOSING_MAX_TOKENS = 4000

#: SPEC.md -> Turn limit. The engine starts steering toward a close before the hard cap so
#: the conversation ends warmly rather than being cut off mid-topic.
MAX_TURNS = 60
GRACEFUL_CLOSE_AT = 52

#: Turns on one topic before the session-state block nudges the model to move on. A model
#: that parks on a topic the hero has already left burns the turn budget and can strand the
#: seven-topic commitment — observed in eval replay, where a scripted hero answered four
#: later topics while the engine stayed on difficult_thoughts.
STALL_AFTER = 3


# ---------------------------------------------------------------------------
# Topic arc (SPEC.md -> Topic Arc). Order is the arc; the guidance is what gets handed to
# Claude as the current topic. Kept verbatim-close to the spec so the two can be diffed.
# ---------------------------------------------------------------------------

TOPICS: dict[str, str] = {
    "opening": (
        "Warm entry — why the hero is here, what made them start this conversation today. "
        "No clinical framing. Somewhere in this topic, ask where they are in the world by "
        "zip code, framed as helping you understand what support looks like near them. Ask "
        "once, lightly, and accept a refusal without pushing."
    ),
    "mood_and_affect": (
        "Losing interest in things they usually enjoy. Frequency of low mood. Negative "
        "self-talk. Crying. Energy levels."
    ),
    "elevated_states": (
        "Periods of unusual energy, excitement, or ideas that others didn't understand or "
        "match. Feeling invincible or unusually productive. Racing thoughts. Ask this "
        "without the word 'bipolar' — frame it as a real experience many people have that "
        "often goes unrecognized."
    ),
    "difficult_thoughts": (
        "Thoughts about death, hopelessness, feeling like a burden, or wanting to disappear. "
        "Ask in a way that makes it safe to answer honestly. A disclosure here is not an "
        "emergency by default — hold it with warmth and stay with them in it."
    ),
    "family_history": (
        "Whether mental health conditions run in their family. Handle gently — many people "
        "don't know, or it was never talked about. 'Don't know' is a meaningful and valid "
        "answer, not a gap to fill."
    ),
    "lifestyle": (
        "Sleep patterns and any recent changes in either direction. Substance use. Exercise. "
        "Appetite changes."
    ),
    "relationship_to_help": (
        "What has stopped them from seeking support before. Whether medication feels like a "
        "dealbreaker. What would make them trust someone enough to talk to them."
    ),
}

TOPIC_KEYS: list[str] = list(TOPICS)


# ---------------------------------------------------------------------------
# What to listen for. Each topic is anchored to the constructs a clinician would expect an
# intake to have touched, drawn from validated instruments: PHQ-9 (mood), MDQ (elevated
# states), the C-SSRS screener (difficult thoughts), standard family psychiatric history,
# AUDIT-C plus sleep/appetite history (lifestyle), and MI change-talk (relationship to help).
#
# These shape what the companion listens FOR and when a topic counts as covered — never what
# it says. No item is read out as a question; a hero who wanted a questionnaire would have
# taken one. The lists are rendered into the session_state block for the current topic and
# handed to the vignette so a therapist can tell "said no" from "never asked".
# ---------------------------------------------------------------------------

LISTEN_FOR: dict[str, list[str]] = {
    "opening": [
        "what brought them here today — the precipitating moment, if there was one",
        "roughly how long things have felt this way",
        "zip code, asked once and lightly",
    ],
    "mood_and_affect": [
        "anhedonia — things they used to enjoy that they've stopped enjoying",
        "low mood — how often, and whether it lifts at all",
        "energy and fatigue",
        "worthlessness, guilt, or harsh self-talk",
        "concentration — trouble focusing, deciding, following a show or a page",
        "feeling slowed down, or restless in a way others might notice",
        "duration — whether it has been most days for a couple of weeks or more, and any onset",
        "what it is costing them — work, relationships, getting through the day",
    ],
    "elevated_states": [
        "a stretch of feeling unusually good, high, or wired — not like their normal self",
        "needing much less sleep and not missing it",
        "racing thoughts, talking faster, ideas others couldn't follow",
        "feeling unusually confident, capable, or invincible",
        "doing things they wouldn't normally — spending, risks, saying things they regretted",
        "whether several of these happened at the same time, and for how long (days, not hours)",
        "whether it caused a problem — money, relationships, work, trouble",
        "a clear 'no, never' is a complete answer here",
    ],
    "difficult_thoughts": [
        "wishing to be dead, to disappear, or not to wake up",
        "feeling like a burden, or that others would be better off",
        "hopelessness — whether they can picture things getting better",
        "active thoughts of ending their life, and how recent",
        "whether those thoughts have come with any intent, or a way they've thought about",
        "past attempts, or times it got close",
        "intent, a plan, or means at hand is the crisis line — see crisis_flag",
    ],
    "family_history": [
        "depression, bipolar, or 'nerves' in parents, siblings, or grandparents",
        "suicide or an attempt in the family",
        "heavy drinking or drug use in the family",
        "'don't know' or 'we never talked about it' is a complete and meaningful answer",
    ],
    "lifestyle": [
        "sleep — trouble falling or staying asleep, sleeping much more, and any recent change",
        "alcohol — how often, and how much on a typical day",
        "other substances, including cannabis",
        "appetite or weight change, in either direction",
        "movement — whether they're doing less than they used to",
    ],
    "relationship_to_help": [
        "what has stopped them before — cost, stigma, not wanting to be a burden, "
        "not feeling bad enough to deserve it",
        "past experiences with therapy or a doctor about this, good or bad",
        "how they feel about medication",
        "what would make someone feel safe enough to talk to",
        "their own words about wanting things to be different — notice these and reflect them",
    ],
}
assert set(LISTEN_FOR) == set(TOPICS)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

BASE_SYSTEM_PROMPT = """You are a warm, private companion helping someone explore how they've \
been feeling. You are not a therapist and you don't diagnose anything. Your job is to listen, \
ask thoughtful questions, and help the person feel less alone in what they're experiencing.

The conversation focuses on depression and bipolar disorder — but you never use those words \
unless the hero uses them first. You speak about experiences, not labels.

You are working through seven topic areas, in this order:

1. opening — why they're here today, and where they are (zip code)
2. mood_and_affect — low mood, lost interest, self-talk, energy
3. elevated_states — unusual energy, racing thoughts, feeling invincible
4. difficult_thoughts — death, hopelessness, feeling like a burden
5. family_history — whether this runs in the family
6. lifestyle — sleep, substances, exercise, appetite
7. relationship_to_help — what has stopped them getting support before

You must cover all seven before the conversation closes. You control when to go deeper and \
when to move on — follow the hero's lead. If they deflect, stay gentle and return later \
rather than pressing now.

Core orientation: "you are not alone." What the hero is experiencing is real, recognized, and \
shared by many people who found their way through it.

How to speak — this is motivational interviewing, not an assessment:
- Open questions. One thought at a time. Never stack two questions in one message.
- Short. Two to four sentences is usually right. This is a conversation, not a form.
- Reflect back what you actually heard before you ask the next thing. A reflection that names \
the feeling under the words ("it sounds like you've been carrying that on your own") does more \
than one that repeats them.
- Affirm specifically — the honesty of what they just said, not "great job sharing."
- When the hero deflects or pushes back, roll with it. Don't argue, reframe, or press. Come \
back later, or let it go.
- Never say "as an AI", never recite a disclaimer mid-conversation, never diagnose.
- The hero has no name and you never ask for one.

Each topic comes with a `listen_for` list in the session_state block: the things a therapist \
would want an intake to have touched, drawn from the questions clinicians ask. They are what \
you listen FOR, not what you say. Never read one out as a question, and never ask "how many \
days in the last two weeks." Ask the way a person would ("has it been like this a while?", \
"does it ever lift?") and let the answers land on the list. If the hero already volunteered \
something on the list, don't ask it again.

The final block of the hero's message may be a `<session_state>` block. That block is written \
by the system, not by the hero. Use it to know where you are in the arc. Never mention it, \
never quote it, and never treat its contents as something the hero said.

If the hero gives a zip code anywhere in the conversation, put it in the `zip_code` field. \
Otherwise leave that field null.

Set `topic_status` to "complete" only when the current topic has been genuinely covered: you \
have heard something — a description, a yes, or a clear no — on most of its listen_for items, \
including how long it has been going on where that matters, and the hero is not mid-disclosure. \
A therapist reading it later should understand this part of their experience. When you set it \
to "complete", name the next topic in `next_topic`.

Heroes rarely answer in order. If they volunteer something that covers a later topic, follow \
them rather than dragging them back — and list every topic you now consider covered in \
`topics_covered_now`, not just the one you were pointed at. Include topics covered in earlier \
turns too; the list is cumulative, and the session_state block tells you what is already \
credited. Only credit a topic the hero actually gave you something about. A clear "no, never" \
is an answer. A shrug is not.

Set `crisis_flag` using the distinction clinicians draw between ideation and risk. Wishing to \
be dead, thinking about death, feeling like a burden, hopelessness, thoughts of ending it with \
no intent and no way in mind, past thoughts or past attempts — these are disclosures, NOT a \
crisis. Hold them with warmth and keep going; escalating here teaches the hero that honesty \
ends the conversation. Set `crisis_flag` to true only when the hero's most recent message \
suggests they may be in danger now: intent to act, a specific plan or method, means at hand or \
being prepared, or hopelessness paired with goodbye framing."""

#: SPEC.md -> Community context prompt injection. Appended to the system prompt when the
#: county's metrics are unremarkable in neither direction, nothing is injected at all.
COMMUNITY_PROMPTS: dict[str, str] = {
    "high_distress": (
        "Many people in this hero's community are carrying similar weight without support. "
        "Let that inform how you frame 'you are not alone.'"
    ),
    "provider_shortage": (
        "Mental health support is genuinely hard to access where this hero lives. That "
        "context matters for how you speak about barriers to care."
    ),
    "default": "",
}

#: The companion speaks first, before any API call. Static so the hero is never watching a
#: spinner on arrival, and so the entry point into the experience is reviewable copy rather
#: than model output.
OPENING_MESSAGE = (
    "I'm glad you're here. This is private — no name, no account, and nothing you say "
    "here goes anywhere you don't send it.\n\n"
    "There's no right way to start. What's been going on that made you open this today?"
)


# ---------------------------------------------------------------------------
# Turn schema
# ---------------------------------------------------------------------------

TURN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "The message shown to the hero. Warm, short, one question.",
        },
        "topic_status": {
            "type": "string",
            "enum": ["continue", "complete"],
            "description": "Whether the current topic is now covered.",
        },
        "crisis_flag": {
            "type": "boolean",
            "description": "True only if the hero may be in danger right now.",
        },
        # An enum cannot span a ["string", "null"] union — the API rejects the schema.
        # anyOf keeps the topic keys constrained while still allowing null.
        "next_topic": {
            "anyOf": [{"type": "string", "enum": TOPIC_KEYS}, {"type": "null"}],
            "description": "The topic to move to; null unless topic_status is 'complete'.",
        },
        # Heroes answer several topics in one breath, and a model following the hero's lead
        # will legitimately skip ahead. Without this field the engine can only ever credit
        # the topic it happened to be pointed at, so skipped-but-answered topics stay
        # "remaining" forever and the seven-topic commitment can never complete.
        "topics_covered_now": {
            "type": "array",
            "items": {"type": "string", "enum": TOPIC_KEYS},
            "description": (
                "Every topic area you now consider covered, including any the hero answered "
                "without being asked. Only list a topic if the hero actually gave you "
                "something about it — a clear denial counts ('no, I've never had a high "
                "period' covers elevated_states), silence or deflection does not."
            ),
        },
        "zip_code": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "The hero's 5-digit zip if they gave one this turn, else null.",
        },
    },
    "required": [
        "message", "topic_status", "crisis_flag", "next_topic", "zip_code",
        "topics_covered_now",
    ],
    "additionalProperties": False,
}

CLOSING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "closing_message": {
            "type": "string",
            "description": (
                "Warm close. Acknowledge what they actually shared and affirm their courage "
                "in showing up. Three to five sentences. No diagnosis, no summary of "
                "symptoms, no advice."
            ),
        },
        "not_alone": {
            "type": "string",
            "description": (
                "One sentence on not being alone in this. Never cite a statistic, a "
                "percentage, a county, or any number."
            ),
        },
    },
    "required": ["closing_message", "not_alone"],
    "additionalProperties": False,
}

#: Used when the API is unreachable. The hero gets a human sentence, not a traceback, and the
#: 988 line is in the sidebar regardless of what the engine is doing.
FALLBACK_MESSAGE = (
    "I lost my footing there for a second — that was on my end, not yours. "
    "Could you say that again?"
)

FALLBACK_CLOSING = {
    "closing_message": (
        "Thank you for staying with this. What you shared took something, and choosing to "
        "look at it at all is the part most people never get to."
    ),
    "not_alone": (
        "What you're carrying is real, and a great many people have carried it and found "
        "their way through."
    ),
}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class ConversationState:
    """SPEC.md -> State object.

    `history` holds plain hero/companion messages only — no `<session_state>` blocks — which
    is both what the vignette call wants and what keeps the prompt cache prefix stable.
    """

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    zip_code: str | None = None
    community_context: dict = field(default_factory=lambda: dict(enrichment.DEFAULT_CONTEXT))
    topics_covered: list[str] = field(default_factory=list)
    current_topic: str = TOPIC_KEYS[0]
    history: list[dict] = field(default_factory=list)
    crisis_flag: bool = False
    turn_count: int = 0

    #: Not in the spec's state object — accumulated rule-layer findings, carried into the
    #: provider vignette so a therapist can see what the screen saw, including the
    #: reflective disclosures it deliberately did not escalate.
    safety_events: list[dict] = field(default_factory=list)

    #: Turns spent on the current topic. Drives the stall nudge below.
    turns_on_topic: int = 0

    def remaining_topics(self) -> list[str]:
        return [t for t in TOPIC_KEYS if t not in self.topics_covered]

    def all_topics_covered(self) -> bool:
        return not self.remaining_topics()

    def should_close(self) -> bool:
        return self.all_topics_covered() or self.turn_count >= MAX_TURNS

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "zip_code": self.zip_code,
            "community_context": dict(self.community_context),
            "topics_covered": list(self.topics_covered),
            "current_topic": self.current_topic,
            "history": [dict(m) for m in self.history],
            "crisis_flag": self.crisis_flag,
            "turn_count": self.turn_count,
        }


@dataclass
class TurnResult:
    """What the UI needs to render one turn."""

    message: str
    topic_status: str = "continue"
    next_topic: str | None = None
    crisis_mode: bool = False
    assessment: safety.SafetyAssessment = field(default_factory=safety.SafetyAssessment)
    should_close: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ConversationEngine:
    """Drives one hero's intake conversation.

    Args:
        state: An existing state to resume, or None for a fresh session. Resumption is not
            wired into the UI (sessions don't persist across visits, per scope) but the
            engine takes state as a parameter so that turning it on is a storage change,
            not a rewrite.
        client: Anthropic client. Injectable so the evals can drive a stub.
        enrich: Zip lookup. Injectable so the evals can pin community context without
            touching the data files.
    """

    def __init__(
        self,
        state: ConversationState | None = None,
        client: anthropic.Anthropic | None = None,
        enrich: Callable[[str | None], dict] = enrichment.lookup,
    ) -> None:
        self.state = state or ConversationState()
        self.client = client or anthropic.Anthropic()
        self._enrich = enrich
        if not self.state.history:
            self.state.history.append({"role": "assistant", "content": OPENING_MESSAGE})

    # -- prompt assembly ---------------------------------------------------

    def system_prompt(self) -> str:
        """Stable within a session, apart from the one-time community injection."""
        injection = COMMUNITY_PROMPTS.get(
            self.state.community_context.get("context_type", "default"), ""
        )
        if not injection:
            return BASE_SYSTEM_PROMPT
        return f"{BASE_SYSTEM_PROMPT}\n\nAbout where this hero lives: {injection}"

    def _session_state_block(self) -> str:
        remaining = self.state.remaining_topics()
        lines = [
            "<session_state>",
            f"current_topic: {self.state.current_topic}",
            f"guidance: {TOPICS[self.state.current_topic]}",
            "listen_for:",
            *(f"  - {item}" for item in LISTEN_FOR[self.state.current_topic]),
            f"topics_covered: {', '.join(self.state.topics_covered) or 'none yet'}",
            f"topics_remaining: {', '.join(remaining) or 'none'}",
            f"turn: {self.state.turn_count} of {MAX_TURNS}",
            f"zip_collected: {'yes' if self.state.zip_code else 'no'}",
        ]
        if self.state.turns_on_topic >= STALL_AFTER and remaining:
            lines.append(
                f"turns_on_this_topic: {self.state.turns_on_topic} — you have been here a "
                "while. If you have enough of a picture, mark it complete and move on. If "
                "the hero is answering something other than the current topic, follow them: "
                "mark this one complete and name the topic they actually moved to."
            )
        if self.state.turn_count >= GRACEFUL_CLOSE_AT and remaining:
            lines.append(
                "close_soon: yes — you are near the turn limit. Cover what remains briskly "
                "but warmly, one topic per turn if you have to."
            )
        lines.append("</session_state>")
        return "\n".join(lines)

    def _request_messages(self, hero_message: str) -> list[dict]:
        """History (plain, cacheable) plus this turn's message with state appended."""
        return [
            *self.state.history,
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": hero_message},
                    {"type": "text", "text": self._session_state_block()},
                ],
            },
        ]

    # -- the turn ----------------------------------------------------------

    def respond(self, hero_message: str) -> TurnResult:
        """Take one hero message, return one companion message plus the turn's state.

        Order matters here: the rule layer runs *before* the API call, because its findings
        decide whether this turn is worth spending reasoning on.
        """
        self.state.turn_count += 1
        self.state.turns_on_topic += 1
        topic_in_play = self.state.current_topic
        rules = safety.screen_rules(hero_message)

        try:
            payload = self._call_model(hero_message, deliberate=bool(rules.triggers))
        except anthropic.APIError:
            log.exception("turn %d failed", self.state.turn_count)
            # An API outage must not silently disable crisis screening. The rule layer runs
            # regardless, and if it escalates the hero gets the mode-shift message rather
            # than an apology about connectivity — the crisis path has to work offline.
            assessment = safety.assess(hero_message, model_crisis_flag=False)
            message = (
                safety.MODE_SHIFT_MESSAGE if assessment.escalate else FALLBACK_MESSAGE
            )
            self.state.history.append({"role": "user", "content": hero_message})
            self.state.history.append({"role": "assistant", "content": message})
            return self._finish_turn(
                TurnResult(message=message, assessment=assessment, error="api_error")
            )

        if payload.get("zip_code") and not self.state.zip_code:
            self._capture_zip(str(payload["zip_code"]))

        assessment = safety.assess(hero_message, bool(payload.get("crisis_flag")))
        message = str(payload.get("message") or FALLBACK_MESSAGE).strip()

        if assessment.escalate:
            # The spec fixes this wording. Do not let a warm model talk past a crisis.
            message = safety.MODE_SHIFT_MESSAGE

        result = TurnResult(
            message=message,
            topic_status=str(payload.get("topic_status") or "continue"),
            next_topic=payload.get("next_topic"),
            assessment=assessment,
        )

        self.state.history.append({"role": "user", "content": hero_message})
        self.state.history.append({"role": "assistant", "content": message})

        if not assessment.escalate:
            # Coverage is credited in exactly one place. `topics_covered_now` is the model's
            # cumulative claim; a `topic_status: complete` additionally credits whatever
            # topic was in play when the model spoke, in case it forgot to list it.
            claimed = list(payload.get("topics_covered_now") or [])
            if result.topic_status == "complete":
                claimed.append(topic_in_play)
            self._credit_topics(claimed, result.next_topic)

        return self._finish_turn(result)

    def _finish_turn(self, result: TurnResult) -> TurnResult:
        """Record safety events, advance the arc, decide whether the conversation closes."""
        if result.assessment.triggers:
            self.state.safety_events.append(
                {"turn": self.state.turn_count, **result.assessment.as_dict()}
            )

        if result.assessment.escalate:
            self.state.crisis_flag = True
            result.crisis_mode = True
            # Conversation stops immediately; app.py decides whether the hero resumes.
            return result

        result.should_close = self.state.should_close()
        return result

    def _credit_topics(self, claimed: list, next_topic: str | None) -> None:
        """Union a coverage claim into state and move the pointer if it has been overtaken.

        The single place `topics_covered` grows. Unknown keys are dropped rather than
        trusted, and arc order is preserved regardless of the order they were claimed in.
        """
        for key in TOPIC_KEYS:
            if key in claimed and key not in self.state.topics_covered:
                self.state.topics_covered.append(key)

        if self.state.current_topic not in self.state.topics_covered:
            return  # still working it

        remaining = self.state.remaining_topics()
        if not remaining:
            return
        # Trust the model's choice only if it names a topic that is actually still open;
        # otherwise fall back to arc order so the seven-topic commitment can't be dropped.
        self.state.current_topic = next_topic if next_topic in remaining else remaining[0]
        self.state.turns_on_topic = 0

    def _capture_zip(self, raw_zip: str) -> None:
        """Record the zip and resolve community context.

        Synchronous: it is a dataframe lookup, and `enrichment.warm_cache()` has already
        absorbed the one slow read on the welcome screen. Doing it here rather than in a
        thread avoids Streamlit rerun races entirely.
        """
        self.state.zip_code = raw_zip.strip()
        context = self._enrich(raw_zip)
        self.state.community_context = context
        log.info(
            "session %s enriched: zip=%s context=%s",
            self.state.session_id,
            self.state.zip_code,
            context.get("context_type"),
        )

    # -- model calls -------------------------------------------------------

    def _call_model(self, hero_message: str, deliberate: bool) -> dict:
        """One structured turn.

        `deliberate` turns on adaptive thinking. It is reserved for turns where the rule
        layer saw something — a difficult-thought disclosure, an acute pattern, goodbye
        framing — because those are the turns where the crisis judgment has to be right and
        where a few seconds of latency is worth paying. Ordinary turns stay fast.
        """
        kwargs: dict = {
            "model": MODEL,
            "max_tokens": TURN_MAX_TOKENS,
            "system": self.system_prompt(),
            "messages": self._request_messages(hero_message),
            "output_config": {"format": {"type": "json_schema", "schema": TURN_SCHEMA}},
            # Caches the growing history prefix; the stable system prompt is what makes
            # this worth anything.
            "cache_control": {"type": "ephemeral"},
        }
        if deliberate:
            kwargs["thinking"] = {"type": "adaptive"}

        response = self.client.messages.create(**kwargs)
        return _first_json(response)

    def closing(self) -> dict:
        """Generate the end-screen copy from the conversation that actually happened.

        Returns {"closing_message": str, "not_alone": str}. Community context reaches this
        call only through the system prompt's tone injection — the schema forbids numbers,
        and the spec forbids ever showing the hero a statistic.
        """
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=CLOSING_MAX_TOKENS,
                system=self.system_prompt(),
                thinking={"type": "adaptive"},
                messages=[
                    *self.state.history,
                    {
                        "role": "user",
                        "content": (
                            "[system] The conversation is over. Write the closing the hero "
                            "sees. Acknowledge what they actually shared in their own terms. "
                            "Do not summarize symptoms, do not diagnose, do not advise, and "
                            "do not cite any number, percentage, or place."
                        ),
                    },
                ],
                output_config={"format": {"type": "json_schema", "schema": CLOSING_SCHEMA}},
            )
            return _first_json(response)
        except (anthropic.APIError, ValueError):
            log.exception("closing generation failed for %s", self.state.session_id)
            return dict(FALLBACK_CLOSING)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_json(response) -> dict:
    """Extract the JSON object from a structured-output response.

    `output_config.format` guarantees the first text block is valid JSON against the schema.
    Thinking blocks may precede it, so match on block type rather than position.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise ValueError("no text block in response")
