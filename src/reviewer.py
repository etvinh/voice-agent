"""Claude-powered review of a patient-simulator call.

Reads a transcript.json (written by server.py), scores the PATIENT bot's
performance against the persona goal, and proposes concrete edits to the tunable
config (persona text + VAD numbers). Returns structured output via
client.messages.parse so the tuner can apply changes without parsing prose.

    ./.venv/bin/python src/reviewer.py artifacts/CAxxxx/transcript.json
"""

import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

MODEL = "claude-opus-4-8"

RUBRIC = """You are grading a SYNTHETIC PATIENT bot that phoned a real medical
voice assistant. The patient should behave like a believable human caller. Score
each dimension 0-10 (10 = perfect):

- role_fidelity: Stayed a CALLER. Never acted like staff / receptionist, never
  said "how can I help you", never offered services.
- disclosure_discipline: Gave one fact per turn, answered only what was asked,
  never volunteered DOB / phone / reason unprompted.
- turn_taking: Waited for the assistant to finish; did not talk over it. Fewer
  barge-ins and no shredded assistant turns is better.
- coherence: Sounded like a natural, on-task phone call; adapted sensibly; ended
  cleanly.

For every deduction, quote the offending transcript line as evidence."""


class ProposedConfig(BaseModel):
    persona: str                 # full replacement persona text
    vad_threshold: float         # 0.0-1.0
    vad_silence_duration_ms: int  # ms of silence before the patient replies


class Review(BaseModel):
    role_fidelity: int
    disclosure_discipline: int
    turn_taking: int
    coherence: int
    overall: int                 # 0-100
    summary: str
    findings: list[str]          # each should quote transcript evidence
    changed: bool                # true if proposed_config differs meaningfully
    change_rationale: str        # why these specific edits should help
    proposed_config: ProposedConfig


def review_transcript(transcript: dict) -> Review:
    cfg = transcript.get("config", {})
    convo = "\n".join(f'{t["speaker"].upper()}: {t["text"]}' for t in transcript.get("turns", []))

    client = anthropic.Anthropic()
    prompt = f"""{RUBRIC}

CURRENT PATIENT PERSONA (the prompt that produced this call):
<persona>
{cfg.get("persona", "")}
</persona>

CURRENT VAD: threshold={cfg.get("vad", {}).get("threshold")}, \
silence_duration_ms={cfg.get("vad", {}).get("silence_duration_ms")}
BARGE-INS THIS CALL: {transcript.get("barge_ins")} (each is the patient starting \
to talk while the assistant was speaking; high counts mean choppy turn-taking).

TRANSCRIPT:
{convo or "(no turns captured)"}

Score the dimensions, then propose an IMPROVED config:
- proposed_config.persona: the full revised persona text. Keep what works; only
  change what the findings justify. Front-load the most important rules.
- proposed_config.vad_threshold and vad_silence_duration_ms: raise silence_ms
  (e.g. +150-300) and/or threshold if turn_taking suffered from barge-ins; leave
  them if turn-taking was clean.
Set changed=false and echo the current values if the call was already good."""

    resp = client.messages.parse(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
        output_format=Review,
    )
    return resp.parsed_output


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: reviewer.py <transcript.json>")
    transcript = json.loads(Path(sys.argv[1]).read_text())
    review = review_transcript(transcript)
    print(json.dumps(review.model_dump(), indent=2))


if __name__ == "__main__":
    main()
