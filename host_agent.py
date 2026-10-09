# =========================================================================
#  Spark ⚡ — Distributed Desktop Companion: PC Host Server
#
#  Licensed under the MIT License — see LICENSE for details.
#
#  Distributed Extensions & Multi-Device Refactoring:
#  Decoupled Host Server running Silero VAD, Faster-Whisper STT,
#  Ollama (gemma4:e2b) LLM Engine, Sentence-Buffered Piper TTS,
#  and Real-Time State Synchronization over WebSockets.
# =========================================================================

import asyncio
import json
import logging
import os
import re
import signal
import struct
import subprocess
import sys
import time
import traceback
from typing import Any, AsyncIterator, Dict, List, Optional

import numpy as np

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
except ImportError:
    print("FATAL: 'websockets' package is required. Install via: pip install websockets>=12.0")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Optional AI / Audio Processing Libraries (graceful degradation)
# ---------------------------------------------------------------------------
try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("HostAgent")


# =========================================================================
# Configuration Loader
# =========================================================================

DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
    },
    "llm": {
        "model": "gemma4:e2b",
        "fallback_model": "gemma:2b",
        "temperature": 0.7,
        "ollama_url": "http://localhost:11434",
        "system_prompt": (
            "You are Spark, a cheerful, curious, and loyal miniature robotic desktop companion. "
            "You speak in a warm, lively, and concise manner. Keep every response "
            "to 1 or 2 short sentences maximum. Never use markdown, bullet points, "
            "asterisks, or lists — only plain spoken dialogue."
        ),
    },
    "vad": {
        "threshold": 0.5,
        "min_speech_duration_ms": 250,
        "min_silence_duration_ms": 700,
        "speech_pad_ms": 200,
    },
    "stt": {
        "model_size": "base.en",
        "device": "auto",
        "compute_type": "auto",
    },
    "tts": {
        "voice_model": "piper/en_GB-semaine-medium.onnx",
        "voice_config": "piper/en_GB-semaine-medium.onnx.json",
        "piper_binary": "piper",
        "sample_rate": 22050,
    },
}


def load_config(config_path: str = "config_host.json") -> Dict[str, Any]:
    """Deep-merge user config on top of defaults."""
    config: Dict[str, Any] = {}
    # Deep-copy defaults
    for k, v in DEFAULT_CONFIG.items():
        config[k] = dict(v) if isinstance(v, dict) else v

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                user_cfg = json.load(fh)
            for k, v in user_cfg.items():
                if isinstance(v, dict) and k in config and isinstance(config[k], dict):
                    config[k].update(v)
                else:
                    config[k] = v
            logger.info("Loaded configuration from %s", config_path)
        except Exception as exc:
            logger.warning("Failed to read %s (%s). Using default configuration.", config_path, exc)
    else:
        logger.info("No %s found. Using defaults.", config_path)
    return config


# =========================================================================
# Silero Voice Activity Detector (VAD)
# =========================================================================

