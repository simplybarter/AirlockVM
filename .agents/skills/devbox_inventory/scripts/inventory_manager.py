#!/usr/bin/env python3
"""
DevBox Inventory Manager Utility (inventory_manager.py)
Provides convenient listing, inspection, and querying of recorded hypervisors and VMs.
"""

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent


def find_repo_root() -> Path:
    if len(SKILL_DIR.parents) >= 3 and SKILL_DIR.parents[1].name == ".agents":
        return SKILL_DIR.parents[2].resolve()
    cwd = Path.cwd().resolve()
    for p in [cwd] + list(cwd.parents):
        if (p / ".git").exists() or (p / ".agents").exists():
            return p
    return cwd


REPO_ROOT = find_repo_root()
INVENTORY_ROOT = REPO_ROOT / "inventory"


def list_inventory(inventory_root: Path):
    """Lists all discovered vmhosts and virtual machines."""
    if not inventory_root.exists():
        print(f"No inventory found at {inventory_root}.")
        return

    hosts = [p for p in inventory_root.iterdir() if p.is_dir()]
    if not hosts:
        print(f"Inventory directory {inventory_root} is empty.")
        return

    print("=" * 70)
    print(" DevBox Hypervisor & Virtual Machine Inventory")
    print("=" * 70)

    for host_dir in sorted(hosts, key=lambda p: p.name):
        host_yaml = host_dir / "vmhost.yaml"
        host_status = "unrecorded"
        host_specs = ""
        if host_yaml.exists() and yaml is not None:
            try:
                data = yaml.safe_load(host_yaml.read_text(encoding="utf-8"))
                vmhost = data.get("vmhost", {})
                sys_info = vmhost.get("system", {})
                hw_info = vmhost.get("hardware", {})
                cpu_total = hw_info.get("cpu", {}).get("total_cores", "?")
                ram_gb = hw_info.get("memory", {}).get("total_gb", "?")
                host_specs = f"({cpu_total} cores, {ram_gb} GB RAM, {sys_info.get('os', {}).get('pretty_name', '')})"
                host_status = "documented"
            except Exception:
                host_status = "yaml parse error"

        print(f"\n[Host] {host_dir.name} {host_specs} [{host_status}]")
        print(f"       File: {host_yaml.relative_to(REPO_ROOT) if host_yaml.exists() else 'missing'}")

        # Subdirectories (VMs)
        vms = [v for v in host_dir.iterdir() if v.is_dir()]
        if not vms:
            print("       No virtual machines documented.")
        else:
            for vm_dir in sorted(vms, key=lambda p: p.name):
                vm_yaml = vm_dir / "vm.yaml"
                vm_detail = ""
                if vm_yaml.exists() and yaml is not None:
                    try:
                        vm_data = yaml.safe_load(vm_yaml.read_text(encoding="utf-8")).get("vm", {})
                        state = vm_data.get("state", "unknown")
                        vcpu = vm_data.get("resources", {}).get("vcpu", "?")
                        ram = vm_data.get("resources", {}).get("memory_mib", "?")
                        # get primary ip
                        ifaces = vm_data.get("network", {}).get("interfaces", [])
                        ip_str = ifaces[0].get("ip", "") if ifaces else ""
                        bp = vm_data.get("blueprint", {})
                        bp_str = f" | Project: {bp.get('project_name', '')}" if bp.get("project_name") else ""
                        vm_detail = f"[{state} | {vcpu} vCPU | {ram} MiB RAM | IP: {ip_str}{bp_str}]"
                    except Exception:
                        vm_detail = "[error reading vm.yaml]"
                print(f"       • VM: {vm_dir.name:<22} {vm_detail}")
                print(f"             Path: {vm_dir.relative_to(REPO_ROOT)}/vm.yaml")

    print("\n" + "=" * 70 + "\n")


def show_record(host: str, vm: str | None, inventory_root: Path):
    """Outputs the raw or formatted YAML for a given host or VM."""
    host_dir = inventory_root / host
    if not host_dir.exists():
        print(f"Error: Host '{host}' not found in inventory ({host_dir}).")
        sys.exit(1)

    if vm:
        vm_file = host_dir / vm / "vm.yaml"
        if not vm_file.exists():
            print(f"Error: VM '{vm}' not found under host '{host}' ({vm_file}).")
            sys.exit(1)
        print(vm_file.read_text(encoding="utf-8"))
    else:
        host_file = host_dir / "vmhost.yaml"
        if not host_file.exists():
            print(f"Error: vmhost.yaml not found for host '{host}' ({host_file}).")
            sys.exit(1)
        print(host_file.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Query and manage DevBox VM host and guest inventory")
    subparsers = parser.add_subparsers(dest="command")

    # list
    list_p = subparsers.add_parser("list", help="List all hosts and VMs in inventory")
    list_p.add_argument("--inventory", type=Path, default=INVENTORY_ROOT)

    # show
    show_p = subparsers.add_parser("show", help="Display YAML record for a host or VM")
    show_p.add_argument("host", help="Hypervisor host name")
    show_p.add_argument("vm", nargs="?", default=None, help="Optional VM name")
    show_p.add_argument("--inventory", type=Path, default=INVENTORY_ROOT)

    args = parser.parse_args()

    if args.command == "show":
        show_record(args.host, args.vm, args.inventory)
    else:
        list_inventory(args.inventory if hasattr(args, "inventory") else INVENTORY_ROOT)


if __name__ == "__main__":
    main()
