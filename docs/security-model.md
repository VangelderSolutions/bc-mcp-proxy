---
title: Security model
permalink: /security-model/
---

# Security model: MCP respects Business Central permissions

**Short version.** Microsoft's Business Central MCP server runs every tool call under the identity of the signed-in user. Whatever a user cannot see or do in the Business Central client, they cannot see or do through Claude either. `vgs-bc-mcp` adds nothing that could widen that; it adds clarity (a permission denial is reported as such) and, optionally, tidiness (tools the user cannot use are not shown). This page lists the enforcement layers with Microsoft's own documentation, the measurements we ran to confirm them, and what remains for the administrator to decide.

Last verified: September 2026 against Business Central 28.0 (2026 release wave 1). Version 29 (2026 release wave 2) keeps the same model.

## The layers Business Central enforces

| Layer | What it controls | Where it is configured | Source |
|---|---|---|---|
| **Identity** | Who can obtain a token for the MCP server at all | Microsoft Entra app registration: *Assignment required = Yes* and assigned users/groups; Conditional Access (MFA, device, location); optionally an Entra security group on the environment | [Restrict an app to a set of users](https://learn.microsoft.com/entra/identity-platform/howto-restrict-your-app-to-a-set-of-users), [Manage access to environments](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/administration/tenant-admin-center-manage-access) |
| **Licence** | The ceiling of what any user can do (a Team Member licence caps even SUPER) | Business Central licence entitlements | [Access controls for Business Central](https://learn.microsoft.com/azure/azure-sovereign-clouds/public/access-controls-d365-business-central) |
| **Object** | Read/Insert/Modify/Delete on each table, Execute on each page and codeunit | Permission sets assigned to the user (directly or via security group) | [Assign permissions to users and groups](https://learn.microsoft.com/dynamics365/business-central/ui-define-granular-permissions) |
| **Record** | Which rows of a table the user may see | Security filters on the permission set's table permission (for example `Salesperson Code = AH`) | [Using security filters](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/security/security-filters) |
| **Company** | Which company a session works in | The `Company` header on the MCP session (one company per session; `vgs-bc-mcp` binds an installation to one company unless *Allow switching company* is on). Permission sets are assigned per company, so this is a scoping choice, not the access control itself | [Assign permissions per company](https://learn.microsoft.com/dynamics365/business-central/ui-define-granular-permissions) |
| **Exposure** | Which API pages and which operations exist as MCP tools | MCP Server Configuration: Available Tools, Allow Read/Create/Modify/Delete/Bound Actions, *Unblock Edit Tools*, *Dynamic Tool Mode*, *Discover Additional Objects* | [Configure Business Central MCP Server](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/ai/configure-mcp-server) |

The MCP server sits *after* all of them. Microsoft states it directly: "All operations are performed with your user identity and permissions, ensuring audit trails show who performed each action" ([MCP overview](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/ai/mcp-overview)) and "you can only create a customer if you have Create permission on the Customer API" ([Connect with Visual Studio Code](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/ai/use-mcp-server-in-vscode)).

Every call is also auditable: telemetry event `RT0054` records each MCP tool call with the user, the client (`clientName`, which this proxy sets to `vgs-bc-mcp/<version>`), the Entra app (`authAppId`) and `toolInvocationFailureReason`; event `RT0031` records permission errors shown to users. Changes to MCP configurations are written to the Microsoft Purview audit log. See [Analyze MCP server tool calls telemetry](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/administration/telemetry-mcp-server-trace).

## Least-privilege recipe

1. **One MCP Server Configuration per audience.** Give each role (sales, finance, warehouse) its own configuration containing only the API pages that role needs, and hand out the matching `ConfigurationName`. Users cannot switch configurations from the client without the name, and a name they do not have permission for is rejected at connect.
2. **Keep write access explicit.** Leave *Unblock Edit Tools* off unless a configuration is meant to write; turn on Allow Create/Modify/Delete per page, not globally.
3. **Use permission sets as the real gate.** The configuration decides what *could* be called; the user's permission sets decide what *is* allowed. A user without Read on `Customer` gets an error from `List_Customers_PAG30009` even if the page is in the configuration.
4. **Use security filters for row-level rules.** A salesperson-only view of customers and sales documents works through MCP exactly as it does in the client. Do not assign the same table in two permission sets to one user: the least restrictive filter wins.
5. **Restrict the Entra app.** Set *Assignment required = Yes* on the enterprise application of your MCP client app registration and assign the users or groups that may use AI clients. Add a Conditional Access policy if your tenant uses them.
6. **Watch `RT0054`.** Alert on `toolInvocationResult == 'Failure'` to catch users probing beyond their permissions, and on unexpected `authAppId` values to catch unregistered clients.

## What we measured

All runs on 12 September 2026 against a Business Central 28.0 sandbox (BE localisation, user language EN) with `scripts/probe_permission_errors.py`. The restricted user is a normal licensed user; the administrator (SUPER) is the control. Raw results, scrubbed, are in [`tests/fixtures/bc_permission_errors.json`](https://github.com/VangelderSolutions/bc-mcp-proxy/blob/main/tests/fixtures/bc_permission_errors.json).

**Static tool mode** (one MCP configuration exposing all 412 standard `List…_PAG…` tools, each called once with `top` set):

| User | Permission sets | Readable | Permission denials | Structural errors* |
|---|---|---|---|---|
| Administrator | SUPER | 129 | 0 | 283 |
| Restricted, no base rights | LOGIN, LOCAL, AUTOMATE - EXEC, EXCEL EXPORT ACTION, D365 ITEM, EDIT | 0 | 142 (`Page … Execute`) | 270 |
| Restricted, base rights | same plus D365 BASIC | 106 | 23 (`TableData … Read`) | 283 |
| Restricted, base rights with a security filter | same but D365 BASIC replaced by a tenant copy carrying `Customer No. = 10000`, without D365 ITEM, EDIT | 102 | 27 | 283 |

\* Sub-pages that need a parent key (`BadRequest_NotFound`, "Error in query syntax") and pages whose setup dialog fails (`Application_DialogException`). They fail before any permission check, identically for every user, and are not denials.

What the rows show:

1. **Without Execute on the API page nothing is readable.** Every one of the 142 pages that reached the permission check answered the same payload, `isError: true`, with the page named: `Sorry, the current permissions prevented the action. (Page 30009 APIV2 - Customers Execute: _Exclude_APIV2_)`.
2. **With base rights the table permission decides.** The 23 remaining denials are exactly the tables D365 BASIC does not grant Read on: Employees, Fixed Assets and their locations, G/L Entries, G/L Accounts, Bank Accounts, Aged AP/AR, Trial Balances, Projects, Opportunities, Customer Contacts and Customer Financial Details, Document Attachments, Sales Shipments, Purchase Receipts, Transfer Orders/Receipts/Shipments (with lines). The message names the table: `(TableData 5200 Employee Read: _Exclude_APIV2_)`. The four extra denials in the last row (Item Categories, Assembly Orders and lines, Transfer Order Lines) come from removing D365 ITEM, EDIT, not from the filter.
3. **A security filter is invisible, not an error.** With `Customer No. = 10000` on the Customer table permission, `List_Customers_PAG30009` returned 1 of the company's 7 customers, in static and in dynamic mode alike. The other 6 do not exist for that user. Sales orders, quotes and invoices of the other customers remained visible (23 orders): a filter on Customer does not cascade to Sales Header or to posted documents; those tables need their own filter (for example `Sell-to Customer No. = 10000`).
4. **Changes apply without a new sign-in.** The security filter and the permission set assignments made through the automation API were effective on the next MCP call, within minutes, on an already-authenticated MCP session.

**Dynamic tool mode** (`bc_actions_search`, `bc_actions_describe`, `bc_actions_invoke`): search and describe succeed for the restricted user regardless of permissions, so the catalogue does not leak data but does reveal which API pages exist. `bc_actions_invoke` on a forbidden page returns the same payload as the static tool (`TableData 5200 Employee Read`); on the filtered Customer page it returned the single permitted row.

**Shape of a denial**, as the MCP client sees it (keys are lower-case, unlike some other Business Central error payloads):

```json
{
  "error": {
    "code": "Internal_ServerError",
    "message": "Sorry, the current permissions prevented the action. (TableData 5200 Employee Read: _Exclude_APIV2_)"
  }
}
```

The HTTP status is 200 and the MCP result carries `isError: true`. The message is localised to the user's language; the code and the bracketed object reference are not. `vgs-bc-mcp` matches on the object reference.

Two limits we hit while measuring: the automation API's `expandedPermissionSets` does not expose security filters (the filtered Customer line reports `readPermission: Yes` and nothing else), so filters can only be verified in the client or by reading through the API as that user; and the Permission Set page only commits a security filter when you leave the line, so verify the value after closing the card.

## What the proxy adds (0.9.0)

- **A permission denial is named as such.** When Business Central refuses a call for lack of permission, the proxy appends a short note to the tool result so the AI client explains "you do not have Read on TableData Customer; ask your Business Central administrator" instead of retrying or blaming configuration. Always on.
- **Tools you cannot use are not shown** (optional, `BC_HIDE_UNAUTHORIZED_TOOLS=1`). In static tool mode the proxy reads one record from each listed page after connecting and hides every tool for pages that answer with a permission denial, then pushes `tools/list_changed`; a denial seen on a live call hides its page at once. Transient errors never hide anything. The probe costs one call per API page per connect and shows up in `RT0054` telemetry under the user. Business Central remains the enforcer: a hidden tool that is still called is still forwarded and still refused.
- **Nothing is cached across users.** The tool list cache is keyed by tenant, environment, company and configuration; a filtered list is never written to disk.

## What the proxy cannot do, and what a companion app could

- **Dynamic tool mode** exposes three system tools; the actions returned by `bc_actions_search` cannot be filtered client-side without calling each one. The denial note still applies when `bc_actions_invoke` is refused.
- **Field-level security** does not exist in Business Central: whoever may read an API page sees all its fields. The native answer is a narrower API page (or an API query) in a separate configuration.
- **Write permissions cannot be probed safely** (a probe would write). Only Read is verified by the proxy; write tools are hidden only when Read is denied for the same page.
- The companion app **MCP Guard** (Vangelder Solutions, free on AppSource) closes most of the gap: its `effectivePermissions` API page reports, per API page, the signed-in user's Read (`RecordRef.ReadPermission`), Insert/Modify/Delete and page Execute from the expanded permission sets, and whether a security filter applies. With the page in the MCP configuration, `vgs-bc-mcp` 0.9.1+ reads it once instead of probing and hides write tools per operation. Measured against the MCP server on the same restricted user (129 judgeable pages): agreement on 87; on 36 pages with an **empty** source table the guard reports "no Read" where the server returned an empty result without a permission check (the guard is right, a probe cannot tell); on 6 pages the guard is optimistic because the page reads a second table in code (Aged AR/AP, Trial Balance, Customer Financial Details, Document Attachments, Customer Contacts), which is why a live denial still hides a page.

## Sources

- [Model Context Protocol (MCP) in Business Central overview](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/ai/mcp-overview)
- [Configure Business Central MCP Server](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/ai/configure-mcp-server)
- [Connect to Business Central MCP server with non-Microsoft hosts](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/ai/use-mcp-server-non-microsoft)
- [Data security in Business Central](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/security/data-security)
- [Using security filters](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/security/security-filters)
- [Assign permissions to users and groups](https://learn.microsoft.com/dynamics365/business-central/ui-define-granular-permissions)
- [Analyze MCP server tool calls telemetry](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/administration/telemetry-mcp-server-trace)
- [Analyzing permission error trace telemetry](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/administration/telemetry-permission-error-trace)
- [Restrict a Microsoft Entra app to a set of users](https://learn.microsoft.com/entra/identity-platform/howto-restrict-your-app-to-a-set-of-users)
- [Troubleshooting REST API/OData calls (error codes)](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/webservices/dynamics-error-codes)
