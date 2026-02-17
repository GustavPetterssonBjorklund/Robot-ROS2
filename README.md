# Robot-ROS2

Simple voice-to-text + remote LLM text response demo, with your own async API.

## Architecture
1. `server.py` runs on your remote server.
2. It exposes an async HTTP API (`aiohttp`) endpoint: `POST /chat`.
3. The server runs:
- STT using `whisper-cli` (or equivalent command you configure).
- LLM response using `ollama run` (or equivalent command you configure).
4. `app.py` runs on your local machine:
- records microphone audio
- sends audio to the remote API
- prints transcript + LLM response

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

Configure `.env` (server section) and run on remote:
```bash
python server.py
```

## Local client setup
Configure `.env` (client section), then run locally:
```bash
python app.py
```

Press Enter to record, or type `q` to quit.

## API contract
`POST /chat` (multipart):
- file field: `audio` (WAV)

Response JSON:
```json
{
  "transcript": "user speech",
  "response": "assistant text"
}
```
