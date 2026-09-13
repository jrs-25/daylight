# Daylight — Project Specification

## Purpose

Daylight is a private, conversational mental health intake experience. It guides a hero
through a warm, adaptive conversation focused on depression and bipolar disorder, builds an
anonymous profile from what they share, enriches it with community-level mental health
context, and closes by connecting them to resources and the option to speak with a therapist.

The project's working name references Paul Dalio, director of *Touched with Fire*, a film
about bipolar disorder inspired by his own experience with the condition.

**The core insight this product encodes:** People who need mental health support often can't
access it not because of cost or availability alone, but because of the commitment threshold —
they aren't ready to admit they need help to another person. This system lowers that threshold
by making the first step private, anonymous, and low-stakes.

**Design philosophy:**
- The hero is never diagnosed or labeled
- The system is oriented toward "you are not alone" — not toward a verdict
- The hero never provides their name
- Community data informs tone but never surfaces as statistics
- The conversation holds difficult disclosures without flinching
- The system refers to users as "heroes" internally and in code throughout

---

## Scope

**In scope for this prototype:**
- Conversational intake focused on depression and bipolar disorder
- Community-level mental health enrichment via County Health Rankings
- Anonymous session management (no login, no name)
- Backend hero vignette for therapist view
- End-of-conversation resource links and therapist connection option
- Crisis detection with mode shift

**Explicitly out of scope:**
- Dynamic therapist matching
- Session persistence across visits
- User accounts or login
- Conditions beyond depression and bipolar

---

## Architecture

```
Welcome screen (disclaimer + anonymous session token generated)
        |
Conversational intake — Claude-driven, topic arc (src/conversation.py)
        | (zip code collected early -> enrichment)
Community enrichment lookup (src/enrichment.py) -> data/chr.csv + data/zip_county.csv
        |
Vignette assembly (src/vignette.py) -> stored in SQLite
        |
End screen — resources + therapist connection option (app.py)

Backend view (app.py, separate tab) — therapist-facing vignette
```

---

## Tech Stack

| Component | Choice |
|-----------|--------|
| Frontend | Streamlit |
| LLM | Claude via Anthropic API |
| Community data | County Health Rankings CSV + HUD zip-county crosswalk |
| Session storage | SQLite |
| Language | Python 3.11+ |

---

## File Structure

```
daylight/
├── .env
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
├── SPEC.md
├── app.py                      # Streamlit frontend — conversation + backend view
├── data/
│   ├── chr.csv                 # County Health Rankings (annual release)
│   ├── zip_county.csv          # HUD zip-to-county crosswalk
│   └── samples/                # Small committed fixtures so evals run without real data
├── evals/
│   ├── golden_dataset.json     # Synthetic conversations with expected outputs
│   └── run_evals.py            # Eval runner
└── src/
    ├── __init__.py
    ├── conversation.py         # Claude-driven conversation engine
    ├── enrichment.py           # County Health Rankings lookup
    ├── vignette.py             # Profile assembly from conversation
    └── safety.py               # Crisis detection logic
```

---

## Session Management

- On load, generate a UUID session token (uuid4)
- Token is displayed to the hero as a simple reference code: "Your session: [token]"
- No name collected at any point
- Session token is the primary key for vignette storage
- In production, this token would enable session persistence; the architecture should make
  that extension obvious

---

## Topic Arc

The conversation covers seven topic areas in order. Claude controls pacing and depth within
each topic — it decides when to probe deeper and when to move on. The system is committed to
covering all seven areas before closing.

**1. Opening**
Warm entry — why the hero is here, what made them start this conversation today. No clinical
framing.

**2. Mood and affect**
Losing interest in things they usually enjoy. Frequency of low mood. Negative self-talk.
Crying. Energy levels.

**3. Elevated states**
Periods of unusual energy, excitement, or ideas that others didn't understand or match.
Feeling invincible or unusually productive. Racing thoughts. This topic is asked without the
word "bipolar" — framed as a real experience many people have that often goes unrecognized.

