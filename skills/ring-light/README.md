# NIX Ring Light skill

This is a separately publishable NIX skill package for controlling an Echo Dot 2 LED ring. Its NIX worker uses ESPHome's documented `aioesphomeapi` native API client; it is a fresh implementation and does not redistribute the upstream Ring Light repository's unlicensed bridge/client code.

## NIX Skills marketplace format

The repository root `nix-skills.json` and `skills/ring-light/skill.json` follow NIX marketplace schema v1. The marketplace downloads only the paths in `files`. `entrypoint` and `runtime.protocol` opt the package into NIX's general worker contract (`nix-skill-jsonl-v1`); an install remains static and non-executable.

To try the local repo through the same flow as GitHub, serve it as a local GitHub-compatible HTTP fixture in tests; `/api/skills`' public GitHub importer remains unmodified and accepts only public GitHub repositories. After publishing, enter the canonical public GitHub repository URL, install Ring Light, configure its inputs, inspect the selected package version, then explicitly trust it.

## Device requirements and configuration

- Echo Dot 2 running ESPHome with the Ring Light implementation/API enabled (the Dot's firmware/ESPHome API already must exist; this skill does not flash firmware).
- NIX host can reach the Dot over a trusted LAN, TCP 6053.
- `aioesphomeapi` installed in NIX's Python environment. Setup of third-party dependencies is deliberately separate and never runs during package install or trust.
- Configure the private LAN IPv4 and a rotated ESPHome Noise PSK in Skills setup. The key is a base64-encoded, exactly 32-byte Noise key. NIX stores it under `NIX_DATA_DIR/skills/config/` with restrictive permissions; it is excluded from installed package files, logs, API status, and model prompts.
- Reserve the Dot's address in DHCP. Change the setup value if it changes.

No upstream setup script, SSH access, shell-autostart installation, exposed WebSocket/HTTP bridge, or code from an unlicensed upstream project is required.

## Capabilities

- Get current on/off, brightness, RGB and effect state.
- Turn the ring on or off.
- Set solid RGB using common color names, `#RRGGBB` or three 0–255 channels.
- Set brightness between 5% and 100%.
- Run any exact prebuilt effect reported by current Dot firmware; `None` stops effects.
- Paint or animate the documented 12 named palettes when supported; paint mode needs all 12 segment entities.
- State-changing calls require a subsequent state report from the device; otherwise NIX reports unconfirmed status.

The worker does not expose arbitrary entity keys/protocol messages to a model. Every command is constrained to one declared tool, a validated argument/result schema, live catalog data and device confirmation.

The files in this package are licensed under MIT; see `LICENSE`. The separate ESPHome API client dependency retains its own license and notices.

## Security boundary

NIX executes only after a user explicitly trusts the currently installed package digest. Runtime processes are isolated from NIX's Python process, but **not OS sandboxed**: arbitrary installed skill code runs as the NIX account. It may access that account's files, network and installed tools. Trust is not proof that code is safe. A changed digest must be trusted again. Untrust stops the persistent worker.

This implementation keeps the ESPHome PSK out of logs/model prompts and communicates directly with the Dot using ESPHome's encrypted native API. Use a rotated key. Keep ESPHome API access on the trusted local network; don't port-forward it. Home Assistant may concurrently write the same LEDs, with last-writer-wins behavior.

## Tests

Local tests mock GitHub API/raw endpoints using this package tree, then exercise the same marketplace preview and static install functions as a public repository import. They do not contact GitHub, contact a Dot, flash firmware, install dependencies, or start a trusted worker.
