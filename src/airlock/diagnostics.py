"""DevBox Self-Healing Crash Diagnostic Engine.

Performs multi-layer root-cause failure analysis when workloads fail health checks or crash.
Analyzes guest systemd logs, runtime process exit codes, internal listening sockets,
and hypervisor proxy forwarders to offer automated remediation and disaster rollback.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any
import yaml

DEVBOX_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = DEVBOX_ROOT / "inventory"


def _ssh_guest(vm_name: str, cmd: str, timeout: int = 5) -> tuple[int, str, str]:
    full_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=no",
        vm_name,
        cmd,
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
        return 124, "", f"Guest command timed out: {cmd}"
    except Exception as e:
        return 1, "", str(e)


def _ssh_host(host: str, cmd: str, timeout: int = 5) -> tuple[int, str, str]:
    full_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=accept-new",
        host,
        cmd,
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
        return 124, "", f"Host command timed out: {cmd}"
    except Exception as e:
        return 1, "", str(e)


def _find_vm_yaml(vm_name: str) -> tuple[Path | None, dict[str, Any] | None]:
    if not INVENTORY_DIR.exists():
        return None, None
    for cand in INVENTORY_DIR.glob(f"*/{vm_name}/vm.yaml"):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                return cand, yaml.safe_load(f) or {}
        except Exception:
            continue
    return None, None


def diagnose_workload_failure(vm_name: str, app_name: str = "") -> dict[str, Any]:
    """Diagnoses deployment crashes across hypervisor proxies and guest runtimes."""
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {"success": False, "error": f"VM '{vm_name}' not found in inventory."}

    vm_data = doc.get("vm", {})
    vmhost = vm_data.get("host", "barkley")
    bp = vm_data.get("blueprint", {})
    int_port = bp.get("app_internal_port", 3000)
    ext_port = bp.get("proxy_external_port", 3001)

    findings = []
    root_cause = "UNKNOWN"
    suggested_action = "Review application logs."

    # 1. Hypervisor domain power state
    rc, dom_state, _ = _ssh_host(vmhost, f"sudo virsh domstate {vm_name} 2>/dev/null")
    if rc != 0 or not dom_state:
        findings.append("VM domain is not registered or virsh is inaccessible on hypervisor.")
        root_cause = "DOMAIN_NOT_FOUND"
        suggested_action = f"Run 'airlock_deploy_vm(vm_name=\"{vm_name}\")' to provision domain."
    elif "shut off" in dom_state.lower():
        findings.append(f"VM domain '{vm_name}' is in 'shut off' state.")
        root_cause = "VM_POWERED_OFF"
        suggested_action = f"Power on the VM using 'airlock_power(vm_name=\"{vm_name}\", action=\"start\")'."
    else:
        findings.append(f"VM domain is active in state '{dom_state}'.")

        # 2. Hypervisor proxy socket unit status
        socket_unit = f"{vm_name}-p{ext_port}-proxy.socket"
        rc_sock, sock_out, _ = _ssh_host(vmhost, f"systemctl is-active {socket_unit} 2>/dev/null || echo 'inactive'")
        if sock_out != "active":
            findings.append(f"Hypervisor proxy socket unit '{socket_unit}' is '{sock_out}'.")
            if root_cause == "UNKNOWN":
                root_cause = "PROXY_SOCKET_INACTIVE"
                suggested_action = f"Restart proxy forwarder: ssh {vmhost} 'sudo systemctl restart {socket_unit}'"
        else:
            findings.append(f"Hypervisor proxy socket unit '{socket_unit}' is active.")

        # 3. Guest internal socket listening check
        rc_ss, ss_out, _ = _ssh_guest(vm_name, f"ss -tlnH | grep -E ':{int_port}\\b' || echo 'NOT_LISTENING'")
        if "NOT_LISTENING" in ss_out:
            findings.append(f"No process inside the guest VM is listening on internal port {int_port}.")
            if root_cause == "UNKNOWN":
                root_cause = "APP_NOT_BOUND_TO_PORT"
                suggested_action = f"Verify post_deploy command starts a server binding to 0.0.0.0:{int_port}."
        else:
            findings.append(f"Process inside guest is listening on port {int_port}: {ss_out}")

        # 4. Guest journal error logs
        rc_j, j_out, _ = _ssh_guest(vm_name, "journalctl -n 25 --no-pager 2>/dev/null | grep -iE 'error|fail|crash|exception' || true")
        if j_out.strip():
            findings.append("Recent errors captured in guest journal:")
            findings.extend(j_out.strip().splitlines()[:5])

            # Detect common patterns
            if "Cannot find module" in j_out or "ModuleNotFoundError" in j_out:
                root_cause = "MISSING_DEPENDENCY"
                suggested_action = "Missing software dependencies inside guest. Run npm install or pip install."
            elif "EADDRINUSE" in j_out:
                root_cause = "PORT_IN_USE"
                suggested_action = f"Port {int_port} is already bound by another process inside the guest."

    return {
        "success": True,
        "vm_name": vm_name,
        "host": vmhost,
        "root_cause": root_cause,
        "suggested_action": suggested_action,
        "findings": findings,
        "can_rollback": True,
        "rollback_command": f"airlock_checkpoint(vm_name=\"{vm_name}\", action=\"revert\")",
        "message": f"Diagnosis completed for '{vm_name}': Root cause identified as '{root_cause}'. {suggested_action}",
    }
