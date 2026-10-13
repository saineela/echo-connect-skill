#!/usr/bin/env python3
"""NIX Echo Connect worker: ESPHome native API client and bounded tool adapter.

This implementation is based only on the published ESPHome native API
client/protocol. It does not copy the unlicensed upstream bridge or client.
Volume, mute, and stop playback act on the Dot's own ESPHome media player
entity. The say action renders speech on the NIX host: the Unreal Speech cloud
voice when the operator configured one or more API keys, otherwise (and as a
fallback) the built-in local Piper/espeak path. The worker accepts the NIX
JSONL contract on stdin/stdout; diagnostics go to stderr. Only calls returned
by its fixed control_ring tool can reach the Dot.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import ipaddress
import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from typing import Any

PROTOCOL = "nix-skill-jsonl-v1"

VOLUME_STEPS = 30


# ── Local neural TTS ───────────────────────────────────────────────────────
# Piper (local onnx) renders speech to a WAV the Dot fetches over the LAN. It
# is instant, offline and lightweight; espeak-ng stays as the last resort.
# The catalog is what the console Voice Lab offers, and the DEFAULT_* values
# are the tuned Nix voice chosen from that lab.
_TTS_DIR = os.environ.get("NIX_TTS_DIR", "/root/data/tts")
_PIPER_FALLBACK_PYTHON = (
    "/root/data/skills/runtime_envs/fb08f7b46e0c91a3861edf4ccf8ccfeac59f16e8a4e8725ccab337b75c453987"
    "/.venv/bin/python"
)
# Voice id -> Piper voice stem. All entries are female English voices.
_VOICE_CATALOG = {
    "amy": "en_US-amy-medium",
    "lessac": "en_US-lessac-medium",
    "hfc_female": "en_US-hfc_female-medium",
    "kathleen": "en_US-kathleen-low",
    "jenny": "en_GB-jenny_dioco-medium",
    "alba": "en_GB-alba-medium",
}
DEFAULT_VOICE = "amy"          # cute, bright US female
DEFAULT_SPEED = 1.05           # 1.0 = neutral pace, higher is faster
DEFAULT_EXPRESSIVENESS = 0.7   # Piper noise_scale
DEFAULT_VARIABILITY = 0.8      # Piper noise_w_scale
SPEED_MIN, SPEED_MAX = 0.5, 1.6

# ── Cloud TTS (Unreal Speech) ─────────────────────────────────────────────
# Optional provider (https://docs.v8.unrealspeech.com). With one to three API
# keys stored in setup, /speech renders the announcement in a cloud voice and
# the local engine above becomes the fallback. Characters and generated audio
# seconds are tracked per key per UTC month, so a key that reaches the switch
# threshold hands the next announcement to the following key (or to the local
# voice when every key is spent) instead of failing mid-request.
_UNREAL_SPEECH_BASE = os.environ.get(
    "NIX_UNREALSPEECH_BASE_URL", "https://api.v8.unrealspeech.com"
).rstrip("/")
# Config voice id -> the VoiceId the v8 /speech endpoint accepts. The provider
# replaced its older catalog (Amy/Scarlett/Dan/Liv/Will are now rejected with
# HTTP 400 "Invalid VoiceId"), so each saved option maps to a current voice of
# the same character. Keeping the config ids stable means an already-saved
# ``unrealspeech_voice`` keeps working without reconfiguration.
_CLOUD_VOICE_CATALOG = {
    "amy": "Amelia",
    "scarlett": "Charlotte",
    "dan": "Daniel",
    "liv": "Lauren",
    "will": "Oliver",
}
DEFAULT_CLOUD_VOICE = "amy"
DEFAULT_CLOUD_CHARACTER_LIMIT = 250_000   # Unreal Speech free tier, per key per month
DEFAULT_CLOUD_SECONDS_LIMIT = 6 * 3600    # six generated audio hours, per key per month
CLOUD_SWITCH_THRESHOLD = 0.9              # rotate once a key is 90% used
CLOUD_KEY_FIELDS = ("unrealspeech_api_key", "unrealspeech_api_key_2", "unrealspeech_api_key_3")
_CLOUD_HTTP_TIMEOUT = 12.0
_CLOUD_DOWNLOAD_LIMIT = 12 * 1024 * 1024


_PIPER_PYTHON_CACHE: str | None = None


def _piper_python() -> str:
    """First interpreter that can actually import piper (own venv, then fallback)."""
    global _PIPER_PYTHON_CACHE
    if _PIPER_PYTHON_CACHE and os.path.isfile(_PIPER_PYTHON_CACHE):
        return _PIPER_PYTHON_CACHE
    candidates = [sys.executable, _PIPER_FALLBACK_PYTHON]
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import piper"],
                timeout=60,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            _PIPER_PYTHON_CACHE = candidate
            return candidate
    return sys.executable


def _voice_model_path(voice: str) -> str | None:
    """Onnx path for a catalog voice, only when the voice is fully on disk.

    Piper loads a voice from the onnx weights *and* the ``.onnx.json`` config
    beside them, so a half-finished download (which leaves a truncated onnx and
    no config) must count as missing: otherwise every request for that voice
    silently falls back to the robotic espeak-ng path instead of re-fetching.
    """
    stem = _VOICE_CATALOG.get(voice)
    if stem is None:
        return None
    path = os.path.join(_TTS_DIR, f"{stem}.onnx")
    config = f"{path}.json"
    if os.path.isfile(path) and os.path.isfile(config) and os.path.getsize(path) > 1_000_000:
        return path
    return None


def _ensure_voice(voice: str) -> str | None:
    """Onnx path for a catalog voice, fetching it once from the Piper voices repo."""
    path = _voice_model_path(voice)
    if path is not None:
        return path
    stem = _VOICE_CATALOG.get(voice)
    if stem is None:
        return None
    try:
        os.makedirs(_TTS_DIR, exist_ok=True)
        subprocess.run(
            [_piper_python(), "-m", "piper.download_voices", "--download-dir", _TTS_DIR, stem],
            timeout=600,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _voice_model_path(voice)


# A piper voice is ~60 MB of onnx weights. Spawning `python -m piper` per
# sentence pays interpreter start plus that model load every single time, which
# is most of the wait a user hears before the Dot speaks. This helper process
# loads each voice once and answers jobs as JSON lines, so repeated speech costs
# only synthesis. It is best-effort: any failure falls back to the CLI path.
_PIPER_HELPER_SOURCE = r'''
import json, os, sys, wave
from piper import PiperVoice
try:
    from piper.config import SynthesisConfig
except Exception:  # older piper exposes flags on the CLI only
    SynthesisConfig = None
voices = {}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        job = json.loads(line)
    except Exception:
        continue
    try:
        model = job["model"]
        voice = voices.get(model)
        if voice is None:
            voice = PiperVoice.load(model)
            voices[model] = voice
        out = job["out"]
        syn_config = None
        if SynthesisConfig is not None:
            syn_config = SynthesisConfig(
                length_scale=float(job["length_scale"]),
                noise_scale=float(job["noise_scale"]),
                noise_w_scale=float(job["noise_w_scale"]),
            )
        with wave.open(out, "wb") as wav_file:
            voice.synthesize_wav(job["text"], wav_file, syn_config=syn_config)
        size = os.path.getsize(out) if os.path.isfile(out) else 0
        sys.stdout.write(json.dumps({"id": job.get("id"), "ok": size > 44, "bytes": size, "path": out, "warm": bool(job.get("warm"))}) + "\n")
    except Exception as exc:
        sys.stdout.write(json.dumps({"id": job.get("id") if isinstance(job, dict) else None,
                                     "ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}) + "\n")
    sys.stdout.flush()
'''


class _PiperDaemon:
    """One long-lived piper process with loaded voices kept in memory."""

    def __init__(self, python: str, timeout: float = 90.0):
        self.python = python
        self.timeout = timeout
        self.process: subprocess.Popen | None = None
        self.responses: "queue.Queue[str | None]" = queue.Queue()
        self.lock = threading.Lock()
        self.counter = 0
        # The background warm-up and a user's `say` share this one helper, so
        # jobs run one at a time: overlapping jobs would steal each other's
        # correlated replies and each would wait out the whole timeout.
        self.job_lock = threading.Lock()

    def start(self) -> bool:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return True
            try:
                self.process = subprocess.Popen(
                    [self.python, "-I", "-c", _PIPER_HELPER_SOURCE],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", bufsize=1, close_fds=True,
                )
            except (OSError, subprocess.SubprocessError):
                self.process = None
                return False
            self.responses = queue.Queue()
            threading.Thread(target=self._read_loop, daemon=True, name="nix-piper-reader").start()
            return True

    def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.responses.put(line)
        finally:
            self.responses.put(None)

    def synthesize(self, *, text: str, model: str, out: str, length_scale: float,
                   noise_scale: float, noise_w_scale: float, warm: bool = False) -> bool:
        """Render one WAV through the cached voices; False means "fall back"."""
        if not self.start():
            return False
        with self.job_lock:
            # `self.process` and `self.responses` are shared with stop(), which
            # can run on another thread: a concurrent `say`, or _piper_daemon()
            # retiring this helper when the chosen interpreter changes. Take
            # both handles once, under the job lock this method holds for the
            # whole job, so neither can go away between the check and its use.
            process, responses = self.process, self.responses
            if process is None or process.stdin is None:
                return False
            self.counter += 1
            job_id = f"j{self.counter}"
            job = {
                "id": job_id, "model": model, "out": out, "text": text, "warm": warm,
                "length_scale": length_scale, "noise_scale": noise_scale, "noise_w_scale": noise_w_scale,
            }
            try:
                process.stdin.write(json.dumps(job) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                self.stop()
                return False
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stop()
                    return False
                try:
                    line = responses.get(timeout=remaining)
                except queue.Empty:
                    self.stop()
                    return False
                if line is None:
                    self.stop()
                    return False
                try:
                    answer = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if answer.get("id") != job_id:
                    continue
                if answer.get("ok") is not True:
                    return False
                return True

    def stop(self) -> None:
        with self.lock:
            process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


_PIPER_DAEMON: _PiperDaemon | None = None
_PIPER_DAEMON_LOCK = threading.Lock()


def _piper_daemon() -> _PiperDaemon:
    global _PIPER_DAEMON
    with _PIPER_DAEMON_LOCK:
        if _PIPER_DAEMON is None or _PIPER_DAEMON.python != _piper_python():
            if _PIPER_DAEMON is not None:
                _PIPER_DAEMON.stop()
            _PIPER_DAEMON = _PiperDaemon(_piper_python())
        return _PIPER_DAEMON


def _cached_speech_path(out_dir: str, text: str, voice: str, speed: float, expressiveness: float, variability: float, provider: str = "local") -> str:
    """Deterministic WAV name: repeated phrases are replayed instead of re-rendered.
    The provider is part of the digest so a local render never shadows a cloud one."""
    digest = hashlib.sha256(
        f"{provider}|{voice}|{speed:.4f}|{expressiveness:.4f}|{variability:.4f}|{text}".encode("utf-8")
    ).hexdigest()[:32]
    return os.path.join(out_dir, f"say-{digest}.wav")


def _synthesize_speech_wav(
    text: str,
    out_dir: str,
    name: str,
    *,
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    expressiveness: float = DEFAULT_EXPRESSIVENESS,
    variability: float = DEFAULT_VARIABILITY,
) -> str:
    """Render one WAV file with the local TTS engine. Raises RuntimeError
    with user language when synthesis is impossible."""
    wav_path = os.path.join(out_dir, f"{name}.wav")
    model_path = _ensure_voice(voice)
    if model_path:
        length_scale = 1.0 / max(SPEED_MIN, min(SPEED_MAX, float(speed)))
        noise_scale, noise_w_scale = float(expressiveness), float(variability)
        cached = _cached_speech_path(out_dir, text, voice, float(speed), noise_scale, noise_w_scale)
        if os.path.isfile(cached) and os.path.getsize(cached) > 44:
            return cached
        # Preferred: the warm piper helper, which keeps the voice in memory.
        try:
            if _piper_daemon().synthesize(
                text=text, model=model_path, out=wav_path, length_scale=length_scale,
                noise_scale=noise_scale, noise_w_scale=noise_w_scale,
            ) and os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44:
                os.replace(wav_path, cached)
                return cached
        except OSError:
            pass
        try:
            subprocess.run(
                [
                    _piper_python(), "-m", "piper",
                    "-m", model_path,
                    "-f", wav_path,
                    "--length-scale", f"{length_scale:.4f}",
                    "--noise-scale", f"{noise_scale:.4f}",
                    "--noise-w-scale", f"{noise_w_scale:.4f}",
                    "--", text,
                ],
                timeout=90,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            pass  # fall through to espeak-ng
        else:
            if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44:
                os.replace(wav_path, cached)
                return cached
    # Fallback: espeak-ng robotic voice.
    try:
        subprocess.run(
            ["espeak-ng", "-a", "170", "-s", "150", "-w", wav_path, text],
            timeout=30,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "No text-to-speech engine is available on the server. Install "
            "piper-tts or espeak-ng, then try again."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Speech synthesis took too long; try a shorter text.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Speech synthesis failed on the server; try again.") from exc
    if not os.path.isfile(wav_path) or os.path.getsize(wav_path) < 44:
        raise RuntimeError("Speech synthesis produced no audio; try again.")
    try:
        os.replace(wav_path, cached)
    except OSError:
        return wav_path
    return cached


class _CloudKeyExhausted(RuntimeError):
    """The provider refused this key for the rest of its monthly allowance."""


def _cloud_usage_path() -> str:
    """Where per-key monthly usage is remembered (override for tests)."""
    return os.environ.get("NIX_TTS_USAGE_FILE") or os.path.join(_TTS_DIR, "unrealspeech-usage.json")


def _cloud_month(stamp: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(stamp))


def _cloud_key_id(api_key: str) -> str:
    """Stable non-reversible slot id, so the usage file never stores the key."""
    return hashlib.sha256(f"nix-unrealspeech:{api_key}".encode("utf-8")).hexdigest()[:16]


def _read_cloud_usage() -> dict[str, Any]:
    try:
        with open(_cloud_usage_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    keys = data.get("keys") if isinstance(data, dict) else None
    return keys if isinstance(keys, dict) else {}


def _write_cloud_usage(keys: dict[str, Any]) -> None:
    """Persist usage atomically. Accounting is best-effort: an unwritable data
    directory must never turn a spoken announcement into an error."""
    path = _cloud_usage_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temporary = f"{path}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "keys": keys}, handle, separators=(",", ":"))
        os.replace(temporary, path)
    except OSError:
        pass


def _wav_seconds(path: str) -> float:
    """Exact duration of a PCM WAV, so billed audio time is measured, not guessed."""
    try:
        with wave.open(path, "rb") as handle:
            rate, frames = handle.getframerate(), handle.getnframes()
    except (OSError, wave.Error):
        return 0.0
    return round(frames / rate, 3) if rate else 0.0


def _cloud_quota_error(status: int, detail: str) -> bool:
    """True when the key is spent or unusable, rather than this one request
    being unlucky. A rate limit or an upstream 5xx must not burn a key for the
    whole month."""
    if status in (401, 402, 403):
        return True
    lowered = detail.casefold()
    return any(
        word in lowered
        for word in ("quota", "allowance", "insufficient", "credit", "limit reached", "exceeded your")
    )


def _clamp_cloud_limit(value: Any, default: int, label: str) -> int:
    if value in (None, ""):
        return default
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError(f"{label} must be a whole number of 1 or more")
    number = int(text)
    if not 1 <= number <= 100_000_000:
        raise ValueError(f"{label} must be from 1 to 100000000")
    return number


def _clamp_cloud_hours(value: Any, default_hours: float, label: str) -> int:
    if value in (None, ""):
        return int(round(default_hours * 3600))
    try:
        hours = float(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number of hours") from exc
    if not math.isfinite(hours) or not 0.1 <= hours <= 10_000:
        raise ValueError(f"{label} must be from 0.1 to 10000 hours")
    return int(round(hours * 3600))


def _announcement_target(playable: list[dict[str, Any]]) -> dict[str, Any]:
    """The device's own announcement WAV entry when it reports one, else the
    16 kHz mono channel an Echo Dot 2 advertises for announcements."""
    for purpose in ("announcement", "default", "media"):
        for entry in playable:
            if entry.get("purpose") == purpose and entry.get("format") == "wav":
                return entry
    return {"format": "wav", "sample_rate": 16000, "num_channels": 1,
            "purpose": "announcement", "sample_bytes": 2}


def _cloud_speed(speed: float) -> str:
    """Piper's 0.5-1.6 pace maps onto the API's -1.0..1.0 around 1.0 = neutral."""
    return f"{max(-1.0, min(1.0, float(speed) - 1.0)):.2f}"


def _download_cloud_audio(url: str, dest: str) -> None:
    """Fetch the provider's MP3 in one bounded request: these clips are short,
    so a ranged resume is unnecessary, but the size cap keeps a bad response
    from filling the disk."""
    request = urllib.request.Request(url, headers={"User-Agent": "nix-echo-connect"})
    try:
        with urllib.request.urlopen(request, timeout=_CLOUD_HTTP_TIMEOUT) as response, open(dest, "wb") as handle:
            total = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _CLOUD_DOWNLOAD_LIMIT:
                    raise RuntimeError("the synthesized audio was unexpectedly large")
                handle.write(chunk)
    except urllib.error.URLError as error:
        raise RuntimeError(f"could not download the synthesized audio: {error.reason}") from error
    if not os.path.isfile(dest) or os.path.getsize(dest) < 128:
        raise RuntimeError("the synthesized audio was empty")


def _unrealspeech_speech_wav(
    text: str,
    out_dir: str,
    name: str,
    *,
    api_key: str,
    voice: str,
    speed: float,
    playable: list[dict[str, Any]],
) -> tuple[str, float, bool]:
    """Render one announcement WAV with the Unreal Speech cloud voice.

    Returns (path, seconds, billed): a phrase already cached is replayed
    without calling the API, so it consumes no monthly allowance. Raises
    _CloudKeyExhausted when this key is spent for the month and RuntimeError
    for every other failure, so the caller can move to the next key and then
    to the local engine.
    """
    cached = _cached_speech_path(out_dir, text, voice, float(speed), 0.0, 0.0, provider="unrealspeech")
    if os.path.isfile(cached) and os.path.getsize(cached) > 44:
        return cached, _wav_seconds(cached), False
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("cloud speech needs ffmpeg to hand the Dot a playable WAV")
    request = urllib.request.Request(
        f"{_UNREAL_SPEECH_BASE}/speech",
        data=json.dumps({
            "Text": text,
            "VoiceId": _CLOUD_VOICE_CATALOG.get(voice, _CLOUD_VOICE_CATALOG[DEFAULT_CLOUD_VOICE]),
            "Bitrate": "192k",
            "Speed": _cloud_speed(speed),
            "Pitch": "1",
            "TimestampType": "sentence",
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "nix-echo-connect",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_CLOUD_HTTP_TIMEOUT) as response:
            body = response.read(64 * 1024)
    except urllib.error.HTTPError as error:
        detail = error.read(2048).decode("utf-8", "replace").strip() if error.fp else ""
        summary = f"Unreal Speech HTTP {error.code}: {detail[:120] or error.reason}"
        if _cloud_quota_error(error.code, detail):
            raise _CloudKeyExhausted(summary) from error
        raise RuntimeError(summary) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"could not reach Unreal Speech: {error.reason}") from error
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RuntimeError("Unreal Speech returned an unreadable response") from exc
    output_uri = data.get("OutputUri") if isinstance(data, dict) else None
    if not isinstance(output_uri, str) or not output_uri.lower().startswith(("http://", "https://")):
        raise RuntimeError(
            f"Unreal Speech returned no audio URL: {json.dumps(data)[:140] if isinstance(data, dict) else ''}"
        )
    download = os.path.join(out_dir, f"{name}.mp3")
    try:
        _download_cloud_audio(output_uri, download)
        path = os.path.join(out_dir, f"{name}.wav")
        _convert_audio(download, ffmpeg, path, _announcement_target(playable))
    finally:
        try:
            os.remove(download)
        except OSError:
            pass
    if not os.path.isfile(path) or os.path.getsize(path) <= 44:
        raise RuntimeError("Unreal Speech produced no usable audio")
    os.replace(path, cached)
    return cached, _wav_seconds(cached), True


def _local_ip_toward(device_ip: str) -> str:
    """The server's LAN IP on the route to the device (for a reachable URL)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((device_ip, 6053))
        return probe.getsockname()[0]
    finally:
        probe.close()


