# Patient Simulator

A synthetic patient that phones a healthcare voice assistant, holds a real
conversation, and records what happens — so you can find bugs in the assistant.

It also tunes itself: after each call, Claude reads the transcript and a GPT
audio model listens to the recording, then they rewrite the patient's script to
sound more human. This can run automatically on GitHub Actions.

**Outputs:** call recordings + transcripts, saved per call.

