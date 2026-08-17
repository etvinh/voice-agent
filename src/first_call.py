"""Build step 1: prove outbound calling works.

Places a real PSTN call and plays a canned TwiML message. No ngrok, no media
stream — this isolates Twilio credentials + outbound dialing so any failure here
is unambiguous.

Usage:
    python src/first_call.py +14155550123     # call this number
    python src/first_call.py                   # falls back to TARGET_AGENT_NUMBER
"""

import os
import sys

from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

# A Twilio-hosted TwiML that speaks a line and plays a short clip. Lets us prove
# the call connects without standing up our own public URL yet.
DEMO_TWIML_URL = "http://demo.twilio.com/docs/voice.xml"


def main() -> None:
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]

    to_number = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TARGET_AGENT_NUMBER")
    if not to_number:
        sys.exit("No destination. Pass a number as an argument or set TARGET_AGENT_NUMBER.")

    client = Client(account_sid, auth_token)
    call = client.calls.create(to=to_number, from_=from_number, url=DEMO_TWIML_URL)

    print(f"Placed call to {to_number} from {from_number}")
    print(f"  call SID: {call.sid}")
    print(f"  status:   {call.status}")
    print("Answer the phone — you should hear a short spoken message.")


if __name__ == "__main__":
    main()
