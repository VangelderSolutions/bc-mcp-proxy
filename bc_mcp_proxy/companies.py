"""Working with several Business Central companies from one installation.

The Business Central MCP server takes the company as a `Company` header on
the HTTP session, and refuses a session without one (measured on BC 28.0).
So one upstream session serves exactly one company. To let a user who may
work in several companies reach them without changing the extension
settings, the proxy (when `allow_company_switch` is on):

* adds an optional `company` argument to every forwarded tool, and routes a
  call that carries one to an upstream session opened for that company;
* adds one proxy-native tool, `bc_list_companies`, that lists the companies
  of the environment (Business Central's standard `companies` API, called
  with the user's own token; it lists every company, permission or not);
* optionally limits both to `allowed_companies` (plus the configured
  company), so an administrator or an embedding package can narrow the
  choice without Business Central knowing about it;
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
ENVIRONMENT_ARGUMENT = "environment"
LIST_COMPANIES_TOOL = "bc_list_companies"
_COMPANY_ARGUMENT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": ("Business Central company to run this call in. Defaults to the company "
                    "configured for this connection. Use bc_list_companies to see the names."),
}
_ENVIRONMENT_ARGUMENT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": ("Business Central environment to run this call in. Defaults to the "
                    "environment configured for this connection. Use bc_list_companies to see "
                    "the environments and their companies. Each environment has its own data: "
                    "a company name can exist in several of them."),
}
_STANDARD_API_HOST = f"https://{LEGACY_HOST}"
# Without this, a client reads the limited list as the environment's full list
# and tells the user a company "does not exist" when it is only not allowed.
# The proxy does not know who limited the list: the installation's own
# setting, an administrator's rule, or an embedding package that keeps only
# the companies the user has permissions in. So the text names both causes
# (a client told users "an administrator limited the choice" when only their
# own permissions did).
LIMITED_REASON = "limited by its settings or by the signed-in user's permissions"
_LIMITED_NOTE = ("The environment may contain other companies; they are not available for this "
                 f"connection, which is {LIMITED_REASON}. Say that a company is not available "
                 "here rather than that it does not exist.")


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
  """The companies of the environment, fetched once and kept.

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
  def config(self) -> ProxyConfig:
    """The effective configuration of this environment: its company,
    configuration name and allowed companies."""
    return self._config

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
      self._logger.info("Company directory: %d company(ies) in the environment", len(companies))
      return companies
    except Exception as exc:  # noqa: BLE001 - the directory is best-effort
      self._logger.warning(
          "Could not list companies through the standard API (%s); company names "
          "will be validated by Business Central at connect time instead", type(exc).__name__)
      return []

  @property
  def allowed(self) -> Optional[tuple[str, ...]]:
    allowed = self._config.allowed_companies
    if allowed is None:
      return None
    return tuple(a.strip() for a in allowed if a and a.strip())

  def visible(self, companies: list[Company]) -> list[Company]:
    """The companies a call may be routed to: all of them, or those named in
    allowed_companies (by API or display name) plus the configured company."""
    allowed = self.allowed
    if allowed is None:
      return list(companies)
    folded = {a.casefold() for a in allowed}
    default = self.default_company.casefold()
    return [c for c in companies
            if c.name.casefold() in folded or c.name.casefold() == default
            or (c.display_name and c.display_name.casefold() in folded)]

  async def resolve(self, requested: str) -> tuple[Optional[str], list[Company]]:
    """Map a name Claude typed to the exact company name, or None if unknown
    or not allowed for this connection.

    Matches the API name and the display name, case-insensitively. Returns
    the requested text unchanged when the directory is empty (unverifiable),
    unless allowed_companies is set: then only names on that list pass."""
    wanted = requested.strip()
    companies = await self.companies()
    if not companies:
      allowed = self.allowed
      if allowed is None:
        return wanted, companies
      names = allowed + ((self.default_company,) if self.default_company else ())
      return next((n for n in names if n.casefold() == wanted.casefold()), None), companies
    match = _match(wanted, self.visible(companies))
    if match is None:
      companies = await self.companies(refresh=True)
      match = _match(wanted, self.visible(companies))
    return (match.name if match else None), self.visible(companies)

  async def describe(self) -> str:
    companies = self.visible(await self.companies())
    default = self.default_company
    if not companies and self.allowed is not None:
      names = [default] if default else []
      names += [a for a in self.allowed if a.casefold() != default.casefold()]
      lines = [f"The company list could not be read from Business Central. Companies available "
               f"for this connection (pass the name as the '{COMPANY_ARGUMENT}' argument):"]
      lines += [f"- {n}" + (" (default for this connection)" if n == default else "") for n in names]
      lines.append(_LIMITED_NOTE)
      return "\n".join(lines)
    if not companies:
      return (f"The company list could not be read from Business Central. The configured "
              f"company is '{default}'; other companies can be tried by name with the "
              f"'{COMPANY_ARGUMENT}' argument and Business Central will accept or refuse them.")
    scope = "Companies" if self.allowed is None else "Companies available for this connection"
    lines = [f"{scope} in environment '{self._config.environment}' (pass the company name, or its "
             f"display name, as the '{COMPANY_ARGUMENT}' argument of any tool; Business Central "
             f"decides per company whether the signed-in user may work in it):"]
    for c in sorted(companies, key=lambda c: c.name.lower()):
      notes = []
      if c.display_name and c.display_name != c.name:
        notes.append(f'display name "{c.display_name}"')
      if c.name == default:
        notes.append("default for this connection")
      lines.append(f"- {c.name}" + (f" ({'; '.join(notes)})" if notes else ""))
    if default and not any(c.name == default for c in companies):
      lines.append(f"- {default} (default for this connection; not in the list above)")
    if self.allowed is not None:
      lines.append(_LIMITED_NOTE)
    return "\n".join(lines)


