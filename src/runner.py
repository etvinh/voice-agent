"""Run the scenario suite against the agent under test and report bugs.

For each scenarios/*.yaml: compose (base_persona + scenario) into config, place a
real call, review the transcript against that scenario's must_happen /
must_not_happen, and collect the agent's bugs into a categorized FINDINGS report.

    ./.venv/bin/python src/runner.py                     # run the whole suite
    ./.venv/bin/python src/runner.py scenarios/05_*.yaml # run one scenario
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from twilio.rest import Client

from reviewer import review_transcript

load_dotenv()

CONFIG_PATH = Path("config.yaml")
ARTIFACTS_DIR = Path("artifacts")
SCENARIOS_DIR = Path("scenarios")
TRANSCRIPT_WAIT = int(os.environ.get("SUITE_TRANSCRIPT_WAIT", "240"))


def _str_presenter(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(str, _str_presenter)


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(yaml.dump(cfg, sort_keys=False, allow_unicode=True))


def compose_persona(base: str, scenario: dict) -> str:
    lines = [base.strip(), "",
             f"WHO YOU ARE: {scenario['character'].strip()}",
             f"WHAT YOU WANT: {scenario['goal'].strip()}"]
    facts = scenario.get("facts") or {}
    if facts:
        lines.append("WHAT YOU KNOW (give ONE item, only when that exact item is asked for):")
        lines += [f"- {k}: {v}" for k, v in facts.items()]
    twist = (scenario.get("twist") or "").strip()
    if twist:
        lines.append(f"COMPLICATION: {twist}")
    return "\n".join(lines)


def place_call() -> str:
    base = os.environ["PUBLIC_BASE_URL"].strip().rstrip("/")
    call = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]).calls.create(
        to=os.environ["TARGET_AGENT_NUMBER"],
        from_=os.environ["TWILIO_FROM_NUMBER"],
        url=f"https://{base}/twiml",
        record=True, recording_channels="dual",
        time_limit=180,             # 3-min hard cap
    )
    return call.sid


def wait_for_transcript(call_sid: str) -> dict | None:
    path = ARTIFACTS_DIR / call_sid / "transcript.json"
    deadline = time.time() + TRANSCRIPT_WAIT
    while time.time() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(3)
    return None


def run_scenario(scenario: dict, base_persona: str) -> tuple[dict, str] | None:
    cfg = load_config()
    cfg["persona"] = compose_persona(base_persona, scenario)
    save_config(cfg)

    print(f"\n▶ {scenario['id']} [{scenario['category']}]")
    call_sid = place_call()
    print(f"  call {call_sid}")
    transcript = wait_for_transcript(call_sid)
    if transcript is None:
        print(f"  no transcript within {TRANSCRIPT_WAIT}s — skipped")
        return None
    # (recording.wav is fetched by the server automatically — no need to re-fetch)

    review = review_transcript(transcript, scenario)
    (ARTIFACTS_DIR / call_sid / "findings.json").write_text(json.dumps({
        "call_sid": call_sid,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "scenario": scenario["id"],
        "category": scenario["category"],
        "patient_overall": review.overall,
        "agent_findings": [f.model_dump() for f in review.agent_findings],
    }, indent=2))
    print(f"  patient overall={review.overall}  agent bugs={len(review.agent_findings)}")
    return scenario, call_sid, review


def write_report(results: list) -> None:
    by_cat: dict[str, list] = {}
    for scenario, call_sid, review in results:
        by_cat.setdefault(scenario["category"], []).append((scenario, call_sid, review))

    lines = ["# Findings — agent under test",
             f"_generated {datetime.now(timezone.utc).isoformat()}_\n"]
    total = 0
    for cat, items in by_cat.items():
        lines.append(f"\n## {cat}\n")
        for scenario, call_sid, review in items:
            lines.append(f"### {scenario['id']}  (call `{call_sid}`)")
            if not review.agent_findings:
                lines.append("- no issues found\n")
                continue
            for f in review.agent_findings:
                total += 1
                lines.append(f"- **[{f.severity}]** {f.issue}")
                lines.append(f"  - evidence: \"{f.evidence}\"")
            lines.append("")
    lines.insert(1, f"\n{total} issue(s) across {len(results)} calls.\n")
    Path("FINDINGS.md").write_text("\n".join(lines))
    print(f"\nWrote FINDINGS.md — {total} issue(s) across {len(results)} calls.")


def main() -> None:
    files = [Path(a) for a in sys.argv[1:]] or sorted(SCENARIOS_DIR.glob("*.yaml"))
    original_persona = load_config().get("persona")
    results = []
    try:
        for f in files:
            scenario = yaml.safe_load(f.read_text())
            base = load_config()["base_persona"]
            out = run_scenario(scenario, base)
            if out:
                results.append(out)
    finally:
        cfg = load_config()             # always restore the persona we clobbered
        cfg["persona"] = original_persona
        save_config(cfg)

    if results:
        write_report(results)


if __name__ == "__main__":
    main()
