"""AirlockVM Multi-Hypervisor Host Placement Optimizer.

Audits hypervisor resources (CPU threads, RAM headroom, storage, running VMs)
to automatically recommend or select the optimal hypervisor host for new VMs or workloads.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any
import yaml

from airlock.inventory import find_vmhost_yaml, get_inventory_search_dirs, get_primary_inventory_dir

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = get_primary_inventory_dir()


def inspect_hypervisor(vmhost: str) -> dict[str, Any]:
    """Inspects live or documented capacity of a hypervisor host."""
    host_yaml, doc = find_vmhost_yaml(vmhost)
    if not host_yaml or not doc:
        return {"success": False, "error": f"Host '{vmhost}' not found in inventory."}

    data = doc.get("vmhost", {})
    hw = data.get("hardware", {})
    sys_info = data.get("system", {})
    vms_info = data.get("vms", {})

    total_cores = hw.get("cpu", {}).get("total_cores", 4)
    total_ram_mib = hw.get("memory", {}).get("total_mib", 8192)
    avail_ram_mib = hw.get("memory", {}).get("available_mib", total_ram_mib // 2)
    avail_disk_gb = hw.get("storage", {}).get("root_filesystem", {}).get("available_gb", 50)
    running_vms = vms_info.get("running_count", 0)

    # Attempt live probe if connection is configured
    live_probe = False
    load_avg = 0.5
    conn = data.get("connection", {})
    conn_type = conn.get("type", "ssh")
    remote_host = conn.get("host", vmhost)

    if conn_type == "ssh":
        try:
            cmd = [
                "ssh",
                "-o", "ConnectTimeout=3",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                remote_host,
                "uptime && free -m",
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=4)
            if proc.returncode == 0:
                live_probe = True
                lines = proc.stdout.splitlines()
                # Parse load average from uptime line
                if lines:
                    uptime_line = lines[0]
                    if "load average:" in uptime_line:
                        parts = uptime_line.split("load average:")[-1].split(",")
                        if parts:
                            load_avg = float(parts[0].strip())
                # Parse free RAM
                for line in lines[1:]:
                    if line.startswith("Mem:"):
                        mparts = line.split()
                        if len(mparts) >= 7:
                            avail_ram_mib = int(mparts[6])  # available column
        except Exception:
            pass

    return {
        "success": True,
        "vmhost": vmhost,
        "live_probe": live_probe,
        "total_cores": total_cores,
        "total_ram_mib": total_ram_mib,
        "available_ram_mib": avail_ram_mib,
        "available_disk_gb": avail_disk_gb,
        "running_vms": running_vms,
        "load_average_1m": load_avg,
    }


def select_optimal_hypervisor(
    required_vcpu: int = 2,
    required_ram_mib: int = 2048,
    required_disk_gb: int = 20,
) -> dict[str, Any]:
    """Evaluates all registered hypervisors and selects the best host for placement."""
    candidates = []
    seen_hosts: set[str] = set()

    for inv_dir in get_inventory_search_dirs():
        if not inv_dir.exists():
            continue
        for host_dir in inv_dir.iterdir():
            if host_dir.is_dir() and (host_dir / "vmhost.yaml").exists() and host_dir.name not in seen_hosts:
                seen_hosts.add(host_dir.name)
                info = inspect_hypervisor(host_dir.name)
                if info.get("success"):
                    candidates.append(info)

    if not candidates:
        return {"success": False, "error": "No hypervisors discovered in inventory."}

    # Score candidates
    scored = []
    for c in candidates:
        ram = c["available_ram_mib"]
        disk = c["available_disk_gb"]
        load = c["load_average_1m"]
        running = c["running_vms"]

        # Disqualify if insufficient resources
        if ram < required_ram_mib:
            continue
        if disk < required_disk_gb:
            continue

        # Score formula: higher is better
        # Emphasizes available RAM headroom while penalizing high CPU load
        score = ram - (running * 512) - int(load * 1000)
        scored.append((score, c))

    if not scored:
        # Fallback to candidate with maximum available RAM
        best = max(candidates, key=lambda x: x["available_ram_mib"])
        return {
            "success": True,
            "selected_host": best["vmhost"],
            "selected_hypervisor": best["vmhost"],
            "warning": "None of the hypervisors strictly met all requested headroom limits; selected host with highest remaining RAM.",
            "metrics": best,
            "all_hosts": candidates,
        }

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_host = scored[0]

    return {
        "success": True,
        "selected_host": best_host["vmhost"],
        "selected_hypervisor": best_host["vmhost"],
        "placement_score": best_score,
        "metrics": best_host,
        "all_hosts": candidates,
        "message": f"Selected hypervisor '{best_host['vmhost']}' with {best_host['available_ram_mib']} MiB available RAM and load average {best_host['load_average_1m']}.",
    }
