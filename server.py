import asyncio
import os
import shlex
import tempfile
from collections import deque
from pathlib import Path

from aiohttp import web
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


async def transcribe(audio_path: Path, workdir: Path) -> str:
    out_prefix = workdir / "stt_output"
    cmd = WHISPER_COMMAND.format(
        audio_path=audio_path,
        out_prefix=out_prefix,
        whisper_model_path=WHISPER_MODEL_PATH,
        workdir=workdir,
    )
    code, _, err = await run_cmd(cmd)
    if code != 0:
        model_msg = ""
        if "failed to open" in err.lower() or "no such file" in err.lower():
            model_msg = (
                f" (check WHISPER_MODEL_PATH='{WHISPER_MODEL_PATH}' on the remote server)"
            )
        raise RuntimeError(f"transcription failed: {err.strip()}{model_msg}")

    txt_path = Path(f"{out_prefix}.txt")
    if not txt_path.exists():
        raise RuntimeError("transcription output file not found")
    return txt_path.read_text(encoding="utf-8").strip()


async def run_llm(history: list[tuple[str, str]], user_text: str) -> str:
    prompt_lines = [SYSTEM_PROMPT, ""]
    for old_user, old_assistant in history:
        prompt_lines.append(f"User: {old_user}")
        prompt_lines.append(f"Assistant: {old_assistant}")
    prompt_lines.append(f"User: {user_text}")
    prompt_lines.append("Assistant:")
    prompt = "\n".join(prompt_lines)
    cmd = f"ollama run {shlex.quote(OLLAMA_MODEL)} {shlex.quote(prompt)}"
    code, out, err = await run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"llm failed: {err.strip()}")
    return out.strip()


async def chat_handler(request: web.Request) -> web.Response:
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
        return web.json_response({"error": "missing 'audio' file field"}, status=400)

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        audio_path = workdir / "input.wav"
        with audio_path.open("wb") as f:
            f.write(audio_bytes)

        try:
            transcript = await transcribe(audio_path, workdir)
            histories: dict[str, deque[tuple[str, str]]] = request.app["histories"]
            history = histories.setdefault(
                session_id, deque(maxlen=max(1, CONTEXT_WINDOW_TURNS))
            )
            response_text = await run_llm(list(history), transcript)
            history.append((transcript, response_text))
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response(
            {"transcript": transcript, "response": response_text, "session_id": session_id}
        )


def main() -> None:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app["histories"] = {}
    app.router.add_post("/chat", chat_handler)
    web.run_app(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    main()
