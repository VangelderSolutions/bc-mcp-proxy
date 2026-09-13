"""Permission awareness for the Business Central MCP proxy.

Business Central enforces permissions itself: every MCP tool call runs under
the signed-in user's permission sets and security filters, and a call the
user may not make is refused. What BC does *not* do is say so clearly. The
refusal arrives as a tool result with `isError: true` and a JSON body whose
code is the generic `Internal_ServerError`; only the message names the
object and the missing permission:

    {"error": {"code": "Internal_ServerError",
               "message": "Sorry, the current permissions prevented the
                           action. (TableData 5200 Employee Read: _Exclude_APIV2_)"}}

Measured on BC 28.0 (see tests/fixtures/bc_permission_errors.json and
docs/security-model.md): the same payload comes back from static `List…_PAG…`
tools and from `bc_actions_invoke`; without Execute on the API page the
object is `(Page 30009 APIV2 - Customers Execute: …)`; keys are lower-case
(other BC payloads use `Error`/`Code`/`Message`). The message text is
localised to the user's language; the bracketed object reference is not.

This module gives the proxy two things:

* `detect_permission_denied` / `annotate_permission_denied`: recognise the
  refusal and append a note that tells the AI client it is looking at a
  Business Central permission decision, so it explains instead of retrying
  or blaming the configuration.
* `PermissionRegistry` + `probe_static_permissions`: optionally read one
  record from every static List tool after connecting and hide the tools
  (and their write siblings) for pages the user cannot read. Business
  Central stays the enforcer; hiding is a courtesy to the client, never a
  security boundary.
* `read_guard_permissions`: when the environment runs the companion app
  MCP Guard (Vangelder Solutions, free on AppSource), its
  `effectivePermissions` API page answers the same question in one call
  and also knows about write permissions and page Execute, so the write
  tools can be hidden per operation. The guard sees empty tables the user
  may not read (a probe cannot); it does not see tables a page reads in code
  besides its source table, so a live denial still hides a page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

# BC's MCP server names static-mode tools after the API page and operation
# (Microsoft Learn, "Configure Business Central MCP Server"):
#   List<object>_PAG<id> / Create<object>_PAG<id> / ListUpdate<object>_PAG<id> /
#   Delete<object>_PAG<id> / <boundAction>_PAG<id>.
# ListUpdate must come before List so the alternation cannot read
# "ListUpdateX" as List + "UpdateX".
STATIC_TOOL_RE = re.compile(r"^(?P<verb>ListUpdate|List|Create|Delete)?(?P<rest>.+?)_PAG(?P<id>\d+)$")

# Object kinds BC names in a permission error, as they appear on the wire.
_OBJECT_KINDS = "TableData|Table|Page|Codeunit|Report|Query|XMLport|System"
_PERMISSIONS = "Read|Insert|Modify|Delete|Execute"

# "(TableData 5200 Employee Read: _Exclude_APIV2_)" — the bracketed object
# reference BC appends to a permission refusal. Not localised.
_OBJECT_REF_RE = re.compile(
    rf"\((?P<kind>{_OBJECT_KINDS}) (?P<id>\d+) (?P<name>.+?) "
    rf"(?P<permission>(?:Indirect)?(?:{_PERMISSIONS})): (?P<app>[^)]*)\)")

# Sentences that identify a permission refusal. The first is what BC 28
# returns through MCP (measured); the second is the classic AL runtime
# wording seen through OData; the third is the older client wording.
_DENIAL_SENTENCES = (
    re.compile(r"current permissions prevented the action", re.IGNORECASE),
    re.compile(
        rf"do not have the following permissions on ({_OBJECT_KINDS}) .+?: ({_PERMISSIONS})",
        re.IGNORECASE),
    re.compile(r"you do not have permission to (read|insert|modify|delete|run)", re.IGNORECASE),
)
# BC's own authorization codes, when it uses one (OData error-code table).
_DENIAL_CODE_PREFIXES = ("Authorization_",)

# Marker at the start of the proxy's note; also the idempotency guard.
NOTE_MARKER = "[bc-mcp-proxy] Business Central refused this call"

# The MCP Guard companion's API page as BC names its static List tool
# (entity set "effectivePermissions"; the page id is whatever the installing
# partner chose).
GUARD_TOOL_RE = re.compile(r"^List_?EffectivePermissions_PAG(?P<id>\d+)$", re.IGNORECASE)
# Static verb -> the guard field that must be true for the tool to be usable.
_VERB_FIELD = {"List": "canRead", "Create": "canInsert", "ListUpdate": "canModify",
               "Delete": "canDelete", "": "canModify"}  # "" = bound action


@dataclass(frozen=True)
class PermissionDenial:
  """What BC refused, as far as the payload says."""

  code: Optional[str]
  message: str
  kind: Optional[str] = None        # "TableData", "Page", ...
  object_id: Optional[int] = None
  object_name: Optional[str] = None
  permission: Optional[str] = None  # "Read", "Execute", "IndirectRead", ...
  app: Optional[str] = None

  @property
  def object_label(self) -> Optional[str]:
    if self.kind is None:
      return None
    return f"{self.kind} {self.object_id} {self.object_name}"


def parse_bc_error(text: str) -> Optional[tuple[Optional[str], Optional[str]]]:
  """Return (code, message) when `text` is a BC JSON error payload.

  Only a top-level `error` / `Error` object counts; record payloads carry
  fields called `code` too (a unit of measure has one), and the regex
  fallback is anchored at the start of the text for the same reason.
  """
  stripped = text.strip()
  if not stripped.startswith("{"):
    return None
  try:
    payload = json.loads(stripped)
  except ValueError:
    payload = None
  if isinstance(payload, dict):
    err = None
    for key, value in payload.items():
      if key.lower() == "error":
        err = value
        break
    if isinstance(err, dict):
      lowered = {k.lower(): v for k, v in err.items()}
      code = lowered.get("code")
      message = lowered.get("message")
      return (str(code) if code is not None else None,
              str(message) if message is not None else None)
    return None
  # Truncated or otherwise unparseable: take the body of a leading error
  # object up to its closing brace, or to the end when that brace is gone.
  match = re.match(r'\s*\{\s*"[Ee]rror"\s*:\s*\{(?P<body>[^}]*)', stripped, re.DOTALL)
  if match is None:
    return None
  body = match.group("body")
  code = re.search(r'"[Cc]ode"\s*:\s*"([^"]*)"', body)
  message = re.search(r'"[Mm]essage"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
  return (code.group(1) if code else None,
          json.loads(f'"{message.group(1)}"') if message else None)


def _denial_from(code: Optional[str], message: Optional[str]) -> Optional[PermissionDenial]:
  text = message or ""
  is_denial = any(p.search(text) for p in _DENIAL_SENTENCES) or bool(
      code and code.startswith(_DENIAL_CODE_PREFIXES))
  if not is_denial:
    return None
  ref = _OBJECT_REF_RE.search(text)
  if ref is None:
    return PermissionDenial(code=code, message=text)
  return PermissionDenial(
      code=code, message=text, kind=ref.group("kind"), object_id=int(ref.group("id")),
      object_name=ref.group("name"), permission=ref.group("permission"),
      app=ref.group("app").strip() or None)


def detect_permission_denied(result: CallToolResult) -> Optional[PermissionDenial]:
  """Return the denial BC reported in `result`, or None.

  Scans every text part, whether or not `isError` is already set. A text
  part that is the proxy's own note is skipped so a result cannot re-trigger
  on itself.
  """
  for item in getattr(result, "content", None) or []:
    text = getattr(item, "text", None)
    if not isinstance(text, str) or not text or text.lstrip().startswith(NOTE_MARKER):
      continue
    parsed = parse_bc_error(text)
    if parsed is not None:
      denial = _denial_from(*parsed)
    else:
      # No JSON error object: accept only the unmistakable sentences, never
      # keyword hits (a permission-set listing contains "Read" and
      # "Permission" in every row).
      denial = _denial_from(None, text) if any(p.search(text) for p in _DENIAL_SENTENCES) else None
    if denial is not None:
      return denial
  return None


def _permission_word(denial: PermissionDenial) -> str:
  permission = denial.permission or "the required"
  if permission.startswith("Indirect"):
    return f"indirect {permission[len('Indirect'):].lower()}"
  return permission.lower() if denial.permission else permission


def permission_note(denial: PermissionDenial, tool_name: str,
                    arguments: Optional[dict[str, Any]] = None) -> str:
  """The text appended to a refused tool result."""
  target = tool_name
  if tool_name == "bc_actions_invoke" and arguments and arguments.get("ActionName"):
    target = f"action {arguments['ActionName']!s} (via bc_actions_invoke)"
  if denial.object_label is not None:
    what = (f"the signed-in user lacks {_permission_word(denial)} permission on "
            f"{denial.object_label}")
  else:
    what = "the signed-in user lacks a permission it needs"
  return (
      f"{NOTE_MARKER} to {target}: {what}. "
      "This is decided by the user's permission sets and security filters in "
      "Business Central, not by this proxy, the MCP configuration or the sign-in; "
      "retrying, changing settings or re-authenticating will not change it. "
      "Business Central enforces the same limit in its own client. Ask a "
      "Business Central administrator to grant the permission if the user "
      "should have it; otherwise treat the data as out of scope for this user."
  )


def annotate_permission_denied(
    result: CallToolResult,
    tool_name: str,
    arguments: Optional[dict[str, Any]] = None,
    denial: Optional[PermissionDenial] = None,
) -> CallToolResult:
  """Append the explanatory note and set isError. Unrelated results and
  results that already carry the note are returned unchanged (same object)."""
  if denial is None:
    denial = detect_permission_denied(result)
  if denial is None:
    return result
  content = list(getattr(result, "content", None) or [])
  for item in content:
    text = getattr(item, "text", None)
    if isinstance(text, str) and text.lstrip().startswith(NOTE_MARKER):
      return result
  content.append(TextContent(type="text", text="\n\n" + permission_note(denial, tool_name, arguments)))
  return result.model_copy(update={"content": content, "isError": True})


# --- static-mode tool hiding ------------------------------------------------


def static_page_id(tool_name: str) -> Optional[str]:
  """'List_Customers_PAG30009' -> '30009'; None for dynamic-mode / unknown names."""
  match = STATIC_TOOL_RE.match(tool_name)
  return match.group("id") if match else None


def static_verb(tool_name: str) -> Optional[str]:
  """'List' | 'Create' | 'ListUpdate' | 'Delete' | '' (bound action) | None."""
  match = STATIC_TOOL_RE.match(tool_name)
  if match is None:
    return None
  return match.group("verb") or ""


def probe_arguments(tool: Tool) -> dict[str, Any]:
  """Smallest read the tool's schema allows: `top`-style paging set to 1."""
  props = (getattr(tool, "inputSchema", None) or {}).get("properties") or {}
  for key in ("top", "$top", "Top"):
    if key in props:
      return {key: 1}
  return {}


