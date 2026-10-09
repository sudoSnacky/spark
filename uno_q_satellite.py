# =========================================================================
#  Spark ⚡ — Distributed Desktop Companion: Arduino UNO Q Satellite Client
#
#  Licensed under the MIT License — see LICENSE for details.
#
#  Distributed Extensions & Multi-Device Refactoring:
#  Lightweight daemon running on Debian Linux (Qualcomm QRB2210 MPU on UNO Q).
#  Features:
#   - Dynamic USB-C Headset Audio Device Resolver (no hardcoded card IDs)
#   - 16 kHz 16-bit Mono PCM Streaming Audio Capture
#   - Low-latency Audio Playback Loop with Jitter Buffering
#   - Half-Duplex Acoustic Feedback Suppression (mute mic during playback)
#   - Local Serial IPC / Bridge Forwarder to STM32U585 LED Matrix Face
# =========================================================================

import asyncio
import json
import logging
import os
import queue
import struct
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import sounddevice as sd

try:
    import websockets
except ImportError:
    print("FATAL: 'websockets' package is required. Install via: pip install websockets>=12.0")
    sys.exit(1)

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Satellite] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("UNOSatellite")


# =========================================================================
# Configuration Loader
# =========================================================================

DEFAULT_CONFIG: Dict[str, Any] = {
    "host_url": "ws://127.0.0.1:8765",
    "audio": {
        "device_keyword": "USB",
        "capture_sample_rate": 16000,
        "playback_sample_rate": 22050,
        "channels": 1,
        "chunk_size": 512,
    },
    "stm32": {
        "enabled": True,
        "port": "/dev/ttyRPMSG0",
        "fallback_ports": ["/dev/ttyACM0", "/dev/ttyUSB0", "COM3"],
        "baudrate": 115200,
    },
}


def load_config(config_path: str = "config_satellite.json") -> Dict[str, Any]:
    """Deep-merge user config on top of defaults."""
    config: Dict[str, Any] = {}
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
            logger.info("Loaded satellite config from %s", config_path)
        except Exception as exc:
            logger.warning("Error loading %s: %s. Using defaults.", config_path, exc)
    return config


# =========================================================================
# Dynamic Audio Device Resolver
# =========================================================================

class AudioDeviceResolver:
    """Discovers audio hardware dynamically to bind the USB-C headset interface."""

    @staticmethod
    def resolve_devices(keyword: str = "USB") -> Tuple[Optional[int], Optional[int]]:
        """
        Scan available audio devices and return (input_index, output_index).

        Matches devices whose name contains *keyword* (case-insensitive).
        Falls back to system defaults if no keyword match is found.
        """
        devices = sd.query_devices()
        input_idx: Optional[int] = None
        output_idx: Optional[int] = None
        kw_lower = keyword.lower()

        logger.info("Scanning audio devices (keyword='%s')...", keyword)
        for idx, dev in enumerate(devices):
            name = dev.get("name", "")
            max_in = dev.get("max_input_channels", 0)
            max_out = dev.get("max_output_channels", 0)
            logger.debug("  [%d] %s  in=%d out=%d", idx, name, max_in, max_out)

            if kw_lower in name.lower():
                if max_in > 0 and input_idx is None:
                    input_idx = idx
                    logger.info("  -> INPUT  [%d]: %s", idx, name)
                if max_out > 0 and output_idx is None:
                    output_idx = idx
                    logger.info("  -> OUTPUT [%d]: %s", idx, name)

        # Fallback to system defaults
        if input_idx is None:
            default_in = sd.default.device[0]
            if default_in is not None and isinstance(default_in, int) and default_in >= 0:
                input_idx = default_in
                logger.warning("No USB input match. Falling back to default input [%d].", input_idx)
            else:
                logger.error("No usable input device found!")

        if output_idx is None:
            default_out = sd.default.device[1]
            if default_out is not None and isinstance(default_out, int) and default_out >= 0:
                output_idx = default_out
                logger.warning("No USB output match. Falling back to default output [%d].", output_idx)
            else:
                logger.error("No usable output device found!")

        return input_idx, output_idx


# =========================================================================
# STM32 Bridge / Serial Face Controller
# =========================================================================

