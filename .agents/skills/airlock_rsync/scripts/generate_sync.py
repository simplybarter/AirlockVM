#!/usr/bin/env python3
"""
AirlockVM Rsync Generator
Evaluates the project tech stack, determines essential runtime files,
and generates an optimized 'deploy_app_to_airlock.sh' in the project root.
"""

import argparse
import base64
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent


def find_repo_root() -> Path:
    if len(SKILL_DIR.parents) >= 3 and SKILL_DIR.parents[1].name == ".agents":
        return SKILL_DIR.parents[2].resolve()
    cwd = Path.cwd().resolve()
    for p in [cwd] + list(cwd.parents):
        if (p / ".git").exists() or (p / ".agents").exists():
            return p
    return cwd


REPO_ROOT = find_repo_root()
DEFAULT_OUTPUT_SCRIPT = REPO_ROOT / "deploy_app_to_airlock.sh"


def format_path(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        try:
            return f"~/{p.relative_to(Path.home())}"
        except ValueError:
            return str(p)


def detect_vm_target() -> tuple[str, str]:
    """Inspects VM specification in inventory/ or defaults to project name."""
    project_name = REPO_ROOT.name
    slug = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-")
    default_vm_host = f"{slug}-vm"
    default_remote_dir = f"~/{project_name}/"

    inv_dir = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))
    if inv_dir.exists():
        # First check vm.yaml as SSOT
        yaml_candidates = list(inv_dir.glob(f"*/{slug}-vm/vm.yaml")) + sorted(inv_dir.glob("*/*/vm.yaml"))
        for yp in yaml_candidates:
            if yp.exists() and yaml is not None:
                try:
                    doc = yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
                    vm_data = doc.get("vm", {})
                    bp = vm_data.get("blueprint", {})
                    vm_name = vm_data.get("name", default_vm_host)
                    remote_dir = bp.get("remote_code_dir", default_remote_dir)
                    return vm_name, remote_dir
                except Exception:
                    pass

        # Fallback to VM.md if present
        md_candidates = list(inv_dir.glob(f"*/{slug}-vm/VM.md")) + sorted(inv_dir.glob("*/*/VM.md"))
        for mp in md_candidates:
            if mp.exists():
                content = mp.read_text(encoding="utf-8")
                rsync_match = re.search(r"rsync[^\n]+(?:\./|\.\s+)\s+([a-zA-Z0-9_\-\.]+):([~a-zA-Z0-9_\-\/\.]+)", content)
                if rsync_match:
                    return rsync_match.group(1), rsync_match.group(2)
                host_match = re.search(r"Host\s+([a-zA-Z0-9_\-\.]+)", content)
                if host_match:
                    default_vm_host = host_match.group(1)
                break

    return default_vm_host, default_remote_dir


def evaluate_project_exclusions() -> list[str]:
    """Analyzes the repository to determine what must NOT be synced to the VM."""
    # Standard baseline exclusions (never needed on execution VM)
    exclusions = [
        # Version control & repository metadata
        ".git/",
        ".svn/",
        ".hg/",
        ".github/",
        ".gitignore",
        ".gitattributes",
        # Agent & AI configuration trees
        ".agents/",
        ".gemini/",
        ".claude/",
        ".cursor/",
        ".codeium/",
        ".mcp.json",
        # IDE, Editor, and OS cache
        ".vscode/",
        ".idea/",
        "*.swp",
        "*.swo",
        "*~",
        ".DS_Store",
        "Thumbs.db",
        # Documentation & local runbooks
        "docs/",
        "*.md",
        "LICENSE",
        "inventory/",
        "analysis/",
        # Synchronization scripts
        "deploy_app_to_airlock.sh",
        "deploy_app_to_vm.sh",
        "sync_to_airlock.sh",
        "install_skill.sh",
        "connect_airlock.sh",
        # Local secrets, private keys, and environment overrides
        ".env*.local",
        ".env.production",
        "*.local.json",
        "my_*.json",
        "*.pem",
        "*.key",
        "id_rsa",
        "id_ed25519",
    ]

    # Always exclude common Python and Node.js cache/build artifacts across polyglot workspaces
    exclusions.extend([
        "__pycache__/",
        "*.py[cod]",
        "*.class",
        "venv/",
        ".venv/",
        "env/",
        "ENV/",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        ".tox/",
        "dist/",
        "*.egg-info/",
        "node_modules/",
        ".next/",
        ".nuxt/",
        ".turbo/",
        "out/",
        "build/",
        ".cache/",
        ".pnpm-store/",
    ])

    # Rust exclusions
    if (REPO_ROOT / "Cargo.toml").exists():
        exclusions.append("target/")

    # Remove duplicates preserving order
    seen = set()
    unique_exclusions = []
    for item in exclusions:
        if item not in seen:
            seen.add(item)
            unique_exclusions.append(item)

    return unique_exclusions


