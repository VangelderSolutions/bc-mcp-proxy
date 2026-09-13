"""Tests for the boolean config flags (annotate tools / forward resources)."""

from __future__ import annotations

from typing import Any

import pytest

from bc_mcp_proxy.__main__ import parse_args

_FLAG_ENV_VARS = ("BC_ANNOTATE_TOOLS", "BC_FORWARD_RESOURCES_PROMPTS", "BC_HIDE_UNAUTHORIZED_TOOLS", "BC_ALLOW_COMPANY_SWITCH", "BC_BASE_URL")


def _run_parse(monkeypatch: pytest.MonkeyPatch, argv: list[str] | None = None, **env: str) -> Any:
  for name in _FLAG_ENV_VARS:
    monkeypatch.delenv(name, raising=False)
  for name, value in env.items():
    monkeypatch.setenv(name, value)
  return parse_args(argv or [])


def test_defaults_are_on(monkeypatch: pytest.MonkeyPatch) -> None:
  cfg = _run_parse(monkeypatch)
  assert cfg.annotate_tools is True
  assert cfg.forward_resources_prompts is True
  assert cfg.hide_unauthorized_tools is False
  assert cfg.allow_company_switch is False


def test_allow_company_switch_env_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
  assert _run_parse(monkeypatch, BC_ALLOW_COMPANY_SWITCH="1").allow_company_switch is True
  assert _run_parse(monkeypatch, ["--AllowCompanySwitch"]).allow_company_switch is True
  assert _run_parse(monkeypatch, ["--NoAllowCompanySwitch"], BC_ALLOW_COMPANY_SWITCH="1").allow_company_switch is False


def test_hide_unauthorized_tools_env_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
  assert _run_parse(monkeypatch, BC_HIDE_UNAUTHORIZED_TOOLS="1").hide_unauthorized_tools is True
  assert _run_parse(monkeypatch, BC_HIDE_UNAUTHORIZED_TOOLS="false").hide_unauthorized_tools is False
  assert _run_parse(monkeypatch, ["--HideUnauthorizedTools"]).hide_unauthorized_tools is True
  assert _run_parse(monkeypatch, ["--NoHideUnauthorizedTools"],
                    BC_HIDE_UNAUTHORIZED_TOOLS="1").hide_unauthorized_tools is False


@pytest.mark.parametrize("value", ["0", "false", "No", " off "])
def test_env_opt_out_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
  cfg = _run_parse(monkeypatch, BC_ANNOTATE_TOOLS=value, BC_FORWARD_RESOURCES_PROMPTS=value)
  assert cfg.annotate_tools is False
  assert cfg.forward_resources_prompts is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_env_opt_in_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
  cfg = _run_parse(monkeypatch, BC_ANNOTATE_TOOLS=value)
  assert cfg.annotate_tools is True


def test_empty_env_value_means_default(monkeypatch: pytest.MonkeyPatch) -> None:
  cfg = _run_parse(monkeypatch, BC_ANNOTATE_TOOLS="")
  assert cfg.annotate_tools is True


def test_invalid_env_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
  with pytest.raises(ValueError, match="annotate_tools"):
    _run_parse(monkeypatch, BC_ANNOTATE_TOOLS="maybe")


def test_cli_flags_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
  cfg = _run_parse(monkeypatch, ["--NoAnnotateTools", "--NoForwardResourcesPrompts"],
                   BC_ANNOTATE_TOOLS="1", BC_FORWARD_RESOURCES_PROMPTS="1")
  assert cfg.annotate_tools is False
  assert cfg.forward_resources_prompts is False
  cfg = _run_parse(monkeypatch, ["--AnnotateTools"], BC_ANNOTATE_TOOLS="0")
  assert cfg.annotate_tools is True


def test_server_version_defaults_to_package_version(monkeypatch: pytest.MonkeyPatch) -> None:
  from bc_mcp_proxy import __version__
  monkeypatch.delenv("BC_SERVER_VERSION", raising=False)
  monkeypatch.delenv("BC_SERVER_NAME", raising=False)
  cfg = _run_parse(monkeypatch)
  assert cfg.server_version == __version__
  assert cfg.server_name == "vgs-bc-mcp"


def test_package_version_matches_pyproject() -> None:
  import pathlib, re
  from bc_mcp_proxy import __version__
  text = pathlib.Path(__file__).resolve().parent.parent.joinpath("pyproject.toml").read_text(encoding="utf-8")
  assert re.search(r'^version = "([^"]+)"', text, re.M).group(1) == __version__
