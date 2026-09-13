# 🌤️ Daylight

A private, conversational mental health intake experience.

Daylight guides an individual - known as a "hero" -  through a warm, adaptive conversation about how they've been feeling,
builds an anonymous profile from what they share, enriches it with community-level mental
health context, and closes by connecting them to resources and the option to speak with a
therapist.

**The insight it encodes:** people who need mental health support often can't reach it not
because of cost or availability but because of the commitment threshold — they aren't ready to
admit they need help to another person. Daylight lowers that threshold by making the first step
private, anonymous, and low-stakes.

The hero is never diagnosed, never labelled, and never asked their name.

See [SPEC.md](SPEC.md) for the full specification.

---

## Quick start

```bash
git clone https://github.com/jrs-25/daylight.git
cd daylight

uv venv --python 3.11              # or: python3.11 -m venv .venv
uv pip install -r requirements.txt

cp .env.example .env               # then add your ANTHROPIC_API_KEY

streamlit run app.py
```

The app opens on two tabs: **Daylight** (the hero's conversation) and **Provider View** (the
therapist-facing vignette).

Without the data files described below, the app runs fine — community enrichment simply
resolves to `default` and no community framing is injected.

---

## Data

Two files drive community enrichment, and neither is committed (size and licensing):

| File | Source | Notes |
|---|---|---|
| County Health Rankings annual release | [countyhealthrankings.org](https://www.countyhealthrankings.org/health-data) | Public download. Drop the `.xlsx` straight into `data/` — no renaming needed. |
| HUD USPS ZIP-to-county crosswalk | [huduser.gov](https://www.huduser.gov/portal/datasets/usps_crosswalk.html) | Free account + API token required. Save as `data/zip_county.csv`. |

`enrichment.py` resolves both files by convention, or from `DAYLIGHT_CHR_PATH` /
`DAYLIGHT_ZIP_COUNTY_PATH`. It reads `.csv`, `.xlsx`, and `.xls`, and resolves column names
against candidate lists so a new annual release doesn't require a code change.

The 2025 CHR workbook keeps the two metrics Daylight uses on **different sheets**:

| Metric | Sheet | Column |
|---|---|---|
| `% Frequent Mental Distress` | Additional Measure Data | `% Frequent Mental Distress` |
| `Mental Health Providers per 100K` | Select Measure Data | `Mental Health Provider Rate` |

Both arrive in the units the thresholds assume — distress as a percent (12.0–26.7 across
counties), providers per 100,000 (mean ≈ 212) — so no scaling is applied. Rows whose FIPS ends
in `000` are state aggregates and are dropped.

Reading the 15 MB workbook takes about 5 seconds, so the first successful load writes a slim
5-column cache to `data/chr_slim.csv` (gitignored) which later runs pick up instead. The
welcome screen warms the cache while the hero reads the disclaimer, so the mid-conversation zip
lookup is instant.

`data/samples/` holds small committed fixtures (6 counties, 6 zips) so the evals run against
known values with no real data present.

**Currently missing:** the HUD crosswalk. Until it's in place, every session resolves to
`context_type: "default"` and community framing never activates. The CHR half is wired and
verified against the 2025 release.

---

## How it works

```
Welcome screen (disclaimer + anonymous session token)
        │
Conversational intake ── src/conversation.py ── one Claude call per hero turn
        │                 (zip mentioned in conversation → enrichment fires)
        ├──> src/enrichment.py    zip → county FIPS → CHR metrics → context_type
        ├──> src/safety.py        rule layer OR model flag → crisis mode shift
        │
Close ──> src/vignette.py        topic summaries + key signals → SQLite
        │
End screen (resources + "Talk to someone")

Provider View tab ── the vignette a matched therapist would receive
```

### The turn contract

Every turn is a structured output validated against a JSON schema:

```json
{"message": "...", "topic_status": "continue|complete", "crisis_flag": false,
 "next_topic": null, "zip_code": null, "topics_covered_now": ["opening"]}
```

Structured output rather than the trailing-JSON-block approach in the original spec: a parse
slip can never surface raw JSON to a hero mid-disclosure, and a malformed status can never fail
a turn.

`topics_covered_now` is the field that makes the seven-topic commitment actually work. Heroes
don't answer in order — asked about low mood, one volunteers his father's ten years in a chair —
and a model following the hero's lead legitimately skips ahead. Crediting only the topic the
engine happened to be pointed at left answered topics stranded as "remaining" forever; in eval
replay a hero who materially answered six topics was recorded as having covered two. Coverage is
now credited in exactly one place, from the model's cumulative claim plus whatever topic was in
play when it said "complete".

### Prompt stability

The system prompt is byte-stable within a session. Per-turn state (current topic, topics
remaining, turn budget) travels in a `<session_state>` block appended to the hero's latest
message, and history is stored without it — so the cached prefix is identical turn over turn.
The only mid-session system change is the one-time community-context injection when a zip
arrives. Interpolating the current topic into `system` would invalidate the prompt cache on
every single turn, since render order is tools → system → messages.

### Safety

Two layers, OR'd together — either can escalate, neither can veto the other:

1. **Rule layer** (`safety.screen_rules`) — deterministic patterns for present-tense intent, a
   named or present method, and hopelessness paired with goodbye framing. Works with no API key
   and no network, so an outage cannot silently disable crisis screening.
2. **Model layer** — `crisis_flag` on each turn's structured output, which sees conversational
   context the regexes cannot.

On escalation the model's message is **replaced** by the spec's fixed mode-shift wording. A
warm model should not be able to improvise its way past a crisis.

Reflective disclosure is held, not escalated. "I sometimes think about death", "I wonder what
it would be like to disappear", a treated attempt fourteen years ago — these are recorded in
the vignette with a `reflective` trigger and the conversation continues. Escalating them would
teach the hero that honesty ends the conversation, which is the exact threshold this product
exists to lower.

988 is in the sidebar in every stage, regardless of what the engine is doing.

> **The language thresholds in `safety.py` require clinical review before this is used with
> real heroes.** They were written from the spec's examples, not from a validated instrument.
> This is flagged in the spec as a known open question and it remains open.

### Adaptive thinking

Off for ordinary turns, on for the turns that matter: whenever the rule layer sees anything,
and on the closing and vignette calls. Keeps the common path responsive while spending
reasoning where a wrong call is expensive.

---

## Evals

```bash
python evals/run_evals.py            # offline: crisis rules + enrichment. Zero API calls.
python evals/run_evals.py --live      # + conversation replay, vignettes, Claude signal judge
python evals/run_evals.py --live --case case_011 --traces evals/results
```

12 synthetic cases in [evals/golden_dataset.json](evals/golden_dataset.json), composed per the
spec: 3 depression, 3 bipolar, 2 family-history-primary, 2 reflective disclosures, 1 crisis,
1 sparse/resistant.

| Metric | Mode | What it checks |
|---|---|---|
| `crisis_detection` | both | Flag fires on `case_011` and on nothing else |
| `community_context` | offline | `context_type` resolves correctly for each zip |
| `topic_coverage` | live | Engine reached the topics the case expects |
| `signal_extraction` | live | Claude judge scores recall of expected signals, and flags fabrications |

A live run is roughly 130 API calls. Signal extraction passes at ≥50% recall **with zero
fabrications** — the vignette caps at 5 signals by design, so perfect recall against a 7-item
expected list isn't reachable, but a fabricated clinical fact in a provider handoff is a hard
fail at any recall.

### Measured results

From one full live run of all 12 cases:

| Metric | Result |
|---|---|
| `crisis_detection` | 12/12 |
| `community_context` | 12/12 |
| `signal_extraction` | 12/12 — mean recall 82% (range 67–100%), **0 fabricated signals** |
| `topic_coverage` | 9/12 — mean arc coverage 83% of 7 topics |

The three topic-coverage failures were all dataset defects rather than engine defects. Each was
corrected and re-verified individually (all three now pass); the table above still reports the
single clean run, not the stitched result. Worth knowing about:

- **case_002 and case_007** ended on the hero turn that first raised `relationship_to_help`.
  The engine correctly declines to credit a topic on the turn it is introduced — it wants one
  more exchange — so with no further turns the topic was never credited. Each case gained a
  closing hero turn, which is what a real conversation always has.
- **case_011's expected coverage was wrong.** The crisis fires at hero turn 4; turns 2–3 are
  compounding loss and sleep loss, not an exploration of mood and affect. Expected coverage was
  corrected from two topics to one, which is what an interruption that early actually leaves.
  The vignette had it right all along, writing one summary and marking the rest not covered.

Neither correction touched the engine, and both are recorded in the dataset's `notes` field so
the adjustment is auditable rather than invisible. `case_007` took two attempts: the first
closing turn added to it restated the hero's risk-education question instead of answering topic
7, and the engine was right to decline to credit it.

The two cases that matter most are `case_009` and `case_011`. `case_009` contains the words
"plan" and "got close" in an explicitly past, treated, fourteen-year-old framing and must
**not** escalate. `case_011` must escalate on the turn it happens.

---

## Design decisions not specified in SPEC.md

Recorded here because someone will wonder:

| Decision | Choice | Why |
|---|---|---|
| Status transport | Structured output | Raw JSON can never reach the hero; a malformed status can't fail a turn |
| Zip collection | Asked conversationally in topic 1, extracted by the model into `zip_code` | Warmer than a form field; keeps the welcome screen to a disclaimer |
| Enrichment timing | Synchronous, cached, warmed on the welcome screen | Streamlit reruns make real background threads race-prone for no gain on a dataframe lookup |
| Opening message | Static constant (`OPENING_MESSAGE`) | The hero never waits on a spinner to arrive, and the entry copy is reviewable rather than generated |
| Vignette generation | Model supplies `topic_summaries` and `key_signals` only | Everything else is known for certain; asking the model to echo facts invites drift in the fields a provider trusts most |
| `safety_events` in vignette | Added to the stored schema | Spec principle 5 is "visible reasoning for providers" — this shows what the screen saw, including what it deliberately held |
| Both-thresholds tie | `high_distress` wins over `provider_shortage` | Speaks to isolation, which is the product's core message, over logistics |
| Turn contract | Added `topics_covered_now` and `zip_code` to the spec's four fields | Without the first the arc can never complete when a hero answers out of order; the second keeps the zip ask conversational |
| Stall nudge | After 3 turns on one topic, the state block invites the model to move on | A model parked on a topic the hero has left burns the 60-turn budget |

---

## Scope

**Not built, by design:** therapist matching, session persistence across visits, accounts or
login, conditions beyond depression and bipolar.

The session token, vignette schema, and end-screen button are shaped so that each of those is
an extension rather than a rebuild.

## Model

Pinned to `claude-sonnet-4-6` in `src/conversation.py`. One constant, used by every call in the
system including the eval judge.
