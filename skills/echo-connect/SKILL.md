<p align="center"><a href="https://github.com/saineela/puca"><img src="https://res.cloudinary.com/dh5uxc6ql/image/upload/v1790917615/93d18a47-5f3a-46e1-ad71-705b2680442f_anp8f1.png" alt="NIX PUCA" width="320"></a></p>

# Echo Connect skill for NIX PUCA

[☆ Star NIX PUCA on GitHub](https://github.com/saineela/puca/stargazers)

Control a configured Echo Dot 2's 12-segment LED ring through NIX's local ESPHome API worker. NIX launches the skill only after the user explicitly trusts the exact installed package. Setup collects the Dot's **private IPv4 address** and its **rotated ESPHome Noise PSK** in the NIX Skills page; keys are stored in a private file and passed only to the worker. Never ask the conversational model to expose or repeat the key.

## What you can do

Users can ask naturally, for example:

- “Turn the Echo Dot ring on/off.”
- “Make the ring solid purple.” The conversation model chooses RGB channels for the request; the skill does not contain a named-color-to-RGB table. Explicit hex/RGB values are accepted as provided.
- “Set the ring to 30 percent brightness.” (NIX safely uses a 5% minimum to avoid device-off ambiguity.)
- “Run the Rainbow animation.” Use only exact effects from the currently connected Dot's catalog; NIX confirms the selected effect with a returned device state.
- “Say dinner is ready.” Speak the announcement in the configured voice: the Unreal Speech cloud voice when API keys are stored, otherwise (and as a fallback) the built-in local voice.
- “What is the ring doing?” Return the last state reported by the connected device.
- Use the operator's NIX Devices-page device name to identify this device; that editable name is an alias, not device authorization.

## Speech providers

Announcements (`say`) use the provider chosen in Skills → Echo Connect setup:

- **Unreal Speech (optional; used when configured).** Store up to three API keys from [unrealspeech.com](https://docs.v8.unrealspeech.com/). `say` renders the text with the selected cloud voice (`amy`, `scarlett`, `dan`, `liv`, `will`) by calling the documented `/speech` endpoint, downloads the resulting MP3, converts it to a WAV the Dot plays, and serves it over the LAN like the local path. Each key's characters and generated audio seconds are tracked per UTC month against the configured limits (defaults: 250,000 characters and 6 audio hours per key, the free tier). At **90%** of either limit a key hands the next announcement to the following key; a key the provider rejects or reports as out of quota is retired for the rest of that month. When every key is spent, missing, or failing, the built-in voice speaks instead and the result says why.
- **Built-in local voice (fallback).** Piper neural TTS from the voice catalog (`amy`, `lessac`, `hfc_female`, `kathleen`, `jenny`, `alba`), with espeak-ng as the last resort. Needs no key and no network, so announcements keep working with no cloud account, no allowance left, or no connectivity.

`Speech provider` accepts `auto` (default: cloud when a key is saved, local otherwise), `unrealspeech` (cloud first, local fallback), or `local` (never call the cloud). Limits are per key and per month (`Monthly characters per key`, `Monthly audio hours per key`).

Every result carries a `tts` object: `engine` (`unrealspeech` or `local`), `key_index` (1–3, or 0), `voice`, `fallback` (why the cloud was skipped), `month`, the two limits, and one entry per configured key with `characters`, `seconds`, `characters_percent`, `seconds_percent`, and `ready`. Reported usage is what the worker measured, not a provider invoice. Totals persist in `unrealspeech-usage.json` inside the TTS cache directory (default `/root/data/tts`), keyed by a hash of each key, so the keys themselves are never written to it. Announcement text only leaves the NIX host while a configured cloud key is actually used.

Solid colors are set with the RGB triplet proposed by Luna for that request; no named-color lookup table is bundled. Hex and explicit numeric RGB are passed through as supplied. Animation names must exactly match the effect list from the currently connected device firmware. The live catalog is authoritative; never invent effects or claim the device supports a guessed animation.

The speaker supports:

- “Set the volume to 40 percent.” / “Mute the Dot.” / “Unmute it.” — the level is the device's own 0.0–1.0 volume, the same Home Assistant shows.
- “Play this URL on the Dot.” — `play_url` streams an **http(s)** audio URL through the media player (music, not the announce channel). Formats the Dot cannot decode are converted locally with **ffmpeg** (the skill's `imageio-ffmpeg` dependency provides a bundled binary, or set `NIX_FFMPEG`), served from a private LAN HTTP port, and handed to the device. Only build the URL argument from data the user or a trusted provider supplied; never fetch or echo secrets.
- “Pause the music.” / “Resume it.” — `pause`/`resume` control URL playback (pause requires the device to advertise `supports_pause`). They answer as soon as the speaker reports the new state, polled for a fraction of a second, so a transport tap is not held while waiting for a pushed event some firmware never sends.
- “Stop the Dot speaking.” — `stop_speaking` stops both speech and URL playback.

URL playback is prepared in one bounded pass: the NIX host fetches the stream once, converts it when the Dot cannot decode its container (the reference Dot advertises 48 kHz stereo WAV only), and hands the device a URL on its own LAN file server. That is about a second for a whole track, and the result says how many seconds of audio it prepared. When an origin serves only the opening of a stream, the part it did serve is played and the shorter length is reported — a track that starts beats a stall that ends in an error. Report a conversion or fetch failure honestly rather than claiming the device played something it did not.

## Operational contract

1. The selected NIX conversation model may propose one natural-language-matched `control_ring` call using the declared schema. Core checks the current installed digest, explicit trust, configured state, selected skill/tool, and argument/result schema; only then may the worker execute it. The model never receives credentials or sends raw network requests, entity IDs, shell commands, or arbitrary protocol messages.
2. One action at a time. Wait for a result confirmed by a state update from the Dot. If no confirmation arrives, report that the state could not be confirmed; do not claim success or retry in a loop. A timeout or connectivity issue may leave the device state uncertain.
3. Keep requested brightness in the accepted 0.05–1.0 interval. No per-frame animation API exists: use only exact prebuilt device-side effects in the live catalog. Speech text is 1–500 characters; a `say` reports which engine spoke, and the local fallback must be stated plainly rather than presented as the cloud voice.
4. Device status is the last state update received over ESPHome's encrypted API and includes on/off, brightness, RGB, and effect. It confirms the controller's reported values, not photons or visible output; do not claim physical visual confirmation without an independent sensor or user observation.
5. A running effect owns the ring. A solid RGB request explicitly stops the effect.
6. Segment overrides and animation/color state may interact with Home Assistant or its automations; whichever controller writes last wins.
7. Do not flash or run attention-seeking animations unless the user clearly requested them. Ask if intent is unclear.

## Trust and network security

Trust is explicit and separate from package installation. The dashboard warns that trusted skill code runs in a subprocess **without an OS sandbox**, with NIX's user permissions; it can access that user's files, local network, and other capabilities. Read the repository and license before trusting. A changed package digest invalidates trust and requires review again. Untrusting stops the worker.

The worker connects directly from the NIX host to the Dot's ESPHome API on port 6053, encrypted with the configured PSK. This NIX skill has no unauthenticated WebSocket/HTTP bridge listener. Use a Dot API key you have rotated; never reuse a credential published in any public source or diagnostic. The PSK is a device-control credential and must not be committed, logged, or included in model prompts/results. The NIX host and Dot must share a routable trusted LAN/VLAN; firewall narrowly to that host. Do not expose ESPHome control to the internet.

## Setup and testing

- Install the skill package from a public NIX skills repository (or the local GitHub-compatible test repository used during development). Installation only downloads declared files.
- Set the Dot's IP and rotated 32-byte base64 ESPHome API key under Skills → Echo Connect setup.
- Review and explicitly trust that package; NIX starts the local worker and verifies it can authenticate, find the LED ring entity and receive state.
- Optional: paste up to three Unreal Speech API keys to speak with cloud voices. Keys use NIX's masked `token` setup value type: they are stored in the skill's private config file, never echoed back by the API after saving, and covered by the same blank-field-keeps-the-stored-key rule as a secret.
- Declared Python dependencies are provisioned during installation into this skill's private `.venv`; install does not launch the entrypoint or connect to a Dot. Cloud speech also needs `ffmpeg` (the declared `imageio-ffmpeg` wheel provides it); without it the built-in voice speaks instead.
- Unit/integration tests use simulated ESPHome clients. Live hardware commands are not sent during package tests.
