# VGS-BC-MCP

[![CI](https://github.com/VangelderSolutions/bc-mcp-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/VangelderSolutions/bc-mcp-proxy/actions/workflows/ci.yml)
[![Snyk](https://github.com/VangelderSolutions/bc-mcp-proxy/actions/workflows/snyk.yml/badge.svg)](https://github.com/VangelderSolutions/bc-mcp-proxy/actions/workflows/snyk.yml)
[![Known Vulnerabilities](https://snyk.io/test/github/VangelderSolutions/bc-mcp-proxy/badge.svg)](https://snyk.io/test/github/VangelderSolutions/bc-mcp-proxy)
[![Latest Release](https://img.shields.io/github/v/release/VangelderSolutions/bc-mcp-proxy)](https://github.com/VangelderSolutions/bc-mcp-proxy/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

> **Fork of [microsoft/BCTech `samples/BcMCPProxyPython`](https://github.com/microsoft/BCTech/tree/master/samples/BcMCPProxyPython)** — a resilient Python MCP stdio proxy that bridges Claude Desktop, VS Code, Cursor and other MCP-compatible clients to the Microsoft Dynamics 365 Business Central MCP HTTP endpoint.
>
> Built and maintained by **[Vangelder Solutions](https://www.vangeldersolutions.be)**. Original: Copyright (c) Microsoft Corporation. Modifications: Copyright (c) 2026 Vangelder Solutions. Licensed under the MIT License.

> ✅ **BC v28 (2026 release wave 1) and v29 (2026 release wave 2, GA October 2026).** Microsoft's Business Central MCP server is generally available from version 28 onward on a single header-routed endpoint, and this proxy targets it by default. Version 29 keeps that contract and adds data-query and report-inbox tools, which the proxy forwards unchanged. The v26/v27 per-environment endpoint that preceded it was a preview; it is still supported here as an explicit legacy path for environments that haven't upgraded.
>
> The proxy itself is MIT-licensed open source, actively maintained, with a 230+ test suite and Snyk-monitored dependencies. Suitable for development, evaluation, and pilot deployments. Production fitness is your organisation's call — see [Security](#security) for the threat model.

---

## TL;DR

- **What you get**: ask Claude (or another AI client) *"show me the top 10 customers in CRONUS USA by outstanding balance"* and it pulls that data live from **your** BC environment over a secure, authenticated connection. No exports, no copy-paste, no intermediate step.
- **What it costs**: nothing in licenses — MCP is built into BC starting from version 26 (May 2025 release wave). You choose your AI client (Claude Desktop is free for personal use).
- **The hard part**: creating an Azure App Registration with the right permissions. Ten minutes for someone IT-comfortable; the toughest part for everyone else. We're happy to help — see the [Need help?](#need-help) section.

## Quick install (Claude Desktop)

Pre-built `.mcpb` bundles (MCP Bundle, the successor of the `.dxt` format — older `.dxt` releases still install) are published on each release with all Python dependencies vendored — no `pip install` step required.

1. **Download** the bundle for your platform from the [latest release](https://github.com/VangelderSolutions/bc-mcp-proxy/releases/latest):

   | Platform | Asset |
   |---|---|
   | Windows 64-bit | `vgs-bc-mcp-<version>-win-amd64.mcpb` |
   | macOS Apple Silicon | `vgs-bc-mcp-<version>-darwin-arm64.mcpb` |
   | Linux x86_64 | `vgs-bc-mcp-<version>-linux-x86_64.mcpb` |

   Every release also ships `SHA256SUMS.txt` so you can verify the download.

   *Intel macOS (`x86_64`) is not shipped as a pre-built asset — Apple Silicon has been the default since 2020 and the audience for Intel-only Macs is vanishing. Intel Mac users can build from source via `./dxt/build.sh` (any Mac with Python 3.10+).*

2. **Double-click** the downloaded file. Claude Desktop opens an install dialog.
3. **Fill in** Tenant ID, Client ID, Environment, Company, (optional) Configuration Name. The defaults already point at the BC v28/v29 endpoint; override via the *Business Central MCP endpoint* field for v26/v27.
4. **Restart Claude Desktop.**

That's the whole install. The Azure App Registration setup is the only remaining step — see [Step 1 — Azure App Registration](#step-1--azure-app-registration) below, or contact us via the [Need help?](#need-help) section if you'd prefer it done for you.

> Building from source (`pwsh dxt/build.ps1` or `./dxt/build.sh`) produces an identical bundle. Use the build path if you want to inspect the artifact, customise the manifest, or ship a private fork.

## What this fork adds

- ✅ **Reconnect on transient upstream errors.** `httpx.ReadTimeout`, `RemoteProtocolError`, and `NetworkError` (including the same errors wrapped in an `ExceptionGroup` by anyio) trigger an exponential backoff reconnect — `1s → 2s → 4s → 8s → 16s`, default 5 attempts. The local stdio pipe to your MCP client stays open while reconnecting.
- ✅ **Pre-emptive MSAL silent token refresh.** Each acquired access token's expiry is tracked locally; when remaining validity drops below `token_refresh_skew_seconds` (default 300) the next call asks MSAL to mint a new token via `acquire_token_silent(force_refresh=True)` instead of letting Business Central reject the stale one with `Authentication_InvalidCredentials`.
- ✅ **Surface masked upstream errors.** Some Business Central MCP responses ship with `isError: false` but the content is actually an error message ("Semantic search is not enabled", "Authentication_InvalidCredentials", etc.). The proxy now flags those as real MCP errors so the client sees them.
- ✅ **BC v28 endpoint support.** Auto-detects `mcp.businesscentral.dynamics.com` and switches to the new header-based routing (TenantId + EnvironmentName headers) while keeping v26/v27 behaviour intact.
- ✅ **Cold-start mitigation.** Three-tier `tools/list` cache (disk → in-memory pre-warm → upstream) masks BC's 30s+ first-call latency that otherwise trips Claude Desktop's hardcoded MCP request timeout. See [Cold-start mitigation](#cold-start-mitigation).
- ✅ **SSRF hardening.** `BC_BASE_URL` is validated at startup: scheme must be `https`, host must be `*.businesscentral.dynamics.com`. Override via `BC_ALLOW_NON_STANDARD_BASE_URL=1` for local mock testing.
- ✅ **Pinned transitive dependencies.** Explicit security floors for `h11`, `cryptography`, `pyjwt`, `python-multipart`, `starlette`, `urllib3`, `requests`, `python-dotenv` — eliminates 16 CVE paths Snyk flagged in the upstream `mcp`/`msal`/`httpx` transitive trees. See [SECURITY.md](SECURITY.md).
- ✅ **Pytest test suite.** 95 tests cover error classification, backoff progression, MSAL refresh-skew boundaries, masked-error pattern matching, v28 endpoint detection, OAuth scope auto-switch, the persistent tools cache, and `base_url` validation.

The CLI surface is unchanged — every flag and env var from the upstream sample still works.

---

## What is the Model Context Protocol?

MCP is an **open standard** Anthropic published in 2024, since adopted by OpenAI, Microsoft and most of the AI tooling industry. It solves one problem: how do you give an AI assistant safe access to your data and systems without building a bespoke integration for every combination?

- An **MCP server** sits on the side of the system that exposes data or actions — in our case, Business Central.
- An **MCP client** is the AI tool the user interacts with — Claude Desktop, VS Code Copilot, Cursor, ChatGPT desktop with MCP support.
- Between them, **tools** are exchanged in a standardised format: name, description, parameter schema, and the result of an invocation.

Microsoft has, since **Business Central 2025 release wave 1 (version 26)**, shipped a built-in MCP server in every BC environment. It listens on a Microsoft-hosted endpoint, validates your OAuth token, and exposes your configuration as a set of tools the AI client can call.

## What this proxy does (and why you need it)

The BC MCP server speaks **HTTP** — a streamable-HTTP variant with Server-Sent Events. Most AI clients (Claude Desktop, VS Code, Cursor) run locally on your machine and speak **stdio** to their MCP servers, not HTTP.

The proxy is the translator:

```
┌─────────────────┐  stdio    ┌──────────────────┐  HTTPS + OAuth   ┌────────────────────────┐
│  Claude Desktop │ ◄────────►│ bc-mcp-proxy     │ ◄───────────────►│ Business Central MCP   │
│  / VS Code      │           │ (on your machine)│                  │ (Microsoft-hosted)     │
└─────────────────┘           └──────────────────┘                  └────────────────────────┘
```

---

## Prerequisites

| Item | Detail |
|---|---|
| **BC environment** | Version 28.0 or later recommended (v29 supported); v26/v27 still work on the legacy endpoint. Sandbox or production. Read-only access to all API pages works out of the box; writes need an MCP Server Configuration with **Unblock Edit Tools** on. |
| **Microsoft Entra (Azure AD) tenant** | With **administrator** rights — you'll create an App Registration and grant API permissions. |
| **An AI client** | Claude Desktop (free), VS Code with MCP support, Cursor, or any other stdio-MCP capable tool. |
| **Python 3.10+** on your machine | Claude Desktop launches the proxy with the system `python3`. The `.mcpb` bundle vendors all Python dependencies internally (wheels for Python 3.10–3.14 of the host platform), so no separate `pip install` is required. |

---

## Step-by-step setup

### Step 1 — Azure App Registration

In the Azure portal:

1. Open **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name it something recognisable, e.g. `BC MCP Proxy — production`.
3. Supported account types: **Accounts in this organizational directory only** (single tenant).
4. Leave the Redirect URI blank for now.
5. Click **Register**. Note the **Application (client) ID** and **Directory (tenant) ID**.

Then in the same app:

6. **Authentication** → **Add a platform** → **Mobile and desktop applications**, then:
   - **Tick the `http://localhost` checkbox.** *(Required.)* The proxy signs you
     in with an interactive browser flow that redirects back to a localhost
     loopback listener — Microsoft Entra allows any port on `http://localhost`
     for public clients, so the checkbox is all you need (no port to specify).
     Without this you'll get `AADSTS500113`/`AADSTS50011` and the proxy falls
     back to the slower device-code flow.
   - Also add the custom redirect URI (used by the device-code fallback):
     ```
     ms-appx-web://Microsoft.AAD.BrokerPlugin/<your-client-id>
     ```
7. Lower on the same page: set **"Allow public client flows"** to **Yes**, save.

Permissions:

8. **API permissions** → **Add a permission** → **Dynamics 365 Business Central** → **Delegated permissions**:
   - Tick `Financials.ReadWrite.All` (or `Financials.Read.All` for read-only).
   - Tick `user_impersonation`.
9. Click **Add permissions**, then **Grant admin consent for [tenant]**.

> Without admin consent the first sign-in will fail. The green checkmarks next to each permission after *Grant admin consent* are the signal that you're done.

### Step 2 — Create a BC MCP Configuration

In Business Central, in your target environment:

1. Search for **"MCP Server Configurations"** (the page is also titled *Model Context Protocol Server Configurations*).
2. Click **+ New**.
3. Fill in:
   - **Name**: e.g. `Default MCP`. The name flows through as a header — case and trailing whitespace matter.
   - **Active**: **switch on**. ← *The* most common pitfall: you save the page (BC shows "Saved"), but Active is off by default. Without `Active = Yes` BC rejects every tool call with *"The MCP Configuration named X was not found or not active"*.
   - **Dynamic Tool Mode**:
     - **Off (Static mode)** — BC generates one `List_<EntityName>_PAG<id>` tool per selected page. Predictable, fast, but you choose tools up front.
     - **On (Dynamic mode)** — BC offers three generic tools (`bc_actions_search`, `bc_actions_describe`, `bc_actions_invoke`) that let the AI client search, describe and invoke an action at runtime. Far more flexible, but slower on first call (see below).
   - **Discover Additional Objects** (only relevant in dynamic mode): tick this to expose objects outside your explicit toolset for read-only discovery.
4. Add **System Tools** or **Available Tools** as needed.
5. **Save**.

> **Tip**: create two configurations with the same toolset, one with `Active = No` and one with `Active = Yes`. That way you can experiment safely without touching the live config.

### Step 3 — Install the proxy

```bash
python -m pip install --upgrade vgs-bc-mcp
```

> **Renamed in 0.6.0.** Earlier releases were published as `360solutions-bc-mcp`. That project
> is archived and stops at 0.5.7 — it does **not** upgrade into this one, so install the new
> name explicitly. If you already have the old package, replace it rather than upgrading it:
>
> ```bash
> python -m pip uninstall -y 360solutions-bc-mcp
> python -m pip install --upgrade vgs-bc-mcp
> ```
>
> Both distributions ship the same `bc_mcp_proxy` files, so having them installed side by side
> means uninstalling either one takes the other's files with it. Keep only the new one.
>
> The import package (`bc_mcp_proxy`) and the `python -m bc_mcp_proxy` command are unchanged, so
> existing MCP client configurations keep working. Bundle files are named `vgs-bc-mcp-<version>-<platform>.mcpb` since 0.8.0 (previously `bc-mcp-proxy-…​.dxt`).

Or from source:

```bash
git clone https://github.com/VangelderSolutions/bc-mcp-proxy.git
cd bc-mcp-proxy
python -m pip install -e .
```

Verify:

```bash
python -m bc_mcp_proxy --help
```

### Step 4 — Configure

Create a `.env` next to the proxy (this file is git-ignored):

```ini
BC_TENANT_ID=<your-tenant-id>
BC_CLIENT_ID=<your-client-id-from-step-1>
BC_ENVIRONMENT=Production
BC_COMPANY=CRONUS USA
BC_CONFIGURATION_NAME=Default MCP
```

**For BC v26 or v27 (legacy)** add:

```ini
BC_BASE_URL=https://api.businesscentral.dynamics.com
```

(The v28+/v29 host is the default — Microsoft's documented endpoint for every MCP client. The proxy auto-switches both the OAuth scope and the request headers to match.)

**Company or configuration names with accents** (`Société Générale`, `CRONUS Århus A/S`, `Ærø Handel`) need no special treatment: the proxy Base64-encodes non-ASCII header values the way Microsoft's MCP server requires (`=?base64?…?=`, MCP SEP-2243). Type the name exactly as it appears in Business Central.

### Step 5 — Wire it into your AI client

#### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac):

```json
{
  "mcpServers": {
    "business-central": {
      "command": "python",
      "args": [
        "-m", "bc_mcp_proxy",
        "--TenantId", "<your-tenant-id>",
        "--ClientId", "<your-client-id>",
        "--Environment", "Production",
        "--Company", "CRONUS USA",
        "--ConfigurationName", "Default MCP"
      ]
    }
  }
}
```

Restart Claude Desktop. Your BC tools are now available in every chat.

#### VS Code / Cursor

Both support MCP via a similar JSON config. The repository ships a `python -m bc_mcp_proxy setup` wizard that generates ready-to-paste install links for Cursor and VS Code, plus a Claude Desktop snippet:

```bash
python -m bc_mcp_proxy setup
```

### Step 6 — First sign-in

**Recommended: pre-authenticate once from a terminal** so your very first
Claude Desktop launch already has a token and tools cached:

```bash
python -m bc_mcp_proxy setup
```

The wizard signs you in (browser opens to the normal Microsoft sign-in — no
code to copy, nothing to read from logs) and caches the token + tool list to
disk. After that, **every** Claude Desktop launch is instant and fully
non-interactive until token expiry (refreshed silently via the refresh token).
This is the smoothest path and the one we recommend documenting to end users.

**Without the pre-auth step**, the first launch inside Claude Desktop still
works, just less smoothly. The proxy opens the browser for sign-in and, so it
doesn't block, returns an empty tool list immediately while you authenticate
(no more 30-second hang). It then sends `notifications/tools/list_changed`.
**Caveat:** current Claude Desktop does *not* re-request the tool list when it
receives that notification over stdio, so on this very first run the tools
won't appear until you either ran the `setup` step above, or toggle the
extension off/on once after signing in. This is a one-time step — from the
second launch onward the disk cache makes the tool list appear on the first
call with no reconnect. (Other MCP clients such as Cursor/VS Code may honour
the notification and refresh automatically; behaviour is client-specific.)

If no browser is available (headless box, locked-down VM) the proxy
automatically falls back to the **device-code flow**:

```
To sign in, use a web browser to open https://microsoft.com/devicelogin
and enter the code ABCD-1234 to authenticate.
```

You can force a specific method with `--AuthMode` / `BC_AUTH_MODE`
(`auto` — default, interactive then device-code fallback; `interactive`;
`device_code`). Use `device_code` for headless/server installs where opening a
browser on the host is undesirable.

---

## First test: ask your AI

Restart your AI client and try:

- *"Show me the top 5 customers in CRONUS USA."*
- *"Which vendors have an outstanding balance over €5,000?"*
- *"List Sales Invoices from last month with status Open."*

In **static mode** the AI picks the tool directly (e.g. `ListCustomers_PAG30009`) with parameters (`top: 5`).

In **dynamic mode** you'll see three steps — `bc_actions_search` → `bc_actions_describe` → `bc_actions_invoke`.

Both modes return the same data. Which one to choose depends on how big your BC installation is and how much flexibility you want to give the AI.

---

## Example prompts

These work against a standard CRONUS demo company with the default read-only MCP access (no configuration needed):

1. *"List my customers and show the five with the highest balance due."*
2. *"Which items are below their reorder point? Include inventory and vendor."*
3. *"Show the open sales orders for customer 10000 with their amounts and shipment dates."*
4. *"What were total posted sales invoices per month this year?"*

With an MCP configuration that has **Unblock Edit Tools** on, writes work too — Claude asks for confirmation before each one because the proxy marks those tools as destructive:

5. *"Create a new customer called Acme Trading in Antwerp with payment terms 30 days."*

### Known limitations

- The first request after Business Central has been idle can take 30–60 s server-side; the proxy answers `tools/list` from cache meanwhile, but the first *data* call waits for BC.
- In dynamic tool mode `bc_actions_invoke` performs reads and writes through one tool, so Claude will ask for confirmation on every call to it. Use static mode (explicit `List…`/`Create…` tools) if you want reads to run without prompts.
- Business Central online throttles per user (6000 requests / 5 min, 5 concurrent). The proxy backs off and retries, so bulk questions may take longer rather than fail.
- Only Business Central **online** is supported; on-premises has no MCP server.

## Static vs Dynamic Tool Mode

| | **Static** | **Dynamic** |
|---|---|---|
| **Tools the AI sees** | One per selected page (e.g. 10 tools) | Three generic tools |
| **First-call performance** | Fast (sub-second) | Slow on first call (50–60s when *Discover Additional Objects* is on, while BC walks the catalog) |
| **Subsequent calls** | Fast | Fast — the catalog is cached |
| **AI prompt cost** | Higher (the AI sees all tool schemas) | Lower (only three meta-tools) |
| **Best for** | A bounded set of use cases you've explicitly chosen to expose | Flexible exploration, especially with many pages |

Our recommendation: start with **static mode** for the first test (fast feedback, you know exactly which tools exist). Switch to **dynamic** once your users start asking things you didn't preselect a tool for.

---

## BC version compatibility

Microsoft changed the MCP endpoint shape in BC v28 and now documents a single host for every MCP client. Version 29 (2026 release wave 2) keeps the same endpoint, headers and OAuth scope and adds new tools (custom data queries, report-inbox automation). The proxy detects the host you point it at and adapts everything — URL shape, request headers, **and OAuth scope** — automatically:

| BC version | `BC_BASE_URL`                                  | URL shape                             | Routing info                                  | OAuth scope                                                 |
|------------|------------------------------------------------|---------------------------------------|-----------------------------------------------|--------------------------------------------------------------|
| 28 / 29 (default) | `https://mcp.businesscentral.dynamics.com` | bare host, no path                    | `TenantId` + `EnvironmentName` headers (plus `Company`, `ConfigurationName`; non-ASCII values Base64-encoded per SEP-2243) | `https://mcp.businesscentral.dynamics.com/.default`         |
| 26 / 27 (legacy) | `https://api.businesscentral.dynamics.com` | `/v2.0/{environment}/mcp` is appended | `Company`, `ConfigurationName` headers        | `https://api.businesscentral.dynamics.com/.default`         |

Only the legacy `api.` host gets the legacy shape; any other `*.businesscentral.dynamics.com` host (a future regional subdomain, for instance) is treated as the modern header-routed endpoint. Switching versions is a single-line change in `.env` or `--BaseUrl` — the scope and headers follow automatically. Set `BC_TOKEN_SCOPE` only if you need to override the auto-pick.

### What the proxy adds on top of BC's tools

- **Tool annotations.** BC's tools arrive without `title`, `readOnlyHint` or `destructiveHint`. The proxy fills those in from Microsoft's documented naming — `bc_actions_search`/`bc_actions_describe` and `List…_PAG…` are read-only; `bc_actions_invoke`, `Create…`, `ListUpdate…`, `Delete…` and bound actions are destructive — so Claude can run reads without a per-call confirmation and always asks before a write. Anything BC does set is kept verbatim. Note that `bc_actions_invoke` (dynamic tool mode) executes reads *and* writes through one tool, so it is marked destructive. Disable with `BC_ANNOTATE_TOOLS=0` / `--NoAnnotateTools`.
- **Resources and prompts.** From v28 the BC MCP server can hand back large results as embedded resources / file references, and v29 adds more. The proxy forwards `resources/*` and `prompts/*` alongside `tools/*` whenever BC advertises them at connect time (empty lists otherwise, never a blocking call). Disable with `BC_FORWARD_RESOURCES_PROMPTS=0` / `--NoForwardResourcesPrompts`.
- **Permission denials, explained.** Business Central refuses a call the signed-in user may not make with a generic `Internal_ServerError` whose message names the object and the missing permission. The proxy recognises that payload, appends a `[bc-mcp-proxy]` note saying this is a Business Central permission decision (not a proxy, configuration or sign-in problem) and marks the result as an error, so the AI client explains instead of retrying. Always on. See [Security model](https://vangeldersolutions.github.io/bc-mcp-proxy/security-model/).
- **Hide tools the user may not use** (`BC_HIDE_UNAUTHORIZED_TOOLS=1` / `--HideUnauthorizedTools`, off by default). Static tool mode only: after each connect the proxy reads one record from every `List…_PAG…` tool and hides the tools for pages Business Central refuses, together with their `Create…`/`ListUpdate…`/`Delete…`/bound-action siblings, then pushes `tools/list_changed`. Timeouts, rate limits and structural errors never hide anything, a live denial hides its page immediately, and Business Central still enforces every call. It costs one call per API page per connect (visible in `RT0054` telemetry), so leave it off on large configurations unless the tool list confuses users. With the companion app **MCP Guard** by Vangelder Solutions (free on AppSource) installed and its `effectivePermissions` page in the MCP configuration, the proxy makes one call instead and also hides `Create…`/`ListUpdate…`/`Delete…` tools individually where the user lacks that table permission; pages that read a second table in code are still caught by the live denial.
- **Rate limits.** Business Central online throttles per user (6000 requests per 5 minutes, 5 concurrent). A `429`, `503`, `408` or `504` is retried with exponential backoff, honouring `Retry-After` up to the 16 s cap, instead of ending the proxy process.
- **Telemetry name.** Every request carries `X-Client-Application: vgs-bc-mcp/<version>`, which BC telemetry records as `clientName` on event `RT0054`, so proxy traffic is easy to isolate in Application Insights.

### Cold-start mitigation

BC's MCP endpoint can take 30s+ to answer the very first `tools/list` call after the environment has been idle, which is longer than Claude Desktop's hardcoded MCP request timeout. The proxy masks this with a three-tier cache:

1. On startup, load the previously cached tools list from disk (per tenant/environment/company/configuration).
2. After the upstream session connects, eagerly pre-warm `tools/list` and refresh both the in-memory and disk caches.
3. The stdio handler answers from the in-memory cache (5-minute TTL) instead of round-tripping to BC on every call.

The very first install on a freshly cold-started BC environment may still hit the 30s timeout once — there is no disk cache yet to fall back on. Every subsequent launch is instant.

---

## Configuration parameters

| Parameter           | CLI argument           | Env var                  | Default                                                       |
|---------------------|------------------------|--------------------------|---------------------------------------------------------------|
| Tenant ID           | `--TenantId`           | `BC_TENANT_ID`           | *required*                                                    |
| Client ID           | `--ClientId`           | `BC_CLIENT_ID`           | *required*                                                    |
| Environment         | `--Environment`        | `BC_ENVIRONMENT`         | `Production`                                                  |
| Company             | `--Company`            | `BC_COMPANY`             | *required*                                                    |
| Configuration Name  | `--ConfigurationName`  | `BC_CONFIGURATION_NAME`  | unset                                                         |
| Custom Auth Header  | `--CustomAuthHeader`   | `BC_CUSTOM_AUTH_HEADER`  | unset (skips interactive/device flow when provided)           |
| Auth Mode           | `--AuthMode`           | `BC_AUTH_MODE`           | `auto` (`auto` \| `interactive` \| `device_code`)             |
| Base URL            | `--BaseUrl`            | `BC_BASE_URL`            | `https://mcp.businesscentral.dynamics.com` (v28 / v29)        |
| Token Scope         | `--TokenScope`         | `BC_TOKEN_SCOPE`         | auto-picked from base URL host (legacy `api.` host → legacy scope, anything else → modern scope) |
| Annotate Tools      | `--AnnotateTools` / `--NoAnnotateTools` | `BC_ANNOTATE_TOOLS` | on — adds `title`/`readOnlyHint`/`destructiveHint` where BC omits them |
| Forward Resources/Prompts | `--ForwardResourcesPrompts` / `--NoForwardResourcesPrompts` | `BC_FORWARD_RESOURCES_PROMPTS` | on — forwards `resources/*` and `prompts/*` when BC advertises them |
| Hide Unauthorized Tools | `--HideUnauthorizedTools` / `--NoHideUnauthorizedTools` | `BC_HIDE_UNAUTHORIZED_TOOLS` | off — static mode: probe each List tool after connect and hide pages the user cannot read |
| HTTP Timeout (s)    | `--HttpTimeoutSeconds` | `BC_HTTP_TIMEOUT_SECONDS`| `120.0`                                                       |
| SSE Timeout (s)     | `--SseTimeoutSeconds`  | `BC_SSE_TIMEOUT_SECONDS` | `300.0`                                                       |
| Log Level           | `--LogLevel`           | `BC_LOG_LEVEL`           | `INFO`                                                        |
| Debug               | `--Debug`              | `BC_DEBUG=1`             | off                                                           |

Token cache locations (when no custom auth header is supplied):

- **Windows**: `%LOCALAPPDATA%\BcMCPProxyPython\bc_mcp_proxy.bin`
- **macOS**: `~/Library/Caches/BcMCPProxyPython/bc_mcp_proxy.bin`
- **Linux**: `$XDG_CACHE_HOME/BcMCPProxyPython/bc_mcp_proxy.bin` (or `~/.cache/…`)

---

## Troubleshooting

- **`HTTP 400`/`404` from `mcp.businesscentral.dynamics.com` (or `Session terminated` at connect).** Sign-in worked; BC is rejecting the request *routing*, not the token. Almost always: a wrong **Environment name** (exact, case-sensitive — typically a `404`), a missing/incorrect **MCP Configuration Name** (required when a named MCP configuration exists — typically a `400`; easy to leave blank in the extension settings since it's not shown in Claude Desktop's args log), a wrong **Company**, or the signed-in account lacking access to that environment. The proxy surfaces this as a single actionable line in the log (echoing your effective Environment/Company/ConfigurationName) and **stays alive** instead of crash-looping with a Python traceback — fix the field and restart.
- **`The MCP Configuration named X was not found or not active`.** Open the configuration in BC and verify the **Active** toggle is on. Saving the page does not flip Active automatically. The error also fires when the `ConfigurationName` header value differs from the BC record by even a trailing space.
- **Authentication failures.** `AADSTS500113` / `AADSTS50011` (no reply address / redirect URI mismatch) means the **`http://localhost` redirect URI is not registered** under *Authentication → Mobile and desktop applications* — add it (see [Step 1](#step-1--azure-app-registration)). The proxy names this exact fix in its error message and falls back to device-code in `auto` mode. Otherwise verify *"Allow public client flows"* is **Yes**, the `ms-appx-web://Microsoft.AAD.BrokerPlugin/<clientID>` redirect URI is present, all API permissions are granted (and admin-consented where required), and rerun setup if device-code times out. For headless/server hosts where no browser can open, set `--AuthMode device_code` (or `BC_AUTH_MODE=device_code`).
- **Calls hang or time out (especially in Dynamic Tool Mode).** The first `bc_actions_search` against a configuration with *Discover Additional Objects* enabled enumerates the entire metadata catalog — measured at 50–60s server-side on a Cronus demo. Raise `BC_HTTP_TIMEOUT_SECONDS` (default 120) if you see `httpx.ReadTimeout` on the first call. Subsequent calls within the same session are typically sub-second.
- **JSON-RPC `-32603 "An error occurred."` with no detail.** This is BC's catch-all when something inside a dynamic-tool call goes wrong. The actual reason is logged to Azure Application Insights as event `RT0054` with custom dimension `toolInvocationFailureReason`. Enable telemetry on the BC environment and query (`traces | where customDimensions.eventId == 'RT0054' | where customDimensions.toolInvocationResult == 'Failure'`) to see what BC actually rejected.
- **Frequent reconnects in logs.** Inspect upstream availability — the proxy logs `Upstream connection error (...); reconnecting in Xs (attempt N/M)` whenever it retries. After the configured budget the proxy gives up and the local stdio pipe closes.
- **Repeated sign-in prompts.** The MSAL token cache may not be writable. Pass `--DeviceCacheLocation` to point at a directory you control.
- **`AADSTS90002: Tenant '…' not found` / `Tenant ID … is not a valid GUID`.** The tenant or client ID lost a character when it was pasted (a 35-character value is the classic symptom). Since 0.8.1 the proxy checks both IDs at startup and names the problem; re-copy the value from the Entra admin center (*App registrations → Overview*).
- **`spawn python3 ENOENT` in the Claude Desktop log (Windows).** Claude Desktop launches the extension as `python3`, so a `python3` command must resolve on `PATH`. A regular python.org install only provides `python.exe`; the Microsoft Store Python (or its *App execution alias* for `python3`, Settings → Apps → Advanced app settings → App execution aliases) provides `python3.exe`. Any Python 3.10–3.14 works; the bundle ships wheels for each.
- **`No module named bc_mcp_proxy`.** Install the distribution into the same Python interpreter your MCP client is configured to launch (`python -m pip install --upgrade vgs-bc-mcp`). If this appeared right after you uninstalled or upgraded the old `360solutions-bc-mcp` package, that uninstall removed the shared `bc_mcp_proxy` files — restore them with `python -m pip install --force-reinstall vgs-bc-mcp`.

---

## Why this fork?

Three issues were reproducible against the upstream `BcMCPProxyPython` sample in production-style use:

1. **Process crash on `httpx.ReadTimeout`.** The upstream `streamablehttp_client` is wrapped in an anyio task group; an unhandled timeout bubbles out as a `BaseExceptionGroup` and tears down the entire proxy, killing the stdio pipe to the MCP client. This fork wraps the upstream connection in a manager that reconnects with exponential backoff while keeping the local stdio server alive.

2. **No silent token refresh.** Access tokens are valid for ~60 minutes. Once expired, every `bc_actions_invoke` call returns `Authentication_InvalidCredentials` until the proxy is restarted. This fork tracks each token's `expires_in` and refreshes pre-emptively when remaining validity drops below `token_refresh_skew_seconds`.

3. **Masked errors.** Several upstream responses set `isError: false` even when the content is an error message — for example *"Semantic search is not enabled for this environment"*. Clients display those as normal output and the user has no idea why their query "didn't work." This fork inspects responses for known error patterns and re-flags them as MCP errors.

The CLI surface and on-disk configuration layout are unchanged so this is a drop-in replacement.

---

## Claude Desktop Extension

A `.mcpb` bundle (MCP Bundle) for one-click install in Claude Desktop is built from the source in [`dxt/`](dxt/README.md):

```bash
pwsh dxt/build.ps1     # Windows
./dxt/build.sh         # macOS / Linux
```

The output (`dist/vgs-bc-mcp-<version>-<platform>.mcpb`) installs into Claude Desktop, prompts for tenant ID / client ID / environment / company / configuration name, and runs the same proxy as the CLI version.

**Self-contained bundle.** The build script vendors all Python dependencies (`mcp`, `httpx`, `msal`, plus the security-floor pins) into the bundle as wheels for Python 3.10 through 3.14 matching the host platform. Claude Desktop launches the proxy with the system `python3` and the bundled deps take precedence over anything in the system's site-packages, so no separate `pip install` is required on the install side. Each platform has its own bundle:

| Platform | Filename pattern |
|---|---|
| Windows 64-bit | `vgs-bc-mcp-<version>-win-amd64.mcpb` |
| macOS Apple Silicon | `vgs-bc-mcp-<version>-darwin-arm64.mcpb` |
| Linux x86_64 | `vgs-bc-mcp-<version>-linux-x86_64.mcpb` |

Pre-built bundles for these three platforms are attached to every GitHub Release. Intel macOS (`darwin-x86_64`) isn't in the official matrix; Intel Mac users can run `./dxt/build.sh` on their own machine to produce a matching bundle. Building on each target platform's host (a Windows machine for the Windows bundle, etc.) is what the release CI does — wheels for `cryptography` are platform-specific.

---

## Security

> **Does MCP respect Business Central permissions?** Yes: Microsoft's MCP server runs every call under the signed-in user's identity, so permission sets, security filters, licence entitlements and the MCP configuration all apply unchanged. The proxy adds a clear note on every permission denial and, optionally, hides the tools the user cannot use. Read the full write-up with Microsoft's sources and our measurements: [Security model](https://vangeldersolutions.github.io/bc-mcp-proxy/security-model/) (source: [`docs/security-model.md`](docs/security-model.md)).

- **No application secrets.** Delegated permissions only via the device-code flow. No client secret to manage or rotate.
- **Tokens cached locally** via `msal-extensions` with OS-specific secure storage (DPAPI on Windows, Keychain on macOS, libsecret on Linux). No plaintext on disk.
- **No tokens in logs.** The proxy never logs access or refresh tokens; debug output contains only expiry timestamps for diagnosis.
- **Permissions are delegated.** Whatever the proxy can see and do, the signed-in user could already do manually in BC. The AI gets no extra rights.
- **Configuration name as gate.** In BC you decide per MCP Configuration which pages and objects are exposed. Put sensitive data behind a separate configuration that you only flip Active for specific users.

---

## Privacy Policy

The privacy policy for this software and the Claude Desktop extension is published at **https://vangeldersolutions.github.io/bc-mcp-proxy/privacy/** (source: [`docs/privacy.md`](docs/privacy.md)). In short: the proxy runs on your own device, talks only to Microsoft Entra ID and your Business Central environment, keeps tokens (OS-encrypted) and a tools cache locally, collects no telemetry, and sends nothing to Vangelder Solutions.

### Anthropic subscription

When you use this proxy, your BC queries and the data returned in response are processed by whichever AI provider your MCP client is wired to — most commonly Anthropic's Claude. **The subscription tier you pick (Team / Enterprise / API vs Free / Pro / Max) materially changes how that data is retained and whether it can be used for model training**, and it determines whether a Data Processing Addendum is available — which matters for GDPR if you are established in the EU/EEA.

For production use against a live BC tenant we recommend Claude Team, Claude Enterprise, or Anthropic API access — see [`NOTICE.md`](NOTICE.md) for the full recommendation, the responsibility split, and the trademark notice.

---

## Development

```bash
git clone https://github.com/VangelderSolutions/bc-mcp-proxy.git
cd bc-mcp-proxy
python -m pip install -e ".[test]"
python -m pytest
```

---

## Need help?

The Azure App Registration and the right permissions take attention to detail. For customers who would rather not deal with `Manifest.json`, redirect URIs and delegated permissions themselves, **Vangelder Solutions** offers an **end-to-end MCP setup package**:

- Azure App Registration created and validated in your tenant
- BC MCP Configuration created on the right environment(s)
- Proxy plus AI client (Claude Desktop, VS Code, Cursor) installed and tested on your workstation
- Short user training: which questions work well, which don't, what the privacy and cost trade-offs look like
- Optional: Application Insights telemetry hookup so you can see who uses which tools later

One appointment (online or on-site), configuration done, MCP working in your production environment.

📧 **support@vangeldersolutions.be**
🌐 [www.vangeldersolutions.be](https://www.vangeldersolutions.be)
📦 [github.com/VangelderSolutions/bc-mcp-proxy](https://github.com/VangelderSolutions/bc-mcp-proxy)

---

## Sources

- [`vgs-bc-mcp` on GitHub](https://github.com/VangelderSolutions/bc-mcp-proxy) — this fork, MIT-licensed, maintained by Vangelder Solutions
- [`microsoft/BCTech BcMCPProxyPython`](https://github.com/microsoft/BCTech/tree/master/samples/BcMCPProxyPython) — Microsoft's reference implementation
- [Configure Business Central MCP Server](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/ai/configure-mcp-server) — Microsoft Learn
- [Analyze MCP Server Tool Calls Telemetry](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/administration/telemetry-mcp-server-trace) — RT0054 event reference
- [Model Context Protocol specification](https://modelcontextprotocol.io) — Anthropic
- [MCP Bundles (MCPB)](https://github.com/modelcontextprotocol/mcpb) — the one-click install format for Claude Desktop
- [Submitting to the Connectors Directory](https://claude.com/docs/connectors/building/submission) — Anthropic's review criteria and desktop-extension submission form
- [Connect to Business Central MCP server with non-Microsoft hosts](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/ai/use-mcp-server-non-microsoft) — Microsoft Learn (headers, Base64 encoding, app registration)

---

## License

MIT — see [`LICENSE`](LICENSE).

Original work © Microsoft Corporation. Modifications © 2026 Vangelder Solutions.
