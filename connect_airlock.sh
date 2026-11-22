#!/usr/bin/env bash
# ==============================================================================
# AirlockVM Multi-IDE & Agent MCP Client Connector
# Registers or removes AirlockVM MCP server across major IDEs and AI agent harnesses.
# Supports: Antigravity, VS Code, Cursor, Claude, Windsurf, JetBrains & Universal .mcp.json
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIRLOCK_ROOT="$SCRIPT_DIR"

# Resolve airlock-mcp binary: prefer standard user tool (~/.local/bin), then PATH, then local .venv
if [[ "${AIRLOCK_USE_VENV:-false}" == "true" && -x "${AIRLOCK_ROOT}/.venv/bin/airlock-mcp" ]]; then
  AIRLOCK_MCP_BIN="${AIRLOCK_ROOT}/.venv/bin/airlock-mcp"
elif [[ -x "${HOME}/.local/bin/airlock-mcp" ]]; then
  AIRLOCK_MCP_BIN="${HOME}/.local/bin/airlock-mcp"
elif command -v airlock-mcp >/dev/null 2>&1; then
  AIRLOCK_MCP_BIN="$(command -v airlock-mcp)"
elif [[ -x "${AIRLOCK_ROOT}/.venv/bin/airlock-mcp" ]]; then
  AIRLOCK_MCP_BIN="${AIRLOCK_ROOT}/.venv/bin/airlock-mcp"
else
  AIRLOCK_MCP_BIN="${HOME}/.local/bin/airlock-mcp"
fi

if [[ -x "${AIRLOCK_ROOT}/.venv/bin/python" ]]; then
  AIRLOCK_PYTHON="${AIRLOCK_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  AIRLOCK_PYTHON="$(command -v python3)"
else
  AIRLOCK_PYTHON="python3"
fi

MODE="target"
TARGET_DIR=""
ACTION="add"
DRY_RUN=false
AUTO_YES=false
PLATFORMS=()

# Terminal color codes for target highlighting
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  BOLD='\033[1m'
  CYAN='\033[1;36m'
  GREEN='\033[1;32m'
  YELLOW='\033[1;33m'
  RESET='\033[0m'
else
  BOLD=''
  CYAN=''
  GREEN=''
  YELLOW=''
  RESET=''
fi

show_help() {
  cat <<EOF
AirlockVM Multi-IDE & Agent MCP Client Connector

Usage:
  ./connect_airlock.sh [MODE] [PLATFORMS...] [OPTIONS]

Modes:
  -t, --target <path>     Register into a specific project workspace (default: current directory '.')
  -g, --global            Register globally into user home configs
  -l, --list              Inspect current AirlockVM registrations across all platforms

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
  -y, --yes               Automatic yes to confirmation prompt (non-interactive)
  -r, --remove            Remove AirlockVM MCP registration
  -d, --dry-run           Preview configuration updates without modifying disk
  -h, --help              Show this help message

Examples:
  # Connect all detected IDEs in the current project:
  ./connect_airlock.sh

  # Connect all detected IDEs in a specific project workspace:
  ./connect_airlock.sh --target /path/to/my-app --all

  # Connect VS Code and Antigravity globally for the current user:
  ./connect_airlock.sh --global --vscode --antigravity

  # Connect all platforms globally:
  ./connect_airlock.sh --global --all

  # Preview changes for Cursor and Claude in dry-run mode:
  ./connect_airlock.sh --target /path/to/my-app --cursor --claude --dry-run

  # Check active AirlockVM registrations:
  ./connect_airlock.sh --list
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
    -y|--yes)
      AUTO_YES=true
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

# Validate AirlockVM binary
if [[ ! -x "$AIRLOCK_MCP_BIN" && ! -f "$AIRLOCK_MCP_BIN" && ! -f "$AIRLOCK_PYTHON" ]]; then
  echo "Error: AirlockVM MCP server binary not found at ${AIRLOCK_MCP_BIN} or ${AIRLOCK_ROOT}/.venv" >&2
  echo "Please install it first:" >&2
  echo "  ./install_airlock_mcp.sh" >&2
  exit 1
fi

COMMAND_PATH="$AIRLOCK_MCP_BIN"
if [[ ! -f "$COMMAND_PATH" ]]; then
  COMMAND_PATH="$AIRLOCK_PYTHON"
  ARGS_LIST='["-m", "airlock.mcp.server"]'
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

# Resolve Target Path if target mode (defaults to current directory '.' if unspecified)
TARGET_ROOT=""
if [[ "$MODE" == "target" && "$ACTION" != "list" ]]; then
  if [[ -z "$TARGET_DIR" ]]; then
    TARGET_DIR="."
  fi
  if [[ ! -d "$TARGET_DIR" ]]; then
    echo "Error: Target directory does not exist: $TARGET_DIR" >&2
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
    TARGET_DISPLAY="Global User Environment (${HOME})"
    resolve_targets "global" "${HOME}"
  else
    TARGET_DISPLAY="${TARGET_ROOT}"
    resolve_targets "project" "$TARGET_ROOT"
  fi