class _SpeechFileServer:
    """Minimal HTTP server that serves only the worker's speech directory,
    so the Dot can fetch the WAV by URL."""

    def __init__(self, root_dir: str) -> None:
        self.root = root_dir
        handler = type(
            "Handler",
            (http.server.SimpleHTTPRequestHandler,),
            {"__init__": lambda self_, *a, **kw: super(type(self_), self_).__init__(*a, directory=root_dir, **kw)},
        )

        def _quiet(*_args, **_kwargs) -> None:
            pass

        handler.log_message = _quiet  # type: ignore[assignment]
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self.port = self.httpd.server_address[1]  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()


def _clamp_volume(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("volume must be a finite number from 0.0 to 1.0")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError("volume must be from 0.0 to 1.0")
    return float(value)


def _clamp_range(value: Any, low: float, high: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number from {low} to {high}")
    if not low <= float(value) <= high:
        raise ValueError(f"{label} must be from {low} to {high}")
    return float(value)


def _clamp_say_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("say needs a text string")
    text = " ".join(value.split())
    if not 1 <= len(text) <= 500:
        raise ValueError("say text must be 1 to 500 characters")
    return text


def _clamp_wait(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("wait must be a boolean")
    return value


def _clamp_say_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("say_from_url needs a url string")
    url = value.strip()
    if not 1 <= len(url) <= 500 or not url.lower().startswith(("http://", "https://")):
        raise ValueError("say_from_url needs an http(s) URL")
    if any(character in url for character in "\r\n\t\x00"):
        raise ValueError("say_from_url url contains control characters")
    return url


# ── URL playback (music, not announcements) ───────────────────────────────
# play_url streams an http(s) audio URL through the Dot's ESPHome media
# player. Formats the device cannot decode are converted locally with ffmpeg
# and served over the LAN by the same tiny file server the speech path uses.
# Bound on a single stream download: how many bytes to ask for at once, from
# the whole file down to a partial opening, and how long one request may take.
_FETCH_BOUNDED_SIZES = (1 << 21, 1 << 20, 1 << 19, 1 << 18, 1 << 17)
_FETCH_TIMEOUT_SECONDS = 20

_MIME_CONTAINER = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/x-mpeg": "mp3",
    "audio/mp4": "m4a", "audio/aac": "aac", "audio/aacp": "aac",
    "audio/ogg": "ogg", "application/ogg": "ogg",
    "audio/flac": "flac", "audio/x-flac": "flac",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/vnd.wave": "wav", "audio/webm": "webm",
}
_CONTAINER_MIME = {
    "wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4",
    "aac": "audio/aac", "ogg": "audio/ogg", "flac": "audio/flac",
}


def _clamp_media_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("play_url needs a url string")
    url = value.strip()
    if not 1 <= len(url) <= 2048 or not url.lower().startswith(("http://", "https://")):
        raise ValueError("play_url needs an http(s) URL")
    if any(character in url for character in "\r\n\t\x00"):
        raise ValueError("play_url url contains control characters")
    return url


def _clamp_media_title(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("play_url title must be text")
    return " ".join(value.split())[:200]


def _find_ffmpeg() -> str | None:
    """ffmpeg for play_url conversions: NIX_FFMPEG, PATH, or the bundled
    imageio-ffmpeg binary."""
    exe = os.environ.get("NIX_FFMPEG")
    if exe and os.path.isfile(exe):
        return exe
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _probe_content_type(url: str) -> str:
    """Upstream Content-Type without downloading the body. A 1-byte Range
    probe first (some proxies refuse a body-less plain GET), then a plain
    GET as a fallback."""
    last: Exception | None = None
    for headers in ({"User-Agent": "nix-echo-connect", "Range": "bytes=0-0"},
                    {"User-Agent": "nix-echo-connect"}):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.headers.get("Content-Type", "") or ""
        except urllib.error.HTTPError as error:
            content_type = error.headers.get("Content-Type", "") if error.headers else ""
            if error.code < 500 and content_type:
                return content_type
            last = error
        except urllib.error.URLError as error:
            last = error
    raise RuntimeError(f"could not reach {url}: {last}")


def _fetch_remote(url: str, dest: str) -> int:
    """Download as much of url as its origin will serve, and return how many
    bytes arrived.

    One plain streaming GET first: a progressive stream answers it whole. An
    origin that serves only the opening of a stream (OpenDeck relays a CDN URL,
    and the CDN refuses a DASH audio URL past its first ~256 KiB) refuses that
    GET outright, so every remaining attempt is a single *bounded* range at a
    smaller size - each one request, no retry storm, no sleeping, so a stream
    that cannot be fetched completely costs a few hundred milliseconds instead
    of a long stall. The first response that carries bytes wins even when it is
    only part of the stream: audio that plays beats a wait that ends in an
    error. Raises only when nothing at all could be fetched."""
    headers = {"User-Agent": "nix-echo-connect"}
    last: Exception | None = None
    attempts: list[dict[str, str]] = [{}, *({"Range": f"bytes=0-{size - 1}"} for size in _FETCH_BOUNDED_SIZES)]
    for extra in attempts:
        try:
            request = urllib.request.Request(url, headers={**headers, **extra})
            with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response, open(dest, "wb") as handle:
                shutil.copyfileobj(response, handle, 262144)
        except Exception as error:  # refused range, HTTP error, or timeout
            last = error
            continue
        size = os.path.getsize(dest)
        if size:
            return size
        last = RuntimeError("the origin returned an empty body")
    raise RuntimeError(f"could not fetch {url}: {last}")


def _conversion_target(playable: list[dict[str, Any]]) -> dict[str, Any]:
    """The format to convert to: the device's first default/media entry,
    preferring wav; falls back to 48 kHz stereo WAV when the device told us
    nothing."""
    for preferred in ("wav", "mp3"):
        for entry in playable:
            if entry.get("format") == preferred:
                return entry
    return {"format": "wav", "sample_rate": 48000, "num_channels": 2,
            "purpose": "default", "sample_bytes": 2}


def _convert_audio(src: str, ffmpeg: str, dest: str, target: dict[str, Any]) -> None:
    """Transcode a downloaded source file to the device's playable format,
    writing to a temp file and renaming into place so a concurrent fetch
    never sees a half-written track."""
    muxer = {"m4a": "ipod"}.get(str(target.get("format")), str(target.get("format")))
    args = [ffmpeg, "-loglevel", "error", "-nostdin", "-i", src, "-vn", "-acodec"]
    if target.get("format") == "wav":
        args += ["pcm_s16le", "-f", "wav", "-ar", str(int(target.get("sample_rate") or 48000)),
                 "-ac", str(int(target.get("num_channels") or 2))]
    elif target.get("format") == "mp3":
        args += ["libmp3lame", "-f", "mp3"]
    else:
        args += ["copy", "-f", muxer]
    tmp = dest + ".part"
    args.append(tmp)
    try:
        proc = subprocess.run(args, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"ffmpeg timed out converting {src}") from error
    if proc.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"could not convert the audio: {detail or 'ffmpeg produced no audio'}")
    os.replace(tmp, dest)


def _clamp_max_s(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("listen needs a finite max_s")
    if not 0.5 <= float(value) <= 60.0:
        raise ValueError("listen max_s must be from 0.5 to 60 seconds")


def _media_command(name: str) -> Any:
    """Send ESPHome media-player commands as their proper enum when available.

    The production aioesphomeapi expects MediaPlayerCommand members; test
    stubs (and very old installs) may not provide the enum, so fall back to
    the plain command-name string.
    """
    try:
        import aioesphomeapi  # resolved at call time; lazy like initialize
    except Exception:
        return name
    commands = getattr(aioesphomeapi, "MediaPlayerCommand", None)
    if commands is None:
        return name
    return getattr(commands, name, name)
    return float(value)


def _clamp_mic_updates(updates: Any) -> dict[str, Any]:
    if not isinstance(updates, dict) or not updates or set(updates) - {"mixing", "mute"}:
        raise ValueError("mic_config accepts 'mixing' and/or 'mute'")
    checked: dict[str, Any] = {}
    if "mixing" in updates:
        value = updates["mixing"]
        if not isinstance(value, str) or len(value) > 60:
            raise ValueError("mic_config mixing must be one of the device's mixing options")
        checked["mixing"] = value
    if "mute" in updates:
        checked["mute"] = _clamp_wait(updates["mute"])
    return checked


def _clamp_volume_step(delta: Any, current_volume: float | None) -> int:
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise ValueError("volume_step needs an integer delta")
    if current_volume is None:
        raise RuntimeError("no speaker state yet; use action 'volume' first")
    computed = current_volume + delta / VOLUME_STEPS
    if not 0.0 <= computed <= 1.0:
        raise ValueError("volume_step moves the level outside 0.0..1.0")
    return delta


def _rgb_float(rgb: tuple[int, int, int] | list[int]) -> tuple[float, float, float]:
    return tuple(float(channel) / 255.0 for channel in rgb)  # type: ignore[return-value]


def _rgb_byte(value: float) -> int:
    return max(0, min(255, round(float(value) * 255)))


def _clamp_brightness(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("brightness must be a finite number from 0.05 to 1.0")
    if not 0.05 <= float(value) <= 1.0:
        raise ValueError("brightness must be from 0.05 to 1.0")
    return float(value)


def _warm_speech() -> None:
    """Load the default voice and the helper's model cache in the background."""
    try:
        model = _ensure_voice(DEFAULT_VOICE)
    except (OSError, subprocess.SubprocessError):
        return
    if not model:
        return
    out_dir = os.environ.get("NIX_TTS_DIR", _TTS_DIR)
    try:
        os.makedirs(out_dir, exist_ok=True)
        _piper_daemon().synthesize(
            text="Nix is ready.", model=model, out=os.path.join(out_dir, "nix-tts-warm.wav"),
            length_scale=1.0 / DEFAULT_SPEED, noise_scale=DEFAULT_EXPRESSIVENESS,
            noise_w_scale=DEFAULT_VARIABILITY, warm=True,
        )
    except Exception:
        # This runs on the connect-time warm-up thread, off the critical path:
        # any failure here just means the first `say` pays the model load, so it
        # must never escape as an unhandled thread exception.
        return


class RingService:
    def __init__(self) -> None:
        self.client: Any = None
        self.ring_key: int | None = None
        self.segment_keys: dict[int, int] = {}
        self.effects: list[str] = []
        self.device_name = "Echo Dot"
        self.connected = False
        self.state: dict[str, Any] = {"on": False, "brightness": 0.0, "rgb": [0, 0, 0], "effect": "None"}
        self.speaker: dict[str, Any] = {"volume": 0.0, "muted": False, "state": "unknown"}
        self.media_key: int | None = None
        self.mic_mixing_key: int | None = None
        self.mic_mute_key: int | None = None
        self.mic_mixing_options: list[str] = []
        self._condition = threading.Condition()
        self._revision = 0
        self._speaker_revision = 0
        self._command_lock = asyncio.Lock()
        self._config: dict[str, str] = {}
        self._speech_dir: str | None = None
        self._speech_server: _SpeechFileServer | None = None
        self.media_formats: list[dict[str, Any]] = []
        self.media_supports_pause = False
        self.playing_url: str | None = None
        self.playing_title: str | None = ""
        self._media_dir: str | None = None
        self._media_server: _SpeechFileServer | None = None
        self.tts_provider = "auto"
        self.cloud_keys: list[str] = []
        self.cloud_voice = DEFAULT_CLOUD_VOICE
        self.cloud_character_limit = DEFAULT_CLOUD_CHARACTER_LIMIT
        self.cloud_seconds_limit = DEFAULT_CLOUD_SECONDS_LIMIT
        self._cloud_usage: dict[str, Any] = {}

    def _speech_url(self, name: str) -> str:
        """Lazily start the speech file server and return a reachable URL."""
        if self._speech_dir is None:
            self._speech_dir = tempfile.mkdtemp(prefix="nix-echo-say-")
        if self._speech_server is None:
            self._speech_server = _SpeechFileServer(self._speech_dir)
        host = _local_ip_toward(self._config["device_ip"])
        return f"http://{host}:{self._speech_server.port}/{name}.wav"

    def _media_url(self, name: str) -> str:
        """Lazily start the media file server and return a reachable URL."""
        if self._media_dir is None:
            self._media_dir = tempfile.mkdtemp(prefix="nix-echo-media-")
        if self._media_server is None:
            self._media_server = _SpeechFileServer(self._media_dir)
        host = _local_ip_toward(self._config["device_ip"])
        return f"http://{host}:{self._media_server.port}/{name}"

    def _prepare_playback(self, url: str) -> tuple[str, float]:
        """Return a URL the Dot can actually decode, and the seconds of audio
        it carries (0.0 when nothing had to be converted).

        The upstream URL is used as-is when its container is one the device
        advertises for playback, otherwise it is fetched and converted to the
        device's own format and served from the media server. A stream whose
        origin serves only its opening becomes a shorter track rather than an
        error, and the seconds returned say how much was prepared. Raises with a
        clear message when ffmpeg is missing or the origin served no audio at
        all."""
        content_type = _probe_content_type(url)
        container = _MIME_CONTAINER.get((content_type or "").split(";")[0].strip().lower())
        playable = [entry for entry in self.media_formats if entry.get("purpose") in ("default", "media")]
        if container and any(entry.get("format") == container for entry in playable):
            return url, 0.0
        ffmpeg = _find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError(
                "This track needs conversion and ffmpeg is not available. "
                "Install ffmpeg on the NIX host (or set NIX_FFMPEG), then retry."
            )
        target = _conversion_target(playable)
        name = f"{hashlib.sha256(url.encode()).hexdigest()[:32]}.{target['format']}"
        if self._media_dir is None:
            self._media_dir = tempfile.mkdtemp(prefix="nix-echo-media-")
        dest = os.path.join(self._media_dir, name)
        if not os.path.isfile(dest):
            source = dest + ".src"
            try:
                _fetch_remote(url, source)
                _convert_audio(source, ffmpeg, dest, target)
            finally:
                try:
                    os.remove(source)
                except OSError:
                    pass
        # The converted file's own duration is the honest answer for a stream
        # whose origin served only its opening (and 0.0 for a non-PCM target).
        return self._media_url(name), _wav_seconds(dest)

    async def initialize(self, config: dict[str, Any]) -> dict[str, Any]:
        address = config.get("device_ip")
        psk = config.get("api_key")
        try:
            parsed_ip = ipaddress.ip_address(str(address))
        except ValueError as exc:
            raise ValueError("Configure the Echo Dot's private LAN IPv4 address in Skills setup first.") from exc
        if not isinstance(parsed_ip, ipaddress.IPv4Address) or not parsed_ip.is_private or parsed_ip.is_loopback or parsed_ip.is_link_local or parsed_ip.is_multicast:
            raise ValueError("Echo Dot address must be a private LAN IPv4 address.")
        if not isinstance(psk, str):
            raise ValueError("Configure the rotated ESPHome API key in Skills setup first.")
        try:
            if len(base64.b64decode(psk, validate=True)) != 32:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ValueError("ESPHome API key must be base64 encoding of exactly 32 bytes.") from exc
        self._config = {"device_ip": str(parsed_ip), "api_key": psk}
        self._configure_speech(config)
        try:
            import aioesphomeapi
        except ImportError as exc:
            raise RuntimeError("Echo Connect's ESPHome client is missing. Install aioesphomeapi in NIX's Python environment, then trust/enable the skill again.") from exc
        self.client = aioesphomeapi.APIClient(
            str(parsed_ip), 6053, noise_psk=psk
        )
        try:
            await asyncio.wait_for(self.client.connect(login=True), timeout=15)
            device, entities, _services = await asyncio.wait_for(self.client.device_info_and_list_entities(), timeout=15)
            reported_name = getattr(device, "friendly_name", None) or getattr(device, "name", None)
            if isinstance(reported_name, str) and reported_name.strip():
                self.device_name = " ".join(reported_name.split())[:80]
            media_player_info = getattr(aioesphomeapi, "MediaPlayerInfo", None)
            select_info = getattr(aioesphomeapi, "SelectInfo", None)
            switch_info = getattr(aioesphomeapi, "SwitchInfo", None)
            for entity in entities:
                if media_player_info is not None and isinstance(entity, media_player_info):
                    object_id = (getattr(entity, "object_id", "") or "").casefold()
                    if object_id == "speaker":
                        self.media_key = entity.key
                        self.media_formats = [
                            {
                                "format": str(getattr(entry, "format", "") or ""),
                                "sample_rate": int(getattr(entry, "sample_rate", 0) or 0),
                                "num_channels": int(getattr(entry, "num_channels", 0) or 0),
                                "purpose": str(getattr(getattr(entry, "purpose", ""), "name", getattr(entry, "purpose", "")) or "").casefold(),
                                "sample_bytes": int(getattr(entry, "sample_bytes", 0) or 0),
                            }
                            for entry in (getattr(entity, "supported_formats", None) or [])
                        ]
                        self.media_supports_pause = bool(getattr(entity, "supports_pause", False))
                        continue
                if select_info is not None and isinstance(entity, select_info):
                    object_id = (getattr(entity, "object_id", "") or "").casefold()
                    if object_id == "microphone_mixing":
                        self.mic_mixing_key = entity.key
                        self.mic_mixing_options = [str(option) for option in (entity.options or [])][:16]
                        continue
                if switch_info is not None and isinstance(entity, switch_info):
                    object_id = (getattr(entity, "object_id", "") or "").casefold()
                    if object_id == "mic_mute":
                        self.mic_mute_key = entity.key
                        continue
                if not isinstance(entity, aioesphomeapi.LightInfo):
                    continue
                object_id = (getattr(entity, "object_id", "") or "").casefold()
                name = (getattr(entity, "name", "") or "").casefold()
                if object_id == "ring" or name == "led ring":
                    self.ring_key = entity.key
                    self.effects = list(entity.effects or [])
                    continue
                segment = re.fullmatch(r"segment_(\d+)", object_id)
                if segment:
                    number = int(segment.group(1))
                    if 1 <= number <= 12:
                        self.segment_keys[number] = entity.key
            if self.ring_key is None:
                raise RuntimeError("Echo Dot ESPHome API connected, but no LED ring light entity was found.")
            self.connected = True
            self.client.subscribe_states(self._on_state)
            await asyncio.wait_for(self.client.device_info(), timeout=10)
            await asyncio.wait_for(
                asyncio.to_thread(self._wait_for_state, 0, lambda _state: True, 5.0),
                timeout=6,
            )
            # Warm the neural voice off the critical path so the first `say`
            # after a connect does not pay the model load. A cloud-only setup
            # skips this: downloading a local voice there would be dead weight.
            if not (self.tts_provider == "unrealspeech" and self.cloud_keys):
                threading.Thread(target=_warm_speech, daemon=True, name="nix-tts-warm").start()
            return {"ready": True}
        except Exception:
            self.connected = False
            try:
                await self.client.disconnect()
            except Exception:
                pass
            raise

    def _configure_speech(self, config: dict[str, Any]) -> None:
        """Read the optional speech settings: the provider choice, up to three
        Unreal Speech keys, the cloud voice, and this month's per-key limits.
        Keys are validated here only for shape; the provider is the judge."""
        provider = config.get("tts_provider")
        provider = "auto" if provider in (None, "") else str(provider).strip().casefold()
        if provider not in {"auto", "unrealspeech", "local"}:
            raise ValueError("Speech provider must be auto, unrealspeech, or local.")
        keys: list[str] = []
        for field_id in CLOUD_KEY_FIELDS:
            value = config.get(field_id)
            if value in (None, ""):
                continue
            if not isinstance(value, str):
                raise ValueError("Unreal Speech API keys must be text.")
            key = value.strip()
            if not key:
                continue  # a blank setup field means "no key in this slot"
            if not 8 <= len(key) <= 200 or any(ord(character) < 33 for character in key):
                raise ValueError(
                    "Unreal Speech API keys must be 8 to 200 characters without spaces or control characters."
                )
            if key not in keys:
                keys.append(key)
        voice = config.get("unrealspeech_voice")
        if voice in (None, ""):
            voice = DEFAULT_CLOUD_VOICE
        elif isinstance(voice, str) and voice.strip().casefold() in _CLOUD_VOICE_CATALOG:
            voice = voice.strip().casefold()
        else:
            raise ValueError(f"Unreal Speech voice must be one of: {', '.join(sorted(_CLOUD_VOICE_CATALOG))}")
        self.tts_provider = provider
        self.cloud_keys = keys
        self.cloud_voice = voice
        self.cloud_character_limit = _clamp_cloud_limit(
            config.get("unrealspeech_monthly_characters"),
            DEFAULT_CLOUD_CHARACTER_LIMIT,
            "Unreal Speech monthly character limit",
        )
        self.cloud_seconds_limit = _clamp_cloud_hours(
            config.get("unrealspeech_monthly_audio_hours"),
            DEFAULT_CLOUD_SECONDS_LIMIT / 3600,
            "Unreal Speech monthly audio limit",
        )
        self._cloud_usage = _read_cloud_usage()

    def _cloud_key_entry(self, api_key: str) -> dict[str, Any]:
        """This month's usage for one key, reset when the month rolls over."""
        identifier = _cloud_key_id(api_key)
        month = _cloud_month()
        entry = self._cloud_usage.get(identifier)
        if isinstance(entry, dict) and entry.get("month") == month:
            entry = {
                "month": month,
                "characters": max(0, int(entry.get("characters") or 0)),
                "seconds": max(0.0, float(entry.get("seconds") or 0.0)),
                "exhausted": bool(entry.get("exhausted")),
            }
        else:
            entry = {"month": month, "characters": 0, "seconds": 0.0, "exhausted": False}
        self._cloud_usage[identifier] = entry
        return entry

    def _save_cloud_usage(self) -> None:
        _write_cloud_usage(dict(self._cloud_usage))

    def _cloud_key_blocked(self, entry: dict[str, Any], text: str, index: int) -> str:
        """Why this key should be skipped, or an empty string to use it."""
        if entry["exhausted"]:
            return f"key {index} is out of monthly allowance"
        character_percent = entry["characters"] * 100 / self.cloud_character_limit
        if character_percent >= CLOUD_SWITCH_THRESHOLD * 100:
            return f"key {index} is at {round(character_percent)}% of its character allowance"
        second_percent = entry["seconds"] * 100 / self.cloud_seconds_limit
        if second_percent >= CLOUD_SWITCH_THRESHOLD * 100:
            return f"key {index} is at {round(second_percent)}% of its audio allowance"
        if entry["characters"] + len(text) > self.cloud_character_limit:
            return f"key {index} has only {self.cloud_character_limit - entry['characters']} characters left this month"
        return ""

    def _render_announcement(
        self, text: str, voice: str, speed: float, expressiveness: float, variability: float
    ) -> tuple[str, str, int, str]:
        """Render one announcement: the cloud voice while a configured key still
        has monthly allowance, otherwise the built-in local voice. Returns
        (wav path, engine, key slot 1..3 or 0, why the cloud was skipped)."""
        if self._speech_dir is None:
            self._speech_dir = tempfile.mkdtemp(prefix="nix-echo-say-")
        out_dir = self._speech_dir
        name = f"say-{uuid.uuid4().hex[:12]}"
        reason = ""
        if self.tts_provider != "local":
            if not self.cloud_keys:
                reason = "no Unreal Speech API key is configured" if self.tts_provider == "unrealspeech" else ""
            else:
                for index, api_key in enumerate(self.cloud_keys, start=1):
                    entry = self._cloud_key_entry(api_key)
                    blocked = self._cloud_key_blocked(entry, text, index)
                    if blocked:
                        reason = blocked
                        continue
                    try:
                        path, seconds, billed = _unrealspeech_speech_wav(
                            text, out_dir, name, api_key=api_key,
                            voice=self.cloud_voice, speed=speed, playable=self.media_formats,
                        )
                    except _CloudKeyExhausted as error:
                        entry["exhausted"] = True
                        self._save_cloud_usage()
                        reason = f"key {index} is spent: {error}"
                        continue
                    except Exception as error:
                        # Deliberate degradation: any provider failure (network,
                        # auth, conversion) moves on to the next key and finally
                        # to the local voice, and the reason is reported back.
                        reason = f"key {index} failed: {error}"
                        continue
                    if billed:
                        # A cached phrase is replayed locally and costs the
                        # provider nothing, so it must not consume allowance.
                        entry["characters"] += len(text)
                        entry["seconds"] = round(entry["seconds"] + seconds, 3)
                        self._save_cloud_usage()
                    return path, "unrealspeech", index, ""
        path = _synthesize_speech_wav(
            text, out_dir, name, voice=voice, speed=speed,
            expressiveness=expressiveness, variability=variability,
        )
        return path, "local", 0, reason[:160]

    def _tts_report(self) -> dict[str, Any]:
        """Quota snapshot on every result: the limits, how much of each
        configured key is used this month, and which keys can still be used."""
        month = _cloud_month()
        keys = []
        for index, api_key in enumerate(self.cloud_keys, start=1):
            entry = self._cloud_usage.get(_cloud_key_id(api_key))
            if not isinstance(entry, dict) or entry.get("month") != month:
                entry = {}
            characters = max(0, int(entry.get("characters") or 0))
            seconds = max(0.0, float(entry.get("seconds") or 0.0))
            characters_percent = min(100, round(characters * 100 / self.cloud_character_limit))
            seconds_percent = min(100, round(seconds * 100 / self.cloud_seconds_limit))
            keys.append({
                "index": index,
                "characters": characters,
                "seconds": round(seconds, 3),
                "characters_percent": characters_percent,
                "seconds_percent": seconds_percent,
                "ready": (
                    not entry.get("exhausted")
                    and characters_percent < CLOUD_SWITCH_THRESHOLD * 100
                    and seconds_percent < CLOUD_SWITCH_THRESHOLD * 100
                ),
            })
        return {
            "provider": self.tts_provider,
            "engine": "local",
            "voice": self.cloud_voice,
            "key_index": 0,
            "fallback": "",
            "month": month,
            "characters_limit": self.cloud_character_limit,
            "seconds_limit": self.cloud_seconds_limit,
            "keys": keys,
        }

    def _on_state(self, state: Any) -> None:
        state_key = getattr(state, "key", None)
        if self.media_key is not None and state_key == self.media_key:
            volume = getattr(state, "volume", None)
            # MediaPlayerState is an IntEnum, and on Python 3.11+ str(IntEnum)
            # returns its number, so use the member NAME (an int here would
            # never match the "playing"/"paused"/"idle" comparisons below).
            raw_state = getattr(state, "state", "")
            state_name = getattr(raw_state, "name", None) or str(raw_state or "unknown")
            with self._condition:
                updated = {
                    "volume": self.speaker.get("volume"),
                    "muted": bool(getattr(state, "muted", False)),
                    "state": str(state_name).replace("MediaPlayerState.", "").casefold() or "unknown",
                }
                if isinstance(volume, (int, float)) and not isinstance(volume, bool) and math.isfinite(float(volume)):
                    updated["volume"] = max(0.0, min(1.0, float(volume)))
                self.speaker = updated
                if updated["state"] in ("idle", "off"):
                    # URL playback ended (or was stopped): forget it so the
                    # result never reports music that is no longer playing.
                    self.playing_url = None
                    self.playing_title = ""
                self._revision += 1
                self._speaker_revision += 1
                self._condition.notify_all()
            return
        if state_key in self.segment_keys.values():
            with self._condition:
                self._revision += 1
                self._condition.notify_all()
            return
        if self.ring_key is None or state_key != self.ring_key:
            return
        with self._condition:
            self.state = {
                "on": bool(state.state),
                "brightness": float(state.brightness),
                "rgb": [_rgb_byte(state.red), _rgb_byte(state.green), _rgb_byte(state.blue)],
                "effect": str(state.effect or "None"),
            }
            self._revision += 1
            self._condition.notify_all()

    def _wait_for_state(self, before: int, predicate, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._revision > before and predicate(self.state):
                    return dict(self.state)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Echo Dot did not confirm the requested ring state.")
                self._condition.wait(remaining)

    def _poll_speaker(self, predicate, timeout: float = 2.0) -> dict[str, Any]:
        """Poll the reported speaker state until the predicate holds.

        Unlike :meth:`_wait_for_state` this does not require a *pushed*
        revision: some firmware applies a pause/resume command and reports the
        new state without emitting an entity event, and gating on a revision
        then wastes the whole timeout before returning the state it already
        had. Polling the reported state confirms in a few tens of milliseconds
        and never blocks a transport control for seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            if predicate(self.speaker):
                return dict(self.speaker)
            if time.monotonic() >= deadline:
                raise TimeoutError("Echo Dot did not confirm the requested speaker state.")
            time.sleep(0.05)

    def _send_and_confirm(self, changes: dict[str, Any], action: str) -> dict[str, Any]:
        if not self.connected or self.client is None or self.ring_key is None:
            raise RuntimeError("Echo Dot is not connected. Check its power and Wi-Fi, then retry once.")
        with self._condition:
            before = self._revision
        self.client.light_command(self.ring_key, **changes)
        if action == "on":
            predicate = lambda state: state["on"]
        elif action == "off":
            predicate = lambda state: not state["on"]
        elif action == "color":
            expected = tuple(_rgb_byte(channel) for channel in changes["rgb"])
            predicate = lambda state: state["on"] and state["effect"] in {"", "None"} and all(abs(actual - wanted) <= 1 for actual, wanted in zip(state["rgb"], expected))
        elif action == "effect":
            expected = changes.get("effect", "None")
            predicate = lambda state: state["on"] and state["effect"] == expected
        elif action == "brightness":
            expected = changes["brightness"]
            predicate = lambda state: abs(state["brightness"] - expected) <= 0.03
        else:
            predicate = lambda _state: True
        state = self._wait_for_state(before, predicate)
        return state

    def _result(self, action: str, message: str, **fields: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "action": action[:32],
            "state": dict(self.state),
            "speaker": {
                "volume": self.speaker.get("volume"),
                "muted": bool(self.speaker.get("muted")),
                "state": (str(self.speaker.get("state") or "unknown"))[:30],
            },
            "device_connected": self.connected,
            "device_name": self.device_name,
            "available_effects": self.effects[:64],
            "message": message[:240],
            "tts": self._tts_report(),
            **fields,
        }

    def _send_speaker_command(self, changes: dict[str, Any], expected_volume: float | None, expected_muted: bool | None) -> dict[str, Any]:
        if not self.connected or self.client is None or self.media_key is None:
            raise RuntimeError("Echo Dot is not connected. Check its power and Wi-Fi, then retry once.")
        with self._condition:
            before = self._revision
            before_speaker = self._speaker_revision
        self.client.media_player_command(self.media_key, **changes)
        def predicate(_state: dict[str, Any]) -> bool:
            if expected_volume is not None:
                current = self.speaker.get("volume")
                if current is None or abs(float(current) - expected_volume) > (1.0 / (VOLUME_STEPS * 2)):
                    return False
            if expected_muted is not None and bool(self.speaker.get("muted")) is not expected_muted:
                return False
            return True
        try:
            self._wait_for_state(before, predicate)
        except TimeoutError:
            # The device rounds volume to its own 1/30 steps, and a rapid second
            # change (a held ▲/▼ or a slider settle) can supersede this one. If
            # the speaker reported anything after our command, that report is
            # the truth: return it instead of failing the control. Only a silent
            # device is an error.
            with self._condition:
                if self._speaker_revision <= before_speaker:
                    raise
            return dict(self.speaker)
        return dict(self.speaker)

    def _arguments(self, text: str) -> dict[str, Any] | None:
        clean = " ".join((text or "").strip().split())
        low = clean.casefold()
        if len(clean) > 4000 or not re.search(r"\b(?:ring|echo\s*dot|dot|light|led|echo\s*connect)\b", low):
            return None
        if re.search(r"\b(?:what|which|show|list|tell me|available|can|could)\b.*\b(?:colors?|colours?)\b", low) and not re.search(r"\b(?:turn|switch|set|make|run|start|play|stop|disable|enable)\b", low):
            return {"action": "color_catalog"}
        if re.search(r"\b(?:what|which|show|list|tell me|available|can|could)\b.*\b(?:effects?|animations?)\b", low) and not re.search(r"\b(?:turn|switch|set|make|run|start|play|stop|disable|enable)\b", low):
            return {"action": "catalog"}
        if re.search(r"\b(?:state|status|what(?:'s| is) the ring doing|how does the ring look)\b", low):
            return {"action": "state"}
        if re.search(r"\b(?:stop|turn off|switch off|power off)\b.*\b(?:effect|animation)\b", low):
            return {"action": "effect", "effect": "None"}
        brightness_match = re.search(r"(?:\b(?:brightness|bright|dim|dimmer)\b[^\d]{0,24}(\d{1,3})\s*(?:%|percent)?|(\d{1,3})\s*(?:%|percent)\s*(?:brightness|bright|dim)?)", low)
        if brightness_match:
            brightness_text = brightness_match.group(1) or brightness_match.group(2)
            value = float(brightness_text) / (100.0 if int(brightness_text) > 1 or "%" in low or "percent" in low else 1.0)
            if 0.05 <= value <= 1:
                return {"action": "brightness", "brightness": value}
        rgb_match = re.search(r"\b(?:rgb|color|colour)\s*\(?\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*\)?", low)
        rgb = None
        if rgb_match:
            channels = tuple(int(item) for item in rgb_match.groups())
            if all(channel <= 255 for channel in channels):
                rgb = list(channels)
        if rgb is None:
            hex_match = re.search(r"#([0-9a-f]{6})\b", low)
            if hex_match:
                hex_value = hex_match.group(1)
                rgb = [int(hex_value[index:index + 2], 16) for index in (0, 2, 4)]
        if rgb is not None and re.search(r"\b(?:color|colour|rgb|make|turn|set|glow|shine|light|ring|dot)\b", low):
            return {"action": "color", "rgb": rgb}
        has_effect_intent = bool(re.search(r"\b(?:animation|animate|effect)\b", low))
        if re.search(r"\b(?:turn|switch|power)\s+(?:the\s+)?(?:(?:echo\s+)?dot\s+)?(?:ring|light|led|dot)\s+(?:off|down)\b|\b(?:turn|switch|power)\s+(?:off|down)\s+(?:the\s+)?(?:(?:echo\s+)?dot\s+)?(?:ring|light|led|dot)\b|\b(?:turn|switch|power)\s+(?:the\s+)?echo\s+connect\s+(?:off|down)\b|\b(?:turn|switch|power)\s+(?:off|down)\s+(?:the\s+)?echo\s+connect\b|\b(?:off|disable)\b.*\b(?:ring|light|dot)\b", low) and not re.search(r"\bon\s+light\b", low) and not has_effect_intent:
            return {"action": "off"}
        if re.search(r"\b(?:turn|switch|power)\s+(?:the\s+)?(?:(?:echo\s+)?dot\s+)?(?:ring|light|led|dot)\s+(?:on|up)\b|\b(?:turn|switch|power)\s+(?:on|up)\s+(?:the\s+)?(?:(?:echo\s+)?dot\s+)?(?:ring|light|led|dot)\b|\b(?:turn|switch|power)\s+(?:the\s+)?echo\s+connect\s+(?:on|up)\b|\b(?:turn|switch|power)\s+(?:on|up)\s+(?:the\s+)?echo\s+connect\b|\b(?:on|enable)\s+(?:the\s+)?echo\s+connect\b", low) and not re.search(r"\bon\s+light\b", low) and not has_effect_intent and not re.search(r"\b(?:run|start|play|animate)\b", low):
            return {"action": "on"}
        effect_request = low
        for marker in (" on the echo dot ring", " on echo dot ring", " on the dot ring", " on dot ring", " on the ring", " on ring"):
            if effect_request.endswith(marker):
                effect_request = effect_request[:-len(marker)].strip()
                break
        for prefix in ("run ", "start ", "play ", "animate ", "animation ", "effect ", "set ", "do ", "make ", "run the ", "play the ", "start the "):
            if effect_request.startswith(prefix):
                effect_request = effect_request[len(prefix):].strip()
                break
        effect_request = effect_request.strip(" \"'`.,!?;:")
        effect = next((item for item in sorted(self.effects, key=len, reverse=True) if effect_request == item.casefold()), None)
        if effect and effect.casefold() != "none":
            return {"action": "effect", "effect": effect}
        return None

    async def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        if action == "catalog":
            return self._result(action, "The connected Echo Dot's firmware effect catalog is available.")
        if action == "color_catalog":
            return self._result(action, "The Dot does not publish a finite named-color list; Luna chooses RGB channels for each color description, and any integer RGB triplet from 0 through 255 is accepted.")
        if action == "state":
            if not self.connected:
                raise RuntimeError("Echo Dot is offline. Check its power and Wi-Fi.")
            return self._result(action, "Current ring state returned by the Echo Dot.")
        async with self._command_lock:
            if action == "on":
                state = await asyncio.to_thread(self._send_and_confirm, {"state": True}, "on")
                return self._result(action, "The Echo Dot confirmed the ring is on.", state=state)
            if action == "off":
                state = await asyncio.to_thread(self._send_and_confirm, {"state": False}, "off")
                return self._result(action, "The Echo Dot confirmed the ring is off.", state=state)
            if action == "color":
                rgb = args.get("rgb")
                if not isinstance(rgb, list) or len(rgb) != 3 or any(isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 255 for channel in rgb):
                    raise ValueError("The selected RGB channels must be integers from 0 through 255.")
                state = await asyncio.to_thread(self._send_and_confirm, {"state": True, "rgb": _rgb_float(rgb), "effect": "None"}, "color")
                return self._result(action, f"The Dot reports RGB ({rgb[0]}, {rgb[1]}, {rgb[2]}) with effect {state['effect']}.", state=state, rgb=rgb)
            if action == "brightness":
                level = _clamp_brightness(args.get("brightness"))
                state = await asyncio.to_thread(self._send_and_confirm, {"state": True, "brightness": level}, "brightness")
                return self._result(action, f"The Echo Dot confirmed brightness at {round(level * 100)} percent.", state=state, brightness=level)
            if action == "effect":
                effect = args.get("effect")
                if not isinstance(effect, str) or effect not in self.effects:
                    raise ValueError("That effect is not in the Dot's live effect catalog.")
                state = await asyncio.to_thread(self._send_and_confirm, {"state": True, "effect": effect}, "effect")
                message = "The Echo Dot confirmed the animation is stopped." if effect == "None" else f"The Echo Dot confirmed the {effect} animation is running."
                return self._result(action, message, state=state, effect=effect)
            if action == "volume":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                level = _clamp_volume(args.get("volume"))
                speaker = await asyncio.to_thread(self._send_speaker_command, {"volume": level}, level, None)
                return self._result(action, f"The Echo Dot confirmed speaker volume at {round(level * 100)} percent.", speaker=speaker, volume=level)
            if action == "mute":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                speaker = await asyncio.to_thread(self._send_speaker_command, {"command": _media_command("MUTE")}, None, True)
                return self._result(action, "The Echo Dot confirmed the speaker is muted.", speaker=speaker)
            if action == "unmute":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                speaker = await asyncio.to_thread(self._send_speaker_command, {"command": _media_command("UNMUTE")}, None, False)
                return self._result(action, "The Echo Dot confirmed the speaker is unmuted.", speaker=speaker)
            if action == "say":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                text = _clamp_say_text(args.get("text"))
                voice = args.get("voice") or DEFAULT_VOICE
                speed = args.get("speed", DEFAULT_SPEED)
                wav_path, engine, key_index, fallback = await asyncio.to_thread(
                    self._render_announcement,
                    text,
                    voice,
                    speed,
                    args.get("expressiveness", DEFAULT_EXPRESSIVENESS),
                    args.get("variability", DEFAULT_VARIABILITY),
                )
                # The synthesizer may answer with a cached WAV for repeated text,
                # so serve whatever it actually produced rather than the request name.
                media_name = os.path.basename(wav_path)
                media_url = self._speech_url(media_name[:-4] if media_name.endswith(".wav") else media_name)
                with self._condition:
                    before = self._revision
                await asyncio.to_thread(
                    self.client.media_player_command,
                    self.media_key,
                    media_url=media_url,
                    announcement=True,
                )
                # Soft confirm: announcement playback may or may not surface
                # as a state change on every firmware, so report honestly.
                try:
                    self._wait_for_state(
                        before,
                        lambda s: True,
                        0.0,
                    )
                except Exception:
                    pass
                speaker = dict(self.speaker)
                spoken = self.cloud_voice if engine == "unrealspeech" else voice
                note = f" [local fallback: {fallback[:80]}]" if engine == "local" and fallback else ""
                return self._result(
                    action,
                    f"Playing announcement in the {spoken} voice on the {self.device_name}: "
                    f"{text[:60]}{'…' if len(text) > 60 else ''}{note}",
                    speaker=speaker,
                    voice=voice,
                    voice_speed=float(speed),
                    tts={**self._tts_report(), "engine": engine, "key_index": key_index, "fallback": fallback[:160]},
                )
            if action == "stop_speaking":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                await asyncio.to_thread(self.client.media_player_command, self.media_key, command=_media_command("STOP"))
                self.playing_url = None
                self.playing_title = ""
                return self._result(action, "The Echo Dot confirmed the stop request for its speaker.")
            if action == "play_url":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                url = _clamp_media_url(args.get("url"))
                title = _clamp_media_title(args.get("title"))
                playable, seconds = await asyncio.to_thread(self._prepare_playback, url)
                await asyncio.to_thread(
                    self.client.media_player_command,
                    self.media_key,
                    command=_media_command("PLAY"),
                    media_url=playable,
                )
                # URL playback does not push a transition on every firmware, so
                # state_confirmed=false means "not observed", never "failed".
                # The wait runs in a thread: the ESPHome state callback is
                # delivered on this worker's event loop, so blocking here would
                # stop the very update the wait is looking for and burn the
                # whole timeout. The wait is short because the caller polls the
                # speaker anyway.
                confirmed = False
                try:
                    await asyncio.to_thread(
                        self._poll_speaker,
                        lambda speaker: speaker.get("state") in ("playing", "announcing"),
                        1.5,
                    )
                    confirmed = True
                except TimeoutError:
                    pass
                self.playing_url, self.playing_title = playable, title
                label = f"“{title}” " if title else ""
                note = f" ({int(seconds)}s converted)" if seconds else ""
                return self._result(
                    action,
                    f"The Echo Dot is playing {label}on its speaker{note}.",
                    media={
                        "url": playable,
                        "title": title,
                        "state": str(self.speaker.get("state") or "unknown"),
                        "state_confirmed": confirmed,
                    },
                )
            if action == "pause":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                if not self.media_supports_pause:
                    raise ValueError("The connected Echo Dot does not report pause support.")
                await asyncio.to_thread(self.client.media_player_command, self.media_key, command=_media_command("PAUSE"))
                try:
                    await asyncio.to_thread(
                        self._poll_speaker, lambda speaker: speaker.get("state") == "paused", 1.5
                    )
                except TimeoutError:
                    pass
                return self._result(action, "The Echo Dot confirmed the speaker is paused.")
            if action == "resume":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                # PLAY with no URL resumes the current item (pause <-> play).
                await asyncio.to_thread(self.client.media_player_command, self.media_key, command=_media_command("PLAY"))
                try:
                    await asyncio.to_thread(
                        self._poll_speaker, lambda speaker: speaker.get("state") in ("playing", "announcing"), 1.5
                    )
                except TimeoutError:
                    pass
                return self._result(action, "The Echo Dot confirmed the speaker is playing again.")
        raise ValueError("Unsupported Echo Connect action.")

    @staticmethod
    def _validate_tool_arguments(arguments: Any) -> dict[str, Any]:
        """Enforce each action's exact parameter format at the skill boundary."""
        if not isinstance(arguments, dict) or not isinstance(arguments.get("action"), str):
            raise ValueError("control_ring arguments must be an object with a string action")
        action = arguments["action"]
        allowed = {
            "catalog": {"action"},
            "color_catalog": {"action"},
            "state": {"action"},
            "on": {"action"},
            "off": {"action"},
            "color": {"action", "rgb"},
            "brightness": {"action", "brightness"},
            "effect": {"action", "effect"},
            "volume": {"action", "volume"},
            "mute": {"action"},
            "unmute": {"action"},
            "say": {"action", "text", "voice", "speed", "expressiveness", "variability"},
            "stop_speaking": {"action"},
            "play_url": {"action", "url", "title"},
            "pause": {"action"},
            "resume": {"action"},
        }
        if action not in allowed:
            raise ValueError("Unsupported Echo Connect action")
        extra = set(arguments) - allowed[action]
        if extra:
            raise ValueError(f"Unsupported {action} arguments: {', '.join(sorted(extra))}")
        checked = dict(arguments)
        if action == "color":
            rgb = checked.get("rgb")
            if not isinstance(rgb, list) or len(rgb) != 3 or any(
                isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 255
                for channel in rgb
            ):
                raise ValueError("rgb must contain exactly three integer channels from 0 through 255")
        elif action == "brightness":
            _clamp_brightness(checked.get("brightness"))
        elif action == "effect":
            effect = checked.get("effect")
            if not isinstance(effect, str) or not 1 <= len(effect) <= 64:
                raise ValueError("effect must be a non-empty name of at most 64 characters")
        elif action == "volume":
            _clamp_volume(checked.get("volume"))
        elif action == "play_url":
            checked["url"] = _clamp_media_url(checked.get("url"))
            if "title" in checked:
                checked["title"] = _clamp_media_title(checked["title"])
        elif action == "say":
            text = " ".join(str(checked.get("text") or "").split())
            if not 1 <= len(text) <= 500:
                raise ValueError("say text must be 1 to 500 characters")
            checked["text"] = text
            if "voice" in checked:
                voice = checked["voice"]
                if not isinstance(voice, str) or voice not in _VOICE_CATALOG:
                    raise ValueError(f"voice must be one of: {', '.join(sorted(_VOICE_CATALOG))}")
            if "speed" in checked:
                checked["speed"] = _clamp_range(checked["speed"], SPEED_MIN, SPEED_MAX, "speed")
            if "expressiveness" in checked:
                checked["expressiveness"] = _clamp_range(checked["expressiveness"], 0.0, 1.0, "expressiveness")
            if "variability" in checked:
                checked["variability"] = _clamp_range(checked["variability"], 0.0, 1.0, "variability")
        return checked

    async def handle(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "match":
            return {"match": self._arguments(str(payload.get("text") or ""))}
        if operation == "execute":
            if payload.get("tool") != "control_ring" or not isinstance(payload.get("arguments"), dict):
                raise ValueError("Only the declared control_ring tool is available.")
            arguments = self._validate_tool_arguments(payload["arguments"])
            return {"result": await self._execute(arguments)}
        raise ValueError("Unsupported operation.")

    async def close(self) -> None:
        self.connected = False
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None


async def _run_worker() -> int:
    service: RingService | None = None
    loop = asyncio.get_running_loop()
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            break
        request: dict[str, Any] = {}
        try:
            if len(line.encode("utf-8")) > 256 * 1024:
                raise ValueError("Request exceeds size limit.")
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("id"), str):
                raise ValueError("Request requires a string correlation id.")
            operation = request.get("op")
            if operation == "initialize":
                if service is not None:
                    await service.close()
                service = RingService()
                await service.initialize(request.get("config") or {})
                response = {"id": request["id"], "ok": True, "ready": True}
            elif service is None:
                raise ValueError("Worker is not initialized.")
            else:
                response = {"id": request["id"], "ok": True, **await service.handle(str(operation), request)}
        except Exception as exc:
            response = {"id": request.get("id", "invalid"), "ok": False, "error": f"{type(exc).__name__}: {str(exc)}"[:240]}
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    if service is not None:
        await service.close()
    return 0


def main() -> int:
    if "--nix-skill-worker" not in sys.argv[1:]:
        print("This module is a NIX skill worker; use the NIX Skills dashboard to configure, trust, and run it.", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run_worker())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
