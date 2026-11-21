"""DevBox Model Context Protocol (MCP) Server.

Provides tools for discovering, provisioning, deploying, and managing dedicated
execution virtual machines and application workloads.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import yaml

from mcp.server.mcpserver import MCPServer

# DevBox canonical root directory
DEVBOX_ROOT = Path(__file__).resolve().parents[3]
INVENTORY_DIR = DEVBOX_ROOT / "inventory"
DEPLOY_SCRIPT = DEVBOX_ROOT / ".agents" / "skills" / "devbox_deploy" / "scripts" / "deploy_vm.py"
GENERATE_SYNC_SCRIPT = DEVBOX_ROOT / ".agents" / "skills" / "devbox_rsync" / "scripts" / "generate_sync.py"
ROOT_DISPATCHER = DEVBOX_ROOT / "deploy_app_to_vm.sh"
PYTHON_BIN = DEVBOX_ROOT / ".venv" / "bin" / "python"
if not PYTHON_BIN.exists():
    PYTHON_BIN = Path(sys.executable)

server = MCPServer("devbox")


def _run_command(cmd: list[str], timeout: int = 120) -> dict[str, Any]:
    """Executes a subprocess command safely and returns structured output."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=DEVBOX_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds: {' '.join(cmd)}",
        }
    except Exception as e:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"Execution failed: {e}",
        }


def _find_vm_yaml(vm_name: str) -> tuple[Path | None, dict[str, Any] | None]:
    """Locates vm.yaml for a given VM domain name across inventory."""
    if not INVENTORY_DIR.exists():
        return None, None

    for candidate in INVENTORY_DIR.glob(f"*/{vm_name}/vm.yaml"):
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return candidate, data
        except Exception:
            continue

    # Case-insensitive fallback
    for candidate in INVENTORY_DIR.glob("*/*/vm.yaml"):
        if candidate.parent.name.lower() == vm_name.lower():
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                return candidate, data
            except Exception:
                continue

    return None, None


@server.tool()
def devbox_ping() -> str:
    """Health check verifying that DevBox MCP server and Python runtime are responsive."""
    return f"DevBox MCP Server is online (Python {sys.version.split()[0]} | {DEVBOX_ROOT})"


@server.tool()
def devbox_list() -> dict[str, Any]:
    """Lists all registered hypervisors and VMs in inventory along with their status, ports, and repositories."""
    vms: list[dict[str, Any]] = []
    if not INVENTORY_DIR.exists():
        return {"success": True, "vms": []}

    for vm_yaml_path in sorted(INVENTORY_DIR.glob("*/*/vm.yaml")):
        try:
            with open(vm_yaml_path, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
            vm_data = doc.get("vm", {})
            bp = vm_data.get("blueprint", {})
            res = vm_data.get("resources", {})
            net = vm_data.get("network", {})
            ifaces = net.get("interfaces", [])
            ip = ifaces[0].get("ip", "unknown") if ifaces else "unknown"

            repos = bp.get("repositories", [])
            vms.append({
                "vm_name": vm_data.get("name", vm_yaml_path.parent.name),
                "host": vm_data.get("host", vm_yaml_path.parents[1].name),
                "state": vm_data.get("state", "unknown"),
                "ip": ip,
                "vcpu": res.get("vcpu", 0),
                "memory_mib": res.get("memory_mib", 0),
                "internal_port": bp.get("app_internal_port", 3000),
                "external_port": bp.get("proxy_external_port", 3001),
                "repositories": repos,
                "config_path": str(vm_yaml_path.relative_to(DEVBOX_ROOT)),
            })
        except Exception:
            continue

    return {"success": True, "vms": vms, "total_vms": len(vms)}


@server.tool()
def devbox_status(vm_name: str) -> dict[str, Any]:
    """Queries live status of a target VM from the hypervisor or inventory."""
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {
            "success": False,
            "error": f"VM '{vm_name}' not found in inventory directory.",
        }

    cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", vm_name, "--status"]
    result = _run_command(cmd, timeout=30)
    result["vm_name"] = vm_name
    result["config_path"] = str(yaml_path.relative_to(DEVBOX_ROOT))
    return result


@server.tool()
def devbox_attach_app(
    vm_name: str,
    app_name: str,
    local_path: str,
    internal_port: int = 3000,
    external_port: int = 3001,
    post_deploy: str = "",
) -> dict[str, Any]:
    """Registers or updates an application repository in inventory/{vmhost}/{vm}/vm.yaml and regenerates sync scripts.

    Args:
        vm_name: Domain name of the target VM in inventory (e.g. 'barkley-vm' or 'devbox-vm').
        app_name: Name of the application repository (e.g. 'DummyDevBoxApp').
        local_path: Absolute or relative local path to the application repository.
        internal_port: Internal port the application listens on inside the VM (e.g. 3000).
        external_port: External port mapped on the hypervisor host via systemd-socket-proxyd (e.g. 3001).
        post_deploy: Optional lifecycle command to execute inside the VM after code sync.
    """
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {
            "success": False,
            "error": f"Target VM '{vm_name}' not found in inventory.",
        }

    resolved_path = Path(local_path).expanduser().resolve()
    if not resolved_path.exists():
        return {
            "success": False,
            "error": f"Local repository path does not exist: {resolved_path}",
        }

    vm_data = doc.setdefault("vm", {})
    bp = vm_data.setdefault("blueprint", {})
    repos = bp.setdefault("repositories", [])

    matched = False
    for r in repos:
        if r.get("name") == app_name or r.get("local_path") == str(resolved_path):
            r["name"] = app_name
            r["local_path"] = str(resolved_path)
            r["remote_path"] = f"~/{app_name}/"
            r["internal_port"] = internal_port
            r["external_port"] = external_port
            r["post_deploy"] = post_deploy
            matched = True
            break

    if not matched:
        repos.append({
            "name": app_name,
            "local_path": str(resolved_path),
            "remote_path": f"~/{app_name}/",
            "internal_port": internal_port,
            "external_port": external_port,
            "post_deploy": post_deploy,
        })

    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f, sort_keys=False)

    sync_cmd = [str(PYTHON_BIN), str(GENERATE_SYNC_SCRIPT)]
    sync_res = _run_command(sync_cmd)

    return {
        "success": True,
        "vm_name": vm_name,
        "app_name": app_name,
        "config_path": str(yaml_path.relative_to(DEVBOX_ROOT)),
        "local_path": str(resolved_path),
        "internal_port": internal_port,
        "external_port": external_port,
        "post_deploy": post_deploy,
        "sync_generator": sync_res.get("stdout", ""),
        "message": f"Repository '{app_name}' successfully attached to VM '{vm_name}'.",
    }