def _expand_include_patterns(patterns: list[str]) -> list[str]:
    """Expands nested include patterns so rsync descends through otherwise-excluded parent directories.

    Example: 'docs/templates/' -> ['docs/', 'docs/templates/', 'docs/templates/***']
    Example: 'docs/templates/index.md' -> ['docs/', 'docs/templates/', 'docs/templates/index.md']
    """
    expanded: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        if p and p not in seen:
            seen.add(p)
            expanded.append(p)

    for raw in patterns:
        pat = str(raw).strip()
        while pat.startswith("./"):
            pat = pat[2:]
        pat = pat.lstrip("/")
        if not pat:
            continue
        is_dir = pat.endswith("/")
        clean = pat.rstrip("/")
        parts = [p for p in clean.split("/") if p]
        # Add parent directories so rsync traverses into nested paths even if top-level dir is excluded
        for i in range(1, len(parts)):
            if not any(ch in parts[i - 1] for ch in "*?["):
                _add("/".join(parts[:i]) + "/")
        if is_dir:
            _add(clean + "/")
            _add(clean + "/***")
        else:
            _add(pat)
    return expanded


def generate_shell_script(
    vm_host: str,
    repos: list[dict],
    exclusions: list[str],
    hypervisor_host: str = "localhost",
) -> str:
    """Renders a robust deploy_app_to_airlock.sh bash script supporting multi-repo synchronization."""
    exclude_items = "\n".join(f"  --exclude='{exc}'" for exc in exclusions)
    repo_lines = []
    for r in repos:
        post_raw = str(r.get("post_deploy", "") or "")
        post_b64 = base64.b64encode(post_raw.encode("utf-8")).decode("ascii") if post_raw else ""
        inc_raw = r.get("include") or []
        exc_raw = r.get("exclude") or []
        inc_list = _expand_include_patterns(inc_raw) if isinstance(inc_raw, list) else []
        exc_list = [str(x).strip() for x in exc_raw if str(x).strip()] if isinstance(exc_raw, list) else []
        inc_b64 = base64.b64encode("\n".join(inc_list).encode("utf-8")).decode("ascii") if inc_list else ""
        exc_b64 = base64.b64encode("\n".join(exc_list).encode("utf-8")).decode("ascii") if exc_list else ""
        repo_lines.append(
            f'  "{r["name"]}|{r["local_path"]}|{r["remote_path"]}|{post_b64}|{r.get("internal_port", 3000)}|{r.get("external_port", 3001)}|{inc_b64}|{exc_b64}"'
        )
    repo_entries = "\n".join(repo_lines)

    template = """#!/usr/bin/env bash
# ==============================================================================
# AirlockVM Code Synchronizer & Deployment Engine
# Synchronizes application source code to __VM_HOST__
# Auto-generated by .agents/skills/airlock_rsync
# ==============================================================================

set -euo pipefail

VM_HOST="__VM_HOST__"
HYPERVISOR_HOST="__HYPERVISOR_HOST__"
LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=false
DELETE_FLAG=false
VERBOSE=false
SKIP_CHECK=false
OFFLINE_ONLY=false
SKIP_POST_DEPLOY=false

EXCLUDE_ARGS=(
__EXCLUDE_ITEMS__
)

# Configured application repositories (name|local_path|remote_path|post_deploy_b64|internal_port|external_port|include_b64|exclude_b64)
REPOSITORIES=(
__REPOSITORY_ENTRIES__
)

decode_b64_str() {
  local b64="${1:-}"
  if [[ -n "$b64" ]]; then
    printf '%s' "$b64" | base64 -d 2>/dev/null || printf '%s' "$b64"
  fi
}

decode_post_cmd() {
  decode_b64_str "${1:-}"
}

build_repo_filter_args() {
  local inc_b64="${1:-}"
  local exc_b64="${2:-}"
  REPO_FILTER_ARGS=()
  if [[ -n "$inc_b64" ]]; then
    while IFS= read -r pat; do
      [[ -n "$pat" ]] && REPO_FILTER_ARGS+=("--include=${pat}")
    done < <(decode_b64_str "$inc_b64"; echo)
  fi
  REPO_FILTER_ARGS+=("${EXCLUDE_ARGS[@]}")
  if [[ -n "$exc_b64" ]]; then
    while IFS= read -r pat; do
      [[ -n "$pat" ]] && REPO_FILTER_ARGS+=("--exclude=${pat}")
    done < <(decode_b64_str "$exc_b64"; echo)
  fi
}

TARGET_REPO=""

show_help() {
  cat <<EOF
Usage: $0 [OPTIONS]

Syncs application repositories to $VM_HOST and optionally executes post-deploy hooks.

Configured Repositories:
$(for entry in "${REPOSITORIES[@]}"; do
  IFS='|' read -r rname lpath rpath post_b64 int_port ext_port inc_b64 exc_b64 <<< "$entry"
  post_cmd="$(decode_post_cmd "${post_b64:-}")"
  int_port="${int_port:-3000}"
  ext_port="${ext_port:-3001}"
  echo "  - $rname: $lpath -> $VM_HOST:$rpath (http://$HYPERVISOR_HOST:$ext_port/ -> :$int_port)"
  if [[ -n "$post_cmd" ]]; then
    echo "      post-deploy: '$post_cmd'"
  fi
done)

Options:
  -r, --repo <name>      Sync only a specific repository (alias: --only)
  -o, --offline          Show local file inventory only (offline, 0 network calls)
  -n, --dry-run          Preview local inventory & remote changes without modifying files
  -d, --delete           Delete files in destination that no longer exist locally
  -v, --verbose          Show detailed file transfer output
  -s, --skip-check       Skip VM readiness & SSH reachability check
      --no-restart       Skip executing post-deploy commands on VM
      --sync-only        Alias for --no-restart
  -h, --help             Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -r|--repo|--only)
      TARGET_REPO="$2"
      shift 2
      ;;
    -o|--offline)
      OFFLINE_ONLY=true
      shift
      ;;
    -n|--dry-run)
      DRY_RUN=true
      shift
      ;;
    -d|--delete)
      DELETE_FLAG=true
      shift
      ;;
    -v|--verbose)
      VERBOSE=true
      shift
      ;;
    -s|--skip-check)
      SKIP_CHECK=true
      shift
      ;;
    --no-restart|--sync-only|--skip-post-deploy)
      SKIP_POST_DEPLOY=true
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

# ------------------------------------------------------------------------------
# 1. Local Offline File Inventory (Executed BEFORE any network connection)
# ------------------------------------------------------------------------------
if [[ "$DRY_RUN" == "true" || "$OFFLINE_ONLY" == "true" ]]; then
  echo "============================================================"
  echo " AirlockVM Local File Inventory (Offline Evaluation)"
  echo " Target VM: $VM_HOST"
  echo "============================================================"
  for entry in "${REPOSITORIES[@]}"; do
    IFS='|' read -r rname lpath rpath post_b64 int_port ext_port inc_b64 exc_b64 <<< "$entry"
    post_cmd="$(decode_post_cmd "${post_b64:-}")"
    if [[ -n "$TARGET_REPO" && "$rname" != "$TARGET_REPO" ]]; then
      continue
    fi
    echo "[*] Repository '$rname': $lpath"
    if [[ ! -d "$lpath" ]]; then
      echo "    [-] WARNING: Local directory does not exist: $lpath"
      continue
    fi
    build_repo_filter_args "${inc_b64:-}" "${exc_b64:-}"
    rsync -r --list-only "${REPO_FILTER_ARGS[@]}" "$lpath/" | awk -v name="$rname" 'NR>1 {
      path = $5;
      for (i=6; i<=NF; i++) path = path " " $i;
      printf "  %-11s %10s  %s\\n", $1, $2, path;
      count++;
    }
    END {
      if (count == 0) {
        print "  (No application files matched for synchronization)";
      } else {
        printf "\\n  Total: %d candidate items for %s\\n", count, name;
      }
    }'
    echo ""
  done

  if [[ "$OFFLINE_ONLY" == "true" ]]; then
    echo "[✓] Offline file inventory complete (0 network connections made)."
    exit 0
  fi
fi

# ------------------------------------------------------------------------------
# 2. Remote Synchronization Configuration
# ------------------------------------------------------------------------------
RSYNC_OPTS=(-avz --compress-level=6)

if [[ "$VERBOSE" == "true" ]]; then
  RSYNC_OPTS+=(--progress)
else
  RSYNC_OPTS+=(--stats)
fi

if [[ "$DELETE_FLAG" == "true" ]]; then
  RSYNC_OPTS+=(--delete)
fi

if [[ "$DRY_RUN" == "true" ]]; then
  RSYNC_OPTS+=(--dry-run --itemize-changes)
fi

echo "============================================================"
echo " AirlockVM Remote Sync: Workstation -> $VM_HOST"
echo "============================================================"

# Check whether the VM is set up and reachable before attempting sync
if [[ "$SKIP_CHECK" != "true" ]]; then
  echo "[*] Checking VM readiness ($VM_HOST)..."
  if ! ssh -o ConnectTimeout=3 -o BatchMode=yes -o StrictHostKeyChecking=no "$VM_HOST" "echo ok" >/dev/null 2>&1; then
    echo ""
    echo "[!] Notice: Cannot connect to VM host '$VM_HOST' via SSH."
    echo ""
    if [[ "$DRY_RUN" == "true" ]]; then
      echo "    The local candidate files listed above will be synced once the VM is online."
      echo "    To check VM state on the hypervisor: virsh list --all"
      echo ""
      exit 0
    fi

    echo "Possible causes:"
    echo "  1. The VM has not been created or booted yet."
    echo "  2. The VM is powered off or paused."
    echo "  3. SSH configuration for '$VM_HOST' is missing in ~/.ssh/config."
    echo ""
    echo "Troubleshooting steps:"
    echo "  - Check VM state on hypervisor: virsh list --all"
    echo "  - Start the VM:                 virsh start $VM_HOST"
    echo "  - Verify SSH configuration in ~/.ssh/config:"
    echo "      Host $VM_HOST"
    echo "          HostName <VM_IP>"
    echo "          User <vm_user>"
    echo "          IdentityFile ~/.ssh/<key>"
    echo ""
    echo "Refer to inventory/<vmhost>/$VM_HOST/VM.md for the complete VM provisioning runbook."
    echo ""
    exit 1
  fi
  echo "[✓] VM '$VM_HOST' is online and accessible."
fi

# Synchronize each repository
for entry in "${REPOSITORIES[@]}"; do
  IFS='|' read -r rname lpath rpath post_b64 int_port ext_port inc_b64 exc_b64 <<< "$entry"
  post_cmd="$(decode_post_cmd "${post_b64:-}")"
  int_port="${int_port:-3000}"
  ext_port="${ext_port:-3001}"
  if [[ -n "$TARGET_REPO" && "$rname" != "$TARGET_REPO" ]]; then
    continue
  fi
  echo ""
  echo "------------------------------------------------------------"
  echo " Synchronizing '$rname': $lpath -> $VM_HOST:$rpath"
  echo " Hypervisor Route: http://$HYPERVISOR_HOST:$ext_port/ -> :$int_port"
  echo "------------------------------------------------------------"

  if [[ ! -d "$lpath" ]]; then
    echo "[-] Skipping '$rname': local directory not found ($lpath)"
    continue
  fi

  # Ensure remote destination directory exists before syncing
  remote_dir="${rpath/#~/\\$HOME}"
  if [[ "$DRY_RUN" != "true" ]]; then
    ssh -q "$VM_HOST" "mkdir -p $remote_dir"
  fi

  # Synchronize with optimized exclusions and per-repo include/exclude overrides
  build_repo_filter_args "${inc_b64:-}" "${exc_b64:-}"
  rsync "${RSYNC_OPTS[@]}" \\
    "${REPO_FILTER_ARGS[@]}" \\
    "$lpath/" "$VM_HOST:$rpath"

  # Post-deploy lifecycle execution (if configured)
  if [[ -n "$post_cmd" ]]; then
    echo ""
    if [[ "$SKIP_POST_DEPLOY" == "true" ]]; then
      echo "    [-] Skipping post-deploy command (--no-restart enabled): $post_cmd"
    elif [[ "$DRY_RUN" == "true" ]]; then
      echo "    [PLAN] Post-deploy on $VM_HOST (PORT=$int_port): cd $remote_dir && export HOST=0.0.0.0 PORT=$int_port && $post_cmd"
    else
      echo "[*] Executing post-deploy command on $VM_HOST (PORT=$int_port):"
      echo "    -> cd $remote_dir && export HOST=0.0.0.0 PORT=$int_port && (nohup $post_cmd </dev/null > app.log 2>&1 &)"
      ssh "$VM_HOST" bash -s -- "$rpath" "$int_port" "$post_b64" <<'POST_DEPLOY_EOF'
set -euo pipefail
target_dir="${1/#\\~/$HOME}"
int_port="$2"
post_b64="$3"
old_pids="$(ss -tlnp "sport = :${int_port}" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true)"
if [[ -n "$old_pids" ]]; then
  kill -TERM $old_pids 2>/dev/null || true
  sleep 0.5
  kill -9 $old_pids 2>/dev/null || true
fi
cd "$target_dir"
export HOST=0.0.0.0 PORT="$int_port"
cmd_str="$(printf '%s' "$post_b64" | base64 -d)"
nohup bash -lc "$cmd_str" </dev/null > app.log 2>&1 &
sleep 1
POST_DEPLOY_EOF
      echo "[✓] Post-deploy command completed."
    fi
  fi
done

if [[ "$DRY_RUN" == "true" ]]; then
  echo ""
  echo "============================================================"
  echo " [✓] Dry-run complete. No files were modified on $VM_HOST."
  echo " Run without --dry-run to apply changes."
  echo "============================================================"
else
  echo ""
  echo "[✓] Synchronization complete across all repositories!"
fi
"""
    return (
        template.replace("__VM_HOST__", vm_host)
        .replace("__HYPERVISOR_HOST__", hypervisor_host)
        .replace("__EXCLUDE_ITEMS__", exclude_items)
        .replace("__REPOSITORY_ENTRIES__", repo_entries)
    )


