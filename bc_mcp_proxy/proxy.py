from __future__ import annotations

import asyncio
import base64
import dataclasses
import email.utils
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolResult,
    ErrorData,
    GetPromptResult,
    Implementation,
    ListPromptsResult,
    ListResourcesResult,
    ListToolsResult,
    ReadResourceRequest,
    ServerCapabilities,
    ServerResult,
    TextContent,
    Tool,
    ToolAnnotations,
)

import os

from . import tools_cache
from .auth import TokenProvider, create_token_provider
from .companies import (
    LIST_COMPANIES_TOOL,
    CompanyDirectory,
    add_company_switch,
    company_error,
    pop_company,
)
from .config import V27_SCOPE, ProxyConfig, is_v28_endpoint, validate_base_url
from .permissions import (
    STATIC_TOOL_RE,
    PermissionRegistry,
    annotate_permission_denied,
    detect_permission_denied,
    probe_static_permissions,
    read_guard_permissions,
    static_page_id,
    static_verb,
)

# Re-exported for backward compatibility — older callers (and the existing
# v28 endpoint test suite) import _is_v28_endpoint from this module.
_is_v28_endpoint = is_v28_endpoint

try:
  # Python 3.11+
  _BaseExceptionGroup = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:  # pragma: no cover - exercised only on 3.10
  from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup  # type: ignore[no-redef]

# httpx errors we treat as recoverable upstream blips and retry through.
_RECOVERABLE_HTTPX_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

# HTTP statuses Business Central online documents as transient: 429 (rate
# limit -- per user 6000 requests per 5-minute window, 5 concurrent), 503
# (queued request timed out), 408/504 (request ran past the operation
# limit). Microsoft's guidance is to retry with a cool-off period; a
# Retry-After header, when present, is honoured by the reconnect loop.
_RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({408, 429, 503, 504})

DEFAULT_RECONNECT_MAX_ATTEMPTS = 5
DEFAULT_RECONNECT_BASE_BACKOFF = 1.0
DEFAULT_RECONNECT_MAX_BACKOFF = 16.0

# MCP streamable_http client emits this code when BC returns HTTP 404 to a
# tool POST — see mcp/client/streamable_http.py:_send_session_terminated_error.
# BC invalidates its session some time after the original access token
# expires; every reuse of the cached session_id 404s after that point.
_SESSION_TERMINATED_ERROR_CODE = 32600


class _UpstreamSessionExpiredError(Exception):
  """Internal signal that triggers a reconnect from inside _open_and_serve.

  The connection manager treats this as a recoverable error, so the existing
  backoff/reconnect loop reopens the HTTP connection and runs initialize()
  again — which mints a new session_id and (via _AsyncBearerAuth) asks MSAL
  for a fresh access token."""


class _UpstreamConnectRejected(Exception):
  """BC refused the initial handshake itself (raised from initialize()).

  A 404 on the connect POST is mapped by the MCP client lib to
  McpError("Session terminated") rather than an httpx.HTTPStatusError, so
  it must be intercepted here and treated as a *permanent* rejection (wrong
  Environment / ConfigurationName / no access) — not the transient
  mid-session "session terminated" that the reconnect path handles."""

# Substrings that indicate the upstream returned an error message inside a
# successful (isError=False) response. Match is case-insensitive.
_MASKED_ERROR_PATTERNS: tuple[str, ...] = (
    "Authentication_InvalidCredentials",
    "is not enabled",
    "Internal Server Error",
    "BadRequest_NotFound",
    "Bad Request",
)

# BC's error code when a tool call cannot resolve a company. Its message
# ("specify a default company in the service configuration file") is written
# for on-premises NST and is actively misleading for a SaaS environment --
# there is no configuration file to edit, and the Company header is being
# sent. See _annotate_company_not_found.
_COMPANY_NOT_FOUND_CODE = "Internal_CompanyNotFound"


class _AsyncBearerAuth(httpx.Auth):
  """httpx authentication helper that fetches tokens on-demand."""

  def __init__(self, token_provider: TokenProvider) -> None:
    self._token_provider = token_provider

  async def async_auth_flow(self, request: httpx.Request) -> Any:
    token = await self._token_provider.get_token()
    request.headers["Authorization"] = f"Bearer {token}"
    yield request


def _iter_leaf_exceptions(exc: BaseException):
  if isinstance(exc, _BaseExceptionGroup):
    for sub in exc.exceptions:
      yield from _iter_leaf_exceptions(sub)
  else:
    yield exc


def _is_recoverable_upstream_error(exc: BaseException) -> bool:
  """Return True iff every leaf inside `exc` is a recoverable httpx error
  (or the deliberate `_UpstreamSessionExpiredError` reconnect signal).

  The streamablehttp_client transport runs inside an anyio task group, so
  what bubbles out is often an ExceptionGroup wrapping one or more
  httpx errors — or, when we raise our own reconnect signal from inside
  `_open_and_serve`, an ExceptionGroup wrapping a `_UpstreamSessionExpiredError`.
  We treat the bundle as recoverable only when *all* leaves are recoverable
  — a non-recoverable cause (KeyboardInterrupt, an internal AssertionError,
  etc.) must always propagate.
  """
  if isinstance(exc, _UpstreamSessionExpiredError):
    return True
  leaves = list(_iter_leaf_exceptions(exc))
  if not leaves:
    return False
  recoverable_leaf_types = _RECOVERABLE_HTTPX_ERRORS + (_UpstreamSessionExpiredError,)
  return all(
      isinstance(leaf, recoverable_leaf_types)
      or _retryable_status_in_chain(leaf) is not None
      for leaf in leaves
  )


def _status_if_retryable(exc: BaseException) -> Optional[int]:
  if isinstance(exc, httpx.HTTPStatusError):
    status = getattr(exc.response, "status_code", None)
    if status in _RETRYABLE_HTTP_STATUSES:
      return status
  return None


def _retryable_status_in_chain(exc: BaseException) -> Optional[int]:
  """Return the transient HTTP status (429/503/408/504) carried by `exc` or
  by anything in its __cause__/__context__ chain, else None.

  The MCP client lib sometimes wraps the transport's HTTPStatusError in an
  McpError (notably during initialize()), so the status has to be looked for
  along the chain, not only on the leaf itself."""
  seen: set[int] = set()
  current: Optional[BaseException] = exc
  while current is not None and id(current) not in seen:
    seen.add(id(current))
    status = _status_if_retryable(current)
    if status is not None:
      return status
    current = current.__cause__ or current.__context__
  return None


def _retryable_upstream_status(exc: BaseException) -> Optional[int]:
  """First transient HTTP status found across every leaf of `exc`."""
  for leaf in _iter_leaf_exceptions(exc):
    status = _retryable_status_in_chain(leaf)
    if status is not None:
      return status
  return None


def _retry_after_seconds(
    exc: BaseException,
    now: Optional[datetime] = None,
) -> Optional[float]:
  """Parse the Retry-After header from the first transient HTTP error in `exc`.

  Accepts delta-seconds ("30") or an HTTP-date. Returns None when absent,
  unparseable or already in the past, so callers fall back to their own
  backoff."""
  for leaf in _iter_leaf_exceptions(exc):
    current: Optional[BaseException] = leaf
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
      seen.add(id(current))
      if _status_if_retryable(current) is not None:
        header = current.response.headers.get("Retry-After")  # type: ignore[union-attr]
        return _parse_retry_after(header, now)
      current = current.__cause__ or current.__context__
  return None


