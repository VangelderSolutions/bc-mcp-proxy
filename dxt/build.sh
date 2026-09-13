#!/usr/bin/env bash
# Build a .mcpb bundle (MCP Bundle, formerly .dxt) for Claude Desktop from the current source tree.
#
# Usage (from the repo root):
#   ./dxt/build.sh
#
# Output: dist/vgs-bc-mcp-<version>-<platform>.mcpb
#
# What this does:
#   1. Stages the proxy source under dxt/build/server/bc_mcp_proxy.
#   2. Vendors all Python dependencies once per supported ABI under
#      dxt/build/server/wheels/cp{311,312,313,314}/. Several deps
#      (pydantic_core, charset_normalizer, rpds, mypyc-built ones)
#      ship Python-version-specific compiled wheels rather than abi3,
#      so a single ABI's wheels won't load on a different Python.
#      The shim in bc_mcp_proxy/__init__.py picks the right dir at
#      startup based on sys.version_info.
#   3. Wheels target the host's platform tag (manylinux2014 / macosx).
#
# Requires:
#   - Python 3.11+ on PATH
#   - Node + npx, OR a working `mcpb` / `dxt` CLI on PATH

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

version="$(awk -F\" '/^__version__/ {print $2; exit}' bc_mcp_proxy/_version.py)"
if [[ -z "${version:-}" ]]; then
  echo "Could not determine package version from bc_mcp_proxy/_version.py." >&2
  exit 1
fi

# Detect platform tag pip uses for wheel filenames.
case "$(uname -s)" in
  Darwin)
    arch="$(uname -m)"
    if [[ "$arch" == "arm64" ]]; then
      pip_platform="macosx_11_0_arm64"
      platform_tag="darwin-arm64"
    else
      pip_platform="macosx_10_15_x86_64"
      platform_tag="darwin-x86_64"
    fi
    ;;
  Linux)
    pip_platform="manylinux2014_x86_64"
    platform_tag="linux-x86_64"
    ;;
  *)
    echo "Unsupported host: $(uname -s). Run dxt/build.ps1 on Windows." >&2
    exit 1
    ;;
esac

build_dir="$repo_root/dxt/build"
dist_dir="$repo_root/dist"
bundle="$dist_dir/vgs-bc-mcp-$version-$platform_tag.mcpb"

rm -rf "$build_dir"
mkdir -p "$build_dir/server" "$dist_dir"

echo "Staging extension contents in $build_dir ..."
cp dxt/manifest.json    "$build_dir/manifest.json"
cp dxt/requirements.txt "$build_dir/server/requirements.txt"
cp -R bc_mcp_proxy      "$build_dir/server/bc_mcp_proxy"
cp LICENSE              "$build_dir/LICENSE"
for icon in icon.png icon-256.png; do
  if [[ -f "dxt/$icon" ]]; then
    cp "dxt/$icon" "$build_dir/$icon"
  else
    echo "  (no dxt/$icon — bundle will ship without it)"
  fi
done

for abi in 311 312 313 314; do
  py_ver="3.${abi:1}"
  abi_dir="$build_dir/server/wheels/cp$abi"
  echo "Vendoring wheels for Python $py_ver (cp$abi) into $abi_dir ..."
  python3 -m pip install \
    --target "$abi_dir" \
    --upgrade \
    --no-compile \
    --python-version "$py_ver" \
    --only-binary ':all:' \
    --platform "$pip_platform" \
    -r dxt/requirements.txt
done

# Strip only __pycache__. Do NOT strip *.dist-info — the mcp package
# (and any other dep that calls importlib.metadata.version("<self>") at
# import time) needs that metadata to be present in the bundle.
find "$build_dir" -type d -name __pycache__ -prune -exec rm -rf {} +

echo "Packing $bundle ..."
if command -v mcpb >/dev/null 2>&1; then
  mcpb pack "$build_dir" "$bundle"
elif command -v dxt >/dev/null 2>&1; then
  dxt pack "$build_dir" "$bundle"
else
  npx --yes @anthropic-ai/mcpb pack "$build_dir" "$bundle"
fi

bundle_size="$(du -h "$bundle" | cut -f1)"
echo "Built $bundle ($bundle_size)"