def generate_dispatcher_script(default_vm: str = "") -> str:
    """Generates a top-level dispatcher script that routes synchronization to inventory VMs.

    When invoked without an explicit VM_NAME, dynamically resolves the VM whose
    vm.yaml -> vm.blueprint.repositories[].local_path aligns with the current
    working directory ($PWD). Refuses to fall back to an arbitrary default VM.
    """
    return r"""#!/usr/bin/env bash
# ==============================================================================
# AirlockVM Multi-VM Application Deployment Dispatcher
# ==============================================================================
# Auto-generated by airlock_rsync. Dispatches code synchronization to
# dedicated VM deployment scripts in inventory/<vmhost>/<vm>/deploy_app_to_airlock.sh
# (or legacy deploy_app_to_vm.sh).
#
# Target VM resolution:
#   - Explicit VM_NAME argument: dispatches directly to that VM.
#   - Omitted VM_NAME: dynamically matches $PWD against vm.yaml
#     (vm.blueprint.repositories[].local_path) across active inventory paths.
#     Never falls back to an arbitrary default VM.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve inventory search paths:
# 1. AIRLOCK_INVENTORY_DIR environment variable (highest priority)
# 2. ~/.airlock/inventory (standard user persistent directory)
INVENTORY_DIRS=()
if [[ -n "${AIRLOCK_INVENTORY_DIR:-}" ]]; then
  INVENTORY_DIRS+=("${AIRLOCK_INVENTORY_DIR}")
fi
if [[ -d "${HOME}/.airlock/inventory" ]]; then
  INVENTORY_DIRS+=("${HOME}/.airlock/inventory")
fi
if [[ ${#INVENTORY_DIRS[@]} -eq 0 ]]; then
  INVENTORY_DIRS+=("${HOME}/.airlock/inventory")
fi

list_vms() {
  echo "Available AirlockVM Virtual Machines in inventory:"
  declare -A seen_vms=()
  local found=0
  for inv in "${INVENTORY_DIRS[@]}"; do
    if [ -d "$inv" ]; then
      for s in "${inv}"/*/*/deploy_app_to_airlock.sh "${inv}"/*/*/deploy_app_to_vm.sh; do
        if [ -f "$s" ]; then
          vm_dir="$(dirname "$s")"
          vm_name="$(basename "$vm_dir")"
          host_name="$(basename "$(dirname "$vm_dir")")"
          if [ -z "${seen_vms["$vm_name"]:-}" ]; then
            seen_vms["$vm_name"]=1
            found=1
            if [[ "$s" == "${SCRIPT_DIR}/"* ]]; then
              script_disp="${s#"${SCRIPT_DIR}/"}"
            elif [[ "$s" == "${HOME}/"* ]]; then
              script_disp="~/${s#"${HOME}/"}"
            else
              script_disp="$s"
            fi
            echo "  - ${vm_name} (host: ${host_name}) -> ${script_disp}"
          fi
        fi
      done
    fi
  done
  if [ $found -eq 0 ]; then
    echo "  (No virtual machine deployment scripts found in: ${INVENTORY_DIRS[*]})"
  fi
}

resolve_vm_from_cwd() {
  local py_bin="python3"
  if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    py_bin="${SCRIPT_DIR}/.venv/bin/python"
  fi

  local inv_joined=""
  inv_joined="$(printf '%s\n' "${INVENTORY_DIRS[@]}")"

  AIRLOCK_INV_LIST="$inv_joined" "$py_bin" - <<'PYEOF'
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

cwd = os.path.realpath(os.getcwd())
inv_dirs = [Path(p) for p in os.environ.get("AIRLOCK_INV_LIST", "").splitlines() if p.strip()]

# Collect (prefix_len, vm_name) matches where cwd == local_path or is inside local_path
matches = {}  # vm_name -> best_prefix_len

for inv in inv_dirs:
    if not inv.is_dir():
        continue
    for yf in sorted(inv.glob("*/*/vm.yaml")):
        vm_name = yf.parent.name
        local_paths = []
        text = ""
        try:
            text = yf.read_text(encoding="utf-8")
        except Exception:
            continue

        if yaml is not None:
            try:
                doc = yaml.safe_load(text) or {}
                vm_data = doc.get("vm", {}) or {}
                if vm_data.get("name"):
                    vm_name = str(vm_data["name"]).strip()
                repos = (vm_data.get("blueprint", {}) or {}).get("repositories", []) or []
                for r in repos:
                    if isinstance(r, dict) and r.get("local_path"):
                        local_paths.append(str(r["local_path"]).strip())
            except Exception:
                pass
        else:
            for m in re.finditer(r"^\s*local_path:\s*[\"']?([^\"'\n]+)[\"']?", text, re.MULTILINE):
                local_paths.append(m.group(1).strip())

        for lp in local_paths:
            if not lp:
                continue
            resolved = os.path.realpath(os.path.expanduser(lp))
            if cwd == resolved or cwd.startswith(resolved.rstrip(os.sep) + os.sep):
                prev = matches.get(vm_name, -1)
                if len(resolved) > prev:
                    matches[vm_name] = len(resolved)

if not matches:
    sys.exit(2)

max_len = max(matches.values())
best_vms = sorted(vm for vm, plen in matches.items() if plen == max_len)
if len(best_vms) == 1:
    print(best_vms[0])
    sys.exit(0)

print("AMBIGUOUS:" + ",".join(best_vms))
sys.exit(3)
PYEOF
}

show_usage() {
  echo "Usage: $0 [VM_NAME | --all | --list] [RSYNC_OPTIONS...]"
  echo ""
  echo "Commands & Targets:"
  echo "  (omitted)              Auto-resolve VM matching current directory via vm.yaml repositories[].local_path"
  echo "  [VM_NAME]              Deploy app to a specific VM (e.g. my-app-vm)"
  echo "  --all                  Deploy app to all VMs in inventory sequentially"
  echo "  --list, -l             List all discovered VMs with deployment scripts"
  echo "  --help, -h             Show this help message"
  echo ""
  echo "Supported Flags (forwarded directly to the VM deployment script):"
  echo "  --dry-run              Show files that would be transferred without modifying VM"
  echo "  --delete               Delete extraneous remote files not present locally"
  echo "  --verbose, -v          Increase rsync output verbosity"
  echo "  --offline              Scan and display local sync candidate files (no network calls)"
  echo "  --no-restart           Skip executing post-deploy commands on the VM"
  echo "  --sync-only            Alias for --no-restart"
  echo ""
  list_vms
  exit 0
}

TARGET_VM=""
PASSTHROUGH_ARGS=()

if [ $# -eq 0 ]; then
  PASSTHROUGH_ARGS=()
elif [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
  show_usage
elif [ "$1" = "--list" ] || [ "$1" = "-l" ]; then
  list_vms
  exit 0
elif [ "$1" = "--all" ]; then
  shift
  echo "[*] Deploying application to all inventory VMs..."
  declare -A seen_all_vms=()
  for inv in "${INVENTORY_DIRS[@]}"; do
    if [ -d "$inv" ]; then
      for s in "${inv}"/*/*/deploy_app_to_airlock.sh "${inv}"/*/*/deploy_app_to_vm.sh; do
        if [ -f "$s" ]; then
          vm="$(basename "$(dirname "$s")")"
          if [ -z "${seen_all_vms["$vm"]:-}" ]; then
            seen_all_vms["$vm"]=1
            echo ""
            echo "============================================================"
            echo " Target VM: ${vm}"
            echo "============================================================"
            "$s" "$@"
          fi
        fi
      done
    fi
  done
  echo ""
  echo "[✓] All inventory VMs synchronized."
  exit 0
elif [[ "$1" == -* ]]; then
  # First arg is a flag (e.g. --offline, --dry-run): resolve VM dynamically from $PWD
  PASSTHROUGH_ARGS=("$@")
else
  TARGET_VM="$1"
  shift
  PASSTHROUGH_ARGS=("$@")
fi

# If no VM_NAME was explicitly supplied, resolve dynamically from $PWD via vm.yaml repositories[].local_path
if [ -z "${TARGET_VM}" ]; then
  set +e
  resolved_out="$(resolve_vm_from_cwd)"
  resolved_rc=$?
  set -e

  if [ $resolved_rc -eq 0 ] && [ -n "${resolved_out}" ]; then
    TARGET_VM="${resolved_out}"
    echo "[*] Resolved target VM from project path ($(pwd)): ${TARGET_VM}"
  elif [ $resolved_rc -eq 3 ]; then
    ambig_list="${resolved_out#AMBIGUOUS:}"
    echo "[-] Error: Multiple VMs match current directory '$(pwd)': ${ambig_list}" >&2
    echo "    Specify the target VM explicitly: $0 <VM_NAME> ${PASSTHROUGH_ARGS[*]:-}" >&2
    echo "" >&2
    list_vms >&2
    exit 1
  else
    echo "[-] Error: No VM in inventory has a repository local_path matching '$(pwd)'." >&2
    echo "    Refusing to deploy to an arbitrary default VM." >&2
    echo "    Run from a registered project directory, or specify a VM explicitly: $0 <VM_NAME>" >&2
    echo "" >&2
    list_vms >&2
    exit 1
  fi
fi

# Locate the deployment script for the target VM across inventory search paths
TARGET_SCRIPT=""
for inv in "${INVENTORY_DIRS[@]}"; do
  if [ -d "$inv" ]; then
    for s in "${inv}"/*/"${TARGET_VM}"/deploy_app_to_airlock.sh "${inv}"/*/"${TARGET_VM}"/deploy_app_to_vm.sh; do
      if [ -f "$s" ]; then
        TARGET_SCRIPT="$s"
        break 2
      fi
    done
  fi
done

if [ -z "${TARGET_SCRIPT}" ]; then
  echo "[-] Error: Could not find deployment script for VM '${TARGET_VM}' in inventory/" >&2
  echo "" >&2
  list_vms >&2
  exit 1
fi

echo "[*] Dispatching application deployment to: ${TARGET_SCRIPT}"
exec "${TARGET_SCRIPT}" "${PASSTHROUGH_ARGS[@]}"
"""


