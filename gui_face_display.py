"""
Spark ⚡ — Optional Screen GUI Face Module

This module provides an optional on-screen (HDMI / DSI LCD / Desktop) face
animation engine using Tkinter. It can be toggled on/off via configuration.

When enabled:
- Displays animated face expressions for states: IDLE, LISTENING, THINKING, SPEAKING.
- Loads custom PNG animation frames from the 'faces/' directory if present.
- Seamlessly falls back to procedural vector eyes (Tkinter canvas) if PNG frames are absent.

Usage:
    # Standalone test:
    python gui_face_display.py

    # Programmatic integration:
    from gui_face_display import ScreenFaceEngine
    engine = ScreenFaceEngine(fullscreen=False)
    engine.start()
    engine.set_state("LISTENING")
"""

import os
import sys
import time
import random
import threading
import logging
from typing import Dict, List, Optional

try:
    import tkinter as tk
    from PIL import Image, ImageTk
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

logger = logging.getLogger("spark.screen_gui")


class ScreenFaceEngine:
    """Tkinter-based GUI face animation engine."""

    def __init__(
        self,
        faces_dir: str = "faces",
        width: int = 800,
        height: int = 480,
        fullscreen: bool = False,
    ):
        self.faces_dir = faces_dir
        self.width = width
        self.height = height
        self.fullscreen = fullscreen

        self.root: Optional[tk.Tk] = None
        self.canvas: Optional[tk.Canvas] = None
        self.current_state = "IDLE"
        self.is_running = False
        self._thread: Optional[threading.Thread] = None

        self.frames: Dict[str, List] = {
            "IDLE": [],
            "LISTENING": [],
            "THINKING": [],
            "SPEAKING": [],
            "ERROR": [],
        }
        self.current_frame_idx = 0
        self._last_blink = time.time()
        self._is_blinking = False

    def load_frames(self):
        """Loads PNG frames from the faces directory if available."""
        if not HAS_GUI:
            return

        state_mappings = {
            "IDLE": "idle",
            "LISTENING": "listening",
            "THINKING": "thinking",
            "SPEAKING": "speaking",
            "ERROR": "error",
        }

        for state, folder_name in state_mappings.items():
            folder_path = os.path.join(self.faces_dir, folder_name)
            self.frames[state] = []
            if os.path.isdir(folder_path):
                files = sorted(
                    [f for f in os.listdir(folder_path) if f.lower().endswith((".png", ".jpg", ".jpeg"))]
                )
                for f in files:
                    try:
                        img = Image.open(os.path.join(folder_path, f)).resize(
                            (self.width, self.height), Image.Resampling.LANCZOS
                        )
                        self.frames[state].append(ImageTk.PhotoImage(img))
                    except Exception as exc:
                        logger.warning("Failed loading frame %s: %s", f, exc)

    def _draw_procedural_face(self):
        """Draws cute modern animated vector eyes when PNG frames are absent."""
        if not self.canvas:
            return

        self.canvas.delete("all")
        bg_color = "#0B0F19"
        eye_color = "#38BDF8"  # Cyan spark glow

        self.canvas.configure(bg=bg_color)
        cx, cy = self.width // 2, self.height // 2
        eye_spacing = self.width // 5
        eye_w, eye_h = self.width // 10, self.height // 5

        now = time.time()
        # Blink logic
        if now - self._last_blink > random.uniform(2.5, 4.5):
            self._is_blinking = True
            self._last_blink = now

        if self._is_blinking and (now - self._last_blink < 0.15):
            # Squint line for blink
            for offset in (-eye_spacing, eye_spacing):
                self.canvas.create_line(
                    cx + offset - eye_w, cy,
                    cx + offset + eye_w, cy,
                    fill=eye_color, width=6, capstyle=tk.ROUND
                )
            return
        else:
            self._is_blinking = False

        if self.current_state == "IDLE":
            for offset in (-eye_spacing, eye_spacing):
                self.canvas.create_oval(
                    cx + offset - eye_w, cy - eye_h,
                    cx + offset + eye_w, cy + eye_h,
                    fill=eye_color, outline=""
                )
        elif self.current_state == "LISTENING":
            # Alert wide eyes with sparkle pupil
            for offset in (-eye_spacing, eye_spacing):
                self.canvas.create_oval(
                    cx + offset - eye_w - 6, cy - eye_h - 12,
                    cx + offset + eye_w + 6, cy + eye_h + 12,
                    fill="#34D399", outline=""  # Greenish alert
                )
                self.canvas.create_oval(
                    cx + offset - 8, cy - eye_h + 4,
                    cx + offset + 8, cy - eye_h + 20,
                    fill="#FFFFFF", outline=""
                )
        elif self.current_state == "THINKING":
            # Squinted eyes with pulsing pupil
            pulse = int(10 * abs(random.random() - 0.5))
            for offset in (-eye_spacing, eye_spacing):
                self.canvas.create_arc(
                    cx + offset - eye_w, cy - eye_h,
                    cx + offset + eye_w, cy + eye_h,
                    start=30, extent=120, fill="#FBBF24", outline=""
                )
        elif self.current_state == "SPEAKING":
            # Talking mouth + expressive eyes
            for offset in (-eye_spacing, eye_spacing):
                self.canvas.create_oval(
                    cx + offset - eye_w, cy - eye_h,
                    cx + offset + eye_w, cy + eye_h,
                    fill=eye_color, outline=""
                )
            mouth_open = random.randint(10, 35)
            self.canvas.create_oval(
                cx - 30, cy + eye_h + 15,
                cx + 30, cy + eye_h + 15 + mouth_open,
                fill=eye_color, outline=""
            )

    def _animation_tick(self):
        """Periodic loop to update frames or redraw vector canvas."""
        if not self.is_running or not self.root:
            return

        frames = self.frames.get(self.current_state, [])
        if frames:
            # Render PNG frame
            self.current_frame_idx = (self.current_frame_idx + 1) % len(frames)
            frame = frames[self.current_frame_idx]
            self.canvas.delete("all")
            self.canvas.create_image(self.width // 2, self.height // 2, image=frame)
        else:
            # Render procedural vector face
            self._draw_procedural_face()

        speed = 100 if self.current_state == "SPEAKING" else 200
        self.root.after(speed, self._animation_tick)

    def set_state(self, state: str):
        """Sets the active facial expression state: IDLE, LISTENING, THINKING, SPEAKING."""
        state = state.upper().strip()
        if state in self.frames or state in ("IDLE", "LISTENING", "THINKING", "SPEAKING"):
            self.current_state = state
            self.current_frame_idx = 0

    def _gui_thread_worker(self):
        if not HAS_GUI:
            logger.error("Tkinter/PIL is not installed. Screen GUI cannot start.")
            return

        self.root = tk.Tk()
        self.root.title("Spark ⚡ Face Display")
        self.root.geometry(f"{self.width}x{self.height}")
        self.root.configure(bg="#0B0F19")

        if self.fullscreen:
            self.root.attributes("-fullscreen", True)
            self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))

        self.canvas = tk.Canvas(
            self.root, width=self.width, height=self.height, bg="#0B0F19", highlightthickness=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.load_frames()
        self.is_running = True
        self.root.after(100, self._animation_tick)

        try:
            self.root.mainloop()
        finally:
            self.is_running = False

    def start(self):
        """Launches the GUI in a background daemon thread."""
        if not HAS_GUI:
            logger.warning("Tkinter/PIL unavailable; skipping screen GUI start.")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._gui_thread_worker, daemon=True)
        self._thread.start()

    def stop(self):
        """Stops the GUI face display."""
        self.is_running = False
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Testing Spark Screen Face GUI. Press Ctrl+C or close window to exit.")
    engine = ScreenFaceEngine(fullscreen=False)
    engine._gui_thread_worker()
