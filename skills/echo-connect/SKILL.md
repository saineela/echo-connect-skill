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
- “What is the ring doing?” Return the last state reported by the connected device.
- Use the operator's NIX Devices-page device name to identify this device; that editable name is an alias, not device authorization.

Solid colors are set with the RGB triplet proposed by Luna for that request; no named-color lookup table is bundled. Hex and explicit numeric RGB are passed through as supplied. Animation names must exactly match the effect list from the currently connected device firmware. The live catalog is authoritative; never invent effects or claim the device supports a guessed animation.

## Operational contract

1. The selected NIX conversation model may propose one natural-language-matched `control_ring` call using the declared schema. Core checks the current installed digest, explicit trust, configured state, selected skill/tool, and argument/result schema; only then may the worker execute it. The model never receives credentials or sends raw network requests, entity IDs, shell commands, or arbitrary protocol messages.
2. One action at a time. Wait for a result confirmed by a state update from the Dot. If no confirmation arrives, report that the state could not be confirmed; do not claim success or retry in a loop. A timeout or connectivity issue may leave the device state uncertain.
3. Keep requested brightness in the accepted 0.05–1.0 interval. No per-frame animation API exists: use only exact prebuilt device-side effects in the live catalog.
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
- Declared Python dependencies are provisioned during installation into this skill's private `.venv`; install does not launch the entrypoint or connect to a Dot.
- Unit/integration tests use simulated ESPHome clients. Live hardware commands are not sent during package tests.