def _parse_retry_after(header: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
  if not header:
    return None
  value = header.strip()
  if value.isdigit():
    return float(value)
  try:
    when = email.utils.parsedate_to_datetime(value)
  except (TypeError, ValueError, IndexError):
    return None
  if when is None:
    return None
  if when.tzinfo is None:
    when = when.replace(tzinfo=timezone.utc)
  reference = now or datetime.now(timezone.utc)
  delta = (when - reference).total_seconds()
  return delta if delta > 0 else None


def _is_session_terminated_error(exc: BaseException) -> bool:
  """True iff `exc` is the McpError the client lib raises when BC has
  invalidated our server-side session (HTTP 404 on the tool POST)."""
  if not isinstance(exc, McpError):
    return False
  error = getattr(exc, "error", None)
  if error is None:
    return False
  return getattr(error, "code", None) == _SESSION_TERMINATED_ERROR_CODE


def _exception_hints_at_client_cancel(exc: BaseException) -> bool:
  """Heuristic: did this disconnect look like the client cancelled mid-call?

  Claude Desktop's hardcoded 30s timeout fires `notifications/cancelled`,
  the SSE GET stream drops, and the next reconnect attempt sees HTTP 4xx
  because the session id is now invalid. Surfacing that link in logs
  helps users understand the failure mode.
  """
  for leaf in _iter_leaf_exceptions(exc):
    if isinstance(leaf, httpx.HTTPStatusError):
      status = getattr(leaf.response, "status_code", None)
      if (status is not None and 400 <= status < 500
          and status not in _RETRYABLE_HTTP_STATUSES):
        return True
    if isinstance(leaf, httpx.RemoteProtocolError):
      return True
  return False


def _permanent_upstream_status(exc: BaseException) -> Optional[int]:
  """If `exc` carries an httpx 4xx (other than 408/429), return that status.

  A 4xx from Business Central means the request/config/permissions are
  wrong — not a transient blip. Retrying or letting the process crash so
  Claude Desktop respawns it just produces a tight crash-loop into the
  same failure. 429 (rate limit) and 408 (request timeout) are excluded:
  BC documents both as transient and they stay on the recoverable path.
  The transport wraps errors in an anyio ExceptionGroup, so walk every leaf.
  """
  for leaf in _iter_leaf_exceptions(exc):
    if isinstance(leaf, httpx.HTTPStatusError):
      status = getattr(leaf.response, "status_code", None)
      if (isinstance(status, int) and 400 <= status < 500
          and status not in _RETRYABLE_HTTP_STATUSES):
        return status
  return None


def _permanent_rejection_reason(exc: BaseException) -> Optional[str]:
  """Human reason if `exc` is a permanent BC rejection, else None.

  Two shapes, both meaning "your routing/config/permissions are wrong, this
  won't fix itself this process":
    * an httpx 4xx (≠429) — e.g. a wrong ConfigurationName yields HTTP 400;
    * `_UpstreamConnectRejected` / an McpError("Session terminated") raised
      from the initial handshake — a wrong Environment yields HTTP 404,
      which the MCP client lib reports as a terminated session, not a 4xx.
  """
  status = _permanent_upstream_status(exc)
  if status is not None:
    return f"HTTP {status}"
  for leaf in _iter_leaf_exceptions(exc):
    if isinstance(leaf, _UpstreamConnectRejected):
      return ("the connection was not established (commonly HTTP 404 -- the "
              "Environment or MCP Configuration was not found, or the "
              "account lacks access)")
    if _is_session_terminated_error(leaf):
      return ("the session was rejected at connect (commonly HTTP 404 -- the "
              "Environment or MCP Configuration was not found, or the "
              "account lacks access)")
  return None


def _format_upstream_rejection(reason: str, config: ProxyConfig) -> str:
  """A single actionable line for a permanent BC rejection — no traceback.

  The token is valid by the time the request is sent (auth happens first),
  so this is almost always Environment/ConfigurationName/Company/access,
  not sign-in. Echo the effective values so the fix is obvious from the log.
  """
  return (
      f"Business Central rejected the connection ({reason}). "
      "Your sign-in worked -- this is a configuration or permission issue, "
      "not authentication. Check these against the BC admin center: the "
      "Environment name (exact, case-sensitive), the MCP Configuration Name "
      "(required when a named MCP configuration exists -- and easy to leave "
      "blank in the extension settings), the Company name, and that the "
      "signed-in account has access to that environment/configuration. "
      f"Effective config: environment={config.environment!r} "
      f"company={config.company!r} "
      f"configuration_name={config.configuration_name or '<not set>'!r}."
  )


def _detect_masked_error(result: CallToolResult) -> Optional[str]:
  """If `result` claims success but its text content contains a known error
  pattern, return the offending text. Otherwise return None.

  Example: the BC MCP endpoint returns `isError: false` with content
  `"Semantic search is not enabled for this environment"` when the
  feature isn't licensed — clients then treat the failure as a normal
  tool result, hiding the cause from the user.
  """
  if getattr(result, "isError", False):
    return None
  content = getattr(result, "content", None) or []
  for item in content:
    text = getattr(item, "text", None)
    if not isinstance(text, str) or not text:
      continue
    lowered = text.lower()
    for pattern in _MASKED_ERROR_PATTERNS:
      if pattern.lower() in lowered:
        return text
  return None


def _flag_as_error(result: CallToolResult) -> CallToolResult:
  """Return a CallToolResult with isError=True, preserving content."""
  return result.model_copy(update={"isError": True})


def _result_text(result: CallToolResult) -> str:
  """Concatenate the text parts of a tool result (non-text parts ignored)."""
  parts = []
  for item in getattr(result, "content", None) or []:
    text = getattr(item, "text", None)
    if isinstance(text, str) and text:
      parts.append(text)
  return "\n".join(parts)


def _annotate_company_not_found(
    result: CallToolResult,
    config: ProxyConfig,
) -> CallToolResult:
  """Append an accurate explanation when BC answers Internal_CompanyNotFound.

  BC's own message says to "specify a default company in the service
  configuration file", which is on-premises advice: an online environment has
  no such file. The instinct it produces is to go and change the configured
  company name, and that has cost real debugging time against a configuration
  that was correct all along.

  What was measured (17 Aug 2026, both a v28 and a v26/v27 endpoint, headers
  logged on the wire): the Company header rides on the httpx client's default
  headers, so it is present on *every* request including the tool-call POST,
  not only the connect POST; a company that does not exist is rejected at
  connect with a 404 instead, so reaching this error means the name resolved
  once already; and the failure appears and clears on its own, hitting every
  company and every environment on the tenant at the same time. So it is not
  the configured name. Say that here rather than let the next reader re-derive
  it from a message written for a different product shape.
  """
  text = _result_text(result)
  if _COMPANY_NOT_FOUND_CODE.lower() not in text.lower():
    return result
  note = (
      f"\n\n[bc-mcp-proxy] Business Central could not resolve a company for "
      f"this call. Effective config: environment={config.environment!r} "
      f"company={config.company!r} "
      f"configuration_name={config.configuration_name or '<not set>'!r}.\n"
      "Before changing any of the above, note what this error does NOT mean. "
      "The Company header is sent on every request, including this one. A "
      "company name that does not exist is rejected earlier, at connect, with "
      "a 404 -- so the name resolved at least once for this session. And BC's "
      "advice to edit a 'service configuration file' applies to on-premises "
      "installations only; there is no such file for an online environment.\n"
      "This has been observed as a transient server-side fault that takes out "
      "every company and every environment on a tenant at once, and clears on "
      "its own. Reading the same data in the web client meanwhile works. If it "
      "persists, that is worth reporting to Microsoft rather than reconfiguring."
  )
  content = list(getattr(result, "content", None) or [])
  content.append(TextContent(type="text", text=note))
  return result.model_copy(update={"content": content, "isError": True})


def _backoff_for_attempt(
    zero_based_attempt: int,
    base: float = DEFAULT_RECONNECT_BASE_BACKOFF,
    max_value: float = DEFAULT_RECONNECT_MAX_BACKOFF,
) -> float:
  """1.0, 2.0, 4.0, 8.0, 16.0, 16.0, ... — capped at max_value."""
  if zero_based_attempt < 0:
    return base
  return min(base * (2 ** zero_based_attempt), max_value)


def _tools_signature(result: Optional[ListToolsResult]) -> int:
  """Order-independent fingerprint of a tools/list result.

  Used to decide whether the tool set the client currently holds differs
  from a freshly fetched one — i.e. whether a tools/list_changed push is
  warranted. Keyed on the sorted tool names; an empty list (the cold-start
  placeholder) hashes distinctly from any populated list."""
  tools = getattr(result, "tools", None) or []
  names = tuple(sorted(getattr(t, "name", "") for t in tools))
  return hash(names)


# BC's MCP server names its tools two ways (Microsoft Learn, "Configure
# Business Central MCP Server"): in dynamic tool mode three system tools,
# and in static mode one tool per allowed operation on an API page:
#   List<object>_PAG<id>        read
#   Create<object>_PAG<id>      write
#   ListUpdate<object>_PAG<id>  write
#   Delete<object>_PAG<id>      write
#   <boundAction>_PAG<id>       write (posting, status changes, codeunits)
_DYNAMIC_READ_TOOLS: frozenset[str] = frozenset({"bc_actions_search", "bc_actions_describe"})
_DYNAMIC_WRITE_TOOLS: frozenset[str] = frozenset({"bc_actions_invoke"})
_DYNAMIC_TITLES: dict[str, str] = {
    "bc_actions_search": "Search Business Central actions",
    "bc_actions_describe": "Describe a Business Central action",
    "bc_actions_invoke": "Invoke a Business Central action",
}
# The regex lives in permissions.py (shared with the tool-hiding filter);
# kept under this name for scripts that import it from here.
_STATIC_TOOL_RE = STATIC_TOOL_RE
_STATIC_VERB_WORDS: dict[str, str] = {
    "List": "List", "Create": "Create", "ListUpdate": "Update", "Delete": "Delete",
}
# Anthropic's directory requires tool names of at most 64 characters.
_MAX_TOOL_NAME_LENGTH = 64


def _classify_tool(name: str) -> Optional[tuple[str, str]]:
  """Return ("read" | "write", derived title) for a BC-shaped tool name.

  Unknown names return None and are forwarded untouched; guessing wrong on
  a tool we do not recognise would be worse than leaving it unannotated."""
  if name in _DYNAMIC_READ_TOOLS:
    return "read", _DYNAMIC_TITLES[name]
  if name in _DYNAMIC_WRITE_TOOLS:
    return "write", _DYNAMIC_TITLES[name]
  match = _STATIC_TOOL_RE.match(name)
  if match is None:
    return None
  verb = match.group("verb")
  subject = match.group("rest").strip(" -_")
  if verb is None:
    # A bound action: BC exposes it only when "Allow Bound Actions" is on,
    # and bound actions post documents / run business logic.
    return "write", subject
  return ("read" if verb == "List" else "write"), f"{_STATIC_VERB_WORDS[verb]} {subject}"


def _annotate_tool(tool: Tool) -> Tool:
  classified = _classify_tool(tool.name)
  if classified is None:
    return tool
  kind, title = classified
  read_only = kind == "read"
  existing = tool.annotations
  updates: dict[str, Any] = {}
  if existing is None or existing.readOnlyHint is None:
    updates["readOnlyHint"] = read_only
  if existing is None or existing.destructiveHint is None:
    updates["destructiveHint"] = not read_only
  if existing is None or existing.title is None:
    updates["title"] = tool.title or title
  if existing is None:
    annotations = ToolAnnotations(**updates)
  elif updates:
    annotations = existing.model_copy(update=updates)
  else:
    annotations = existing
  tool_updates: dict[str, Any] = {}
  if annotations is not existing:
    tool_updates["annotations"] = annotations
  if tool.title is None:
    tool_updates["title"] = annotations.title or title
  return tool.model_copy(update=tool_updates) if tool_updates else tool


def _annotate_tools(result: ListToolsResult) -> ListToolsResult:
  """Fill in `title`, `readOnlyHint` and `destructiveHint` where BC left them
  out, based on the tool naming documented for the BC MCP server.

  Only missing fields are filled; anything BC sends is kept verbatim. Claude
  uses the hints for auto-permissions (read-only tools run without a per-call
  confirmation, destructive ones always prompt) and Anthropic's directory
  review requires them on every tool. Idempotent, so the same result can pass
  through the in-memory and on-disk cache tiers any number of times.

  Caveat: `bc_actions_invoke` (dynamic tool mode) executes reads and writes
  through one tool; marking it destructive is the conservative choice."""
  tools = list(getattr(result, "tools", None) or [])
  if not tools:
    return result
  annotated = [_annotate_tool(t) for t in tools]
  long_names = [t.name for t in tools if len(t.name) > _MAX_TOOL_NAME_LENGTH]
  if long_names:
    logging.getLogger("bc_mcp_proxy").warning(
        "%d tool name(s) exceed %d characters (e.g. %r); Anthropic's directory "
        "rejects such tools -- shorten the API page name in Business Central",
        len(long_names), _MAX_TOOL_NAME_LENGTH, long_names[0],
    )
  if all(a is t for a, t in zip(annotated, tools)):
    return result
  return result.model_copy(update={"tools": annotated})


class _ClientNotifier:
  """Bridges the background upstream pre-warm to the connected MCP client.

  The stdio request handlers run inside an MCP request context (where
  `server.request_context.session` is valid); the upstream pre-warm task
  does not. We capture the ServerSession from the first request handler
  call so the pre-warm can later push `notifications/tools/list_changed`
  when the tool set transitions — most importantly empty placeholder ->
  real list once first-run auth completes, so the client refetches without
  a restart."""

  def __init__(self, logger: logging.Logger) -> None:
    self._session: Any = None
    self._logger = logger
    self._last_signature: Optional[int] = None

  def capture(self, session: Any) -> None:
    if self._session is None and session is not None:
      self._session = session

  def record_served(self, result: Optional[ListToolsResult]) -> None:
    """Remember what the client now holds so we only notify on real change."""
    self._last_signature = _tools_signature(result)

  async def maybe_notify(self, result: Optional[ListToolsResult]) -> None:
    signature = _tools_signature(result)
    if signature == self._last_signature:
      return
    self._last_signature = signature
    if self._session is None:
      # No client request has happened yet; the client will pick up the
      # fresh list on its first tools/list call, so no push is needed.
      return
    try:
      await self._session.send_tool_list_changed()
      self._logger.debug("Pushed notifications/tools/list_changed to client")
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
      self._logger.warning(
          "Failed to push tools/list_changed (%s); client will refresh on its "
          "next tools/list", type(exc).__name__,
      )


class _ToolsCache:
  """In-memory tools/list cache shared between stdio handler and upstream
  pre-warm. Reads are lock-free; writes use a lock so concurrent refreshers
  can't interleave a partial state."""

  def __init__(
      self,
      ttl_seconds: float,
      annotate: bool = False,
      read_filter: Optional[Callable[[ListToolsResult], ListToolsResult]] = None,
      company_switch: bool = False,
  ) -> None:
    self._ttl = ttl_seconds
    self._annotate = annotate
    self._company_switch = company_switch
    # Applied on every read, never on store: the stored (and on-disk) list
    # stays complete because the disk cache is keyed per tenant/env/company/
    # configuration, not per user, and verdicts belong to one user.
    self._read_filter = read_filter
    self._result: Optional[ListToolsResult] = None
    self._fetched_at: float = 0.0
    self._lock = asyncio.Lock()

  def _view(self, result: Optional[ListToolsResult]) -> Optional[ListToolsResult]:
    if result is None or self._read_filter is None:
      return result
    return self._read_filter(result)

  def get_fresh(self, now: Optional[float] = None) -> Optional[ListToolsResult]:
    if self._result is None:
      return None
    if (now or time.monotonic()) - self._fetched_at > self._ttl:
      return None
    return self._view(self._result)

  def get_any(self) -> Optional[ListToolsResult]:
    return self._view(self._result)

  def get_unfiltered(self) -> Optional[ListToolsResult]:
    return self._result

  def store(self, result: ListToolsResult, now: Optional[float] = None) -> None:
    # Single choke point for every tier (disk -> memory -> pre-warm ->
    # background refresh), so the client always sees the same annotated set.
    stored = _annotate_tools(result) if self._annotate else result
    if self._company_switch:
      stored = add_company_switch(stored)
    self._result = stored
    self._fetched_at = now if now is not None else time.monotonic()

  @property
  def lock(self) -> asyncio.Lock:
    return self._lock


class _UpstreamSessionHolder:
  """Thread-safe-ish holder for the currently active upstream ClientSession.

  list_tools / call_tool callbacks await wait_active() and then call into
  the session. While a reconnect is in progress, set_session has been
  cleared and waiters block until the new session is up.
  """

  def __init__(self) -> None:
    self._session: Optional[ClientSession] = None
    self._get_session_id: Optional[Callable[[], Optional[str]]] = None
    # What the upstream declared at initialize(); drives whether resources/*
    # and prompts/* are forwarded or answered locally with an empty list.
    self._capabilities: Optional[ServerCapabilities] = None
    self._ready = asyncio.Event()
    # Set once the upstream has permanently failed (a 4xx that won't fix
    # itself this process). Sticky: never cleared, so waiters surface it
    # instead of hanging forever or crash-looping.
    self._fatal: Optional[McpError] = None

  @property
  def fatal(self) -> Optional[McpError]:
    return self._fatal

  def set_fatal(self, error: McpError) -> None:
    self._fatal = error
    # Wake any wait_active() waiters so they raise the error promptly.
    self._ready.set()

  def set_session(
      self,
      session: ClientSession,
      get_session_id: Callable[[], Optional[str]],
      capabilities: Optional[ServerCapabilities] = None,
  ) -> None:
    self._session = session
    self._get_session_id = get_session_id
    self._capabilities = capabilities
    self._ready.set()

  def clear_session(self) -> None:
    self._session = None
    self._get_session_id = None
    self._capabilities = None
    self._ready.clear()

  @property
  def upstream_capabilities(self) -> Optional[ServerCapabilities]:
    return self._capabilities

  def upstream_supports(self, capability: str) -> bool:
    """True if the live upstream session advertised `capability`
    ("resources", "prompts", "tools", ...)."""
    caps = self._capabilities
    return caps is not None and getattr(caps, capability, None) is not None

  async def wait_active(self) -> ClientSession:
    while True:
      if self._fatal is not None:
        raise self._fatal
      if self._session is not None:
        return self._session
      await self._ready.wait()

  def session_id(self) -> Optional[str]:
    if self._get_session_id is None:
      return None
    try:
      return self._get_session_id()
    except Exception:  # pragma: no cover - defensive
      return None


class _UpstreamConnectionManager:
  """Owns the upstream connection and reconnects on transient httpx errors.

  Exposes `_open_and_serve` as a hook so tests can substitute a fake
  connection routine without mocking the full streamable-http stack.
  """

  def __init__(
      self,
      *,
      state: _UpstreamSessionHolder,
      config: ProxyConfig,
      url: str,
      headers: dict[str, str],
      auth: httpx.Auth,
      logger: logging.Logger,
      tools_cache_obj: Optional[_ToolsCache] = None,
      notifier: Optional[_ClientNotifier] = None,
      permission_registry: Optional[PermissionRegistry] = None,
      max_attempts: int = DEFAULT_RECONNECT_MAX_ATTEMPTS,
      base_backoff: float = DEFAULT_RECONNECT_BASE_BACKOFF,
      max_backoff: float = DEFAULT_RECONNECT_MAX_BACKOFF,
      sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
      disk_cache: bool = True,
  ) -> None:
    self.state = state
    self.config = config
    self.url = url
    self.headers = headers
    self.auth = auth
    self.logger = logger
    self.tools_cache_obj = tools_cache_obj
    # The on-disk tools/list cache is keyed per tenant/environment/company/
    # configuration, not per user: right for one user per process, wrong for
    # a hosted server, which turns it off.
    self.disk_cache = disk_cache
    self.notifier = notifier
    self.permission_registry = permission_registry
    self.max_attempts = max_attempts
    self.base_backoff = base_backoff
    self.max_backoff = max_backoff
    self.sleep = sleep
    self._attempt = 0
    # Set by request_reconnect() to break out of the in-serve wait below
    # and force the run-loop to reopen the upstream connection.
    self._reconnect_requested = asyncio.Event()
    self._reconnect_reason: str = ""

  def request_reconnect(self, *, reason: str) -> None:
    """Tear down the current upstream and reconnect on the next loop iteration.

    Called when an in-flight tool call discovers the server-side session has
    been invalidated (BC returns 404 / "Session terminated"). Clearing the
    holder up-front makes any concurrent waiters block on `wait_active()`
    until `_open_and_serve` brings the new session online.
    """
    self._reconnect_reason = reason
    self.state.clear_session()
    self._reconnect_requested.set()

  async def run(self) -> None:
    while True:
      try:
        await self._open_and_serve()
        return  # graceful shutdown — upstream closed without error.
      except asyncio.CancelledError:
        self.state.clear_session()
        raise
      except BaseException as exc:
        rejection = _permanent_rejection_reason(exc)
        if rejection is not None:
          # BC permanently rejected us (4xx, or a 404 surfaced as a
          # terminated session at connect). Don't crash (Claude Desktop
          # would just respawn into the same failure) and don't retry
          # (config won't change mid-process). Log ONE actionable line,
          # record a sticky fatal so the stdio handlers surface it to the
          # client, and park so the local server keeps serving until the
          # client disconnects (which cancels this task).
          self.state.clear_session()
          message = _format_upstream_rejection(rejection, self.config)
          self.logger.error(message)
          self.state.set_fatal(
              McpError(ErrorData(code=INTERNAL_ERROR, message=message)))
          await asyncio.Event().wait()
          return  # pragma: no cover - only on cancellation, which re-raises
        if not _is_recoverable_upstream_error(exc):
          self.state.clear_session()
          raise
        session_id = self.state.session_id()
        self.state.clear_session()
        self._attempt += 1
        if self._attempt >= self.max_attempts:
          self.logger.error(
              "Upstream reconnect gave up after %d attempts: %s",
              self._attempt, exc,
          )
          raise
        backoff = _backoff_for_attempt(
            self._attempt - 1, self.base_backoff, self.max_backoff,
        )
        transient_status = _retryable_upstream_status(exc)
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
          # Never shorter than our own step, never longer than the cap: a
          # Retry-After of 60s on a stdio proxy would look like a hang.
          backoff = min(max(retry_after, backoff), self.max_backoff)
        hint = (
            " -- possible client-side cancellation"
            if _exception_hints_at_client_cancel(exc) else ""
        )
        if transient_status is not None:
          hint += (
              f" -- Business Central answered HTTP {transient_status}"
              + (" (rate limited)" if transient_status == 429 else "")
              + (f", Retry-After={retry_after:g}s" if retry_after is not None else "")
          )
        self.logger.warning(
            "Upstream connection error (%s); session=%s%s; reconnecting in %.1fs (attempt %d/%d)",
            type(exc).__name__,
            session_id or "<none>",
            hint,
            backoff,
            self._attempt,
            self.max_attempts,
        )
        await self.sleep(backoff)

  async def _open_and_serve(self) -> None:
    async with streamablehttp_client(
        url=self.url,
        headers=self.headers,
        timeout=self.config.http_timeout_seconds,
        sse_read_timeout=self.config.sse_timeout_seconds,
        auth=self.auth,
    ) as (remote_read, remote_write, get_session_id):
      client_info = Implementation(
          name=self.config.server_name,
          version=self.config.server_version,
      )
      async with ClientSession(
          remote_read,
          remote_write,
          client_info=client_info,
      ) as remote_session:
        try:
          init_result = await remote_session.initialize()
        except McpError as exc:
          if _retryable_status_in_chain(exc) is not None:
            # A rate limit / transient 5xx during the handshake is not a
            # rejection; let run() see it as recoverable and back off.
            raise
          # A failure during the initial handshake means BC refused the
          # connection itself — most often a 404 (wrong Environment / MCP
          # configuration / no access) which the client lib reports as
          # "Session terminated". Convert it so run() classifies it as a
          # permanent rejection (clean error + stay alive) instead of a
          # crash-loop. This is connect-time only — the mid-session
          # terminate is handled by _invoke_with_session_recovery and
          # never reaches here.
          raise _UpstreamConnectRejected(str(exc)) from exc
        capabilities = getattr(init_result, "capabilities", None)
        server_info = getattr(init_result, "serverInfo", None)
        self.logger.info(
            "Connected to Business Central MCP server %s %s (protocol %s; "
            "tools=%s resources=%s prompts=%s)",
            getattr(server_info, "name", "?"),
            getattr(server_info, "version", "?"),
            init_result.protocolVersion,
            getattr(capabilities, "tools", None) is not None,
            getattr(capabilities, "resources", None) is not None,
            getattr(capabilities, "prompts", None) is not None,
        )

        # Pre-warm tools/list before exposing the session so the stdio
        # handler can answer Claude's first request from cache instead of
        # racing BC's cold-start. If pre-warm fails for any reason, fall
        # back to the existing behaviour — set the session active and
        # let the stdio handler hit upstream lazily.
        tools_result: Optional[ListToolsResult] = None
        if self.tools_cache_obj is not None:
          try:
            tools_result = await remote_session.list_tools()
            self.tools_cache_obj.store(tools_result)
            if self.disk_cache:
              tools_cache.save_disk_cache(self.config, tools_result)
            self.logger.info(
                "Pre-warmed tools/list cache (%d tools)",
                len(getattr(tools_result, "tools", []) or []),
            )
            # Cold first run: the client was handed an empty placeholder
            # list while auth was pending. Now that real tools exist, push
            # tools/list_changed so it refetches without a restart.
            if self.notifier is not None:
              await self.notifier.maybe_notify(tools_result)
          except Exception as exc:
            # Log but don't propagate — a warmed cache is best-effort.
            self.logger.warning(
                "tools/list pre-warm failed (%s); cache stays cold",
                type(exc).__name__,
            )

        self.state.set_session(remote_session, get_session_id, capabilities)
        # Each successful init resets the retry budget; subsequent failures
        # start the backoff sequence over.
        self._attempt = 0
        probe_task: Optional[asyncio.Task[None]] = None
        if self.permission_registry is not None and tools_result is not None:
          # Runs beside the client's own calls; verdicts from a previous
          # session stay in force until this probe replaces them.
          probe_task = asyncio.create_task(
              self._probe_permissions(remote_session, tools_result),
              name="bc-mcp-permission-probe")
        try:
          # Park here until the session dies on its own (the upstream raises
          # out from under us) or _call_tool calls request_reconnect() because
          # BC told us the session is gone. Either way, raising on wakeup lets
          # the surrounding async-with's run their cleanup before run() retries.
          self._reconnect_requested.clear()
          await self._reconnect_requested.wait()
        finally:
          if probe_task is not None and not probe_task.done():
            probe_task.cancel()
        reason = self._reconnect_reason or "reconnect requested"
        self._reconnect_reason = ""
        raise _UpstreamSessionExpiredError(reason)

  async def _probe_permissions(
      self, session: ClientSession, tools_result: ListToolsResult) -> None:
    """Hide static tools for pages the user cannot read (opt-in)."""
    registry = self.permission_registry
    assert registry is not None
    call = lambda name, args: session.call_tool(name, args)  # noqa: E731
    try:
      outcome = await read_guard_permissions(
          call, tools_result, timeout=self.config.http_timeout_seconds, logger=self.logger)
      if outcome is not None:
        self.logger.info("Using effectivePermissions from bc-mcp-guard (one call, write verdicts included)")
      else:
        outcome = await probe_static_permissions(
            call, tools_result, timeout=self.config.http_timeout_seconds, logger=self.logger)
    except asyncio.CancelledError:
      raise
    except Exception as exc:  # noqa: BLE001 - hiding is best-effort
      self.logger.warning("Permission probe failed (%s); no tools hidden", type(exc).__name__)
      return
    if outcome.aborted:
      self.logger.warning(
          "Permission probe interrupted by an upstream session loss; keeping "
          "previous verdicts (%s)", registry.summary())
      return
    registry.replace(outcome.denied, outcome.allowed, outcome.denied_verbs, source=outcome.source)
    if outcome.source == "bc-mcp-guard":
      self.logger.info("bc-mcp-guard: %d page(s) hidden, %d readable page(s) with write tools hidden -- %s",
                       len(outcome.denied), len(outcome.denied_verbs), registry.summary())
      if outcome.filtered_pages:
        self.logger.info("bc-mcp-guard: security filter active on %d page(s): %s",
                         len(outcome.filtered_pages),
                         ", ".join(f"PAG{p} ({f})" for p, f in sorted(outcome.filtered_pages.items())))
    else:
      self.logger.info(
          "Permission probe: %d page(s) readable, %d without Read permission, "
          "%d undetermined -- %s",
          len(outcome.allowed), len(outcome.denied), outcome.unknown, registry.summary())
    if self.notifier is not None and self.tools_cache_obj is not None:
      # The filtered view differs from what the client holds -> list_changed.
      await self.notifier.maybe_notify(self.tools_cache_obj.get_any())


class _CompanySessions:
  """One upstream connection per company, opened on first use.

  The default company's session is the one run_proxy owns (it fills the
  tools cache and the permission registry); every other company gets a
  plain manager whose only job is to serve tool calls. Sessions live until
  the proxy stops."""

  def __init__(
      self,
      default_company: str,
      factory: Callable[[str], tuple[_UpstreamSessionHolder, _UpstreamConnectionManager]],
      logger: logging.Logger,
  ) -> None:
    self._default = default_company
    self._factory = factory
    self._logger = logger
    self._sessions: dict[str, tuple[_UpstreamSessionHolder, _UpstreamConnectionManager]] = {}
    self._tasks: list[asyncio.Task[None]] = []

  @property
  def default_company(self) -> str:
    return self._default

  def is_default(self, company: str) -> bool:
    return company.strip() == self._default

  def get_or_start(self, company: str) -> tuple[_UpstreamSessionHolder, _UpstreamConnectionManager]:
    key = company.strip()
    if key not in self._sessions:
      holder, manager = self._factory(key)
      self._sessions[key] = (holder, manager)
      self._tasks.append(asyncio.create_task(manager.run(), name=f"bc-mcp-upstream[{key}]"))
      self._logger.info("Opening an upstream session for company %r", key)
    return self._sessions[key]

  @property
  def open_companies(self) -> list[str]:
    return list(self._sessions)

  async def close(self) -> None:
    for task in self._tasks:
      task.cancel()
    if self._tasks:
      await asyncio.gather(*self._tasks, return_exceptions=True)
    self._tasks.clear()


PrepareHook = Callable[[ProxyConfig], Awaitable[ProxyConfig]]
"""Async callback an embedding package passes to run_proxy.

It receives the configuration parsed from the command line / environment and
returns the configuration to connect with (typically a dataclasses.replace of
it: environment, company, configuration_name, allowed_companies, ...). It runs
in the background after the stdio server is up, so it may take as long as it
needs (sign-in, API calls); meanwhile tools/list answers with an empty list and
tool calls wait. An exception becomes the error the client sees on every
request, so raise with a message written for the end user.

Fields read before prepare runs and therefore not changeable by it:
server_name, server_version, instructions, enable_debug and
forward_resources_prompts."""


@dataclasses.dataclass
class RuntimeOptions:
  """What an embedder may supply per runtime instead of the stdio defaults.

  token_provider: bearer tokens for the MCP endpoint (default: MSAL sign-in of
  the user running the process). api_token_provider: bearer tokens for the
  standard API, used by the company directory (default: MSAL with the api.*
  scope). disk_cache: keep the tools/list cache on disk (default on; a hosted
  server that serves many users turns it off, the cache is not keyed per user).
  """

  token_provider: Optional[TokenProvider] = None
  api_token_provider: Optional[TokenProvider] = None
  disk_cache: bool = True


@dataclasses.dataclass
class _Runtime:
  """Everything derived from the effective (post-prepare) configuration."""

  config: ProxyConfig
  url: str
  auth: httpx.Auth
  registry: Optional[PermissionRegistry]
  cache: _ToolsCache
  directory: Optional[CompanyDirectory]
  companies: Optional[_CompanySessions]
  manager: _UpstreamConnectionManager
  disk_cache: bool = True


@dataclasses.dataclass
class RuntimeSlot:
  """Where the runtime of one MCP client lives.

  Created empty while the prepare hook runs; `runtime` is set once the
  effective configuration is known. The stdio proxy has exactly one slot; a
  hosted multi-user server keeps one per authenticated user and hands the
  right one to the handlers through a RuntimeResolver."""

  state: _UpstreamSessionHolder
  notifier: _ClientNotifier
  runtime: Optional[_Runtime] = None

  def get_manager(self) -> _UpstreamConnectionManager:
    """The default upstream manager; only called once a session exists, which
    implies the runtime exists."""
    assert self.runtime is not None
    return self.runtime.manager


RuntimeResolver = Callable[[], Awaitable[RuntimeSlot]]
"""Called at the start of every MCP request handler and returns the slot the
request belongs to. It runs inside the request context, so a hosted server can
look at `server.request_context` (the authenticated HTTP user) to pick it."""


def _build_runtime(
    config: ProxyConfig,
    state: _UpstreamSessionHolder,
    notifier: _ClientNotifier,
    logger: logging.Logger,
    options: Optional[RuntimeOptions] = None,
) -> _Runtime:
  options = options or RuntimeOptions()
  # Defense-in-depth: re-validate the URL at the boundary just before it's
  # handed to the HTTP client. __main__ also validates on startup, but
  # callers that construct ProxyConfig directly (tests, embedders) need
  # this guard too — and using the *returned* sanitized URL (rather than
  # the original) is what lets Snyk's data-flow analysis recognize the
  # sanitization.
  sanitized_base_url = validate_base_url(
      config.base_url,
      allow_non_standard=_env_flag("BC_ALLOW_NON_STANDARD_BASE_URL"),
  )

  token_provider = options.token_provider or create_token_provider(config, logger=logger)

  headers = _build_transport_headers(config)
  url = _build_endpoint_url(config, base_url_override=sanitized_base_url)

  logger.info("Connecting to Business Central MCP endpoint at %s", url)

  auth = _AsyncBearerAuth(token_provider)

  registry = PermissionRegistry() if config.hide_unauthorized_tools else None
  cache = _ToolsCache(
      ttl_seconds=config.tools_cache_ttl_seconds,
      annotate=config.annotate_tools,
      read_filter=registry.filter if registry is not None else None,
      company_switch=config.allow_company_switch,
  )
  directory: Optional[CompanyDirectory] = None
  companies: Optional[_CompanySessions] = None
  if config.allow_company_switch:
    # The company list comes from the standard API, which needs the api.*
    # scope; the same MSAL cache and refresh token serve both scopes.
    api_config = dataclasses.replace(config, token_scope=V27_SCOPE)
    api_provider = options.api_token_provider or create_token_provider(api_config, logger=logger)
    directory = CompanyDirectory(config, api_provider, logger)

    def _start_company(company: str) -> tuple[_UpstreamSessionHolder, _UpstreamConnectionManager]:
      company_config = dataclasses.replace(config, company=company)
      holder = _UpstreamSessionHolder()
      manager = _UpstreamConnectionManager(
          state=holder, config=company_config, url=url,
          headers=_build_transport_headers(company_config), auth=auth, logger=logger)
      return holder, manager

    companies = _CompanySessions((config.company or "").strip(), _start_company, logger)
    logger.info(
        "BC_ALLOW_COMPANY_SWITCH is on: tools accept a 'company' argument and "
        "bc_list_companies lists the user's companies (default company %r)", config.company)
    if config.allowed_companies is not None:
      logger.info("BC_ALLOWED_COMPANIES limits the switch to: %s",
                  ", ".join(config.allowed_companies) or "<none besides the default>")
  if registry is not None:
    logger.info(
        "BC_HIDE_UNAUTHORIZED_TOOLS is on: after each connect the proxy reads "
        "one record from every static List tool and hides the pages Business "
        "Central refuses (Business Central still enforces every call)")

  # Prepopulate the in-memory cache from disk (if a previous run cached
  # tools for this exact tenant/env/company/config). This is the only
  # thing that lets a freshly-launched proxy answer Claude's first
  # tools/list within Claude's 30s window when BC is mid-cold-start.
  disk_cached = tools_cache.load_disk_cache(config) if options.disk_cache else None
  if disk_cached is not None:
    cache.store(disk_cached)
    logger.info(
        "Loaded tools/list from disk cache (%d tools)",
        len(getattr(disk_cached, "tools", []) or []),
    )

  manager = _UpstreamConnectionManager(
      state=state,
      config=config,
      url=url,
      headers=headers,
      auth=auth,
      logger=logger,
      tools_cache_obj=cache,
      notifier=notifier,
      permission_registry=registry,
      disk_cache=options.disk_cache,
  )
  return _Runtime(config=config, url=url, auth=auth, registry=registry, cache=cache,
                  directory=directory, companies=companies, manager=manager,
                  disk_cache=options.disk_cache)


async def _prepare_runtime(
    config: ProxyConfig,
    prepare: PrepareHook,
    state: _UpstreamSessionHolder,
    notifier: _ClientNotifier,
    logger: logging.Logger,
    options: Optional[RuntimeOptions] = None,
) -> Optional[_Runtime]:
  """Run the embedder's prepare hook, then build the runtime from its result.

  Returns None after recording a sticky fatal error when either step fails,
  so the stdio handlers surface the message instead of hanging."""
  try:
    prepared = await prepare(config)
    if not isinstance(prepared, ProxyConfig):
      raise TypeError(f"prepare returned {type(prepared).__name__}, expected ProxyConfig")
    # Positional call kept for embedders and tests that substitute _build_runtime.
    runtime = (_build_runtime(prepared, state, notifier, logger) if options is None
               else _build_runtime(prepared, state, notifier, logger, options))
  except asyncio.CancelledError:
    raise
  except Exception as exc:  # noqa: BLE001 - every failure becomes the client-facing error
    message = str(exc).strip() or f"{type(exc).__name__} while preparing the connection"
    logger.error("Startup preparation failed: %s", message)
    state.set_fatal(McpError(ErrorData(code=INTERNAL_ERROR, message=message)))
    return None
  # The client may already hold the empty placeholder list; if the prepared
  # configuration has tools on disk, let it refetch now instead of after connect.
  await notifier.maybe_notify(runtime.cache.get_any())
  return runtime


def build_server(
    config: ProxyConfig,
    resolve: RuntimeResolver,
    logger: Optional[logging.Logger] = None,
) -> tuple[Server, Any]:
  """Build the MCP server whose handlers serve the runtime that `resolve` returns.

  The stdio proxy resolves to its single slot (see run_proxy); a hosted server
  keeps one slot per authenticated user and resolves on the request context.
  Returns the server and its initialization options (tools.listChanged on).
  Fields read here and therefore fixed per server: server_name, server_version,
  instructions and forward_resources_prompts."""
  logger = logger or logging.getLogger("bc_mcp_proxy")
  instructions = config.instructions or (
      "Bridge MCP stdio clients to Microsoft Dynamics 365 Business Central."
      " All tool definitions and executions are forwarded to the configured Business"
      " Central environment.")
  server = Server(
      name=config.server_name,
      version=config.server_version,
      instructions=instructions,
  )

  @server.list_tools()
  async def _list_tools() -> Any:
    slot = await resolve()
    state, notifier, runtime = slot.state, slot.notifier, slot.runtime
    # Capture the live ServerSession so the background upstream pre-warm
    # can push tools/list_changed once auth completes. request_context is
    # only valid inside a request — which this always is.
    try:
      notifier.capture(server.request_context.session)
    except LookupError:  # pragma: no cover - defensive; always in a request here
      pass

    fatal = state.fatal
    if fatal is not None:
      # Upstream permanently rejected us (4xx). Surface the actionable
      # error rather than an empty or stale list the user can't act on.
      logger.debug("Surfacing fatal upstream error on tools/list")
      raise fatal

    if runtime is None:
      # The prepare hook is still running; same answer as a cold cache.
      logger.info("tools/list requested while the connection is being prepared; "
                  "returning empty list and will push tools/list_changed when ready")
      empty = ListToolsResult(tools=[])
      notifier.record_served(empty)
      return empty
    cache, config_now = runtime.cache, runtime.config

    fresh = cache.get_fresh()
    if fresh is not None:
      logger.debug("Serving tools/list from cache")
      notifier.record_served(fresh)
      return fresh

    stale = cache.get_any()
    if stale is not None:
      # We have something cached but it's beyond the TTL. Serve it now
      # to keep the client unblocked, and refresh in the background.
      logger.debug("Serving stale tools/list; refreshing in background")
      notifier.record_served(stale)
      asyncio.create_task(
          _refresh_tools_cache(state, cache, config_now, logger, notifier,
                               disk_cache=runtime.disk_cache))
      return stale

    # Nothing cached (cold first run, auth almost certainly still pending).
    # Do NOT block on the upstream session here — that is exactly what made
    # the first tools/list hang past Claude's ~30s request timeout. Return
    # an empty list immediately; the upstream pre-warm task will populate
    # the cache and push notifications/tools/list_changed so the client
    # refetches and the tools appear, with no restart.
    logger.info(
        "tools/list requested before upstream is ready; returning empty list "
        "and will push tools/list_changed once authentication completes")
    empty = ListToolsResult(tools=[])
    notifier.record_served(empty)
    return empty

  @server.call_tool()
  async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    slot = await resolve()
    state, notifier = slot.state, slot.notifier
    if slot.runtime is None:
      # Waits out the prepare hook: returns once the default session is up,
      # raises its error if preparation failed.
      await state.wait_active()
    runtime = slot.runtime
    assert runtime is not None
    config, manager, registry, cache = runtime.config, runtime.manager, runtime.registry, runtime.cache
    directory, companies = runtime.directory, runtime.companies
    company: Optional[str] = None
    holder, target_manager = state, manager
    if directory is not None and companies is not None:
      if name == LIST_COMPANIES_TOOL:
        return CallToolResult(content=[TextContent(type="text", text=await directory.describe())])
      requested, arguments = pop_company(arguments)
      if requested is not None and not companies.is_default(requested):
        resolved, known = await directory.resolve(requested)
        if resolved is None:
          names = ", ".join(sorted(c.name for c in known))
          if config.allowed_companies is not None:
            return company_error(
                f"Company '{requested}' is not available for this connection in environment "
                f"'{config.environment}': an administrator limited the companies this connection "
                f"may use (the company may still exist in the environment). Available: {names}.")
          return company_error(
              f"Company '{requested}' does not exist in environment "
              f"'{config.environment}'. Companies: {names}.")
        company = resolved
        holder, target_manager = companies.get_or_start(company)
    logger.debug("Calling tool '%s' (company %s, session %s)", name,
                 company or config.company, holder.session_id() or "<pending>")
    try:
      result = await _invoke_with_session_recovery(
          holder, target_manager, logger,
          f"call_tool[{name}]",
          lambda s: s.call_tool(name, arguments or {}),
      )
    except McpError as exc:
      if company is None:
        raise
      # A company Business Central does not know is rejected at connect;
      # tell the client in the tool result instead of a bare protocol error.
      return company_error(
          f"Business Central refused to open company '{company}' in environment "
          f"'{config.environment}': {exc.error.message} Use bc_list_companies for the exact names.")
    denial = detect_permission_denied(result)
    if denial is not None:
      # Before the masked-error check: a denial that also happens to contain
      # a masked-error pattern deserves the explanation, not just the flag.
      logger.warning(
          "Business Central refused tool '%s' for lack of permission (%s); "
          "see the note appended to the tool result",
          name, denial.object_label or denial.message[:120],
      )
      if registry is not None and static_verb(name) == "List":
        page_id = static_page_id(name)
        if page_id is not None and registry.mark_denied(page_id, denial):
          logger.info("Hiding static tools for PAG%s after a live denial", page_id)
          await notifier.maybe_notify(cache.get_any())
      return annotate_permission_denied(result, name, arguments, denial, company)
    masked = _detect_masked_error(result)
    if masked is not None:
      logger.warning(
          "Upstream returned masked error for tool '%s'; flagging as error: %s",
          name, masked,
      )
      return _flag_as_error(result)
    annotated = _annotate_company_not_found(result, config)
    if annotated is not result:
      logger.warning(
          "Upstream could not resolve a company for tool '%s' "
          "(environment=%s company=%s configuration=%s); the Company header "
          "was sent -- see the appended note in the tool result",
          name, config.environment, config.company,
          config.configuration_name or "<not set>",
      )
    return annotated

  if config.forward_resources_prompts:
    _register_resource_and_prompt_handlers_for(server, resolve, logger)

  # Advertise tools.listChanged so the client honours the
  # notifications/tools/list_changed we push after a cold-start auth.
  # resources/prompts capabilities are advertised automatically when their
  # handlers are registered above.
  init_options = server.create_initialization_options(
      NotificationOptions(tools_changed=True))
  return server, init_options


async def run_slot_upstream(
    slot: RuntimeSlot,
    config: ProxyConfig,
    prepare: Optional[PrepareHook],
    logger: logging.Logger,
    options: Optional[RuntimeOptions] = None,
) -> None:
  """Prepare the slot (when a hook is given) and run its upstream connection
  until cancelled. After a failed preparation it stays parked, so the client
  keeps getting the error instead of a dead connection."""
  if slot.runtime is None:
    if prepare is None:
      raise RuntimeError("the slot has no runtime and no prepare hook to build one")
    slot.runtime = await _prepare_runtime(config, prepare, slot.state, slot.notifier, logger, options)
    if slot.runtime is None:
      # Like a permanent rejection: stay up so the client sees the error.
      await asyncio.Event().wait()
      return  # pragma: no cover - only on cancellation, which re-raises
  await slot.runtime.manager.run()


async def close_slot(slot: RuntimeSlot) -> None:
  """Close the per-company upstream sessions of a slot. The default session
  closes with the task that runs run_slot_upstream."""
  if slot.runtime is not None and slot.runtime.companies is not None:
    await slot.runtime.companies.close()


async def run_proxy(config: ProxyConfig, prepare: Optional[PrepareHook] = None) -> None:
  """Run the stdio proxy until the MCP client disconnects.

  `prepare` lets a package that embeds the proxy decide the effective
  configuration at startup (see PrepareHook). Without it the behaviour is
  exactly that of a plain installation. Embedders that need another transport
  or several users build on build_server, RuntimeSlot and run_slot_upstream
  instead."""
  logger = logging.getLogger("bc_mcp_proxy")
  if config.enable_debug:
    logger.setLevel(logging.DEBUG)

  slot = RuntimeSlot(state=_UpstreamSessionHolder(), notifier=_ClientNotifier(logger))
  # Built up front without a hook (so configuration errors still fail the
  # process at once, as before); built by the upstream task after the hook.
  if prepare is None:
    slot.runtime = _build_runtime(config, slot.state, slot.notifier, logger)

  async def _resolve() -> RuntimeSlot:
    return slot

  server, init_options = build_server(config, _resolve, logger)

  async with stdio_server() as (local_read, local_write):
    upstream_task = asyncio.create_task(
        run_slot_upstream(slot, config, prepare, logger), name="bc-mcp-upstream")
    server_task = asyncio.create_task(
        server.run(local_read, local_write, init_options),
        name="bc-mcp-stdio-server",
    )
    tasks = {upstream_task, server_task}
    try:
      done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
      # Also on cancellation of run_proxy itself (an embedder stopping it).
      pending = [task for task in tasks if not task.done()]
      for task in pending:
        task.cancel()
      await asyncio.gather(*pending, return_exceptions=True)
      await close_slot(slot)
    for task in done:
      task.result()  # re-raise upstream/server failures


def _register_resource_and_prompt_handlers(
    server: Server,
    state: _UpstreamSessionHolder,
    get_manager: Callable[[], _UpstreamConnectionManager],
    logger: logging.Logger,
) -> None:
  """Register resources/* and prompts/* for one fixed upstream.

  Kept for callers that hold a state and a manager getter (the tests among
  them); build_server uses the resolver-based variant below."""
  slot = RuntimeSlot(state=state, notifier=_ClientNotifier(logger))
  slot.get_manager = get_manager  # type: ignore[method-assign]

  async def _resolve() -> RuntimeSlot:
    return slot

  _register_resource_and_prompt_handlers_for(server, _resolve, logger)


def _register_resource_and_prompt_handlers_for(
    server: Server,
    resolve: RuntimeResolver,
    logger: logging.Logger,
) -> None:
  """Forward resources/* and prompts/* to Business Central.

  BC v28+ can return large datasets as embedded resources / file references
  and v29 adds data-query and report tools; a tools-only proxy would leave
  the client unable to follow those references. List calls never block on
  a cold upstream: while the session is not up (or BC did not advertise the
  capability) they answer with an empty list, mirroring tools/list. Reads
  and prompt fetches need the live session and go through the same
  session-terminated recovery as tool calls.
  """

  def _raise_if_fatal(state: _UpstreamSessionHolder) -> None:
    fatal = state.fatal
    if fatal is not None:
      raise fatal

  def _unsupported(capability: str) -> McpError:
    return McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"Business Central did not advertise {capability} for this "
                 "session, so the request cannot be forwarded."),
    ))

  @server.list_resources()
  async def _list_resources() -> Any:
    slot = await resolve()
    _raise_if_fatal(slot.state)
    if not slot.state.upstream_supports("resources"):
      return ListResourcesResult(resources=[])
    return await _invoke_with_session_recovery(
        slot.state, slot.get_manager(), logger, "list_resources",
        lambda s: s.list_resources())

  @server.list_resource_templates()
  async def _list_resource_templates() -> Any:
    slot = await resolve()
    _raise_if_fatal(slot.state)
    if not slot.state.upstream_supports("resources"):
      return []
    result = await _invoke_with_session_recovery(
        slot.state, slot.get_manager(), logger, "list_resource_templates",
        lambda s: s.list_resource_templates())
    return list(getattr(result, "resourceTemplates", None) or [])

  async def _read_resource(req: ReadResourceRequest) -> ServerResult:
    # Registered directly rather than through @server.read_resource(): the
    # SDK decorator (1.27) re-wraps whatever the handler returns into new
    # TextResourceContents/BlobResourceContents keyed on the *request* URI,
    # which drops per-content URIs and mangles multi-part results. BC's
    # ReadResourceResult must reach the client verbatim.
    slot = await resolve()
    _raise_if_fatal(slot.state)
    if not slot.state.upstream_supports("resources"):
      raise _unsupported("resources")
    uri = req.params.uri
    result = await _invoke_with_session_recovery(
        slot.state, slot.get_manager(), logger, f"read_resource[{uri}]",
        lambda s: s.read_resource(uri))
    return ServerResult(result)

  server.request_handlers[ReadResourceRequest] = _read_resource

  @server.list_prompts()
  async def _list_prompts() -> Any:
    slot = await resolve()
    _raise_if_fatal(slot.state)
    if not slot.state.upstream_supports("prompts"):
      return ListPromptsResult(prompts=[])
    return await _invoke_with_session_recovery(
        slot.state, slot.get_manager(), logger, "list_prompts",
        lambda s: s.list_prompts())

  @server.get_prompt()
  async def _get_prompt(name: str, arguments: Optional[dict[str, str]]) -> GetPromptResult:
    slot = await resolve()
    _raise_if_fatal(slot.state)
    if not slot.state.upstream_supports("prompts"):
      raise _unsupported("prompts")
    return await _invoke_with_session_recovery(
        slot.state, slot.get_manager(), logger, f"get_prompt[{name}]",
        lambda s: s.get_prompt(name, arguments))


