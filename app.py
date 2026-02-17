import asyncio
import io
import json
import os
import queue
import re
import threading
import time
import wave
from array import array
from collections import deque

import aiohttp
import pyttsx3
import sounddevice as sd
from dotenv import load_dotenv


load_dotenv()

REMOTE_API_URL = os.getenv("REMOTE_API_URL", "http://127.0.0.1:8080/chat")
REMOTE_STREAM_API_URL = os.getenv(
    "REMOTE_STREAM_API_URL", REMOTE_API_URL.rstrip("/") + "/stream"
)
SESSION_ID = os.getenv("SESSION_ID", "default")
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
BLOCK_MS = int(os.getenv("REALTIME_BLOCK_MS", "30"))
RMS_THRESHOLD = int(os.getenv("REALTIME_RMS_THRESHOLD", "450"))
MIN_SPEECH_SECONDS = float(os.getenv("REALTIME_MIN_SPEECH_SECONDS", "0.35"))
SILENCE_SECONDS = float(os.getenv("REALTIME_SILENCE_SECONDS", "0.8"))
PRE_ROLL_SECONDS = float(os.getenv("REALTIME_PRE_ROLL_SECONDS", "0.2"))
MAX_UTTERANCE_SECONDS = float(os.getenv("REALTIME_MAX_UTTERANCE_SECONDS", "12.0"))
TTS_ENABLED = os.getenv("TTS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
TTS_RATE = int(os.getenv("TTS_RATE", "185"))
TTS_VOICE_HINT = os.getenv("TTS_VOICE_HINT", "").strip().lower()


class TTSWorker:
    def __init__(self, rate: int = 185, voice_hint: str = "", max_chunk_chars: int = 220):
        self._rate = rate
        self._voice_hint = (voice_hint or "").strip().lower()
        self._max_chunk_chars = max_chunk_chars
        self._q: queue.Queue[tuple[str, int] | None] = queue.Queue()
        self._seq = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="tts-worker", daemon=True)
        self._thread.start()

    def speak_now(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._q.put((text, seq))

    def stop(self) -> None:
        with self._lock:
            self._seq += 1

    def close(self) -> None:
        self._q.put(None)

    def join(self, timeout: float = 2.0) -> None:
        self._thread.join(timeout)

    def _build_engine(self):
        eng = pyttsx3.init()
        eng.setProperty("rate", self._rate)
        if self._voice_hint:
            try:
                for v in eng.getProperty("voices"):
                    blob = f"{getattr(v, 'id', '')} {getattr(v, 'name', '')}".lower()
                    if self._voice_hint in blob:
                        eng.setProperty("voice", v.id)
                        break
            except Exception:
                pass
        return eng

    def _split_chunks(self, text: str) -> list[str]:
        pieces = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
        chunks: list[str] = []
        for piece in pieces:
            if len(piece) <= self._max_chunk_chars:
                chunks.append(piece)
                continue
            start = 0
            while start < len(piece):
                end = min(start + self._max_chunk_chars, len(piece))
                chunk = piece[start:end].strip()
                if chunk:
                    chunks.append(chunk)
                start = end
        return chunks or [text[: self._max_chunk_chars].strip()]

    def _run(self) -> None:
        engine = None
        while True:
            item = self._q.get()
            if item is None:
                try:
                    if engine:
                        engine.stop()
                except Exception:
                    pass
                return

            text, my_seq = item
            if engine is None:
                try:
                    engine = self._build_engine()
                except Exception as exc:
                    print(f"TTS init failed: {exc}")
                    time.sleep(0.1)
                    engine = None
                    continue

            try:
                engine.stop()
            except Exception:
                pass

            try:
                for chunk in self._split_chunks(text):
                    with self._lock:
                        if my_seq != self._seq:
                            break
                    engine.say(chunk)
                    engine.runAndWait()
            except Exception as exc:
                print(f"TTS error (restarting engine): {exc}")
                try:
                    engine.stop()
                except Exception:
                    pass
                engine = None
                time.sleep(0.1)


async def send_wav_bytes_stream(session: aiohttp.ClientSession, wav_bytes: bytes) -> tuple[str, str]:
    data = aiohttp.FormData()
    data.add_field("audio", wav_bytes, filename="input.wav", content_type="audio/wav")
    data.add_field("session_id", SESSION_ID)

    transcript = ""
    response_chunks: list[str] = []

    async with session.post(REMOTE_STREAM_API_URL, data=data) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"server returned {resp.status}: {body}")

        print("\nAssistant (stream): ", end="", flush=True)
        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data: "):
                continue

            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            if event_type == "transcript":
                transcript = event.get("text", "")
            elif event_type == "token":
                token = event.get("text", "")
                response_chunks.append(token)
                print(token, end="", flush=True)
            elif event_type == "error":
                raise RuntimeError(event.get("error", "unknown stream error"))
            elif event_type == "done":
                break

    print()
    return transcript, "".join(response_chunks).strip()


