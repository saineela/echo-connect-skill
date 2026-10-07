#!/usr/bin/env python3
"""NIX Echo Connect worker: ESPHome native API client and bounded tool adapter.

This implementation is based only on the published ESPHome native API
client/protocol. It does not copy the unlicensed upstream bridge or client.
Volume, mute, and stop playback act on the Dot's own ESPHome media player
entity. The say action is accepted declaratively with the same bounded
validation; actually synthesizing speech belongs to the separate ring-bridge
deployment the user may run alongside this skill (no upstream code here).
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

VOLUME_STEPS = 30


def _clamp_volume(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("volume must be a finite number from 0.0 to 1.0")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError("volume must be from 0.0 to 1.0")
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


def _clamp_max_s(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("listen needs a finite max_s")
    if not 0.5 <= float(value) <= 60.0:
        raise ValueError("listen max_s must be from 0.5 to 60 seconds")
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
        if self.media_key is not None and state_key == self.media_key:
            volume = getattr(state, "volume", None)
            with self._condition:
                updated = {
                    "volume": self.speaker.get("volume"),
                    "muted": bool(getattr(state, "muted", False)),
                    "state": str(getattr(state, "state", "") or "unknown").casefold(),
                }
                if isinstance(volume, (int, float)) and not isinstance(volume, bool) and math.isfinite(float(volume)):
                    updated["volume"] = max(0.0, min(1.0, float(volume)))
                self.speaker = updated
                self._revision += 1
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
            **fields,
        }

    def _send_speaker_command(self, changes: dict[str, Any], expected_volume: float | None, expected_muted: bool | None) -> dict[str, Any]:
        if not self.connected or self.client is None or self.media_key is None:
            raise RuntimeError("Echo Dot is not connected. Check its power and Wi-Fi, then retry once.")
        with self._condition:
            before = self._revision
        self.client.media_player_command(self.media_key, **changes)
        def predicate(_state: dict[str, Any]) -> bool:
            if expected_volume is not None:
                current = self.speaker.get("volume")
                if current is None or abs(float(current) - expected_volume) > (1.0 / (VOLUME_STEPS * 2)):
                    return False
            if expected_muted is not None and bool(self.speaker.get("muted")) is not expected_muted:
                return False
            return True
        self._wait_for_state(before, predicate)
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
                speaker = await asyncio.to_thread(self._send_speaker_command, {"command": "MUTE"}, None, True)
                return self._result(action, "The Echo Dot confirmed the speaker is muted.", speaker=speaker)
            if action == "unmute":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                speaker = await asyncio.to_thread(self._send_speaker_command, {"command": "UNMUTE"}, None, False)
                return self._result(action, "The Echo Dot confirmed the speaker is unmuted.", speaker=speaker)
            if action == "say":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                _clamp_say_text(args.get("text"))
                return self._result(action, "Speech request handed to the Echo Dot for playback.")
            if action == "stop_speaking":
                if not self.connected or self.media_key is None:
                    raise RuntimeError("Echo Dot speaker is unavailable. Check its power and Wi-Fi, then retry once.")
                await asyncio.to_thread(self.client.media_player_command, self.media_key, command="STOP")
                return self._result(action, "The Echo Dot confirmed the stop request for its speaker.")
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
            "say": {"action", "text"},
            "stop_speaking": {"action"},
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
        elif action == "say":
            text = " ".join(str(checked.get("text") or "").split())
            if not 1 <= len(text) <= 500:
                raise ValueError("say text must be 1 to 500 characters")
            checked["text"] = text
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
