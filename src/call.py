"""Place a call that connects to our media-stream server.

Dials a number and points Twilio at our /twiml endpoint, which opens the
WebSocket media stream back to the running server.

    ./.venv/bin/python src/call.py +18315352318

Reads PUBLIC_BASE_URL from the environment (.env or an inline override).
"""

import os
import sys

from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()


def main() -> None:
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]
    base = os.environ["PUBLIC_BASE_URL"].strip().rstrip("/")

    to_number = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TARGET_AGENT_NUMBER")
    if not to_number:
        sys.exit("No destination. Pass a number as an argument or set TARGET_AGENT_NUMBER.")

    twiml_url = f"https://{base}/twiml"
    client = Client(sid, token)
    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=twiml_url,
        record=True,
        recording_channels="dual",  # assistant + patient on separate channels
    )

    print(f"Calling {to_number}")
    print(f"  twiml: {twiml_url}")
    print(f"  sid:   {call.sid}   status: {call.status}")
    print("Recording: ON (dual channel)")
    print(f"After the call ends, fetch the audio with:")
    print(f"  ./.venv/bin/python src/fetch_recording.py {call.sid}")


if __name__ == "__main__":
    main()