def pcm_to_wav_bytes(pcm_frames: list[bytes], sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        for frame in pcm_frames:
            wf.writeframes(frame)
    return buffer.getvalue()


def rms_int16(frame: bytes) -> int:
    if not frame:
        return 0
    samples = array("h")
    samples.frombytes(frame)
    if not samples:
        return 0
    total = 0.0
    for s in samples:
        total += float(s) * float(s)
    return int((total / len(samples)) ** 0.5)


async def main() -> None:
    print(f"Remote stream API: {REMOTE_STREAM_API_URL}")
    print(f"Session ID: {SESSION_ID}")
    print("Realtime mode enabled. Press Ctrl+C to stop.")
    print(f"TTS: {'enabled' if TTS_ENABLED else 'disabled'}")

    block_size = max(1, int(SAMPLE_RATE * BLOCK_MS / 1000))
    min_speech_blocks = max(1, int(MIN_SPEECH_SECONDS * 1000 / BLOCK_MS))
    silence_blocks_to_end = max(1, int(SILENCE_SECONDS * 1000 / BLOCK_MS))
    pre_roll_blocks = max(0, int(PRE_ROLL_SECONDS * 1000 / BLOCK_MS))
    max_utterance_blocks = max(1, int(MAX_UTTERANCE_SECONDS * 1000 / BLOCK_MS))

    audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=300)
    state = {"paused": False}

    def callback(indata, frames, time, status):
        if status:
            print(f"Audio warning: {status}")
        if state["paused"]:
            return
        try:
            audio_queue.put_nowait(bytes(indata))
        except queue.Full:
            pass

    in_speech = False
    speech_blocks = 0
    silence_blocks = 0
    pre_roll: deque[bytes] = deque(maxlen=pre_roll_blocks)
    utterance_frames: list[bytes] = []
    utterance_blocks = 0
    tts_worker = TTSWorker(rate=TTS_RATE, voice_hint=TTS_VOICE_HINT) if TTS_ENABLED else None

    def reset_utterance_state() -> None:
        nonlocal in_speech, speech_blocks, silence_blocks, utterance_frames, utterance_blocks
        in_speech = False
        speech_blocks = 0
        silence_blocks = 0
        utterance_blocks = 0
        pre_roll.clear()
        utterance_frames = []
        while not audio_queue.empty():
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                break

    try:
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
                    rms = rms_int16(frame)
                    voiced = rms >= RMS_THRESHOLD

                    if not in_speech:
                        pre_roll.append(frame)
                        if voiced:
                            if tts_worker:
                                tts_worker.stop()
                            in_speech = True
                            speech_blocks = 1
                            silence_blocks = 0
                            utterance_blocks = len(pre_roll)
                            utterance_frames = list(pre_roll)
                        continue

                    utterance_frames.append(frame)
                    utterance_blocks += 1
                    if voiced:
                        speech_blocks += 1
                        silence_blocks = 0
                    else:
                        silence_blocks += 1

                    enough_voice = speech_blocks >= min_speech_blocks
                    end_of_utterance = silence_blocks >= silence_blocks_to_end
                    force_end = utterance_blocks >= max_utterance_blocks
                    if end_of_utterance or force_end:
                        if enough_voice:
                            wav_bytes = pcm_to_wav_bytes(utterance_frames, SAMPLE_RATE)
                            state["paused"] = True
                            try:
                                transcript, response = await send_wav_bytes_stream(
                                    http_session, wav_bytes
                                )
                                print(f"You said: {transcript}")
                                if tts_worker:
                                    tts_worker.speak_now(response)
                            except Exception as exc:
                                print(f"\nError: {exc}")
                            finally:
                                state["paused"] = False
                        reset_utterance_state()
    finally:
        if tts_worker:
            tts_worker.close()
            await asyncio.to_thread(tts_worker.join, 2.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0)
