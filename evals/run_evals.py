#!/usr/bin/env python3
"""Daylight eval runner.

Four metrics, from SPEC.md -> Eval Spec:

    topic_coverage       all 7 topics reached          (live)
    signal_extraction    key signals match expected    (live, Claude judge)
    crisis_detection     flag fires when and only when it should
    community_context    correct context_type for zip  (offline)

Two modes, because the deterministic layers are the ones you want to run on every commit and
the model layers cost money:

    python evals/run_evals.py           # offline: crisis rules + enrichment. No API calls.
    python evals/run_evals.py --live     # adds conversation replay, vignettes, signal judging.

A live run is roughly 130 API calls (104 replayed hero turns, 12 vignettes, 12 judgements).
Use --limit or --case while iterating.

A known limitation of transcript replay: the scripted hero cannot adapt to whatever the
companion actually asked, so a hero turn sometimes answers a question that was not put. Topic
coverage therefore measures whether the engine still drives the arc under imperfect input,
which is the property worth testing, rather than conversational realism.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The golden dataset's expected context types are pinned to the committed fixtures, not to
# whatever release happens to be sitting in data/. Set before importing enrichment.
os.environ.setdefault("DAYLIGHT_CHR_PATH", str(REPO_ROOT / "data/samples/chr_sample.csv"))
os.environ.setdefault(
    "DAYLIGHT_ZIP_COUNTY_PATH", str(REPO_ROOT / "data/samples/zip_county_sample.csv")
)

from src import enrichment, safety, vignette  # noqa: E402
from src.conversation import (  # noqa: E402
    MODEL,
    TOPIC_KEYS,
    ConversationEngine,
    ConversationState,
)

DATASET_PATH = Path(__file__).parent / "golden_dataset.json"

JUDGE_PROMPT = """You are scoring a mental health intake system's extraction of clinical signals.

You will be given the conversation transcript, the signals a human author expected the system
to surface, and the signals the system actually produced.

For each expected signal, decide whether the produced list captures it. A match means the same
clinical fact is present, in any wording, at any level of detail. Paraphrase matches. A produced
signal that covers the fact as part of a broader statement matches. A produced signal about a
different fact does not match, however plausible it is.

Separately, check every produced signal against the transcript. A produced signal is fabricated
only if the transcript does not support it — not merely because the expected list omits it. A
signal the author did not anticipate but the hero clearly said is correct, not fabricated.

