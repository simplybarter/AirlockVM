#!/usr/bin/env python3
"""
Hypervisor Auto-Discovery & Network Probing Tool
Queries target hypervisor (local or via SSH) to automatically detect hardware,
network bridges, DHCP reservations, and propose conflict-free IP/MAC parameters.
"""

import argparse
import ipaddress
import json
import random
import re
import shlex
import subprocess
import sys
from pathlib import Path

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
DEFAULT_PROJECT_NAME = REPO_ROOT.name


class HypervisorCapabilityError(Exception):
    """Raised when target host fails pre-flight virtualization requirements."""
    pass


def run_cmd_status(cmd: list[str], host: str | None = None) -> tuple[int, str, str]:
    """Executes a command locally or over SSH and returns (returncode, stdout, stderr)."""
    if host and host not in ("localhost", "127.0.0.1"):
        full_cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            host,
            shlex.join(cmd),
        ]
    else:
        full_cmd = cmd

    try:
        res = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def check_hypervisor_capabilities(host: str) -> None:
    """Runs rigorous pre-flight validation on the target hypervisor before probing.
    
    Verifies:
    1. SSH reachability & host connectivity
    2. Hardware virtualization (/dev/kvm and CPU flags)
    3. libvirt & QEMU installation and running daemon
    4. Virtual network (default) & bridge (virbr0) state
    5. Minimum RAM and backing disk storage capacity
    """
    print(f"[*] Running Pre-Flight Hypervisor Capability Checks on '{host}'...")
    failures = []
    warnings = []

    # 1. Connectivity Check
    ret, out, err = run_cmd_status(["echo", "connected"], host=host)
    if ret != 0:
        raise HypervisorCapabilityError(
            f"Cannot connect to target host '{host}'.\n"
            f"  Details: {err or 'Connection timed out or host unreachable'}\n"
            f"  Remediation: Verify SSH alias/IP in ~/.ssh/config, ensure public key authentication is configured."
        )

    sudo_ret, _, _ = run_cmd_status(["sudo", "-n", "true"], host=host)
    if sudo_ret != 0:
        warnings.append("User does not have passwordless sudo on hypervisor. Administrative commands may prompt for password.")
        print(f"    [!] Host Connectivity & Sudo: WARNING (passwordless sudo not active)")
    else:
        print(f"    [✓] Host Connectivity & Sudo: PASS")

    # 2. Hardware Virtualization Support (/dev/kvm)
    kvm_ret, _, _ = run_cmd_status(["test", "-e", "/dev/kvm"], host=host)
    if kvm_ret != 0:
        _, cpu_flags, _ = run_cmd_status(["grep", "-E", "-m1", "(vmx|svm)", "/proc/cpuinfo"], host=host)
        if cpu_flags:
            failures.append(
                "Hardware Virtualization: /dev/kvm device is missing, but CPU supports virtualization.\n"
                "  Remediation: Load KVM kernel module: 'sudo modprobe kvm kvm_intel' (or kvm_amd)."
            )
        else:
            failures.append(
                "Hardware Virtualization: Host CPU does not have VT-x/AMD-V virtualization extensions enabled.\n"
                "  Remediation:\n"
                "  - If physical server: Enable Intel VT-x or AMD-V in system BIOS/UEFI.\n"
                "  - If cloud/virtual machine: Enable nested virtualization on the parent hypervisor (e.g. kvm_intel.nested=1)."
            )
        print(f"    [✗] Hardware Virtualization (/dev/kvm): FAIL")
    else:
        print(f"    [✓] Hardware Virtualization (/dev/kvm): PASS")

    # 3. libvirt & QEMU Installation
    virsh_ret, virsh_path, _ = run_cmd_status(["which", "virsh"], host=host)
    if virsh_ret != 0:
        failures.append(
            "libvirt Hypervisor: 'virsh' CLI is not installed.\n"
            "  Remediation: Install libvirt & QEMU packages:\n"
            "    sudo apt update && sudo apt install -y qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils"
        )
        print(f"    [✗] libvirt Hypervisor Daemon: FAIL ('virsh' command not found)")
    else:
        uri_ret, uri_out, _ = run_cmd_status(["sudo", "virsh", "uri"], host=host)
        if uri_ret != 0:
            failures.append(
                f"libvirt Daemon: 'virsh' is installed but daemon is not responding.\n"
                f"  Remediation: Start and enable libvirtd: 'sudo systemctl enable --now libvirtd'"
            )
            print(f"    [✗] libvirt Hypervisor Daemon: FAIL (daemon not running)")
        else:
            print(f"    [✓] libvirt Hypervisor Daemon: PASS ({uri_out})")

    # 4. Virtual Network & Bridge
    net_ret, net_out, _ = run_cmd_status(["sudo", "virsh", "net-info", "default"], host=host)
    if net_ret != 0:
        failures.append(
            "Virtual Network: Default libvirt network does not exist.\n"
            "  Remediation: Define and start default network:\n"
            "    sudo virsh net-define /etc/libvirt/qemu/networks/default.xml\n"
            "    sudo virsh net-start default && sudo virsh net-autostart default"
        )
        print(f"    [✗] Virtual Network (default / virbr0): FAIL (network missing)")
    elif "Active:         no" in net_out or "Active: no" in net_out:
        failures.append(
            "Virtual Network: Default network exists but is inactive.\n"
            "  Remediation: Start default network: 'sudo virsh net-start default && sudo virsh net-autostart default'"
        )
        print(f"    [✗] Virtual Network (default / virbr0): FAIL (network inactive)")
    else:
        print(f"    [✓] Virtual Network (default / virbr0): PASS (Active)")

    # 5. Host Memory & Storage Capacity
    mem_ret, mem_out, _ = run_cmd_status(["free", "-m"], host=host)
    total_ram_mb = 16384
    if mem_ret == 0:
        for line in mem_out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                if len(parts) >= 2:
                    total_ram_mb = int(parts[1])
                break

    if total_ram_mb < 3500:
        warnings.append(
            f"Host Memory: Host has only {total_ram_mb} MB RAM. Running execution VMs alongside host services may cause high memory pressure."
        )

    # Check disk space in /var/lib/libvirt/images or root /
    df_ret, df_out, _ = run_cmd_status(["df", "-m", "/var/lib/libvirt/images"], host=host)
    if df_ret != 0:
        df_ret, df_out, _ = run_cmd_status(["df", "-m", "/"], host=host)

    free_disk_gb = 50
    if df_ret == 0:
        lines = df_out.splitlines()
        if len(lines) >= 2:
            parts = lines[-1].split()
            if len(parts) >= 4:
                try:
                    free_disk_gb = int(parts[3]) // 1024
                except ValueError:
                    pass

    if free_disk_gb < 10:
        failures.append(
            f"Disk Storage: Host has only {free_disk_gb} GB free disk space.\n"
            f"  Remediation: Free up disk space on target hypervisor before provisioning a 50 GB VM disk image."
        )
        print(f"    [✗] Host Memory & Storage: FAIL ({free_disk_gb} GB disk space remaining)")
    else:
        print(f"    [✓] Host Memory & Storage: PASS ({total_ram_mb // 1024} GB RAM, {free_disk_gb} GB disk free)")

    if warnings:
        for w in warnings:
            print(f"    [!] Warning: {w}")

    if failures:
        msg = f"\n[!] Pre-Flight Capability Checks FAILED for host '{host}'.\n"
        msg += f"    The host cannot be used as a AirlockVM VM execution hypervisor until the following issues are resolved:\n\n"
        for idx, f in enumerate(failures, 1):
            msg += f"    Issue #{idx}: {f}\n\n"
        raise HypervisorCapabilityError(msg)


