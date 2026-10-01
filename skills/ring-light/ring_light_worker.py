#!/usr/bin/env python3
"""NIX Ring Light worker: ESPHome native API client and bounded tool adapter.

This implementation is based only on the published ESPHome native API
client/protocol. It does not copy the unlicensed upstream bridge or client.
The worker accepts the NIX JSONL contract on stdin/stdout; diagnostics go to
stderr. Only calls returned by its fixed control_ring tool can reach the Dot.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import math
import re
import sys
import threading
import time
import uuid
from typing import Any

PROTOCOL = "nix-skill-jsonl-v1"
PALETTES: dict[str, tuple[tuple[int, int, int], ...]] = {
    "rainbow": ((153, 0, 0), (153, 153, 0), (0, 153, 0), (0, 153, 153), (0, 0, 153), (153, 0, 153)),
    "aurora": ((0, 200, 80), (0, 176, 176), (48, 64, 192), (112, 32, 160), (16, 144, 128)),
    "fire": ((255, 224, 160), (255, 144, 32), (208, 48, 0), (64, 4, 0)),
    "ocean": ((0, 12, 64), (0, 80, 160), (0, 176, 192), (144, 240, 224)),
    "ice": ((8, 24, 80), (16, 96, 192), (96, 192, 240), (224, 248, 255)),
    "sunset": ((48, 8, 96), (160, 16, 96), (224, 80, 32), (255, 176, 48)),
    "forest": ((4, 40, 16), (16, 112, 32), (64, 176, 32), (160, 224, 64)),
    "crimson": ((40, 0, 4), (128, 0, 8), (208, 0, 20), (255, 64, 72)),
    "alarm": ((48, 0, 0), (192, 0, 0), (255, 48, 24), (255, 176, 160)),
    "meter": ((0, 176, 24), (80, 192, 0), (224, 144, 0), (255, 16, 0)),
    "duo": ((0, 224, 192), (224, 0, 160)),
    "pacman": ((255, 208, 0), (48, 48, 56)),
}
COLOR_NAMES: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "white": (255, 255, 255), "warm white": (255, 226, 190), "cool white": (205, 230, 255),
    "yellow": (255, 255, 0), "orange": (255, 128, 0), "purple": (128, 0, 255),
    "violet": (148, 0, 211), "pink": (255, 105, 180), "hot pink": (255, 20, 147),
    "magenta": (255, 0, 255), "cyan": (0, 255, 255), "teal": (0, 180, 160),
    "turquoise": (64, 224, 208), "lime": (128, 255, 0), "amber": (255, 191, 0),
    "gold": (255, 215, 0), "lavender": (181, 126, 220), "lilac": (200, 162, 200),
    "coral": (255, 127, 80), "indigo": (75, 0, 130), "brown": (139, 69, 19),
}
ALIASES = {"off white": "warm white", "ice blue": "cool white", "aqua": "cyan", "fuchsia": "magenta"}


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


def _parse_color(value: str) -> tuple[int, int, int] | None:
    normalized = " ".join(value.casefold().replace("-", " ").split())
    normalized = ALIASES.get(normalized, normalized)
    if normalized in COLOR_NAMES:
        return COLOR_NAMES[normalized]
    if re.fullmatch(r"#[0-9a-f]{6}", normalized):
        return tuple(int(normalized[index:index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]
    match = re.fullmatch(r"(?:rgb\s*)?\(?(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\)?", normalized)
    if match:
        channels = tuple(int(item) for item in match.groups())
        if all(0 <= item <= 255 for item in channels):
            return channels  # type: ignore[return-value]
    return None


def _interpolate(stops: tuple[tuple[int, int, int], ...], count: int = 12) -> list[tuple[int, int, int]]:
    result = []
    for index in range(count):
        position = index / count * len(stops)
        left_index = int(position) % len(stops)
        right = stops[(left_index + 1) % len(stops)]
        left = stops[left_index]
        fraction = position - int(position)
        result.append(tuple(round(left[channel] + (right[channel] - left[channel]) * fraction) for channel in range(3)))
    return result  # type: ignore[return-value]


class RingService:
    def __init__(self) -> None:
        self.client: Any = None
        self.ring_key: int | None = None
        self.segment_keys: dict[int, int] = {}
        self.effects: list[str] = []
        self.connected = False
        self.state: dict[str, Any] = {"on": False, "brightness": 0.0, "rgb": [0, 0, 0], "effect": "None"}
        self._condition = threading.Condition()
        self._revision = 0
        self._command_lock = asyncio.Lock()
        self._config: dict[str, str] = {}

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
        try:
            import aioesphomeapi
        except ImportError as exc:
            raise RuntimeError("Ring Light's ESPHome client is missing. Install aioesphomeapi in NIX's Python environment, then trust/enable the skill again.") from exc
        self.client = aioesphomeapi.APIClient(
            str(parsed_ip), 6053, noise_psk=psk
        )
        try:
            await asyncio.wait_for(self.client.connect(login=True), timeout=15)
            _device, entities, _services = await asyncio.wait_for(self.client.device_info_and_list_entities(), timeout=15)
            for entity in entities:
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
            with self._condition:
                self._revision = 0
            await asyncio.wait_for(self.client.device_info(), timeout=10)
            return {"ready": True}
        except Exception:
            self.connected = False
            try:
                await self.client.disconnect()
            except Exception:
                pass
            raise

    def _on_state(self, state: Any) -> None:
        state_key = getattr(state, "key", None)
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
        elif action == "palette":
            if "effect" in changes:
                expected = changes["effect"]
                predicate = lambda state: state["on"] and state["effect"] == expected
            else:
                predicate = lambda state: state["on"] and state["effect"] in {"", "None"}
        else:
            predicate = lambda _state: True
        state = self._wait_for_state(before, predicate)
        return state

    def _result(self, action: str, message: str, **fields: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "action": action,
            "state": dict(self.state),
            "device_connected": self.connected,
            "available_effects": self.effects[:64],
            "available_palettes": list(PALETTES),
            "message": message[:240],
            **fields,
        }

    def _arguments(self, text: str) -> dict[str, Any] | None:
        clean = " ".join((text or "").strip().split())
        low = clean.casefold()
        if len(clean) > 4000 or not re.search(r"\b(?:ring|echo\s*dot|dot|light|led)\b", low):
            return None
        if re.search(r"\b(?:what|which|show|list|tell me|available|can|could)\b.*\b(?:effects?|animations?|colors?|colours?)\b", low) and not re.search(r"\b(?:turn|switch|set|make|run|start|play|stop|disable|enable)\b", low):
            return {"action": "catalog"}
        if re.search(r"\b(?:state|status|what(?:'s| is) the ring doing|how does the ring look)\b", low):
            return {"action": "state"}
        if re.search(r"\b(?:stop|turn off|switch off|power off)\b.*\b(?:effect|animation|ring|light|dot)\b", low):
            return {"action": "effect", "effect": "None"}
        if re.search(r"\b(?:turn|switch|power)\b.*\b(?:off|down)\b|\b(?:off|disable)\b.*\b(?:ring|light|dot)\b", low):
            return {"action": "off"}
        if re.search(r"\b(?:turn|switch|power)\b.*\b(?:on|up)\b|\b(?:on|enable)\b.*\b(?:ring|light|dot)\b", low):
            return {"action": "on"}
        brightness_match = re.search(r"(?:\b(?:brightness|bright|dim|dimmer)\b[^\d]{0,24}(\d{1,3})\s*(?:%|percent)?|(\d{1,3})\s*(?:%|percent)\s*(?:brightness|bright|dim)?)", low)
        if brightness_match:
            brightness_text = brightness_match.group(1) or brightness_match.group(2)
            value = float(brightness_text) / (100.0 if int(brightness_text) > 1 or "%" in low or "percent" in low else 1.0)
            if 0.05 <= value <= 1:
                return {"action": "brightness", "brightness": value}
        rgb_match = re.search(r"\b(?:rgb|color|colour)\s*\(?\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*\)?", low)
        color: tuple[int, int, int] | None = None
        if rgb_match:
            channels = tuple(int(item) for item in rgb_match.groups())
            if all(channel <= 255 for channel in channels):
                color = channels  # type: ignore[assignment]
        if color is None:
            hex_match = re.search(r"#([0-9a-f]{6})\b", low)
            if hex_match:
                hex_value = hex_match.group(1)
                color = tuple(int(hex_value[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[assignment]
        if color is None:
            for name in sorted([*COLOR_NAMES, *ALIASES], key=len, reverse=True):
                if re.search(rf"\b{re.escape(name)}\b", low):
                    color = COLOR_NAMES[ALIASES.get(name, name)]
                    break
        if color is not None and re.search(r"\b(?:color|colour|rgb|make|turn|set|glow|shine|light|ring|dot)\b", low):
            return {"action": "color", "rgb": list(color)}
        for effect in sorted(self.effects, key=len, reverse=True):
            if effect.casefold() == "none":
                continue
            if effect.casefold() in low and re.search(r"\b(?:animation|animate|effect|run|start|play|do|make|set)\b", low):
                return {"action": "effect", "effect": effect}
        for palette in sorted(PALETTES, key=len, reverse=True):
            if re.search(rf"\b{palette}\b", low) and re.search(r"\b(?:palette|gradient|paint|animate|animation|effect|color|colour|look|display|make|set|use)\b", low):
                animate = bool(re.search(r"\b(?:animate|animation|effect)\b", low))
                if animate and palette in {"meter"}:
                    animate = False
                return {"action": "palette", "palette": palette, "mode": "animate" if animate else "paint"}
        return None

    async def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        if action == "catalog":
            return self._result(action, "The Echo Dot's live effect catalog and supported palettes are listed.")
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
                    raise ValueError("Choose a supported color or RGB channels from 0 through 255.")
                state = await asyncio.to_thread(self._send_and_confirm, {"state": True, "rgb": _rgb_float(rgb), "effect": "None"}, "color")
                color_name = next((name for name, value in COLOR_NAMES.items() if list(value) == rgb), "custom RGB")
                return self._result(action, f"The Echo Dot confirmed {color_name} ({rgb[0]}, {rgb[1]}, {rgb[2]}).", state=state, color_name=color_name, rgb=rgb)
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
            if action == "palette":
                name = args.get("palette")
                mode = args.get("mode", "paint")
                if name not in PALETTES or mode not in {"paint", "animate"}:
                    raise ValueError("Choose one of the available palettes in paint or animate mode.")
                if mode == "animate":
                    animate_effect = {"rainbow": "Rainbow", "aurora": "Aurora", "fire": "Fireplace", "ocean": "Ocean Ripple", "ice": "Ice Comet", "sunset": "Sunset Drift", "forest": "Forest Twinkle", "crimson": "Crimson Heartbeat", "alarm": "Alert", "duo": "DNA", "pacman": "Pac-Man"}.get(name)
                    if not animate_effect or animate_effect not in self.effects:
                        raise ValueError("The Dot does not advertise an animation for that palette.")
                    state = await asyncio.to_thread(self._send_and_confirm, {"state": True, "effect": animate_effect}, "palette")
                    return self._result(action, f"The Echo Dot confirmed its {name} palette animation ({animate_effect}).", state=state, palette=name, mode=mode, effect=animate_effect)
                # Paint the 12 channel values into the device's native segment lights.
                if len(self.segment_keys) < 12:
                    raise RuntimeError("The Dot does not expose all 12 segment entities required to paint a palette.")
                if self.state.get("effect") not in {None, "", "None"}:
                    raise RuntimeError("A running effect owns the ring. Stop the effect before painting a palette.")
                await asyncio.to_thread(self._send_and_confirm, {"state": True, "effect": "None"}, "effect")
                frame = _interpolate(PALETTES[name])
                for number, rgb in enumerate(frame, 1):
                    if number not in self.segment_keys:
                        raise RuntimeError(f"The Dot has no LED ring segment {number}.")
                    with self._condition:
                        before = self._revision
                    self.client.light_command(self.segment_keys[number], state=True, rgb=_rgb_float(rgb), brightness=1.0)
                    await asyncio.to_thread(self._wait_for_state, before, lambda _state: True)
                return self._result(action, f"The Echo Dot reported all 12 segment commands for the {name} palette.", palette=name, mode=mode)
        raise ValueError("Unsupported Ring Light action.")

    async def handle(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "match":
            return {"match": self._arguments(str(payload.get("text") or ""))}
        if operation == "execute":
            if payload.get("tool") != "control_ring" or not isinstance(payload.get("arguments"), dict):
                raise ValueError("Only the declared control_ring tool is available.")
            return {"result": await self._execute(payload["arguments"])}
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