class STM32FaceBridge:
    """Sends face state commands to the STM32U585 coprocessor over RPMSG/Serial."""

    VALID_STATES = frozenset(("idle", "listening", "thinking", "speaking"))

    def __init__(self, config: Dict[str, Any]):
        self.enabled: bool = config.get("enabled", True)
        self.port: str = config.get("port", "/dev/ttyRPMSG0")
        self.fallback_ports: List[str] = config.get("fallback_ports", ["/dev/ttyACM0", "/dev/ttyUSB0"])
        self.baudrate: int = config.get("baudrate", 115200)
        self.serial_conn: Optional[serial.Serial] = None
        self._lock = threading.Lock()

        if self.enabled:
            self._connect()

    def _connect(self):
        if not SERIAL_AVAILABLE:
            logger.warning("pyserial not installed. STM32 Face Bridge disabled.")
            return

        for port in [self.port] + self.fallback_ports:
            try:
                if os.path.exists(port) or sys.platform == "win32":
                    conn = serial.Serial(port, self.baudrate, timeout=0.1)
                    self.serial_conn = conn
                    logger.info("Connected to STM32 Face on port: %s", port)
                    self.set_state("idle")
                    return
            except Exception as exc:
                logger.debug("Could not open %s: %s", port, exc)

        logger.warning("Could not connect to STM32 on any candidate port. Running in mock mode.")

    def set_state(self, state: str):
        """Send a newline-terminated state command (e.g. 'IDLE\\n') to STM32."""
        state = state.strip().lower()
        if state not in self.VALID_STATES:
            return

        cmd = f"{state.upper()}\n".encode("utf-8")
        with self._lock:
            if self.serial_conn is not None and self.serial_conn.is_open:
                try:
                    self.serial_conn.write(cmd)
                    self.serial_conn.flush()
                except Exception as exc:
                    logger.error("Serial write error: %s", exc)
            else:
                logger.debug("[STM32 Mock] -> %s", state.upper())

    def close(self):
        with self._lock:
            if self.serial_conn is not None and self.serial_conn.is_open:
                try:
                    self.serial_conn.close()
                except Exception:
                    pass


# =========================================================================
# Audio Playback Engine (Jitter Buffer & Callback-driven Stream)
# =========================================================================