def run_cmd(cmd: list[str], host: str | None = None, check: bool = True) -> str:
    """Executes a command locally or over SSH."""
    if host and host not in ("localhost", "127.0.0.1"):
        full_cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            host,
            shlex.join(cmd),
        ]
    else:
        full_cmd = cmd

    try:
        res = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=check,
        )
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            raise RuntimeError(f"Command failed ({' '.join(full_cmd)}): {e.stderr.strip()}") from e
        return ""


def get_local_info() -> dict:
    """Detects development workstation properties."""
    dev_host = run_cmd(["hostname", "-s"])
    local_user = run_cmd(["whoami"])
    return {
        "DEV_HOST": dev_host,
        "DEV_HOST_TITLE": dev_host.capitalize(),
        "VM_SECONDARY_USER": local_user,
    }


def parse_hypervisor_hardware(host: str) -> dict:
    """Probes OS, CPU, and RAM on the hypervisor."""
    os_release = run_cmd(["cat", "/etc/os-release"], host=host, check=False)
    pretty_os = "Linux"
    for line in os_release.splitlines():
        if line.startswith("PRETTY_NAME="):
            pretty_os = line.split("=", 1)[1].strip('"')
            break

    # CPU model & cores
    lscpu_out = run_cmd(["lscpu"], host=host, check=False)
    cpu_model = "CPU"
    cores = "4"
    for line in lscpu_out.splitlines():
        if line.startswith("Model name:"):
            cpu_model = re.sub(r"\s+", " ", line.split(":", 1)[1].strip())
        elif line.startswith("CPU(s):"):
            cores = line.split(":", 1)[1].strip()

    # RAM
    free_out = run_cmd(["free", "-m"], host=host, check=False)
    total_ram_mb = 16384
    total_ram_gb = "16"
    for line in free_out.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 2:
                total_ram_mb = int(parts[1])
                total_ram_gb = str(round(total_ram_mb / 1024))
            break

    ram_str = f"{total_ram_gb} GB RAM" if total_ram_gb else "RAM"
    specs_str = f"{cores} Cores {cpu_model} | {ram_str} | KVM / libvirt"

    # LAN IP detection
    route_out = run_cmd(["ip", "-4", "route", "get", "1.1.1.1"], host=host, check=False)
    lan_ip = ""
    ip_match = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", route_out)
    if ip_match:
        lan_ip = ip_match.group(1)
    else:
        # Fallback to hostname -I
        host_i = run_cmd(["hostname", "-I"], host=host, check=False)
        if host_i:
            lan_ip = host_i.split()[0]

    return {
        "HYPERVISOR_HOST": host,
        "HYPERVISOR_HOST_TITLE": host.capitalize(),
        "HYPERVISOR_OS": pretty_os,
        "HYPERVISOR_SPECS": specs_str,
        "HYPERVISOR_LAN_IP": lan_ip,
        "_TOTAL_CORES": int(cores) if cores.isdigit() else 4,
        "_TOTAL_RAM_MB": total_ram_mb,
    }


