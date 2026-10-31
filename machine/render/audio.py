"""Narration track: place each scene's speech at its scene start + LEAD, write one WAV, mux with the video."""
from __future__ import annotations

import io
import subprocess
import wave

import numpy as np

from .video import LEAD, SceneTime

SR = 24000


def wav_to_array(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError("expected 16-bit audio")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != SR:
        n = int(len(a) * SR / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return a, SR


def trim_silence(a: np.ndarray, thresh: float = 0.004) -> np.ndarray:
    idx = np.where(np.abs(a) > thresh)[0]
    if len(idx) == 0:
        return a
    start = max(0, idx[0] - int(0.03 * SR))
    end = min(len(a), idx[-1] + int(0.08 * SR))
    return a[start:end]


def duration(a: np.ndarray) -> float:
    return len(a) / SR


def build_track(clips: list[np.ndarray], timeline: list[SceneTime], total: float, out_wav: str) -> None:
    track = np.zeros(int((total + 0.5) * SR), dtype=np.float32)
    for clip, tm in zip(clips, timeline):
        st = int(tm.speech_start * SR)
        end = min(len(track), st + len(clip))
        track[st:end] += clip[: end - st]
    peak = float(np.max(np.abs(track))) or 1.0
    if peak > 0.98:
        track *= 0.98 / peak
    pcm = (track * 32767).astype(np.int16)
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def mux(video_path: str, wav_path: str, out_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-i", wav_path,
           "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-shortest", "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg mux failed: " + r.stderr[-500:])


def media_duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def placeholder_speech(seconds: float) -> np.ndarray:
    """Silent clip (used when rendering a preview without a voice key)."""
    return np.zeros(int(seconds * SR), dtype=np.float32)