class AudioPlaybackEngine:
    """Plays received TTS audio to the USB headset with minimal underrun risk."""

    def __init__(self, device_idx: Optional[int], sample_rate: int = 22050):
        self.device_idx = device_idx
        self.sample_rate = sample_rate
        # Thread-safe byte queue. Each item is a bytes object of raw int16 PCM.
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stream: Optional[sd.RawOutputStream] = None
        self.is_playing = False
        self._start_stream()

    def _start_stream(self):
        try:
            self._stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=self.device_idx,
                callback=self._audio_callback,
                blocksize=1024,
                latency="low",
            )
            self._stream.start()
            logger.info("Playback stream started at %d Hz on device [%s].", self.sample_rate, self.device_idx)
        except Exception as exc:
            logger.error("Failed to start playback stream: %s", exc)

    def _audio_callback(self, outdata, frames, time_info, status):
        """sounddevice raw output callback — feeds PCM bytes from the queue."""
        bytes_needed = frames * 2  # 16-bit mono = 2 bytes per sample
        collected = bytearray()

        while len(collected) < bytes_needed:
            try:
                chunk = self._queue.get_nowait()
                collected.extend(chunk)
            except queue.Empty:
                break

        if collected:
            self.is_playing = True
            if len(collected) < bytes_needed:
                collected.extend(b"\x00" * (bytes_needed - len(collected)))
            # Truncate to exact size if we collected a bit extra
            outdata[:] = bytes(collected[:bytes_needed])
        else:
            self.is_playing = False
            outdata[:] = b"\x00" * bytes_needed

    def enqueue_audio(self, pcm_bytes: bytes):
        """Push raw PCM bytes into the playback jitter buffer."""
        try:
            self._queue.put_nowait(pcm_bytes)
        except queue.Full:
            logger.warning("Playback queue full — dropping oldest chunk.")
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(pcm_bytes)
            except queue.Full:
                pass

    def drain_wait(self, timeout: float = 3.0):
        """Block until the playback queue is empty or timeout elapses."""
        deadline = time.monotonic() + timeout
        while not self._queue.empty() or self.is_playing:
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)

    def clear(self):
        """Flush all buffered audio."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self.is_playing = False

    def close(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass


# =========================================================================
# UNO Q Satellite Main Daemon
# =========================================================================

class UNOSatellite:
    """Full daemon managing mic streaming, speaker playback, and STM32 LED face."""

    def __init__(self, config_path: str = "config_satellite.json"):
        self.config = load_config(config_path)

        # 1. Resolve Audio Devices
        kw = self.config["audio"].get("device_keyword", "USB")
        self.in_dev, self.out_dev = AudioDeviceResolver.resolve_devices(kw)

        # 2. STM32 Face Bridge
        self.face_bridge = STM32FaceBridge(self.config.get("stm32", {}))

        # 3. Audio Playback Engine
        playback_sr = self.config["audio"].get("playback_sample_rate", 22050)
        self.playback = AudioPlaybackEngine(self.out_dev, sample_rate=playback_sr)

        # Audio Capture Config
        self.capture_sr: int = self.config["audio"].get("capture_sample_rate", 16000)
        self.chunk_size: int = self.config["audio"].get("chunk_size", 512)

        # Half-Duplex state (suppress mic while TTS is playing)
        self.is_tts_active = False
        self.host_state = "idle"

        # Async mic queue — initialized in run()
        self._mic_queue: Optional[asyncio.Queue] = None

    # ------------------------------------------------------------------
    # Sample Rate Negotiation
    # ------------------------------------------------------------------

    def _negotiate_capture_rate(self) -> int:
        """Probe for a hardware-supported capture sample rate."""
        candidates = [self.capture_sr, 48000, 44100, 32000, 16000]
        for rate in candidates:
            try:
                sd.check_input_settings(device=self.in_dev, samplerate=rate, channels=1, dtype="int16")
                logger.info("Input device supports %d Hz.", rate)
                return rate
            except Exception:
                continue
        logger.warning("Could not verify any capture rate. Defaulting to %d Hz.", self.capture_sr)
        return self.capture_sr

    # ------------------------------------------------------------------
    # Microphone Capture Loop
    # ------------------------------------------------------------------

    async def _mic_capture_loop(self, websocket):
        """Capture audio from the headset mic, downsample to 16 kHz, and send to Host."""
        loop = asyncio.get_running_loop()
        self._mic_queue = asyncio.Queue(maxsize=60)
        native_sr = self._negotiate_capture_rate()
        needs_resample = (native_sr != 16000)

        def audio_in_callback(indata, frames, time_info, status):
            # --- Half-Duplex: suppress mic while TTS is playing ---
            if self.is_tts_active or self.playback.is_playing or self.host_state == "speaking":
                return

            audio = indata.flatten()

            if needs_resample:
                target_len = int(len(audio) * (16000.0 / native_sr))
                if target_len < 1:
                    return
                resampled = np.interp(
                    np.linspace(0, len(audio), target_len, endpoint=False),
                    np.arange(len(audio)),
                    audio,
                ).astype(np.int16)
                pcm_bytes = resampled.tobytes()
            else:
                pcm_bytes = audio.tobytes()

            # Schedule queue put on the event loop (thread-safe)
            loop.call_soon_threadsafe(self._enqueue_mic_data, pcm_bytes)

        try:
            hw_chunk = int(self.chunk_size * (native_sr / 16000.0))
            with sd.InputStream(
                samplerate=native_sr,
                channels=1,
                dtype="int16",
                device=self.in_dev,
                blocksize=hw_chunk,
                callback=audio_in_callback,
            ):
                logger.info(
                    "Mic capture active [device %s] (%d Hz -> 16 kHz).",
                    self.in_dev, native_sr,
                )
                while True:
                    pcm_chunk = await self._mic_queue.get()
                    await websocket.send(pcm_chunk)
        except asyncio.CancelledError:
            logger.info("Mic capture loop cancelled.")
        except Exception as exc:
            logger.error("Mic capture error: %s", exc)

    def _enqueue_mic_data(self, data: bytes):
        """Put mic data into the async queue, dropping if full."""
        if self._mic_queue is not None:
            try:
                self._mic_queue.put_nowait(data)
            except asyncio.QueueFull:
                # Drop oldest to prevent accumulating stale audio
                try:
                    self._mic_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    self._mic_queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

    # ------------------------------------------------------------------
    # Network Receive Loop (State Packets + TTS Audio)
    # ------------------------------------------------------------------

    async def _network_receive_loop(self, websocket):
        """Listen for state packets and synthesized TTS audio from the Host."""
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    # Raw PCM audio from Piper TTS
                    self.playback.enqueue_audio(message)

                elif isinstance(message, str):
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        logger.warning("Unparsable JSON: %s", message[:100])
                        continue

                    msg_type = data.get("type", "")

                    if msg_type == "state":
                        new_state = data.get("state", "idle")
                        self.host_state = new_state
                        logger.info("Host state -> [%s]", new_state.upper())
                        self.face_bridge.set_state(new_state)

                    elif msg_type == "tts_start":
                        self.is_tts_active = True
                        logger.info("TTS active — mic muted (half-duplex).")

                    elif msg_type == "tts_end":
                        # Wait for playback buffer to drain before unmuting
                        self.playback.drain_wait(timeout=5.0)
                        self.is_tts_active = False
                        logger.info("TTS finished — mic unmuted.")

                    elif msg_type == "config":
                        # Server handshake — update playback sample rate if changed
                        new_sr = data.get("audio_sr")
                        if new_sr and new_sr != self.playback.sample_rate:
                            logger.info("Updating playback sample rate to %d Hz.", new_sr)
                            self.playback.close()
                            self.playback = AudioPlaybackEngine(self.out_dev, sample_rate=new_sr)

                    elif msg_type == "pong":
                        pass  # Heartbeat acknowledged
        except asyncio.CancelledError:
            logger.info("Network receive loop cancelled.")
        except Exception as exc:
            logger.error("Network receive error: %s", exc)

    # ------------------------------------------------------------------
    # Heartbeat Ping
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, websocket, interval: float = 15.0):
        """Periodically ping the host server to detect stale connections."""
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await websocket.send(json.dumps({"type": "ping", "ts": time.time()}))
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Main Run Loop (with exponential backoff reconnect)
    # ------------------------------------------------------------------

    async def run(self):
        host_url = self.config.get("host_url", "ws://127.0.0.1:8765")
        logger.info("=" * 50)
        logger.info("⚡ Spark — UNO Q Satellite Daemon")
        logger.info("   Host: %s", host_url)
        logger.info("   Input Device:  [%s]", self.in_dev)
        logger.info("   Output Device: [%s]", self.out_dev)
        logger.info("=" * 50)

        backoff = 1.0

        while True:
            try:
                async with websockets.connect(
                    host_url,
                    max_size=10 * 1024 * 1024,
                    ping_interval=None,  # We handle our own heartbeat
                ) as ws:
                    logger.info("Connected to Host at %s.", host_url)
                    backoff = 1.0  # Reset on successful connect

                    tasks = [
                        asyncio.create_task(self._mic_capture_loop(ws)),
                        asyncio.create_task(self._network_receive_loop(ws)),
                        asyncio.create_task(self._heartbeat_loop(ws)),
                    ]

                    # Wait until any task finishes (usually means disconnect)
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                    # Cancel remaining tasks
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                    # Check for exceptions in completed tasks
                    for task in done:
                        if task.exception() is not None:
                            logger.warning("Task exited with error: %s", task.exception())

            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError) as exc:
                logger.warning("Connection lost/failed (%s). Retrying in %.1fs...", exc, backoff)
                self.face_bridge.set_state("idle")
                self.host_state = "idle"
                self.is_tts_active = False
                self.playback.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            except Exception as exc:
                logger.error("Unexpected error: %s", exc)
                await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self):
        logger.info("Satellite shutting down...")
        self.playback.close()
        self.face_bridge.set_state("idle")
        self.face_bridge.close()


# =========================================================================
# Main Entry Point
# =========================================================================

if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config_satellite.json"
    satellite = UNOSatellite(config_path=cfg_path)
    try:
        asyncio.run(satellite.run())
    except KeyboardInterrupt:
        satellite.shutdown()
        logger.info("Satellite terminated by user.")
