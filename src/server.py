"""Build step 3: relay Twilio Media Stream <-> OpenAI Realtime.

The whole program is a bidirectional relay between two WebSockets:

    Twilio WS  <--- relay --->  OpenAI Realtime WS

  GET/POST /twiml        -> TwiML: connect the call's audio to /media-stream
  WS       /media-stream -> for each call, open an OpenAI Realtime session and
                            shuttle audio both ways

Both sides speak G.711 mu-law @ 8kHz, so no resampling is needed. Every OpenAI
event type is logged (including `error`) so we can correct the session schema
from real API feedback.

Run:
    ./.venv/bin/python -m uvicorn server:app --app-dir src --host 0.0.0.0 --port 5050
"""

import asyncio
import base64
import hmac
import json
import logging
import os
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import certifi
import websockets
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client

load_dotenv()

PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].strip().rstrip("/")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

# REST client used to hang up the call and fetch the recording.
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Background tasks (recording fetch + review) kept referenced so they aren't GC'd.
_bg_tasks: set = set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)

# --- Auth ---
# 1) /twiml: verify the request was signed by Twilio (HMAC keyed by the Auth Token).
# 2) /media-stream: require a secret that we only ever hand out inside a signed
#    /twiml response, checked before we open any billable OpenAI session.
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
STREAM_SECRET = os.environ.get("STREAM_SECRET", "")
if not STREAM_SECRET:
    raise SystemExit(
        "STREAM_SECRET is not set. Add a high-entropy value to .env, e.g.\n"
        "  echo \"STREAM_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')\" >> .env"
    )

log = logging.getLogger("uvicorn.error")

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", "artifacts"))


def load_config() -> dict:
    """Read the tunable config. Re-read per call so the tuner's edits take effect."""
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


app = FastAPI()


@app.get("/health")
async def health():
    cfg = load_config()
    return {"ok": True, "public_base_url": PUBLIC_BASE_URL, "model": cfg.get("model")}


@app.post("/twiml")
async def twiml(request: Request):
    # Verify Twilio's signature over the exact URL it called + the POST fields.
    # Rebuild the URL from PUBLIC_BASE_URL (request.url shows the proxied host).
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"https://{PUBLIC_BASE_URL}/twiml"
    form = dict(await request.form())
    if not twilio_validator.validate(url, form, signature):
        log.warning("twiml: invalid Twilio signature from %s", request.client.host if request.client else "?")
        return Response(status_code=403)

    # Hand the stream secret to Twilio (and only Twilio) as a Stream parameter.
    ws_url = f"wss://{PUBLIC_BASE_URL}/media-stream"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="{ws_url}">'
        f'<Parameter name="token" value="{STREAM_SECRET}"/>'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )
    return PlainTextResponse(content=xml, media_type="text/xml")


def session_update_payload(cfg: dict) -> dict:
    """GA Realtime session config from the tunable config. mu-law in/out, server VAD."""
    vad = cfg["vad"]
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": cfg["persona"],
            "tools": [
                {
                    "type": "function",
                    "name": "end_call",
                    "description": (
                        "End the phone call. Call this immediately after you have "
                        "said goodbye and the conversation is complete."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "transcription": {"model": "whisper-1"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": vad["threshold"],
                        "prefix_padding_ms": vad["prefix_padding_ms"],
                        "silence_duration_ms": vad["silence_duration_ms"],
                    },
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": cfg["voice"],
                },
            },
        },
    }


# python.org's framework build ships without wired-up CA certs, so verify TLS
# against certifi's bundle explicitly.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


async def open_openai_ws(model: str):
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    return await websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        ssl=SSL_CONTEXT,
    )


def write_transcript(call_sid: str, cfg: dict, turns: list, barge_ins: int) -> dict | None:
    """Persist the conversation + metadata; return the transcript dict."""
    if not call_sid:
        return None
    out_dir = ARTIFACTS_DIR / call_sid
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "call_sid": call_sid,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "config": cfg,
        "barge_ins": barge_ins,
        "turns": turns,  # [{speaker, text, t}] in order
    }
    (out_dir / "transcript.json").write_text(json.dumps(data, indent=2))
    log.info("wrote transcript for %s (%d turns)", call_sid, len(turns))
    return data


async def fetch_recording(call_sid: str, dest: Path) -> bool:
    """Poll Twilio until the recording is processed, then download the WAV."""
    def _try() -> bool:
        recs = twilio_client.recordings.list(call_sid=call_sid, limit=1)
        if not recs or recs[0].status != "completed":
            return False
        url = (f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}"
               f"/Recordings/{recs[0].sid}.wav")
        auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, context=SSL_CONTEXT) as r:
            dest.write_bytes(r.read())
        return True

    for _ in range(30):  # up to ~60s
        try:
            if await asyncio.to_thread(_try):
                return True
        except Exception as e:
            log.error("recording fetch error for %s: %r", call_sid, e)
        await asyncio.sleep(2)
    return False


async def save_findings(call_sid: str, transcript: dict, out_dir: Path) -> None:
    """Always save a timestamped bug report. Skip if one already exists (a
    scenario-aware review from the runner/tuner wins)."""
    findings_path = out_dir / "findings.json"
    if findings_path.exists() or not os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        from reviewer import review_transcript  # lazy: server doesn't need Anthropic to run
        review = await asyncio.to_thread(review_transcript, transcript)
        findings = [f.model_dump() for f in review.agent_findings]
        ts = datetime.now(timezone.utc).isoformat()
        findings_path.write_text(json.dumps({
            "call_sid": call_sid, "reviewed_at": ts,
            "patient_overall": review.overall, "agent_findings": findings,
        }, indent=2))
        with open("FINDINGS.md", "a") as f:
            f.write(f"\n### {call_sid} — {ts}\n")
            f.write("- no issues found\n" if not findings else "")
            for x in findings:
                f.write(f"- **[{x['severity']}]** {x['issue']}\n  - evidence: \"{x['evidence']}\"\n")
        log.info("saved findings for %s (%d issues)", call_sid, len(findings))
    except Exception as e:
        log.error("review failed for %s: %r", call_sid, e)


