"""Single source of truth for the package version.

Kept in its own module so bc_mcp_proxy.config can import it without
triggering the package __init__ (which imports config -> proxy).
Bump together with pyproject.toml and dxt/manifest.json.
"""

__version__ = "0.11.2"
