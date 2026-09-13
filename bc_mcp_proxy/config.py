from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from ._version import __version__


# Since BC 2026 release wave 1 (v28) Microsoft documents a single MCP host for
# every client -- mcp.businesscentral.dynamics.com -- with the environment,
# tenant, company and configuration carried in request headers. Version 29
# (2026 release wave 2) keeps that contract and only adds tools. The older
# per-environment path on api.businesscentral.dynamics.com is no longer
# documented; it is kept as an explicit legacy path for tenants that have not
# been upgraded yet.
V28_HOST = "mcp.businesscentral.dynamics.com"
V28_BASE_URL = "https://mcp.businesscentral.dynamics.com"
LEGACY_HOST = "api.businesscentral.dynamics.com"
V27_BASE_URL = "https://api.businesscentral.dynamics.com"

# The modern host requires a different OAuth scope than the legacy path.
# Hitting the modern host with the legacy scope yields a 401.
V27_SCOPE = "https://api.businesscentral.dynamics.com/.default"
V28_SCOPE = "https://mcp.businesscentral.dynamics.com/.default"
MODERN_SCOPE = V28_SCOPE

# Valid values for ProxyConfig.auth_mode / --AuthMode / BC_AUTH_MODE.
AUTH_MODES = ("auto", "interactive", "device_code")


def is_legacy_endpoint(base_url: str) -> bool:
  """True only for the pre-v28 per-environment host (api.businesscentral...)."""
  host = (urlparse(base_url).hostname or "").lower()
  return host == LEGACY_HOST


def is_v28_endpoint(base_url: str) -> bool:
  """Detect the modern (v28+) header-routed Business Central MCP endpoint.

  legacy : api.businesscentral.dynamics.com/v2.0/{env}/mcp
  modern : mcp.businesscentral.dynamics.com (env flows through headers)

  Everything that is not the legacy host is treated as modern: the documented
  host, any future *.businesscentral.dynamics.com regional or staging
  subdomain, and non-BC hosts opted in via BC_ALLOW_NON_STANDARD_BASE_URL
  (a local mock of today's server looks like the modern one). Before this
  an unknown host silently fell back to the legacy URL shape and scope,
  which fails closed with a 401 instead of an actionable error.
  """
  return not is_legacy_endpoint(base_url)


# Hosts the proxy will talk to without prompting. Anything outside this
# allowlist must be opted-in explicitly via BC_ALLOW_NON_STANDARD_BASE_URL=1
# (e.g. a local mock server in tests). The check defends against accidental
# or malicious misconfiguration that would point the bearer token at a
# third-party host (a form of SSRF).
_TRUSTED_BC_HOST_SUFFIX = ".businesscentral.dynamics.com"


def is_trusted_bc_host(base_url: str) -> bool:
  """Return True if base_url is https and points at a Business Central host.

  Accepts api.businesscentral.dynamics.com (legacy), mcp.businesscentral.dynamics.com
  (v28+), and any future *.businesscentral.dynamics.com regional or staging
  subdomain Microsoft might introduce.
  """
  parsed = urlparse(base_url)
  if parsed.scheme != "https":
    return False
  host = (parsed.hostname or "").lower()
  if not host:
    return False
  return host == V28_HOST or host.endswith(_TRUSTED_BC_HOST_SUFFIX) or host == "businesscentral.dynamics.com"


class InvalidBaseUrlError(ValueError):
  """Raised when base_url isn't an https URL pointing at a BC host."""


def validate_base_url(base_url: str, allow_non_standard: bool = False) -> str:
  """Reject base_urls that aren't https or aren't pointed at Business Central.

  Returns the validated URL so callers can use the return value as a
  sanitization source rather than passing through the original input.
  This matters for SAST tools (e.g. Snyk Code) that follow the data flow
  from CLI/env input into the HTTP client.

  Set allow_non_standard=True (e.g. via BC_ALLOW_NON_STANDARD_BASE_URL=1)
  to skip the host check for local development or mock-server testing.
  The scheme check is always enforced — sending bearer tokens over plain
  http is never something the proxy should do silently.
  """
  parsed = urlparse(base_url)
  if parsed.scheme != "https":
    raise InvalidBaseUrlError(
        f"BC_BASE_URL must use https (got: {base_url!r}). "
        "The proxy refuses to send bearer tokens over an unencrypted connection.")
  if not allow_non_standard and not is_trusted_bc_host(base_url):
    raise InvalidBaseUrlError(
        f"BC_BASE_URL host {parsed.hostname!r} is not a recognized Business "
        "Central endpoint. Expected *.businesscentral.dynamics.com. "
        "Set BC_ALLOW_NON_STANDARD_BASE_URL=1 to allow custom hosts (testing only).")
  # Reconstruct from parsed components rather than returning the original
  # string. The reconstructed value carries no taint as far as Snyk is
  # concerned because every component came from urlparse(), not from
  # the raw input. Functionally identical to base_url.rstrip("/").
  path = (parsed.path or "").rstrip("/")
  netloc = parsed.netloc
  return f"{parsed.scheme}://{netloc}{path}"


