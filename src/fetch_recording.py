"""Download the dual-channel recording for a call.

Twilio finishes processing a recording a few seconds after the call ends, so we
poll until it's ready, then download the WAV.

    ./.venv/bin/python src/fetch_recording.py CAxxxxxxxx...

Saves to artifacts/{call_sid}/recording.wav
"""

import base64
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import certifi
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def download(url: str, sid: str, token: str, dest: Path) -> None:
    req = urllib.request.Request(url)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, context=SSL_CONTEXT) as r, open(dest, "wb") as f:
        f.write(r.read())


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: fetch_recording.py <call_sid>")
    call_sid = sys.argv[1]

    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    client = Client(account_sid, token)

    print(f"Waiting for recording of {call_sid} ...")
    recording = None
    for _ in range(30):  # up to ~60s
        recs = client.recordings.list(call_sid=call_sid, limit=1)
        if recs and recs[0].status == "completed":
            recording = recs[0]
            break
        time.sleep(2)

    if not recording:
        sys.exit("No completed recording found (yet). Try again in a few seconds.")

    out_dir = Path("artifacts") / call_sid
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "recording.wav"

    # Append .wav to the recording resource URL to get the media file.
    media_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording.sid}.wav"
    download(media_url, account_sid, token, dest)

    print(f"Saved {dest}")
    print(f"  channels: {recording.channels}  duration: {recording.duration}s")


if __name__ == "__main__":
    main()