Be strict about facts and generous about wording."""

JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "expected": {"type": "string"},
                    "matched": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["expected", "matched", "reason"],
                "additionalProperties": False,
            },
        },
        "fabricated": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Produced signals asserting something the hero never said. Empty if none."
            ),
        },
    },
    "required": ["matches", "fabricated"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Result plumbing
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    kind: str
    checks: dict = field(default_factory=dict)   # metric -> bool | None (None = not run)
    detail: dict = field(default_factory=dict)

    def mark(self, metric: str, passed: bool | None, **detail) -> None:
        self.checks[metric] = passed
        if detail:
            self.detail.setdefault(metric, {}).update(detail)


METRICS = ("crisis_detection", "community_context", "topic_coverage", "signal_extraction")


# ---------------------------------------------------------------------------
# Offline metrics
# ---------------------------------------------------------------------------


def check_crisis_rules(case: dict, result: CaseResult) -> None:
    """Run the deterministic rule layer over every hero turn.

    This is the layer that still works with no API key and no network, so it is held to the
    full standard: it must fire on the crisis case and on nothing else. The reflective cases
    are the real test — case_009 contains the words "plan" and "got close" and must still not
    escalate.
    """
    escalations = []
    reflective_turns = []
    for index, turn in enumerate(case["conversation"], start=1):
        assessment = safety.screen_rules(turn["content"])
        if assessment.escalate:
            escalations.append({"turn": index, "triggers": assessment.triggers,
                                "evidence": assessment.evidence})
        if assessment.reflective_only:
            reflective_turns.append(index)

    fired = bool(escalations)
    result.mark(
        "crisis_detection",
        fired == case["expected_crisis_flag"],
        expected=case["expected_crisis_flag"],
        rules_fired=fired,
        escalations=escalations,
        reflective_turns=reflective_turns,
    )


def check_community_context(case: dict, result: CaseResult) -> None:
    context = enrichment.lookup(case.get("zip_code"))
    actual = context["context_type"]
    result.mark(
        "community_context",
        actual == case["expected_context_type"],
        expected=case["expected_context_type"],
        actual=actual,
        county=context.get("county"),
    )


# ---------------------------------------------------------------------------
# Live metrics
# ---------------------------------------------------------------------------


def replay(case: dict) -> tuple[ConversationState, list[dict]]:
    """Drive the real engine through a case's hero turns.

    Stops at a crisis escalation, exactly as the app does — a crisis case is supposed to end
    with an incomplete arc, and continuing to feed it turns would measure something the
    product never does.
    """
    engine = ConversationEngine(state=ConversationState())
    trace: list[dict] = []
    for index, turn in enumerate(case["conversation"], start=1):
        result = engine.respond(turn["content"])
        trace.append(
            {
                "turn": index,
                "hero": turn["content"],
                "companion": result.message,
                "topic_after": engine.state.current_topic,
                "topic_status": result.topic_status,
                "crisis_mode": result.crisis_mode,
                "error": result.error,
            }
        )
        if result.crisis_mode or result.should_close:
            break
    return engine.state, trace


def check_topic_coverage(case: dict, state: ConversationState, result: CaseResult) -> None:
    """Did the engine reach the topics this case expects?

    Scored as a superset test rather than exact equality: reaching more of the arc than
    expected is never a failure, and the sparse and crisis cases legitimately expect few.
    """
    expected = set(case["expected_topics_covered"])
    covered = set(state.topics_covered)
    result.mark(
        "topic_coverage",
        expected.issubset(covered),
        expected=sorted(expected),
        covered=sorted(covered),
        missing=sorted(expected - covered),
        full_arc=len(covered) == len(TOPIC_KEYS),
    )


def check_signals(
    case: dict, record: dict, state: ConversationState, result: CaseResult, client
) -> None:
    """Claude-judged recall of the expected signals, plus a fabrication check.

    Keyword overlap was the cheaper option and a bad one: "stopped playing guitar" and
    "anhedonia with specific onset" share no words and are the same finding. The judge is
    told to be strict about facts and generous about wording.

    The transcript goes to the judge as well. Fabrication cannot be assessed from the
    expected and produced lists alone — without the source, a correct signal the dataset
    author simply did not anticipate looks identical to an invented one.
    """
    produced = record.get("key_signals") or []
    expected = case["expected_key_signals"]

    if not produced:
        result.mark("signal_extraction", False, recall=0.0, produced=[],
                    note="no signals produced")
        return

    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=JUDGE_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "TRANSCRIPT:\n"
                    + "\n".join(
                        f"{'HERO' if m['role'] == 'user' else 'COMPANION'}: {m['content']}"
                        for m in state.history
                    )
                    + "\n\nEXPECTED SIGNALS:\n"
                    + "\n".join(f"- {s}" for s in expected)
                    + "\n\nPRODUCED SIGNALS:\n"
                    + "\n".join(f"- {s}" for s in produced)
                ),
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
    )
    payload = next(json.loads(b.text) for b in response.content if b.type == "text")
    matches = payload.get("matches", [])
    matched = [m for m in matches if m.get("matched")]
    recall = len(matched) / len(expected) if expected else 0.0

    # The vignette caps at 5 signals by design, so perfect recall against a 7-item expected
    # list is not achievable. The bar is majority recall with nothing fabricated — a
    # fabricated clinical fact in a provider handoff is a hard fail regardless of recall.
    fabricated = payload.get("fabricated", [])
    result.mark(
        "signal_extraction",
        recall >= 0.5 and not fabricated,
        recall=round(recall, 2),
        matched=[m["expected"] for m in matched],
        missed=[m["expected"] for m in matches if not m.get("matched")],
        fabricated=fabricated,
        produced=produced,
    )


def check_crisis_live(case: dict, state: ConversationState, result: CaseResult) -> None:
    """Combined rule + model crisis outcome from the actual replay."""
    result.mark(
        "crisis_detection",
        state.crisis_flag == case["expected_crisis_flag"],
        expected=case["expected_crisis_flag"],
        actual=state.crisis_flag,
        events=state.safety_events,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(cases: list[dict], live: bool, keep_traces: Path | None) -> list[CaseResult]:
    client = None
    if live:
        import anthropic

        client = anthropic.Anthropic()

    results: list[CaseResult] = []
    for case in cases:
        result = CaseResult(case_id=case["case_id"], kind=case["kind"])
        print(f"  {case['case_id']} ({case['kind']})… ", end="", flush=True)

        check_community_context(case, result)
        check_crisis_rules(case, result)

        if live:
            state, trace = replay(case)
            check_topic_coverage(case, state, result)
            check_crisis_live(case, state, result)  # overrides the rules-only verdict
            record = vignette.generate(state, client=client)
            check_signals(case, record, state, result, client)
            if keep_traces:
                keep_traces.mkdir(parents=True, exist_ok=True)
                (keep_traces / f"{case['case_id']}.json").write_text(
                    json.dumps({"trace": trace, "vignette": record}, indent=2)
                )
        else:
            result.mark("topic_coverage", None)
            result.mark("signal_extraction", None)

        marks = "".join(
            {True: "✓", False: "✗", None: "·"}[result.checks.get(m)] for m in METRICS
        )
        print(marks)
        results.append(result)
    return results


def report(results: list[CaseResult], live: bool) -> bool:
    print()
    print("=" * 78)
    print(f"{'case':10} {'kind':15} " + " ".join(f"{m[:13]:>14}" for m in METRICS))
    print("-" * 78)
    for result in results:
        row = " ".join(
            f"{ {True: 'pass', False: 'FAIL', None: '–'}[result.checks.get(m)] :>14}"
            for m in METRICS
        )
        print(f"{result.case_id:10} {result.kind:15} {row}")

    print("-" * 78)
    all_passed = True
    for metric in METRICS:
        scored = [r for r in results if r.checks.get(metric) is not None]
        if not scored:
            print(f"{metric:22} not run")
            continue
        passed = sum(1 for r in scored if r.checks[metric])
        rate = passed / len(scored)
        all_passed = all_passed and passed == len(scored)
        print(f"{metric:22} {passed}/{len(scored)}  {rate:.0%}")

    if live:
        coverage = [
            len(r.detail["topic_coverage"]["covered"]) / len(TOPIC_KEYS)
            for r in results
            if "topic_coverage" in r.detail
        ]
        if coverage:
            print(f"{'mean arc coverage':22} {sum(coverage) / len(coverage):.0%} of 7 topics")
        recalls = [
            r.detail.get("signal_extraction", {}).get("recall")
            for r in results
            if r.detail.get("signal_extraction", {}).get("recall") is not None
        ]
        if recalls:
            print(f"{'mean signal recall':22} {sum(recalls) / len(recalls):.0%}")
        fabrications = [
            (r.case_id, f)
            for r in results
            for f in r.detail.get("signal_extraction", {}).get("fabricated", [])
        ]
        if fabrications:
            print("\nFABRICATED SIGNALS (hard fail — a provider would read these as fact):")
            for case_id, text in fabrications:
                print(f"  {case_id}: {text}")

    print("=" * 78)

    failures = [
        (r.case_id, m) for r in results for m in METRICS if r.checks.get(m) is False
    ]
    if failures:
        print("\nFailures:")
        for case_id, metric in failures:
            detail = next(r for r in results if r.case_id == case_id).detail.get(metric, {})
            print(f"  {case_id} / {metric}: {json.dumps(detail, default=str)[:300]}")
    return all_passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="replay conversations and generate vignettes (costs API calls)")
    parser.add_argument("--case", action="append", default=None,
                        help="run only this case_id (repeatable)")
    parser.add_argument("--kind", action="append", default=None,
                        help="run only cases of this kind (repeatable)")
    parser.add_argument("--limit", type=int, default=None, help="run at most N cases")
    parser.add_argument("--traces", type=Path, default=None,
                        help="directory to write replay traces and vignettes into")
    parser.add_argument("--json", type=Path, default=None, help="write full results as JSON")
    args = parser.parse_args()

    dataset = json.loads(DATASET_PATH.read_text())
    cases = dataset["cases"]
    if args.case:
        cases = [c for c in cases if c["case_id"] in set(args.case)]
    if args.kind:
        cases = [c for c in cases if c["kind"] in set(args.kind)]
    if args.limit:
        cases = cases[: args.limit]

    if not cases:
        print("no cases selected")
        return 2

    mode = "LIVE (replay + vignettes + judge)" if args.live else "OFFLINE (rules + enrichment)"
    print(f"daylight evals — {mode}")
    print(f"{len(cases)} case(s) · model {MODEL}\n")
    print(f"legend: {' '.join(METRICS)}\n")

    results = run(cases, live=args.live, keep_traces=args.traces)
    passed = report(results, live=args.live)

    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {"case_id": r.case_id, "kind": r.kind, "checks": r.checks,
                     "detail": r.detail}
                    for r in results
                ],
                indent=2,
                default=str,
            )
        )
        print(f"\nwrote {args.json}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
