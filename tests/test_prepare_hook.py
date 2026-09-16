"""Tests for the embedding hooks: run_proxy(prepare=...) and allowed_companies.

A package that embeds the proxy (BC MCP Enterprise) decides the effective
configuration at startup. run_proxy is driven here over in-memory streams with
the upstream replaced by a fake manager, so the test sees exactly what an MCP
client would: an empty tool list while preparing, the prepared configuration
used for the connection, and the hook's error on every request when it fails.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import pytest
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from bc_mcp_proxy import proxy
from bc_mcp_proxy.__main__ import _parse_company_list, parse_args
from bc_mcp_proxy.companies import LIMITED_REASON, Company, CompanyDirectory, parse_companies
from bc_mcp_proxy.config import ProxyConfig

API_PAYLOAD = {"value": [
    {"id": "1", "name": "CRONUS BE", "displayName": "Vangelder Solutions BV"},
    {"id": "2", "name": "Demo Nutrisan", "displayName": "Demo Nutrisan"},
    {"id": "3", "name": "My Company", "displayName": ""},
]}


# -- run_proxy with a prepare hook ---------------------------------------------


class _FakeSession:
  def __init__(self, company: Optional[str]) -> None:
    self.company = company

  async def list_tools(self) -> ListToolsResult:
    return ListToolsResult(tools=[Tool(name="List_Customers_PAG30009", inputSchema={"type": "object"})])

  async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"{name} in {self.company}")])


class _FakeManager:
  def __init__(self, state: proxy._UpstreamSessionHolder, config: ProxyConfig) -> None:
    self.state = state
    self.config = config

  async def run(self) -> None:
    self.state.set_session(_FakeSession(self.config.company), lambda: "sid")  # type: ignore[arg-type]
    await asyncio.Event().wait()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
  built: list[ProxyConfig] = []

  def fake_build(config, state, notifier, logger):
    built.append(config)
    return proxy._Runtime(
        config=config, url="https://example.invalid", auth=None, registry=None,  # type: ignore[arg-type]
        cache=proxy._ToolsCache(ttl_seconds=60.0), directory=None, companies=None,
        manager=_FakeManager(state, config))  # type: ignore[arg-type]

  monkeypatch.setattr(proxy, "_build_runtime", fake_build)

  @asynccontextmanager
  async def run(config: ProxyConfig, prepare):
    async with create_client_server_memory_streams() as (client_streams, server_streams):

      @asynccontextmanager
      async def fake_stdio():
        yield server_streams

      monkeypatch.setattr(proxy, "stdio_server", fake_stdio)
      task = asyncio.create_task(proxy.run_proxy(config, prepare))
      try:
        async with ClientSession(*client_streams) as client:
          await client.initialize()
          yield client
      finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

  return run, built


async def test_prepare_changes_the_config_before_the_connection_is_built(harness) -> None:
  run, built = harness
  release = asyncio.Event()

  async def prepare(config: ProxyConfig) -> ProxyConfig:
    await release.wait()
    return dataclasses.replace(config, company="Demo Nutrisan", configuration_name="Sales")

  # No first-list wait: this test holds preparation open on purpose (the wait
  # itself is covered in test_first_tools_wait.py).
  async with run(ProxyConfig(company="CRONUS BE", initial_tools_wait_seconds=0), prepare) as client:
    # Still preparing: nothing built, an empty list rather than a hang.
    assert (await client.list_tools()).tools == []
    assert built == []
    release.set()
    result = await asyncio.wait_for(client.call_tool("List_Customers_PAG30009", {}), 5)
    assert result.content[0].text == "List_Customers_PAG30009 in Demo Nutrisan"
    assert [(c.company, c.configuration_name) for c in built] == [("Demo Nutrisan", "Sales")]


async def test_prepare_failure_is_the_error_on_tools_list_and_calls(harness) -> None:
  run, built = harness

  async def prepare(config: ProxyConfig) -> ProxyConfig:
    raise RuntimeError("No BC MCP Enterprise licence is assigned to you.")

  async with run(ProxyConfig(company="CRONUS BE"), prepare) as client:
    await asyncio.sleep(0.05)
    with pytest.raises(McpError, match="No BC MCP Enterprise licence"):
      await client.list_tools()
    result = await asyncio.wait_for(client.call_tool("List_Customers_PAG30009", {}), 5)
    assert result.isError and "No BC MCP Enterprise licence" in result.content[0].text
    assert built == []


async def test_prepare_must_return_a_proxy_config() -> None:
  state = proxy._UpstreamSessionHolder()

  async def prepare(config: ProxyConfig) -> Any:
    return {"company": "x"}

  runtime = await proxy._prepare_runtime(
      ProxyConfig(), prepare, state, proxy._ClientNotifier(logging.getLogger("t")), logging.getLogger("t"))
  assert runtime is None
  assert state.fatal is not None and "expected ProxyConfig" in state.fatal.error.message


async def test_without_prepare_the_runtime_is_built_before_serving(harness) -> None:
  run, built = harness
  # The SDK client lists tools inside call_tool (output validation) and this fake
  # upstream never pre-warms the cache, so skip the first-list wait here.
  async with run(ProxyConfig(company="CRONUS BE", initial_tools_wait_seconds=0), None) as client:
    assert [c.company for c in built] == ["CRONUS BE"]
    result = await asyncio.wait_for(client.call_tool("List_Customers_PAG30009", {}), 5)
    assert result.content[0].text == "List_Customers_PAG30009 in CRONUS BE"


# -- allowed_companies ---------------------------------------------------------


class _Directory(CompanyDirectory):
  def __init__(self, config: ProxyConfig, payload: Any) -> None:
    super().__init__(config, token_provider=None)
    self.payload = payload

  async def _fetch(self) -> list[Company]:
    return parse_companies(self.payload)


def _cfg(allowed: Optional[tuple[str, ...]]) -> ProxyConfig:
  return ProxyConfig(environment="Dev", company="CRONUS BE", allow_company_switch=True, allowed_companies=allowed)


async def test_allowed_companies_filters_resolve_by_name_or_display_name() -> None:
  d = _Directory(_cfg(("demo nutrisan",)), API_PAYLOAD)
  assert (await d.resolve("Demo Nutrisan"))[0] == "Demo Nutrisan"
  assert (await d.resolve("Vangelder Solutions BV"))[0] == "CRONUS BE"  # the configured company
  resolved, visible = await d.resolve("My Company")
  assert resolved is None
  assert sorted(c.name for c in visible) == ["CRONUS BE", "Demo Nutrisan"]


async def test_allowed_companies_filters_the_listing() -> None:
  text = await _Directory(_cfg(("Demo Nutrisan",)), API_PAYLOAD).describe()
  assert "available for this connection" in text
  assert "CRONUS BE" in text and "Demo Nutrisan" in text and "My Company" not in text
  # A limited list must not read as the environment's full list (a client then
  # claims a company "does not exist" when it is only not allowed).
  assert "may contain other companies" in text and "rather than that it does not exist" in text
  # The proxy cannot tell who limited the list (an administrator's rule, or
  # only the user's own permissions), so the note must not name an administrator.
  assert LIMITED_REASON in text and "administrator" not in text
  assert '- CRONUS BE (display name "Vangelder Solutions BV"; default for this connection)' in text


async def test_allowed_companies_without_a_readable_directory_only_passes_listed_names() -> None:
  d = _Directory(_cfg(("Demo Nutrisan",)), {"value": []})
  assert (await d.resolve("demo nutrisan"))[0] == "Demo Nutrisan"
  assert (await d.resolve("My Company"))[0] is None
  text = await d.describe()
  assert "- CRONUS BE (default for this connection)" in text and "- Demo Nutrisan" in text
  assert "may contain other companies" in text


async def test_a_call_in_a_company_outside_allowed_companies_is_not_available(
    harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
  run, _ = harness

  def no_session(company: str):
    raise AssertionError(f"no upstream session may open for {company!r}")

  def build_with_directory(config, state, notifier, logger):
    return proxy._Runtime(
        config=config, url="https://example.invalid", auth=None, registry=None,  # type: ignore[arg-type]
        cache=proxy._ToolsCache(ttl_seconds=60.0), directory=_Directory(config, API_PAYLOAD),
        companies=proxy._CompanySessions("CRONUS BE", no_session, logger),
        manager=_FakeManager(state, config))  # type: ignore[arg-type]

  monkeypatch.setattr(proxy, "_build_runtime", build_with_directory)
  config = dataclasses.replace(_cfg(("Demo Nutrisan",)), initial_tools_wait_seconds=0)
  with caplog.at_level(logging.WARNING, logger="bc_mcp_proxy"):
    async with run(config, None) as client:
      result = await asyncio.wait_for(
          client.call_tool("List_Customers_PAG30009", {"company": "My Company"}), 5)
  text = result.content[0].text
  assert result.isError and text.startswith("Company 'My Company' is not available for this connection")
  # An embedding package may keep only the companies the user has permissions
  # in: no administrator, and no claim that the company does not exist.
  assert LIMITED_REASON in text and "administrator" not in text and "does not exist" not in text
  assert text.endswith("Available: CRONUS BE, Demo Nutrisan.")
  # The server has to show the refusal too. Without it, "the client never asked"
  # and "the proxy said no" are indistinguishable in the log afterwards.
  assert any("Refused a call" in r.getMessage() and "My Company" in r.getMessage()
             for r in caplog.records)


def test_a_refusal_without_a_logger_is_still_returned() -> None:
  """company_error is part of the embedding surface; the logger is optional."""
  from bc_mcp_proxy.companies import company_error
  assert company_error("Company 'X' is not available.").isError


async def test_no_allowed_companies_keeps_the_existing_behaviour() -> None:
  d = _Directory(_cfg(None), API_PAYLOAD)
  assert (await d.resolve("My Company"))[0] == "My Company"
  assert (await _Directory(_cfg(None), {"value": []}).resolve("Anything"))[0] == "Anything"


def test_allowed_companies_env_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.delenv("BC_ALLOWED_COMPANIES", raising=False)
  assert parse_args([]).allowed_companies is None
  monkeypatch.setenv("BC_ALLOWED_COMPANIES", "Demo Nutrisan; Acme, Inc.;")
  assert parse_args([]).allowed_companies == ("Demo Nutrisan", "Acme, Inc.")
  assert parse_args(["--AllowedCompanies", "X"]).allowed_companies == ("X",)
  assert _parse_company_list(" ; ") is None and _parse_company_list(None) is None
