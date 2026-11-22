"""AirlockVM Pre-Flight Deployability Confirmation Engine.

Validates that a defined virtual machine specification in inventory/{vmhost}/{vm}/vm.yaml
can be deployed successfully before provisioning commands are executed.
Audits live hypervisor connectivity, hardware VT acceleration, IP collisions,
listening socket conflicts, storage pool headroom, and host RAM pressure.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any
import yaml

from airlock.inventory import find_vm_yaml, get_primary_inventory_dir
from airlock.ports import get_hypervisor_live_ports

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = get_primary_inventory_dir()


def _ssh_cmd(host: str, command: str, timeout: int = 5) -> tuple[int, str, str]:
    """Executes a command locally or on a remote hypervisor via SSH."""
    if not host or host in ("localhost", "127.0.0.1"):
        full_cmd = ["bash", "-c", command]
    else:
        full_cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            host,
            command,
        ]

    try:
        proc = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s: {command}"
    except Exception as e:
        return 1, "", str(e)


def _find_vm_yaml(vm_name: str) -> tuple[Path | None, dict[str, Any] | None]:
    """Locates vm.yaml for a given VM domain name across inventory."""
    return find_vm_yaml(vm_name)


def validate_deployment_preflight(vm_name: str) -> dict[str, Any]:
    """Audits live hypervisor state to confirm that the VM configuration is ready to deploy.

    Checks:
    1. SSH & Sudo connectivity to target hypervisor
    2. KVM & Hardware Virtualization (/dev/kvm present & writable)
    3. Libvirt daemon responsiveness (virsh connect)
    4. Static IP silence (ARP/ping test to ensure IP is not occupied by rogue client)
    5. Hypervisor proxy port availability (ensures external port is not bound)
    6. Storage pool headroom (sufficient free GiB on backing filesystem)
    7. Host RAM safety headroom (prevents OOM kill)
    8. Base cloud image cache status
    """
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {
            "deployable": False,
            "confidence_score": 0.0,
            "verdict": "BLOCKED",
            "error": f"Target VM '{vm_name}' not found in inventory.",
            "checks": {},
            "remediations": [f"Create or discover VM '{vm_name}' in inventory before running preflight."],
        }

    vm_data = doc.get("vm", {})
    vmhost = vm_data.get("host", "remotehost")
    bp = vm_data.get("blueprint", {})
    res = vm_data.get("resources", {})
    net = vm_data.get("network", {})
    ifaces = net.get("interfaces", [])
    target_ip = ifaces[0].get("ip", "") if ifaces else ""
    target_mac = ifaces[0].get("mac", "") if ifaces else ""
    requested_ram_mib = int(res.get("memory_mib", 2048))
    requested_disk_gib = int(bp.get("disk_size_gib", 50))
    ext_port = int(bp.get("proxy_external_port", 3001))

    checks: dict[str, dict[str, Any]] = {}
    remediations: list[str] = []
    blockers = 0
    warnings = 0

    # 1. Hypervisor Connectivity & Sudo
    rc, out, err = _ssh_cmd(vmhost, "sudo -n true 2>/dev/null && echo 'SUDO_OK' || echo 'SUDO_FAIL'")
    if rc == 0 and "SUDO_OK" in out:
        checks["hypervisor_access"] = {"status": "PASS", "message": f"SSH key access and passwordless sudo verified on {vmhost}"}
    else:
        checks["hypervisor_access"] = {"status": "FAIL", "message": f"Sudo or SSH failed on {vmhost}: {err or out}"}
        remediations.append(f"Configure passwordless sudo for user on hypervisor '{vmhost}'.")
        blockers += 1

    # 2. Hardware Virtualization (/dev/kvm)
    rc, out, err = _ssh_cmd(vmhost, "[ -w /dev/kvm ] && echo 'KVM_OK' || echo 'KVM_FAIL'")
    if rc == 0 and "KVM_OK" in out:
        checks["hardware_virtualization"] = {"status": "PASS", "message": "/dev/kvm is accessible and writable"}
    else:
        checks["hardware_virtualization"] = {"status": "FAIL", "message": "/dev/kvm missing or not writable"}
        remediations.append(f"Enable hardware VT-x/AMD-V in BIOS on '{vmhost}' and ensure /dev/kvm has permissions.")
        blockers += 1

    # 3. Libvirt Daemon Active
    rc, out, err = _ssh_cmd(vmhost, "virsh uri 2>/dev/null")
    if rc == 0 and "qemu" in out:
        checks["libvirt_daemon"] = {"status": "PASS", "message": f"Libvirt daemon active ({out})"}
    else:
        checks["libvirt_daemon"] = {"status": "FAIL", "message": "Libvirt daemon not responding to virsh"}
        remediations.append(f"Start libvirtd service on hypervisor '{vmhost}': sudo systemctl start libvirtd")
        blockers += 1

    # 4. Static IP Conflict Probe (Ensures target IP is truly silent)
    if target_ip:
        # Ping probe from hypervisor on virbr0
        rc, out, err = _ssh_cmd(vmhost, f"ping -c 1 -W 1 {target_ip} >/dev/null 2>&1 && echo 'COLLISION' || echo 'SILENT'")
        if "COLLISION" in out:
            checks["ip_silence"] = {
                "status": "FAIL",
                "message": f"Static IP {target_ip} is already responding to ICMP ping on the network!",
            }
            remediations.append(f"IP {target_ip} is currently occupied. Re-allocate IP using airlock_alloc_network.")
            blockers += 1
        else:
            checks["ip_silence"] = {"status": "PASS", "message": f"Static IP {target_ip} is silent and conflict-free"}

    # 5. External Port Collision Check
    live_ports = get_hypervisor_live_ports(vmhost)
    if ext_port in live_ports:
        checks["proxy_port_free"] = {
            "status": "FAIL",
            "message": f"External proxy port {ext_port} is already actively listening on {vmhost}!",
        }
        remediations.append(f"Port {ext_port} is busy on {vmhost}. Allocate a free port via airlock_find_port.")
        blockers += 1
    else:
        checks["proxy_port_free"] = {"status": "PASS", "message": f"External proxy port {ext_port} is free on {vmhost}"}

    # 6. Storage Pool Free Headroom
    rc, out, err = _ssh_cmd(vmhost, "df -BG /var/lib/nhr/vm 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G'")
    free_gib = int(out) if rc == 0 and out.isdigit() else 0
    if free_gib >= requested_disk_gib:
        checks["storage_headroom"] = {
            "status": "PASS",
            "message": f"Storage pool has {free_gib} GiB free headroom (requested {requested_disk_gib} GiB)",
            "free_gib": free_gib,
        }
    elif free_gib >= 10:
        checks["storage_headroom"] = {
            "status": "WARN",
            "message": f"Storage pool has only {free_gib} GiB free (sparse qcow2 can start, but expansion is limited)",
            "free_gib": free_gib,
        }
        warnings += 1
    else:
        checks["storage_headroom"] = {
            "status": "FAIL",
            "message": f"Insufficient disk space ({free_gib} GiB available < 10 GiB required minimum)",
            "free_gib": free_gib,
        }
        remediations.append(f"Free up backing storage on '{vmhost}' before provisioning {requested_disk_gib} GiB disk.")
        blockers += 1

    # 7. Host RAM Safety Headroom
    rc, out, err = _ssh_cmd(vmhost, "awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo")
    avail_ram_mib = int(out) if rc == 0 and out.isdigit() else 0
    safe_threshold = requested_ram_mib + 1024  # Leave at least 1 GB for host OS
    if avail_ram_mib >= safe_threshold:
        checks["host_ram_headroom"] = {
            "status": "PASS",
            "message": f"Host has {avail_ram_mib} MiB available RAM ({avail_ram_mib - requested_ram_mib} MiB margin after allocation)",
            "available_ram_mib": avail_ram_mib,
        }
    else:
        checks["host_ram_headroom"] = {
            "status": "WARN",
            "message": f"Low RAM headroom: {avail_ram_mib} MiB available on host, allocating {requested_ram_mib} MiB may cause swap pressure.",
            "available_ram_mib": avail_ram_mib,
        }
        warnings += 1

    # 8. Base Cloud Image Cached
    rc, out, err = _ssh_cmd(vmhost, "[ -f /var/lib/nhr/vm/ubuntu-24.04-minimal-cloudimg.img ] && echo 'CACHED' || echo 'MISSING'")
    if rc == 0 and "CACHED" in out:
        checks["base_image_cached"] = {"status": "PASS", "message": "Ubuntu 24.04 minimal cloud image is cached on hypervisor"}
    else:
        checks["base_image_cached"] = {
            "status": "WARN",
            "message": "Base cloud image not cached locally; deployment will automatically download it on first launch.",
        }
        warnings += 1

    # Determine Verdict & Score
    total_checks = len(checks)
    passed_checks = sum(1 for c in checks.values() if c["status"] == "PASS")
    confidence_score = round(passed_checks / total_checks, 2) if total_checks > 0 else 0.0

    if blockers > 0:
        verdict = "BLOCKED"
        deployable = False
    elif warnings > 0:
        verdict = "DEPLOYABLE_WITH_WARNINGS"
        deployable = True
    else:
        verdict = "READY_TO_DEPLOY"
        deployable = True

    return {
        "success": True,
        "vm_name": vm_name,
        "host": vmhost,
        "deployable": deployable,
        "verdict": verdict,
        "confidence_score": confidence_score,
        "checks_passed": f"{passed_checks}/{total_checks}",
        "checks": checks,
        "remediations": remediations,
        "message": (
            f"Pre-flight verification passed: VM '{vm_name}' is {verdict} on '{vmhost}' (Confidence: {int(confidence_score * 100)}%)."
            if deployable
            else f"Pre-flight verification BLOCKED: {len(remediations)} issue(s) require attention before deployment."
        ),
    }
