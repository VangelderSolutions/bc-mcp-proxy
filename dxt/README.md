# Claude Desktop Extension (.mcpb) packaging

This directory contains everything needed to build a Claude Desktop Extension
bundle from the proxy. The resulting `.mcpb` file (MCP Bundle, the successor of
`.dxt`) is a one-click install for Claude Desktop and is what gets submitted to
Anthropic's Connectors Directory as a desktop extension.

## What's here

| File              | Purpose                                                                                       |
|-------------------|-----------------------------------------------------------------------------------------------|
| `manifest.json`   | The MCPB manifest (`manifest_version` 0.3). Defines metadata, the privacy-policy URL, the user_config schema (TenantId, ClientId, Environment, Company, …) and how Claude Desktop should launch the proxy. |
| `requirements.txt`| pip-compiled lockfile of every Python dependency; the build vendors these as wheels into the bundle. Compile it on Linux (see `requirements.in`). |
| `build.ps1`       | PowerShell build script (Windows / cross-platform via `pwsh`).                                |
| `build.sh`        | POSIX shell build script (macOS / Linux).                                                     |
| `icon.png`        | 512×512 PNG extension icon (Vangelder Solutions artwork: white "BC" with the V5 green rule on teal); `icon-256.png` is the 256×256 variant. Both are downsampled from the master in the Vangelder branding archive — replace both together. |

## Building

From the repo root:

```bash
# Windows / pwsh
pwsh dxt/build.ps1

# macOS / Linux
./dxt/build.sh
```

The script:
1. Stages `manifest.json`, `requirements.txt`, the `bc_mcp_proxy` package and `LICENSE` into `dxt/build/`.
2. Vendors wheels for Python 3.11–3.14 of the host platform under `server/wheels/cp3XY/`.
3. Packs that staging directory into `dist/vgs-bc-mcp-<version>-<platform>.mcpb` using the `@anthropic-ai/mcpb` CLI (via `npx` if `mcpb` is not on PATH).

The build artifacts (`dist/*.mcpb` and `dxt/build/`) are git-ignored.

## Installing locally

Double-click the `.mcpb` file (or open Settings → Extensions → Install from file). Claude Desktop will:

1. Prompt for each `user_config` value (Tenant ID, Client ID, Environment, Company, optional Configuration Name, log level, endpoint, auth mode).
2. Launch the proxy on demand with the system `python3` / `python`, using the `mcp_config.command` + `args` from the manifest and the vendored wheels on `PYTHONPATH`.

The first BC tool call triggers the sign-in (browser, with device-code fallback); the bundled proxy is the same code as the PyPI package.

## Publishing to Anthropic's Connectors Directory

Desktop extensions are submitted through Anthropic's desktop-extension form (<https://clau.de/desktop-extention-submission>), not the remote-connector portal. Before submitting a release:

1. Keep `dxt/icon.png` (512×512) and `dxt/icon-256.png` (256×256) in sync with the master icon in the Vangelder branding archive. The icon is original Vangelder Solutions artwork; the Business Central logo itself is a Microsoft trademark and must not be used as a product icon.
2. Keep `privacy_policies` in `manifest.json` and the README's *Privacy Policy* section pointing at https://vangeldersolutions.github.io/bc-mcp-proxy/privacy/ (source `docs/privacy.md`, published with GitHub Pages). A missing or incomplete privacy policy is an immediate rejection.
3. Every tool must carry `title` and `readOnlyHint`/`destructiveHint`; the proxy adds them for BC's tools (see the README). Names must be ≤ 64 characters.
4. Test the bundle on Windows **and** macOS, and run every tool once (MCP Inspector or Claude Desktop) — the form asks you to confirm this.
5. Have ready: the release asset URL of the `.mcpb`, the repository URL, the documentation URL (README), the privacy-policy URL, a support contact, at least three example prompts, and a fully populated test account (BC sandbox + Entra app + user) for the reviewer. Never commit test credentials.
6. Bump the version in `bc_mcp_proxy/_version.py`, `pyproject.toml` and `dxt/manifest.json` together, merge, push the `v*.*.*` tag; the release workflow attaches the three bundles plus `SHA256SUMS.txt`.

Review criteria: <https://claude.com/docs/connectors/building/review-criteria>. Questions or escalations: mcp-review@anthropic.com.

## Verifying the manifest

The MCPB CLI ships a validator:

```bash
npx --yes @anthropic-ai/mcpb validate dxt/manifest.json
```

Run this after any manifest edit to catch schema regressions before packing.
