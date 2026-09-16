"""Tests for running tools in another Business Central environment.

Environments are set by an embedding package through
`ProxyConfig.environments`; there is no environment variable or flag for it.
Each environment brings its own company, configuration name and allowed
companies, and gets its own upstream session, because the environment travels
in the session (a header on the v28 endpoint, the URL on the legacy one).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Optional

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from bc_mcp_proxy import proxy
from bc_mcp_proxy.companies import (
    COMPANY_ARGUMENT,
    ENVIRONMENT_ARGUMENT,
    LIST_COMPANIES_TOOL,
    CompanyDirectory,
    EnvironmentDirectory,
    add_company_switch,
    pop_environment,
)
from bc_mcp_proxy.config import EnvironmentTarget, ProxyConfig
from bc_mcp_proxy.proxy import _CompanySessions, _ToolsCache, _UpstreamSessionHolder

from .test_prepare_hook import API_PAYLOAD, _FakeManager, harness  # noqa: F401  (fixture)

LOGGER = logging.getLogger("t")
SANDBOX_PAYLOAD = {"value": [
    {"id": "9", "name": "CRONUS BE", "displayName": "Vangelder Solutions BV"},
    {"id": "8", "name": "My Company", "displayName": ""},
]}


def _cfg(*targets: EnvironmentTarget, **overrides: Any) -> ProxyConfig:
  return dataclasses.replace(
      ProxyConfig(environment="Development-V28", company="CRONUS BE",
                  configuration_name="Demo V28 MCP Dynamic", allow_company_switch=True,
                  environments=targets or None),
      **overrides)


SANDBOX = EnvironmentTarget(name="Sandbox-BE", company="CRONUS BE",
                            configuration_name="BC-MCP-V29", allowed_companies=("CRONUS BE",))


class _Directory(CompanyDirectory):
  """A company directory with a scripted API payload."""

  def __init__(self, config: ProxyConfig, payload: dict[str, Any]) -> None:
    super().__init__(config, None, LOGGER)
    self._payload = payload

  async def _fetch(self):  # type: ignore[override]
    from bc_mcp_proxy.companies import parse_companies
    return parse_companies(self._payload)


# -- the configuration per environment -----------------------------------------


def test_without_environments_the_connection_stays_on_the_configured_one() -> None:
  config = _cfg()
  assert proxy._environment_switch_on(config) is False
  names = [name for name, _ in proxy._environment_configs(config)]
  assert names == ["Development-V28"]


def test_the_configured_environment_comes_first_and_is_not_duplicated() -> None:
  config = _cfg(EnvironmentTarget(name="Development-V28", company="Demo Nutrisan"), SANDBOX)
  assert proxy._environment_switch_on(config) is True
  configs = dict(proxy._environment_configs(config))
  assert list(configs) == ["Development-V28", "Sandbox-BE"]
  # The connection's own company and configuration win for the default: the
  # first entry describes the connection, not an override of it.
  assert configs["Development-V28"].company == "CRONUS BE"
  assert configs["Development-V28"].configuration_name == "Demo V28 MCP Dynamic"
  assert configs["Sandbox-BE"].company == "CRONUS BE"
  assert configs["Sandbox-BE"].configuration_name == "BC-MCP-V29"
  assert configs["Sandbox-BE"].allowed_companies == ("CRONUS BE",)


def test_the_switch_needs_the_company_switch() -> None:
  assert proxy._environment_switch_on(_cfg(SANDBOX, allow_company_switch=False)) is False


# -- schema and arguments ------------------------------------------------------


def test_the_environment_argument_is_added_only_with_more_than_one_environment() -> None:
  tools = ListToolsResult(tools=[Tool(name="bc_actions_search", inputSchema={
      "type": "object", "properties": {"top": {"type": "integer"}}})])
  one = add_company_switch(tools)
  assert ENVIRONMENT_ARGUMENT not in one.tools[0].inputSchema["properties"]
  several = add_company_switch(tools, environment_switch=True)
  props = several.tools[0].inputSchema["properties"]
  assert set(props) == {"top", COMPANY_ARGUMENT, ENVIRONMENT_ARGUMENT}
  listing = [t for t in several.tools if t.name == LIST_COMPANIES_TOOL][0]
  assert "environments" in (listing.description or "")
  # Idempotent, like the company switch: the cache tiers compare identity.
  assert add_company_switch(several, environment_switch=True) is several


def test_the_environment_argument_is_split_off() -> None:
  assert pop_environment({"environment": " Sandbox-BE ", "top": 1}) == ("Sandbox-BE", {"top": 1})
  assert pop_environment({"environment": "  "}) == (None, {})
  assert pop_environment(None) == (None, {})


def test_the_tools_cache_injects_both_arguments() -> None:
  cache = _ToolsCache(ttl_seconds=60.0, company_switch=True, environment_switch=True)
  cache.store(ListToolsResult(tools=[Tool(name="bc_actions_search", inputSchema={"type": "object"})]))
  stored = cache.get_any()
  assert stored is not None
  assert ENVIRONMENT_ARGUMENT in stored.tools[0].inputSchema["properties"]


# -- the environment directory -------------------------------------------------


def _directories() -> EnvironmentDirectory:
  configs = dict(proxy._environment_configs(_cfg(SANDBOX)))
  return EnvironmentDirectory(
      {"Development-V28": _Directory(configs["Development-V28"], API_PAYLOAD),
       "Sandbox-BE": _Directory(configs["Sandbox-BE"], SANDBOX_PAYLOAD)},
      "Development-V28")


def test_an_environment_name_is_matched_case_insensitively() -> None:
  directories = _directories()
  assert directories.resolve("sandbox-be") == "Sandbox-BE"
  assert directories.resolve(" Development-V28 ") == "Development-V28"
  assert directories.resolve("Production") is None
  assert directories.is_default("development-v28") and not directories.is_default("Sandbox-BE")


async def test_the_description_covers_every_environment() -> None:
  text = await _directories().describe()
  assert "Environment 'Development-V28' (default for this connection):" in text
  assert "Environment 'Sandbox-BE':" in text
  # Each environment lists its own companies and its own default, and its own
  # limit: the sandbox allows CRONUS BE only, so its My Company is not offered
  # while the one of the default environment is.
  development, sandbox = text.split("Environment 'Sandbox-BE':")
  assert "- Demo Nutrisan" in development and "- My Company" in development
  assert "My Company" not in sandbox and "may contain other companies" in sandbox
  assert development.count("; default for this connection)") == 1
  assert sandbox.count("; default for this connection)") == 1


# -- sessions ------------------------------------------------------------------


class _Manager:
  def __init__(self) -> None:
    self.started = asyncio.Event()

  async def run(self) -> None:
    self.started.set()
    await asyncio.Event().wait()


async def test_one_session_per_environment_and_company() -> None:
  made: list[tuple[str, str]] = []

  def factory(company: str, environment: str = ""):
    made.append((environment, company))
    return _UpstreamSessionHolder(), _Manager()

  sessions = _CompanySessions("CRONUS BE", factory, LOGGER, "Development-V28")
  assert sessions.is_default("CRONUS BE", "Development-V28")
  # The same company name in another environment is another session.
  assert not sessions.is_default("CRONUS BE", "Sandbox-BE")
  h1, _ = sessions.get_or_start("CRONUS BE", "Sandbox-BE")
  h2, _ = sessions.get_or_start(" CRONUS BE ", "Sandbox-BE")
  assert h1 is h2 and made == [("Sandbox-BE", "CRONUS BE")]
  sessions.get_or_start("Demo Nutrisan")
  assert sessions.open_targets == [("Sandbox-BE", "CRONUS BE"), ("Development-V28", "Demo Nutrisan")]
  await sessions.close()


async def test_each_environment_gets_its_own_url_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
  """The environment travels in the session: a header on v28, the path on the
  legacy endpoint. A session built for another environment must carry it."""
  from bc_mcp_proxy.auth import StaticTokenProvider

  monkeypatch.setattr(proxy, "create_token_provider",
                      lambda config, logger=None: StaticTokenProvider(token="t"))
  monkeypatch.setattr(proxy, "_AsyncBearerAuth", lambda provider: None)
  monkeypatch.setattr(proxy.tools_cache, "load_disk_cache", lambda config: None)

  runtime = proxy._build_runtime(_cfg(SANDBOX), _UpstreamSessionHolder(),
                                 proxy._ClientNotifier(LOGGER), LOGGER,
                                 proxy.RuntimeOptions(disk_cache=False))
  assert runtime.environments is not None and runtime.companies is not None
  _, manager = runtime.companies.get_or_start("CRONUS BE", "Sandbox-BE")
  assert manager.config.environment == "Sandbox-BE"
  assert manager.config.configuration_name == "BC-MCP-V29"
  assert manager.headers["EnvironmentName"] == "Sandbox-BE"
  assert manager.headers["ConfigurationName"] == "BC-MCP-V29"
  assert manager.headers["Company"] == "CRONUS BE"


# -- routing a call ------------------------------------------------------------


def _runtime_with_environments(config, state, notifier, logger):
  configs = dict(proxy._environment_configs(config))
  directories = {name: _Directory(cfg, API_PAYLOAD if name == config.environment.strip()
                                  else SANDBOX_PAYLOAD)
                 for name, cfg in configs.items()}
  managers: dict[tuple[str, str], Any] = {}

  def factory(company: str, environment: str = ""):
    holder = _UpstreamSessionHolder()
    manager = _FakeManager(holder, dataclasses.replace(configs[environment], company=company))
    managers[(environment, company)] = manager
    return holder, manager

  sessions = _CompanySessions((config.company or "").strip(), factory, logger,
                              config.environment.strip())
  return proxy._Runtime(
      config=config, url="https://example.invalid", auth=None, registry=None,  # type: ignore[arg-type]
      cache=_ToolsCache(ttl_seconds=60.0), directory=directories[config.environment.strip()],
      companies=sessions, manager=_FakeManager(state, config),  # type: ignore[arg-type]
      environments=EnvironmentDirectory(directories, config.environment.strip()))


@pytest.fixture
def running(harness, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
  run, _ = harness
  monkeypatch.setattr(proxy, "_build_runtime", _runtime_with_environments)

  def start(config: Optional[ProxyConfig] = None):
    return run(dataclasses.replace(config or _cfg(SANDBOX), initial_tools_wait_seconds=0), None)

  return start


async def _text(client, arguments: dict[str, Any]) -> CallToolResult:
  return await asyncio.wait_for(client.call_tool("List_Customers_PAG30009", arguments), 5)


async def test_a_call_runs_in_the_environment_that_was_asked_for(running) -> None:
  async with running() as client:
    result = await _text(client, {ENVIRONMENT_ARGUMENT: "sandbox-be"})
  # The fake upstream answers with the company of the session it belongs to;
  # without a company of its own the environment's default is used.
  assert result.content[0].text == "List_Customers_PAG30009 in CRONUS BE"
  assert not result.isError


async def test_a_call_without_an_environment_uses_the_configured_one(running) -> None:
  async with running() as client:
    result = await _text(client, {})
  assert result.content[0].text == "List_Customers_PAG30009 in CRONUS BE"


async def test_an_unknown_environment_is_refused_without_opening_a_session(running) -> None:
  async with running() as client:
    result = await _text(client, {ENVIRONMENT_ARGUMENT: "Production"})
  text = result.content[0].text
  assert result.isError
  assert text.startswith("Environment 'Production' is not available for this connection")
  assert "Development-V28, Sandbox-BE" in text
  # Production exists in Business Central; only this connection cannot reach it.
  assert "does not exist" not in text


async def test_a_company_outside_the_other_environment_is_refused(running) -> None:
  async with running() as client:
    result = await _text(client, {ENVIRONMENT_ARGUMENT: "Sandbox-BE",
                                  COMPANY_ARGUMENT: "Demo Nutrisan"})
  text = result.content[0].text
  # Demo Nutrisan exists in Development-V28, not in the sandbox: the refusal
  # names the environment the call was routed to.
  assert result.isError and "Sandbox-BE" in text and "Demo Nutrisan" in text


async def test_an_environment_without_a_default_company_asks_for_one(running) -> None:
  config = _cfg(EnvironmentTarget(name="Sandbox-BE"))
  async with running(config) as client:
    result = await _text(client, {ENVIRONMENT_ARGUMENT: "Sandbox-BE"})
  text = result.content[0].text
  assert result.isError and f"'{COMPANY_ARGUMENT}' argument" in text


async def test_the_listing_tool_describes_every_environment(running) -> None:
  async with running() as client:
    result = await asyncio.wait_for(client.call_tool(LIST_COMPANIES_TOOL, {}), 5)
  text = result.content[0].text
  assert "Environment 'Development-V28' (default for this connection):" in text
  assert "Environment 'Sandbox-BE':" in text
