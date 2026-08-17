"""Auto-tuning loop: call -> review -> edit config -> repeat.

Each iteration places a few real calls with the current config, has Claude review
the transcripts (src/reviewer.py), and hill-climbs on the score by editing
config.yaml. Improvements are committed to the current git branch. Hard budget
and iteration caps keep an unattended run from burning money.

Env knobs (all optional):
    TUNE_MAX_ITERATIONS   default 4
    TUNE_CALLS_PER_ITER   default 2
    TUNE_MAX_CALLS        default 12   (hard budget across the whole run)
    TUNE_SCORE_THRESHOLD  default 90   (stop once best avg overall >= this)
    TUNE_TRANSCRIPT_WAIT  default 240  (seconds to wait for a call to finish)

    ./.venv/bin/python src/tuner.py
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import yaml
from dotenv import load_dotenv
from twilio.rest import Client

from audio_reviewer import audio_review
from reviewer import review_transcript

load_dotenv()

# How much the gpt-audio "does it sound human" score counts toward the combined
# score the tuner optimizes. The rest is Claude's text/content/role review.
AUDIO_WEIGHT = float(os.environ.get("TUNE_AUDIO_WEIGHT", "0.4"))

CONFIG_PATH = Path("config.yaml")
ARTIFACTS_DIR = Path("artifacts")

MAX_ITERATIONS = int(os.environ.get("TUNE_MAX_ITERATIONS", "4"))
CALLS_PER_ITER = int(os.environ.get("TUNE_CALLS_PER_ITER", "2"))
MAX_CALLS = int(os.environ.get("TUNE_MAX_CALLS", "12"))
SCORE_THRESHOLD = int(os.environ.get("TUNE_SCORE_THRESHOLD", "90"))
TRANSCRIPT_WAIT = int(os.environ.get("TUNE_TRANSCRIPT_WAIT", "240"))

_calls_made = 0


# ---------- config io ----------

def _str_presenter(dumper, data):
    """Dump multi-line strings (the persona) as readable block scalars."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, _str_presenter)


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(yaml.dump(cfg, sort_keys=False, allow_unicode=True))


def apply_proposal(cfg: dict, proposal) -> dict:
    """Return a new config with the reviewer's proposed persona + VAD applied."""
    new = json.loads(json.dumps(cfg))  # deep copy
    new["persona"] = proposal.persona
    new["vad"]["threshold"] = float(proposal.vad_threshold)
    new["vad"]["silence_duration_ms"] = int(proposal.vad_silence_duration_ms)
    return new


# ---------- calling ----------

