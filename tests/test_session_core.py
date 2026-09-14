"""The session-scoped core: build_server, RuntimeSlot, RuntimeOptions and run_slot_upstream.

An embedder that needs another transport or several users (a hosted server for
Claude.ai) builds on these instead of run_proxy. Driven over memory streams with
fake upstreams, like the stdio tests, so the assertions are what an MCP client sees.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from bc_mcp_proxy import proxy
from bc_mcp_proxy.auth import StaticTokenProvider
from bc_mcp_proxy.config import ProxyConfig

LOGGER = logging.getLogger("test-session-core")


class _FakeSession:
  def __init__(self, label: str) -> None:
    self.label = label

  async def list_tools(self) -> ListToolsResult:
    return ListToolsResult(tools=[Tool(name=f"List_{self.label}_PAG1", inputSchema={"type": "object"})])

  async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"{name} for {self.label}")])


class _FakeManager:
  def __init__(self, state: proxy._UpstreamSessionHolder, label: str) -> None:
    self.state = state
    self.label = label

  async def run(self) -> None:
    self.state.set_session(_FakeSession(self.label), lambda: "sid")  # type: ignore[arg-type]
    await asyncio.Event().wait()


def _slot(label: str) -> proxy.RuntimeSlot:
  slot = proxy.RuntimeSlot(state=proxy._UpstreamSessionHolder(), notifier=proxy._ClientNotifier(LOGGER))
  slot.runtime = proxy._Runtime(
      config=ProxyConfig(company=label), url="https://example.invalid", auth=None, registry=None,  # type: ignore[arg-type]
      cache=proxy._ToolsCache(ttl_seconds=60.0), directory=None, companies=None,
      manager=_FakeManager(slot.state, label), disk_cache=False)  # type: ignore[arg-type]
  return slot


@asynccontextmanager
async def _serve(server: Any, init_options: Any):
  async with create_client_server_memory_streams() as (client_streams, server_streams):
    task = asyncio.create_task(server.run(server_streams[0], server_streams[1], init_options))
    try:
      async with ClientSession(*client_streams) as client:
        await client.initialize()
        yield client
    finally:
      task.cancel()
      await asyncio.gather(task, return_exceptions=True)


async def test_the_resolver_picks_the_slot_of_each_request() -> None:
  """Two users behind one server: whoever the resolver names gets their own upstream."""
  slots = {"alice": _slot("alice"), "bob": _slot("bob")}
  current = {"user": "alice"}

  async def resolve() -> proxy.RuntimeSlot:
    return slots[current["user"]]

  server, init_options = proxy.build_server(ProxyConfig(), resolve, LOGGER)
  tasks = [asyncio.create_task(proxy.run_slot_upstream(s, s.runtime.config, None, LOGGER))  # type: ignore[union-attr]
           for s in slots.values()]
  try:
    async with _serve(server, init_options) as client:
      result = await asyncio.wait_for(client.call_tool("List_Customers_PAG30009", {}), 5)
      assert result.content[0].text == "List_Customers_PAG30009 for alice"
      current["user"] = "bob"
      result = await asyncio.wait_for(client.call_tool("List_Customers_PAG30009", {}), 5)
      assert result.content[0].text == "List_Customers_PAG30009 for bob"
      # tools/list serves the cache of the resolved slot: bob's is cold, so empty and no hang.
      assert (await asyncio.wait_for(client.list_tools(), 5)).tools == []
  finally:
    for task in tasks:
      task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for slot in slots.values():
      await proxy.close_slot(slot)


async def test_a_slot_without_runtime_or_hook_is_refused() -> None:
  slot = proxy.RuntimeSlot(state=proxy._UpstreamSessionHolder(), notifier=proxy._ClientNotifier(LOGGER))
  try:
    await proxy.run_slot_upstream(slot, ProxyConfig(), None, LOGGER)
  except RuntimeError as exc:
    assert "no prepare hook" in str(exc)
  else:  # pragma: no cover
    raise AssertionError("expected RuntimeError")


def test_runtime_options_replace_the_sign_in_and_skip_the_disk_cache(monkeypatch) -> None:
  """A hosted server supplies the user's tokens and must not touch the machine-wide caches."""
  calls: list[str] = []
  captured: dict[str, Any] = {}

  def no_msal(config: ProxyConfig, logger: Optional[logging.Logger] = None) -> StaticTokenProvider:
    calls.append("msal")
    return StaticTokenProvider(token="never")

  class _Auth:
    def __init__(self, token_provider: Any) -> None:
      captured["provider"] = token_provider

  monkeypatch.setattr(proxy, "create_token_provider", no_msal)
  monkeypatch.setattr(proxy, "_AsyncBearerAuth", _Auth)
  monkeypatch.setattr(proxy.tools_cache, "load_disk_cache", lambda config: calls.append("disk") or None)

  provider = StaticTokenProvider(token="from-the-embedder")
  api_provider = StaticTokenProvider(token="from-the-embedder-api")
  runtime = proxy._build_runtime(
      ProxyConfig(company="CRONUS BE", allow_company_switch=True),
      proxy._UpstreamSessionHolder(), proxy._ClientNotifier(LOGGER), LOGGER,
      proxy.RuntimeOptions(token_provider=provider, api_token_provider=api_provider, disk_cache=False))

  assert calls == []
  assert captured["provider"] is provider
  assert runtime.disk_cache is False and runtime.manager.disk_cache is False
  assert runtime.directory is not None


def test_runtime_options_default_to_the_stdio_behaviour(monkeypatch) -> None:
  calls: list[str] = []
  monkeypatch.setattr(proxy, "create_token_provider",
                      lambda config, logger=None: calls.append("msal") or StaticTokenProvider(token="x"))
  monkeypatch.setattr(proxy.tools_cache, "load_disk_cache", lambda config: calls.append("disk") or None)
  runtime = proxy._build_runtime(
      ProxyConfig(company="CRONUS BE"), proxy._UpstreamSessionHolder(), proxy._ClientNotifier(LOGGER), LOGGER)
  assert calls == ["msal", "disk"]
  assert runtime.disk_cache is True and runtime.manager.disk_cache is True