**4. Difficult thoughts**
Thoughts about death, hopelessness, feeling like a burden, or wanting to disappear. Asked in a
way that makes it safe to answer honestly — the system does not treat disclosure as an
automatic crisis flag. See Crisis Detection below for how the system distinguishes.

**5. Family history**
Whether mental health conditions run in their family. Handled gently — many people don't know,
or it was never talked about. "Don't know" is a meaningful and valid answer.

**6. Lifestyle context**
Sleep patterns and any recent changes in either direction. Substance use. Exercise. Appetite
changes.

**7. Relationship to help**
What has stopped them from seeking support before. Whether medication feels like a dealbreaker.
What would make them trust someone enough to talk to them. This is the topic most tools skip —
it is core to this product.

---

## Conversation Engine (`src/conversation.py`)

### State object
```python
{
    "session_id": str,              # UUID
    "zip_code": str | None,         # Collected early, optional
    "community_context": dict,      # From enrichment layer, used to inform tone
    "topics_covered": list[str],    # Which of the 7 topics are complete
    "current_topic": str,           # Active topic
    "history": list[dict],          # Full message history {role, content}
    "crisis_flag": bool,            # True if mode shift triggered
    "turn_count": int
}
```

### System prompt
```
You are a warm, private companion helping someone explore how they've been feeling.
You are not a therapist and you don't diagnose anything. Your job is to listen,
ask thoughtful questions, and help the person feel less alone in what they're
experiencing.

The conversation focuses on depression and bipolar disorder — but you never use
those words unless the hero uses them first. You speak about experiences, not labels.

You are working through seven topic areas. You must cover all seven before the
conversation closes. You control when to go deeper and when to move on — follow
the hero's lead. If they deflect, stay gentle and return.

Core orientation: "you are not alone." What the hero is experiencing is real,
recognized, and shared by many people who found their way through it.

{community_context_prompt}
```

Per-turn state (current topic, topics remaining, turn budget) is delivered as a
`<session_state>` block appended to the hero's latest message, keeping the system prompt and
message history byte-stable across turns so the prompt cache holds.

