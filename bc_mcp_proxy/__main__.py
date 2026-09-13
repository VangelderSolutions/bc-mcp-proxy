from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Optional

from .config import (
    AUTH_MODES,
    InvalidBaseUrlError,
    ProxyConfig,
    resolve_token_scope,
    validate_base_url,
)
from .proxy import run_sync
from .setup_flow import run_interactive_setup


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
      description="Business Central MCP proxy implemented in Python.")
  parser.add_argument("--TenantId", dest="tenant_id")
  parser.add_argument("--ClientId", dest="client_id")
  parser.add_argument("--Company", dest="company")
  parser.add_argument("--Environment", dest="environment")
  parser.add_argument("--ConfigurationName", dest="configuration_name")
  parser.add_argument("--CustomAuthHeader", dest="custom_auth_header")
  parser.add_argument("--BaseUrl", dest="base_url")
  parser.add_argument("--TokenScope", dest="token_scope")
  parser.add_argument("--ServerName", dest="server_name")
  parser.add_argument("--ServerVersion", dest="server_version")
  parser.add_argument("--Instructions", dest="instructions")
  parser.add_argument("--LogLevel", dest="log_level")
  parser.add_argument("--HttpTimeoutSeconds", type=float, dest="http_timeout_seconds")
  parser.add_argument("--SseTimeoutSeconds", type=float, dest="sse_timeout_seconds")
  parser.add_argument("--DeviceCacheLocation", dest="device_cache_location")
  parser.add_argument("--DeviceCacheName", dest="device_cache_name")
  parser.add_argument(
      "--AuthMode", dest="auth_mode", choices=AUTH_MODES,
      help="Token acquisition strategy when no valid cached token exists: "
           "'auto' (interactive browser flow, fall back to device code), "
           "'interactive', or 'device_code'. Default: auto.")
  parser.add_argument(
      "--AnnotateTools", action="store_true", dest="annotate_tools", default=None,
      help="Add title/readOnlyHint/destructiveHint to forwarded tools that lack "
           "them (default: on).")
  parser.add_argument(
      "--NoAnnotateTools", action="store_false", dest="annotate_tools",
      help="Forward tool definitions exactly as Business Central sends them.")
  parser.add_argument(
      "--ForwardResourcesPrompts", action="store_true",
      dest="forward_resources_prompts", default=None,
      help="Forward resources/* and prompts/* to Business Central (default: on).")
  parser.add_argument(
      "--NoForwardResourcesPrompts", action="store_false",
      dest="forward_resources_prompts",
      help="Expose only tools/* to the client.")
  parser.add_argument(
      "--HideUnauthorizedTools", action="store_true",
      dest="hide_unauthorized_tools", default=None,
      help="Static tool mode: probe each List tool after connecting and hide "
           "the tools for pages the signed-in user may not read (default: off).")
  parser.add_argument(
      "--NoHideUnauthorizedTools", action="store_false",
      dest="hide_unauthorized_tools",
      help="Show every tool Business Central lists, even those the user cannot use.")
  parser.add_argument(
      "--AllowCompanySwitch", action="store_true",
      dest="allow_company_switch", default=None,
      help="Add a 'company' argument to every tool and a bc_list_companies tool so "
           "a call can run in another company of the environment (default: off).")
  parser.add_argument(
      "--NoAllowCompanySwitch", action="store_false",
      dest="allow_company_switch",
      help="Keep the connection bound to the configured company.")
  parser.add_argument("--Debug", action="store_true", dest="enable_debug")
  return parser