async def _invoke_with_session_recovery(
    state: _UpstreamSessionHolder,
    manager: _UpstreamConnectionManager,
    logger: logging.Logger,
    operation: str,
    do: Callable[[ClientSession], Awaitable[Any]],
) -> Any:
  """Run `do(session)`, retrying once on a "Session terminated" error.

  When the upstream raises McpError(code=32600), call request_reconnect()
  and try again with the new session. A second failure propagates so the
  client sees it instead of the proxy looping."""
  for attempt in range(2):
    session = await state.wait_active()
    try:
      return await do(session)
    except McpError as exc:
      if attempt == 0 and _is_session_terminated_error(exc):
        logger.warning(
            "Upstream returned 'Session terminated' during %s; "
            "forcing reconnect and retrying once", operation,
        )
        manager.request_reconnect(reason=f"session terminated during {operation}")
        continue
      raise


async def _refresh_tools_cache(
    state: _UpstreamSessionHolder,
    cache: _ToolsCache,
    config: ProxyConfig,
    logger: logging.Logger,
    notifier: Optional[_ClientNotifier] = None,
    *,
    disk_cache: bool = True,
) -> None:
  """Background refresh used when serving a stale cached entry."""
  try:
    session = await state.wait_active()
    async with cache.lock:
      result = await session.list_tools()
      cache.store(result)
      if disk_cache:
        tools_cache.save_disk_cache(config, result)
    logger.debug("Refreshed stale tools/list cache")
    if notifier is not None:
      # If the refreshed set differs from what the client holds, nudge it.
      await notifier.maybe_notify(result)
  except Exception as exc:
    logger.warning("Background tools/list refresh failed: %s", type(exc).__name__)


