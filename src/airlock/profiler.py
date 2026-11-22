"""AirlockVM Workload Profiling & Rightsizing Optimization Engine.

Connects to guest virtual machines to measure real-time CPU, RAM, swap, and process load,
and computes actionable rightsizing recommendations to optimize hypervisor resource utilization.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any
import yaml

from airlock.inventory import find_vm_yaml, get_primary_inventory_dir
from airlock.transport import run_guest_cmd

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = get_primary_inventory_dir()


def _ssh_guest(vm_name: str, cmd: str, timeout: int = 5) -> tuple[int, str, str]:
    return run_guest_cmd(vm_name, cmd, timeout=timeout)


def _find_vm_yaml(vm_name: str) -> tuple[Path | None, dict[str, Any] | None]:
    return find_vm_yaml(vm_name)


def profile_workload(vm_name: str, duration_sec: int = 2) -> dict[str, Any]:
    """Profiles real-time workload consumption inside a running VM and generates rightsizing guidance."""
    yaml_path, doc = _find_vm_yaml(vm_name)
    allocated_ram = 2048
    allocated_vcpu = 4
    if doc:
        res = doc.get("vm", {}).get("resources", {})
        allocated_ram = int(res.get("memory_mib", 2048))
        allocated_vcpu = int(res.get("vcpu", 4))

    # Probe guest memory and load
    probe_script = (
        "cat /proc/meminfo | grep -E '^(MemTotal|MemFree|MemAvailable|SwapTotal|SwapFree):' ; "
        "echo '---' ; "
        "cat /proc/loadavg ; "
        "echo '---' ; "
        "ps -eo pmem,pcpu,args --sort=-pmem | head -n 6"
    )
    rc, out, err = _ssh_guest(vm_name, probe_script, timeout=duration_sec + 3)

    if rc != 0:
        return {
            "success": False,
            "vm_name": vm_name,
            "status": "UNREACHABLE",
            "error": f"Cannot connect to guest VM '{vm_name}' over SSH ({err or 'Connection timed out'}). Ensure domain is running.",
        }

    sections = out.split("---")
    mem_lines = sections[0].strip().splitlines() if len(sections) > 0 else []
    load_line = sections[1].strip() if len(sections) > 1 else "0.0 0.0 0.0"
    proc_lines = sections[2].strip().splitlines() if len(sections) > 2 else []

    # Parse memory
    mem_stats: dict[str, int] = {}
    for line in mem_lines:
        parts = line.split(":")
        if len(parts) == 2:
            val = re_val = "".join(filter(str.isdigit, parts[1]))
            mem_stats[parts[0].strip()] = int(val) // 1024 if val else 0  # converted to MiB

    mem_total = mem_stats.get("MemTotal", allocated_ram)
    mem_avail = mem_stats.get("MemAvailable", mem_stats.get("MemFree", mem_total))
    mem_used = mem_total - mem_avail
    ram_pct = round((mem_used / mem_total) * 100, 1) if mem_total > 0 else 0.0

    swap_total = mem_stats.get("SwapTotal", 0)
    swap_free = mem_stats.get("SwapFree", 0)
    swap_used = swap_total - swap_free

    # Parse load
    loads = load_line.split()
    load_1m = float(loads[0]) if len(loads) > 0 else 0.0
    load_5m = float(loads[1]) if len(loads) > 1 else 0.0

    # Parse top processes
    top_processes = []
    for line in proc_lines[1:]:  # skip header
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 3:
            top_processes.append({"pmem": parts[0] + "%", "pcpu": parts[1] + "%", "command": parts[2][:60]})

    # Generate intelligent rightsizing recommendation
    recommendation = {}
    if swap_used > 50 or ram_pct > 85.0:
        recommended_ram = allocated_ram * 2
        recommendation = {
            "action": "EXPAND_RAM",
            "current_ram_mib": allocated_ram,
            "recommended_ram_mib": recommended_ram,
            "reason": f"High memory pressure detected ({ram_pct}% RAM used, {swap_used} MiB swap active). Workload requires memory expansion.",
        }
    elif ram_pct < 25.0 and allocated_ram > 1024:
        recommended_ram = max(1024, allocated_ram // 2)
        recommendation = {
            "action": "REDUCE_RAM",
            "current_ram_mib": allocated_ram,
            "recommended_ram_mib": recommended_ram,
            "reclaimable_mib": allocated_ram - recommended_ram,
            "reason": f"Workload is lightly loaded ({ram_pct}% RAM used: {mem_used} MiB of {allocated_ram} MiB). Downsizing can reclaim {allocated_ram - recommended_ram} MiB on hypervisor.",
        }
    else:
        recommendation = {
            "action": "OPTIMAL",
            "current_ram_mib": allocated_ram,
            "recommended_ram_mib": allocated_ram,
            "reason": f"Memory allocation is balanced ({ram_pct}% RAM used, zero swap thrashing).",
        }

    cpu_status = "NORMAL"
    if load_1m > allocated_vcpu:
        cpu_status = "OVERLOADED"
        recommendation["vcpu_guidance"] = f"CPU 1-min load average ({load_1m}) exceeds allocated cores ({allocated_vcpu}). Consider allocating additional vCPUs."
    else:
        recommendation["vcpu_guidance"] = f"CPU utilization is well within core capacity ({load_1m} load on {allocated_vcpu} cores)."

    return {
        "success": True,
        "vm_name": vm_name,
        "status": "RUNNING",
        "cpu": {
            "allocated_cores": allocated_vcpu,
            "load_1m": load_1m,
            "load_5m": load_5m,
            "status": cpu_status,
        },
        "memory": {
            "allocated_mib": allocated_ram,
            "used_mib": mem_used,
            "available_mib": mem_avail,
            "utilization_pct": ram_pct,
            "swap_used_mib": swap_used,
        },
        "top_processes": top_processes,
        "rightsizing": recommendation,
        "message": f"Profiled '{vm_name}': {ram_pct}% RAM used ({mem_used} / {allocated_ram} MiB), load {load_1m}. {recommendation['reason']}",
    }
