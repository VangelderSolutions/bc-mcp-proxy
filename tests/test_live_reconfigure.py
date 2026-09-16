"""Tests for feeding a new configuration into a running proxy.

An embedding package (BC MCP Enterprise) decides the effective configuration
at startup, but what it decides from -- the user's MCP Guard settings -- can
change while the connection is up. PrepareContext lets the hook hand in the
new result, so bc_list_companies reports it and calls may be routed to a
newly allowed company without the client restarting.

Only the read-only views move: the upstream sessions, the per-company
sessions and the tools cache stay exactly as they were, and a different
default environment or company still needs a restart.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from bc_mcp_proxy import proxy
from bc_mcp_proxy.config import EnvironmentTarget, ProxyConfig
from bc_mcp_proxy.proxy import PrepareContext, RuntimeSlot, _ClientNotifier, _UpstreamSessionHolder

LOGGER = logging.getLogger("t")

SANDBOX_BEFORE = EnvironmentTarget(name="Sandbox-BE", company="CRONUS BE",
                                   configuration_name="BC-MCP-V29",
                                   allowed_companies=("CRONUS BE",))
# What the user sees after adding My Company to their MCP Guard user settings.
SANDBOX_AFTER = dataclasses.replace(SANDBOX_BEFORE,
                                    allowed_companies=("CRONUS BE", "My Company"))


def _cfg(*targets: EnvironmentTarget, **overrides) -> ProxyConfig:
  return dataclasses.replace(
      ProxyConfig(environment="Development-V28", company="CRONUS BE",
                  configuration_name="Demo V28 MCP Dynamic", allow_company_switch=True,
                  allowed_companies=("CRONUS BE",), allow_live_reconfigure=True,
                  environments=targets or None),
      **overrides)


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch):
  """A real runtime with the sign-in and the disk cache stubbed out."""
  from bc_mcp_proxy.auth import StaticTokenProvider

  monkeypatch.setattr(proxy, "create_token_provider",
                      lambda config, logger=None: StaticTokenProvider(token="t"))
  monkeypatch.setattr(proxy, "_AsyncBearerAuth", lambda provider: None)
  monkeypatch.setattr(proxy.tools_cache, "load_disk_cache", lambda config: None)

  def _build(config: ProxyConfig) -> tuple[RuntimeSlot, proxy._Runtime]:
    slot = RuntimeSlot(state=_UpstreamSessionHolder(), notifier=_ClientNotifier(LOGGER))
    slot.runtime = proxy._build_runtime(config, slot.state, slot.notifier, LOGGER,
                                        proxy.RuntimeOptions(disk_cache=False))
    return slot, slot.runtime

  return _build


def _sandbox_limit(runtime: proxy._Runtime) -> tuple[str, ...] | None:
  assert runtime.environments is not None
  return runtime.environments.directory("Sandbox-BE").config.allowed_companies


# -- the flag ------------------------------------------------------------------


def test_the_flag_is_off_by_default() -> None:
  assert ProxyConfig().allow_live_reconfigure is False


async def test_without_the_flag_the_running_connection_is_left_as_it_was(build) -> None:
  slot, runtime = build(_cfg(SANDBOX_BEFORE, allow_live_reconfigure=False))
  before = runtime.directory

  await PrepareContext(slot, LOGGER).reconfigure(_cfg(SANDBOX_AFTER))

  assert _sandbox_limit(runtime) == ("CRONUS BE",)
  assert runtime.directory is before


# -- applying a new configuration ----------------------------------------------


async def test_a_newly_allowed_company_reaches_the_running_connection(build) -> None:
  slot, runtime = build(_cfg(SANDBOX_BEFORE))
  assert _sandbox_limit(runtime) == ("CRONUS BE",)

  await PrepareContext(slot, LOGGER).reconfigure(
      _cfg(SANDBOX_AFTER, allowed_companies=("CRONUS BE", "Demo Nutrisan")))

  assert _sandbox_limit(runtime) == ("CRONUS BE", "My Company")
  # The default environment's limit is the one the call path reads directly.
  assert runtime.config.allowed_companies == ("CRONUS BE", "Demo Nutrisan")


async def test_a_new_note_reaches_the_environment_listing(build) -> None:
  slot, runtime = build(_cfg(SANDBOX_BEFORE, environment_note="old"))

  await PrepareContext(slot, LOGGER).reconfigure(_cfg(SANDBOX_AFTER, environment_note="new"))

  assert runtime.config.environment_note == "new"


async def test_the_open_connections_and_the_tools_cache_are_untouched(build) -> None:
  """A changed allow-list says nothing about the sessions already open."""
  slot, runtime = build(_cfg(SANDBOX_BEFORE))
  manager, companies, cache = runtime.manager, runtime.companies, runtime.cache
  url, auth = runtime.url, runtime.auth

  await PrepareContext(slot, LOGGER).reconfigure(_cfg(SANDBOX_AFTER))

  assert runtime.manager is manager
  assert runtime.companies is companies
  assert runtime.cache is cache
  assert (runtime.url, runtime.auth) == (url, auth)
  # The connection keeps the environment and company it was opened with.
  assert runtime.config.environment == "Development-V28"
  assert runtime.config.company == "CRONUS BE"


# -- ordering and refusals -----------------------------------------------------


async def test_a_reconfigure_that_arrives_before_the_runtime_is_held(build) -> None:
  """A hook may start its background check inside itself, so the first result
  can land before preparation returns."""
  slot = RuntimeSlot(state=_UpstreamSessionHolder(), notifier=_ClientNotifier(LOGGER))
  context = PrepareContext(slot, LOGGER)

  await context.reconfigure(_cfg(SANDBOX_AFTER))  # no runtime yet -> held

  _, runtime = build(_cfg(SANDBOX_BEFORE))
  slot.runtime = runtime
  await context.drain()

  assert _sandbox_limit(runtime) == ("CRONUS BE", "My Company")


async def test_draining_twice_applies_the_held_configuration_once(build) -> None:
  slot, runtime = build(_cfg(SANDBOX_BEFORE))
  context = PrepareContext(slot, LOGGER)
  await context.drain()  # nothing held
  assert _sandbox_limit(runtime) == ("CRONUS BE",)


async def test_a_configuration_without_the_connected_environment_is_refused(build) -> None:
  """Dropping the environment the session runs in would leave the call path
  pointing at a directory that no longer exists; keep what works."""
  slot, runtime = build(_cfg(SANDBOX_BEFORE))
  before = runtime.directory
  elsewhere = EnvironmentTarget(name="Sandbox-FR", company="CRONUS FR")

  await PrepareContext(slot, LOGGER).reconfigure(
      dataclasses.replace(_cfg(elsewhere), environment="Sandbox-FR"))

  assert runtime.directory is before
  assert _sandbox_limit(runtime) == ("CRONUS BE",)


async def test_something_other_than_a_configuration_is_rejected(build) -> None:
  slot, _ = build(_cfg(SANDBOX_BEFORE))
  with pytest.raises(TypeError):
    await PrepareContext(slot, LOGGER).reconfigure({"allowed_companies": ("X",)})  # type: ignore[arg-type]


async def test_without_the_company_switch_there_are_no_views_to_move(build) -> None:
  slot, runtime = build(_cfg(allow_company_switch=False))
  assert runtime.directory is None

  await PrepareContext(slot, LOGGER).reconfigure(_cfg(SANDBOX_AFTER))

  assert runtime.directory is None
  assert runtime.environments is None
