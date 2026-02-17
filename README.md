# Robot-ROS2

Simple voice-to-text + remote LLM text response demo, with your own async API.

## Architecture
1. `server.py` runs on your remote server.
2. It exposes async HTTP API (`aiohttp`) endpoints:
- `POST /chat` (non-streaming)
- `POST /chat/stream` (SSE token streaming)
3. The server runs:
- STT using `whisper-cli` (or equivalent command you configure).
- LLM response using `ollama run` (or equivalent command you configure).
- Per-session context memory with a configurable context window.
4. `app.py` runs on your local machine:
- continuously listens to microphone input
- auto-detects speech/silence
- sends each utterance to the remote API
- prints transcript + streamed LLM tokens in realtime
- plays the LLM response with TTS

No external hosted API is required.

## Install
Create and activate a Python virtual environment, then install:
```bash
pip install -r requirements.txt
```

### Nix shell (recommended on NixOS/remote)
This repo includes `shell.nix` with:
- `ollama`
- Whisper binary (`whisper-cli` via `whisper-cpp`, or `whisper`)
- audio libs (`portaudio`, `libsndfile`)

Enter shell:
```bash
nix-shell
```

Then install Python deps:
```bash
pip install -r requirements.txt
```

Copy env template:
```bash
copy .env.example .env
```

## Remote server setup
On the remote machine, install model tools (example):
- `whisper-cli` for transcription
- `ollama` with your model pulled (example: `ollama pull llama3.1`)

Download a Whisper model on the remote machine (example):
```bash
mkdir -p models
curl -L https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin -o models/ggml-base.en.bin
```

Set in `.env` on remote:
```bash
WHISPER_MODEL_PATH=models/ggml-base.en.bin
WHISPER_COMMAND=whisper-cli -m "{whisper_model_path}" -f "{audio_path}" -otxt -of "{out_prefix}"
```

Configure `.env` (server section) and run on remote:
```bash
python server.py
```

## Local client setup
Configure `.env` (client section), then run locally:
```bash
python app.py
```

The client runs in continuous realtime mode (Ctrl+C to stop).

Useful client tuning env vars:
- `REALTIME_RMS_THRESHOLD`: mic sensitivity threshold (raise for noisy rooms).
- `REALTIME_SILENCE_SECONDS`: silence required before sending an utterance.
- `REALTIME_MIN_SPEECH_SECONDS`: minimum speech duration to accept an utterance.
- `REALTIME_BLOCK_MS`: capture block size in ms.
- `REALTIME_MAX_UTTERANCE_SECONDS`: force-close an utterance if silence is never detected.
- `REMOTE_STREAM_API_URL`: streaming endpoint (SSE).
- `SESSION_ID`: conversation id used for server-side context memory.
- `TTS_ENABLED`: enable/disable response speech.
- `TTS_RATE`: text-to-speech speaking rate.
- `TTS_VOICE_HINT`: optional substring to pick a voice by id/name.

Useful server context env vars:
- `CONTEXT_WINDOW_TURNS`: number of previous user/assistant turns kept per session.
- `DEFAULT_SESSION_ID`: fallback session id when the client does not provide one.
- `OLLAMA_API_URL`: local Ollama server URL (default `http://127.0.0.1:11434`).

## API contract
`POST /chat` (multipart):
- file field: `audio` (WAV)
- text field: `session_id` (optional, defaults to `DEFAULT_SESSION_ID`)

Response JSON:
```json
{
  "transcript": "user speech",
  "response": "assistant text"
}
```

`POST /chat/stream` (multipart, SSE):
- fields: same as `/chat`
- SSE `data` events:
  - `{"type":"transcript","text":"..."}`
  - `{"type":"token","text":"..."}`
  - `{"type":"done","session_id":"..."}`