class PermissionRegistry:
  """Verdicts per API page id, applied as a read-side filter on tools/list.

  `filter()` returns the very same object when nothing is hidden, so cache
  tiers can compare identity; `replace()` swaps the verdict set atomically.
  Unknown pages (not yet probed, transient error) are always visible."""

  def __init__(self) -> None:
    self._denied: dict[str, PermissionDenial] = {}
    self._allowed: set[str] = set()
    # Per page: static verbs hidden on their own (write tools the guard says
    # the user lacks a permission for, while the page stays readable).
    self._denied_verbs: dict[str, set[str]] = {}
    self._source: str = "none"

  @property
  def denied(self) -> dict[str, PermissionDenial]:
    return dict(self._denied)

  @property
  def allowed(self) -> frozenset[str]:
    return frozenset(self._allowed)

  @property
  def denied_verbs(self) -> dict[str, set[str]]:
    return {k: set(v) for k, v in self._denied_verbs.items()}

  @property
  def source(self) -> str:
    return self._source

  def replace(self, denied: dict[str, PermissionDenial], allowed: set[str],
              denied_verbs: Optional[dict[str, set[str]]] = None,
              source: str = "probe") -> None:
    self._denied, self._allowed = dict(denied), set(allowed)
    self._denied_verbs = {k: set(v) for k, v in (denied_verbs or {}).items() if v}
    self._source = source

  def mark_denied(self, page_id: str, denial: PermissionDenial) -> bool:
    """Record a denial seen on a live call. True when it is new."""
    if page_id in self._denied:
      return False
    self._denied[page_id] = denial
    self._allowed.discard(page_id)
    return True

  def is_hidden(self, tool_name: str) -> bool:
    page_id = static_page_id(tool_name)
    if page_id is None or GUARD_TOOL_RE.match(tool_name):
      return False  # never hide the guard's own page
    if page_id in self._denied:
      return True
    verbs = self._denied_verbs.get(page_id)
    return bool(verbs) and (static_verb(tool_name) or "") in verbs

  def filter(self, result: ListToolsResult) -> ListToolsResult:
    if not self._denied and not self._denied_verbs:
      return result
    tools = list(getattr(result, "tools", None) or [])
    kept = [t for t in tools if not self.is_hidden(t.name)]
    if len(kept) == len(tools):
      return result
    return result.model_copy(update={"tools": kept})

  def summary(self) -> str:
    if not self._denied and not self._denied_verbs:
      return "no tools hidden"
    labels = sorted(
        f"PAG{page_id} ({d.object_label or 'permission refused'})"
        for page_id, d in self._denied.items())
    text = f"{len(labels)} page(s) hidden: {', '.join(labels)}" if labels else "no page hidden entirely"
    if self._denied_verbs:
      writes = sum(len(v) for v in self._denied_verbs.values())
      text += f"; {writes} write tool(s) hidden on {len(self._denied_verbs)} readable page(s)"
    return text


