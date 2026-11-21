#!/usr/bin/env bash
# ==============================================================================
# DevBox Multi-IDE & Agent MCP Client Connector
# Registers or removes DevBox MCP server across major IDEs and AI agent harnesses.
# Supports: Antigravity, VS Code, Cursor, Claude, Windsurf, JetBrains & Universal .mcp.json
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
PLATFORMS=()

show_help() {
  cat <<EOF
DevBox Multi-IDE & Agent MCP Client Connector

Usage:
  ./connect_devbox.sh [MODE] [PLATFORMS...] [OPTIONS]

Modes:
  -t, --target <path>     Register into a specific project workspace
  -g, --global            Register globally into user home configs
  -l, --list              Inspect current DevBox registrations across all platforms

Platform Selectors (default: --all):
  -a, --all               Target all supported IDEs and agent platforms
      --antigravity       Google Antigravity / Gemini CLI
      --vscode            Visual Studio Code (native Copilot agent & extensions)
      --cursor            Cursor IDE
      --claude            Claude Desktop & Claude Code CLI
      --windsurf          Codeium Windsurf IDE
      --jetbrains         JetBrains IDEs (IntelliJ, PyCharm, WebStorm)
      --universal         Standard root-level .mcp.json (Neovim, CLI tools)

Options:
  -r, --remove            Remove DevBox MCP registration
  -d, --dry-run           Preview configuration updates without modifying disk
  -h, --help              Show this help message

Examples:
  # Connect all detected IDEs in a project workspace:
  ./connect_devbox.sh --target /path/to/my-app --all

  # Connect VS Code and Antigravity globally for the current user:
  ./connect_devbox.sh --global --vscode --antigravity

  # Connect all platforms globally:
  ./connect_devbox.sh --global --all

  # Preview changes for Cursor and Claude in dry-run mode:
  ./connect_devbox.sh --target /path/to/my-app --cursor --claude --dry-run

  # Check active DevBox registrations:
  ./connect_devbox.sh --list
EOF
}

# Parse Arguments
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
    -l|--list)
      ACTION="list"
      shift
      ;;
    -a|--all)
      PLATFORMS+=("all")
      shift
      ;;
    --antigravity|--gemini)
      PLATFORMS+=("antigravity")
      shift
      ;;
    --vscode|--code)
      PLATFORMS+=("vscode")
      shift
      ;;
    --cursor)
      PLATFORMS+=("cursor")
      shift
      ;;
    --claude)
      PLATFORMS+=("claude")
      shift
      ;;
    --windsurf)
      PLATFORMS+=("windsurf")
      shift
      ;;
    --jetbrains|--idea)
      PLATFORMS+=("jetbrains")
      shift
      ;;
    --universal)
      PLATFORMS+=("universal")
      shift
      ;;
    -r|--remove|--uninstall)
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

# Validate DevBox binary
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

# Default to "all" if no specific platform selector was passed
if [[ ${#PLATFORMS[@]} -eq 0 ]]; then
  PLATFORMS=("all")
fi

# Expand "all"
EXPANDED_PLATFORMS=()
for p in "${PLATFORMS[@]}"; do
  if [[ "$p" == "all" ]]; then
    EXPANDED_PLATFORMS=("antigravity" "vscode" "cursor" "claude" "windsurf" "jetbrains" "universal")
    break
  else
    EXPANDED_PLATFORMS+=("$p")
  fi
done

# Resolve Target Path if target mode
TARGET_ROOT=""
if [[ "$MODE" == "target" && "$ACTION" != "list" ]]; then
  if [[ -z "$TARGET_DIR" ]]; then
    echo "Error: --target <path> is required when not using --global or --list" >&2
    echo "Run with --help for usage details." >&2
    exit 1
  fi
  TARGET_ROOT="$(cd "$TARGET_DIR" && pwd)"
fi

# Resolve file map
# Format: PLATFORM|SCOPE|FILE_PATH
CONFIG_TARGETS=()

resolve_targets() {
  local scope="$1"
  local base="$2"

  for plat in "${EXPANDED_PLATFORMS[@]}"; do
    case "$plat" in
      antigravity)
        if [[ "$scope" == "global" ]]; then
          CONFIG_TARGETS+=("Antigravity|global|${HOME}/.gemini/config/mcp_config.json")
        else
          CONFIG_TARGETS+=("Antigravity|project|${base}/.gemini/mcp_config.json")
        fi
        ;;
      vscode)
        if [[ "$scope" == "global" ]]; then
          CONFIG_TARGETS+=("VS Code|global|${HOME}/.config/Code/User/mcp.json")
        else
          CONFIG_TARGETS+=("VS Code|project|${base}/.vscode/mcp.json")
        fi
        ;;
      cursor)
        if [[ "$scope" == "global" ]]; then
          CONFIG_TARGETS+=("Cursor|global|${HOME}/.config/Cursor/User/mcp.json")
          CONFIG_TARGETS+=("Cursor|global|${HOME}/.cursor/mcp.json")
        else
          CONFIG_TARGETS+=("Cursor|project|${base}/.cursor/mcp.json")
        fi
        ;;
      claude)
        if [[ "$scope" == "global" ]]; then
          CONFIG_TARGETS+=("Claude Desktop|global|${HOME}/.config/Claude/claude_desktop_config.json")
          CONFIG_TARGETS+=("Claude Code CLI|global|${HOME}/.claude.json")
        else
          CONFIG_TARGETS+=("Claude Code CLI|project|${base}/.mcp.json")
        fi
        ;;
      windsurf)
        if [[ "$scope" == "global" ]]; then
          CONFIG_TARGETS+=("Windsurf|global|${HOME}/.codeium/windsurf/mcp_config.json")
        else
          CONFIG_TARGETS+=("Windsurf|project|${base}/.codeium/windsurf/mcp_config.json")
        fi
        ;;
      jetbrains)
        if [[ "$scope" == "global" ]]; then
          CONFIG_TARGETS+=("JetBrains|global|${HOME}/.config/JetBrains/mcp.json")
        else
          CONFIG_TARGETS+=("JetBrains|project|${base}/.idea/mcp.json")
        fi
        ;;
      universal)
        if [[ "$scope" == "project" ]]; then
          CONFIG_TARGETS+=("Universal Root|project|${base}/.mcp.json")
        fi
        ;;
    esac
  done
}

