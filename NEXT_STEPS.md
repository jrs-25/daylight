# Daylight — Next Steps

*Written 2026-09-13, updated 2026-09-14. A plan for picking the prototype back up, in
recommended order. Revise freely; delete sections as they're done.*

---

## Where things stand

- The app runs end to end: hero conversation → vignette → Provider View. Community
  enrichment works when the CHR workbook is in `data/`; the HUD ZIP→county crosswalk is
  **not** downloaded yet, so enrichment currently resolves to `default`.
- **Sep 13:** each topic now carries a `LISTEN_FOR` list of clinical constructs (PHQ-9, MDQ,
  C-SSRS, AUDIT-C, MI) that shapes what the companion listens for and when a topic counts as
  covered. See ADR-010 in `data/DECISIONS.md`.
- **Sep 14:** manual testing showed the conversation read as an aimless chat buddy — warm, but
  with no sense of where it was going. Three fixes: an opening that sets a contract (how long,
  what gets asked, what you get at the end), mandatory transitional summaries at every topic
  change (the missing S in OARS), and an explicit *guiding* stance, since the first pass had
  taken MI's "follow the hero" half and none of its directive half. Sidebar rail now names the
  current and upcoming areas instead of showing a bare "3 of 7".
- **Sep 14:** UI latency fixes — the close ran two API calls behind an unlabeled spinner and
  read as a hang. Vignette generation moved off the hero's critical path; all waits labeled;
  `st.chat_input` transcript ordering fixed (it renders inline inside a tab, not pinned).
- **Both prompt changes have been checked against the offline evals only** — the live
  conversation replay has not been re-run since either of them.
- Repo is clean for public: no secrets in history, MIT license, WIP notice in README.
- Two commits are waiting to be pushed (`git push`).

To resume:

```bash
cd ~/daylight
.venv/bin/streamlit run app.py                  # the app
.venv/bin/python evals/run_evals.py             # offline evals, no API calls
.venv/bin/python evals/run_evals.py --live      # full replay, ~130 API calls
```

---

## 1. First session back (an hour)

1. **`git push`** and flip the repo to public.
2. **Run the live eval**: `run_evals.py --live`. Two prompt changes are now stacked and
   unevaluated — the stricter `topic_status: complete` criterion (Sep 13) and the guiding
   stance plus mandatory summaries (Sep 14). Either could move `topic_coverage`; the summaries
   also add tokens per turn, so watch whether the arc still finishes inside 60 turns. Copy
   `evals/results/summary.json` aside first so you have a before/after.
3. **Play through three personas yourself** — partly done Sep 14; the dialogue fixes above
   came out of it, but the bipolar and sparse/evasive presentations still need a pass. in the app — a clear depression presentation, a
   bipolar-leaning one, and someone evasive/sparse — and read the resulting vignettes in the
   Provider View. The question to hold: *would a therapist recognize this as an intake?*
4. **Download the HUD crosswalk** (free account, `data/zip_county.csv`) so community context
   actually fires during manual testing.

---

## 2. Clinical validation (the thing that matters most)

The product's credibility rests on this and it can't be done from inside the code. Find one
person — a licensed therapist, psychiatric NP, or clinical psychologist — willing to spend two
hours reviewing three artifacts:

| Artifact | Question for them |
|---|---|
| `LISTEN_FOR` in `src/conversation.py` | Is this what you'd want an intake to have touched? What's missing, what's wrong? |
| `safety.py` rule layer + prompt crisis language | Is the ideation/intent line drawn in the right place? Specifically: **method-without-intent** (C-SSRS rung 3) does *not* escalate today. Should it? |
| Three vignettes from step 1.3 | Would you read this before a first session? What would you want that isn't here? |

Turn what they say into golden cases (`evals/golden_dataset.json`) so the review becomes a
regression test rather than a one-time conversation. Cases worth adding regardless:

- Method mentioned, no intent ("I've thought about the pills in the cabinet, but I wouldn't")
- Past attempt disclosed in past tense
- Hopelessness *without* goodbye framing (must not escalate)
- Goodbye framing *without* explicit hopelessness (should it?)
- A hero who answers a screening-shaped question with a number ("like 3 out of 10") — does the
  companion stay conversational?

