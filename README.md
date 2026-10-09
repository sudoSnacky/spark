# Spark ⚡

### Modular, Low-Latency Distributed AI Desktop Companion
**PC Host (Neural Brain) + Arduino UNO Q (Audio Satellite & STM32 LED Matrix Face)**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Host Platform](https://img.shields.io/badge/Host-Windows%20%7C%20Linux%20%7C%20macOS-blue)](#-quick-start)
[![Satellite Platform](https://img.shields.io/badge/Satellite-Arduino%20UNO%20Q%20(QRB2210)-teal)](#2-satellite-setup-arduino-uno-q)
[![Firmware](https://img.shields.io/badge/Firmware-STM32U585%20LED%20Matrix-orange)](#3-firmware-setup-stm32u585-led-matrix)

---

## 💡 Overview

**Spark** is a distributed conversational AI desktop companion built for natural, offline-first voice interaction. It decouples high-power neural compute from lightweight edge interaction hardware:

* **Host Brain (PC)**: Runs speech-activity detection (**Silero VAD**), local transcription (**Faster-Whisper**), local LLM inference (**Ollama / Gemma 4**), and neural speech synthesis (**Piper TTS**).
* **Satellite Edge (Arduino UNO Q - Debian Linux)**: Streams 16 kHz duplex PCM audio via a USB-C headset, manages acoustic feedback suppression, and syncs emotional states with the coprocessor.
* **Emotional Face (STM32U585 Coprocessor)**: Directly animates the onboard 8×12 LED matrix displaying expressive eye states (Idle breathing blink, Listening sparkle, Thinking marquee, and Talking mouth flap).
* **Communication**: Real-time bidirectional WebSocket transport over local Wi-Fi.

---

## 🏛️ System Architecture

```text
               +-----------------------------------------------------------+
               |                      PC HOST SERVER                       |
               |                                                           |
               |  16 kHz PCM ──► [ Silero VAD ]                            |
               |                      │ (Speech Segments)                  |
               |                 [ Faster-Whisper ] (STT)                  |
               |                      │ (User Prompt Text)                 |
               |                 [ Ollama: gemma4:e2b ] (Streaming LLM)    |
               |                      │ (Sentence Chunks)                  |
               |                 [ Piper-TTS Engine ]                      |
               |                      │ (22.05 kHz Raw PCM Audio)          |
               +──────────────────────┼───────────────────────────────────+
                                      ▲ ▼
                      WebSocket Transport (Local Wi-Fi)
                      - Binary Frames: Bidirectional Audio PCM
                      - Text Frames: JSON State & Control Events
                                      ▲ ▼
               +──────────────────────┴───────────────────────────────────+
               |                 ARDUINO UNO Q SATELLITE                  |
               |                                                           |
               |  [ Debian Linux on Qualcomm QRB2210 MPU ]                 |
               |   ├── USB-C Headset In  ──► Capture Stream (16 kHz Mono)  |
               |   ├── USB-C Headset Out ◄── Playback Buffer (22.05 kHz)   |
               |   └── Half-Duplex Mutex ──► Drop Mic Audio during TTS     |
               |                                                           |
               |  Local Serial IPC (/dev/ttyRPMSG0 @ 115200 baud)          |
               |                           │                               |
               |                           ▼                               |
               |  [ STM32U585 Coprocessor (Arduino IDE / C++) ]            |
               |   └── Arduino_LED_Matrix Driver (8 rows × 12 cols)        |
               |       ├── IDLE      : Natural breathing eyes & blinks     |
               |       ├── LISTENING : Alert wide eyes & pupil sparkle     |
               |       ├── THINKING  : Squint eyes & scanning dot marquee  |
               |       └── SPEAKING  : Expressive eyes & animated mouth    |
               +-----------------------------------------------------------+
```

---

## 🚀 Quick Start

### 1. Host Setup (PC)

1. **Install requirements**:
   ```bash
   pip install -r requirements_host.txt
   ```
2. **Pull the LLM model**:
   ```bash
   ollama pull gemma4:e2b
   ```
3. **Start the Host Server**:
   ```bash
   python host_agent.py config_host.json
   ```
   *The server listens on `ws://0.0.0.0:8765` by default.*

---

### 2. Satellite Setup (Arduino UNO Q)

1. **SSH into the UNO Q terminal**:
   ```bash
   ssh arduino@uno-q.local
   ```
2. **Install audio libraries & dependencies**:
   ```bash
   sudo apt update && sudo apt install -y python3-pip portaudio19-dev libasound2-dev
   cd spark
   pip install -r requirements_satellite.txt
   ```
3. **Configure your PC's IP address**:
   In `config_satellite.json`, set `host_url` to your PC's IP:
   ```json
   {
     "host_url": "ws://192.168.1.150:8765"
   }
   ```
4. **Start the Satellite Client**:
   ```bash
   python3 uno_q_satellite.py config_satellite.json
   ```

---

### 3. Firmware Setup (STM32U585 LED Matrix)

1. Connect the Arduino UNO Q via USB-C to your PC.
2. Open the [Arduino IDE](https://www.arduino.cc/en/software).
3. Open `firmware/led_matrix_face/led_matrix_face.ino`.
4. Ensure the **Arduino_LED_Matrix** library is installed.
5. Select the **Arduino UNO Q** board and upload the sketch.

---

## 🎭 State Machine & LED Expressions

| State | LED Matrix (8×12) | Audio Behaviour |
|---|---|---|
| **`IDLE`** | Cute eyes with periodic randomized natural blinks | Mic streams audio; Host VAD monitors for speech onset |
| **`LISTENING`** | Wide alert eyes with pupil sparkle | Speech detected; Host captures voice segment |
| **`THINKING`** | Squint eyes with scanning dot marquee | STT transcribes & Ollama generates response |
| **`SPEAKING`** | Expressive eyes with 4-frame animated talking mouth | Piper TTS streams audio; Satellite mutes mic & plays sound |

---

## 📁 Project Structure

```text
spark/
├── host_agent.py               # PC Host server (VAD + STT + LLM + TTS)
├── uno_q_satellite.py          # UNO Q satellite client (Audio I/O + Matrix bridge)
├── firmware/
│   └── led_matrix_face/
│       └── led_matrix_face.ino # STM32U585 LED matrix firmware (8x12 animations)
├── config_host.json            # Host configuration (Models, ports, prompts)
├── config_satellite.json       # Satellite configuration (IP, devices, serial)
├── requirements_host.txt       # Host Python dependencies
├── requirements_satellite.txt  # Satellite Python dependencies
├── spark.desktop               # Linux desktop launcher entry
├── DISTRIBUTED_README.md       # Extended distributed architecture documentation
├── LICENSE                     # MIT License
└── README.md                   # This file
```

---

## ⚙️ Configuration Reference

### `config_host.json`
* **`server.port`**: WebSocket port (default: `8765`).
* **`llm.model`**: Ollama model (default: `gemma4:e2b`).
* **`llm.system_prompt`**: Persona and behavior instructions for Spark.
* **`vad.threshold`**: Speech detection sensitivity (default: `0.5`).
* **`stt.model_size`**: Whisper transcription size (default: `base.en`).
* **`tts.voice_model`**: Piper ONNX model path.

### `config_satellite.json`
* **`host_url`**: Target WebSocket URL pointing to PC host (`ws://<pc-ip>:8765`).
* **`audio.device_keyword`**: Keyword to identify USB headset (default: `USB`).
* **`stm32.port`**: Serial port for matrix coprocessor bridge (`/dev/ttyRPMSG0`).

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
