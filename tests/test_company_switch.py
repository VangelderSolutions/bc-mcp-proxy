"""Tests for running tools in another company (companies.py + proxy wiring).

The Company header is per upstream session, so the proxy keeps one session
per company. These tests cover the schema injection, the company directory,
the argument routing and the note; the live behaviour is covered by the
e2e script in the release notes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from bc_mcp_proxy.companies import (
    COMPANY_ARGUMENT,
    LIST_COMPANIES_TOOL,
    Company,
    CompanyDirectory,
    add_company_switch,
    companies_url,
    parse_companies,
    pop_company,
)
from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.permissions import annotate_permission_denied
from bc_mcp_proxy.proxy import _CompanySessions, _ToolsCache, _UpstreamSessionHolder


def _tool(name: str, top: bool = True) -> Tool:
  schema: dict[str, Any] = {"type": "object", "properties": {}}
  if top:
    schema["properties"]["top"] = {"type": "integer"}
  return Tool(name=name, description="d", inputSchema=schema)


def _tools(*names: str) -> ListToolsResult:
  return ListToolsResult(tools=[_tool(n) for n in names])


API_PAYLOAD = {"value": [
    {"id": "1", "name": "CRONUS BE", "displayName": "Vangelder Solutions BV"},
    {"id": "2", "name": "Demo Nutrisan", "displayName": "Demo Nutrisan"},
    {"id": "3", "name": "My Company", "displayName": ""},
]}


# -- schema injection ----------------------------------------------------------


def test_company_argument_added_to_every_forwarded_tool_and_list_tool_appended() -> None:
  out = add_company_switch(_tools("bc_actions_search", "List_Customers_PAG30009"))
  names = [t.name for t in out.tools]
  assert names == ["bc_actions_search", "List_Customers_PAG30009", LIST_COMPANIES_TOOL]
  for t in out.tools[:2]:
    assert COMPANY_ARGUMENT in t.inputSchema["properties"]
    assert "top" in t.inputSchema["properties"]  # existing properties kept
  listing = out.tools[2]
  assert COMPANY_ARGUMENT not in listing.inputSchema["properties"]
  assert listing.annotations.readOnlyHint is True


def test_company_switch_is_idempotent_and_keeps_identity_when_done() -> None:
  once = add_company_switch(_tools("bc_actions_search"))
  twice = add_company_switch(once)
  assert twice is once
  assert [t.name for t in twice.tools].count(LIST_COMPANIES_TOOL) == 1


def test_empty_list_is_left_alone() -> None:
  empty = ListToolsResult(tools=[])
  assert add_company_switch(empty) is empty


def test_cache_applies_company_switch_only_when_enabled() -> None:
  on = _ToolsCache(ttl_seconds=10.0, company_switch=True)
  on.store(_tools("bc_actions_search"), now=1.0)
  assert [t.name for t in on.get_any().tools] == ["bc_actions_search", LIST_COMPANIES_TOOL]
  off = _ToolsCache(ttl_seconds=10.0)
  off.store(_tools("bc_actions_search"), now=1.0)
  assert [t.name for t in off.get_any().tools] == ["bc_actions_search"]


# -- argument handling ---------------------------------------------------------


def test_pop_company_strips_the_argument() -> None:
  assert pop_company({"top": 1, COMPANY_ARGUMENT: " Demo Nutrisan "}) == ("Demo Nutrisan", {"top": 1})
  assert pop_company({"top": 1}) == (None, {"top": 1})
  assert pop_company({COMPANY_ARGUMENT: ""}) == (None, {})
  assert pop_company(None) == (None, {})


def test_note_names_the_company() -> None:
  denial_text = ('{"error":{"code":"Internal_ServerError","message":"Sorry, the current permissions '
                 'prevented the action. (TableData 18 Customer Read: _Exclude_APIV2_)"}}')
  result = CallToolResult(content=[TextContent(type="text", text=denial_text)], isError=True)
  note = annotate_permission_denied(result, "List_Customers_PAG30009", {}, company="Demo Nutrisan").content[1].text
  assert "List_Customers_PAG30009 in company 'Demo Nutrisan'" in note


# -- directory -----------------------------------------------------------------


def test_companies_url_uses_the_standard_api_host_and_environment() -> None:
  assert companies_url(ProxyConfig(environment=" Production ")) == \
      "https://api.businesscentral.dynamics.com/v2.0/Production/api/v2.0/companies"


def test_parse_companies_skips_rows_without_a_name() -> None:
  rows = parse_companies({"value": [{"name": "A", "displayName": "AA", "id": "1"}, {"displayName": "x"}, "junk"]})
  assert rows == [Company(name="A", display_name="AA", id="1")]
  assert parse_companies({"error": {}}) == [] and parse_companies(None) == []


class _Directory(CompanyDirectory):
  """Directory with a scripted fetch (no HTTP)."""

  def __init__(self, config: ProxyConfig, payloads: list[Any]) -> None:
    super().__init__(config, token_provider=None)
    self.payloads = list(payloads)
    self.fetches = 0

  async def _fetch(self) -> list[Company]:
    self.fetches += 1
    payload = self.payloads.pop(0) if self.payloads else {"value": []}
    return parse_companies(payload)


async def test_resolve_matches_name_and_display_name_case_insensitively() -> None:
  d = _Directory(ProxyConfig(company="CRONUS BE"), [API_PAYLOAD])
  assert (await d.resolve("demo nutrisan"))[0] == "Demo Nutrisan"
  assert (await d.resolve("vangelder solutions bv"))[0] == "CRONUS BE"
  assert d.fetches == 1  # cached


async def test_resolve_refreshes_once_on_a_miss_then_reports_unknown() -> None:
  d = _Directory(ProxyConfig(company="CRONUS BE"), [{"value": []}, API_PAYLOAD])
  # First payload is empty -> unverifiable -> passes through unchanged.
  assert (await d.resolve("Anything"))[0] == "Anything"
  d2 = _Directory(ProxyConfig(company="CRONUS BE"), [API_PAYLOAD, API_PAYLOAD])
  resolved, known = await d2.resolve("Nope Ltd")
  assert resolved is None and d2.fetches == 2 and len(known) == 3


async def test_describe_marks_the_default_company() -> None:
  d = _Directory(ProxyConfig(environment="Dev", company="CRONUS BE"), [API_PAYLOAD])
  text = await d.describe()
  assert '- CRONUS BE (display name "Vangelder Solutions BV"; default for this connection)' in text
  assert "- Demo Nutrisan\n" in text and "- My Company" in text
  assert "or its display name" in text
  assert "may contain other companies" not in text  # no limit configured


async def test_directory_without_token_provider_is_empty_and_describe_says_so() -> None:
  d = CompanyDirectory(ProxyConfig(company="CRONUS BE"), token_provider=None)
  assert await d.companies() == []
  assert "could not be read" in await d.describe()


# -- per-company sessions ------------------------------------------------------


class _Manager:
  def __init__(self) -> None:
    self.started = asyncio.Event()

  async def run(self) -> None:
    self.started.set()
    await asyncio.Event().wait()


async def test_sessions_are_created_once_per_company_and_closed() -> None:
  made: list[str] = []

  def factory(company: str):
    made.append(company)
    return _UpstreamSessionHolder(), _Manager()

  sessions = _CompanySessions("CRONUS BE", factory, logging.getLogger("t"))
  assert sessions.is_default(" CRONUS BE ")
  h1, m1 = sessions.get_or_start("Demo Nutrisan")
  h2, m2 = sessions.get_or_start("Demo Nutrisan ")
  assert h1 is h2 and m1 is m2 and made == ["Demo Nutrisan"]
  await asyncio.sleep(0)
  assert m1.started.is_set()
  assert sessions.open_companies == ["Demo Nutrisan"]
  await sessions.close()
  assert all(t.cancelled() or t.done() for t in sessions._tasks) or sessions._tasks == []