def load_vm_repos(vm_yaml_path: Path) -> tuple[str, list[dict]]:
    """Loads target VM hostname and repositories from a vm.yaml file."""
    if not vm_yaml_path.exists():
        return REPO_ROOT.name, []

    if yaml is not None:
        try:
            doc = yaml.safe_load(vm_yaml_path.read_text(encoding="utf-8")) or {}
            vm_data = doc.get("vm", {})
            vm_name = vm_data.get("name", REPO_ROOT.name)
            bp = vm_data.get("blueprint", {})
            default_int = bp.get("app_internal_port", 3000)
            default_ext = bp.get("proxy_external_port", 3001)
            repos = bp.get("repositories", [])
            if not repos:
                proj_name = bp.get("project_name", vm_name)
                remote_dest = bp.get("remote_code_dir", f"~/{proj_name}/")
                repos = [{
                    "name": proj_name,
                    "local_path": str(REPO_ROOT),
                    "remote_path": remote_dest,
                    "post_deploy": "",
                    "internal_port": default_int,
                    "external_port": default_ext,
                }]
            else:
                for r in repos:
                    if "post_deploy" not in r:
                        r["post_deploy"] = ""
                    if "internal_port" not in r:
                        r["internal_port"] = default_int
                    if "external_port" not in r:
                        r["external_port"] = default_ext
            return vm_name, repos
        except Exception:
            pass

    return REPO_ROOT.name, [{
        "name": REPO_ROOT.name,
        "local_path": str(REPO_ROOT),
        "remote_path": f"~/{REPO_ROOT.name}/",
        "post_deploy": "",
        "internal_port": 3000,
        "external_port": 3001,
    }]