fi

# Pre-execution summary, target highlighting, and interactive confirmation
if [[ "$ACTION" != "list" ]]; then
  echo -e "============================================================"
  echo -e " ${BOLD}AirlockVM Multi-IDE Client Connector${RESET}"
  echo -e "============================================================"
  if [[ "$DRY_RUN" == "true" ]]; then
    echo -e " Action:           ${BOLD}${YELLOW}${ACTION^^} (DRY-RUN)${RESET}"
  else
    echo -e " Action:           ${BOLD}${ACTION^^}${RESET}"
  fi
  echo -e " Target Location:  ${BOLD}${CYAN}${TARGET_DISPLAY}${RESET}"
  echo -e " Command Target:   ${GREEN}${COMMAND_PATH}${RESET}"
  echo -e " Target Configs:"
  declare -A seen_preview_paths=()
  for item in "${CONFIG_TARGETS[@]}"; do
    IFS="|" read -r label scope path <<< "$item"
    if [[ -z "${seen_preview_paths["$path"]:-}" ]]; then
      seen_preview_paths["$path"]=1
      if [[ "$scope" == "global" && "$ACTION" == "add" && ! -d "$(dirname "$path")" ]]; then
        echo -e "   • ${label} (${scope}): ${path} [skipped: IDE not installed]"
      else
        echo -e "   • ${BOLD}${label}${RESET} (${scope}): ${path}"
      fi
    fi
  done
  echo -e "============================================================"

  if [[ "$DRY_RUN" != "true" && "$AUTO_YES" != "true" ]]; then
    if [[ -t 0 ]]; then
      read -r -p "Apply these configuration changes? [y/N]: " confirm
    else
      if ! read -r confirm; then
        echo -e "${YELLOW}Aborted: Non-interactive execution requires -y/--yes to confirm changes.${RESET}" >&2
        exit 1
      fi
    fi
    if [[ ! "$confirm" =~ ^[Yy]([Ee][Ss])?$ ]]; then
      echo "Aborted by user."
      exit 0
    fi
  fi
fi

# Execute Multi-Target Update via Python (passing values via environment variables to avoid shell interpolation bugs)
AIRLOCK_RAW_TARGETS="$(printf '%s\n' "${CONFIG_TARGETS[@]}")" \
AIRLOCK_ACTION="$ACTION" \
AIRLOCK_DRY_RUN="$DRY_RUN" \
AIRLOCK_COMMAND_PATH="$COMMAND_PATH" \
AIRLOCK_ARGS_LIST="$ARGS_LIST" \
"$AIRLOCK_PYTHON" - <<'PYEOF'
import json
import os
import sys

raw_targets = os.environ.get("AIRLOCK_RAW_TARGETS", "").strip().splitlines()
action = os.environ.get("AIRLOCK_ACTION", "add")
dry_run = os.environ.get("AIRLOCK_DRY_RUN", "false").lower() in ("true", "1", "yes")
command_path = os.environ.get("AIRLOCK_COMMAND_PATH", "")
args_list = json.loads(os.environ.get("AIRLOCK_ARGS_LIST", "[]"))

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
    print(" AirlockVM MCP Client Registration Status")
    print(" Command: " + command_path)
    print("============================================================")
    for label, scope, path in targets:
        exists = os.path.exists(path)
        is_registered = False
        if exists:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    doc = json.load(f)
                is_registered = "airlock" in doc.get("mcpServers", {}) or "airlock" in doc.get("servers", {})
            except Exception:
                pass
        status_str = "[ACTIVE]    " if is_registered else ("[FILE EXISTS]" if exists else "[NOT FOUND] ")
        print(f" {status_str} {label:<16} ({scope:<7}) -> {path}")
    sys.exit(0)

if dry_run:
    print("[*] Simulating configuration updates (DRY-RUN)...")
else:
    print("[*] Applying configuration updates...")
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
            if data["servers"].get("airlock") != server_entry:
                data["servers"]["airlock"] = server_entry
                changed = True
        
        mcp_servers = data.setdefault("mcpServers", {})
        if not isinstance(mcp_servers, dict):
            mcp_servers = {}
            data["mcpServers"] = mcp_servers
            
        if mcp_servers.get("airlock") != server_entry:
            mcp_servers["airlock"] = server_entry
            changed = True

    elif action == "remove":
        if "mcpServers" in data and isinstance(data["mcpServers"], dict) and "airlock" in data["mcpServers"]:
            del data["mcpServers"]["airlock"]
            changed = True
        if "servers" in data and isinstance(data["servers"], dict) and "airlock" in data["servers"]:
            del data["servers"]["airlock"]
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
