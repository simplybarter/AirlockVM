"""AirlockVM Configuration Drift Detection & Reconciliation Engine.

Compares live hypervisor domain and systemd proxy socket states against
the Single Source of Truth (inventory/{vmhost}/{vm}/vm.yaml) to detect drift,
and offers automated reconciliation.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any
import xml.etree.ElementTree as ET
import yaml

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = AIRLOCK_ROOT / "inventory"
ADD_HOST_SCRIPT = AIRLOCK_ROOT / ".agents" / "skills" / "airlock_inventory" / "scripts" / "add_vmhost.py"
DEPLOY_SCRIPT = AIRLOCK_ROOT / ".agents" / "skills" / "airlock_deploy" / "scripts" / "deploy_vm.py"


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
    if not INVENTORY_DIR.exists():
        return None, None
    for cand in INVENTORY_DIR.glob(f"*/{vm_name}/vm.yaml"):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                return cand, yaml.safe_load(f) or {}
        except Exception:
            continue
    return None, None


def audit_vm_drift(vm_name: str) -> dict[str, Any]:
    """Audits live hypervisor domain and proxy state against inventory SSOT."""
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {"success": False, "error": f"VM '{vm_name}' not found in inventory."}

    vm_data = doc.get("vm", {})
    vmhost = vm_data.get("host", "barkley")
    expected_state = vm_data.get("state", "shut off")
    res = vm_data.get("resources", {})
    expected_vcpu = int(res.get("vcpu", 4))
    expected_ram = int(res.get("memory_mib", 2048))
    bp = vm_data.get("blueprint", {})
    expected_proxy_port = bp.get("proxy_external_port")

    # 1. Query live libvirt domain
    rc, xml_str, _ = _ssh_cmd(vmhost, f"sudo virsh dumpxml {vm_name} 2>/dev/null")
    domain_defined = rc == 0 and bool(xml_str.strip())

    drift_items: list[dict[str, Any]] = []

    if not domain_defined:
        drift_items.append({
            "category": "domain_presence",
            "attribute": "libvirt_domain",
            "inventory_value": "defined in inventory",
            "live_value": "not yet provisioned on hypervisor",
            "severity": "info" if expected_state == "shut off" else "warning",
            "message": f"VM '{vm_name}' is documented in inventory but not currently defined on hypervisor '{vmhost}'.",
        })
    else:
        # Parse XML
        try:
            root = ET.fromstring(xml_str)
            live_vcpu = int(root.findtext("vcpu") or "0")
            live_mem_kib = int(root.findtext("memory") or "0")
            live_mem_mib = live_mem_kib // 1024

            if live_vcpu != expected_vcpu:
                drift_items.append({
                    "category": "resources",
                    "attribute": "vcpu",
                    "inventory_value": expected_vcpu,
                    "live_value": live_vcpu,
                    "severity": "warning",
                    "message": f"vCPU allocation drift: inventory specifies {expected_vcpu}, but live domain has {live_vcpu}.",
                })

            if abs(live_mem_mib - expected_ram) > 64:
                drift_items.append({
                    "category": "resources",
                    "attribute": "memory_mib",
                    "inventory_value": expected_ram,
                    "live_value": live_mem_mib,
                    "severity": "warning",
                    "message": f"Memory allocation drift: inventory specifies {expected_ram} MiB, but live domain has {live_mem_mib} MiB.",
                })

            # Check live execution state
            rc_st, live_state, _ = _ssh_cmd(vmhost, f"sudo virsh domstate {vm_name} 2>/dev/null")
            if rc_st == 0 and live_state:
                norm_live = live_state.lower().strip()
                norm_exp = expected_state.lower().strip()
                if norm_live != norm_exp:
                    drift_items.append({
                        "category": "state",
                        "attribute": "execution_state",
                        "inventory_value": expected_state,
                        "live_value": live_state,
                        "severity": "info",
                        "message": f"State mismatch: inventory recorded '{expected_state}', live state is '{live_state}'.",
                    })
        except Exception as e:
            drift_items.append({
                "category": "parse_error",
                "attribute": "xml",
                "inventory_value": "valid",
                "live_value": str(e),
                "severity": "warning",
                "message": f"Error parsing domain XML: {e}",
            })

    # 2. Check hypervisor systemd proxy socket state
    if expected_proxy_port:
        slug = vm_name.replace("-vm", "")
        socket_unit = f"{vm_name}-p{expected_proxy_port}-proxy.socket"
        rc_sock, sock_status, _ = _ssh_cmd(vmhost, f"systemctl is-active {socket_unit} 2>/dev/null || echo 'inactive'")
        if sock_status != "active" and expected_state == "running":
            drift_items.append({
                "category": "proxy",
                "attribute": "socket_forwarder",
                "inventory_value": f"active on port {expected_proxy_port}",
                "live_value": sock_status,
                "severity": "warning",
                "message": f"Hypervisor proxy socket '{socket_unit}' is {sock_status} while VM is running.",
            })

    has_drift = any(d["severity"] in ("warning", "error") for d in drift_items)
    return {
        "success": True,
        "vm_name": vm_name,
        "host": vmhost,
        "has_drift": has_drift,
        "total_drift_items": len(drift_items),
        "drift_items": drift_items,
        "domain_provisioned": domain_defined,
        "message": (
            f"Configuration drift detected on '{vm_name}' ({len(drift_items)} discrepancy items)."
            if has_drift
            else f"No configuration drift detected for '{vm_name}' on '{vmhost}'. Specifications are synchronized."
        ),
    }


def reconcile_vm_drift(vm_name: str, strategy: str = "refresh_inventory") -> dict[str, Any]:
    """Reconciles detected drift between inventory SSOT and live hypervisor.

    Strategies:
    - 'refresh_inventory': Re-discovers live hypervisor state and updates inventory/{vmhost}/{vm}/vm.yaml.
    - 'enforce_specification': Re-runs deploy_vm to push inventory specifications to the hypervisor.
    """
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {"success": False, "error": f"VM '{vm_name}' not found."}

    vmhost = doc.get("vm", {}).get("host", "barkley")

    if strategy == "refresh_inventory":
        cmd = ["python3", str(ADD_HOST_SCRIPT), vmhost, "--vm", vm_name]
        proc = subprocess.run(cmd, cwd=AIRLOCK_ROOT, capture_output=True, text=True)
        return {
            "success": proc.returncode == 0,
            "strategy": strategy,
            "vm_name": vm_name,
            "host": vmhost,
            "stdout": proc.stdout.strip(),
            "message": f"Refreshed inventory record for '{vm_name}' from live hypervisor.",
        }
    elif strategy == "enforce_specification":
        cmd = ["python3", str(DEPLOY_SCRIPT), "--vm", vm_name, "--yes"]
        proc = subprocess.run(cmd, cwd=AIRLOCK_ROOT, capture_output=True, text=True)
        return {
            "success": proc.returncode == 0,
            "strategy": strategy,
            "vm_name": vm_name,
            "host": vmhost,
            "stdout": proc.stdout.strip(),
            "message": f"Applied inventory specification for '{vm_name}' to hypervisor.",
        }
    else:
        return {"success": False, "error": f"Invalid strategy '{strategy}'. Use 'refresh_inventory' or 'enforce_specification'."}