@server.tool()
def devbox_deploy_vm(vm_name: str, dry_run: bool = False) -> dict[str, Any]:
    """Provisions or updates a virtual machine domain and its hypervisor socket proxies from inventory SSOT.

    Args:
        vm_name: Target VM domain name.
        dry_run: When True, simulates the deployment plan without modifying the hypervisor.
    """
    yaml_path, _ = _find_vm_yaml(vm_name)
    if not yaml_path:
        return {"success": False, "error": f"VM '{vm_name}' not found in inventory"}

    cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", vm_name, "--yes"]
    if dry_run:
        cmd.append("--dry-run")

    res = _run_command(cmd, timeout=300)
    res["vm_name"] = vm_name
    res["dry_run"] = dry_run
    return res


@server.tool()
def devbox_deploy_app(
    vm_name: str,
    app_name: str = "",
    dry_run: bool = False,
    no_restart: bool = False,
    offline: bool = False,
) -> dict[str, Any]:
    """Synchronizes application code to the guest VM and executes post-deploy lifecycle hooks.

    Args:
        vm_name: Target VM domain name (e.g. 'devbox-vm').
        app_name: Optional specific repository to deploy. If omitted, all attached repositories are synced.
        dry_run: When True, inspects file candidates and displays the deployment plan without syncing files.
        no_restart: When True, syncs files but skips post-deploy commands.
        offline: When True, generates local file inventory without connecting over the network.
    """
    cmd = [str(ROOT_DISPATCHER), vm_name]
    if app_name:
        cmd.extend(["--repo", app_name])
    if dry_run:
        cmd.append("--dry-run")
    if no_restart:
        cmd.append("--no-restart")
    if offline:
        cmd.append("--offline")

    res = _run_command(cmd, timeout=180)
    res["vm_name"] = vm_name
    res["app_name"] = app_name or "all"
    return res


@server.tool()
def devbox_power(vm_name: str, action: str) -> dict[str, Any]:
    """Manages VM power lifecycle (start, stop, reboot, destroy).

    Args:
        vm_name: Target VM domain name.
        action: Power action to execute ('start', 'stop', 'reboot', 'destroy').
    """
    valid_actions = ("start", "stop", "reboot", "destroy")
    if action not in valid_actions:
        return {
            "success": False,
            "error": f"Invalid action '{action}'. Must be one of {valid_actions}.",
        }

    cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", vm_name, f"--{action}"]
    res = _run_command(cmd, timeout=60)
    res["vm_name"] = vm_name
    res["action"] = action
    return res


@server.tool()
def devbox_snapshot(vm_name: str, action: str, snapshot_name: str = "") -> dict[str, Any]:
    """Manages disk-only snapshots for instant clean-slate rollbacks.

    Args:
        vm_name: Target VM domain name.
        action: Snapshot action ('create', 'revert', 'list').
        snapshot_name: Name of the snapshot (optional for revert/create; defaults to baseline).
    """
    if action == "create":
        cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", vm_name, "--snapshot"]
        if snapshot_name:
            cmd.append(snapshot_name)
    elif action == "revert":
        cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", vm_name, "--rollback"]
        if snapshot_name:
            cmd.append(snapshot_name)
    elif action == "list":
        cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", vm_name, "--status"]
    else:
        return {
            "success": False,
            "error": f"Invalid action '{action}'. Must be 'create', 'revert', or 'list'.",
        }

    res = _run_command(cmd, timeout=60)
    res["vm_name"] = vm_name
    res["action"] = action
    return res


def main() -> None:
    """Runs the DevBox MCP Server over stdio."""
    server.run()


if __name__ == "__main__":
    main()
