# Spark ⚡ — Distributed Voice Assistant & Desktop Companion

### PC Host + Arduino UNO Q Satellite

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Host Platform](https://img.shields.io/badge/Host-PC%20%2F%20Linux%20%2F%20macOS-blue)](#1-pc-host-setup)
[![Satellite Platform](https://img.shields.io/badge/Satellite-Arduino%20UNO%20Q-red)](#2-arduino-uno-q-satellite-setup)

**Spark** is a low-latency, modular distributed desktop companion:

| Component | Role |
|---|---|
| **PC Host** | AI brain — local LLM (`gemma4:e2b` via Ollama), STT (`faster-whisper`), VAD (Silero), streaming TTS (`piper-tts`) |
| **UNO Q Satellite** | Debian Linux on QRB2210 MPU — full-duplex USB-C headset audio I/O, state sync with STM32 coprocessor |
| **STM32 Firmware** | Drives the onboard 8×12 LED matrix with 4 animated face states |
| **Transport** | Bidirectional WebSocket over local Wi-Fi (16 kHz raw PCM + JSON state packets) |

---

## 🏛️ System Architecture

```text
              +-----------------------------------------------------------+
              |                      PC HOST SERVER                       |
              |                                                           |
              |  16 kHz PCM ──► [ Silero VAD ]                            |
              |                      │                                    |
              |                 [ faster-whisper ] (STT)                  |
              |                      │                                    |
              |                 [ Ollama: gemma4:e2b ] (Streaming LLM)    |
              |                      │                                    |
              |                 [ Sentence Buffer + Piper TTS ]           |
              |                      │                                    |
              |              PCM Stream Out ───────┐                      |
              |              State Dispatch ───────┤                      |
              +────────────────────────────────────┼──────────────────────+
                                                   │  WebSocket (TCP/IP)
              +────────────────────────────────────┼──────────────────────+
              |                                    ▼                      |
              |               ARDUINO UNO Q SATELLITE                     |
              |                                                           |
              |  (Qualcomm QRB2210 MPU — Debian Linux)                    |
              |   ├── Dynamic USB Audio Resolver                          |
              |   ├── Audio Capture (16 kHz mono) ────────► Host          |
              |   ├── Audio Playback (Jitter Buffer ──► Headset)          |
              |   ├── Half-Duplex Muting (mic off during TTS)             |
              |   └── Face State Forwarder (/dev/ttyRPMSG0)               |
              |                    │                                      |
              |                    ▼  Serial IPC                          |
              |  (STM32U585 Coprocessor)                                  |
              |   └── 8×12 LED Matrix                                     |
              |        ├── IDLE      (Natural Blink)                      |
              |        ├── LISTENING (Alert Wide Eyes)                     |
              |        ├── THINKING  (Marquee Scan)                       |
              |        └── SPEAKING  (Animated Mouth)                     |
              +-----------------------------------------------------------+
```

---

## 🛠️ Hardware Requirements

### PC Host
- Windows, Linux, or macOS workstation with **8 GB+ RAM**
- Optional: NVIDIA GPU with CUDA for accelerated Whisper STT

### Arduino UNO Q Satellite
- Arduino UNO Q board (Qualcomm QRB2210 MPU + STM32U585 MCU + 8×12 LED matrix)
- Powered USB-C PD Hub
- USB-C headset with integrated microphone
- Local Wi-Fi network connecting Host ↔ Satellite

---

## 🚀 Step-by-Step Installation

### 1. PC Host Setup

```bash
# Clone and enter the repo
cd spark

# Install Python dependencies
pip install -r requirements_host.txt

# Install and launch Ollama (https://ollama.com)
ollama pull gemma4:e2b
# Fallback: ollama pull gemma:2b

# Install Piper TTS — either:
#   a) Download binary from https://github.com/rhasspy/piper/releases
#   b) pip install piper-tts
# Download voice model into piper/ folder:
#   en_GB-semaine-medium.onnx + .onnx.json

# Start the host server
python host_agent.py config_host.json
```

The server listens on **`ws://0.0.0.0:8765`** by default.

### 2. Arduino UNO Q Satellite Setup

```bash
# SSH into the UNO Q Debian environment
ssh arduino@uno-q.local

# Install system dependencies
sudo apt update && sudo apt install -y python3-pip libportaudio2

# Clone and enter the repo
cd spark

# Install Python dependencies
pip install -r requirements_satellite.txt

# Configure Host IP
nano config_satellite.json
# Set "host_url" to your PC's IP: "ws://192.168.1.100:8765"

# Start the satellite daemon
python3 uno_q_satellite.py config_satellite.json
```

### 3. STM32 Coprocessor Firmware

1. Open Arduino IDE or Arduino CLI.
2. Open `firmware/led_matrix_face/led_matrix_face.ino`.
3. Install the `Arduino_LED_Matrix` library if not already present.
4. Select board: **Arduino UNO Q (STM32U585 Coprocessor)**.
5. Upload the sketch.
6. The LED matrix boots into `IDLE` with automatic eye blinking.

---

## 🌐 Network Configuration & Discovery

### Finding the Host IP

| Platform | Command |
|---|---|
| **Windows** | `ipconfig` → IPv4 Address |
| **Linux / macOS** | `ip -br a` or `hostname -I` |
| **mDNS** | Use `ws://<hostname>.local:8765` if supported |

### Static IP (Recommended)

Assign a static IP to the Host PC on your local network (e.g. `192.168.1.100`), then set that in `config_satellite.json`:

```json
{
  "host_url": "ws://192.168.1.100:8765"
}
```

### Auto-Start (systemd Service)

To run the satellite daemon automatically on boot, create `/etc/systemd/system/spark-satellite.service`:

```ini
[Unit]
Description=Spark Satellite Daemon
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=arduino
WorkingDirectory=/home/arduino/spark
ExecStart=/usr/bin/python3 /home/arduino/spark/uno_q_satellite.py /home/arduino/spark/config_satellite.json
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now spark-satellite.service
```

---

## 🎭 State Machine & LED Expressions

| State | LED Matrix | Audio Behaviour |
|---|---|---|
| **`IDLE`** | Friendly eyes, periodic random blink | Satellite streams mic audio; VAD monitors for speech onset |
| **`LISTENING`** | Wide alert eyes, alternating pupil sparkle | VAD detected speech; Host accumulates audio segment |
| **`THINKING`** | Squinted eyes, scanning marquee dot | Faster-Whisper transcribes → Ollama streams LLM response |
| **`SPEAKING`** | Expressive eyes, animated talking mouth (4-frame cycle) | Piper TTS streams PCM; Satellite plays audio & mutes mic |

---

## 📁 Project Structure

```
spark/
├── host_agent.py               # PC Host server (VAD + STT + LLM + TTS)
├── uno_q_satellite.py          # UNO Q satellite daemon (audio I/O + bridge)
├── firmware/
│   └── led_matrix_face/
│       └── led_matrix_face.ino # STM32U585 LED matrix face firmware
├── config_host.json            # Host configuration
├── config_satellite.json       # Satellite configuration
├── requirements_host.txt       # PC Python dependencies
├── requirements_satellite.txt  # UNO Q Python dependencies
├── agent.py                    # Original single-device agent (reference)
├── DISTRIBUTED_README.md       # This file
├── README.md                   # Original project README
└── LICENSE                     # MIT License
```

---

## ⚙️ Configuration Reference

### `config_host.json`

| Section | Key | Default | Description |
|---|---|---|---|
| `server` | `host` | `0.0.0.0` | Bind address |
| `server` | `port` | `8765` | WebSocket port |
| `llm` | `model` | `gemma4:e2b` | Primary Ollama model |
| `llm` | `fallback_model` | `gemma:2b` | Fallback if primary unavailable |
| `llm` | `temperature` | `0.7` | Generation temperature |
| `llm` | `system_prompt` | *(Spark persona)* | System prompt constraining style |
| `vad` | `threshold` | `0.5` | Speech probability threshold |
| `vad` | `min_speech_duration_ms` | `250` | Min speech to trigger listening |
| `vad` | `min_silence_duration_ms` | `700` | Silence to mark speech end |
| `stt` | `model_size` | `base.en` | Whisper model (`tiny.en`, `base.en`, `small.en`, etc.) |
| `stt` | `device` | `auto` | `auto`, `cpu`, or `cuda` |
| `tts` | `voice_model` | `piper/en_GB-semaine-medium.onnx` | Piper voice model path |
| `tts` | `sample_rate` | `22050` | Piper output sample rate |

### `config_satellite.json`

| Key | Default | Description |
|---|---|---|
| `host_url` | `ws://127.0.0.1:8765` | Host WebSocket URL |
| `audio.device_keyword` | `USB` | Substring to match USB headset |
| `audio.capture_sample_rate` | `16000` | Target capture rate (resampled if needed) |
| `audio.playback_sample_rate` | `22050` | Must match Host TTS sample rate |
| `audio.chunk_size` | `512` | Samples per capture frame |
| `stm32.enabled` | `true` | Enable serial bridge to LED matrix |
| `stm32.port` | `/dev/ttyRPMSG0` | Primary serial port to STM32 |
| `stm32.baudrate` | `115200` | Serial baud rate |

---

## 📄 License

- **Spark:** Distributed Host Server, Silero VAD, Faster-Whisper STT, Arduino UNO Q Satellite Daemon, STM32U585 LED Matrix Firmware.
- **License:** MIT License — see [LICENSE](LICENSE)
