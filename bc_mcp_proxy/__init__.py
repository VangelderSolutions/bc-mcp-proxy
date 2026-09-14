"""
Business Central MCP proxy package.

This package exposes the entrypoint for the Python-based proxy that forwards MCP
requests from stdio clients to the official Business Central MCP HTTP endpoint.
"""

import site as _site
import sys as _sys
from pathlib import Path as _Path


def _add_bundled_wheels_to_path() -> None:
  """When running from a DXT bundle, third-party deps live under
  server/wheels/cp{XY}/ alongside this package. Pick the directory matching
  the running Python's ABI and register it as a site dir before any of the
  imports below pull in mcp/httpx/pydantic/cryptography.

  We use site.addsitedir rather than sys.path.insert so .pth files in the
  wheels dir get processed — pywin32 ships pywin32.pth, which both extends
  sys.path with its win32/win32com subdirs and runs pywin32_bootstrap to
  wire up DLL search paths. A plain sys.path insert skips all of that and
  pywintypes fails to import.

  A regular pip install has the deps already on sys.path and no `wheels`
  dir, so this is a no-op there.
  """
  wheels_root = _Path(__file__).resolve().parent.parent / "wheels"
  if not wheels_root.is_dir():
    return  # not a bundle layout

  abi = f"cp{_sys.version_info.major}{_sys.version_info.minor}"
  abi_dir = wheels_root / abi
  if abi_dir.is_dir():
    abi_path = str(abi_dir)
    _site.addsitedir(abi_path)
    # addsitedir appends. Re-promote to index 0 so our bundled deps win
    # over whatever the user might have in their global site-packages.
    try:
      _sys.path.remove(abi_path)
    except ValueError:
      pass
    _sys.path.insert(0, abi_path)
    return

  # Bundle is present but doesn't ship wheels for this Python. Fail loudly
  # via stderr so the Claude Desktop log shows a usable hint instead of
  # the generic ModuleNotFoundError that would otherwise follow.
  supported = sorted(p.name for p in wheels_root.iterdir() if p.is_dir() and p.name.startswith("cp"))
  print(
      f"bc-mcp-proxy: this bundle ships wheels for {', '.join(supported) or '(none)'} "
      f"but you're running Python {_sys.version_info.major}.{_sys.version_info.minor}. "
      f"Install one of the supported Python versions and retry.",
      file=_sys.stderr,
  )
  _sys.exit(2)


_add_bundled_wheels_to_path()


from ._version import __version__  # noqa: E402, F401
from .config import ProxyConfig  # noqa: E402, F401
from .proxy import (  # noqa: E402, F401
    RuntimeOptions,
    RuntimeResolver,
    RuntimeSlot,
    build_server,
    close_slot,
    run_proxy,
    run_slot_upstream,
)

__all__ = [
    "ProxyConfig", "run_proxy", "__version__",
    "build_server", "RuntimeSlot", "RuntimeOptions", "RuntimeResolver", "run_slot_upstream", "close_slot",
]


