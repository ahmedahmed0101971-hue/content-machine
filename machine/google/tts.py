"""Google Cloud Text-to-Speech (REST + API key). One request per scene, LINEAR16 WAV back."""
from __future__ import annotations

import base64
import io
import math
import wave

import numpy as np

from .. import config
from .http import GoogleError, check, request

URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
VOICES = [
    ("en-US-Chirp3-HD-Charon", "Charon · رجالي هادي وواضح"),
    ("en-US-Chirp3-HD-Orus", "Orus · رجالي عميق"),
    ("en-US-Chirp3-HD-Puck", "Puck · رجالي حيوي"),
    ("en-US-Chirp3-HD-Fenrir", "Fenrir · رجالي قوي"),
    ("en-US-Chirp3-HD-Aoede", "Aoede · ستاتي دافي"),
    ("en-US-Chirp3-HD-Kore", "Kore · ستاتي واثق"),
    ("en-US-Chirp3-HD-Leda", "Leda · ستاتي شبابي"),
    ("en-US-Chirp3-HD-Zephyr", "Zephyr · ستاتي هادي"),
]


def synthesize(text: str, voice: str, rate: float = 1.0) -> bytes:
    """Returns WAV bytes (24 kHz mono 16-bit)."""
    if config.OFFLINE:
        return mock_wav(text, rate)
    key = config.load_settings()["google"].get("tts_api_key")
    if not key:
        raise GoogleError("مفتاح الصوت (TTS API key) مش موجود في الإعدادات")
    lang = "-".join(voice.split("-")[:2])
    body = {"input": {"text": text}, "voice": {"languageCode": lang, "name": voice},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000}}
    if abs(rate - 1.0) > 0.01:
        body["audioConfig"]["speakingRate"] = rate
    r = request("POST", URL, params={"key": key}, json=body)
    if r.status_code == 400 and "speakingRate" in body["audioConfig"] and "speaking" in r.text.lower():
        body["audioConfig"].pop("speakingRate")   # some voices don't support pace control
        r = request("POST", URL, params={"key": key}, json=body)
    data = check(r, "توليد الصوت")
    audio = base64.b64decode(data["audioContent"])
    if not audio.startswith(b"RIFF"):
        raise GoogleError("جوجل رجّع صوت بصيغة غير متوقعة")
    return audio


def mock_wav(text: str, rate: float = 1.0) -> bytes:
    """Offline stand-in: a soft tone whose length matches natural speech pace (~15 chars/sec)."""
    sr = 24000
    dur = max(1.0, len(text) / (15.0 * rate))
    t = np.arange(int(dur * sr)) / sr
    env = np.minimum(1, np.minimum(t / 0.05, (dur - t) / 0.05))
    sig = 0.12 * np.sin(2 * math.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * math.pi * 3 * t)) * env
    pcm = (sig * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