### Community context prompt injection
If community data is available, inject one of the following into the system prompt
(chosen based on the county's metrics):

- High distress county: "Many people in this hero's community are carrying similar weight
  without support. Let that inform how you frame 'you are not alone.'"
- Provider shortage area: "Mental health support is genuinely hard to access where this hero
  lives. That context matters for how you speak about barriers to care."
- Default: No injection — do not reference community data if metrics are unremarkable.

### Turn contract
Each turn is a structured output. Claude returns a single JSON object validated against a
schema:

```json
{
    "message": "the text shown to the hero",
    "topic_status": "continue" | "complete",
    "crisis_flag": false,
    "next_topic": "mood_and_affect" | null,
    "zip_code": "40906" | null,
    "topics_covered_now": ["opening", "mood_and_affect"]
}
```

Structured output is used rather than a trailing JSON block so that a parse slip can never
surface raw JSON to the hero and a malformed status can never fail a turn.

`zip_code` carries the zip out of the hero's prose when they mention one, so the ask can stay
conversational instead of becoming a form field.

`topics_covered_now` is the model's cumulative claim about which topic areas the hero has
actually given something to. It exists because heroes do not answer in order: a hero asked
about low mood will volunteer their father's ten years in a chair, and a model following the
hero's lead legitimately skips ahead. Without this field the engine can only credit the topic
it happened to be pointed at, so an answered-but-skipped topic stays "remaining" forever and
the seven-topic commitment can never complete. A clear denial counts as coverage ("no, I've
never had a high period" covers elevated states); a shrug does not.

Coverage is credited in exactly one place. `topics_covered_now` is unioned into
`topics_covered`, and a `topic_status: complete` additionally credits whatever topic was in
play when the model spoke.

### Conversation loop
1. Send stable system prompt + history + hero's message with `<session_state>` appended
2. Read the validated status object from the response
3. Display `message` to hero
4. Run the safety layer over the hero's message and Claude's `crisis_flag` — if either
   escalates, hand off to crisis mode
5. If `topic_status: complete` — advance to next topic
6. If all topics covered — transition to closing
7. Append hero response to history, repeat

### Turn limit
Cap at 60 turns total. If limit approached, begin graceful close regardless of topics
remaining.

### Stall nudge
After three turns on one topic the `<session_state>` block tells the model it may mark the
topic complete and follow the hero to whatever they are actually answering. A model that parks
on a topic the hero has already left burns the turn budget for no gain.

---

## Community Enrichment (`src/enrichment.py`)

### Data sources
- `data/chr.csv` — County Health Rankings annual release (countyhealthrankings.org)
- `data/zip_county.csv` — HUD USPS zip-to-county crosswalk (huduser.gov)

Both loaders accept `.csv`, `.xlsx`, or `.xls`. Column names are resolved against candidate
lists, so both the human-readable CHR headers and the coded `v###_rawvalue` headers work
without renaming the release file.

### Key fields from County Health Rankings
- `% Frequent Mental Distress` — adults reporting 14+ poor mental health days/month
- `Mental Health Providers per 100K` — provider access rate

### Lookup logic
1. Accept zip code string
2. Look up county FIPS via zip_county crosswalk (use highest residential ratio if multiple
   counties)
3. Look up county in chr.csv by FIPS
4. Return enrichment dict:

```python
{
    "county": str,
    "state": str,
    "pct_frequent_distress": float | None,
    "mh_providers_per_100k": float | None,
    "context_type": "high_distress" | "provider_shortage" | "default"
}
```

### Context classification
- `high_distress`: pct_frequent_distress > 20%
- `provider_shortage`: mh_providers_per_100k < 30
- `default`: otherwise, or if zip not found

### Error handling
- Zip not found: return context_type "default", all metrics None
- Missing or unreadable data files: same — never raise into the conversation
- Do not surface errors to hero

---

## Crisis Detection (`src/safety.py`)

### Design principle
The system distinguishes between a hero disclosing difficult thoughts reflectively versus
language signaling acute distress. The former is held with warmth. The latter triggers a mode
shift.

### Known open question
The specific language thresholds for triggering the mode shift are a known design decision
requiring clinical input before production. For the prototype, the approach is conservative
and explicit.

### Reflective disclosure (hold with warmth, do not escalate)
Language patterns indicating the hero is sharing an experience, not expressing immediate
intent:
- "I sometimes think about death"
- "I've thought about not being here"
- "I wonder what it would be like to disappear"
- Past tense disclosures
- Philosophical framing

Response: warm acknowledgment, normalize the experience, continue conversation.

### Acute distress (mode shift)
Language patterns indicating possible immediate risk:
- Explicit statements of current intent to harm self
- Specific plan or method mentioned
- Expressions of immediate hopelessness combined with goodbye framing

### Mode shift behavior
When crisis_flag triggers:
1. Conversation stops immediately
2. System responds with a direct, warm, non-clinical message:
   "What you just shared matters. I want to make sure you're okay right now."
3. 988 Suicide and Crisis Lifeline displayed prominently
4. Option to continue conversation or close
5. Crisis flag stored in vignette

### Always visible
988 Suicide and Crisis Lifeline is displayed in the Streamlit sidebar at all times,
regardless of conversation state.

---

## Vignette Assembly (`src/vignette.py`)

The vignette is built from the completed conversation and stored in SQLite. It is never shown
to the hero. It is available in the therapist backend view.

### Schema
```python
{
    "session_id": str,              # UUID
    "timestamp": str,               # ISO format
    "zip_code": str | None,
    "community_context": dict,      # Full enrichment output
    "topic_summaries": {
        "opening": str,
        "mood_and_affect": str,
        "elevated_states": str,
        "difficult_thoughts": str,
        "family_history": str,
        "lifestyle": str,
        "relationship_to_help": str
    },
    "key_signals": list[str],       # Extracted by Claude at close
    "crisis_flag": bool,
    "turn_count": int
}
```

### Vignette generation
At close of conversation, make a final Claude call with the full conversation history:

```
You have just completed a mental health intake conversation.
Summarize what the hero shared in each topic area in 2-3 sentences each.
Then identify the 3-5 most clinically significant signals from the conversation.
Be specific. Use neutral, non-diagnostic language.
```

The call uses structured output against the vignette schema, so the response is always a
valid vignette.

---

## End Screen

Shown to hero after conversation closes naturally (not after crisis mode shift, which has its
own close).

**Structure:**
1. Warm closing message — acknowledges what they shared, affirms their courage in showing up
2. "You are not alone" statement — one sentence, informed by community context if available
   but not citing statistics
3. Resource links:
   - 988 Suicide and Crisis Lifeline (always first)
   - NAMI (nami.org) — National Alliance on Mental Illness
   - Depression and Bipolar Support Alliance (dbsalliance.org)
   - Mental Health America (mhanational.org)
4. Therapist connection option — a prominent button: "Talk to someone"
   - In prototype: displays a message explaining this feature is coming and suggesting
     Headway (headway.co) as an immediate option
   - Architecture makes clear how this would trigger a matching flow in production

---

## Backend Therapist View

Accessible via a second tab in the Streamlit app labeled "Provider View."

Displays:
- Session token
- Timestamp
- Community context (county, metrics, context type)
- Topic-by-topic summaries
- Key signals as a bulleted list
- Crisis flag (prominent if true)

This view represents what a matched therapist would receive before a first conversation.

---

## Eval Spec

### Golden dataset (`evals/golden_dataset.json`)
12 synthetic conversation transcripts covering:
- 3 clear depression presentations
- 3 clear bipolar indicators (elevated state topic is the key signal)
- 2 cases where family history is the primary signal
- 2 cases with difficult thought disclosures (reflective, not acute)
- 1 crisis mode shift case
- 1 sparse/resistant conversation

Each case includes:
```json
{
    "case_id": "case_001",
    "description": "What this case tests",
    "conversation": [...],
    "expected_topics_covered": [...],
    "expected_key_signals": [...],
    "expected_crisis_flag": false,
    "rationale": "Why this case should produce this output"
}
```

### Metrics
- Topic coverage rate — all 7 topics reached
- Signal extraction accuracy — key signals match expected
- Crisis detection accuracy — flag triggered when expected, not triggered when not
- Community context classification accuracy — correct context_type for zip

### Running
`python evals/run_evals.py` runs the deterministic layers only (crisis rules, enrichment
classification) with zero API spend. `--live` adds conversation replay, vignette generation,
and a Claude judge for signal matching.

---

## Requirements

```
anthropic
streamlit
pandas
python-dotenv
openpyxl
```

---

## Environment Variables

```
ANTHROPIC_API_KEY=
```

---

## Key Design Principles

1. **No name, ever** — the hero is anonymous throughout. Session token is system-generated.
2. **No verdict** — the system never says what the hero has or might have. It builds a picture.
3. **Data informs voice, not output** — community enrichment shapes tone. The hero never sees
   a statistic.
4. **Hold before escalate** — difficult thought disclosures are met with warmth first. Crisis
   mode is reserved for acute distress signals.
5. **Visible reasoning for providers** — the vignette shows what signals were identified and
   why. No black box.
6. **Architecture anticipates production** — session token, vignette schema, and end screen
   button are all designed to make persistence and matching an extension, not a rebuild.
7. **Model pinned** — always use `claude-sonnet-4-6` explicitly.