def calculate_proportional_sizing(host_cores: int, total_ram_mb: int) -> dict:
    """Calculates proportional vCPU and RAM allocations based on host hardware capacity.
    
    Leaves sufficient compute resources for host OS and workstation tasks.
    - vCPUs: 50% of host cores (up to 8 vCPUs max for a standard development VM).
    - RAM: ~25% of host RAM rounded to standard allocation tiers (2GB, 4GB, 8GB, 16GB, 32GB).
    """
    # Proportional vCPUs
    if host_cores <= 2:
        vcpu = 1
    elif host_cores <= 4:
        vcpu = 2
    elif host_cores <= 8:
        vcpu = 4
    elif host_cores <= 16:
        vcpu = 6
    else:
        vcpu = 8

    # Proportional RAM (MiB)
    if total_ram_mb <= 4096:
        ram_mib = 2048
    elif total_ram_mb <= 8192:
        ram_mib = 4096
    elif total_ram_mb <= 16384:
        ram_mib = 4096
    elif total_ram_mb <= 32768:
        ram_mib = 8192
    elif total_ram_mb <= 65536:
        ram_mib = 16384
    else:
        ram_mib = 32768

    ram_gb = str(ram_mib // 1024)
    return {
        "VCPU_CORES": str(vcpu),
        "RAM_MIB": str(ram_mib),
        "RAM_GB": ram_gb,
    }


def derive_vm_naming(project_name: str, existing_domains: list[str] = None) -> dict:
    """Derives default VM name, usernames, and service paths from project name with collision prevention."""
    project_slug = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-")
    base_vm_name = f"{project_slug}-vm"
    vm_name = base_vm_name

    if existing_domains:
        idx = 1
        while vm_name in existing_domains:
            idx += 1
            vm_name = f"{project_slug}-vm-{idx:02d}"

    vm_user = project_slug.replace("-", "")
    return {
        "PROJECT_NAME": project_name,
        "VM_NAME": vm_name,
        "VM_PRIMARY_USER": vm_user,
        "SSH_KEY_PATH": f"~/.ssh/{vm_user}_vm_ed25519",
        "PROXY_SOCKET_SERVICE": f"/etc/systemd/system/{project_slug}-proxy.socket",
        "REMOTE_CODE_DIR": f"~/{project_name}/",
    }


def parse_libvirt_topology(host: str) -> dict:
    """Probes libvirt network XML, existing domains, and DHCP leases."""
    # List domains
    domains_out = run_cmd(["sudo", "virsh", "list", "--all", "--name"], host=host, check=False)
    existing_domains = [d.strip() for d in domains_out.splitlines() if d.strip()]

    # Net dumpxml default
    net_xml = run_cmd(["sudo", "virsh", "net-dumpxml", "default"], host=host, check=False)

    bridge = "virbr0"
    bridge_match = re.search(r"<bridge\s+name='([^']+)'", net_xml)
    if bridge_match:
        bridge = bridge_match.group(1)

    network_name = "default"
    net_name_match = re.search(r"<name>([^<]+)</name>", net_xml)
    if net_name_match:
        network_name = net_name_match.group(1)

    # Subnet IP & netmask
    ip_match = re.search(r"<ip\s+address='([^']+)'\s+netmask='([^']+)'", net_xml)
    subnet_base = "192.168.122.1"
    netmask = "255.255.255.0"
    if ip_match:
        subnet_base = ip_match.group(1)
        netmask = ip_match.group(2)

    net_interface = ipaddress.IPv4Interface(f"{subnet_base}/{netmask}")
    cidr_suffix = f"/{net_interface.network.prefixlen}"

    # Extract existing static hosts in DHCP
    assigned_ips = set()
    assigned_macs = set()
    for host_match in re.finditer(r"<host\s+mac='([^']+)'(?:\s+name='([^']*)')?\s+ip='([^']+)'", net_xml):
        assigned_macs.add(host_match.group(1).lower())
        assigned_ips.add(host_match.group(3))

    # Also check dynamic DHCP leases
    leases_out = run_cmd(["sudo", "virsh", "net-dhcp-leases", network_name], host=host, check=False)
    for line in leases_out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and ":" in parts[2]:
            assigned_macs.add(parts[2].lower())
            ip_val = parts[4].split("/")[0]
            assigned_ips.add(ip_val)

    # Propose next free IP in upper pool (e.g. 192.168.122.230 - 250)
    proposed_ip = None
    network_obj = net_interface.network
    # Search backwards from .245 down to .200
    base_prefix = ".".join(subnet_base.split(".")[:3])
    for last_octet in range(236, 250):
        test_ip = f"{base_prefix}.{last_octet}"
        if test_ip not in assigned_ips:
            proposed_ip = test_ip
            break

    if not proposed_ip:
        # Fallback to any unassigned host IP
        for host_addr in reversed(list(network_obj.hosts())):
            test_ip = str(host_addr)
            if test_ip not in assigned_ips and test_ip != subnet_base:
                proposed_ip = test_ip
                break

    # Generate a unique QEMU/KVM MAC (52:54:00:xx:xx:xx)
    proposed_mac = None
    for _ in range(100):
        suffix = ":".join(f"{random.randint(0, 255):02x}" for _ in range(3))
        candidate_mac = f"52:54:00:{suffix}".lower()
        if candidate_mac not in assigned_macs:
            proposed_mac = candidate_mac
            break

    return {
        "VIRT_BRIDGE": bridge,
        "LIBVIRT_NETWORK": network_name,
        "SUBNET_CIDR": cidr_suffix,
        "VM_IP": proposed_ip or "192.168.122.236",
        "VM_MAC": proposed_mac or "52:54:00:6d:3d:ec",
        "_EXISTING_DOMAINS": existing_domains,
        "_EXISTING_IPS": sorted(list(assigned_ips)),
    }


def find_next_free_port(host: str, base_port: int = 3001) -> int:
    """Checks if external forwarded port is already in use on the hypervisor."""
    ss_out = run_cmd(["ss", "-tulpn"], host=host, check=False)
    for port in range(base_port, base_port + 50):
        if f":{port} " not in ss_out and f":{port}\n" not in ss_out:
            return port
    return base_port


def probe_all(hypervisor_host: str, project_name: str = "AirlockVM", skip_preflight: bool = False) -> dict:
    """Runs end-to-end discovery and builds a recommended parameter dictionary."""
    if not skip_preflight:
        check_hypervisor_capabilities(hypervisor_host)

    print(f"[*] Probing development workstation...")
    local_info = get_local_info()
    print(f"    -> Workstation: {local_info['DEV_HOST']} (User: {local_info['VM_SECONDARY_USER']})")

    print(f"[*] Connecting to hypervisor '{hypervisor_host}'...")
    hw_info = parse_hypervisor_hardware(hypervisor_host)
    total_cores = hw_info.pop("_TOTAL_CORES", 4)
    total_ram_mb = hw_info.pop("_TOTAL_RAM_MB", 16384)
    print(f"    -> OS: {hw_info['HYPERVISOR_OS']}")
    print(f"    -> Specs: {hw_info['HYPERVISOR_SPECS']}")
    print(f"    -> Physical LAN IP: {hw_info['HYPERVISOR_LAN_IP']}")

    print(f"[*] Calculating proportional VM resource sizing...")
    sizing = calculate_proportional_sizing(total_cores, total_ram_mb)
    print(f"    -> Allocated vCPUs: {sizing['VCPU_CORES']} (from {total_cores} host cores)")
    print(f"    -> Allocated RAM: {sizing['RAM_MIB']} MiB ({sizing['RAM_GB']} GB) (from {total_ram_mb // 1024} GB host RAM)")

    print(f"[*] Querying libvirt topology on '{hypervisor_host}'...")
    topo_info = parse_libvirt_topology(hypervisor_host)
    existing_domains = topo_info.pop("_EXISTING_DOMAINS", [])
    existing_ips = topo_info.pop("_EXISTING_IPS", [])
    print(f"    -> Network: {topo_info['LIBVIRT_NETWORK']} (Bridge: {topo_info['VIRT_BRIDGE']})")
    print(f"    -> Existing VMs: {', '.join(existing_domains) if existing_domains else 'None'}")
    print(f"    -> Discovered Next Free IP: {topo_info['VM_IP']}{topo_info['SUBNET_CIDR']}")
    print(f"    -> Generated Collision-Free MAC: {topo_info['VM_MAC']}")

    print(f"[*] Probing port availability on '{hypervisor_host}'...")
    proxy_port = find_next_free_port(hypervisor_host, 3001)
    print(f"    -> Assigned Forward Port: {proxy_port}")

    # Derive default VM naming from project name
    naming = derive_vm_naming(project_name, existing_domains)
    print(f"[*] Derived VM naming from project '{project_name}':")
    print(f"    -> Suggested VM Domain Name: {naming['VM_NAME']}")
    print(f"    -> Primary User: {naming['VM_PRIMARY_USER']}")

    config = {
        "PROJECT_NAME": naming["PROJECT_NAME"],
        "VM_NAME": naming["VM_NAME"],
        "HYPERVISOR_HOST": hw_info["HYPERVISOR_HOST"],
        "HYPERVISOR_HOST_TITLE": hw_info["HYPERVISOR_HOST_TITLE"],
        "HYPERVISOR_OS": hw_info["HYPERVISOR_OS"],
        "HYPERVISOR_SPECS": hw_info["HYPERVISOR_SPECS"],
        "HYPERVISOR_LAN_IP": hw_info["HYPERVISOR_LAN_IP"],
        "DEV_HOST": local_info["DEV_HOST"],
        "DEV_HOST_TITLE": local_info["DEV_HOST_TITLE"],
        "GUEST_OS": "Ubuntu 24.04 LTS Minimal",
        "GUEST_OS_DETAILS": "Pristine Noble Numbat cloud image",
        "VCPU_CORES": sizing["VCPU_CORES"],
        "CPU_MODE": "host-passthrough",
        "RAM_MIB": sizing["RAM_MIB"],
        "RAM_GB": sizing["RAM_GB"],
        "BALLOON_DRIVER": "virtio-balloon",
        "DISK_SIZE_GIB": "50",
        "DISK_FORMAT": "qcow2",
        "UEFI_FIRMWARE": "OVMF_CODE_4M.ms.fd",
        "VM_IP": topo_info["VM_IP"],
        "SUBNET_CIDR": topo_info["SUBNET_CIDR"],
        "VIRT_BRIDGE": topo_info["VIRT_BRIDGE"],
        "LIBVIRT_NETWORK": topo_info["LIBVIRT_NETWORK"],
        "VM_MAC": topo_info["VM_MAC"],
        "VM_PRIMARY_USER": naming["VM_PRIMARY_USER"],
        "VM_SECONDARY_USER": local_info["VM_SECONDARY_USER"],
        "SSH_KEY_PATH": naming["SSH_KEY_PATH"],
        "APP_INTERNAL_PORT": "3000",
        "PROXY_EXTERNAL_PORT": str(proxy_port),
        "PROXY_SOCKET_SERVICE": naming["PROXY_SOCKET_SERVICE"],
        "REMOTE_CODE_DIR": naming["REMOTE_CODE_DIR"],
        "SNAPSHOT_BASE_NAME": "snapshot-base-clean",
        "SNAPSHOT_BASE_DESC": "Pristine base setup with Docker and Python",
    }
    return config
    return config


def main():
    parser = argparse.ArgumentParser(description="Auto-discover hypervisor specifications and network parameters")
    parser.add_argument(
        "--hypervisor",
        required=True,
        help="Target hypervisor hostname, SSH alias, or IP (use 'localhost' for local KVM)",
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT_NAME,
        help=f"Project name (defaults to project root folder: {DEFAULT_PROJECT_NAME})",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip pre-flight virtualization capability checks",
    )
    parser.add_argument("--save-config", type=Path, help="Save discovered parameters to JSON config")
    parser.add_argument("--apply", action="store_true", help="Render inventory VM runbook immediately with discovered values")
    args = parser.parse_args()

    try:
        config = probe_all(args.hypervisor, args.project, skip_preflight=args.skip_preflight)
    except HypervisorCapabilityError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error during probing: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n[+] Discovery Completed Successfully!")
    if args.save_config:
        args.save_config.parent.mkdir(parents=True, exist_ok=True)
        with open(args.save_config, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        print(f"[+] Saved discovered configuration to {args.save_config}")

    if args.apply:
        configure_script = SCRIPT_DIR / "configure_vm.py"
        tmp_config = Path("/tmp/discovered_airlock.json")
        with open(tmp_config, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        subprocess.run([sys.executable, str(configure_script), "--config", str(tmp_config)], check=True)
        tmp_config.unlink(missing_ok=True)
    elif not args.save_config:
        print("\n--- Discovered Configuration (JSON Preview) ---")
        print(json.dumps(config, indent=2))
        print("\nTo apply these settings to inventory VM runbook, run with --apply or --save-config <path>")


if __name__ == "__main__":
    main()