@dataclass
class ProbeOutcome:
  denied: dict[str, PermissionDenial] = field(default_factory=dict)
  allowed: set[str] = field(default_factory=set)
  unknown: int = 0      # timeout / transient / structural error: stays visible
  aborted: bool = False  # upstream session died mid-probe: verdicts incomplete
  denied_verbs: dict[str, set[str]] = field(default_factory=dict)
  source: str = "probe"
  filtered_pages: dict[str, str] = field(default_factory=dict)  # page id -> security filter


def guard_tool_name(tools_result: ListToolsResult) -> Optional[str]:
  """The bc-mcp-guard List tool in this tool list, if the page is exposed."""
  for tool in getattr(tools_result, "tools", None) or []:
    if GUARD_TOOL_RE.match(tool.name):
      return tool.name
  return None


def parse_guard_rows(text: str) -> Optional[list[dict[str, Any]]]:
  """The `value` rows of an effectivePermissions result, or None if the text
  is not such a result (BC prefixes the JSON with a sentence)."""
  start = text.find("{")
  if start < 0:
    return None
  try:
    payload = json.loads(text[start:])
  except ValueError:
    return None
  rows = payload.get("value") if isinstance(payload, dict) else None
  if not isinstance(rows, list):
    return None
  for row in rows:
    if not isinstance(row, dict) or "pageId" not in row or "canRead" not in row:
      return None
  return rows


