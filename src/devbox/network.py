"""DevBox Smart Static IP & MAC Auto-Allocator.

Audits inventory network topologies and reservations to assign conflict-free
static IP addresses and randomized collision-free MAC addresses on virbr0 bridges.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
import random
from typing import Any
import yaml

DEVBOX_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = DEVBOX_ROOT / "inventory"


def allocate_vm_network(
    vmhost: str,
    network_name: str = "default",
    exclude_vm: str | None = None,
) -> dict[str, Any]:
    """Allocates the next available collision-free static IP and MAC on the hypervisor network."""
    host_yaml = INVENTORY_DIR / vmhost / "vmhost.yaml"
    if not host_yaml.exists():
        return {
            "success": False,
            "error": f"Hypervisor '{vmhost}' not found in inventory directory.",
        }

    try:
        with open(host_yaml, "r", encoding="utf-8") as f:
            host_doc = yaml.safe_load(f) or {}
    except Exception as e:
        return {"success": False, "error": f"Error reading {host_yaml}: {e}"}

    vmhost_data = host_doc.get("vmhost", {})
    virt = vmhost_data.get("virtualization", {})
    networks = virt.get("networks", [])
    target_net = next((n for n in networks if n.get("name") == network_name), None)

    gateway_ip = target_net.get("ip", "192.168.122.1") if target_net else "192.168.122.1"
    netmask = target_net.get("netmask", "255.255.255.0") if target_net else "255.255.255.0"
    bridge = target_net.get("bridge", "virbr0") if target_net else "virbr0"

    # Collect all used IPs and MACs across inventory for this vmhost
    used_ips: set[str] = {gateway_ip, "192.168.122.0", "192.168.122.255"}
    used_macs: set[str] = set()

    for vm_yaml in INVENTORY_DIR.glob(f"{vmhost}/*/vm.yaml"):
        if exclude_vm and vm_yaml.parent.name == exclude_vm:
            continue
        try:
            with open(vm_yaml, "r", encoding="utf-8") as f:
                vdoc = yaml.safe_load(f) or {}
            net_data = vdoc.get("vm", {}).get("network", {})
            for iface in net_data.get("interfaces", []):
                if iface.get("ip"):
                    used_ips.add(str(iface["ip"]).strip())
                if iface.get("mac"):
                    used_macs.add(str(iface["mac"]).strip().lower())
        except Exception:
            continue

    # Also collect static_reservations from vmhost.yaml
    if target_net:
        for res in target_net.get("static_reservations", []):
            if exclude_vm and res.get("name") == exclude_vm:
                continue
            if res.get("ip"):
                used_ips.add(str(res["ip"]).strip())
            if res.get("mac"):
                used_macs.add(str(res["mac"]).strip().lower())

    # Calculate base subnet
    network_cidr = ipaddress.IPv4Network(f"{gateway_ip}/{netmask}", strict=False)

    # Search for next available IP starting from high DHCP pool (e.g. .230 -> .254) or ascending from .10
    allocated_ip = None
    # Prioritize typical devbox static range .230-.250
    preferred_candidates = [f"192.168.122.{i}" for i in range(230, 254)]
    for cand in preferred_candidates:
        if cand not in used_ips:
            allocated_ip = cand
            break

    if not allocated_ip:
        for host in network_cidr.hosts():
            cand = str(host)
            if cand not in used_ips:
                allocated_ip = cand
                break

    if not allocated_ip:
        return {"success": False, "error": f"Subnet {network_cidr} is fully exhausted."}

    # Generate unique collision-free MAC (QEMU OUI prefix 52:54:00)
    allocated_mac = None
    for _ in range(100):
        b1 = random.randint(0x00, 0x7F)
        b2 = random.randint(0x00, 0xFF)
        b3 = random.randint(0x00, 0xFF)
        mac_cand = f"52:54:00:{b1:02x}:{b2:02x}:{b3:02x}".lower()
        if mac_cand not in used_macs:
            allocated_mac = mac_cand
            break

    return {
        "success": True,
        "vmhost": vmhost,
        "network": network_name,
        "bridge": bridge,
        "allocated_ip": allocated_ip,
        "allocated_mac": allocated_mac,
        "gateway": gateway_ip,
        "netmask": netmask,
        "subnet": str(network_cidr),
        "message": f"Allocated clean static IP {allocated_ip} and MAC {allocated_mac} on {vmhost}:{bridge}",
    }
