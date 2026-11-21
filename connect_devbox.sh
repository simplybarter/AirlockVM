#!/usr/bin/env bash
# ==============================================================================
# DevBox MCP Client Connector
# Registers or removes DevBox MCP server in client project or global config.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVBOX_ROOT="$SCRIPT_DIR"
DEVBOX_MCP_BIN="${DEVBOX_ROOT}/.venv/bin/devbox-mcp"
DEVBOX_PYTHON="${DEVBOX_ROOT}/.venv/bin/python"

MODE="target"
TARGET_DIR=""
ACTION="add"
DRY_RUN=false

show_help() {
  cat <<EOF
DevBox MCP Client Connector

Usage:
  ./connect_devbox.sh [OPTIONS]

Options:
  -t, --target <path>     Register DevBox MCP server into target project (.gemini/mcp_config.json)
  -g, --global            Register DevBox MCP server globally (~/.gemini/config/mcp_config.json)
  -r, --remove            Remove DevBox MCP server from target or global config
  -d, --dry-run           Print configuration changes without writing to disk
  -h, --help              Show this help message

Examples:
  # Connect a specific project repository:
  ./connect_devbox.sh --target /path/to/my-app

  # Connect globally for all agent workflows:
  ./connect_devbox.sh --global

  # Preview configuration changes:
  ./connect_devbox.sh --target /path/to/my-app --dry-run

  # Remove DevBox MCP registration:
  ./connect_devbox.sh --target /path/to/my-app --remove
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--target)
      MODE="target"
      TARGET_DIR="$2"
      shift 2
      ;;
    -g|--global)
      MODE="global"
      shift
      ;;
    -r|--remove)
      ACTION="remove"
      shift
      ;;
    -d|--dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      show_help
      exit 1
      ;;
  esac
done

# Validate DevBox installation
if [[ ! -f "$DEVBOX_MCP_BIN" && ! -f "$DEVBOX_PYTHON" ]]; then
  echo "Error: DevBox virtual environment not found at ${DEVBOX_ROOT}/.venv" >&2
  echo "Please set up the virtual environment first:" >&2
  echo "  python3.14 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

COMMAND_PATH="$DEVBOX_MCP_BIN"
if [[ ! -f "$COMMAND_PATH" ]]; then
  COMMAND_PATH="$DEVBOX_PYTHON"
  ARGS_LIST='["-m", "devbox.mcp.server"]'
else
  ARGS_LIST='[]'
fi

# Determine target config file path
if [[ "$MODE" == "global" ]]; then
  CONFIG_DIR="${HOME}/.gemini/config"
  CONFIG_FILE="${CONFIG_DIR}/mcp_config.json"
else
  if [[ -z "$TARGET_DIR" ]]; then
    echo "Error: --target <path> is required when not using --global" >&2
    echo "Run with --help for usage details." >&2
    exit 1
  fi
  TARGET_CANONICAL="$(cd "$TARGET_DIR" && pwd)"
  CONFIG_DIR="${TARGET_CANONICAL}/.gemini"
  CONFIG_FILE="${CONFIG_DIR}/mcp_config.json"
fi

# Execute JSON update via Python
"$DEVBOX_PYTHON" - <<PYEOF
import json
import os
import sys

config_file = """$CONFIG_FILE"""
action = """$ACTION"""
dry_run = str("""$DRY_RUN""").lower() in ("true", "1", "yes")
command_path = """$COMMAND_PATH"""
args_list = json.loads("""$ARGS_LIST""")

data = {"mcpServers": {}}
if os.path.exists(config_file):
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
                data["mcpServers"] = {}
    except Exception as e:
        print(f"Warning: Could not parse existing {config_file}: {e}", file=sys.stderr)
        data = {"mcpServers": {}}

if action == "add":
    server_entry = {"command": command_path}
    if args_list:
        server_entry["args"] = args_list
    data["mcpServers"]["devbox"] = server_entry
    print(f"[{'DRY-RUN' if dry_run else 'ACTION'}] Registering 'devbox' server in {config_file}")
elif action == "remove":
    if "devbox" in data["mcpServers"]:
        del data["mcpServers"]["devbox"]
        print(f"[{'DRY-RUN' if dry_run else 'ACTION'}] Removed 'devbox' server from {config_file}")
    else:
        print(f"[{'DRY-RUN' if dry_run else 'ACTION'}] 'devbox' server was not present in {config_file}")

output = json.dumps(data, indent=2) + "\n"

if dry_run:
    print("\n--- Preview Result ---")
    print(output)
else:
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"Successfully updated: {config_file}")
PYEOF