def verdicts_from_guard(rows: list[dict[str, Any]]) -> ProbeOutcome:
  """Turn guard rows into registry verdicts.

  A page is hidden entirely when the user lacks Execute on the page or Read
  on its source table (both are certain failures at the MCP server). Write
  verbs are hidden individually. `canRead=true` is not recorded as allowed:
  the page may still read a second table the user lacks, and a live denial
  must be able to hide it later.
  """
  outcome = ProbeOutcome(source="bc-mcp-guard")
  for row in rows:
    page_id = str(row.get("pageId"))
    name = str(row.get("pageName") or "")
    table = str(row.get("sourceTableName") or "")
    table_id = row.get("sourceTableId")
    if row.get("canExecute") is False:
      outcome.denied[page_id] = PermissionDenial(
          code=None, message="bc-mcp-guard: no Execute permission on the page",
          kind="Page", object_id=int(page_id) if page_id.isdigit() else None,
          object_name=name, permission="Execute")
      continue
    if row.get("canRead") is False:
      outcome.denied[page_id] = PermissionDenial(
          code=None, message="bc-mcp-guard: no Read permission on the source table",
          kind="TableData", object_id=table_id if isinstance(table_id, int) else None,
          object_name=table or name, permission="Read")
      continue
    hidden = {verb for verb, field_name in _VERB_FIELD.items()
              if verb != "List" and row.get(field_name) is False}
    if hidden:
      outcome.denied_verbs[page_id] = hidden
    if row.get("hasSecurityFilter"):
      outcome.filtered_pages[page_id] = str(row.get("securityFilter") or "")
  return outcome