async def finalize_call(call_sid: str, transcript: dict) -> None:
    """After every call: always save the recording, then a timestamped report."""
    out_dir = ARTIFACTS_DIR / call_sid
    ok = await fetch_recording(call_sid, out_dir / "recording.wav")
    log.info("recording %s for %s", "saved" if ok else "NOT saved", call_sid)
    await save_findings(call_sid, transcript, out_dir)


@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket):
    await twilio_ws.accept()
    cfg = load_config()  # snapshot config for this call
    log.info("WS: Twilio connected (model=%s)", cfg.get("model"))

    state = {"stream_sid": None, "call_sid": None}
    turns = []          # ordered [{speaker, text, t}]
    barge_ins = 0
    started = time.monotonic()

    def record(speaker: str, text: str) -> None:
        turns.append({"speaker": speaker, "text": text.strip(),
                      "t": round(time.monotonic() - started, 2)})

    async def hang_up():
        """Let the goodbye audio play, drop the PSTN call, then close OpenAI."""
        await asyncio.sleep(cfg["hangup_grace_seconds"])
        call_sid = state["call_sid"]
        if call_sid:
            try:
                await asyncio.to_thread(
                    lambda: twilio_client.calls(call_sid).update(status="completed")
                )
                log.info("hung up call %s", call_sid)
            except Exception as e:
                log.error("hangup failed: %r", e)
        await openai_ws.close()

    # --- auth gate ---
    # Read until the 'start' event and validate the stream secret BEFORE opening
    # any billable OpenAI session. An unauthenticated socket costs nothing.
    try:
        while True:
            data = json.loads(await twilio_ws.receive_text())
            event = data.get("event")
            if event == "connected":
                continue
            if event == "start":
                params = data["start"].get("customParameters") or {}
                if not hmac.compare_digest(params.get("token", ""), STREAM_SECRET):
                    log.warning("media-stream: missing/invalid stream token -> closing")
                    await twilio_ws.close(code=1008)
                    return
                state["stream_sid"] = data["start"]["streamSid"]
                state["call_sid"] = data["start"].get("callSid")
                log.info("WS: authed start streamSid=%s callSid=%s",
                         state["stream_sid"], state["call_sid"])
                break
            # media or anything else before an authenticated start -> reject
            log.warning("media-stream: '%s' before authenticated start -> closing", event)
            await twilio_ws.close(code=1008)
            return
    except WebSocketDisconnect:
        log.info("WS: Twilio disconnected before start")
        return

    try:
        openai_ws = await open_openai_ws(cfg["model"])
    except Exception as e:
        log.error("Could not open OpenAI WS: %r", e)
        await twilio_ws.close()
        return

    async with openai_ws:
        # Configure the session. The patient is the caller, so it waits for the
        # assistant (callee) to greet first — server VAD triggers its reply.
        await openai_ws.send(json.dumps(session_update_payload(cfg)))

        async def twilio_to_openai():
            """Forward the agent's audio up to OpenAI."""
            try:
                while True:
                    data = json.loads(await twilio_ws.receive_text())
                    event = data.get("event")
                    if event == "start":
                        state["stream_sid"] = data["start"]["streamSid"]
                        state["call_sid"] = data["start"].get("callSid")
                        log.info("WS: start streamSid=%s callSid=%s",
                                 state["stream_sid"], state["call_sid"])
                    elif event == "media":
                        # Twilio payload is already base64 mu-law; pass through.
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }))
                    elif event == "stop":
                        log.info("WS: Twilio stop")
                        break
            except WebSocketDisconnect:
                log.info("WS: Twilio disconnected")

        async def openai_to_twilio():
            """Forward the patient's audio down to Twilio; capture the conversation."""
            nonlocal barge_ins
            async for raw in openai_ws:
                evt = json.loads(raw)
                t = evt.get("type", "")

                if t == "response.output_audio.delta":
                    if state["stream_sid"]:
                        await twilio_ws.send_text(json.dumps({
                            "event": "media",
                            "streamSid": state["stream_sid"],
                            "media": {"payload": evt["delta"]},
                        }))
                elif t == "response.output_audio_transcript.done":
                    text = evt.get("transcript", "").strip()
                    log.info("PATIENT: %s", text)
                    record("patient", text)
                elif t == "conversation.item.input_audio_transcription.completed":
                    text = evt.get("transcript", "").strip()
                    log.info("AGENT:   %s", text)
                    record("agent", text)
                elif t == "response.function_call_arguments.done":
                    if evt.get("name") == "end_call":
                        log.info("PATIENT called end_call -> hanging up")
                        asyncio.create_task(hang_up())
                elif t == "input_audio_buffer.speech_started":
                    barge_ins += 1
                    log.info("(barge-in -> clear Twilio buffer)")
                    if state["stream_sid"]:
                        await twilio_ws.send_text(json.dumps({
                            "event": "clear",
                            "streamSid": state["stream_sid"],
                        }))
                elif t == "error":
                    log.error("OA ERROR: %s", json.dumps(evt))

        await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    transcript = write_transcript(state["call_sid"], cfg, turns, barge_ins)
    if transcript:
        # Always save the recording + a timestamped bug report, in the background
        # so we don't block teardown. Covers every call path (call.py, runner, tuner).
        _spawn(finalize_call(state["call_sid"], transcript))
    log.info("WS: session closed")
