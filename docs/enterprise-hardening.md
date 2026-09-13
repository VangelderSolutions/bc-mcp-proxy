---
title: Enterprise hardening
permalink: /enterprise-hardening/
---

# Enterprise hardening: what users can change, and what actually stops them

**Short version.** Everything a user can change in the extension settings (environment, company, MCP configuration, the tool-hiding and company-switching options) stays inside that user's own Business Central permissions. A determined user can also edit the extension's local files or skip `vgs-bc-mcp` entirely and talk to the Business Central MCP server with another MCP client. So the lock has to sit where the user cannot reach it: in Business Central, in Microsoft Entra ID, or in a gateway the administrator hosts. This page lists what works today, what we measured, and what does not work.

Last verified: 13 September 2026 against Business Central 28.0 (2026 release wave 1).

## What a creative user can do, and what happens

| Change in the extension settings | Effect |
|---|---|
| Another **company** | Permission sets are assigned per company. A company the user has no rights in refuses every call (measured: `Page 30009 APIV2 - Customers Execute`). |
| Another **environment** | Only works if the user may sign in to that environment (Entra security group on the environment, licence). |
| Turn **tool hiding** off, or **company switching** on | Cosmetic. Business Central still decides every call. |
| Another **MCP configuration** | **This is the real gap.** Every active configuration can be used by every user who knows its name. A configuration with write tools meant for another team becomes available, limited only by the user's permission sets. |
| Another **client ID** | Works only if that app registration is consented in the tenant and the user is allowed to use it. |
| Edit the extension files, or use another MCP client | Removes any client-side restriction. Only server-side controls remain. |

The identity Business Central sees is the **Microsoft Entra sign-in**, not the Claude account. `vgs-bc-mcp` keeps that sign-in in a token cache in the user's operating-system profile. Claude Desktop and Claude Code under the same Windows or macOS profile share it, and a user can sign in with any Entra account they have credentials for.

## Measured on Business Central 28

Test user with standard permission sets in one company, a composite tenant permission set built for the test, restored afterwards.

| Experiment | Result |
|---|---|
| Exclude **Read on table 2000000292 MCP Configuration** from the user's permissions | No effect. The user still connects with any configuration name and calls its tools. Business Central reads MCP configurations with system rights, so **a configuration cannot be restricted per user with permission sets or security filters.** |
| Exclude **Execute on API page 30008 APIV2 - Items** | Effective immediately. In a named configuration the call is refused with `Sorry, the current permissions prevented the action.`; in the default configuration the tool is not even offered (`The tool List_Items_PAG30008 was not found on this MCP server.`). The Item List in the web client is a different page and is not affected. |
| Connect with a configuration name that does not exist | `400 Bad Request` at connect, the same for every user. |

Every standard permission set that lets a user log in (`LOGIN`, `D365 BASIC`, `D365 BUS FULL ACCESS` and others) already includes read access to the MCP configuration tables, which is consistent with the platform not relying on it.

## Controls, strongest first

### 1. Business Central

1. **Permission sets per company are the boundary.** Give write permissions only to users who may write. A configuration with write tools is harmless for a user without Insert, Modify or Delete on the underlying tables.
2. **Exclude API pages for users who must not reach certain data through AI or the API.** A tenant permission set with *Exclude* lines on the API pages (for example 30008 Items, 30009 Customers, 30017 Employees), assigned alongside the user's normal sets, removes those pages from MCP while the web client keeps working. This also blocks the same pages for Excel, Power BI and integrations that use the standard API as that user.
3. **Treat every active MCP configuration as available to everyone.** Keep configurations with *Unblock Edit Tools* turned on inactive unless they are in use, and do not rely on nobody knowing the name. Keep the default configuration read-only.
4. **Put record-level rules on the tables, not on the configuration.** Security filters (salesperson, location) apply through MCP exactly as in the client; see the [security model](../security-model/).

### 2. Microsoft Entra ID

1. **Restrict the environment** to an Entra security group in the Business Central admin center (internal administrators are exempt).
2. **Assignment required** on the enterprise application of the app registration used for MCP, with only the users or groups that may use AI clients.
3. **Turn off user consent and user app registration** in the tenant, so a user cannot register and consent to a private client ID.
4. **Conditional Access** on the target resource *Dynamics 365 Business Central* (compliant device, MFA, location). This also covers Microsoft's preregistered MCP clients such as Visual Studio Code and Copilot Studio, which need no app registration.

### 3. Claude Team and Enterprise

- **Desktop extension allowlist** (organization Owners): once on, members can only install extensions from the organization's list and can no longer drag or click to install `.mcpb` files. Upload your own build of `vgs-bc-mcp` as a custom extension.
- **Device policy** for Claude Desktop through MDM (`com.anthropic.claudefordesktop`): `isDesktopExtensionEnabled`, `isDesktopExtensionDirectoryEnabled`, `isLocalDevMcpEnabled`.
- Anthropic states that the allowlist does not protect against changes to local extension files after installation. Treat it as control over *which* software users run, not as data protection.

### 4. Detection

Business Central logs every MCP tool call as telemetry event `RT0054` with the user, `companyName`, `configurationName`, `clientName` (`vgs-bc-mcp/<version>` for this proxy) and `authAppId`. Ready-to-use Application Insights queries are in [`docs/telemetry/mcp-alerts.kql`](https://github.com/VangelderSolutions/bc-mcp-proxy/blob/main/docs/telemetry/mcp-alerts.kql): configurations a user is not expected to use, unexpected companies, clients other than the approved proxy, unknown app registrations, and bursts of refused calls.

## What vgs-bc-mcp will add

- **Managed policy (planned, off by default).** An administrator-deployed policy on the device (Windows registry under `HKLM\SOFTWARE\Policies`, a macOS configuration profile, or `/etc` on Linux) that pins the environment, the allowed companies and the configuration, and can require the Entra sign-in to match the Windows user. It stops casual changes in the settings screen; like every client-side control it is not a security boundary.
- **Hosted gateway (under investigation).** The pattern Atlassian and other vendors use for remote MCP servers: the proxy runs in the customer's own Azure subscription, the administrator decides per Entra group which environment, companies and configuration apply, and Claude Team or Enterprise Owners add it as an organization connector. Members can only connect with their own account and cannot change anything. This is the only option that also closes the configuration gap above.

## Sources

- [Configure Business Central MCP Server](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/ai/configure-mcp-server)
- [Manage access to environments](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/administration/tenant-admin-center-manage-access)
- [Access controls for Dynamics 365 Business Central](https://learn.microsoft.com/azure/azure-sovereign-clouds/public/access-controls-d365-business-central)
- [Analyze MCP server tool calls telemetry](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/administration/telemetry-mcp-server-trace)
- [Restrict a Microsoft Entra app to a set of users](https://learn.microsoft.com/entra/identity-platform/howto-restrict-your-app-to-a-set-of-users)
- [Enabling and using the desktop extension allowlist](https://support.claude.com/en/articles/12592343-enabling-and-using-the-desktop-extension-allowlist)
- [Enterprise configuration for Claude Desktop](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop)
- [Control Atlassian Rovo MCP server settings](https://support.atlassian.com/security-and-access-policies/docs/control-atlassian-rovo-mcp-server-settings/)
