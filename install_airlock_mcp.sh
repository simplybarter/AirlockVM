#!/usr/bin/env bash
# ==============================================================================
# AirlockVM: MCP Server Installer (install_airlock_mcp.sh)
#
# Installs 'airlock' and 'airlock-mcp' into the standard user tool location
# (~/.local/bin/) in an isolated environment using uv, pipx, or standard venv.
#
# Once installed, the MCP server can be invoked from any directory as:
#   airlock-mcp
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIRLOCK_ROOT="$SCRIPT_DIR"

INSTALL_MODE="tool"
EDITABLE=false
FORCE=false
PYTHON_TARGET=""
UNINSTALL=false
RUN_CONNECT=false

show_help() {
  cat <<EOF
AirlockVM: MCP Server Installer

Usage:
  ./install_airlock_mcp.sh [OPTIONS]

Options:
  -e, --editable       Install in editable mode (source modifications take effect immediately)
  -f, --force          Force re-installation / overwrite existing install
  -p, --python <ver>   Specify Python version (default: auto-detect Python >=3.11)
  -c, --connect        Immediately configure IDE agent connectors via ./connect_airlock.sh --all
      --uninstall      Uninstall airlock and airlock-mcp from user environment
  -h, --help           Show this help message

Standard Installation Target:
  Binary executables:  ~/.local/bin/airlock, ~/.local/bin/airlock-mcp
  Isolated runtime:    ~/.local/share/uv/tools/airlockvm (or pipx / user venv)

Example:
  ./install_airlock_mcp.sh                # Standard isolated install
  ./install_airlock_mcp.sh --editable     # Contributor/development mode
  ./install_airlock_mcp.sh --connect      # Install and wire up all IDEs
EOF
}

# Parse CLI options
while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--editable)
      EDITABLE=true
      shift
      ;;
    -f|--force)
      FORCE=true
      shift
      ;;
    -p|--python)
      PYTHON_TARGET="$2"
      shift 2
      ;;
    -c|--connect)
      RUN_CONNECT=true
      shift
      ;;
    --uninstall)
      UNINSTALL=true
      shift
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      echo "[-] Unknown option: $1" >&2
      show_help
      exit 1
      ;;
  esac
done

USER_BIN="${HOME}/.local/bin"
mkdir -p "${USER_BIN}"

# --- Handle Uninstallation ---
if [ "$UNINSTALL" = true ]; then
  echo "[*] Uninstalling AirlockVM MCP tools..."
  if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "airlockvm"; then
    uv tool uninstall airlockvm
  elif command -v pipx >/dev/null 2>&1 && pipx list 2>/dev/null | grep -q "airlockvm"; then
    pipx uninstall airlockvm
  fi

  rm -f "${USER_BIN}/airlock" "${USER_BIN}/airlock-mcp"
  rm -rf "${HOME}/.local/share/airlockvm"
  echo "[✓] AirlockVM MCP tools cleanly uninstalled."
  exit 0
fi

echo "============================================================"
echo " AirlockVM MCP Server Installer"
echo " Source: ${AIRLOCK_ROOT}"
echo " Target: ${USER_BIN}/airlock-mcp"
echo "============================================================"

# --- Installation Execution ---
INSTALLED=false

# Method 1: uv tool install (fastest, cleanest)
if command -v uv >/dev/null 2>&1; then
  echo "[*] Installing via uv tool..."
  UV_CMD=("uv" "tool" "install")
  
  if [ "$EDITABLE" = true ]; then
    UV_CMD+=("--editable")
  fi
  if [ "$FORCE" = true ]; then
    UV_CMD+=("--force")
  fi
  if [ -n "$PYTHON_TARGET" ]; then
    UV_CMD+=("--python" "$PYTHON_TARGET")
  fi

  UV_CMD+=("${AIRLOCK_ROOT}")
  "${UV_CMD[@]}"
  INSTALLED=true

# Method 2: pipx install
elif command -v pipx >/dev/null 2>&1; then
  echo "[*] Installing via pipx..."
  PIPX_CMD=("pipx" "install")
  if [ "$EDITABLE" = true ]; then
    PIPX_CMD+=("--editable")
  fi
  if [ "$FORCE" = true ]; then
    PIPX_CMD+=("--force")
  fi
  if [ -n "$PYTHON_TARGET" ]; then
    PIPX_CMD+=("--python" "python${PYTHON_TARGET}")
  fi
  PIPX_CMD+=("${AIRLOCK_ROOT}")
  "${PIPX_CMD[@]}"
  INSTALLED=true

# Method 3: Fallback standard Python venv + symlinks
else
  echo "[*] Neither 'uv' nor 'pipx' found. Installing via standard Python venv..."
  VENV_DIR="${HOME}/.local/share/airlockvm/venv"
  mkdir -p "${HOME}/.local/share/airlockvm"
  
  PYTHON_EXE="python3"
  if command -v "python${PYTHON_TARGET}" >/dev/null 2>&1; then
    PYTHON_EXE="python${PYTHON_TARGET}"
  fi

  if [ ! -d "${VENV_DIR}" ] || [ "$FORCE" = true ]; then
    rm -rf "${VENV_DIR}"
    "${PYTHON_EXE}" -m venv "${VENV_DIR}"
  fi

  if [ "$EDITABLE" = true ]; then
    "${VENV_DIR}/bin/pip" install -e "${AIRLOCK_ROOT}"
  else
    "${VENV_DIR}/bin/pip" install "${AIRLOCK_ROOT}"
  fi

  ln -sf "${VENV_DIR}/bin/airlock" "${USER_BIN}/airlock"
  ln -sf "${VENV_DIR}/bin/airlock-mcp" "${USER_BIN}/airlock-mcp"
  INSTALLED=true
fi

# --- Post-Install Verification ---
echo "------------------------------------------------------------"
if [ ! -x "${USER_BIN}/airlock-mcp" ]; then
  echo "[-] Error: Executable not found at ${USER_BIN}/airlock-mcp" >&2
  exit 1
fi

# Check PATH
if [[ ":$PATH:" != *":${USER_BIN}:"* ]]; then
  echo "[!] Warning: ${USER_BIN} is not currently in your system PATH."
  echo "    To run 'airlock' and 'airlock-mcp' directly, add this to your ~/.bashrc or ~/.zshrc:"
  echo "    export PATH=\"\${HOME}/.local/bin:\$PATH\""
  echo ""
fi

# Verify executable
echo "[*] Verifying installation with 'airlock ping'..."
if "${USER_BIN}/airlock" ping; then
  echo "[✓] AirlockVM MCP server successfully installed and verified!"
else
  echo "[-] Warning: Server verification failed. Please check your Python environment." >&2
fi

echo ""
echo "============================================================"
echo " MCP Server Configuration Reference"
echo "============================================================"
echo "In your IDE (Antigravity, Cursor, VS Code, Claude Code, etc.):"
echo ""
echo "Option A: Clean PATH command (recommended):"
cat <<'JSON'
{
  "mcpServers": {
    "airlock": {
      "command": "airlock-mcp"
    }
  }
}
JSON
echo ""
echo "Option B: Explicit user binary path:"
cat <<JSON
{
  "mcpServers": {
    "airlock": {
      "command": "${USER_BIN}/airlock-mcp"
    }
  }
}
JSON
echo "============================================================"

# --- Optional IDE Auto-Connect ---
if [ "$RUN_CONNECT" = true ]; then
  echo ""
  echo "[*] Automatically configuring IDE agent connectors..."
  "${AIRLOCK_ROOT}/connect_airlock.sh" --all
fi
