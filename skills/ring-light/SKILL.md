# Ring Light skill for NIX PUCA

Control a configured Echo Dot 2's 12-segment LED ring through NIX's local ESPHome API worker. NIX launches the skill only after the user explicitly trusts the exact installed package. Setup collects the Dot's **private IPv4 address** and its **rotated ESPHome Noise PSK** in the NIX Skills page; keys are stored in a private file and passed only to the worker. Never ask the conversational model to expose or repeat the key.

## What you can do

Users can ask naturally, for example:

- “Turn the Echo Dot ring on/off.”
- “Make the ring solid purple / warm white / #3366FF / RGB 30, 80, 220.”
- “Set the ring to 30 percent brightness.” (NIX safely uses a 5% minimum to avoid device-off ambiguity.)
- “Run the Rainbow animation.” Use only exact effects from the currently connected Dot's catalog; NIX confirms the selected effect with a returned device state.
- “Show a fire palette” (paint its 12-segment gradient) or “animate the ocean palette.”
- “What is the ring doing?” Return the last state reported by the connected device.

Solid colors are set with RGB. Palette names are multi-color gradients, not solid color names. Palette/effect availability can vary with firmware; the live device effect list wins. Named colors supported for solid RGB: red, green, blue, white, warm white, cool white, yellow, orange, purple, violet, pink, hot pink, magenta, cyan, teal, turquoise, lime, amber, gold, lavender, lilac, coral, indigo, brown; 6-digit hex and three numeric RGB channels are supported too.

## Operational contract

1. The deterministic NIX Core skill runtime parses/validates intent and arguments; the selected conversational model does not send raw network requests, select entity IDs, shell commands, or arbitrary JSON. All models in NIX use this same execution path.
2. One action at a time. Wait for a result confirmed by a state update from the Dot. If no confirmation arrives, report that the state could not be confirmed; do not claim success or retry in a loop. A timeout or connectivity issue may leave the device state uncertain.
3. Keep requested brightness in the accepted 0.05–1.0 interval; think about room lighting at night. No per-frame animation API exists: use prebuilt device-side effects.
4. A running effect owns the ring. Stop it before attempting segment painting. Solid RGB stops any effect. Palette painting requires all 12 segment entities to be available.
5. Segment overrides and animation/color state may interact with Home Assistant or its automations; whichever controller writes last wins.
6. Do not flash or run attention-seeking animations unless the user clearly requested them. Ask if intent is unclear.

## Trust and network security

Trust is explicit and separate from package installation. The dashboard warns that trusted skill code runs in a subprocess **without an OS sandbox**, with NIX's user permissions; it can access that user's files, local network, and other capabilities. Read the repository and license before trusting. A changed package digest invalidates trust and requires review again. Untrusting stops the worker.

The worker connects directly from the NIX host to the Dot's ESPHome API on port 6053, encrypted with the configured PSK. This NIX skill has no unauthenticated WebSocket/HTTP bridge listener. Use a Dot API key you have rotated; never reuse a credential published in any public source or diagnostic. The PSK is a device-control credential and must not be committed, logged, or included in model prompts/results. The NIX host and Dot must share a routable trusted LAN/VLAN; firewall narrowly to that host. Do not expose ESPHome control to the internet.

## Setup and testing

- Install the skill package from a public NIX skills repository (or the local GitHub-compatible test repository used during development). Installation only downloads declared files.
- Set the Dot's IP and rotated 32-byte base64 ESPHome API key under Skills → Ring Light setup.
- Review and explicitly trust that package; NIX starts the local worker and verifies it can authenticate, find the LED ring entity and receive state.
- Unit/integration tests use simulated ESPHome clients. Live hardware commands are not sent during package tests.
