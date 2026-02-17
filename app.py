import asyncio
import audioop
import io
import os
import queue
import wave
from collections import deque

import aiohttp
import pyttsx3
import sounddevice as sd
from dotenv import load_dotenv


load_dotenv()

REMOTE_API_URL = os.getenv("REMOTE_API_URL", "http://127.0.0.1:8080/chat")
SESSION_ID = os.getenv("SESSION_ID", "default")
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
BLOCK_MS = int(os.getenv("REALTIME_BLOCK_MS", "30"))
RMS_THRESHOLD = int(os.getenv("REALTIME_RMS_THRESHOLD", "450"))
MIN_SPEECH_SECONDS = float(os.getenv("REALTIME_MIN_SPEECH_SECONDS", "0.35"))
SILENCE_SECONDS = float(os.getenv("REALTIME_SILENCE_SECONDS", "0.8"))
PRE_ROLL_SECONDS = float(os.getenv("REALTIME_PRE_ROLL_SECONDS", "0.2"))
TTS_ENABLED = os.getenv("TTS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
TTS_RATE = int(os.getenv("TTS_RATE", "185"))
TTS_VOICE_HINT = os.getenv("TTS_VOICE_HINT", "").strip().lower()


def init_tts_engine() -> pyttsx3.Engine | None:
    if not TTS_ENABLED:
        return None
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", TTS_RATE)
        if TTS_VOICE_HINT:
            for voice in engine.getProperty("voices"):
                voice_blob = f"{voice.id} {voice.name}".lower()
                if TTS_VOICE_HINT in voice_blob:
                    engine.setProperty("voice", voice.id)
                    break
        return engine
    except Exception as exc:
        print(f"TTS disabled due to init error: {exc}")
        return None


async def speak_text(engine: pyttsx3.Engine | None, text: str) -> None:
    if not engine or not text.strip():
        return

    def _speak() -> None:
        engine.say(text)
        engine.runAndWait()

    try:
        await asyncio.to_thread(_speak)
    except Exception as exc:
        print(f"TTS error: {exc}")


async def send_wav_bytes(session: aiohttp.ClientSession, wav_bytes: bytes) -> dict:
    data = aiohttp.FormData()
    data.add_field("audio", wav_bytes, filename="input.wav", content_type="audio/wav")
    data.add_field("session_id", SESSION_ID)
    async with session.post(REMOTE_API_URL, data=data) as resp:
        payload = await resp.json()
        if resp.status != 200:
            raise RuntimeError(payload.get("error", f"server returned {resp.status}"))
        return payload


def pcm_to_wav_bytes(pcm_frames: list[bytes], sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        for frame in pcm_frames:
            wf.writeframes(frame)
    return buffer.getvalue()


async def main() -> None:
    print(f"Remote API: {REMOTE_API_URL}")
    print(f"Session ID: {SESSION_ID}")
    print("Realtime mode enabled. Press Ctrl+C to stop.")
    print(f"TTS: {'enabled' if TTS_ENABLED else 'disabled'}")

    block_size = max(1, int(SAMPLE_RATE * BLOCK_MS / 1000))
    min_speech_blocks = max(1, int(MIN_SPEECH_SECONDS * 1000 / BLOCK_MS))
    silence_blocks_to_end = max(1, int(SILENCE_SECONDS * 1000 / BLOCK_MS))
    pre_roll_blocks = max(0, int(PRE_ROLL_SECONDS * 1000 / BLOCK_MS))

    audio_queue: queue.Queue[bytes] = queue.Queue()

    def callback(indata, frames, time, status):
        if status:
            print(f"Audio warning: {status}")
        audio_queue.put(bytes(indata))

    in_speech = False
    speech_blocks = 0
    silence_blocks = 0
    pre_roll: deque[bytes] = deque(maxlen=pre_roll_blocks)
    utterance_frames: list[bytes] = []
    tts_engine = init_tts_engine()

    async with aiohttp.ClientSession() as http_session:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=block_size,
            channels=1,
            dtype="int16",
            callback=callback,
        ):
            while True:
                frame = await asyncio.to_thread(audio_queue.get)
                rms = audioop.rms(frame, 2)
                voiced = rms >= RMS_THRESHOLD

                if not in_speech:
                    pre_roll.append(frame)
                    if voiced:
                        in_speech = True
                        speech_blocks = 1
                        silence_blocks = 0
                        utterance_frames = list(pre_roll)
                    continue

                utterance_frames.append(frame)
                if voiced:
                    speech_blocks += 1
                    silence_blocks = 0
                else:
                    silence_blocks += 1

                enough_voice = speech_blocks >= min_speech_blocks
                end_of_utterance = silence_blocks >= silence_blocks_to_end
                if enough_voice and end_of_utterance:
                    wav_bytes = pcm_to_wav_bytes(utterance_frames, SAMPLE_RATE)
                    try:
                        result = await send_wav_bytes(http_session, wav_bytes)
                        transcript = result.get("transcript", "")
                        response = result.get("response", "")
                        print(f"\nYou said: {transcript}")
                        print(f"Assistant: {response}\n")
                        await speak_text(tts_engine, response)
                    except Exception as exc:
                        print(f"Error: {exc}")

                    in_speech = False
                    speech_blocks = 0
                    silence_blocks = 0
                    pre_roll.clear()
                    utterance_frames = []


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        exit(0)
