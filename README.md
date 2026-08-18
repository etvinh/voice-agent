# Patient Simulator

A synthetic patient that phones a healthcare voice assistant, holds a real
conversation, and records what happens — so you can find bugs in the assistant.

It also tunes itself: after each call, Claude reads the transcript and a GPT
audio model listens to the recording, then they rewrite the patient's script to
sound more human. This can run automatically on GitHub Actions.

**Outputs:** per call — `recording.wav`, `transcript.json`, `findings.json`; plus a
consolidated `FINDINGS.md` bug report. Full design + decision log is in `DESIGN.md`.

## Setup (one time)

```bash
# 1. Python env + dependencies
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# 2. Tunnel (Twilio needs a public URL to reach the local server)
brew install cloudflared

# 3. Keys
cp .env.example .env          # then fill in OpenAI, Anthropic, and Twilio values
python3 -c "import secrets; print('STREAM_SECRET='+secrets.token_urlsafe(32))" >> .env
```

You need: a **paid Twilio** account + voice number, **OpenAI Realtime** API access,
an **Anthropic** API key, and a `TARGET_AGENT_NUMBER` you're authorized to call and record.

## Run

One command — it starts the tunnel + server, runs the scenarios, tears down, and
writes the report:

```bash
./run.sh                                 # all scenarios  -> FINDINGS.md
./run.sh scenarios/11_*.yaml             # only specific scenarios
```

Results: **`FINDINGS.md`** (bugs found, grouped by category) and per-call artifacts
under `artifacts/<call_sid>/`.

In CI: push to GitHub, then **Actions → run-scenario-suite → Run workflow**
(optionally list scenario files); download the `findings-<run_id>` artifact.

Optional loops — same one command, different mode (each stands up its own tunnel + server):

```bash
./run.sh tune                            # make the caller sound more human
./run.sh attack                          # evolve a red-team attacker for security bugs
```