async def read_guard_permissions(
    call_tool: Callable[[str, dict[str, Any]], Awaitable[CallToolResult]],
    tools_result: ListToolsResult,
    *,
    timeout: float,
    logger: Optional[logging.Logger] = None,
) -> Optional[ProbeOutcome]:
  """One call to bc-mcp-guard's page instead of a probe per List tool.

  Returns None when the page is not in the tool list or the call did not
  yield usable rows (the caller then falls back to the probe)."""
  log = logger or logging.getLogger("bc_mcp_proxy")
  name = guard_tool_name(tools_result)
  if name is None:
    return None
  tool = next(t for t in tools_result.tools if t.name == name)
  args = {key: 5000 for key in probe_arguments(tool)}  # top=5000: every page
  try:
    result = await asyncio.wait_for(call_tool(name, args), timeout)
  except asyncio.CancelledError:
    raise
  except Exception as exc:  # noqa: BLE001 - fall back to the probe
    log.warning("bc-mcp-guard call %s failed (%s); falling back to the probe", name, type(exc).__name__)
    return None
  if detect_permission_denied(result) is not None or getattr(result, "isError", False):
    log.warning("bc-mcp-guard page %s refused for this user; falling back to the probe", name)
    return None
  text = "\n".join(getattr(c, "text", "") for c in (result.content or []) if getattr(c, "text", None))
  rows = parse_guard_rows(text)
  if rows is None:
    log.warning("bc-mcp-guard result from %s not understood; falling back to the probe", name)
    return None
  return verdicts_from_guard(rows)


def _is_session_terminated(exc: BaseException) -> bool:
  data = getattr(exc, "error", None)
  message = str(getattr(data, "message", "") or exc)
  return getattr(data, "code", None) == -32600 or "session terminated" in message.lower()


async def probe_static_permissions(
    call_tool: Callable[[str, dict[str, Any]], Awaitable[CallToolResult]],
    tools_result: ListToolsResult,
    *,
    timeout: float,
    concurrency: int = 3,
    logger: Optional[logging.Logger] = None,
) -> ProbeOutcome:
  """Read one record from every static List tool and classify each page.

  Only `List…_PAG…` tools are called: reads are safe and one denial hides
  the page's Create/ListUpdate/Delete/bound-action siblings too (they share
  the page id). Dynamic-mode configurations expose no such tools, so the
  probe makes no calls there. Concurrency is capped below BC's limit of
  five concurrent requests per user, leaving room for the client's own
  calls. Anything that is not a clear "ok" or a clear denial (timeout,
  rate limit, structural error) leaves the page visible.
  """
  log = logger or logging.getLogger("bc_mcp_proxy")
  outcome = ProbeOutcome()
  targets = [t for t in (getattr(tools_result, "tools", None) or []) if static_verb(t.name) == "List"]
  if not targets:
    return outcome
  semaphore = asyncio.Semaphore(max(1, concurrency))
  abort = asyncio.Event()

  async def one(tool: Tool) -> None:
    page_id = static_page_id(tool.name) or ""
    if abort.is_set():
      return
    async with semaphore:
      if abort.is_set():
        return
      try:
        result = await asyncio.wait_for(call_tool(tool.name, probe_arguments(tool)), timeout)
      except asyncio.TimeoutError:
        outcome.unknown += 1
        return
      except McpError as exc:
        if _is_session_terminated(exc):
          abort.set()
          return
        outcome.unknown += 1
        return
      except asyncio.CancelledError:
        raise
      except Exception as exc:  # noqa: BLE001 - one bad page must not stop the probe
        log.debug("Permission probe: %s raised %s", tool.name, type(exc).__name__)
        outcome.unknown += 1
        return
    denial = detect_permission_denied(result)
    if denial is not None:
      outcome.denied[page_id] = denial
    elif not getattr(result, "isError", False):
      outcome.allowed.add(page_id)
    else:
      outcome.unknown += 1

  await asyncio.gather(*(one(t) for t in targets))
  outcome.aborted = abort.is_set()
  return outcome
