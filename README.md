# Patient Simulator

A synthetic patient that phones a healthcare voice assistant, holds a real
conversation, and records what happens — so you can find bugs in the assistant.

It also tunes itself: after each call, Claude reads the transcript and a GPT
audio model listens to the recording, then they rewrite the patient's script to
sound more human. This can run automatically on GitHub Actions.

**Outputs:** call recordings + transcripts, saved per call.

## Architecture

A `runner` places a real outbound phone call through **Twilio** for each scenario. Twilio streams the call's audio over a WebSocket to a small **FastAPI relay** (`server.py`), which sits between two sockets: it forwards the agent-under-test's audio up to the **OpenAI Realtime API** (the synthetic patient, driven by a YAML persona) and streams the patient's speech-to-speech reply back down to Twilio, handling barge-in, a hard call-time cap, and hang-up via a registered `end_call` tool. Every call is captured to disk as a dual-channel recording plus a timestamped transcript. Evaluation is two-model and deliberately split by what each model can judge: **Claude** reads the transcript and flags bugs in the agent's responses against each scenario's `must_happen` / `must_not_happen` oracles (PHI-before-verification, missed emergencies, prompt-injection leaks, etc.), while a **GPT audio model** *listens* to the recording and scores how human the patient actually sounds. Scenarios live as data (`scenarios/*.yaml` = character, goal, facts, twist, oracles) composed with a shared, tunable `base_persona`, so adding a test is a data change, not a code change.

The key design choices all follow from one constraint: the top requirement is a *coherent-sounding phone call*, so we chose **speech-to-speech (Realtime)** over a cascaded ASR→LLM→TTS pipeline to get sub-second turn-taking and natural prosody, and **Twilio Media Streams** over the SIP connector specifically so the audio passes through our own process — that's what makes independent recording, timestamping, latency measurement, and endpoint auth possible. Because that relay is exposed on a public tunnel, both endpoints are authenticated (Twilio request-signature on the TwiML webhook + a secret validated before any billable OpenAI session opens) so a stranger with the URL can't drain the API key. The self-tuning loop optimizes the shared `base_persona` (blending Claude's transcript score with the GPT-audio "sounds human" score) so a better caller propagates to every scenario, while a separate red-team loop optimizes an *attacker* persona to maximize security findings — the same machinery pointed at opposite objectives. Hard call/iteration/budget caps and manual-only CI triggers keep an unattended run from running away on cost.

