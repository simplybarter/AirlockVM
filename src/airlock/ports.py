"""AirlockVM Intelligent Port Allocation Engine.

Scans inventory SSOT and queries target hypervisors over SSH to detect
occupied ports and allocate conflict-free external forwarding ports.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any
import yaml

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = AIRLOCK_ROOT / "inventory"


def get_inventory_used_ports(
    vmhost: str | None = None,
    exclude_vm: str | None = None,
    exclude_app: str | None = None,
) -> set[int]:
    """Gathers all external proxy ports registered across inventory VM blueprints."""
    used_ports: set[int] = set()
    if not INVENTORY_DIR.exists():
        return used_ports

    pattern = f"{vmhost}/*/vm.yaml" if vmhost else "*/*/vm.yaml"
    for yaml_path in INVENTORY_DIR.glob(pattern):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
            vm_data = doc.get("vm", {})
            vm_name = vm_data.get("name", yaml_path.parent.name)
            bp = vm_data.get("blueprint", {})

            # Blueprint default port
            ext_default = bp.get("proxy_external_port")
            if ext_default:
                try:
                    used_ports.add(int(ext_default))
                except (ValueError, TypeError):
                    pass

            # Per-repository ports
            for repo in bp.get("repositories", []):
                rname = repo.get("name")
                if exclude_vm and vm_name == exclude_vm and rname == exclude_app:
                    # Allow re-allocating or updating the same app
                    continue
                p = repo.get("external_port")
                if p:
                    try:
                        used_ports.add(int(p))
                    except (ValueError, TypeError):
                        pass
        except Exception:
            continue

    return used_ports


def get_hypervisor_live_ports(vmhost: str, timeout: int = 4) -> set[int]:
    """Queries live listening TCP ports on the hypervisor host over SSH or locally."""
    live_ports: set[int] = set()
    if not vmhost or vmhost in ("localhost", "127.0.0.1"):
        cmd = ["ss", "-tlnH"]
    else:
        cmd = [
            "ssh",
            "-o",
            "ConnectTimeout=3",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            vmhost,
            "ss -tlnH",
        ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 4:
                    local_addr = parts[3]
                    match = re.search(r":(\d+)$", local_addr)
                    if match:
                        try:
                            live_ports.add(int(match.group(1)))
                        except ValueError:
                            pass
    except Exception:
        pass

    return live_ports


def allocate_external_port(
    vmhost: str,
    internal_port: int = 3000,
    preferred_port: int | None = None,
    exclude_vm: str | None = None,
    exclude_app: str | None = None,
) -> tuple[int, str]:
    """Intelligently allocates the next conflict-free external proxy port.

    Returns:
        tuple[int, str]: (allocated_port, reason_message)
    """
    inv_ports = get_inventory_used_ports(
        vmhost=vmhost,
        exclude_vm=exclude_vm,
        exclude_app=exclude_app,
    )
    live_ports = get_hypervisor_live_ports(vmhost)
    all_occupied = inv_ports | live_ports

    # Check preferred port if provided
    if preferred_port and 1024 < preferred_port < 65535:
        if preferred_port not in all_occupied:
            return preferred_port, f"Preferred external port {preferred_port} is available on {vmhost}."
        else:
            reason = "active host service" if preferred_port in live_ports else "inventory reservation"
            prefix = f"Preferred port {preferred_port} was occupied by {reason} on {vmhost}; "
    else:
        prefix = ""

    # Base candidate selection
    if internal_port in (80, 443):
        candidate = 8080
    elif 1024 <= internal_port < 65530:
        candidate = internal_port + 1
    else:
        candidate = 3001

    # Scan for next free port
    while candidate in all_occupied or candidate < 1024 or candidate > 65535:
        candidate += 1

    return candidate, f"{prefix}automatically allocated available external port {candidate} on {vmhost}."