def resolve_hypervisor_host(vm_yaml_path: Path | None) -> str:
    """Resolves the hypervisor hostname from vm.yaml (or its parent inventory directory)."""
    if not vm_yaml_path or not vm_yaml_path.exists():
        return "localhost"
    if yaml is not None:
        try:
            doc = yaml.safe_load(vm_yaml_path.read_text(encoding="utf-8")) or {}
            host = doc.get("vm", {}).get("host")
            if host:
                return str(host).strip()
        except Exception:
            pass
    if len(vm_yaml_path.parents) >= 2:
        return vm_yaml_path.parents[1].name
    return "localhost"


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate project and generate sync scripts for inventory VMs and project root"
    )
    parser.add_argument("--vm-host", help="Target VM host or SSH alias")
    parser.add_argument("--remote-dir", help="Destination path on VM")
    parser.add_argument("--vm", help="Target specific VM domain in inventory/")
    parser.add_argument("--all", action="store_true", help="Generate deploy_app_to_airlock.sh for all VMs across inventory/")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to output script (default: deploy_app_to_airlock.sh and/or inventory/<host>/<vm>/deploy_app_to_airlock.sh)",
    )
    args = parser.parse_args()

    inv_dir = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))
    exclusions = evaluate_project_exclusions()
    print(f"[*] Evaluated exclusions ({len(exclusions)} rules determined).")

    # Always generate deploy_app_to_airlock.sh for inventory VMs if inventory exists
    if inv_dir.exists():
        vm_yamls = sorted(inv_dir.glob("*/*/vm.yaml"))
        print(f"[*] Generating deploy_app_to_airlock.sh for {len(vm_yamls)} VM(s) in {inv_dir}...")
        for yf in vm_yamls:
            vm_name, repos = load_vm_repos(yf)
            hv_host = resolve_hypervisor_host(yf)
            vm_sync_file = yf.parent / "deploy_app_to_airlock.sh"
            content = generate_shell_script(vm_name, repos, exclusions, hypervisor_host=hv_host)
            vm_sync_file.write_text(content, encoding="utf-8")
            vm_sync_file.chmod(0o755)
            print(f"    [✓] Generated: {format_path(vm_sync_file)} ({len(repos)} repo(s))")

    # Determine default / target VM
    default_host, default_dir = detect_vm_target()
    target_host = args.vm_host or (args.vm if args.vm else default_host)

    # Locate vm.yaml for target host if in inventory
    matching_yaml = None
    if inv_dir.exists():
        candidates = list(inv_dir.glob(f"*/{target_host}/vm.yaml"))
        if candidates:
            matching_yaml = candidates[0]

    if matching_yaml:
        vm_name, repos = load_vm_repos(matching_yaml)
        target_hv_host = resolve_hypervisor_host(matching_yaml)
    else:
        vm_name = target_host
        target_hv_host = "localhost"
        repos = [{"name": REPO_ROOT.name, "local_path": str(REPO_ROOT), "remote_path": args.remote_dir or default_dir}]

    # Determine output script destination and content
    if args.output:
        # User specified custom output path -> write specific VM sync script
        out_path = args.output
        content = generate_shell_script(vm_name, repos, exclusions, hypervisor_host=target_hv_host)
        out_path.write_text(content, encoding="utf-8")
        out_path.chmod(0o755)
        print(f"[✓] Successfully generated custom deployment script: {out_path}")
    elif inv_dir.exists():
        # Inventory exists: write multi-VM dispatcher to user persistent bin dir (~/.airlock/bin/)
        # and only update repo-root script when writable, outside site-packages, and not using custom test override.
        dispatcher_content = generate_dispatcher_script()
        user_bin_dir = Path.home() / ".airlock" / "bin"
        try:
            user_bin_dir.mkdir(parents=True, exist_ok=True)
            user_dispatcher = user_bin_dir / "deploy_app_to_airlock.sh"
            user_dispatcher.write_text(dispatcher_content, encoding="utf-8")
            user_dispatcher.chmod(0o755)
        except OSError:
            pass

        in_site_packages = "site-packages" in str(REPO_ROOT) or "_assets" in str(REPO_ROOT)
        custom_inv_override = "AIRLOCK_INVENTORY_DIR" in os.environ
        if not in_site_packages and not custom_inv_override and os.access(REPO_ROOT, os.W_OK):
            root_script = DEFAULT_OUTPUT_SCRIPT
            root_script.write_text(dispatcher_content, encoding="utf-8")
            root_script.chmod(0o755)
            print(f"[✓] Successfully generated Multi-VM Dispatcher: {root_script} (dynamic CWD resolution)")
        else:
            print("[✓] Successfully generated Multi-VM Dispatcher: ~/.airlock/bin/deploy_app_to_airlock.sh (dynamic CWD resolution)")
    else:
        # Standalone project: write single VM deployment script
        root_script = DEFAULT_OUTPUT_SCRIPT
        content = generate_shell_script(vm_name, repos, exclusions)
        root_script.write_text(content, encoding="utf-8")
        root_script.chmod(0o755)
        print(f"[✓] Successfully generated executable: {root_script}")


if __name__ == "__main__":
    main()