Record the outcome as ADR-011.

---

## 3. Eval hardening

The current replay has a known limitation (documented in `run_evals.py`): the scripted hero
can't adapt to what the companion actually asked. Three improvements, in order of value:

1. **Simulated hero.** Replace scripted turns with a second Claude call playing a persona
   (backstory + what they'll disclose readily vs. only if asked well vs. never). This makes
   topic-coverage and pacing evals meaningful and lets you test the "roll with resistance"
   guidance. Keep the scripted mode for the crisis cases where exact wording matters.
2. **Style metrics** that check the MI stance directly, judged by a model over the transcript:
   one question per turn; no verbatim screening items ("how many days in the last two
   weeks"); reflection before question; no diagnostic labels. Cheap, and they'll catch prompt
   regressions the coverage metric can't.
3. **CI.** A GitHub Action that runs the *offline* evals on every push — free for a public repo,
   and it keeps `safety.py` and `enrichment.py` honest. Live evals stay manual.

---

## 4. Provider side

Now that the vignette prompt distinguishes "said no" from "not asked," the Provider View
should show it. A small coverage grid per topic (each `LISTEN_FOR` item: answered / denied /
not reached) would give a therapist more than the prose summaries do, and it's the artifact
the clinical reviewer in step 2 is most likely to react to.

Worth considering at the same time: a structured `signals` schema in the vignette (construct,
what the hero said, onset/duration if given) rather than free-text strings, so the Provider
View can render and filter them.

---

## 4a. Known trade-off to revisit

Vignette generation now runs as the last statement of the end screen, so there is a ~20s
window where a hero could close the tab after seeing a complete-looking ending and lose the
vignette. The window existed before too (it just sat in front of the ending as a blank
spinner), and loss is arguably less likely now, but it is a new failure mode. In production
this is a background job, not something racing the hero's attention.

Also unresolved from the Sep 14 session: **should the hero see their own vignette at the end?**
The new opening promises them "a picture of what you told me, and it's yours" — the end screen
does not currently deliver it. Decide rather than drift; it would be ADR-012.

**Vignettes are only written on a clean exit.** A vignette is generated at the end of
`render_closed`, which is reached either when all seven topics are covered (or turn 60) or when
a hero clicks through the crisis screen to close. Anyone who abandons mid-conversation, reloads
the page, or shuts the tab after escalating produces no record at all — nothing partial is
written.

That biases the Provider View toward heroes who completed all seven areas and waited for their
ending, which is close to the opposite of the population the product exists for: the sessions
most clinically significant are the ones least likely to end politely. For the prototype it is
defensible (the vignette is a matching artifact, and there is nobody to match if the hero left),
but if the provider side should reflect reality, the fix is writing the vignette incrementally —
say once `difficult_thoughts` is covered, updating on each subsequent topic — rather than only
at a clean exit. That is a design change, not a tweak, and wants its own ADR.

---

## 5. Product questions (after 2–4, not before)

These are all deferred in ADR-009 and should stay deferred until the core conversation is
clinically validated. Listed so they don't get lost:

- Session persistence / returning heroes
- Actual provider matching on the vignette (currently the vignette is the interface, matching
  isn't built)
- Anxiety presentations — would need a topic-arc extension, not just prompt changes
- What happens after the crisis mode shift — currently the conversation stops; is there a
  re-entry path?
- HIPAA posture if this ever touches real people

---

## Housekeeping

- Add a `tests/` directory with pytest cases for `safety.py` rules and `enrichment.py`
  lookups — faster iteration than the eval runner for pure-logic changes.
- Pin `streamlit` and `anthropic` versions in `requirements.txt` if they aren't already.
- Since the repo is public: a short CONTRIBUTING note saying this is a prototype seeking
  clinical review, and that PRs to `safety.py` need a clinician sign-off.
- The Streamlit-installed agent skill (`.claude/skills/`, `.agents/`) is gitignored; leave it
  or `rm -rf` it, no other effect.

---

## Recommended order, in one line

Push → live eval → play three personas → **find a clinician** → encode what they say as golden
cases → simulated hero → provider coverage grid. Everything else waits.
