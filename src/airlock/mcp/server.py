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
    find_vmhost_yaml,
    format_config_path,
    get_inventory_search_dirs,
    get_primary_inventory_dir,
    list_inventory_hosts,
)
from airlock.network import allocate_vm_network
from airlock.placement import inspect_hypervisor, select_optimal_hypervisor
from airlock.ports import allocate_external_port
from airlock.preflight import validate_deployment_preflight
from airlock.profiler import profile_workload
from airlock.security import audit_vm_security
from airlock.snapshots import create_pre_deploy_checkpoint, rollback_checkpoint
from airlock.transport import get_python_bin, resolve_asset_path, resolve_sync_dispatcher, validate_identifier

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


def _proxy_socket_path_for_host(vmhost: str, vm_name: str, ext_port: int) -> str:
    """Returns ~/.config/systemd/user/... for rootless hosts, else /etc/systemd/system/..."""
    _, hdoc = find_vmhost_yaml(vmhost)
    priv_mode = ""
    if hdoc:
        priv_mode = str((hdoc.get("vmhost", {}).get("connection", {}) or {}).get("privilege_mode", "")).strip().lower()
    if priv_mode == "rootless":
        return f"~/.config/systemd/user/{vm_name}-p{ext_port}-proxy.socket"
    return f"/etc/systemd/system/{vm_name}-p{ext_port}-proxy.socket"


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
def airlock_list_hypervisors(live_probe: bool = False) -> dict[str, Any]:
    """Lists all registered hypervisors in inventory along with hardware capacity, storage pools, networks, and VM counts.

    Args:
        live_probe: When True, probes each hypervisor over SSH/local shell for live available RAM and 1m load average.
    """
    hypervisors: list[dict[str, Any]] = []
    seen_hosts: set[str] = set()

    for inv_dir in get_inventory_search_dirs():
        if not inv_dir.exists():
            continue
        for host_yaml_path in sorted(inv_dir.glob("*/vmhost.yaml")):
            host_key = host_yaml_path.parent.name
            if host_key in seen_hosts:
                continue
            try:
                with open(host_yaml_path, "r", encoding="utf-8") as f:
                    doc = yaml.safe_load(f) or {}
                vmhost = doc.get("vmhost", {})
                if not isinstance(vmhost, dict):
                    continue
                seen_hosts.add(host_key)

                conn = vmhost.get("connection", {}) or {}
                sys_info = vmhost.get("system", {}) or {}
                hw = vmhost.get("hardware", {}) or {}
                cpu = hw.get("cpu", {}) or {}
                mem = hw.get("memory", {}) or {}
                storage = hw.get("storage", {}) or {}
                root_fs = storage.get("root_filesystem", {}) or {}
                virt = vmhost.get("virtualization", {}) or {}
                vms_info = vmhost.get("vms", {}) or {}

                # Count documented VM directories as fallback if vmhost.yaml has not been refreshed
                documented_vms = [p.name for p in host_yaml_path.parent.iterdir() if p.is_dir() and (p / "vm.yaml").exists()]
                vm_count = vms_info.get("count")
                if vm_count is None:
                    vm_count = len(documented_vms)

                entry: dict[str, Any] = {
                    "host": vmhost.get("name", host_key),
                    "alias": vmhost.get("alias", sys_info.get("hostname", host_key)),
                    "connection_type": conn.get("type", "local" if host_key in ("localhost", "127.0.0.1") else "ssh"),
                    "user": conn.get("user", ""),
                    "ssh_key_path": conn.get("ssh_key_path", ""),
                    "privilege_mode": conn.get("privilege_mode", "sudo"),
                    "os": (sys_info.get("os") or {}).get("pretty_name", "unknown"),
                    "cpu_model": cpu.get("model", "unknown"),
                    "total_cores": int(cpu.get("total_cores", 0) or 0),
                    "kvm_present": bool(cpu.get("kvm_present", False)),
                    "vt_enabled": bool(cpu.get("vt_enabled", False)),
                    "total_ram_mib": int(mem.get("total_mib", 0) or 0),
                    "total_ram_gb": int(mem.get("total_gb", 0) or 0),
                    "available_ram_mib": int(mem.get("available_mib", 0) or 0),
                    "total_disk_gb": int(root_fs.get("total_gb", 0) or 0),
                    "available_disk_gb": int(root_fs.get("available_gb", 0) or 0),
                    "storage_pools": storage.get("pools", []) or [],
                    "networks": [
                        {
                            "name": n.get("name", ""),
                            "bridge": n.get("bridge", ""),
                            "ip": n.get("ip", ""),
                            "netmask": n.get("netmask", ""),
                        }
                        for n in (virt.get("networks") or [])
                        if isinstance(n, dict)
                    ],
                    "vm_count": int(vm_count),
                    "running_vms": int(vms_info.get("running_count", 0) or 0),
                    "live_probe": False,
                    "load_average_1m": None,
                    "config_path": format_config_path(host_yaml_path),
                }

                if live_probe:
                    live = inspect_hypervisor(host_key)
                    if live.get("success") and live.get("live_probe"):
                        entry["live_probe"] = True
                        entry["available_ram_mib"] = live.get("available_ram_mib", entry["available_ram_mib"])
                        entry["load_average_1m"] = live.get("load_average_1m")

                hypervisors.append(entry)
            except Exception:
                continue

    return {
        "success": True,
        "hypervisors": hypervisors,
        "total_hypervisors": len(hypervisors),
    }


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
    vcpu: int = 2,
    memory_mib: int = 2048,
    disk_gib: int = 20,
) -> dict[str, Any]:
    """Generates a new dedicated execution virtual machine specification and runbook in inventory/{vmhost}/{vm}/.

    Automates:
    - Tech stack and port inference from local repository manifests.
    - Optimal hypervisor host selection if unspecified.
    - Safe vCPU capping against host capacity (defaults to 2 vCPUs, never saturating 100% of host cores).
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
        vcpu: Number of virtual CPUs to allocate (default 2, capped safely by host capacity).
        memory_mib: RAM allocation in MiB (default 2048).
        disk_gib: Virtual disk storage in GiB (default 20).
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

    warnings: list[str] = []

    # Cap vCPU safely below 100% of host capacity when host inventory is available
    _, host_doc = find_vmhost_yaml(vmhost)
    if host_doc:
        host_cores = int(
            (host_doc.get("vmhost", {}).get("hardware", {}).get("cpu", {}) or {}).get("total_cores", 0) or 0
        )
        if host_cores > 0 and vcpu >= host_cores:
            capped_vcpu = max(1, host_cores // 2)
            warnings.append(
                f"Requested vcpu={vcpu} would saturate host '{vmhost}' ({host_cores} cores); capped to {capped_vcpu} vCPU(s)."
            )
            vcpu = capped_vcpu

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

    proxy_socket_service = _proxy_socket_path_for_host(vmhost, vm_name, allocated_port)
    memory_gb = max(1, memory_mib // 1024)

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
                "memory_gb": memory_gb,
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
                "proxy_socket_service": proxy_socket_service,
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
        "--vm", vm_name,
        "--set", f"PROJECT_NAME={app_name}",
        "--set", f"VM_NAME={vm_name}",
        "--set", f"VCPU_CORES={vcpu}",
        "--set", f"RAM_MIB={memory_mib}",
        "--set", f"RAM_GB={memory_gb}",
        "--set", f"DISK_SIZE_GIB={disk_gib}",
        "--set", f"VM_IP={allocated_ip}",
        "--set", f"VM_MAC={allocated_mac}",
        "--set", "VM_PRIMARY_USER=airlock",
        "--set", f"APP_INTERNAL_PORT={internal_port}",
        "--set", f"PROXY_EXTERNAL_PORT={allocated_port}",
        "--set", f"PROXY_SOCKET_SERVICE={proxy_socket_service}",
        "--set", f"REMOTE_CODE_DIR=~/{slug}/",
        "--set", f"SSH_KEY_PATH=~/.ssh/{slug}_vm_ed25519",
        "--output", str(runbook_path),
    ]
    conf_res = _run_command(conf_cmd)

    # Regenerate sync scripts
    sync_cmd = [str(PYTHON_BIN), str(GENERATE_SYNC_SCRIPT)]
    sync_res = _run_command(sync_cmd)

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
def airlock_update_vm(
    vm_name: str,
    vcpu: int = 0,
    memory_mib: int = 0,
    disk_size_gib: int = 0,
    internal_port: int = 0,
    external_port: int = 0,
    post_deploy: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    app_name: str = "",
) -> dict[str, Any]:
    """Updates an existing VM specification in inventory/{vmhost}/{vm}/vm.yaml, re-renders VM.md, and regenerates sync scripts.

    Allows modifying resources (vCPU, RAM, disk size), ports, post-deploy commands, or per-repo rsync
    include/exclude rules before deployment (or before re-provisioning) without hand-editing vm.yaml or VM.md.

    Args:
        vm_name: Target VM domain name in inventory.
        vcpu: New virtual CPU count (>0 to update).
        memory_mib: New memory allocation in MiB (>0 to update; automatically updates memory_gb).
        disk_size_gib: New virtual disk size in GiB (>0 to update).
        internal_port: New internal application port (>0 to update).
        external_port: New hypervisor proxy port (>0 to update; automatically updates proxy_socket_service).
        post_deploy: New post-deploy command for the repository (pass "" to clear; omit/None to leave unchanged).
        include: Optional per-repo rsync include patterns (e.g. ["docs/templates/"]).
        exclude: Optional per-repo rsync exclude patterns (e.g. ["data/raw/"]).
        app_name: Optional repository name when targeting a specific app on a multi-app VM.
    """
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {"success": False, "error": f"VM '{vm_name}' not found in inventory."}

    if vcpu < 0 or memory_mib < 0 or disk_size_gib < 0 or internal_port < 0 or external_port < 0:
        return {"success": False, "error": "Numeric resource and port overrides must be non-negative integers."}
    if internal_port > 65535 or external_port > 65535:
        return {"success": False, "error": "Port numbers must be between 1 and 65535."}

    vm_data = doc.setdefault("vm", {})
    vmhost = vm_data.get("host", yaml_path.parents[1].name)
    resources = vm_data.setdefault("resources", {})
    bp = vm_data.setdefault("blueprint", {})
    repos = bp.setdefault("repositories", [])

    changes: dict[str, dict[str, Any]] = {}

    if vcpu > 0 and resources.get("vcpu") != vcpu:
        changes["vcpu"] = {"old": resources.get("vcpu"), "new": vcpu}
        resources["vcpu"] = vcpu

    if memory_mib > 0 and resources.get("memory_mib") != memory_mib:
        changes["memory_mib"] = {"old": resources.get("memory_mib"), "new": memory_mib}
        resources["memory_mib"] = memory_mib
        resources["memory_gb"] = max(1, memory_mib // 1024)

    if disk_size_gib > 0 and bp.get("disk_size_gib") != disk_size_gib:
        changes["disk_size_gib"] = {"old": bp.get("disk_size_gib"), "new": disk_size_gib}
        bp["disk_size_gib"] = disk_size_gib

    # Resolve target repository entry if updating app-level settings
    target_repo = None
    if repos:
        if app_name:
            target_repo = next((r for r in repos if isinstance(r, dict) and r.get("name") == app_name), None)
            if target_repo is None:
                return {"success": False, "error": f"Repository '{app_name}' not found on VM '{vm_name}'."}
        else:
            target_repo = repos[0] if isinstance(repos[0], dict) else None

    if internal_port > 0:
        if bp.get("app_internal_port") != internal_port:
            changes["app_internal_port"] = {"old": bp.get("app_internal_port"), "new": internal_port}
            bp["app_internal_port"] = internal_port
        if target_repo is not None and target_repo.get("internal_port") != internal_port:
            target_repo["internal_port"] = internal_port

    if external_port > 0:
        new_socket = _proxy_socket_path_for_host(vmhost, vm_name, external_port)
        if bp.get("proxy_external_port") != external_port:
            changes["proxy_external_port"] = {"old": bp.get("proxy_external_port"), "new": external_port}
            bp["proxy_external_port"] = external_port
        if bp.get("proxy_socket_service") != new_socket:
            changes["proxy_socket_service"] = {"old": bp.get("proxy_socket_service"), "new": new_socket}
            bp["proxy_socket_service"] = new_socket
        if target_repo is not None and target_repo.get("external_port") != external_port:
            target_repo["external_port"] = external_port

    if post_deploy is not None:
        if target_repo is None:
            return {"success": False, "error": f"VM '{vm_name}' has no repositories configured to update post_deploy."}
        if target_repo.get("post_deploy") != post_deploy:
            changes["post_deploy"] = {"old": target_repo.get("post_deploy", ""), "new": post_deploy}
            target_repo["post_deploy"] = post_deploy

    if include is not None:
        if target_repo is None:
            return {"success": False, "error": f"VM '{vm_name}' has no repositories configured to update include rules."}
        clean_inc = [str(p).strip() for p in include if str(p).strip()]
        if target_repo.get("include", []) != clean_inc:
            changes["include"] = {"old": target_repo.get("include", []), "new": clean_inc}
            if clean_inc:
                target_repo["include"] = clean_inc
            else:
                target_repo.pop("include", None)

    if exclude is not None:
        if target_repo is None:
            return {"success": False, "error": f"VM '{vm_name}' has no repositories configured to update exclude rules."}
        clean_exc = [str(p).strip() for p in exclude if str(p).strip()]
        if target_repo.get("exclude", []) != clean_exc:
            changes["exclude"] = {"old": target_repo.get("exclude", []), "new": clean_exc}
            if clean_exc:
                target_repo["exclude"] = clean_exc
            else:
                target_repo.pop("exclude", None)

    if not changes:
        return {
            "success": True,
            "vm_name": vm_name,
            "host": vmhost,
            "changed": False,
            "changes": {},
            "config_path": format_config_path(yaml_path),
            "message": f"No changes applied to VM '{vm_name}'.",
        }

    # Persist updated vm.yaml SSOT
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f, sort_keys=False)

    # Re-render VM.md runbook from updated SSOT
    runbook_path = yaml_path.parent / "VM.md"
    final_vcpu = int(resources.get("vcpu", 4))
    final_ram_mib = int(resources.get("memory_mib", 2048))
    final_ram_gb = max(1, final_ram_mib // 1024)
    final_disk_gib = int(bp.get("disk_size_gib", 20))
    final_int_port = int(bp.get("app_internal_port", 3000))
    final_ext_port = int(bp.get("proxy_external_port", 3001))
    final_proxy_svc = str(bp.get("proxy_socket_service", f"/etc/systemd/system/{vm_name}-p{final_ext_port}-proxy.socket"))
    ifaces = vm_data.get("network", {}).get("interfaces", [])
    primary_ip = ifaces[0].get("ip", "") if ifaces else ""
    primary_mac = ifaces[0].get("mac", "") if ifaces else ""

    conf_cmd = [
        str(PYTHON_BIN),
        str(CONFIGURE_SCRIPT),
        "--from-inventory", vmhost,
        "--vm", vm_name,
        "--set", f"VM_NAME={vm_name}",
        "--set", f"VCPU_CORES={final_vcpu}",
        "--set", f"RAM_MIB={final_ram_mib}",
        "--set", f"RAM_GB={final_ram_gb}",
        "--set", f"DISK_SIZE_GIB={final_disk_gib}",
        "--set", f"APP_INTERNAL_PORT={final_int_port}",
        "--set", f"PROXY_EXTERNAL_PORT={final_ext_port}",
        "--set", f"PROXY_SOCKET_SERVICE={final_proxy_svc}",
        "--output", str(runbook_path),
    ]
    if primary_ip:
        conf_cmd.extend(["--set", f"VM_IP={primary_ip}"])
    if primary_mac:
        conf_cmd.extend(["--set", f"VM_MAC={primary_mac}"])
    conf_res = _run_command(conf_cmd)

    # Regenerate sync scripts
    sync_res = _run_command([str(PYTHON_BIN), str(GENERATE_SYNC_SCRIPT)])

    warnings: list[str] = []
    if not conf_res.get("success"):
        warnings.append(f"Runbook regeneration failed: {conf_res.get('stderr') or conf_res.get('stdout')}")
    if not sync_res.get("success"):
        warnings.append(f"Sync script regeneration failed: {sync_res.get('stderr') or sync_res.get('stdout')}")
    if str(vm_data.get("state", "shut off")).lower() == "running" and any(
        k in changes for k in ("vcpu", "memory_mib", "disk_size_gib")
    ):
        warnings.append(
            "VM is currently running; hardware resource changes apply when the VM is re-provisioned or reconciled."
        )

    return {
        "success": True,
        "vm_name": vm_name,
        "host": vmhost,
        "changed": True,
        "changes": changes,
        "config_path": format_config_path(yaml_path),
        "runbook_path": format_config_path(runbook_path),
        "runbook_generated": bool(conf_res.get("success")),
        "sync_generated": bool(sync_res.get("success")),
        "warnings": warnings,
        "message": f"Updated VM '{vm_name}' ({', '.join(changes.keys())}) and refreshed VM.md and sync scripts.",
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
    delete: bool = False,
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
        delete: When True, passes --delete to rsync to remove remote files that no longer exist locally.
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
    if delete:
        cmd.append("--delete")
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
def airlock_purge_vm(
    vm_name: str,
    dry_run: bool = False,
    purge_ssh_keys: bool = False,
) -> dict[str, Any]:
    """Permanently removes a VM domain, NVRAM, disk images, snapshots, DHCP reservation, proxy sockets, SSH config block, and inventory record.

    Args:
        vm_name: Target VM domain name in inventory.
        dry_run: When True, previews all purge steps without modifying the hypervisor or workstation.
        purge_ssh_keys: When True, also deletes the VM's dedicated workstation SSH keypair (~/.ssh/<app>_vm_ed25519*).
    """
    try:
        safe_vm = validate_identifier(vm_name, "VM name")
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    yaml_path, _ = _find_vm_yaml(safe_vm)
    if not yaml_path:
        return {"success": False, "error": f"VM '{safe_vm}' not found in inventory."}

    cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", safe_vm, "--purge"]
    if dry_run:
        cmd.append("--dry-run")
    if purge_ssh_keys:
        cmd.append("--purge-ssh-keys")

    res = _run_command(cmd, timeout=120)
    res["vm_name"] = safe_vm
    res["dry_run"] = dry_run
    res["purge_ssh_keys"] = purge_ssh_keys
    return res


@server.tool()
def airlock_snapshot(
    vm_name: str,
    action: str,
    snapshot_name: str = "",
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Manages disk-only snapshots for instant clean-slate rollbacks, safe pruning, and backing-chain flattening.

    Args:
        vm_name: Target VM domain name.
        action: Snapshot action ('create', 'revert', 'list', 'delete', 'flatten').
        snapshot_name: Name of the snapshot (required for 'delete'; optional fresh baseline name for 'flatten'; optional for 'create'/'revert').
        force: Required for 'flatten' (when not dry_run) and for deleting the protected baseline snapshot.
        dry_run: When True, previews the snapshot operation without modifying the hypervisor.
    """
    try:
        safe_vm = validate_identifier(vm_name, "VM name")
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if action == "create":
        cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", safe_vm, "--snapshot"]
        if snapshot_name:
            try:
                cmd.append(validate_identifier(snapshot_name, "snapshot name"))
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
        if dry_run:
            cmd.append("--dry-run")
    elif action == "revert":
        cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", safe_vm, "--rollback"]
        if snapshot_name:
            try:
                cmd.append(validate_identifier(snapshot_name, "snapshot name"))
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
        if dry_run:
            cmd.append("--dry-run")
    elif action == "list":
        cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", safe_vm, "--status"]
    elif action == "delete":
        if not snapshot_name or not snapshot_name.strip():
            return {
                "success": False,
                "vm_name": safe_vm,
                "action": action,
                "error": "Explicit non-empty 'snapshot_name' is required for action='delete'.",
            }
        try:
            safe_snap = validate_identifier(snapshot_name, "snapshot name")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", safe_vm, "--delete-snapshot", safe_snap]
        if force:
            cmd.append("--force")
        if dry_run:
            cmd.append("--dry-run")
    elif action == "flatten":
        cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", safe_vm, "--flatten-snapshots"]
        if snapshot_name and snapshot_name.strip():
            try:
                cmd.append(validate_identifier(snapshot_name, "snapshot name"))
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
        if force:
            cmd.append("--force")
        if dry_run:
            cmd.append("--dry-run")
    else:
        return {
            "success": False,
            "error": f"Invalid action '{action}'. Must be 'create', 'revert', 'list', 'delete', or 'flatten'.",
        }

    res = _run_command(cmd, timeout=300 if action == "flatten" else (180 if action == "delete" else 60))
    res["vm_name"] = safe_vm
    res["action"] = action

    # Parse structured snapshot metadata if present
    if action in ("delete", "flatten") and res.get("stdout"):
        import json as _json
        marker = "AIRLOCK_SNAPSHOT_DELETE_JSON:" if action == "delete" else "AIRLOCK_SNAPSHOT_FLATTEN_JSON:"
        for line in res["stdout"].splitlines():
            if line.startswith(marker):
                try:
                    meta = _json.loads(line[len(marker):])
                    res.update(meta)
                except Exception:
                    pass
                break

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

    res = _run_command(cmd, timeout=1800)
    res["host"] = vmhost
    res["dry_run"] = dry_run
    return res


def main() -> None:
    """Runs the AirlockVM MCP Server over stdio."""
    server.run()


if __name__ == "__main__":
    main()
