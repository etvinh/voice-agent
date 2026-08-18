## Architecture


- A `runner` places a real outbound phone call through **Twilio** for each scenario.

- Twilio streams the call's audio over a WebSocket to a small **FastAPI relay** (`server.py`).

- `server.py` forwards the agent-under-test's audio up to the **OpenAI Realtime API**, and streams the synthetic patient's speech-to-speech reply back down to Twilio.

- Every call is captured to disk as a `.wav` recording and a timestamped transcript.

- Evaluation is two-model, split by what each model can actually judge:
  - **Claude** reads the transcript and flags bugs in the agent's responses against each scenario's `must_happen` / `must_not_happen` oracles.
  - a **GPT audio model** *listens* to the recording, scores how human the patient sounds, and edits the persona prompt.

## Key design choices

- A **tuning stage** improves the shared `base_persona` (every scenario builds on it), so the caller communicates effectively with the PrettyGoodAI agent.

- **Different models for different jobs** — speech-to-speech for the call, Claude for bug-finding, GPT-audio for realism — to get the best output and iterate on my own agent.

- **Authentication on the public endpoints** so nobody with the tunnel URL can run up my bills.

- **Automation** created a workflow with github actions that in the future can be used to run scenarios and tune prompts automatically.