def parse_args(argv: list[str] | None = None) -> ProxyConfig:
  parser = build_parser()
  args = parser.parse_args(argv)

  defaults = ProxyConfig()
  env = _config_from_env()

  base_url = _select("base_url", args.base_url, env, defaults.base_url)
  # Resolve the OAuth scope from the effective base_url: explicit CLI/env
  # override always wins; otherwise the v28 host gets the v28 scope and
  # everything else gets the v26/v27 scope. Without this the v28 host
  # silently fails auth when users set BC_BASE_URL but not BC_TOKEN_SCOPE.
  token_scope_override = args.token_scope if args.token_scope is not None else env.get("token_scope")
  token_scope = resolve_token_scope(base_url, token_scope_override)

  return ProxyConfig(
      tenant_id=_select("tenant_id", args.tenant_id, env, defaults.tenant_id),
      client_id=_select("client_id", args.client_id, env, defaults.client_id),
      company=_select("company", args.company, env, defaults.company),
      environment=_select("environment", args.environment, env, defaults.environment),
      configuration_name=_select(
          "configuration_name", args.configuration_name, env, defaults.configuration_name),
      custom_auth_header=_select(
          "custom_auth_header", args.custom_auth_header, env, defaults.custom_auth_header),
      base_url=base_url,
      token_scope=token_scope,
      server_name=_select("server_name", args.server_name, env, defaults.server_name),
      server_version=_select("server_version", args.server_version, env, defaults.server_version),
      instructions=_select("instructions", args.instructions, env, defaults.instructions),
      http_timeout_seconds=_select_float(
          "http_timeout_seconds", args.http_timeout_seconds, env, defaults.http_timeout_seconds),
      sse_timeout_seconds=_select_float(
          "sse_timeout_seconds", args.sse_timeout_seconds, env, defaults.sse_timeout_seconds),
      device_cache_location=_select(
          "device_cache_location", args.device_cache_location, env, defaults.device_cache_location),
      device_cache_name=_select(
          "device_cache_name", args.device_cache_name, env, defaults.device_cache_name),
      auth_mode=_select("auth_mode", args.auth_mode, env, defaults.auth_mode).lower(),
      log_level=_select("log_level", args.log_level, env, defaults.log_level).upper(),
      annotate_tools=_select_bool(
          "annotate_tools", args.annotate_tools, env, defaults.annotate_tools),
      forward_resources_prompts=_select_bool(
          "forward_resources_prompts", args.forward_resources_prompts, env,
          defaults.forward_resources_prompts),
      hide_unauthorized_tools=_select_bool(
          "hide_unauthorized_tools", args.hide_unauthorized_tools, env,
          defaults.hide_unauthorized_tools),
      allow_company_switch=_select_bool(
          "allow_company_switch", args.allow_company_switch, env,
          defaults.allow_company_switch),
      enable_debug=args.enable_debug or _env_flag("BC_DEBUG"),
  )


def main(argv: list[str] | None = None) -> None:
  if argv is None:
    argv = sys.argv[1:]

  if argv and argv[0].lower() == "setup":
    run_interactive_setup()
    return

  config = parse_args(argv)
  logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
  try:
    validate_base_url(config.base_url, allow_non_standard=_env_flag("BC_ALLOW_NON_STANDARD_BASE_URL"))
  except InvalidBaseUrlError as exc:
    sys.stderr.write(f"ERROR: {exc}\n")
    sys.exit(2)
  if config.auth_mode not in AUTH_MODES:
    # argparse enforces this for --AuthMode, but BC_AUTH_MODE bypasses it.
    sys.stderr.write(
        f"ERROR: invalid auth mode {config.auth_mode!r}. "
        f"Expected one of: {', '.join(AUTH_MODES)}.\n")
    sys.exit(2)
  problem = _guid_problem(config)
  if problem is not None:
    sys.stderr.write(f"ERROR: {problem}\n")
    sys.exit(2)
  run_sync(config)


_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _guid_problem(config: ProxyConfig) -> Optional[str]:
  """One actionable sentence if tenant_id / client_id is not a GUID, else None.

  Without this, a tenant ID that lost its last character when pasted into
  the extension settings surfaces as an MSAL traceback ending in
  AADSTS90002 "Tenant not found", which reads like an Entra outage rather
  than a typo. Skipped when a custom auth header is used (no MSAL then).
  """
  if config.custom_auth_header:
    return None
  for label, value in (("Tenant ID", config.tenant_id), ("Client ID", config.client_id)):
    if not value:
      return (f"{label} is required. Copy it from the Entra admin center "
              "(App registrations > Overview) and set it in the extension settings, "
              f"--{label.replace(' ', '')} or BC_{label.upper().replace(' ', '_')}.")
    if not _GUID_RE.fullmatch(value):
      return (f"{label} {value!r} is not a valid GUID: expected 36 characters in the form "
              f"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx, got {len(value)}. "
              "Re-copy it from the Entra admin center (App registrations > Overview); "
              "a pasted value often loses its first or last character.")
  return None