def resolve_token_scope(base_url: str, override: Optional[str]) -> str:
  """Pick the right OAuth scope for the configured endpoint.

  If the user explicitly set BC_TOKEN_SCOPE (or --TokenScope), honour it.
  Otherwise auto-pick: only the legacy api.* host gets the legacy scope;
  every other host gets the modern mcp.* scope.
  """
  if override:
    return override
  return V27_SCOPE if is_legacy_endpoint(base_url) else MODERN_SCOPE


@dataclass(slots=True)
class ProxyConfig:
  """Configuration values required to run the Business Central MCP proxy."""

  # Sent as the MCP Implementation name and in the X-Client-Application
  # header, which BC telemetry records as `clientName` (event RT0054).
  server_name: str = "vgs-bc-mcp"
  server_version: str = __version__
  instructions: Optional[str] = None

  tenant_id: Optional[str] = None
  client_id: Optional[str] = None
  # Filled in by resolve_token_scope() when not user-overridden — see
  # __main__.parse_args. The default here matches the default base_url below
  # so a bare ProxyConfig() is internally consistent.
  token_scope: str = V28_SCOPE
  base_url: str = V28_BASE_URL
  environment: str = "Production"
  company: Optional[str] = None
  configuration_name: Optional[str] = None

  custom_auth_header: Optional[str] = None
  # How to acquire a token when no valid cached one exists:
  #   "auto"        — try the interactive browser+loopback flow first, fall
  #                    back to device code if no browser / loopback is usable
  #   "interactive" — interactive only; fail with an actionable error
  #   "device_code" — skip interactive entirely (headless / server installs)
  # The silent (cached refresh-token) path is always tried first regardless.
  auth_mode: str = "auto"
  # 30s is too tight for BC v28 dynamic mode with Discover Additional
  # Objects on — the first bc_actions_search measured ~57s server-side.
  http_timeout_seconds: float = 120.0
  sse_timeout_seconds: float = 300.0

  device_cache_name: str = "bc_mcp_proxy"
  device_cache_location: Optional[str] = None

  # Refresh the access token whenever its remaining validity drops below this many seconds.
  token_refresh_skew_seconds: float = 300.0

  # tools/list cache TTL — reduces round-trips and masks BC cold-starts.
  tools_cache_ttl_seconds: float = 300.0
  # Fill in `title` and readOnlyHint/destructiveHint on forwarded tools that
  # lack them, keyed on BC's documented tool naming. Lets Claude auto-approve
  # read-only tools and always confirm writes; required for directory
  # listing. BC_ANNOTATE_TOOLS=0 / --NoAnnotateTools disables it.
  annotate_tools: bool = True
  # Forward resources/* and prompts/* to BC in addition to tools/*. BC v28+
  # returns large datasets as embedded resources / file references; v29 adds
  # more. BC_FORWARD_RESOURCES_PROMPTS=0 / --NoForwardResourcesPrompts disables.
  forward_resources_prompts: bool = True
  # After each upstream connect, read one record from every static List tool
  # and hide the tools (plus their write siblings) for pages Business Central
  # refuses for this user. Opt-in: it costs one call per API page per connect
  # and only helps static tool mode. BC enforces every call regardless.
  # BC_HIDE_UNAUTHORIZED_TOOLS=1 / --HideUnauthorizedTools enables it.
  hide_unauthorized_tools: bool = False
  # Let the client run a tool in another company of the same environment
  # through an optional `company` argument (one upstream session per
  # company) and list the user's companies with bc_list_companies. Off by
  # default: an installation then stays bound to `company`. Business Central
  # enforces per-company permissions either way.
  # BC_ALLOW_COMPANY_SWITCH=1 / --AllowCompanySwitch enables it.
  allow_company_switch: bool = False
  # With the company switch on: the companies (API name or display name) a
  # call may be routed to. None = every company of the environment. The
  # configured `company` stays reachable either way. Like the switch itself
  # a convenience limit, not a security boundary.
  # BC_ALLOWED_COMPANIES="A;B" / --AllowedCompanies "A;B" sets it.
  allowed_companies: Optional[tuple[str, ...]] = None
  # Persistent on-disk tools/list cache TTL.
  tools_disk_cache_ttl_seconds: float = 24 * 60 * 60

  log_level: str = "INFO"
  enable_debug: bool = False
