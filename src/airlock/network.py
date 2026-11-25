"""AirlockVM Smart Static IP & MAC Auto-Allocator.

Audits inventory network topologies and reservations to assign conflict-free
static IP addresses and randomized collision-free MAC addresses on virbr0 bridges.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
import random
import shlex
from typing import Any
import yaml

from airlock.inventory import find_vmhost_yaml, get_inventory_search_dirs, get_primary_inventory_dir
from airlock.transport import run_host_cmd

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = get_primary_inventory_dir()


def get_live_dhcp_leases(vmhost: str, network_name: str = "default") -> tuple[set[str], set[str]]:
    """Queries live libvirt DHCP leases on the hypervisor to avoid reusing recently leased IPs/MACs."""
    q_net = shlex.quote(network_name)
    cmd = f"virsh -c qemu:///system net-dhcp-leases {q_net} 2>/dev/null || sudo -n virsh net-dhcp-leases {q_net} 2>/dev/null"
    rc, out, _ = run_host_cmd(vmhost, cmd, timeout=4)
    leased_ips: set[str] = set()
    leased_macs: set[str] = set()
    if rc != 0 or not out:
        return leased_ips, leased_macs
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and ":" in parts[2]:
            leased_macs.add(parts[2].strip().lower())
            ip_val = parts[4].split("/")[0].strip()
            if ip_val:
                leased_ips.add(ip_val)
    return leased_ips, leased_macs


def allocate_vm_network(
    vmhost: str,
    network_name: str = "default",
    exclude_vm: str | None = None,
) -> dict[str, Any]:
    """Allocates the next available collision-free static IP and MAC on the hypervisor network."""
    host_yaml, host_doc = find_vmhost_yaml(vmhost)
    if not host_yaml or not host_doc:
        return {
            "success": False,
            "error": f"Hypervisor '{vmhost}' not found in inventory directory.",
        }

    vmhost_data = host_doc.get("vmhost", {})
    virt = vmhost_data.get("virtualization", {})
    networks = virt.get("networks", [])
    target_net = next((n for n in networks if n.get("name") == network_name), None)

    gateway_ip = target_net.get("ip", "192.168.122.1") if target_net else "192.168.122.1"
    netmask = target_net.get("netmask", "255.255.255.0") if target_net else "255.255.255.0"
    bridge = target_net.get("bridge", "virbr0") if target_net else "virbr0"

    # Calculate base subnet
    network_cidr = ipaddress.IPv4Network(f"{gateway_ip}/{netmask}", strict=False)

    # Collect all used IPs and MACs across inventory for this vmhost
    used_ips: set[str] = {
        gateway_ip,
        str(network_cidr.network_address),
        str(network_cidr.broadcast_address),
    }
    used_macs: set[str] = set()

    for inv_dir in get_inventory_search_dirs():
        if not inv_dir.exists():
            continue
        for vm_yaml in inv_dir.glob(f"{vmhost}/*/vm.yaml"):
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

    # Also avoid active live DHCP leases on the hypervisor (prevents dnsmasq rejecting static IPs still leased to purged MACs)
    live_ips, live_macs = get_live_dhcp_leases(vmhost, network_name)
    used_ips.update(live_ips)
    used_macs.update(live_macs)

    # Search for next available IP prioritizing high static range (.230-.253 within the subnet)
    allocated_ip = None
    all_hosts = list(network_cidr.hosts())
    if len(all_hosts) >= 253:
        preferred_candidates = [str(ip) for ip in all_hosts[229:253]]
    else:
        preferred_candidates = [str(ip) for ip in all_hosts[-24:]]

    for cand in preferred_candidates:
        if cand not in used_ips:
            allocated_ip = cand
            break

    if not allocated_ip:
        for host in all_hosts:
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
