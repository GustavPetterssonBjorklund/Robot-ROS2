import asyncio
import os
import tempfile
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


async def run_llm(user_text: str) -> str:
    prompt = f"{SYSTEM_PROMPT}\n\nUser: {user_text}\nAssistant:"
    escaped_prompt = prompt.replace('"', '\\"')
    cmd = f'ollama run {OLLAMA_MODEL} "{escaped_prompt}"'
    code, out, err = await run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"llm failed: {err.strip()}")
    return out.strip()


async def chat_handler(request: web.Request) -> web.Response:
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "audio":
        return web.json_response({"error": "missing 'audio' file field"}, status=400)

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        audio_path = workdir / "input.wav"
        with audio_path.open("wb") as f:
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                f.write(chunk)

        try:
            transcript = await transcribe(audio_path, workdir)
            response_text = await run_llm(transcript)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response({"transcript": transcript, "response": response_text})


def main() -> None:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_post("/chat", chat_handler)
    web.run_app(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    main()
