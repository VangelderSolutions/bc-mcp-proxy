"""The first tools/list waits a bounded moment for the tool list of a starting connection.

Claude Desktop judges a local MCP server by its first tools/list answer: an
empty placeholder marks the server "Failed: offered no tools to Cowork and Code
sessions", although tools/list_changed brings the tools seconds later (measured
14-09-2026 on a fresh install: tools 6 s after the first request). The handler
therefore waits up to initial_tools_wait_seconds for the prepare hook, the disk
cache or the upstream pre-warm, and only then answers empty. The wait stays
bounded, so a Business Central cold start cannot run the request into the
client's timeout. Driven over memory streams, so the assertions are what an MCP
client sees.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

import pytest
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import INTERNAL_ERROR, ErrorData, ListToolsResult, Tool

from bc_mcp_proxy import proxy
from bc_mcp_proxy.__main__ import parse_args
from bc_mcp_proxy.config import ProxyConfig

LOGGER = logging.getLogger("test-first-tools-wait")
TOOLS = ListToolsResult(tools=[Tool(name="List_Customers_PAG30009", inputSchema={"type": "object"})])


class _IdleManager:
  async def run(self) -> None:
    await asyncio.Event().wait()


def _runtime(cached: Optional[ListToolsResult] = None) -> proxy._Runtime:
  cache = proxy._ToolsCache(ttl_seconds=60.0)
  if cached is not None:
    cache.store(cached)
  return proxy._Runtime(config=ProxyConfig(), url="https://example.invalid", auth=None,  # type: ignore[arg-type]
                        registry=None, cache=cache, directory=None, companies=None,
                        manager=_IdleManager(), disk_cache=False)  # type: ignore[arg-type]


def _slot() -> proxy.RuntimeSlot:
  return proxy.RuntimeSlot(state=proxy._UpstreamSessionHolder(), notifier=proxy._ClientNotifier(LOGGER))


@asynccontextmanager
async def _client(slot: proxy.RuntimeSlot, wait: float):
  async def resolve() -> proxy.RuntimeSlot:
    return slot

  server, init_options = proxy.build_server(ProxyConfig(initial_tools_wait_seconds=wait), resolve, LOGGER)
  async with create_client_server_memory_streams() as (client_streams, server_streams):
    task = asyncio.create_task(server.run(server_streams[0], server_streams[1], init_options))
    try:
      async with ClientSession(*client_streams) as client:
        await client.initialize()
        yield client
    finally:
      task.cancel()
      await asyncio.gather(task, return_exceptions=True)


async def _later(seconds: float, action) -> None:
  await asyncio.sleep(seconds)
  action()


async def test_tools_prepared_within_the_wait_are_the_first_answer() -> None:
  slot = _slot()
  async with _client(slot, wait=5) as client:
    # The prepare hook finishes and the disk cache is loaded 0.3 s after the request.
    finishing = asyncio.create_task(_later(0.3, lambda: setattr(slot, "runtime", _runtime(TOOLS))))
    started = time.monotonic()
    result = await asyncio.wait_for(client.list_tools(), 10)
    elapsed = time.monotonic() - started
    await finishing
  assert [t.name for t in result.tools] == ["List_Customers_PAG30009"]
  assert elapsed < 3  # answered as soon as the tools were there, not after the whole wait


async def test_a_cache_filled_by_the_pre_warm_within_the_wait_is_served() -> None:
  slot = _slot()
  slot.runtime = _runtime()  # prepared, nothing on disk: the pre-warm fills the cache
  async with _client(slot, wait=5) as client:
    warming = asyncio.create_task(_later(0.3, lambda: slot.runtime.cache.store(TOOLS)))  # type: ignore[union-attr]
    result = await asyncio.wait_for(client.list_tools(), 10)
    await warming
  assert [t.name for t in result.tools] == ["List_Customers_PAG30009"]


async def test_after_the_wait_the_answer_is_empty_and_list_changed_stays_due() -> None:
  slot = _slot()  # preparation does not finish in time
  async with _client(slot, wait=0.3) as client:
    started = time.monotonic()
    result = await asyncio.wait_for(client.list_tools(), 10)
    elapsed = time.monotonic() - started
  assert result.tools == []
  assert 0.25 <= elapsed < 3
  # The client holds the empty placeholder, so the real list will still be pushed.
  assert proxy._tools_signature(result) != proxy._tools_signature(TOOLS)
  assert slot.notifier._last_signature == proxy._tools_signature(ListToolsResult(tools=[]))


async def test_a_preparation_that_fails_during_the_wait_is_the_answer() -> None:
  slot = _slot()
  error = McpError(ErrorData(code=INTERNAL_ERROR, message="No BC MCP Enterprise licence is assigned to you."))
  async with _client(slot, wait=5) as client:
    failing = asyncio.create_task(_later(0.2, lambda: slot.state.set_fatal(error)))
    with pytest.raises(McpError, match="No BC MCP Enterprise licence"):
      await asyncio.wait_for(client.list_tools(), 10)
    await failing


@pytest.mark.parametrize("wait", [0, -1])
async def test_no_wait_answers_at_once(wait: float) -> None:
  slot = _slot()
  async with _client(slot, wait=wait) as client:
    started = time.monotonic()
    assert (await asyncio.wait_for(client.list_tools(), 5)).tools == []
    assert time.monotonic() - started < 1


async def test_cached_tools_never_wait() -> None:
  slot = _slot()
  slot.runtime = _runtime(TOOLS)
  assert await proxy._wait_for_first_tools(slot, 10) == 0.0
  assert slot.first_tools_deadline is None


async def test_the_window_is_spent_once_per_slot() -> None:
  """A cache that stays empty (failed pre-warm) must not make every later tools/list wait."""
  slot = _slot()
  slot.runtime = _runtime()
  async with _client(slot, wait=0.3) as client:
    assert (await asyncio.wait_for(client.list_tools(), 5)).tools == []  # waits the window
    started = time.monotonic()
    assert (await asyncio.wait_for(client.list_tools(), 5)).tools == []
    assert time.monotonic() - started < 0.2  # the window is over: at once


async def test_concurrent_first_requests_share_the_window() -> None:
  slot = _slot()
  async with _client(slot, wait=0.4) as client:
    started = time.monotonic()
    first, second = await asyncio.wait_for(asyncio.gather(client.list_tools(), client.list_tools()), 5)
    assert first.tools == [] and second.tools == []
    assert time.monotonic() - started < 1.5  # one window, not two in a row


def test_the_wait_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.delenv("BC_INITIAL_TOOLS_WAIT_SECONDS", raising=False)
  assert parse_args([]).initial_tools_wait_seconds == 10.0
  assert parse_args(["--InitialToolsWaitSeconds", "2.5"]).initial_tools_wait_seconds == 2.5
  monkeypatch.setenv("BC_INITIAL_TOOLS_WAIT_SECONDS", "0")
  assert parse_args([]).initial_tools_wait_seconds == 0.0
