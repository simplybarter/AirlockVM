"""DevBox Model Context Protocol (MCP) Server.

Provides autonomous tools for discovering, provisioning, deploying, and managing dedicated
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

from devbox.detector import detect_application
from devbox.health import verify_route_health
from devbox.network import allocate_vm_network
from devbox.placement import inspect_hypervisor, select_optimal_hypervisor
from devbox.ports import allocate_external_port, get_inventory_used_ports, get_hypervisor_live_ports
from devbox.snapshots import create_pre_deploy_checkpoint, rollback_checkpoint

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


# ==============================================================================
# Core Observability & Inspection Tools
# ==============================================================================

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


# ==============================================================================
# Intelligence Engine Tools
# ==============================================================================

@server.tool()
def devbox_detect_app(local_path: str) -> dict[str, Any]:
    """Intelligently inspects a local application directory to infer stack, listening port, and post_deploy command.

    Args:
        local_path: Absolute or relative path to the application repository.
    """
    return detect_application(local_path)


@server.tool()
def devbox_find_port(
    vm_name: str,
    internal_port: int = 3000,
    preferred_port: int = 0,
) -> dict[str, Any]:
    """Audits both inventory reservations and live hypervisor TCP sockets over SSH to discover the next available external port.

    Args:
        vm_name: Domain name of the target VM in inventory.
        internal_port: Internal application listening port (e.g. 3000, 8080).
        preferred_port: Optional preferred external port to check (default 0 for auto).
    """
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {
            "success": False,
            "error": f"Target VM '{vm_name}' not found in inventory.",
        }

    vm_data = doc.get("vm", {})
    vmhost = vm_data.get("host", "localhost")
    pref = preferred_port if preferred_port > 0 else None

    port, reason = allocate_external_port(
        vmhost=vmhost,
        internal_port=internal_port,
        preferred_port=pref,
    )

    return {
        "success": True,
        "vm_name": vm_name,
        "host": vmhost,
        "internal_port": internal_port,
        "allocated_external_port": port,
        "message": reason,
    }


@server.tool()
def devbox_alloc_network(vmhost: str, network_name: str = "default") -> dict[str, Any]:
    """Allocates the next available collision-free static IP address and unique MAC address on virbr0.

    Args:
        vmhost: Target hypervisor host name in inventory (e.g. 'barkley' or 'localhost').
        network_name: Libvirt network name (defaults to 'default').
    """
    return allocate_vm_network(vmhost=vmhost, network_name=network_name)


@server.tool()
def devbox_select_host(
    required_vcpu: int = 2,
    required_ram_mib: int = 2048,
    required_disk_gb: int = 20,
) -> dict[str, Any]:
    """Audits all registered hypervisors (CPU, RAM headroom, storage, running VMs) and recommends the optimal host for placement.

    Args:
        required_vcpu: Minimum required vCPUs for the workload.
        required_ram_mib: Minimum required RAM in MiB.
        required_disk_gb: Minimum required free disk storage in GiB.
    """
    return select_optimal_hypervisor(
        required_vcpu=required_vcpu,
        required_ram_mib=required_ram_mib,
        required_disk_gb=required_disk_gb,
    )


@server.tool()
def devbox_health_check(
    vm_name: str,
    app_name: str = "",
    path: str = "/",
    retries: int = 4,
) -> dict[str, Any]:
    """Probes the live application endpoint on the hypervisor and captures crash logs from the VM if unreachable.

    Args:
        vm_name: Target VM domain name in inventory.
        app_name: Name of the application (optional; defaults to first attached repo).
        path: HTTP endpoint path to query (default '/').
        retries: Number of connection attempts before capturing failure diagnostics.
    """
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {"success": False, "error": f"VM '{vm_name}' not found."}

    vm_data = doc.get("vm", {})
    vmhost = vm_data.get("host", "localhost")
    bp = vm_data.get("blueprint", {})
    repos = bp.get("repositories", [])

    target_repo = None
    if app_name:
        target_repo = next((r for r in repos if r.get("name") == app_name), None)
    elif repos:
        target_repo = repos[0]

    if not target_repo:
        ext_port = bp.get("proxy_external_port", 3001)
    else:
        ext_port = target_repo.get("external_port", bp.get("proxy_external_port", 3001))

    return verify_route_health(
        host=vmhost,
        port=ext_port,
        path=path,
        retries=retries,
        vm_name=vm_name,
    )


@server.tool()
def devbox_checkpoint(
    vm_name: str,
    action: str = "create",
    snapshot_name: str = "",
) -> dict[str, Any]:
    """Creates a safety disk snapshot checkpoint or reverts back to one.

    Args:
        vm_name: Target VM domain name.
        action: 'create' (saves new pre-deploy snapshot) or 'revert' (rolls back).
        snapshot_name: Optional custom snapshot identifier.
    """
    if action == "create":
        return create_pre_deploy_checkpoint(vm_name, tag_prefix=snapshot_name or "snap-pre-deploy")
    elif action == "revert":
        return rollback_checkpoint(vm_name, snapshot_name=snapshot_name)
    else:
        return {"success": False, "error": "Action must be 'create' or 'revert'."}


# ==============================================================================
# Workload Lifecycle & Deployment Tools
# ==============================================================================

@server.tool()
def devbox_attach_app(
    vm_name: str,
    local_path: str,
    app_name: str = "",
    internal_port: int = 0,
    external_port: int = 0,
    post_deploy: str = "",
) -> dict[str, Any]:
    """Registers or updates an application repository in inventory/{vmhost}/{vm}/vm.yaml and regenerates sync scripts.

    Intelligent zero-config defaults:
    - If app_name, internal_port, or post_deploy are omitted/0/"", DevBox automatically
      inspects the local project files to infer the stack, port, and startup commands.
    - If external_port is 0 or omitted, DevBox audits the hypervisor to allocate the next
      conflict-free port automatically.

    Args:
        vm_name: Domain name of the target VM in inventory (e.g. 'barkley-vm' or 'devbox-vm').
        local_path: Absolute or relative local path to the application repository.
        app_name: Optional custom name for the application (auto-detected if empty).
        internal_port: Internal port the application listens on (auto-detected if 0).
        external_port: External port mapped on the hypervisor host (auto-allocated if 0).
        post_deploy: Optional lifecycle command to execute inside the VM (auto-detected if empty).
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

    # Auto-detect profile if any fields are omitted
    detection = {}
    if not app_name or internal_port <= 0 or not post_deploy:
        detection = detect_application(resolved_path)
        if not app_name:
            app_name = detection.get("app_name", resolved_path.name)
        if internal_port <= 0:
            internal_port = detection.get("internal_port", 3000)
        if not post_deploy:
            post_deploy = detection.get("post_deploy", "")

    vm_data = doc.setdefault("vm", {})
    vmhost = vm_data.get("host", "localhost")
    bp = vm_data.setdefault("blueprint", {})
    repos = bp.setdefault("repositories", [])

    # Intelligent port allocation
    pref_port = external_port if external_port > 0 else None
    allocated_port, alloc_reason = allocate_external_port(
        vmhost=vmhost,
        internal_port=internal_port,
        preferred_port=pref_port,
        exclude_vm=vm_name,
        exclude_app=app_name,
    )
    final_external_port = allocated_port

    matched = False
    for r in repos:
        if r.get("name") == app_name or r.get("local_path") == str(resolved_path):
            r["name"] = app_name
            r["local_path"] = str(resolved_path)
            r["remote_path"] = f"~/{app_name}/"
            r["internal_port"] = internal_port
            r["external_port"] = final_external_port
            r["post_deploy"] = post_deploy
            matched = True
            break

    if not matched:
        repos.append({
            "name": app_name,
            "local_path": str(resolved_path),
            "remote_path": f"~/{app_name}/",
            "internal_port": internal_port,
            "external_port": final_external_port,
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
        "external_port": final_external_port,
        "port_allocation_message": alloc_reason,
        "post_deploy": post_deploy,
        "detected_profile": detection,
        "sync_generator": sync_res.get("stdout", ""),
        "message": f"Repository '{app_name}' successfully attached to VM '{vm_name}'. {alloc_reason}",
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
    checkpoint: bool = False,
    verify_health: bool = False,
) -> dict[str, Any]:
    """Synchronizes application code to the guest VM and executes post-deploy lifecycle hooks.

    Args:
        vm_name: Target VM domain name (e.g. 'devbox-vm').
        app_name: Optional specific repository to deploy. If omitted, all attached repositories are synced.
        dry_run: When True, inspects file candidates and displays the deployment plan without syncing files.
        no_restart: When True, syncs files but skips post-deploy commands.
        offline: When True, generates local file inventory without connecting over the network.
        checkpoint: When True, takes an automated snapshot baseline before deployment.
        verify_health: When True, probes the HTTP route after deployment and returns crash diagnostics if failing.
    """
    checkpoint_res = None
    if checkpoint and not dry_run and not offline:
        checkpoint_res = create_pre_deploy_checkpoint(vm_name)

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
    if checkpoint_res:
        res["checkpoint"] = checkpoint_res

    # Optional post-deploy health check
    if verify_health and not dry_run and not offline and res.get("success"):
        res["health_check"] = devbox_health_check(vm_name=vm_name, app_name=app_name)

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
