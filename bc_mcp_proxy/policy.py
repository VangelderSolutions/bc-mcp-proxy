"""A tool policy an embedding package can put on a connection.

The proxy itself has no opinion about which Business Central actions a user
may run: Business Central decides that on every call. An embedding package that
already knows the answer -- from a rule the administrator made, say -- can hand
the proxy a policy through `ProxyConfig.tool_policy`, so the client is not
offered the actions in the first place and a call that would be refused anyway
is answered at once, with the reason.

It is a convenience, not a boundary: a user with another MCP client is only
stopped by Business Central. There is no environment variable or command-line
flag for it; only `run_proxy(config, prepare=...)` can set one.

With a policy:
- a tool the policy refuses is left out of tools/list (static tool mode);
- an action it refuses is left out of the bc_actions_search result (dynamic
  tool mode), and bc_actions_describe and bc_actions_invoke answer with the
  policy's reason instead of going to Business Central;
- the answer is asked per call, with the environment the call runs in, so one
  connection can carry different rules per environment.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Protocol, runtime_checkable

from mcp.types import CallToolResult, ListToolsResult, TextContent

SEARCH_TOOL = "bc_actions_search"
# The dynamic tools that name the action they are about in `ActionName`.
ACTION_TOOLS = frozenset({"bc_actions_describe", "bc_actions_invoke"})
ACTION_ARGUMENT = "ActionName"

_NAME_LIST = re.compile(r"\[[^\[\]]*\]", re.DOTALL)


@runtime_checkable
class ToolPolicy(Protocol):
  """What an embedding package implements."""

  def refusal(self, action: str, environment: Optional[str]) -> Optional[str]:
    """Why `action` may not be used in `environment`, or None when it may.

    `action` is a static tool name or a dynamic action name, for example
    `Create_ProductionOrder_PAG73633181`. `environment` is the environment the
    call runs in; None is the connection's default one. The text is shown to
    the client as the answer to the call.
    """
    ...


def action_of(name: str, arguments: Optional[dict[str, Any]]) -> str:
  """The action a call is about: the tool itself, or what a dynamic tool names."""
  if name in ACTION_TOOLS:
    wanted = (arguments or {}).get(ACTION_ARGUMENT)
    if isinstance(wanted, str) and wanted.strip():
      return wanted.strip()
  return name


def filter_tools(result: Optional[ListToolsResult], policy: Optional[ToolPolicy]) -> Optional[ListToolsResult]:
  """tools/list without the tools the policy refuses in the default environment."""
  if policy is None or result is None:
    return result
  kept = [tool for tool in result.tools if policy.refusal(tool.name, None) is None]
  if len(kept) == len(result.tools):
    return result
  return result.model_copy(update={"tools": kept})


def filter_search_result(result: CallToolResult, policy: Optional[ToolPolicy],
                         environment: Optional[str]) -> CallToolResult:
  """A bc_actions_search answer without the actions the policy refuses.

  Business Central answers with a sentence followed by a JSON array of action
  names. An answer in another shape is passed on untouched: the policy still
  refuses the call itself, so nothing is lost but the tidiness.
  """
  if policy is None or result.isError:
    return result
  content = []
  changed = False
  for block in result.content:
    if not isinstance(block, TextContent):
      content.append(block)
      continue
    match = _NAME_LIST.search(block.text)
    names: Any = None
    if match is not None:
      try:
        names = json.loads(match.group(0))
      except ValueError:
        names = None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
      content.append(block)
      continue
    kept = [n for n in names if policy.refusal(n, environment) is None]
    if len(kept) == len(names):
      content.append(block)
      continue
    changed = True
    text = block.text[:match.start()] + json.dumps(kept) + block.text[match.end():]
    content.append(block.model_copy(update={"text": text}))
  if not changed:
    return result
  return result.model_copy(update={"content": content})


__all__ = ["ACTION_ARGUMENT", "ACTION_TOOLS", "SEARCH_TOOL", "ToolPolicy", "action_of", "filter_search_result",
           "filter_tools"]
