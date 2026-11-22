"""AirlockVM Model Context Protocol (MCP) Server.

Provides autonomous tools for discovering, provisioning, deploying, and managing dedicated
execution virtual machines and application workloads.
"""

from __future__ import annotations

import datetime
import getpass
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import uuid
import yaml

from mcp.server.mcpserver import MCPServer

from airlock.detector import detect_application
from airlock.diagnostics import diagnose_workload_failure
from airlock.drift import audit_vm_drift, reconcile_vm_drift
from airlock.executor import exec_in_vm
from airlock.health import verify_route_health
from airlock.inventory import (
    find_vm_yaml,
    format_config_path,
    get_inventory_search_dirs,
    get_primary_inventory_dir,
    list_inventory_hosts,
)
from airlock.network import allocate_vm_network
from airlock.placement import select_optimal_hypervisor
from airlock.ports import allocate_external_port
from airlock.preflight import validate_deployment_preflight
from airlock.profiler import profile_workload
from airlock.security import audit_vm_security
from airlock.snapshots import create_pre_deploy_checkpoint, rollback_checkpoint
from airlock.transport import get_python_bin, resolve_asset_path, resolve_sync_dispatcher

# AirlockVM canonical root directory and script resolution
AIRLOCK_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = resolve_asset_path(".agents/skills/airlock_deploy/scripts/deploy_vm.py", AIRLOCK_ROOT)
CONFIGURE_SCRIPT = resolve_asset_path(".agents/skills/airlock_configure/scripts/configure_vm.py", AIRLOCK_ROOT)
GENERATE_SYNC_SCRIPT = resolve_asset_path(".agents/skills/airlock_rsync/scripts/generate_sync.py", AIRLOCK_ROOT)
ROOT_DISPATCHER = resolve_sync_dispatcher(AIRLOCK_ROOT)
PYTHON_BIN = get_python_bin(AIRLOCK_ROOT)

server = MCPServer("airlock")