def place_call() -> str:
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    frm = os.environ["TWILIO_FROM_NUMBER"]
    to = os.environ["TARGET_AGENT_NUMBER"]
    base = os.environ["PUBLIC_BASE_URL"].strip().rstrip("/")
    call = Client(sid, token).calls.create(
        to=to, from_=frm, url=f"https://{base}/twiml",
        record=True, recording_channels="dual",
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


def run_call() -> dict | None:
    """Place one call, wait for its transcript, and fetch the recording."""
    global _calls_made
    _calls_made += 1
    call_sid = place_call()
    print(f"  call {_calls_made}: {call_sid}")
    transcript = wait_for_transcript(call_sid)
    if transcript is None:
        print(f"  call {call_sid}: no transcript within {TRANSCRIPT_WAIT}s")
        return None
    # Save the dual-channel WAV alongside the transcript (best-effort).
    subprocess.run(["./.venv/bin/python", "src/fetch_recording.py", call_sid], check=False)
    return transcript


# ---------- scoring ----------

def combined_score(text_overall: int, audio: dict | None) -> float:
    """Blend Claude's text review with the gpt-audio 'sounds human' score."""
    if audio and audio.get("audio_overall") is not None:
        return (1 - AUDIO_WEIGHT) * text_overall + AUDIO_WEIGHT * audio["audio_overall"]
    return float(text_overall)


def score_config():
    """Run CALLS_PER_ITER calls, review text + audio, return (avg_combined, best_proposal)."""
    scored = []  # [(combined, proposed_config)]
    for _ in range(CALLS_PER_ITER):
        if _calls_made >= MAX_CALLS:
            break
        transcript = run_call()
        if transcript is None:
            continue
        review = review_transcript(transcript)
        record_agent_findings(transcript["call_sid"], review)
        wav = ARTIFACTS_DIR / transcript["call_sid"] / "recording.wav"
        audio = audio_review(wav)  # None if missing/errored -> text-only
        combined = combined_score(review.overall, audio)
        a = audio.get("audio_overall") if audio else "n/a"
        print(f"    text={review.overall} (role={review.role_fidelity} disc={review.disclosure_discipline} "
              f"turn={review.turn_taking} coh={review.coherence})  audio={a}  -> combined={combined:.1f}")
        scored.append((combined, review.proposed_config))
    if not scored:
        return None, None
    avg = mean(c for c, _ in scored)
    worst_proposal = min(scored, key=lambda s: s[0])[1]  # improve where it hurts most
    return avg, worst_proposal


# ---------- git ----------

def commit_config(message: str) -> None:
    subprocess.run(["git", "add", "config.yaml", "TUNING_LOG.md"], check=False)
    subprocess.run(["git", "commit", "-m", message], check=False)


def log_line(text: str) -> None:
    with open("TUNING_LOG.md", "a") as f:
        f.write(text + "\n")


def record_agent_findings(call_sid: str, review) -> None:
    """Save bugs found in the agent under test — the actual deliverable."""
    findings = [f.model_dump() for f in review.agent_findings]
    ts = datetime.now(timezone.utc).isoformat()
    (ARTIFACTS_DIR / call_sid / "findings.json").write_text(json.dumps({
        "call_sid": call_sid, "reviewed_at": ts, "agent_findings": findings,
    }, indent=2))
    if findings:
        with open("FINDINGS.md", "a") as f:
            f.write(f"\n### {call_sid} — {ts}\n")
            for x in findings:
                f.write(f"- **[{x['severity']}]** {x['issue']}\n"
                        f"  - evidence: \"{x['evidence']}\"\n")
        print(f"    agent bugs found: {len(findings)}")


# ---------- loop ----------

def main() -> None:
    best_cfg = load_config()
    print("Scoring baseline config...")
    best_score, proposal = score_config()
    if best_score is None:
        raise SystemExit("No calls completed — is the server + tunnel up and the target dialable?")
    log_line(f"## Tuning run\n- baseline avg overall: **{best_score:.1f}** "
             f"({_calls_made} calls)")
    commit_config(f"tune: baseline avg={best_score:.1f}")

    for i in range(1, MAX_ITERATIONS + 1):
        if best_score >= SCORE_THRESHOLD:
            print(f"Reached threshold {SCORE_THRESHOLD}; stopping.")
            break
        if _calls_made >= MAX_CALLS:
            print(f"Hit call budget ({MAX_CALLS}); stopping.")
            break
        if proposal is None:
            break

        print(f"\nIteration {i}: applying proposed config and re-scoring...")
        candidate = apply_proposal(best_cfg, proposal)
        save_config(candidate)
        cand_score, cand_proposal = score_config()
        if cand_score is None:
            save_config(best_cfg)  # revert
            break

        if cand_score > best_score:
            print(f"  improved {best_score:.1f} -> {cand_score:.1f} (accepted)")
            log_line(f"- iter {i}: **{best_score:.1f} -> {cand_score:.1f}** accepted")
            best_cfg, best_score, proposal = candidate, cand_score, cand_proposal
            commit_config(f"tune: iter {i} improved to avg={cand_score:.1f}")
        else:
            print(f"  regressed {best_score:.1f} -> {cand_score:.1f} (reverted)")
            log_line(f"- iter {i}: {best_score:.1f} -> {cand_score:.1f} reverted")
            save_config(best_cfg)  # regression guard
            proposal = cand_proposal  # try the newest idea next round

    save_config(best_cfg)  # ensure the best config is what's on disk
    print(f"\nDone. Best avg overall: {best_score:.1f} after {_calls_made} calls.")


if __name__ == "__main__":
    main()