def _encode_header_value(value: str) -> str:
  """Prepare a Company / ConfigurationName value for the wire.

  BC's MCP server follows MCP SEP-2243: header values that are not pure
  ASCII must be sent as `=?base64?<base64 of the UTF-8 bytes>?=`
  (Microsoft Learn's example: `Cronus Århus A/S` becomes
  `=?base64?Q3JvbnVzIMOFcmh1cyBBL1M=?=`). ASCII values go verbatim.
  Surrounding whitespace is stripped first, so a pasted trailing space is
  neither sent nor base64-encoded into the value.
  """
  stripped = value.strip()
  if stripped.isascii():
    return stripped
  encoded = base64.b64encode(stripped.encode("utf-8")).decode("ascii")
  return f"=?base64?{encoded}?="


def _client_application(config: ProxyConfig) -> str:
  """Value for X-Client-Application; BC telemetry stores it as clientName."""
  version = (config.server_version or "").strip()
  return f"{config.server_name}/{version}" if version else config.server_name


def _build_transport_headers(config: ProxyConfig) -> dict[str, str]:
  headers: dict[str, str] = {
      "X-Client-Application": _client_application(config),
  }
  # Use the configured values literally (stripped), consistent with
  # tenant_id/environment below. The upstream Microsoft sample ran these
  # through urllib.parse.unquote, which silently corrupts any name
  # containing '%' or '+' (e.g. "R&D %1" or "A+B Ltd") — our config comes
  # from .dxt fields / CLI / env entered verbatim, never URL-encoded, so
  # decoding was wrong for this input model. Non-ASCII names are base64
  # encoded per SEP-2243 (see _encode_header_value).
  if config.company:
    headers["Company"] = _encode_header_value(config.company)
  if config.configuration_name:
    headers["ConfigurationName"] = _encode_header_value(config.configuration_name)
  if is_v28_endpoint(config.base_url):
    # The modern host requires routing info in headers because the URL no
    # longer carries the environment in its path. .strip() here is
    # defense-in-depth — __main__._clean already strips at the CLI/env
    # boundary, but callers that build ProxyConfig directly (tests,
    # embedders) bypass that path. A trailing space here surfaces as
    # `LocalProtocolError("Illegal header value …")` from httpx/h11.
    if config.tenant_id:
      headers["TenantId"] = config.tenant_id.strip()
    if config.environment:
      headers["EnvironmentName"] = config.environment.strip()
  return headers


def _build_endpoint_url(config: ProxyConfig, base_url_override: Optional[str] = None) -> str:
  # base_url_override carries a value that has been through validate_base_url();
  # use it whenever provided so the URL flowing into the HTTP client can be
  # traced back to the sanitizer. When called directly (e.g. by tests), fall
  # back to validating config.base_url ourselves so there is no path that
  # forwards an unvalidated URL into the network layer.
  if base_url_override is not None:
    base = base_url_override
  else:
    base = validate_base_url(config.base_url, allow_non_standard=True)
  base = base.rstrip("/")
  if is_v28_endpoint(base):
    # Modern (v28+) host expects the bare URL — no /v2.0/{env}/mcp path.
    return base
  return f"{base}/v2.0/{config.environment}/mcp"


def run_sync(config: ProxyConfig, prepare: Optional[PrepareHook] = None) -> None:
  """Helper to run the proxy from synchronous entry points."""
  asyncio.run(run_proxy(config, prepare))


def _env_flag(name: str) -> bool:
  value = os.getenv(name)
  if value is None:
    return False
  return value.strip().lower() in {"1", "true", "yes", "on"}
