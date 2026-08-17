"""GPT audio-model review of a call recording.

Claude (reviewer.py) judges the transcript — content, role, disclosure. It cannot
hear the call. This module sends the actual dual-channel WAV to an OpenAI audio
model (gpt-audio) so a model literally LISTENS and scores how human the patient
sounds: prosody, pacing, robotic-ness, dead air, clipped goodbyes.

Returns a dict; None if the recording is missing or the call fails (the tuner
then falls back to the text-only score).

    ./.venv/bin/python src/audio_reviewer.py artifacts/CAxxxx/recording.wav
"""

import base64
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

AUDIO_MODEL = "gpt-audio"

PROMPT = """You are judging a SYNTHETIC PATIENT bot that phoned a medical clinic's
automated assistant. The recording has two voices: the clinic assistant and the
CALLER (a patient named Margaret trying to register and book an appointment).

Judge ONLY the CALLER's voice — how convincingly human it sounds on a phone call.
Ignore the assistant. Return ONLY a JSON object with these fields:
{
  "naturalness": 0-10,     // human-sounding prosody, intonation, warmth
  "pacing": 0-10,          // natural rhythm; not rushed, not robotic, no long dead air
  "turn_taking_sound": 0-10, // clean hand-offs; no talking over the assistant, no clipping
  "sounds_human": true/false,
  "audio_overall": 0-100,
  "issues": ["short phrases quoting what sounded off"]
}"""


def audio_review(wav_path: str | Path) -> dict | None:
    path = Path(wav_path)
    if not path.exists():
        return None
    try:
        data = base64.b64encode(path.read_bytes()).decode()
        client = OpenAI()
        resp = client.chat.completions.create(
            model=AUDIO_MODEL,
            modalities=["text"],  # gpt-audio doesn't support json_object response_format
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "input_audio", "input_audio": {"data": data, "format": "wav"}},
                ],
            }],
        )
        text = resp.choices[0].message.content
        match = re.search(r"\{.*\}", text, re.DOTALL)  # strip any prose/```json fences
        return json.loads(match.group(0)) if match else None
    except Exception as e:
        print(f"  audio review skipped: {e!r}")
        return None


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: audio_reviewer.py <recording.wav>")
    result = audio_review(sys.argv[1])
    print(json.dumps(result, indent=2) if result else "no result")


if __name__ == "__main__":
    main()
