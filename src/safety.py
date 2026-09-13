"""Crisis detection.

The whole point of this module is the distinction the spec draws: a hero who says
"I sometimes think about death" is disclosing an experience, and the right response is
warmth. A hero who says "I have the pills in front of me" is in acute risk, and the right
response is to stop the intake and put 988 in front of them.

Getting that line wrong is costly in both directions. Escalating a reflective disclosure
teaches the hero that honesty ends the conversation — which is precisely the commitment
threshold this product exists to lower. Failing to escalate real acute risk is worse.

KNOWN OPEN QUESTION (from SPEC.md)
----------------------------------
The specific language thresholds below are a prototype approximation and REQUIRE clinical
review before this is used with real heroes. They were written by inspection of the spec's
examples, not from a validated instrument. Two things follow from that:

  * The rules are deliberately narrow. They fire only on explicit, present-tense intent,
    a named method, or hopelessness combined with goodbye framing. Everything else is
    delegated to the model's judgment.
  * The rule layer and the model layer are OR'd together (see `assess`). Either can
    escalate; neither can veto the other. A rule layer that could be silenced by the
    model would be a false comfort.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# Crisis resource — single source of truth, used by app.py in the sidebar,
# in the mode-shift screen, and at the top of the end-screen resource list.
# ---------------------------------------------------------------------------

CRISIS_LINE_NAME = "988 Suicide and Crisis Lifeline"
CRISIS_LINE_NUMBER = "988"
CRISIS_LINE_URL = "https://988lifeline.org"
CRISIS_LINE_TEXT = "Call or text 988"

#: Shown verbatim when the mode shift triggers. Direct, warm, non-clinical — per spec.
MODE_SHIFT_MESSAGE = (
    "What you just shared matters. I want to make sure you're okay right now."
)


# ---------------------------------------------------------------------------
# Rule layer
# ---------------------------------------------------------------------------

# Present-tense, first-person intent. The `\b(?:am|'m)\b` / "going to" framing is what
# separates these from the reflective patterns below.
_ACUTE_INTENT = [
    r"\bi(?:'m| am) going to (?:kill myself|end (?:it|my life)|die tonight)\b",
    r"\bi(?:'m| am) (?:about to|ready to) (?:kill myself|end (?:it|my life))\b",
    r"\bi want to (?:kill myself|die) (?:right now|tonight|today)\b",
    r"\bi(?:'ve| have) decided to (?:kill myself|end (?:it|my life))\b",
    r"\bi(?:'m| am) killing myself\b",
    r"\btonight(?:'s| is) the night\b",
]

# A named method or the means being present/prepared. Method specificity is one of the
# strongest acute-risk signals in the literature, and it is also the pattern most likely
# to appear without any explicit statement of intent.
_ACUTE_METHOD = [
    r"\b(?:i have|i've got|i got|there(?:'s| is)) (?:the |a |my )?"
    r"(?:pills|gun|rope|knife|razor|blade)s?\b[^.?!]{0,40}"
    r"\b(?:here|ready|with me|in (?:my|front of me)|next to me|loaded)\b",
    r"\b(?:i )?(?:wrote|written|writing|finished) (?:a |my )?(?:suicide )?note\b",
    r"\bi (?:bought|stockpiled|saved up|been saving) (?:the |a |my )?"
    r"(?:pills|gun|bullets|rope)s?\b",
    r"\bi (?:have|made) a plan (?:to|for) (?:kill|end|die)\b",
    r"\bmy plan is to\b[^.?!]{0,40}\b(?:kill|die|end it|overdose|jump)\b",
    r"\bi(?:'m| am) (?:standing|sitting) (?:on|at) the (?:bridge|edge|roof)\b",
]

# Hopelessness alone is NOT acute — it is the texture of depression and appears all over
# topic 2 and topic 4. It only escalates here when paired with goodbye framing, which is
# what turns a description of a state into a signal about the next few hours.
_HOPELESS = [
    r"\bno (?:point|reason) (?:in |to )?(?:living|going on|any of (?:it|this))\b",
    r"\bcan(?:'t|not) (?:do|take) (?:this|it) any(?:more| longer)\b",
    r"\bi(?:'m| am) done\b",
    r"\bnothing (?:will |is going to )?(?:ever )?(?:change|get better)\b",
    r"\bi(?:'m| am) (?:a )?burden\b[^.?!]{0,30}\beveryone\b",
]

_GOODBYE = [
    r"\b(?:this is|these are) (?:my|the) (?:last|final) (?:message|words|night)\b",
    r"\bgood ?bye\b",
    r"\bthank you for (?:everything|listening)\b[^.?!]{0,20}\b(?:good ?bye|that's it)\b",
    r"\bi wanted (?:someone|somebody|you) to know (?:before|first)\b",
    r"\btell (?:my|them) .{0,30}\bi(?:'m| am) sorry\b",
    r"\bi won(?:'t| not) be (?:here|around) (?:tomorrow|much longer|anymore)\b",
    r"\btaking care of (?:my|the) (?:cat|dog|kids|pets)\b[^.?!]{0,30}\bafter\b",
]

# Reflective disclosure. These are recorded, not escalated. They exist as an explicit list
# so that (a) the behaviour is testable and (b) a future reviewer can see exactly which
# phrasings this prototype chose to hold rather than escalate.
_REFLECTIVE = [
    r"\bi sometimes think about death\b",
    r"\bi(?:'ve| have) thought about (?:not being here|dying|death)\b",
    r"\bi wonder what it would be like to (?:disappear|not exist|be gone)\b",
    r"\bi (?:used to|would) think about\b",
    r"\bi(?:'ve| have) never (?:acted on|done anything about) (?:it|them)\b",
    r"\bwhat(?:'s| is) the point of (?:it |any of it)?\b",
    r"\bi wish i could (?:disappear|sleep for(?:ever| a year))\b",
    r"\bi think about (?:death|dying|not being here)\b",
    r"\bi feel like a burden\b",
    # Phrasings observed in the golden dataset that the list above missed. Recording them
    # matters even though none of them escalate: `reflective_only` is what tells the
    # conversation engine to stay with the hero rather than move briskly on, and it is what
    # the provider view shows as "difficult-thought language, deliberately held".
    r"\bi (?:do )?think about not (?:existing|being here)\b",
    r"\bwould(?:n't| not) be missed\b",
    r"\bwant(?:ing|ed)? it to stop\b",
    r"\b(?:she|he|they|everyone|my \w+)(?:'d| would) be better off\b",
    r"\bintrusive (?:thought|picture|image)s?\b",
    r"\bit would be (?:easier|restful|a relief) not to be here\b",
]

_PAST_TENSE_MARKERS = [
    r"\byears? ago\b",
    r"\bwhen i was (?:younger|a (?:kid|teenager)|in (?:college|high school))\b",
    r"\bback then\b",
    r"\bused to\b",
    r"\bnot any ?more\b",
    r"\bthat was a long time ago\b",
]


def _compile(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_RULES: dict[str, list[re.Pattern[str]]] = {
    "acute_intent": _compile(_ACUTE_INTENT),
    "acute_method": _compile(_ACUTE_METHOD),
    "hopelessness": _compile(_HOPELESS),
    "goodbye": _compile(_GOODBYE),
    "reflective": _compile(_REFLECTIVE),
    "past_tense": _compile(_PAST_TENSE_MARKERS),
}


def _matches(text: str, key: str) -> list[str]:
    """Return the matched substrings for one rule family."""
    out: list[str] = []
    for pattern in _RULES[key]:
        found = pattern.search(text)
        if found:
            out.append(found.group(0))
    return out


# ---------------------------------------------------------------------------
# Assessment result
# ---------------------------------------------------------------------------


@dataclass
class SafetyAssessment:
    """The outcome of screening one hero message.

    Attributes:
        escalate: True when the conversation must shift into crisis mode.
        source: Which layer escalated — "rules", "model", "both", or "" when neither did.
        triggers: Rule families that fired, e.g. ["acute_method"]. Recorded even when the
            assessment does not escalate, so the provider vignette and the evals can see
            what the screen saw.
        evidence: The literal matched substrings, for the provider view and for debugging
            a false positive without having to re-run the regexes by hand.
        reflective_only: True when difficult-thought language was present but read as
            reflective. This is the case the spec cares most about getting right — it means
            "hold with warmth", and the conversation engine uses it to stay in topic 4
            rather than moving on briskly.
    """

    escalate: bool = False
    source: str = ""
    triggers: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    reflective_only: bool = False

    def as_dict(self) -> dict:
        return {
            "escalate": self.escalate,
            "source": self.source,
            "triggers": list(self.triggers),
            "evidence": list(self.evidence),
            "reflective_only": self.reflective_only,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def screen_rules(hero_message: str) -> SafetyAssessment:
    """Run the deterministic rule layer over one hero message.

    No API call, no model judgment — this is the layer that still works when the API is
    down, and the layer the evals can assert on without spending tokens.
    """
    assessment = SafetyAssessment()
    if not hero_message or not hero_message.strip():
        return assessment

    text = hero_message.strip()

    intent = _matches(text, "acute_intent")
    method = _matches(text, "acute_method")
    hopeless = _matches(text, "hopelessness")
    goodbye = _matches(text, "goodbye")
    reflective = _matches(text, "reflective")
    past_tense = _matches(text, "past_tense")

    if intent:
        assessment.triggers.append("acute_intent")
        assessment.evidence.extend(intent)
    if method:
        assessment.triggers.append("acute_method")
        assessment.evidence.extend(method)

    # Hopelessness escalates only in combination with goodbye framing. Both are recorded
    # either way so a provider can see the combination that was *almost* there.
    if hopeless:
        assessment.triggers.append("hopelessness")
        assessment.evidence.extend(hopeless)
    if goodbye:
        assessment.triggers.append("goodbye")
        assessment.evidence.extend(goodbye)

    acute = bool(intent or method or (hopeless and goodbye))

    # An explicit past-tense marker downgrades a bare hopelessness+goodbye combination
    # ("years ago I said goodbye to everyone") but never downgrades stated intent or a
    # present method. Narrow on purpose.
    if acute and not (intent or method) and past_tense:
        acute = False
        assessment.triggers.append("past_tense_downgrade")
        assessment.evidence.extend(past_tense)

    if reflective:
        assessment.triggers.append("reflective")
        assessment.evidence.extend(reflective)

    assessment.escalate = acute
    assessment.source = "rules" if acute else ""
    assessment.reflective_only = bool(reflective) and not acute
    return assessment


def assess(hero_message: str, model_crisis_flag: bool = False) -> SafetyAssessment:
    """Combine the rule layer with the model's per-turn `crisis_flag`.

    Either layer can escalate; neither can veto the other. The model sees conversational
    context the regexes cannot (a method disclosed three turns earlier, an implicit
    goodbye); the regexes catch what a warm-toned model may soften past.

    Args:
        hero_message: The hero's latest message.
        model_crisis_flag: `crisis_flag` from the turn's structured output.
    """
    assessment = screen_rules(hero_message)

    if model_crisis_flag:
        assessment.escalate = True
        assessment.source = "both" if assessment.source == "rules" else "model"
        if "model_flag" not in assessment.triggers:
            assessment.triggers.append("model_flag")
        # A model escalation means this is not a hold-with-warmth turn, whatever the
        # reflective patterns suggested.
        assessment.reflective_only = False

    return assessment


def crisis_resources_markdown() -> str:
    """Crisis block for the sidebar and the mode-shift screen."""
    return (
        f"**{CRISIS_LINE_NAME}**\n\n"
        f"### {CRISIS_LINE_TEXT}\n\n"
        f"Free, confidential, 24/7 — for you or for someone you're worried about.\n\n"
        f"[{CRISIS_LINE_URL}]({CRISIS_LINE_URL})"
    )
