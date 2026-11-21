#!/usr/bin/env python3
"""
DevBox Rsync Generator
Evaluates the project tech stack, determines essential runtime files,
and generates an optimized 'deploy_app_to_vm.sh' in the project root.
"""

import argparse
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
DEFAULT_OUTPUT_SCRIPT = REPO_ROOT / "deploy_app_to_vm.sh"


def detect_vm_target() -> tuple[str, str]:
    """Inspects VM specification in inventory/ or defaults to project name."""
    project_name = REPO_ROOT.name
    slug = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-")
    default_vm_host = f"{slug}-vm"
    default_remote_dir = f"~/{project_name}/"

    inv_dir = REPO_ROOT / "inventory"
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
        ".github/",
        ".gitignore",
        ".gitattributes",
        # Agent & AI configuration trees
        ".agents/",
        ".gemini/",
        ".claude/",
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
        "deploy_app_to_vm.sh",
        "sync_to_devbox.sh",
        "install_skill.sh",
        "connect_devbox.sh",
        # Local secrets and environment overrides
        ".env*.local",
        "*.local.json",
        "my_*.json",
        "*.pem",
        "*.key",
    ]

    # Detect language / framework-specific build artifacts & dependencies
    has_python = any(REPO_ROOT.glob("*.py")) or (REPO_ROOT / "requirements.txt").exists() or (REPO_ROOT / "pyproject.toml").exists()
    has_node = (REPO_ROOT / "package.json").exists()
    has_rust = (REPO_ROOT / "Cargo.toml").exists()
    has_go = (REPO_ROOT / "go.mod").exists()

    # Python exclusions
    if has_python or True:
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
        ])

    # Node.js / Web exclusions
    if has_node or True:
        exclusions.extend([
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
    if has_rust:
        exclusions.append("target/")

    # Remove duplicates preserving order
    seen = set()
    unique_exclusions = []
    for item in exclusions:
        if item not in seen:
            seen.add(item)
            unique_exclusions.append(item)

    return unique_exclusions


def generate_shell_script(vm_host: str, repos: list[dict], exclusions: list[str]) -> str:
    """Renders a robust deploy_app_to_vm.sh bash script supporting multi-repo synchronization."""
    exclude_items = "\n".join(f"  --exclude='{exc}'" for exc in exclusions)
    repo_entries = "\n".join(
        f'  "{r["name"]}|{r["local_path"]}|{r["remote_path"]}|{r.get("post_deploy", "")}|{r.get("internal_port", 3000)}|{r.get("external_port", 3001)}"'
        for r in repos
    )

    template = """#!/usr/bin/env bash
# ==============================================================================
# DevBox Code Synchronizer & Deployment Engine
# Synchronizes application source code to __VM_HOST__
# Auto-generated by .agents/skills/devbox_rsync
# ==============================================================================

set -euo pipefail

VM_HOST="__VM_HOST__"
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

# Configured application repositories (name|local_path|remote_path|post_deploy|internal_port|external_port)
REPOSITORIES=(
__REPOSITORY_ENTRIES__
)

TARGET_REPO=""

show_help() {
  cat <<EOF
Usage: $0 [OPTIONS]

Syncs application repositories to $VM_HOST and optionally executes post-deploy hooks.

Configured Repositories:
$(for entry in "${REPOSITORIES[@]}"; do
  IFS='|' read -r rname lpath rpath post_cmd int_port ext_port <<< "$entry"
  post_cmd="${post_cmd:-}"
  int_port="${int_port:-3000}"
  ext_port="${ext_port:-3001}"
  echo "  - $rname: $lpath -> $VM_HOST:$rpath (http://$VM_HOST:$ext_port/ -> :$int_port)"
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
  echo " DevBox Local File Inventory (Offline Evaluation)"
  echo " Target VM: $VM_HOST"
  echo "============================================================"
  for entry in "${REPOSITORIES[@]}"; do
    IFS='|' read -r rname lpath rpath post_cmd <<< "$entry"
    post_cmd="${post_cmd:-}"
    if [[ -n "$TARGET_REPO" && "$rname" != "$TARGET_REPO" ]]; then
      continue
    fi
    echo "[*] Repository '$rname': $lpath"
    if [[ ! -d "$lpath" ]]; then
      echo "    [-] WARNING: Local directory does not exist: $lpath"
      continue
    fi
    rsync -r --list-only "${EXCLUDE_ARGS[@]}" "$lpath/" | awk -v name="$rname" 'NR>1 {
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
echo " DevBox Remote Sync: Workstation -> $VM_HOST"
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
  IFS='|' read -r rname lpath rpath post_cmd int_port ext_port <<< "$entry"
  post_cmd="${post_cmd:-}"
  int_port="${int_port:-3000}"
  ext_port="${ext_port:-3001}"
  if [[ -n "$TARGET_REPO" && "$rname" != "$TARGET_REPO" ]]; then
    continue
  fi
  echo ""
  echo "------------------------------------------------------------"
  echo " Synchronizing '$rname': $lpath -> $VM_HOST:$rpath"
  echo " Hypervisor Route: http://$VM_HOST:$ext_port/ -> :$int_port"
  echo "------------------------------------------------------------"

  if [[ ! -d "$lpath" ]]; then
    echo "[-] Skipping '$rname': local directory not found ($lpath)"
    continue
  fi

  # Ensure remote destination directory exists before syncing
  if [[ "$DRY_RUN" != "true" ]]; then
    ssh -q "$VM_HOST" "mkdir -p '$rpath'"
  fi

  # Synchronize with optimized exclusions
  rsync "${RSYNC_OPTS[@]}" \\
    "${EXCLUDE_ARGS[@]}" \\
    "$lpath/" "$VM_HOST:$rpath"

  # Post-deploy lifecycle execution (if configured)
  if [[ -n "$post_cmd" ]]; then
    echo ""
    if [[ "$SKIP_POST_DEPLOY" == "true" ]]; then
      echo "    [-] Skipping post-deploy command (--no-restart enabled): $post_cmd"
    elif [[ "$DRY_RUN" == "true" ]]; then
      echo "    [PLAN] Post-deploy on $VM_HOST (PORT=$int_port): cd '$rpath' && export HOST=0.0.0.0 PORT=$int_port && $post_cmd"
    else
      echo "[*] Executing post-deploy command on $VM_HOST (PORT=$int_port):"
      echo "    -> cd '$rpath' && export HOST=0.0.0.0 PORT=$int_port && $post_cmd"
      ssh "$VM_HOST" "cd '$rpath' && export HOST=0.0.0.0 PORT=$int_port && $post_cmd"
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
        .replace("__EXCLUDE_ITEMS__", exclude_items)
        .replace("__REPOSITORY_ENTRIES__", repo_entries)
    )


def generate_dispatcher_script(default_vm: str = "devbox-vm") -> str:
    """Generates a top-level dispatcher script that routes synchronization to inventory VMs."""
    return f"""#!/usr/bin/env bash
# ==============================================================================
# DevBox Multi-VM Application Deployment Dispatcher
# ==============================================================================
# Auto-generated by devbox_rsync. Dispatches code synchronization to
# dedicated VM deployment scripts in inventory/<vmhost>/<vm>/deploy_app_to_vm.sh.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
INVENTORY_DIR="${{SCRIPT_DIR}}/inventory"
DEFAULT_VM="{default_vm}"

list_vms() {{
  echo "Available DevBox Virtual Machines in inventory:"
  if [ -d "${{INVENTORY_DIR}}" ]; then
    for s in "${{INVENTORY_DIR}}"/*/*/deploy_app_to_vm.sh; do
      if [ -f "$s" ]; then
        vm_dir="$(dirname "$s")"
        vm_name="$(basename "$vm_dir")"
        host_name="$(basename "$(dirname "$vm_dir")")"
        if [ "${{vm_name}}" = "${{DEFAULT_VM}}" ]; then
          echo "  - ${{vm_name}} (host: ${{host_name}}) [DEFAULT] -> ${{vm_dir}}/deploy_app_to_vm.sh"
        else
          echo "  - ${{vm_name}} (host: ${{host_name}}) -> ${{vm_dir}}/deploy_app_to_vm.sh"
        fi
      fi
    done
  else
    echo "  (No inventory directory found at ${{INVENTORY_DIR}})"
  fi
}}

show_usage() {{
  echo "Usage: $0 [VM_NAME | --all | --list] [RSYNC_OPTIONS...]"
  echo ""
  echo "Commands & Targets:"
  echo "  [VM_NAME]              Deploy app to a specific VM (e.g. ${{DEFAULT_VM}}, appsurfer-vm)"
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
}}

if [ $# -eq 0 ]; then
  TARGET_VM="${{DEFAULT_VM}}"
  PASSTHROUGH_ARGS=()
elif [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
  show_usage
elif [ "$1" = "--list" ] || [ "$1" = "-l" ]; then
  list_vms
  exit 0
elif [ "$1" = "--all" ]; then
  shift
  echo "[*] Deploying application to all inventory VMs..."
  for s in "${{INVENTORY_DIR}}"/*/*/deploy_app_to_vm.sh; do
    if [ -f "$s" ]; then
      vm="$(basename "$(dirname "$s")")"
      echo ""
      echo "============================================================"
      echo " Target VM: ${{vm}}"
      echo "============================================================"
      "$s" "$@"
    fi
  done
  echo ""
  echo "[✓] All inventory VMs synchronized."
  exit 0
elif [[ "$1" == -* ]]; then
  # First arg is a flag (e.g. --offline, --dry-run), default to DEFAULT_VM
  TARGET_VM="${{DEFAULT_VM}}"
  PASSTHROUGH_ARGS=("$@")
else
  TARGET_VM="$1"
  shift
  PASSTHROUGH_ARGS=("$@")
fi

# Locate the deployment script for the target VM
TARGET_SCRIPT=""
if [ -d "${{INVENTORY_DIR}}" ]; then
  for s in "${{INVENTORY_DIR}}"/*/"${{TARGET_VM}}"/deploy_app_to_vm.sh; do
    if [ -f "$s" ]; then
      TARGET_SCRIPT="$s"
      break
    fi
  done
fi

if [ -z "${{TARGET_SCRIPT}}" ]; then
  echo "[-] Error: Could not find deployment script for VM '${{TARGET_VM}}' in inventory/" >&2
  echo "" >&2
  list_vms >&2
  exit 1
fi

echo "[*] Dispatching application deployment to: ${{TARGET_SCRIPT}}"
exec "${{TARGET_SCRIPT}}" "${{PASSTHROUGH_ARGS[@]}}"
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


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate project and generate sync scripts for inventory VMs and project root"
    )
    parser.add_argument("--vm-host", help="Target VM host or SSH alias")
    parser.add_argument("--remote-dir", help="Destination path on VM")
    parser.add_argument("--vm", help="Target specific VM domain in inventory/")
    parser.add_argument("--all", action="store_true", help="Generate deploy_app_to_vm.sh for all VMs across inventory/")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to output script (default: deploy_app_to_vm.sh and/or inventory/<host>/<vm>/deploy_app_to_vm.sh)",
    )
    args = parser.parse_args()

    inv_dir = REPO_ROOT / "inventory"
    exclusions = evaluate_project_exclusions()
    print(f"[*] Evaluated exclusions ({len(exclusions)} rules determined).")

    # Always generate deploy_app_to_vm.sh for inventory VMs if inventory/ exists
    if inv_dir.exists():
        vm_yamls = sorted(inv_dir.glob("*/*/vm.yaml"))
        print(f"[*] Generating deploy_app_to_vm.sh for {len(vm_yamls)} VM(s) in inventory/...")
        for yf in vm_yamls:
            vm_name, repos = load_vm_repos(yf)
            vm_sync_file = yf.parent / "deploy_app_to_vm.sh"
            content = generate_shell_script(vm_name, repos, exclusions)
            vm_sync_file.write_text(content, encoding="utf-8")
            vm_sync_file.chmod(0o755)
            print(f"    [✓] Generated: {vm_sync_file.relative_to(REPO_ROOT)} ({len(repos)} repo(s))")

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
    else:
        vm_name = target_host
        repos = [{"name": REPO_ROOT.name, "local_path": str(REPO_ROOT), "remote_path": args.remote_dir or default_dir}]

    # Determine output script destination and content
    if args.output:
        # User specified custom output path -> write specific VM sync script
        out_path = args.output
        content = generate_shell_script(vm_name, repos, exclusions)
        out_path.write_text(content, encoding="utf-8")
        out_path.chmod(0o755)
        print(f"[✓] Successfully generated custom deployment script: {out_path}")
    elif inv_dir.exists():
        # Inventory exists: write multi-VM dispatcher to project root deploy_app_to_vm.sh
        root_script = DEFAULT_OUTPUT_SCRIPT
        dispatcher_content = generate_dispatcher_script(default_vm=target_host)
        root_script.write_text(dispatcher_content, encoding="utf-8")
        root_script.chmod(0o755)
        print(f"[✓] Successfully generated Multi-VM Dispatcher: {root_script} (Default: {target_host})")
    else:
        # Standalone project: write single VM deployment script
        root_script = DEFAULT_OUTPUT_SCRIPT
        content = generate_shell_script(vm_name, repos, exclusions)
        root_script.write_text(content, encoding="utf-8")
        root_script.chmod(0o755)
        print(f"[✓] Successfully generated executable: {root_script}")


if __name__ == "__main__":
    main()
