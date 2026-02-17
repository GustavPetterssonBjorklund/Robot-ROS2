import asyncio
import os
import tempfile
from pathlib import Path

import aiohttp
import sounddevice as sd
import soundfile as sf
from dotenv import load_dotenv


load_dotenv()

REMOTE_API_URL = os.getenv("REMOTE_API_URL", "http://127.0.0.1:8080/chat")
AUDIO_SECONDS = int(os.getenv("AUDIO_SECONDS", "5"))
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))


def record_to_wav(path: Path, seconds: int, sample_rate: int) -> None:
    audio = sd.rec(int(seconds * sample_rate), samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    sf.write(str(path), audio, sample_rate)


async def send_audio(path: Path) -> dict:
    data = aiohttp.FormData()
    with path.open("rb") as f:
        data.add_field("audio", f, filename="input.wav", content_type="audio/wav")
        async with aiohttp.ClientSession() as session:
            async with session.post(REMOTE_API_URL, data=data) as resp:
                payload = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(payload.get("error", f"server returned {resp.status}"))
                return payload


async def main() -> None:
    print(f"Remote API: {REMOTE_API_URL}")
    print(f"Recording seconds: {AUDIO_SECONDS}")
    print("Press Enter to record, q to quit.")

    while True:
        user_input = input("> ").strip().lower()
        if user_input == "q":
            break

        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "input.wav"
            print("Recording...")
            record_to_wav(wav_path, AUDIO_SECONDS, SAMPLE_RATE)
            print("Sending audio to remote API...")
            try:
                result = await send_audio(wav_path)
            except Exception as exc:
                print(f"Error: {exc}")
                continue

            print(f"\nYou said: {result.get('transcript', '')}")
            print(f"Assistant: {result.get('response', '')}\n")


if __name__ == "__main__":
    asyncio.run(main())