class SileroVAD:
    """Detects speech onset/offset from incoming 16 kHz 16-bit mono PCM frames."""

    SAMPLE_RATE = 16000

    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        min_silence_ms: int = 700,
    ):
        self.threshold = threshold
        self.min_speech_samples = int(self.SAMPLE_RATE * min_speech_ms / 1000.0)
        self.min_silence_samples = int(self.SAMPLE_RATE * min_silence_ms / 1000.0)

        self.model = None
        self._load_model()

        # State machine
        self.is_speaking = False
        self.speech_buffer: List[np.ndarray] = []
        self.current_speech_samples = 0
        self.silence_samples = 0

    def _load_model(self):
        """Load the Silero VAD model via torch.hub (works offline once cached)."""
        if not TORCH_AVAILABLE:
            logger.warning("PyTorch unavailable — falling back to energy-based VAD.")
            return

        try:
            torch.set_num_threads(1)
            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False,
            )
            self.model = model
            logger.info("Silero VAD model loaded successfully.")
        except Exception as exc:
            logger.warning("Silero VAD load failed (%s). Using energy-based fallback.", exc)
            self.model = None

    def process_frame(self, pcm_bytes: bytes) -> Dict[str, Any]:
        """
        Feed raw 16-bit 16 kHz PCM bytes.

        Returns dict with keys:
          speech_prob  (float)
          event        (None | "SPEECH_START" | "SPEECH_END")
          audio_segment (Optional[np.ndarray] — float32 on SPEECH_END)
        """
        audio_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        audio_f32 = audio_int16.astype(np.float32) / 32768.0
        frame_len = len(audio_f32)

        # --- Speech probability ---
        speech_prob = self._compute_speech_prob(audio_f32, frame_len)

        # --- State machine ---
        event = None
        result_audio = None

        if speech_prob >= self.threshold:
            self.silence_samples = 0
            self.speech_buffer.append(audio_f32)
            if not self.is_speaking:
                self.current_speech_samples += frame_len
                if self.current_speech_samples >= self.min_speech_samples:
                    self.is_speaking = True
                    event = "SPEECH_START"
        else:
            if self.is_speaking:
                self.speech_buffer.append(audio_f32)
                self.silence_samples += frame_len
                if self.silence_samples >= self.min_silence_samples:
                    self.is_speaking = False
                    event = "SPEECH_END"
                    if self.speech_buffer:
                        result_audio = np.concatenate(self.speech_buffer)
                    self._reset_buffers()
            else:
                # Pre-buffer (keeps ~200 ms so the first syllable is not clipped)
                self.speech_buffer.append(audio_f32)
                max_prebuf = max(1, int(self.SAMPLE_RATE * 0.2 / max(frame_len, 1)))
                while len(self.speech_buffer) > max_prebuf:
                    self.speech_buffer.pop(0)

        return {"speech_prob": speech_prob, "event": event, "audio_segment": result_audio}

    def _compute_speech_prob(self, audio_f32: np.ndarray, frame_len: int) -> float:
        if self.model is not None and frame_len >= 512:
            try:
                # Silero expects chunks of 512/1024/1536 samples at 16 kHz
                eval_len = (frame_len // 512) * 512
                tensor = torch.from_numpy(audio_f32[:eval_len])
                with torch.no_grad():
                    return float(self.model(tensor, self.SAMPLE_RATE).item())
            except Exception:
                pass  # fall through to energy-based

        # Fallback: simple RMS energy detector
        rms = float(np.sqrt(np.mean(audio_f32 ** 2) + 1e-12))
        return 1.0 if rms > 0.025 else 0.0

    def _reset_buffers(self):
        self.speech_buffer.clear()
        self.current_speech_samples = 0
        self.silence_samples = 0

    def reset(self):
        self.is_speaking = False
        self._reset_buffers()
        if self.model is not None and hasattr(self.model, "reset_states"):
            self.model.reset_states()


# =========================================================================
# Faster-Whisper Speech-To-Text (STT)
# =========================================================================

class WhisperTranscriber:
    """Local speech-to-text using CTranslate2-backed Faster-Whisper."""

    def __init__(self, model_size: str = "base.en", device: str = "auto", compute_type: str = "auto"):
        self.model: Optional[WhisperModel] = None

        if not WHISPER_AVAILABLE:
            logger.warning("faster-whisper is not installed. STT disabled.")
            return

        if device == "auto":
            device = "cuda" if (TORCH_AVAILABLE and torch.cuda.is_available()) else "cpu"
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"

        try:
            logger.info("Loading Faster-Whisper model '%s' on %s (%s)...", model_size, device, compute_type)
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
            logger.info("Faster-Whisper model loaded.")
        except Exception as exc:
            logger.error("Failed to initialise Faster-Whisper: %s", exc)

    def transcribe(self, audio_f32: np.ndarray) -> str:
        """Transcribe float32 16 kHz audio and return the text."""
        if self.model is None or len(audio_f32) == 0:
            return ""
        try:
            segments, _info = self.model.transcribe(
                audio_f32,
                beam_size=1,
                language="en",
                condition_on_previous_text=False,
                vad_filter=False,  # Silero already handled this
            )
            return " ".join(seg.text for seg in segments).strip()
        except Exception as exc:
            logger.error("Transcription error: %s", exc)
            return ""


# =========================================================================
# Piper TTS Engine
# =========================================================================

class PiperTTS:
    """Low-latency sentence-level speech synthesis using Piper."""

    def __init__(
        self,
        voice_model: str,
        voice_config: Optional[str] = None,
        piper_bin: str = "piper",
        speaker: Optional[int] = None,
        target_sr: int = 22050,
    ):
        self.voice_model = voice_model
        self.voice_config = voice_config
        self.piper_bin = piper_bin
        self.speaker = speaker
        self.target_sr = target_sr
        self._resolved_cmd: Optional[List[str]] = None  # cached after first success

    def synthesize_sentence(self, text: str) -> Optional[bytes]:
        """Synthesize *text* into raw 16-bit mono PCM bytes. Returns None on failure."""
        clean = re.sub(r"[*_#`~\[\]]", "", text).strip()
        if not clean:
            return None

        # If we already found a working command, re-use it
        if self._resolved_cmd is not None:
            return self._run_piper(self._resolved_cmd, clean)

        # Try several candidate invocations
        candidates: List[List[str]] = [
            [self.piper_bin, "--model", self.voice_model, "--output-raw"],
            [sys.executable, "-m", "piper", "--model", self.voice_model, "--output-raw"],
        ]
        # Platform-specific binary locations
        if sys.platform == "win32":
            candidates.append([os.path.join(".", "piper", "piper.exe"), "--model", self.voice_model, "--output-raw"])
        else:
            candidates.append([os.path.join(".", "piper", "piper"), "--model", self.voice_model, "--output-raw"])

        for cmd in candidates:
            if self.voice_config and os.path.exists(self.voice_config):
                cmd = cmd + ["--config", self.voice_config]
            if self.speaker is not None:
                cmd = cmd + ["--speaker", str(self.speaker)]
            result = self._run_piper(cmd, clean)
            if result is not None:
                self._resolved_cmd = cmd
                return result

        logger.error("Piper TTS failed: no working binary/module found.")
        return None

    @staticmethod
    def _run_piper(cmd: List[str], text: str) -> Optional[bytes]:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            raw_pcm, stderr = proc.communicate(input=text.encode("utf-8"), timeout=15)
            if proc.returncode == 0 and raw_pcm:
                return raw_pcm
            logger.debug("Piper returned %d: %s", proc.returncode, stderr.decode(errors="ignore")[:200])
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            logger.warning("Piper TTS timed out for text: '%s'", text[:40])
        except Exception as exc:
            logger.debug("Piper attempt exception: %s", exc)
        return None


# =========================================================================
# LLM Engine (Ollama / Gemma)
# =========================================================================

class LLMEngine:
    """Streaming conversational responses via local Ollama."""

    MAX_HISTORY_TURNS = 4  # keep last N user+assistant pairs

    def __init__(
        self,
        model: str = "gemma4:e2b",
        fallback_model: str = "gemma:2b",
        system_prompt: str = "",
        temperature: float = 0.7,
        host: str = "http://localhost:11434",
    ):
        self.model = model
        self.fallback_model = fallback_model
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.host = host
        self.history: List[Dict[str, str]] = []

    def test_connection(self):
        if not OLLAMA_AVAILABLE:
            logger.warning("ollama python package is not installed.")
            return
        try:
            client = ollama.Client(host=self.host)
            model_list = client.list()
            # ollama >=0.3 returns an object with .models attribute
            if hasattr(model_list, "models"):
                names = [m.model for m in model_list.models]
            elif isinstance(model_list, dict):
                names = [m.get("name", "") for m in model_list.get("models", [])]
            else:
                names = []
            logger.info("Connected to Ollama. Available models: %s", names)
        except Exception as exc:
            logger.warning("Could not connect to Ollama at %s: %s", self.host, exc)

    async def generate_response_stream(self, user_prompt: str) -> AsyncIterator[str]:
        """Async generator yielding streaming text tokens from Ollama."""
        messages: List[Dict[str, str]] = [{"role": "system", "content": self.system_prompt}]
        messages.extend(self.history[-(self.MAX_HISTORY_TURNS * 2):])
        messages.append({"role": "user", "content": user_prompt})

        full_response = ""
        succeeded = False

        for target_model in (self.model, self.fallback_model):
            if succeeded:
                break
            try:
                client = ollama.AsyncClient(host=self.host)
                stream = await client.chat(
                    model=target_model,
                    messages=messages,
                    options={"temperature": self.temperature},
                    stream=True,
                )
                async for chunk in stream:
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        full_response += token
                        yield token
                succeeded = True
            except Exception as exc:
                logger.warning("Model '%s' failed (%s). Trying next...", target_model, exc)

        if not succeeded:
            fallback_msg = "Oh no, my brain is taking a quick nap! Please try again."
            yield fallback_msg
            full_response = fallback_msg

        # Update conversation memory
        self.history.append({"role": "user", "content": user_prompt})
        self.history.append({"role": "assistant", "content": full_response})


# =========================================================================
# Binary Framing Protocol for Audio Chunks
# =========================================================================
# Every audio binary frame sent to the satellite is prefixed with a 4-byte
# little-endian uint32 length header so the receiver can distinguish audio
# frames from JSON text frames.  WebSocket already handles framing at the
# transport level — but we use binary frames for audio and text frames for
# JSON, which is the cleanest approach with the websockets library.
# No extra header needed: websockets distinguishes binary vs text natively.

# =========================================================================
# Host Server Protocol & State Manager
# =========================================================================

class HostAgentServer:
    """Coordinates incoming audio, VAD, STT, LLM, TTS, and LED state dispatch."""

    def __init__(self, config_path: str = "config_host.json"):
        self.config = load_config(config_path)
        self._shutdown_event = asyncio.Event()

        # --- Subsystems ---
        vad_cfg = self.config["vad"]
        self.vad = SileroVAD(
            threshold=vad_cfg["threshold"],
            min_speech_ms=vad_cfg["min_speech_duration_ms"],
            min_silence_ms=vad_cfg["min_silence_duration_ms"],
        )

        stt_cfg = self.config["stt"]
        self.stt = WhisperTranscriber(
            model_size=stt_cfg["model_size"],
            device=stt_cfg["device"],
            compute_type=stt_cfg["compute_type"],
        )

        llm_cfg = self.config["llm"]
        self.llm = LLMEngine(
            model=llm_cfg["model"],
            fallback_model=llm_cfg["fallback_model"],
            system_prompt=llm_cfg["system_prompt"],
            temperature=llm_cfg["temperature"],
            host=llm_cfg["ollama_url"],
        )
        self.llm.test_connection()

        tts_cfg = self.config["tts"]
        self.tts = PiperTTS(
            voice_model=tts_cfg["voice_model"],
            voice_config=tts_cfg.get("voice_config"),
            piper_bin=tts_cfg["piper_binary"],
            speaker=tts_cfg.get("speaker"),
            target_sr=tts_cfg["sample_rate"],
        )

        # Connection tracking
        self.active_clients: set[WebSocketServerProtocol] = set()
        self.current_state = "idle"
        self._processing_lock = asyncio.Lock()

        # Optional Screen GUI (disabled by default in favor of hardware LED matrix)
        self.screen_gui = None
        gui_cfg = self.config.get("screen_gui", {})
        if gui_cfg.get("enabled", False):
            try:
                from gui_face_display import ScreenFaceEngine
                self.screen_gui = ScreenFaceEngine(
                    fullscreen=gui_cfg.get("fullscreen", False),
                    width=gui_cfg.get("width", 800),
                    height=gui_cfg.get("height", 480),
                )
                self.screen_gui.start()
                logger.info("Screen Face GUI engine initialized.")
            except Exception as exc:
                logger.warning("Failed to start Screen Face GUI: %s", exc)

    # ------------------------------------------------------------------
    # State Dispatch
    # ------------------------------------------------------------------

    async def broadcast_state(self, state: str):
        """Send an atomic state packet to every connected satellite."""
        valid = ("idle", "listening", "thinking", "speaking")
        if state not in valid:
            logger.warning("Invalid state '%s' — must be one of %s", state, valid)
            return

        self.current_state = state
        packet = json.dumps({"type": "state", "state": state})
        logger.info("State -> [%s]", state.upper())

        if self.screen_gui:
            self.screen_gui.set_state(state.upper())

        if self.active_clients:
            coros = [ws.send(packet) for ws in self.active_clients]
            await asyncio.gather(*coros, return_exceptions=True)

    # ------------------------------------------------------------------
    # Client Lifecycle
    # ------------------------------------------------------------------

    async def handle_client(self, websocket: WebSocketServerProtocol):
        """Manages lifecycle for one connected UNO Q satellite."""
        addr = websocket.remote_address
        logger.info("Satellite connected from %s", addr)
        self.active_clients.add(websocket)

        # Send initial handshake / config sync
        await websocket.send(json.dumps({
            "type": "config",
            "state": self.current_state,
            "audio_sr": self.config["tts"]["sample_rate"],
        }))

        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    await self._process_audio_chunk(websocket, message)
                elif isinstance(message, str):
                    await self._handle_text_message(websocket, message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Satellite disconnected (%s)", addr)
        except Exception as exc:
            logger.error("Client handler error (%s): %s", addr, exc)
        finally:
            self.active_clients.discard(websocket)
            self.vad.reset()

    async def _handle_text_message(self, websocket: WebSocketServerProtocol, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Malformed text frame: %s", raw[:100])
            return

        msg_type = data.get("type", "")
        if msg_type == "ping":
            await websocket.send(json.dumps({"type": "pong", "ts": time.time()}))
        elif msg_type == "state_req":
            await websocket.send(json.dumps({"type": "state", "state": self.current_state}))

    # ------------------------------------------------------------------
    # Audio Ingest -> VAD
    # ------------------------------------------------------------------

    async def _process_audio_chunk(self, websocket: WebSocketServerProtocol, pcm_data: bytes):
        """Feed audio into VAD. Drops frames while the pipeline is already running (half-duplex)."""
        if self._processing_lock.locked():
            return  # Drop mic audio while responding

        vad_result = self.vad.process_frame(pcm_data)

        if vad_result["event"] == "SPEECH_START":
            logger.info("Speech onset detected.")
            await self.broadcast_state("listening")

        elif vad_result["event"] == "SPEECH_END":
            segment = vad_result["audio_segment"]
            if segment is not None and len(segment) > 0:
                duration = len(segment) / SileroVAD.SAMPLE_RATE
                logger.info("Speech endpoint (%.2fs). Launching pipeline...", duration)
                asyncio.create_task(self._run_conversation_pipeline(websocket, segment))

    # ------------------------------------------------------------------
    # Full Conversation Pipeline: STT -> Streaming LLM -> Sentence TTS
    # ------------------------------------------------------------------

    async def _run_conversation_pipeline(
        self,
        websocket: WebSocketServerProtocol,
        audio_segment: np.ndarray,
    ):
        """Execute the complete STT → LLM → TTS pipeline for one utterance."""
        async with self._processing_lock:
            try:
                # 1. THINKING
                await self.broadcast_state("thinking")

                # 2. Speech-to-Text (runs in thread pool to avoid blocking event loop)
                loop = asyncio.get_running_loop()
                transcription = await loop.run_in_executor(None, self.stt.transcribe, audio_segment)
                logger.info("Transcription: '%s'", transcription)

                if not transcription or len(transcription.strip()) < 2:
                    logger.info("Empty/unintelligible transcription. Returning to IDLE.")
                    await self.broadcast_state("idle")
                    return

                # 3. Stream LLM tokens → buffer into sentences → TTS each sentence
                sentence_buffer = ""
                sentence_re = re.compile(r"([.!?\n]+)")
                first_sentence = True

                async for token in self.llm.generate_response_stream(transcription):
                    sentence_buffer += token

                    # Check for sentence-terminating punctuation
                    parts = sentence_re.split(sentence_buffer)
                    if len(parts) > 1:
                        # Everything up to (and including) the delimiter is a complete sentence
                        complete = "".join(parts[:-1]).strip()
                        sentence_buffer = parts[-1]

                        if complete:
                            if first_sentence:
                                await self.broadcast_state("speaking")
                                await websocket.send(json.dumps({"type": "tts_start"}))
                                first_sentence = False

                            await self._synthesize_and_stream(websocket, complete)

                # Flush remaining buffer (last sentence without trailing punctuation)
                remainder = sentence_buffer.strip()
                if remainder:
                    if first_sentence:
                        await self.broadcast_state("speaking")
                        await websocket.send(json.dumps({"type": "tts_start"}))
                        first_sentence = False
                    await self._synthesize_and_stream(websocket, remainder)

                # Signal TTS completion
                if not first_sentence:
                    await websocket.send(json.dumps({"type": "tts_end"}))

            except websockets.exceptions.ConnectionClosed:
                logger.warning("Satellite disconnected mid-pipeline.")
            except Exception as exc:
                logger.error("Pipeline error: %s\n%s", exc, traceback.format_exc())
            finally:
                self.vad.reset()
                # Brief pause so the satellite finishes draining its playback buffer
                await asyncio.sleep(0.4)
                await self.broadcast_state("idle")

    async def _synthesize_and_stream(self, websocket: WebSocketServerProtocol, sentence: str):
        """Synthesize one sentence via Piper and stream the PCM chunks to the satellite."""
        logger.info("TTS: '%s'", sentence)
        loop = asyncio.get_running_loop()
        pcm_bytes = await loop.run_in_executor(None, self.tts.synthesize_sentence, sentence)

        if not pcm_bytes:
            return

        # Stream in ~2 KB chunks (≈46 ms at 22.05 kHz 16-bit mono)
        chunk_size = 2048
        for offset in range(0, len(pcm_bytes), chunk_size):
            chunk = pcm_bytes[offset : offset + chunk_size]
            try:
                await websocket.send(chunk)
                # Minimal yield to keep the event loop responsive
                await asyncio.sleep(0.002)
            except websockets.exceptions.ConnectionClosed:
                break

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self):
        logger.info("Shutting down host server...")
        self._shutdown_event.set()
        # Close all client connections
        for ws in list(self.active_clients):
            await ws.close(1001, "Server shutting down")
        self.active_clients.clear()


# =========================================================================
# Main Entry Point
# =========================================================================

async def main():
    config_file = sys.argv[1] if len(sys.argv) > 1 else "config_host.json"
    server = HostAgentServer(config_path=config_file)

    host = server.config["server"]["host"]
    port = server.config["server"]["port"]

    logger.info("=" * 60)
    logger.info("⚡ Spark — PC Host Server")
    logger.info("   Listening on ws://%s:%d", host, port)
    logger.info("   LLM Model : %s", server.config["llm"]["model"])
    logger.info("   STT Model : %s", server.config["stt"]["model_size"])
    logger.info("   TTS Voice : %s", server.config["tts"]["voice_model"])
    logger.info("=" * 60)

    stop = asyncio.get_running_loop().create_future()

    # Graceful shutdown on SIGINT / SIGTERM
    def _signal_handler():
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows does not support add_signal_handler — fall back to KeyboardInterrupt
            pass

    async with websockets.serve(
        server.handle_client,
        host,
        port,
        max_size=10 * 1024 * 1024,  # 10 MB max WebSocket frame
        ping_interval=20,
        ping_timeout=60,
    ):
        try:
            await stop
        except asyncio.CancelledError:
            pass

    await server.shutdown()
    logger.info("Server stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user (KeyboardInterrupt).")