def _config_from_env() -> dict[str, Optional[str]]:
  return {
      "tenant_id": os.getenv("BC_TENANT_ID"),
      "client_id": os.getenv("BC_CLIENT_ID"),
      "company": os.getenv("BC_COMPANY"),
      "environment": os.getenv("BC_ENVIRONMENT"),
      "configuration_name": os.getenv("BC_CONFIGURATION_NAME"),
      "custom_auth_header": os.getenv("BC_CUSTOM_AUTH_HEADER"),
      "base_url": os.getenv("BC_BASE_URL"),
      "token_scope": os.getenv("BC_TOKEN_SCOPE"),
      "server_name": os.getenv("BC_SERVER_NAME"),
      "server_version": os.getenv("BC_SERVER_VERSION"),
      "instructions": os.getenv("BC_INSTRUCTIONS"),
      "http_timeout_seconds": os.getenv("BC_HTTP_TIMEOUT_SECONDS"),
      "sse_timeout_seconds": os.getenv("BC_SSE_TIMEOUT_SECONDS"),
      "device_cache_location": os.getenv("BC_DEVICE_CACHE_LOCATION"),
      "device_cache_name": os.getenv("BC_DEVICE_CACHE_NAME"),
      "auth_mode": os.getenv("BC_AUTH_MODE"),
      "log_level": os.getenv("BC_LOG_LEVEL"),
      "annotate_tools": os.getenv("BC_ANNOTATE_TOOLS"),
      "forward_resources_prompts": os.getenv("BC_FORWARD_RESOURCES_PROMPTS"),
      "hide_unauthorized_tools": os.getenv("BC_HIDE_UNAUTHORIZED_TOOLS"),
      "allow_company_switch": os.getenv("BC_ALLOW_COMPANY_SWITCH"),
  }


def _select(
    key: str,
    cli_value: Optional[str],
    env: dict[str, Optional[str]],
    default_value: Optional[str],
) -> Optional[str]:
  if cli_value is not None:
    return _clean(key, cli_value)
  env_value = env.get(key)
  if env_value is not None:
    return _clean(key, env_value)
  return default_value


def _clean(key: str, value: str) -> str:
  """Strip surrounding whitespace from user-supplied config values.

  A trailing space on something like BC_TENANT_ID (easy to pick up when
  copy-pasting a GUID from the Azure portal) becomes an illegal HTTP
  header value once the proxy passes it to the v28 host — httpx/h11
  rejects the request with a `LocalProtocolError` that points at the
  transport, not the typo. Strip at the input boundary so the failure
  mode is fixed for every downstream consumer.
  """
  stripped = value.strip()
  if stripped != value:
    logging.getLogger("bc_mcp_proxy").warning(
        "Stripped surrounding whitespace from %s (check the env var or "
        "CLI argument -- pasted values sometimes carry stray spaces).", key,
    )
  return stripped


def _select_float(
    key: str,
    cli_value: Optional[float],
    env: dict[str, Optional[str]],
    default_value: float,
) -> float:
  if cli_value is not None:
    return cli_value
  env_value = env.get(key)
  if env_value is not None:
    try:
      return float(env_value)
    except ValueError as exc:
      raise ValueError(f"Environment variable for {key} must be numeric.") from exc
  return default_value


def _select_bool(
    key: str,
    cli_value: Optional[bool],
    env: dict[str, Optional[str]],
    default_value: bool,
) -> bool:
  if cli_value is not None:
    return cli_value
  env_value = env.get(key)
  if env_value is not None and env_value.strip():
    return _parse_flag(key, env_value)
  return default_value


def _parse_flag(key: str, value: str) -> bool:
  """'1/true/yes/on' -> True, '0/false/no/off' -> False (case-insensitive)."""
  normalized = value.strip().lower()
  if normalized in {"1", "true", "yes", "on"}:
    return True
  if normalized in {"0", "false", "no", "off"}:
    return False
  raise ValueError(f"Environment variable for {key} must be a boolean flag, got {value!r}.")


def _env_flag(name: str) -> bool:
  value = os.getenv(name)
  if value is None:
    return False
  return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
  main()