if [[ "$ACTION" == "list" ]]; then
  # For list, collect all known global paths and any target path if specified
  resolve_targets "global" "${HOME}"
  if [[ -n "$TARGET_DIR" ]]; then
    resolve_targets "project" "$(cd "$TARGET_DIR" && pwd)"
  fi
else
  if [[ "$MODE" == "global" ]]; then
    resolve_targets "global" "${HOME}"
  else
    resolve_targets "project" "$TARGET_ROOT"
  fi
fi

# Execute Multi-Target Update via Python
"$DEVBOX_PYTHON" - <<PYEOF
import json
import os
import sys

raw_targets = """$(printf '%s\n' "${CONFIG_TARGETS[@]}")""".strip().splitlines()
action = """$ACTION"""
dry_run = str("""$DRY_RUN""").lower() in ("true", "1", "yes")
command_path = """$COMMAND_PATH"""
args_list = json.loads("""$ARGS_LIST""")

server_entry = {"command": command_path}
if args_list:
    server_entry["args"] = args_list

# Deduplicate target file paths while preserving label
seen = set()
targets = []
for line in raw_targets:
    if not line.strip():
        continue
    label, scope, path = line.split("|", 2)
    if path not in seen:
        seen.add(path)
        targets.append((label, scope, path))

if action == "list":
    print("============================================================")
    print(" DevBox MCP Client Registration Status")
    print(" Command: " + command_path)
    print("============================================================")
    for label, scope, path in targets:
        exists = os.path.exists(path)
        is_registered = False
        if exists:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    doc = json.load(f)
                is_registered = "devbox" in doc.get("mcpServers", {}) or "devbox" in doc.get("servers", {})
            except Exception:
                pass
        status_str = "[ACTIVE]    " if is_registered else ("[FILE EXISTS]" if exists else "[NOT FOUND] ")
        print(f" {status_str} {label:<16} ({scope:<7}) -> {path}")
    sys.exit(0)

print(f"[*] DevBox Connector Action: {action.upper()} {'(DRY-RUN)' if dry_run else ''}")
print(f"[*] Command Target: {command_path}")
print("------------------------------------------------------------")

modified_count = 0

for label, scope, path in targets:
    # In project mode, if creating a new file, ensure directory exists
    # If in global mode, skip if parent config folder doesn't exist to avoid cluttering unused IDEs
    parent_dir = os.path.dirname(path)
    if scope == "global" and action == "add" and not os.path.exists(parent_dir):
        # Don't create global directories for IDEs the user hasn't installed
        continue

    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[-] Warning: Error reading {path}: {e}", file=sys.stderr)
            data = {}

    if not isinstance(data, dict):
        data = {}

    changed = False

    if action == "add":
        # VS Code and some variants support both mcpServers and servers
        if "servers" in data and isinstance(data["servers"], dict):
            if data["servers"].get("devbox") != server_entry:
                data["servers"]["devbox"] = server_entry
                changed = True
        
        mcp_servers = data.setdefault("mcpServers", {})
        if not isinstance(mcp_servers, dict):
            mcp_servers = {}
            data["mcpServers"] = mcp_servers
            
        if mcp_servers.get("devbox") != server_entry:
            mcp_servers["devbox"] = server_entry
            changed = True

    elif action == "remove":
        if "mcpServers" in data and isinstance(data["mcpServers"], dict) and "devbox" in data["mcpServers"]:
            del data["mcpServers"]["devbox"]
            changed = True
        if "servers" in data and isinstance(data["servers"], dict) and "devbox" in data["servers"]:
            del data["servers"]["devbox"]
            changed = True

    if changed or (action == "add" and not os.path.exists(path)):
        modified_count += 1
        output = json.dumps(data, indent=2) + "\n"
        if dry_run:
            print(f"[DRY-RUN] Would update {label} ({scope}): {path}")
        else:
            os.makedirs(parent_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"[✓] Updated {label} ({scope}): {path}")
    else:
        print(f"[=] Unchanged {label} ({scope}): {path}")

print("------------------------------------------------------------")
print(f"[✓] Completed. Total configs {'evaluated' if dry_run else 'updated'}: {modified_count}")
PYEOF