class EnvironmentDirectory:
  """The environments this connection may reach, each with its own company
  directory, default company and configuration.

  Built by the embedding package through `ProxyConfig.environments`. The
  first environment is the default: the one the connection is configured
  for, whose session fills the tools cache."""

  def __init__(self, directories: dict[str, CompanyDirectory], default_environment: str) -> None:
    self._directories = directories
    self._default = default_environment.strip()

  @property
  def default_environment(self) -> str:
    return self._default

  @property
  def names(self) -> list[str]:
    return list(self._directories)

  def is_default(self, environment: str) -> bool:
    return environment.strip().casefold() == self._default.casefold()

  def resolve(self, requested: str) -> Optional[str]:
    """The exact environment name, case-insensitively, or None if this
    connection may not reach it. Business Central environment names are
    case-insensitive in the URL, but the header and our session keys are
    not, so a name is always mapped back to the configured spelling."""
    wanted = requested.strip().casefold()
    return next((name for name in self._directories if name.casefold() == wanted), None)

  def directory(self, environment: str) -> CompanyDirectory:
    return self._directories[environment]

  async def describe(self) -> str:
    """One text covering every environment: what bc_list_companies returns
    when the connection reaches more than one."""
    lines = [f"This connection reaches {len(self._directories)} Business Central environments. "
             f"Pass an environment name as the '{ENVIRONMENT_ARGUMENT}' argument of any tool, and "
             f"a company name as '{COMPANY_ARGUMENT}'; leaving both out uses environment "
             f"'{self._default}' and its default company. Each environment holds its own data."]
    for name, directory in self._directories.items():
      lines.append("")
      lines.append(f"Environment '{name}'" + (" (default for this connection)"
                                              if self.is_default(name) else "") + ":")
      lines.append(await directory.describe())
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


def list_companies_tool(environment_switch: bool = False) -> Tool:
  description = ("List the companies of the Business Central environment and which one is the "
                 f"default for this connection. Pass a company name as the '{COMPANY_ARGUMENT}' "
                 "argument of any other tool to run it there; Business Central refuses companies "
                 "the signed-in user has no permissions in.")
  if environment_switch:
    description = ("List the Business Central environments this connection reaches, the companies "
                   "in each of them, and which environment and company are the default. Pass an "
                   f"environment name as the '{ENVIRONMENT_ARGUMENT}' argument and a company name "
                   f"as '{COMPANY_ARGUMENT}' on any other tool to run it there; Business Central "
                   "refuses what the signed-in user has no permissions for.")
  return Tool(
      name=LIST_COMPANIES_TOOL,
      title="List Business Central companies",
      description=description,
      inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
      annotations=ToolAnnotations(title="List Business Central companies", readOnlyHint=True,
                                  destructiveHint=False, idempotentHint=True, openWorldHint=False),
  )


def add_company_switch(result: ListToolsResult, environment_switch: bool = False) -> ListToolsResult:
  """Add the `company` argument (and, when the connection reaches more than
  one environment, `environment`) to every forwarded tool and list the
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
    missing = {COMPANY_ARGUMENT: _COMPANY_ARGUMENT_SCHEMA}
    if environment_switch:
      missing[ENVIRONMENT_ARGUMENT] = _ENVIRONMENT_ARGUMENT_SCHEMA
    missing = {key: value for key, value in missing.items() if key not in props}
    if not missing:
      out.append(tool)
      continue
    for key, value in missing.items():
      props[key] = dict(value)
    schema["properties"] = props
    out.append(tool.model_copy(update={"inputSchema": schema}))
    changed = True
  if not seen_list_tool:
    out.append(list_companies_tool(environment_switch))
    changed = True
  return result.model_copy(update={"tools": out}) if changed else result


def pop_company(arguments: Optional[dict[str, Any]]) -> tuple[Optional[str], dict[str, Any]]:
  """Split the `company` argument off the arguments forwarded to BC."""
  return _pop(arguments, COMPANY_ARGUMENT)


def pop_environment(arguments: Optional[dict[str, Any]]) -> tuple[Optional[str], dict[str, Any]]:
  """Split the `environment` argument off the arguments forwarded to BC."""
  return _pop(arguments, ENVIRONMENT_ARGUMENT)


def _pop(arguments: Optional[dict[str, Any]], key: str) -> tuple[Optional[str], dict[str, Any]]:
  args = dict(arguments or {})
  value = args.pop(key, None)
  if isinstance(value, str) and value.strip():
    return value.strip(), args
  return None, args


def company_error(message: str) -> CallToolResult:
  return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)
