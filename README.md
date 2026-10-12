<p align="center">
  <a href="https://github.com/saineela/puca">
    <img src="https://res.cloudinary.com/dh5uxc6ql/image/upload/v1790917615/93d18a47-5f3a-46e1-ad71-705b2680442f_anp8f1.png" alt="NIX PUCA" width="360">
  </a>
</p>

<h1 align="center">Echo Connect</h1>

<p align="center">
  A NIX&nbsp;PUCA skill that controls an Echo Dot&nbsp;2 — its 12-segment LED ring, speaker volume,
  mute and spoken announcements — over the device's own local ESPHome API.
</p>

<p align="center">
  <a href="https://github.com/saineela/puca">
    <img alt="Requires NIX PUCA" src="https://img.shields.io/badge/requires-NIX%20PUCA-2f6f4e?style=for-the-badge">
  </a>
  <a href="https://github.com/saineela/puca/stargazers">
    <img alt="Star NIX PUCA on GitHub" src="https://img.shields.io/github/stars/saineela/puca?style=for-the-badge&label=%E2%98%86%20star">
  </a>
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue?style=for-the-badge">
</p>

---

> [!IMPORTANT]
> **This package does nothing on its own.** Echo Connect is a skill that runs **inside
> [NIX PUCA](https://github.com/saineela/puca)** — a self-hosted personal companion that turns a
> small always-on machine into a voice-and-device assistant. NIX owns the conversation, the skill
> marketplace, the explicit trust step and the worker runtime; this repository only ships the skill.
>
> ### 👉 [Explore NIX PUCA](https://github.com/saineela/puca) · [☆ Star it on GitHub](https://github.com/saineela/puca/stargazers)
>
> If Echo Connect is useful to you, a ⭐ on the NIX PUCA repository helps other people find the
> project. Thank you!

---

## What it does

Echo Connect gives your NIX assistant real control of a **rooted/ESPHome-flashed Echo Dot 2**:

| You can ask | The skill does |
|---|---|
| “Turn the Echo Dot ring on / off.” | Power the LED ring. |
| “Make the ring solid purple.” | Set a solid RGB color (NIX's model picks the channels; no hard-coded color table). |
| “Set the ring to 30 percent brightness.” | Dim to 5–100 %. |
| “Run the Rainbow effect.” | Run an **exact** effect from the connected Dot's live firmware catalog. |
| “Say dinner is ready.” | Speak an announcement in the configured voice. |
| “Set the volume to 40 percent.” / “Mute it.” | Control the Dot's speaker. |
| “Play this URL on the Dot.” / “Pause the music.” | Stream an http(s) audio URL through the media player, then pause/resume. |
| “Stop the Dot speaking.” | Stop speech and URL playback. |
| “What is the ring doing?” | Report the last device state. |

It talks to the Dot directly over ESPHome's **encrypted native API** on your local network — no
cloud bridge, no exposed web server, no unlicensed third-party code.

## Requirements

- **[NIX PUCA](https://github.com/saineela/puca)** with the Skills marketplace. *(Required — the
  skill runs in NIX's worker runtime.)*
- An **Echo Dot 2** flashed with an ESPHome build that exposes the ring/light entities.
- The NIX host and the Dot on the same trusted LAN (ESPHome API, TCP 6053).
- Optional: up to three **Unreal Speech** API keys for cloud-voiced announcements. Without them,
  the built-in Piper neural voice speaks locally.

## Install

1. In NIX PUCA, open **Skills → Add repository** and enter
   `https://github.com/saineela/echo-connect-skill`.
2. **Install** Echo Connect, then fill in setup:
   - **Device IP** — the Dot's private LAN IPv4 address (reserve it in DHCP).
   - **ESPHome key** — the device's rotated, 32-byte base64 ESPHome Noise PSK.
   - *(optional)* **Unreal Speech keys**, **Speech provider** (`auto` / `unrealspeech` / `local`)
     and **voice**.
3. Review and **explicitly trust** the installed package digest. NIX then starts the worker and
   verifies it can authenticate, find the light entity and read state.

> Declared Python dependencies are provisioned into the skill's own private `.venv` at install
> time (binary wheels only). Install never launches the entrypoint or connects to a device.

## Repository layout

```
nix-skills.json                    catalog entry for the NIX marketplace
skills/echo-connect/skill.json     manifest: setup fields, control_ring tool, schemas, device UI
skills/echo-connect/ring_light_worker.py   the JSONL worker (ESPHome API client)
skills/echo-connect/SKILL.md       operational contract for the assistant
skills/echo-connect/README.md      package-level README
```

## How it runs (trust model)

Installing a skill only saves its files. Running it is a **separate, explicit trust step**: NIX
starts the worker only after you trust the exact installed package digest, and a changed digest
invalidates that trust. Trusted skill code runs in a subprocess **without an OS sandbox**, with the
NIX account's permissions — read the source and the licence before trusting.

Credentials (the ESPHome PSK, any Unreal Speech keys) are stored in the skill's private config file,
never placed in logs, model prompts or results.

## Tests

The worker and its marketplace integration are covered by the NIX core test suite:

- `nix_core/test_ring_light_worker.py` — ESPHome state/controls against a simulated client, speech
  providers against a fake Unreal Speech endpoint, key rotation at 90 % of either monthly limit,
  local fallback, and result-schema validation.
- `nix_core/test_skills_marketplace.py` — the marketplace preview/install flow for this package.

Live hardware is never contacted by the tests. One ffmpeg conversion test uses a real `ffmpeg`
when one is present and is skipped otherwise.

## Licence

MIT — see [`skills/echo-connect/LICENSE`](skills/echo-connect/LICENSE). The separate `aioesphomeapi`
dependency keeps its own licence and notices.

---

<p align="center">
  Built for <a href="https://github.com/saineela/puca"><b>NIX PUCA</b></a> —
  <a href="https://github.com/saineela/puca/stargazers">☆ star the project</a> if you build with it.
</p>
