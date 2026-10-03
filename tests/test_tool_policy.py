"""Tests for the tool policy an embedding package can put on a connection.

The policy answers, per action and environment, whether the connection offers
and runs it. Refused actions disappear from the listings, and a refused call
is answered with the policy's reason without going to Business Central.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Optional

from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from bc_mcp_proxy import proxy
from bc_mcp_proxy.companies import ENVIRONMENT_ARGUMENT
from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.policy import ToolPolicy, action_of, filter_search_result, filter_tools

from .test_environment_switch import SANDBOX, _cfg, running  # noqa: F401  (fixture)
from .test_prepare_hook import harness  # noqa: F401  (fixture)

LOGGER = logging.getLogger("t")


class _ReadOnlyOutsideSandbox:
  """Writes are refused, except in Sandbox-BE."""

  def __init__(self) -> None:
    self.asked: list[tuple[str, Optional[str]]] = []

  def refusal(self, action: str, environment: Optional[str]) -> Optional[str]:
    self.asked.append((action, environment))
    if action.startswith("List_") or action.startswith("bc_"):
      return None
    if (environment or "").casefold() == "sandbox-be":
      return None
    return f"{action} is not allowed here: the rule gives you Read."


def _search(*names: str) -> CallToolResult:
  import json
  text = "Here are Business Central action names matching your search:\n" + json.dumps(list(names))
  return CallToolResult(content=[TextContent(type="text", text=text)])


# -- the helpers ---------------------------------------------------------------


def test_the_policy_is_a_protocol_any_object_can_meet() -> None:
  assert isinstance(_ReadOnlyOutsideSandbox(), ToolPolicy)


def test_a_dynamic_tool_is_judged_by_the_action_it_names() -> None:
  assert action_of("bc_actions_invoke", {"ActionName": " Create_Item_PAG30008 "}) == "Create_Item_PAG30008"
  assert action_of("bc_actions_describe", {"ActionName": "Post_SalesInvoices_PAG30012"}) == "Post_SalesInvoices_PAG30012"
  # Nothing named, or a static tool: the tool itself.
  assert action_of("bc_actions_invoke", {}) == "bc_actions_invoke"
  assert action_of("Create_Item_PAG30008", {"ActionName": "x"}) == "Create_Item_PAG30008"


def test_refused_tools_are_left_out_of_the_tool_list() -> None:
  listed = ListToolsResult(tools=[Tool(name=n, inputSchema={"type": "object"})
                                  for n in ("List_Items_PAG30008", "Create_Item_PAG30008")])
  shown = filter_tools(listed, _ReadOnlyOutsideSandbox())
  assert shown is not None and [t.name for t in shown.tools] == ["List_Items_PAG30008"]
  # No policy, or nothing refused: the same object, so nothing is copied.
  assert filter_tools(listed, None) is listed
  reads = ListToolsResult(tools=listed.tools[:1])
  assert filter_tools(reads, _ReadOnlyOutsideSandbox()) is reads


def test_refused_actions_are_left_out_of_a_search_result() -> None:
  found = _search("List_Items_PAG30008", "Create_Item_PAG30008", "Post_SalesInvoices_PAG30012")
  shown = filter_search_result(found, _ReadOnlyOutsideSandbox(), None)
  text = shown.content[0].text
  assert "List_Items_PAG30008" in text and "Create_Item" not in text and "Post_" not in text
  assert text.startswith("Here are Business Central action names")
  # The same search in the environment where writing is allowed keeps them all.
  assert filter_search_result(found, _ReadOnlyOutsideSandbox(), "Sandbox-BE") is found


def test_an_answer_in_another_shape_is_passed_on() -> None:
  odd = CallToolResult(content=[TextContent(type="text", text="No matching actions found.")])
  assert filter_search_result(odd, _ReadOnlyOutsideSandbox(), None) is odd
  failed = CallToolResult(content=[TextContent(type="text", text='["Create_Item_PAG30008"]')], isError=True)
  assert filter_search_result(failed, _ReadOnlyOutsideSandbox(), None) is failed


# -- on a running connection -----------------------------------------------------


async def test_a_refused_call_is_answered_without_reaching_business_central(running) -> None:  # noqa: F811
  policy = _ReadOnlyOutsideSandbox()
  async with running(_cfg(SANDBOX, tool_policy=policy)) as client:
    refused = await asyncio.wait_for(client.call_tool("Create_Item_PAG30008", {}), 5)
    allowed = await asyncio.wait_for(client.call_tool("List_Customers_PAG30009", {}), 5)
  assert refused.isError and "the rule gives you Read" in refused.content[0].text
  assert not allowed.isError and allowed.content[0].text == "List_Customers_PAG30009 in CRONUS BE"


async def test_the_policy_is_asked_with_the_environment_of_the_call(running) -> None:  # noqa: F811
  policy = _ReadOnlyOutsideSandbox()
  async with running(_cfg(SANDBOX, tool_policy=policy)) as client:
    there = await asyncio.wait_for(
        client.call_tool("Create_Item_PAG30008", {ENVIRONMENT_ARGUMENT: "sandbox-be"}), 5)
    here = await asyncio.wait_for(client.call_tool("Create_Item_PAG30008", {}), 5)
  assert not there.isError and there.content[0].text == "Create_Item_PAG30008 in CRONUS BE"
  assert here.isError
  assert ("Create_Item_PAG30008", "Sandbox-BE") in policy.asked
  assert ("Create_Item_PAG30008", None) in policy.asked


async def test_a_dynamic_invoke_is_refused_by_the_action_it_names(running) -> None:  # noqa: F811
  async with running(_cfg(SANDBOX, tool_policy=_ReadOnlyOutsideSandbox())) as client:
    refused = await asyncio.wait_for(
        client.call_tool("bc_actions_invoke", {"ActionName": "Post_SalesInvoices_PAG30012"}), 5)
    read = await asyncio.wait_for(
        client.call_tool("bc_actions_invoke", {"ActionName": "List_Items_PAG30008"}), 5)
  assert refused.isError and "Post_SalesInvoices_PAG30012 is not allowed here" in refused.content[0].text
  assert not read.isError


async def test_without_a_policy_nothing_changes(running) -> None:  # noqa: F811
  async with running() as client:
    result = await asyncio.wait_for(client.call_tool("Create_Item_PAG30008", {}), 5)
  assert not result.isError and result.content[0].text == "Create_Item_PAG30008 in CRONUS BE"


def test_a_live_reconfigure_takes_the_new_policy() -> None:
  config = ProxyConfig(environment="Development-V28", company="CRONUS BE", allow_company_switch=True)
  assert config.tool_policy is None
  policy = _ReadOnlyOutsideSandbox()
  merged = dataclasses.replace(config, tool_policy=policy)
  assert merged.tool_policy is policy
  # _rebuild_views copies it with the allow-lists (see its docstring).
  import inspect
  assert "tool_policy=config.tool_policy" in inspect.getsource(proxy._rebuild_views)
