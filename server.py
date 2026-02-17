import asyncio
import json
import os
import tempfile
from collections import deque
from pathlib import Path

from aiohttp import ClientSession, web
from dotenv import load_dotenv


load_dotenv()

API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8080"))
WHISPER_MODEL_PATH = os.getenv("WHISPER_MODEL_PATH", "models/ggml-base.en.bin")
WHISPER_COMMAND = os.getenv(
    "WHISPER_COMMAND",
    'whisper-cli -m "{whisper_model_path}" -f "{audio_path}" -otxt -of "{out_prefix}"',
)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://127.0.0.1:11434")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a concise assistant.")
CONTEXT_WINDOW_TURNS = int(os.getenv("CONTEXT_WINDOW_TURNS", "6"))
DEFAULT_SESSION_ID = os.getenv("DEFAULT_SESSION_ID", "default")


async def run_cmd(command: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="ignore"), stderr.decode(
        "utf-8", errors="ignore"
    )


def build_prompt(history: list[tuple[str, str]], user_text: str) -> str:
    prompt_lines = [SYSTEM_PROMPT, ""]
    for old_user, old_assistant in history:
        prompt_lines.append(f"User: {old_user}")
        prompt_lines.append(f"Assistant: {old_assistant}")
    prompt_lines.append(f"User: {user_text}")
    prompt_lines.append("Assistant:")
    return "\n".join(prompt_lines)


async def transcribe(audio_path: Path, workdir: Path) -> str:
    out_prefix = workdir / "stt_output"
    cmd = WHISPER_COMMAND.format(
        audio_path=audio_path,
        out_prefix=out_prefix,
        whisper_model_path=WHISPER_MODEL_PATH,
        workdir=workdir,
    )
    code, out, err = await run_cmd(cmd)
    if code != 0:
        model_msg = ""
        if "failed to open" in err.lower() or "no such file" in err.lower():
            model_msg = (
                f" (check WHISPER_MODEL_PATH='{WHISPER_MODEL_PATH}' on the remote server)"
            )
        raise RuntimeError(f"transcription failed: {err.strip()}{model_msg}")

    txt_path = Path(f"{out_prefix}.txt")
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8").strip()
    transcript = out.strip()
    if transcript:
        return transcript
    raise RuntimeError("transcription output file not found and stdout was empty")


async def run_llm_once(prompt: str) -> str:
    url = f"{OLLAMA_API_URL.rstrip('/')}/api/generate"
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    async with ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"llm failed with {resp.status}: {text}")
            data = await resp.json()
    if data.get("error"):
        raise RuntimeError(f"llm failed: {data['error']}")
    return str(data.get("response", "")).strip()


async def run_llm_stream(prompt: str):
    url = f"{OLLAMA_API_URL.rstrip('/')}/api/generate"
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": True}
    async with ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"llm failed with {resp.status}: {text}")
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("error"):
                    raise RuntimeError(f"llm failed: {data['error']}")
                token = str(data.get("response", ""))
                if token:
                    yield token
                if data.get("done"):
                    break


async def parse_audio_request(request: web.Request) -> tuple[str, bytes]:
    reader = await request.multipart()
    session_id = DEFAULT_SESSION_ID
    audio_bytes = b""

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "session_id":
            session_id = (await part.text()).strip() or DEFAULT_SESSION_ID
        elif part.name == "audio":
            chunks = []
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                chunks.append(chunk)
            audio_bytes = b"".join(chunks)

    if not audio_bytes:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "missing 'audio' file field"}),
            content_type="application/json",
        )
    return session_id, audio_bytes


def get_history(app: web.Application, session_id: str) -> deque[tuple[str, str]]:
    histories: dict[str, deque[tuple[str, str]]] = app["histories"]
    return histories.setdefault(session_id, deque(maxlen=max(1, CONTEXT_WINDOW_TURNS)))


async def chat_handler(request: web.Request) -> web.Response:
    try:
        session_id, audio_bytes = await parse_audio_request(request)
    except web.HTTPException as exc:
        return web.Response(status=exc.status, text=exc.text, content_type=exc.content_type)

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        audio_path = workdir / "input.wav"
        audio_path.write_bytes(audio_bytes)

        try:
            transcript = await transcribe(audio_path, workdir)
            history = get_history(request.app, session_id)
            prompt = build_prompt(list(history), transcript)
            response_text = await run_llm_once(prompt)
            history.append((transcript, response_text))
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    return web.json_response(
        {"transcript": transcript, "response": response_text, "session_id": session_id}
    )


async def chat_stream_handler(request: web.Request) -> web.StreamResponse:
    try:
        session_id, audio_bytes = await parse_audio_request(request)
    except web.HTTPException as exc:
        return web.Response(status=exc.status, text=exc.text, content_type=exc.content_type)

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        audio_path = workdir / "input.wav"
        audio_path.write_bytes(audio_bytes)

        try:
            transcript = await transcribe(audio_path, workdir)
            await resp.write(
                f"data: {json.dumps({'type': 'transcript', 'text': transcript})}\n\n".encode(
                    "utf-8"
                )
            )

            history = get_history(request.app, session_id)
            prompt = build_prompt(list(history), transcript)
            chunks: list[str] = []
            async for chunk in run_llm_stream(prompt):
                chunks.append(chunk)
                await resp.write(
                    f"data: {json.dumps({'type': 'token', 'text': chunk})}\n\n".encode(
                        "utf-8"
                    )
                )

            response_text = "".join(chunks).strip()
            history.append((transcript, response_text))
            await resp.write(
                f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n".encode(
                    "utf-8"
                )
            )
        except Exception as exc:
            await resp.write(
                f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n".encode(
                    "utf-8"
                )
            )

    await resp.write_eof()
    return resp


def main() -> None:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app["histories"] = {}
    app.router.add_post("/chat", chat_handler)
    app.router.add_post("/chat/stream", chat_stream_handler)
    web.run_app(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    main()
