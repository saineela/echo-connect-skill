<p align="center"><a href="https://github.com/saineela/puca"><img src="https://res.cloudinary.com/dh5uxc6ql/image/upload/v1790917615/93d18a47-5f3a-46e1-ad71-705b2680442f_anp8f1.png" alt="NIX PUCA" width="320"></a></p>

[☆ Star NIX PUCA on GitHub](https://github.com/saineela/puca/stargazers)

# NIX Ring Light skill

This is a separately publishable NIX skill package for controlling an Echo Dot 2 LED ring. Its NIX worker uses ESPHome's documented `aioesphomeapi` native API client; it is a fresh implementation and does not redistribute the upstream Ring Light repository's unlicensed bridge/client code.

## NIX Skills marketplace format

The repository root `nix-skills.json` and `skills/ring-light/skill.json` follow NIX marketplace schema v1. The marketplace downloads only the paths in `files`. `entrypoint` and `runtime.protocol` opt the package into NIX's general worker contract (`nix-skill-jsonl-v1`); an install remains static and non-executable.

To try the local repo through the same flow as GitHub, serve it as a local GitHub-compatible HTTP fixture in tests; `/api/skills`' public GitHub importer remains unmodified and accepts only public GitHub repositories. After publishing, enter the canonical public GitHub repository URL, install Ring Light, configure its inputs, inspect the selected package version, then explicitly trust it.

## Device requirements and configuration

- Echo Dot 2 running ESPHome with the Ring Light implementation/API enabled (the Dot's firmware/ESPHome API already must exist; this skill does not flash firmware).
- NIX host can reach the Dot over a trusted LAN, TCP 6053.
- The declared `aioesphomeapi` dependency is provisioned during install into a private per-skill `.venv` (binary wheels only); the package entrypoint is not launched and no device connection is made at install time. This does not modify NIX's global Python.
- Configure the private LAN IPv4 and a rotated ESPHome Noise PSK in Skills setup. The key is a base64-encoded, exactly 32-byte Noise key. NIX stores it under `NIX_DATA_DIR/skills/config/` with restrictive permissions; it is excluded from installed package files, logs, API status, and model prompts.
- Reserve the Dot's address in DHCP. Change the setup value if it changes.

No upstream setup script, SSH access, shell-autostart installation, exposed WebSocket/HTTP bridge, or code from an unlicensed upstream project is required.

## Dashboard device visualization

The Devices page shows the Dot's reported RGB value as a twelve-segment ring, dims the ring when the reported power state is off, and updates it on Connect / Refresh. Each generated control input has a stable ID and name for browser accessibility/autofill; color changes remain explicit, confirmed control actions.

## Capabilities

This package intentionally has no preset color table, guessed firmware effects, or locally invented palettes. Luna selects RGB values for color words, and only exact effects from the live Dot firmware catalog are offered.

- Get current on/off, brightness, RGB and effect state, plus the device's reported friendly name when available. State comes from the ESPHome controller and is not independent visual confirmation.
- Turn the ring on or off.
- Set solid color from three RGB channels selected by the conversation model for the user's description, or use a user-provided hex/RGB triplet. There is no hardcoded named-color lookup.
- Set brightness between 5% and 100%.
- Run only an exact prebuilt effect reported by the connected Dot's current firmware catalog; `None` stops effects.
- State-changing calls require a subsequent state report from the device; otherwise NIX reports unconfirmed status.

The worker does not expose arbitrary entity keys/protocol messages to a model. Every command is constrained to one declared tool, a validated argument/result schema, live catalog data and device confirmation.

The files in this package are licensed under MIT; see `LICENSE`. The separate ESPHome API client dependency retains its own license and notices.

## Security boundary

NIX executes only after a user explicitly trusts the currently installed package digest. Runtime processes are isolated from NIX's Python process, but **not OS sandboxed**: arbitrary installed skill code runs as the NIX account. It may access that account's files, network and installed tools. Trust is not proof that code is safe. A changed digest must be trusted again. Untrust stops the persistent worker.

This implementation keeps the ESPHome PSK out of logs/model prompts and communicates directly with the Dot using ESPHome's encrypted native API. Use a rotated key. Keep ESPHome API access on the trusted local network; don't port-forward it. Home Assistant may concurrently write the same LEDs, with last-writer-wins behavior.

## Tests

Local tests mock GitHub API/raw endpoints and dependency provisioning, then exercise the marketplace preview/install flow and use a simulated ESPHome client for state and controls. They do not contact GitHub or a Dot, install external dependencies, flash firmware, or start a trusted worker.