def _run_command(cmd: list[str], timeout: int = 120) -> dict[str, Any]:
    """Executes a subprocess command safely and returns structured output."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=AIRLOCK_ROOT,
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
    return find_vm_yaml(vm_name)


# ==============================================================================
# Core Observability & Inspection Tools
# ==============================================================================

@server.tool()
def airlock_ping() -> str:
    """Health check verifying that AirlockVM MCP server and Python runtime are responsive."""
    return f"AirlockVM MCP Server is online (Python {sys.version.split()[0]} | {AIRLOCK_ROOT})"


@server.tool()
def airlock_list() -> dict[str, Any]:
    """Lists all registered hypervisors and VMs across active inventory paths along with their status, ports, and repositories."""
    vms: list[dict[str, Any]] = []
    seen_vms: set[str] = set()

    for inv_dir in get_inventory_search_dirs():
        if not inv_dir.exists():
            continue
        for vm_yaml_path in sorted(inv_dir.glob("*/*/vm.yaml")):
            try:
                with open(vm_yaml_path, "r", encoding="utf-8") as f:
                    doc = yaml.safe_load(f) or {}
                vm_data = doc.get("vm", {})
                vm_name = vm_data.get("name", vm_yaml_path.parent.name)
                if vm_name in seen_vms:
                    continue
                seen_vms.add(vm_name)

                bp = vm_data.get("blueprint", {})
                res = vm_data.get("resources", {})
                net = vm_data.get("network", {})
                ifaces = net.get("interfaces", [])
                ip = ifaces[0].get("ip", "unknown") if ifaces else "unknown"

                repos = bp.get("repositories", [])
                vms.append({
                    "vm_name": vm_name,
                    "host": vm_data.get("host", vm_yaml_path.parents[1].name),
                    "state": vm_data.get("state", "unknown"),
                    "ip": ip,
                    "vcpu": res.get("vcpu", 0),
                    "memory_mib": res.get("memory_mib", 0),
                    "internal_port": bp.get("app_internal_port", 3000),
                    "external_port": bp.get("proxy_external_port", 3001),
                    "repositories": repos,
                    "config_path": format_config_path(vm_yaml_path),
                })
            except Exception:
                continue

    return {"success": True, "vms": vms, "total_vms": len(vms)}


@server.tool()
def airlock_status(vm_name: str) -> dict[str, Any]:
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
    result["config_path"] = format_config_path(yaml_path)
    return result


# ==============================================================================
# Intelligence Engine Tools
# ==============================================================================

@server.tool()
def airlock_detect_app(local_path: str) -> dict[str, Any]:
    """Intelligently inspects a local application directory to infer stack, listening port, and post_deploy command.

    Args:
        local_path: Absolute or relative path to the application repository.
    """
    return detect_application(local_path)


@server.tool()
def airlock_find_port(
    vm_name: str = "",
    internal_port: int = 3000,
    preferred_port: int = 0,
    vmhost: str = "",
) -> dict[str, Any]:
    """Audits both inventory reservations and live hypervisor TCP sockets over SSH to discover the next available external port.

    Args:
        vm_name: Optional domain name of the target VM in inventory.
        internal_port: Internal application listening port (e.g. 3000, 8080).
        preferred_port: Optional preferred external port to check (default 0 for auto).
        vmhost: Target hypervisor host (auto-detected if omitted).
    """
    # Handle case where user or caller passed an integer as first positional argument
    if isinstance(vm_name, int) or (isinstance(vm_name, str) and vm_name.isdigit()):
        internal_port = int(vm_name)
        vm_name = ""

    target_host = vmhost
    if vm_name:
        yaml_path, doc = _find_vm_yaml(vm_name)
        if yaml_path and doc:
            target_host = doc.get("vm", {}).get("host", target_host)

    if not target_host:
        hosts = list_inventory_hosts()
        target_host = hosts[0] if hosts else "remotehost"

    pref = preferred_port if preferred_port > 0 else None

    port, reason = allocate_external_port(
        vmhost=target_host,
        internal_port=internal_port,
        preferred_port=pref,
    )

    return {
        "success": True,
        "vm_name": vm_name or "unassigned",
        "host": target_host,
        "internal_port": internal_port,
        "allocated_external_port": port,
        "message": reason,
    }


@server.tool()
def airlock_alloc_network(vmhost: str = "", network_name: str = "default") -> dict[str, Any]:
    """Allocates the next available collision-free static IP address and unique MAC address on virbr0.

    Args:
        vmhost: Target hypervisor host name in inventory (e.g. 'remotehost' or 'localhost'). Defaults to first registered host.
        network_name: Libvirt network name (defaults to 'default').
    """
    if not vmhost:
        hosts = list_inventory_hosts()
        vmhost = hosts[0] if hosts else "remotehost"

    return allocate_vm_network(vmhost=vmhost, network_name=network_name)


@server.tool()
def airlock_select_host(
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
def airlock_health_check(
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
def airlock_diagnose(vm_name: str, app_name: str = "") -> dict[str, Any]:
    """Performs deep failure diagnosis when an application fails to start or respond.

    Audits domain state, hypervisor proxy unit, guest internal port listeners,
    and runtime journal errors to isolate the root cause and propose remediation.

    Args:
        vm_name: Target VM domain name in inventory.
        app_name: Optional application name.
    """
    return diagnose_workload_failure(vm_name=vm_name, app_name=app_name)


@server.tool()
def airlock_checkpoint(
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


@server.tool()
def airlock_audit_drift(vm_name: str) -> dict[str, Any]:
    """Audits live hypervisor domain and systemd proxy socket state against inventory SSOT.

    Detects discrepancies in execution state, vCPUs, RAM allocation, MAC address, and proxy units.

    Args:
        vm_name: Target VM domain name in inventory.
    """
    return audit_vm_drift(vm_name)


@server.tool()
def airlock_reconcile(vm_name: str, strategy: str = "refresh_inventory") -> dict[str, Any]:
    """Reconciles detected drift between inventory SSOT and live hypervisor.

    Args:
        vm_name: Target VM domain name.
        strategy: 'refresh_inventory' (updates vm.yaml from live state) or 'enforce_specification' (applies vm.yaml to hypervisor).
    """
    return reconcile_vm_drift(vm_name, strategy=strategy)


@server.tool()
def airlock_profile_workload(vm_name: str, duration_sec: int = 2) -> dict[str, Any]:
    """Measures real-time CPU, RAM, swap, and process load inside a running VM and computes rightsizing recommendations.

    Args:
        vm_name: Target VM domain name in inventory.
        duration_sec: Sampling window in seconds (default 2).
    """
    return profile_workload(vm_name=vm_name, duration_sec=duration_sec)


@server.tool()
def airlock_audit_security(vm_name: str, app_name: str = "") -> dict[str, Any]:
    """Audits security isolation boundaries across development workstation and guest VM.

    Verifies zero credentials/secrets leaked into guest, audits AppArmor confinement,
    and confirms zero workload processes running on local workstation.

    Args:
        vm_name: Target VM domain name in inventory.
        app_name: Optional application name.
    """
    return audit_vm_security(vm_name=vm_name, app_name=app_name)


# ==============================================================================
# Workload Lifecycle & Deployment Tools
# ==============================================================================

@server.tool()
def airlock_create_vm(
    local_path: str,
    vmhost: str = "",
    vm_name: str = "",
    app_name: str = "",
    internal_port: int = 0,
    external_port: int = 0,
    post_deploy: str = "",
    vcpu: int = 4,
    memory_mib: int = 2048,
    disk_gib: int = 50,
) -> dict[str, Any]:
    """Generates a new dedicated execution virtual machine specification and runbook in inventory/{vmhost}/{vm}/.

    Automates:
    - Tech stack and port inference from local repository manifests.
    - Optimal hypervisor host selection if unspecified.
    - Static DHCP IP and unique MAC reservation on virbr0.
    - Conflict-free hypervisor proxy port allocation.
    - inventory/{vmhost}/{vm}/vm.yaml generation (Single Source of Truth).
    - inventory/{vmhost}/{vm}/VM.md generation (operational runbook).
    - Synchronizing multi-VM deployment scripts via airlock_rsync.

    Args:
        local_path: Absolute or relative local path to the application repository.
        vmhost: Target hypervisor host ('remotehost', 'localhost'). Auto-selected if omitted.
        vm_name: Target VM domain name. Derived from app_name if omitted.
        app_name: Application display name. Inferred from manifests if omitted.
        internal_port: Application internal listening port. Inferred if 0.
        external_port: Hypervisor proxy forwarding port. Auto-allocated if 0.
        post_deploy: Post-deploy startup command. Inferred if omitted.
        vcpu: Number of virtual CPUs to allocate (default 4).
        memory_mib: RAM allocation in MiB (default 2048).
        disk_gib: Virtual disk storage in GiB (default 50).
    """
    resolved_path = Path(local_path).expanduser().resolve()
    if not resolved_path.exists():
        return {
            "success": False,
            "error": f"Local repository path does not exist: {resolved_path}",
        }

    # Auto-detect profile
    detection = detect_application(resolved_path)
    if not app_name:
        app_name = detection.get("app_name", resolved_path.name)
    if internal_port <= 0:
        internal_port = detection.get("internal_port", 3000)
    if not post_deploy:
        post_deploy = detection.get("post_deploy", "")

    slug = re.sub(r"[^a-z0-9]+", "-", app_name.lower()).strip("-")
    if not vm_name:
        vm_name = f"{slug}-vm"

    # Select optimal hypervisor if not specified
    if not vmhost:
        placement = select_optimal_hypervisor(
            required_vcpu=vcpu,
            required_ram_mib=memory_mib,
            required_disk_gb=disk_gib,
        )
        if placement.get("success") and (placement.get("selected_hypervisor") or placement.get("selected_host")):
            vmhost = placement.get("selected_hypervisor") or placement.get("selected_host")
        else:
            hosts = list_inventory_hosts()
            vmhost = hosts[0] if hosts else "remotehost"

    # Allocate collision-free network IP & MAC
    net_alloc = allocate_vm_network(vmhost=vmhost)
    allocated_ip = net_alloc.get("allocated_ip", "192.168.122.238")
    allocated_mac = net_alloc.get("allocated_mac", "52:54:00:52:2e:25")

    # Allocate external port
    pref_port = external_port if external_port > 0 else None
    allocated_port, alloc_reason = allocate_external_port(
        vmhost=vmhost,
        internal_port=internal_port,
        preferred_port=pref_port,
        exclude_vm=vm_name,
        exclude_app=app_name,
    )

    vm_dir = get_primary_inventory_dir(ensure_exists=True) / vmhost / vm_name
    vm_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = vm_dir / "vm.yaml"
    runbook_path = vm_dir / "VM.md"

    # Build SSOT YAML structure
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    vm_doc = {
        "vm": {
            "name": vm_name,
            "host": vmhost,
            "state": "shut off",
            "id": "-",
            "uuid": str(uuid.uuid4()),
            "os_type": "hvm",
            "resources": {
                "vcpu": vcpu,
                "memory_mib": memory_mib,
                "memory_gb": max(1, memory_mib // 1024),
            },
            "disks": [
                {
                    "device": "disk",
                    "target": "vda",
                    "bus": "virtio",
                    "format": "qcow2",
                    "source": f"/var/lib/libvirt/images/{vm_name}.snapshot-base-clean",
                },
                {
                    "device": "disk",
                    "target": "vdb",
                    "bus": "virtio",
                    "format": "qcow2",
                    "source": f"/var/lib/libvirt/images/{vm_name}-seed.snapshot-base-clean",
                },
            ],
            "network": {
                "interfaces": [
                    {
                        "type": "network",
                        "source": "default",
                        "mac": allocated_mac,
                        "ip": allocated_ip,
                        "model": "virtio",
                    }
                ]
            },
            "security": {
                "apparmor_profile": "unconfined",
                "guest_agent_channel": True,
            },
            "blueprint": {
                "project_name": app_name,
                "primary_user": "airlock",
                "secondary_user": getpass.getuser(),
                "ssh_key_path": f"~/.ssh/{slug}_vm_ed25519",
                "app_internal_port": internal_port,
                "proxy_external_port": allocated_port,
                "proxy_socket_service": f"/etc/systemd/system/{slug}-proxy.socket",
                "remote_code_dir": f"~/{slug}/",
                "guest_os": "Ubuntu 24.04 LTS Minimal",
                "guest_os_details": "Pristine Noble Numbat cloud image",
                "cpu_mode": "host-passthrough",
                "balloon_driver": "virtio-balloon",
                "disk_size_gib": disk_gib,
                "disk_format": "qcow2",
                "uefi_firmware": "OVMF_CODE_4M.ms.fd",
                "snapshot_base_name": "snapshot-base-clean",
                "snapshot_base_desc": "Pristine base setup with Docker and Python",
                "repositories": [
                    {
                        "name": app_name,
                        "local_path": str(resolved_path),
                        "remote_path": f"~/{slug}/",
                        "internal_port": internal_port,
                        "external_port": allocated_port,
                        "post_deploy": post_deploy,
                    }
                ],
            },
            "metadata": {
                "directory": f"inventory/{vmhost}/{vm_name}/",
                "record_path": f"inventory/{vmhost}/{vm_name}/vm.yaml",
                "last_discovered": now_iso,
            },
        }
    }

    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(vm_doc, f, sort_keys=False)

    # Render VM.md runbook via configure_vm.py
    conf_cmd = [
        str(PYTHON_BIN),
        str(CONFIGURE_SCRIPT),
        "--from-inventory", vmhost,
        "--set", f"PROJECT_NAME={app_name}",
        "--set", f"VM_NAME={vm_name}",
        "--set", f"VM_IP={allocated_ip}",
        "--set", f"VM_MAC={allocated_mac}",
        "--set", f"APP_INTERNAL_PORT={internal_port}",
        "--set", f"PROXY_EXTERNAL_PORT={allocated_port}",
        "--set", f"PROXY_SOCKET_SERVICE=/etc/systemd/system/{slug}-proxy.socket",
        "--set", f"REMOTE_CODE_DIR=~/{slug}/",
        "--set", f"SSH_KEY_PATH=~/.ssh/{slug}_vm_ed25519",
        "--output", str(runbook_path),
    ]
    conf_res = _run_command(conf_cmd)

    # Regenerate sync scripts
    sync_cmd = [str(PYTHON_BIN), str(GENERATE_SYNC_SCRIPT)]
    sync_res = _run_command(sync_cmd)

    warnings: list[str] = []
    if not conf_res.get("success"):
        warnings.append(f"Runbook generation failed: {conf_res.get('stderr') or conf_res.get('stdout')}")
    if not sync_res.get("success"):
        warnings.append(f"Sync script generation failed: {sync_res.get('stderr') or sync_res.get('stdout')}")

    return {
        "success": True,
        "vm_name": vm_name,
        "host": vmhost,
        "app_name": app_name,
        "config_path": format_config_path(yaml_path),
        "runbook_path": format_config_path(runbook_path),
        "local_path": str(resolved_path),
        "allocated_ip": allocated_ip,
        "allocated_mac": allocated_mac,
        "internal_port": internal_port,
        "external_port": allocated_port,
        "post_deploy": post_deploy,
        "detected_profile": detection,
        "port_allocation_message": alloc_reason,
        "runbook_generated": bool(conf_res.get("success")),
        "sync_generated": bool(sync_res.get("success")),
        "sync_generator": sync_res.get("stdout", ""),
        "warnings": warnings,
        "message": f"Successfully created dedicated VM '{vm_name}' for '{app_name}' on '{vmhost}'.",
    }


@server.tool()
def airlock_attach_app(
    vm_name: str,
    local_path: str,
    app_name: str = "",
    internal_port: int = 0,
    external_port: int = 0,
    post_deploy: str = "",
) -> dict[str, Any]:
    """Registers or updates an application repository in inventory/{vmhost}/{vm}/vm.yaml and regenerates sync scripts.

    Intelligent zero-config defaults:
    - If app_name, internal_port, or post_deploy are omitted/0/"", AirlockVM automatically
      inspects the local project files to infer the stack, port, and startup commands.
    - If external_port is 0 or omitted, AirlockVM audits the hypervisor to allocate the next
      conflict-free port automatically.

    Args:
        vm_name: Domain name of the target VM in inventory (e.g. 'remotehost-vm' or 'airlock-vm').
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

    remote_slug = re.sub(r"[^a-z0-9._-]+", "-", app_name.lower()).strip("-") or "app"
    matched = False
    for r in repos:
        if r.get("name") == app_name or r.get("local_path") == str(resolved_path):
            r["name"] = app_name
            r["local_path"] = str(resolved_path)
            r["remote_path"] = f"~/{remote_slug}/"
            r["internal_port"] = internal_port
            r["external_port"] = final_external_port
            r["post_deploy"] = post_deploy
            matched = True
            break

    if not matched:
        repos.append({
            "name": app_name,
            "local_path": str(resolved_path),
            "remote_path": f"~/{remote_slug}/",
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
        "config_path": format_config_path(yaml_path),
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
def airlock_preflight(vm_name: str) -> dict[str, Any]:
    """Audits live hypervisor state to confirm that a VM specification is ready to be deployed.

    Verifies:
    1. SSH and passwordless sudo on hypervisor.
    2. Hardware virtualization (/dev/kvm present and writable).
    3. Libvirt daemon responsiveness.
    4. Static IP silence (ensures target IP is not actively occupied).
    5. Hypervisor proxy port availability.
    6. Storage pool headroom on backing disk.
    7. Host RAM safety headroom.
    8. Base cloud image cache status.

    Args:
        vm_name: Target VM domain name in inventory.
    """
    return validate_deployment_preflight(vm_name=vm_name)


@server.tool()
def airlock_deploy_vm(vm_name: str, dry_run: bool = False) -> dict[str, Any]:
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
def airlock_deploy_app(
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
        vm_name: Target VM domain name (e.g. 'airlock-vm').
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

    dispatcher = resolve_sync_dispatcher(AIRLOCK_ROOT)
    cmd = [str(dispatcher), vm_name]
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
        res["health_check"] = airlock_health_check(vm_name=vm_name, app_name=app_name)

    return res


@server.tool()
def airlock_power(vm_name: str, action: str) -> dict[str, Any]:
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
def airlock_snapshot(vm_name: str, action: str, snapshot_name: str = "") -> dict[str, Any]:
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


@server.tool()
def airlock_exec(
    vm_name: str,
    command: str,
    workdir: str = "",
    timeout: int = 120,
) -> dict[str, Any]:
    """Executes a build, test, or diagnostic shell command inside an isolated guest VM.

    Args:
        vm_name: Target VM domain name in inventory.
        command: Shell command to run inside the guest (e.g. 'pytest', 'npm test', 'docker ps').
        workdir: Optional working directory inside the guest (defaults to blueprint remote_code_dir).
        timeout: Maximum execution time in seconds (1..600, default 120).
    """
    return exec_in_vm(vm_name=vm_name, command=command, workdir=workdir, timeout=timeout)


@server.tool()
def airlock_bake_image(
    vmhost: str = "localhost",
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Pre-bakes a golden Ubuntu 24.04 qcow2 base image with Docker, Node.js, and Python on a hypervisor.

    New VMs cloned from the golden image skip cloud-init package downloads and boot to SSH in seconds.

    Args:
        vmhost: Target hypervisor host ('localhost' or remote SSH alias).
        force: Overwrite an existing golden image if already present.
        dry_run: Preview the image baking plan without modifying hypervisor storage.
    """
    cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--bake-golden", "--host", vmhost]
    if force:
        cmd.append("--force")
    if dry_run:
        cmd.append("--dry-run")

    res = _run_command(cmd, timeout=600)
    res["host"] = vmhost
    res["dry_run"] = dry_run
    return res


def main() -> None:
    """Runs the AirlockVM MCP Server over stdio."""
    server.run()


if __name__ == "__main__":
    main()
