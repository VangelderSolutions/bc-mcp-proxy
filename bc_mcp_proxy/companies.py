"""Working with several Business Central companies from one installation.

The Business Central MCP server takes the company as a `Company` header on
the HTTP session, and refuses a session without one (measured on BC 28.0).
So one upstream session serves exactly one company. To let a user who may
work in several companies reach them without changing the extension
settings, the proxy (when `allow_company_switch` is on):

* adds an optional `company` argument to every forwarded tool, and routes a
  call that carries one to an upstream session opened for that company;
* adds one proxy-native tool, `bc_list_companies`, that lists the companies
  the signed-in user can open (Business Central's standard `companies` API,
  called with the user's own token);
* never widens what the user may do: Business Central assigns permission
  sets per company and refuses calls in a company the user lacks rights in,
  which surfaces through the usual permission note.

Off by default so an installation stays bound to the configured company
unless an administrator decides otherwise. That is a convenience limit, not
a security boundary: any MCP client the user runs can send another header.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from .auth import TokenProvider
from .config import LEGACY_HOST, ProxyConfig

COMPANY_ARGUMENT = "company"
LIST_COMPANIES_TOOL = "bc_list_companies"
_COMPANY_ARGUMENT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": ("Business Central company to run this call in. Defaults to the company "
                    "configured for this connection. Use bc_list_companies to see the names."),
}
_STANDARD_API_HOST = f"https://{LEGACY_HOST}"


@dataclass(frozen=True)
class Company:
  name: str          # the value Business Central expects in the Company header
  display_name: str
  id: str


def companies_url(config: ProxyConfig) -> str:
  """The standard API endpoint listing the companies the user can open."""
  return f"{_STANDARD_API_HOST}/v2.0/{config.environment.strip()}/api/v2.0/companies"


def parse_companies(payload: Any) -> list[Company]:
  rows = payload.get("value") if isinstance(payload, dict) else None
  companies: list[Company] = []
  for row in rows or []:
    if not isinstance(row, dict) or not row.get("name"):
      continue
    companies.append(Company(name=str(row["name"]), display_name=str(row.get("displayName") or ""),
                             id=str(row.get("id") or "")))
  return companies


class CompanyDirectory:
  """The companies the signed-in user can open, fetched once and kept.

  A miss (an unknown name) triggers one refresh, so a company created after
  the proxy started is still found. When the API cannot be read (no token
  for the standard API, network), the directory is empty and validation is
  skipped: Business Central then decides at connect time."""

  def __init__(self, config: ProxyConfig, token_provider: Optional[TokenProvider],
               logger: Optional[logging.Logger] = None, timeout: float = 30.0) -> None:
    self._config = config
    self._token_provider = token_provider
    self._logger = logger or logging.getLogger("bc_mcp_proxy")
    self._timeout = timeout
    self._companies: Optional[list[Company]] = None
    self._lock = asyncio.Lock()

  @property
  def default_company(self) -> str:
    return (self._config.company or "").strip()

  async def companies(self, refresh: bool = False) -> list[Company]:
    async with self._lock:
      if self._companies is not None and not refresh:
        return list(self._companies)
      self._companies = await self._fetch()
      return list(self._companies)

  async def _fetch(self) -> list[Company]:
    if self._token_provider is None:
      return []
    try:
      token = await self._token_provider.get_token()
      async with httpx.AsyncClient(timeout=self._timeout) as client:
        response = await client.get(companies_url(self._config),
                                    headers={"Authorization": f"Bearer {token}"})
      response.raise_for_status()
      companies = parse_companies(response.json())
      self._logger.info("Company directory: %d company(ies) available to the signed-in user", len(companies))
      return companies
    except Exception as exc:  # noqa: BLE001 - the directory is best-effort
      self._logger.warning(
          "Could not list companies through the standard API (%s); company names "
          "will be validated by Business Central at connect time instead", type(exc).__name__)
      return []

  async def resolve(self, requested: str) -> tuple[Optional[str], list[Company]]:
    """Map a name Claude typed to the exact company name, or None if unknown.

    Matches the API name and the display name, case-insensitively. Returns
    the requested text unchanged when the directory is empty (unverifiable)."""
    wanted = requested.strip()
    companies = await self.companies()
    if not companies:
      return wanted, companies
    match = _match(wanted, companies)
    if match is None:
      companies = await self.companies(refresh=True)
      match = _match(wanted, companies)
    return (match.name if match else None), companies

  async def describe(self) -> str:
    companies = await self.companies()
    default = self.default_company
    if not companies:
      return (f"The company list could not be read from Business Central. The configured "
              f"company is '{default}'; other companies can be tried by name with the "
              f"'{COMPANY_ARGUMENT}' argument and Business Central will accept or refuse them.")
    lines = [f"Companies in environment '{self._config.environment}' available to the signed-in user "
             f"(pass the name as the '{COMPANY_ARGUMENT}' argument of any tool):"]
    for c in sorted(companies, key=lambda c: c.name.lower()):
      marker = " (default for this connection)" if c.name == default else ""
      shown = f"{c.name}" + (f" -- {c.display_name}" if c.display_name and c.display_name != c.name else "")
      lines.append(f"- {shown}{marker}")
    if default and not any(c.name == default for c in companies):
      lines.append(f"- {default} (default for this connection; not in the list above)")
    return "\n".join(lines)


def _match(wanted: str, companies: list[Company]) -> Optional[Company]:
  folded = wanted.casefold()
  for c in companies:
    if c.name.casefold() == folded:
      return c
  for c in companies:
    if c.display_name and c.display_name.casefold() == folded:
      return c
  return None


def list_companies_tool() -> Tool:
  return Tool(
      name=LIST_COMPANIES_TOOL,
      title="List Business Central companies",
      description=("List the Business Central companies the signed-in user can work in, and which "
                   f"one is the default for this connection. Pass a company name as the "
                   f"'{COMPANY_ARGUMENT}' argument of any other tool to run it there."),
      inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
      annotations=ToolAnnotations(title="List Business Central companies", readOnlyHint=True,
                                  destructiveHint=False, idempotentHint=True, openWorldHint=False),
  )


def add_company_switch(result: ListToolsResult) -> ListToolsResult:
  """Add the `company` argument to every forwarded tool and list the
  bc_list_companies tool. Idempotent; returns the same object when nothing
  changes (the cache tiers compare identity)."""
  tools = list(getattr(result, "tools", None) or [])
  if not tools:
    return result
  changed = False
  out: list[Tool] = []
  seen_list_tool = False
  for tool in tools:
    if tool.name == LIST_COMPANIES_TOOL:
      seen_list_tool = True
      out.append(tool)
      continue
    schema = dict(tool.inputSchema or {"type": "object"})
    props = dict(schema.get("properties") or {})
    if COMPANY_ARGUMENT in props:
      out.append(tool)
      continue
    props[COMPANY_ARGUMENT] = dict(_COMPANY_ARGUMENT_SCHEMA)
    schema["properties"] = props
    out.append(tool.model_copy(update={"inputSchema": schema}))
    changed = True
  if not seen_list_tool:
    out.append(list_companies_tool())
    changed = True
  return result.model_copy(update={"tools": out}) if changed else result


def pop_company(arguments: Optional[dict[str, Any]]) -> tuple[Optional[str], dict[str, Any]]:
  """Split the `company` argument off the arguments forwarded to BC."""
  args = dict(arguments or {})
  value = args.pop(COMPANY_ARGUMENT, None)
  if isinstance(value, str) and value.strip():
    return value.strip(), args
  return None, args


def company_error(message: str) -> CallToolResult:
  return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)
