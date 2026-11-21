#!/usr/bin/env python3
"""
DevBox Host & VM Inventory Script (add_vmhost.py)

Probes a hypervisor host (local or remote via SSH), documents its hardware,
operating system, libvirt networks, storage pools, and virtual machines using
defined YAML templates, and persists the documented files into:
  - inventory/{vmhost}/vmhost.yaml
  - inventory/{vmhost}/{vm}/vm.yaml
"""

import argparse
import datetime
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
TEMPLATES_DIR = SKILL_DIR / "template"
VMHOST_TEMPLATE = TEMPLATES_DIR / "vmhost.yaml.template"
VM_TEMPLATE = TEMPLATES_DIR / "vm.yaml.template"


def find_repo_root() -> Path:
    if len(SKILL_DIR.parents) >= 3 and SKILL_DIR.parents[1].name == ".agents":
        return SKILL_DIR.parents[2].resolve()
    cwd = Path.cwd().resolve()
    for p in [cwd] + list(cwd.parents):
        if (p / ".git").exists() or (p / ".agents").exists():
            return p
    return cwd


REPO_ROOT = find_repo_root()
DEFAULT_INVENTORY_ROOT = REPO_ROOT / "inventory"


def run_cmd(
    cmd: list[str],
    host: str | None = None,
    user: str | None = None,
    port: int = 22,
    key: str | None = None,
    timeout: int = 15,
    check: bool = False,
) -> tuple[int, str, str]:
    """Executes a command locally or via SSH."""
    is_remote = host and host not in ("localhost", "127.0.0.1")
    if is_remote:
        ssh_args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(port),
        ]
        if key:
            ssh_args.extend(["-i", key])
        destination = f"{user}@{host}" if user else host
        full_cmd = ssh_args + [destination] + cmd
    else:
        full_cmd = cmd

    try:
        res = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=check,
        )
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s: {' '.join(full_cmd)}"
    except Exception as e:
        return 1, "", str(e)


def probe_host(host: str, user: str | None = None, port: int = 22, key: str | None = None) -> dict:
    """Probes the target hypervisor host for hardware, OS, and libvirt specs."""
    data = {
        "host": host,
        "is_remote": host not in ("localhost", "127.0.0.1"),
        "user": user or "",
        "port": port,
    }

    # 1. Connectivity test
    rc, out, err = run_cmd(["echo", "connected"], host=host, user=user, port=port, key=key)
    if rc != 0:
        raise ConnectionError(f"Cannot reach host '{host}': {err or 'Connection failed'}")

    # 2. Hostname & FQDN
    _, hostname, _ = run_cmd(["hostname", "-s"], host=host, user=user, port=port, key=key)
    _, fqdn, _ = run_cmd(["hostname", "-f"], host=host, user=user, port=port, key=key)
    data["hostname"] = hostname or host
    data["fqdn"] = fqdn or (hostname or host)

    # 3. OS & Kernel
    _, os_rel, _ = run_cmd(["cat", "/etc/os-release"], host=host, user=user, port=port, key=key)
    os_info = {"id": "linux", "version_id": "unknown", "pretty_name": "Linux"}
    for line in os_rel.splitlines():
        if line.startswith("ID="):
            os_info["id"] = line.split("=", 1)[1].strip('"\'')
        elif line.startswith("VERSION_ID="):
            os_info["version_id"] = line.split("=", 1)[1].strip('"\'')
        elif line.startswith("PRETTY_NAME="):
            os_info["pretty_name"] = line.split("=", 1)[1].strip('"\'')
    data["os"] = os_info

    _, uname_r, _ = run_cmd(["uname", "-r"], host=host, user=user, port=port, key=key)
    _, uname_m, _ = run_cmd(["uname", "-m"], host=host, user=user, port=port, key=key)
    _, uptime_str, _ = run_cmd(["uptime", "-p"], host=host, user=user, port=port, key=key)
    data["kernel"] = uname_r or "unknown"
    data["arch"] = uname_m or "x86_64"
    data["uptime"] = uptime_str or "unknown"

    # 4. CPU Specs via lscpu
    _, lscpu_out, _ = run_cmd(["lscpu"], host=host, user=user, port=port, key=key)
    cpu_info = {
        "model": "Generic CPU",
        "vendor": "Unknown",
        "sockets": 1,
        "cores_per_socket": 1,
        "threads_per_core": 1,
        "total_cores": 1,
        "flags": "",
        "vt_enabled": False,
        "kvm_present": False,
    }
    for line in lscpu_out.splitlines():
        if line.startswith("Model name:"):
            cpu_info["model"] = re.sub(r"\s+", " ", line.split(":", 1)[1].strip())
        elif line.startswith("Vendor ID:"):
            cpu_info["vendor"] = line.split(":", 1)[1].strip()
        elif line.startswith("Socket(s):"):
            val = line.split(":", 1)[1].strip()
            if val.isdigit():
                cpu_info["sockets"] = int(val)
        elif line.startswith("Core(s) per socket:"):
            val = line.split(":", 1)[1].strip()
            if val.isdigit():
                cpu_info["cores_per_socket"] = int(val)
        elif line.startswith("Thread(s) per core:"):
            val = line.split(":", 1)[1].strip()
            if val.isdigit():
                cpu_info["threads_per_core"] = int(val)
        elif line.startswith("CPU(s):"):
            val = line.split(":", 1)[1].strip()
            if val.isdigit():
                cpu_info["total_cores"] = int(val)
        elif line.startswith("Flags:"):
            cpu_info["flags"] = line.split(":", 1)[1].strip()

    # Hardware VT check
    vt_flags = []
    if "vmx" in cpu_info["flags"]:
        vt_flags.append("vmx")
    if "svm" in cpu_info["flags"]:
        vt_flags.append("svm")
    cpu_info["vt_flags_str"] = " ".join(vt_flags) if vt_flags else "none"
    cpu_info["vt_enabled"] = len(vt_flags) > 0

    rc_kvm, _, _ = run_cmd(["test", "-e", "/dev/kvm"], host=host, user=user, port=port, key=key)
    cpu_info["kvm_present"] = (rc_kvm == 0)
    data["cpu"] = cpu_info

    # 5. Memory via free -m
    _, free_out, _ = run_cmd(["free", "-m"], host=host, user=user, port=port, key=key)
    mem_info = {"total_mib": 0, "total_gb": 0, "free_mib": 0, "available_mib": 0}
    for line in free_out.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                total_m = int(parts[1])
                mem_info["total_mib"] = total_m
                mem_info["total_gb"] = round(total_m / 1024)
                mem_info["free_mib"] = int(parts[3])
                mem_info["available_mib"] = int(parts[6])
            elif len(parts) >= 4:
                total_m = int(parts[1])
                mem_info["total_mib"] = total_m
                mem_info["total_gb"] = round(total_m / 1024)
                mem_info["free_mib"] = int(parts[3])
                mem_info["available_mib"] = int(parts[3])
            break
    data["memory"] = mem_info

    # 6. Storage root filesystem
    _, df_out, _ = run_cmd(["df", "-B1G", "/"], host=host, user=user, port=port, key=key)
    storage_root = {"device": "/dev/root", "mount": "/", "total_gb": 0, "available_gb": 0, "percent_used": "0%"}
    lines = df_out.splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5:
            storage_root["device"] = parts[0]
            val_total = re.sub(r"[^0-9]", "", parts[1])
            storage_root["total_gb"] = int(val_total) if val_total.isdigit() else 0
            val_avail = re.sub(r"[^0-9]", "", parts[3])
            storage_root["available_gb"] = int(val_avail) if val_avail.isdigit() else 0
            storage_root["percent_used"] = parts[4]
    data["storage_root"] = storage_root

    # 7. Libvirt Daemon & Version
    rc_virsh, virsh_ver, _ = run_cmd(["sudo", "virsh", "version"], host=host, user=user, port=port, key=key)
    libvirt_info = {
        "status": "active" if rc_virsh == 0 else "inactive",
        "type": "kvm",
        "libvirt_version": "unknown",
        "qemu_version": "unknown",
    }
    for line in virsh_ver.splitlines():
        if "libvirt" in line and "using" in line.lower():
            libvirt_info["libvirt_version"] = line.split(":", 1)[-1].strip()
        elif "library" in line.lower() and "libvirt" in line.lower():
            libvirt_info["libvirt_version"] = line.split(":", 1)[-1].strip()
        elif "hypervisor" in line.lower() and "qemu" in line.lower():
            libvirt_info["qemu_version"] = line.split(":", 1)[-1].strip()
    data["libvirt"] = libvirt_info

    # 8. Storage Pools
    _, pool_out, _ = run_cmd(["sudo", "virsh", "pool-list", "--all"], host=host, user=user, port=port, key=key)
    pools = []
    pool_lines = pool_out.splitlines()
    if len(pool_lines) > 2:
        for line in pool_lines[2:]:
            parts = line.split()
            if len(parts) >= 2:
                pname = parts[0]
                pstate = parts[1]
                # inspect pool path
                _, px_out, _ = run_cmd(["sudo", "virsh", "pool-dumpxml", pname], host=host, user=user, port=port, key=key)
                ppath = "/var/lib/libvirt/images"
                path_match = re.search(r"<path>([^<]+)</path>", px_out)
                if path_match:
                    ppath = path_match.group(1)
                pools.append({"name": pname, "state": pstate, "path": ppath})
    if not pools:
        pools.append({"name": "default", "state": "active", "path": "/var/lib/libvirt/images"})
    data["pools"] = pools

    # 9. Libvirt Networks & DHCP
    _, net_list, _ = run_cmd(["sudo", "virsh", "net-list", "--all"], host=host, user=user, port=port, key=key)
    networks = []
    net_names = []
    for line in net_list.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 2:
            net_names.append(parts[0])

    if not net_names:
        net_names = ["default"]

    static_leases_by_mac = {}
    for net_name in net_names:
        _, net_xml, _ = run_cmd(["sudo", "virsh", "net-dumpxml", net_name], host=host, user=user, port=port, key=key)
        net_obj = {
            "name": net_name,
            "bridge": "virbr0",
            "ip": "192.168.122.1",
            "netmask": "255.255.255.0",
            "dhcp_range_start": "",
            "dhcp_range_end": "",
            "static_hosts": [],
        }
        b_m = re.search(r"<bridge\s+name='([^']+)'", net_xml)
        if b_m:
            net_obj["bridge"] = b_m.group(1)
        ip_m = re.search(r"<ip\s+address='([^']+)'\s+netmask='([^']+)'", net_xml)
        if ip_m:
            net_obj["ip"] = ip_m.group(1)
            net_obj["netmask"] = ip_m.group(2)
        r_m = re.search(r"<range\s+start='([^']+)'\s+end='([^']+)'", net_xml)
        if r_m:
            net_obj["dhcp_range_start"] = r_m.group(1)
            net_obj["dhcp_range_end"] = r_m.group(2)
        for h_m in re.finditer(r"<host\s+mac='([^']+)'(?:\s+name='([^']*)')?\s+ip='([^']+)'", net_xml):
            mac = h_m.group(1).lower()
            name = h_m.group(2) or ""
            ip = h_m.group(3)
            net_obj["static_hosts"].append({"mac": mac, "name": name, "ip": ip})
            static_leases_by_mac[mac] = {"ip": ip, "name": name}
        networks.append(net_obj)

    # Dynamic DHCP leases
    dynamic_leases_by_mac = {}
    for net_name in net_names:
        _, leases_out, _ = run_cmd(["sudo", "virsh", "net-dhcp-leases", net_name], host=host, user=user, port=port, key=key)
        for line in leases_out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and ":" in parts[2]:
                mac = parts[2].lower()
                ip_val = parts[4].split("/")[0]
                host_name = parts[5] if len(parts) > 5 else ""
                dynamic_leases_by_mac[mac] = {"ip": ip_val, "name": host_name}

    data["networks"] = networks
    data["static_leases"] = static_leases_by_mac
    data["dynamic_leases"] = dynamic_leases_by_mac

    # 10. List All Domains
    _, dom_list, _ = run_cmd(["sudo", "virsh", "list", "--all"], host=host, user=user, port=port, key=key)
    domains = []
    lines = dom_list.splitlines()
    if len(lines) > 2:
        for line in lines[2:]:
            parts = line.split()
            if len(parts) >= 3:
                dom_id = parts[0]
                dom_name = parts[1]
                dom_state = " ".join(parts[2:])
                domains.append({"id": dom_id, "name": dom_name, "state": dom_state})
    data["domains"] = domains

    return data


def derive_vm_blueprint(vm_name: str, host: str, inventory_root: Path) -> dict:
    """Extracts or derives project and provisioning blueprint for a specific VM."""
    existing_vm_yaml = inventory_root / host / vm_name / "vm.yaml"
    if existing_vm_yaml.exists() and yaml is not None:
        try:
            parsed = yaml.safe_load(existing_vm_yaml.read_text(encoding="utf-8"))
            bp = parsed.get("vm", {}).get("blueprint", {})
            if bp and bp.get("project_name"):
                return {
                    "project_name": bp.get("project_name"),
                    "primary_user": bp.get("primary_user", "devbox"),
                    "secondary_user": bp.get("secondary_user", "mother"),
                    "ssh_key_path": bp.get("ssh_key_path", f"~/.ssh/{vm_name}_ed25519"),
                    "app_internal_port": str(bp.get("app_internal_port", 3000)),
                    "proxy_external_port": str(bp.get("proxy_external_port", 3001)),
                    "proxy_socket_service": bp.get("proxy_socket_service", f"/etc/systemd/system/{vm_name}-proxy.socket"),
                    "remote_code_dir": bp.get("remote_code_dir", f"~/{bp.get('project_name', vm_name)}/"),
                    "guest_os": bp.get("guest_os", "Ubuntu 24.04 LTS Minimal"),
                    "guest_os_details": bp.get("guest_os_details", "Pristine Noble Numbat cloud image"),
                    "cpu_mode": bp.get("cpu_mode", "host-passthrough"),
                    "balloon_driver": bp.get("balloon_driver", "virtio-balloon"),
                    "disk_size_gib": str(bp.get("disk_size_gib", 50)),
                    "disk_format": bp.get("disk_format", "qcow2"),
                    "uefi_firmware": bp.get("uefi_firmware", "OVMF_CODE_4M.ms.fd"),
                    "snapshot_base_name": bp.get("snapshot_base_name", "snapshot-base-clean"),
                    "snapshot_base_desc": bp.get("snapshot_base_desc", "Pristine base setup with Docker and Python"),
                }
        except Exception:
            pass

    clean_name = re.sub(r"-(?:vm|worker|node)(?:-\d+)?$", "", vm_name, flags=re.IGNORECASE)
    parts = clean_name.replace("_", "-").split("-")
    project_display = "".join(p.capitalize() for p in parts) if parts else vm_name.capitalize()

    if "devbox" in vm_name.lower():
        proj_name = "DevBox"
        prim_user = "devbox"
        int_port = "3000"
        ext_port = "3001"
    elif "appsurfer" in vm_name.lower():
        proj_name = "AppSurfer"
        prim_user = "appsurfer"
        int_port = "8000"
        ext_port = "8001"
    elif "nhr" in vm_name.lower():
        proj_name = f"NHR-{parts[-1].capitalize() if len(parts) > 1 else 'Worker'}"
        prim_user = "nhr-worker"
        int_port = "8080"
        ext_port = "8081"
    elif "rugby" in vm_name.lower():
        proj_name = "RugbyAssistant"
        prim_user = "rugby"
        int_port = "5000"
        ext_port = "5001"
    else:
        proj_name = project_display
        prim_user = clean_name.lower().replace("-", "")
        int_port = "3000"
        ext_port = "3001"

    proj_slug = re.sub(r"[^a-z0-9]+", "-", proj_name.lower()).strip("-")

    return {
        "project_name": proj_name,
        "primary_user": prim_user,
        "secondary_user": "mother",
        "ssh_key_path": f"~/.ssh/{prim_user}_vm_ed25519",
        "app_internal_port": int_port,
        "proxy_external_port": ext_port,
        "proxy_socket_service": f"/etc/systemd/system/{proj_slug}-proxy.socket",
        "remote_code_dir": f"~/{proj_name}/",
        "guest_os": "Ubuntu 24.04 LTS Minimal",
        "guest_os_details": "Pristine Noble Numbat cloud image",
        "cpu_mode": "host-passthrough",
        "balloon_driver": "virtio-balloon",
        "disk_size_gib": "50",
        "disk_format": "qcow2",
        "uefi_firmware": "OVMF_CODE_4M.ms.fd",
        "snapshot_base_name": "snapshot-base-clean",
        "snapshot_base_desc": "Pristine base setup with Docker and Python",
    }


def probe_vm_details(
    vm_name: str,
    host_data: dict,
    user: str | None = None,
    port: int = 22,
    key: str | None = None,
) -> dict:
    """Probes details for a specific virtual machine domain on the hypervisor."""
    host = host_data["host"]
    vm_info = {
        "name": vm_name,
        "host": host,
        "id": "-",
        "state": "shut off",
        "uuid": "",
        "os_type": "hvm",
        "vcpu": 2,
        "memory_mib": 2048,
        "memory_gb": 2,
        "disks": [],
        "interfaces": [],
        "apparmor_profile": "unconfined",
        "guest_agent_present": False,
        "blueprint": derive_vm_blueprint(vm_name, host, DEFAULT_INVENTORY_ROOT),
    }

    # Find matching domain in host_data domains
    for d in host_data.get("domains", []):
        if d["name"] == vm_name:
            vm_info["id"] = d["id"]
            vm_info["state"] = d["state"]
            break

    # Get dominfo
    _, dominfo_out, _ = run_cmd(["sudo", "virsh", "dominfo", vm_name], host=host, user=user, port=port, key=key)
    for line in dominfo_out.splitlines():
        if line.startswith("UUID:"):
            vm_info["uuid"] = line.split(":", 1)[1].strip()
        elif line.startswith("State:"):
            vm_info["state"] = line.split(":", 1)[1].strip()
        elif line.startswith("CPU(s):"):
            val = line.split(":", 1)[1].strip()
            if val.isdigit():
                vm_info["vcpu"] = int(val)
        elif line.startswith("Max memory:"):
            val = line.split(":", 1)[1].strip().split()[0]
            if val.isdigit():
                # Max memory is reported in KiB
                vm_info["memory_mib"] = int(val) // 1024
                vm_info["memory_gb"] = round(vm_info["memory_mib"] / 1024)

    # Get dumpxml
    _, xml_out, _ = run_cmd(["sudo", "virsh", "dumpxml", vm_name], host=host, user=user, port=port, key=key)
    if xml_out:
        try:
            root = ET.fromstring(xml_out)
            # UUID
            uuid_elem = root.find("uuid")
            if uuid_elem is not None and uuid_elem.text:
                vm_info["uuid"] = uuid_elem.text.strip()

            # OS type
            os_elem = root.find("os/type")
            if os_elem is not None and os_elem.text:
                vm_info["os_type"] = os_elem.text.strip()

            # vCPU
            vcpu_elem = root.find("vcpu")
            if vcpu_elem is not None and vcpu_elem.text and vcpu_elem.text.strip().isdigit():
                vm_info["vcpu"] = int(vcpu_elem.text.strip())

            # Memory
            mem_elem = root.find("memory")
            if mem_elem is not None and mem_elem.text and mem_elem.text.strip().isdigit():
                unit = mem_elem.attrib.get("unit", "KiB")
                mem_kib = int(mem_elem.text.strip())
                if unit == "bytes":
                    mem_kib //= 1024
                elif unit == "MiB":
                    mem_kib *= 1024
                elif unit == "GiB":
                    mem_kib *= 1024 * 1024
                vm_info["memory_mib"] = mem_kib // 1024
                vm_info["memory_gb"] = round(vm_info["memory_mib"] / 1024)

            # Disks
            disks = []
            for disk in root.findall(".//devices/disk"):
                dev_type = disk.attrib.get("device", "disk")
                target = disk.find("target")
                dev_name = target.attrib.get("dev", "unknown") if target is not None else "unknown"
                bus = target.attrib.get("bus", "virtio") if target is not None else "virtio"

                source = disk.find("source")
                source_file = ""
                if source is not None:
                    source_file = source.attrib.get("file") or source.attrib.get("dev") or ""

                driver = disk.find("driver")
                format_type = driver.attrib.get("type", "qcow2") if driver is not None else "qcow2"

                disks.append({
                    "device": dev_type,
                    "target": dev_name,
                    "bus": bus,
                    "format": format_type,
                    "source": source_file or "none",
                })
            vm_info["disks"] = disks

            # Interfaces
            interfaces = []
            for iface in root.findall(".//devices/interface"):
                iface_type = iface.attrib.get("type", "network")
                mac_elem = iface.find("mac")
                mac = mac_elem.attrib.get("address", "").lower() if mac_elem is not None else ""

                src_name = ""
                if iface_type == "network":
                    src = iface.find("source")
                    if src is not None:
                        src_name = src.attrib.get("network", "default")
                elif iface_type == "bridge":
                    src = iface.find("source")
                    if src is not None:
                        src_name = src.attrib.get("bridge", "virbr0")

                model_elem = iface.find("model")
                model = model_elem.attrib.get("type", "virtio") if model_elem is not None else "virtio"

                # Resolve IP from host static or dynamic leases
                ip_addr = "DHCP / Unassigned"
                if mac in host_data.get("static_leases", {}):
                    ip_addr = host_data["static_leases"][mac]["ip"]
                elif mac in host_data.get("dynamic_leases", {}):
                    ip_addr = host_data["dynamic_leases"][mac]["ip"]

                interfaces.append({
                    "type": iface_type,
                    "source": src_name,
                    "mac": mac,
                    "model": model,
                    "ip": ip_addr,
                })
            vm_info["interfaces"] = interfaces

            # Guest agent check
            ga_found = False
            for channel in root.findall(".//devices/channel"):
                t = channel.find("target")
                if t is not None and t.attrib.get("name") == "org.qemu.guest_agent.0":
                    ga_found = True
                    break
            vm_info["guest_agent_present"] = ga_found

            # AppArmor profile check
            seclabel = root.find("seclabel")
            if seclabel is not None:
                model_attr = seclabel.attrib.get("model", "")
                label_elem = seclabel.find("label")
                if label_elem is not None and label_elem.text:
                    vm_info["apparmor_profile"] = label_elem.text.strip()
                elif model_attr:
                    vm_info["apparmor_profile"] = f"{model_attr} (managed)"

        except Exception as e:
            print(f"    [!] Error parsing XML for VM '{vm_name}': {e}")

    return vm_info


def render_vmhost_yaml(host_data: dict, vm_records: list[dict], template_path: Path) -> str:
    """Renders the vmhost.yaml document from the template."""
    template_content = template_path.read_text(encoding="utf-8")
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Render storage pools YAML
    pools_lines = []
    for p in host_data.get("pools", []):
        pools_lines.append(f"        - name: {p['name']}")
        pools_lines.append(f"          path: {p['path']}")
        pools_lines.append(f"          state: {p['state']}")
    pools_yaml = "\n".join(pools_lines) if pools_lines else "        []"

    # Render networks YAML
    nets_lines = []
    for n in host_data.get("networks", []):
        nets_lines.append(f"        - name: {n['name']}")
        nets_lines.append(f"          bridge: {n['bridge']}")
        nets_lines.append(f"          ip: {n['ip']}")
        nets_lines.append(f"          netmask: {n['netmask']}")
        if n.get("dhcp_range_start") and n.get("dhcp_range_end"):
            nets_lines.append(f"          dhcp_range: \"{n['dhcp_range_start']} - {n['dhcp_range_end']}\"")
        if n.get("static_hosts"):
            nets_lines.append(f"          static_reservations:")
            for sh in n["static_hosts"]:
                nets_lines.append(f"            - name: {sh['name']}")
                nets_lines.append(f"              mac: \"{sh['mac']}\"")
                nets_lines.append(f"              ip: {sh['ip']}")
    nets_yaml = "\n".join(nets_lines) if nets_lines else "        []"

    # Render VM list YAML
    vms_lines = []
    running_count = 0
    for v in vm_records:
        if "running" in v["state"].lower():
            running_count += 1
        vms_lines.append(f"        - name: {v['name']}")
        vms_lines.append(f"          state: \"{v['state']}\"")
        vms_lines.append(f"          vcpu: {v['vcpu']}")
        vms_lines.append(f"          memory_mib: {v['memory_mib']}")
        vms_lines.append(f"          inventory_path: \"inventory/{host_data['host']}/{v['name']}/vm.yaml\"")
    vms_yaml = "\n".join(vms_lines) if vms_lines else "        []"

    conn_type = "ssh" if host_data["is_remote"] else "local"
    ssh_host = host_data["host"] if host_data["is_remote"] else "localhost"
    ssh_port = host_data["port"]
    ssh_user = host_data["user"] or os.environ.get("USER", "root")

    replacements = {
        "VMHOST_NAME": host_data["host"],
        "VMHOST_ALIAS": host_data["hostname"],
        "GENERATED_AT": now_iso,
        "CONNECTION_TYPE": conn_type,
        "SSH_HOST": ssh_host,
        "SSH_PORT": str(ssh_port),
        "SSH_USER": ssh_user,
        "HOSTNAME": host_data["hostname"],
        "FQDN": host_data["fqdn"],
        "OS_DISTRO": host_data["os"]["id"],
        "OS_VERSION": host_data["os"]["version_id"],
        "OS_PRETTY_NAME": host_data["os"]["pretty_name"],
        "KERNEL_VERSION": host_data["kernel"],
        "ARCHITECTURE": host_data["arch"],
        "UPTIME": host_data["uptime"],
        "CPU_MODEL": host_data["cpu"]["model"],
        "CPU_VENDOR": host_data["cpu"]["vendor"],
        "CPU_SOCKETS": str(host_data["cpu"]["sockets"]),
        "CPU_CORES_PER_SOCKET": str(host_data["cpu"]["cores_per_socket"]),
        "CPU_THREADS_PER_CORE": str(host_data["cpu"]["threads_per_core"]),
        "CPU_TOTAL_CORES": str(host_data["cpu"]["total_cores"]),
        "HARDWARE_VT_ENABLED": "true" if host_data["cpu"]["vt_enabled"] else "false",
        "CPU_VT_FLAGS": host_data["cpu"]["vt_flags_str"],
        "KVM_DEVICE_PRESENT": "true" if host_data["cpu"]["kvm_present"] else "false",
        "MEMORY_TOTAL_MIB": str(host_data["memory"]["total_mib"]),
        "MEMORY_TOTAL_GB": str(host_data["memory"]["total_gb"]),
        "MEMORY_FREE_MIB": str(host_data["memory"]["free_mib"]),
        "MEMORY_AVAILABLE_MIB": str(host_data["memory"]["available_mib"]),
        "ROOT_FS_DEVICE": host_data["storage_root"]["device"],
        "ROOT_FS_MOUNT": host_data["storage_root"]["mount"],
        "ROOT_FS_TOTAL_GB": str(host_data["storage_root"]["total_gb"]),
        "ROOT_FS_AVAILABLE_GB": str(host_data["storage_root"]["available_gb"]),
        "ROOT_FS_PERCENT_USED": host_data["storage_root"]["percent_used"],
        "STORAGE_POOLS_YAML": pools_yaml,
        "HYPERVISOR_TYPE": host_data["libvirt"]["type"],
        "LIBVIRT_DAEMON_STATUS": host_data["libvirt"]["status"],
        "LIBVIRT_VERSION": host_data["libvirt"]["libvirt_version"],
        "QEMU_VERSION": host_data["libvirt"]["qemu_version"],
        "NETWORKS_YAML": nets_yaml,
        "VM_COUNT": str(len(vm_records)),
        "VM_RUNNING_COUNT": str(running_count),
        "VM_LIST_YAML": vms_yaml,
        "INVENTORY_STATUS": "synced",
    }

    rendered = template_content
    for key, val in replacements.items():
        pattern = r"\{\{\s*" + re.escape(key) + r"\s*\}\}"
        rendered = re.sub(pattern, val, rendered)

    return rendered


def render_vm_yaml(vm_data: dict, template_path: Path) -> str:
    """Renders the vm.yaml document from the template."""
    template_content = template_path.read_text(encoding="utf-8")
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Render Disks YAML
    disks_lines = []
    for d in vm_data.get("disks", []):
        disks_lines.append(f"        - device: {d['device']}")
        disks_lines.append(f"          target: {d['target']}")
        disks_lines.append(f"          bus: {d['bus']}")
        disks_lines.append(f"          format: {d['format']}")
        disks_lines.append(f"          source: \"{d['source']}\"")
    disks_yaml = "\n".join(disks_lines) if disks_lines else "        []"

    # Render Interfaces YAML
    ifaces_lines = []
    for i in vm_data.get("interfaces", []):
        ifaces_lines.append(f"        - type: {i['type']}")
        ifaces_lines.append(f"          source: {i['source']}")
        ifaces_lines.append(f"          mac: \"{i['mac']}\"")
        ifaces_lines.append(f"          ip: \"{i['ip']}\"")
        ifaces_lines.append(f"          model: {i['model']}")
    ifaces_yaml = "\n".join(ifaces_lines) if ifaces_lines else "        []"

    replacements = {
        "VMHOST_NAME": vm_data["host"],
        "VM_NAME": vm_data["name"],
        "GENERATED_AT": now_iso,
        "VM_STATE": vm_data["state"],
        "VM_ID": str(vm_data["id"]),
        "VM_UUID": vm_data["uuid"],
        "VM_OS_TYPE": vm_data["os_type"],
        "VM_VCPU": str(vm_data["vcpu"]),
        "VM_MEMORY_MIB": str(vm_data["memory_mib"]),
        "VM_MEMORY_GB": str(vm_data["memory_gb"]),
        "VM_DISKS_YAML": disks_yaml,
        "VM_INTERFACES_YAML": ifaces_yaml,
        "VM_APPARMOR_PROFILE": vm_data["apparmor_profile"],
        "VM_GUEST_AGENT_PRESENT": "true" if vm_data["guest_agent_present"] else "false",
    }

    bp = vm_data.get("blueprint", {})
    replacements.update({
        "BLUEPRINT_PROJECT_NAME": bp.get("project_name", "DevBox"),
        "BLUEPRINT_PRIMARY_USER": bp.get("primary_user", "devbox"),
        "BLUEPRINT_SECONDARY_USER": bp.get("secondary_user", "mother"),
        "BLUEPRINT_SSH_KEY_PATH": bp.get("ssh_key_path", "~/.ssh/devbox_vm_ed25519"),
        "BLUEPRINT_APP_INTERNAL_PORT": str(bp.get("app_internal_port", 3000)),
        "BLUEPRINT_PROXY_EXTERNAL_PORT": str(bp.get("proxy_external_port", 3001)),
        "BLUEPRINT_PROXY_SOCKET_SERVICE": bp.get("proxy_socket_service", "/etc/systemd/system/devbox-proxy.socket"),
        "BLUEPRINT_REMOTE_CODE_DIR": bp.get("remote_code_dir", "~/DevBox/"),
        "BLUEPRINT_GUEST_OS": bp.get("guest_os", "Ubuntu 24.04 LTS Minimal"),
        "BLUEPRINT_GUEST_OS_DETAILS": bp.get("guest_os_details", "Pristine Noble Numbat cloud image"),
        "BLUEPRINT_CPU_MODE": bp.get("cpu_mode", "host-passthrough"),
        "BLUEPRINT_BALLOON_DRIVER": bp.get("balloon_driver", "virtio-balloon"),
        "BLUEPRINT_DISK_SIZE_GIB": str(bp.get("disk_size_gib", 50)),
        "BLUEPRINT_DISK_FORMAT": bp.get("disk_format", "qcow2"),
        "BLUEPRINT_UEFI_FIRMWARE": bp.get("uefi_firmware", "OVMF_CODE_4M.ms.fd"),
        "BLUEPRINT_SNAPSHOT_BASE_NAME": bp.get("snapshot_base_name", "snapshot-base-clean"),
        "BLUEPRINT_SNAPSHOT_BASE_DESC": bp.get("snapshot_base_desc", "Pristine base setup with Docker and Python"),
        "BLUEPRINT_REPOSITORIES_YAML": render_repositories_yaml(vm_data, bp),
    })

    rendered = template_content
    for key, val in replacements.items():
        pattern = r"\{\{\s*" + re.escape(key) + r"\s*\}\}"
        rendered = re.sub(pattern, val, rendered)

    return rendered


def render_vm_runbook(vm_data: dict, host_data: dict, template_path: Path) -> str:
    """Renders VM.md for an individual VM from template."""
    template_content = template_path.read_text(encoding="utf-8")
    bp = vm_data.get("blueprint", {})

    ifaces = vm_data.get("interfaces", [])
    primary_ip = ifaces[0].get("ip", "DHCP / Unassigned") if ifaces else "DHCP / Unassigned"
    primary_mac = ifaces[0].get("mac", "52:54:00:xx:xx:xx") if ifaces else "52:54:00:xx:xx:xx"
    bridge = ifaces[0].get("source", "virbr0") if ifaces else "virbr0"

    dev_host = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip() or "workstation"

    replacements = {
        "PROJECT_NAME": bp.get("project_name", vm_data["name"]),
        "VM_NAME": vm_data["name"],
        "HYPERVISOR_HOST": host_data["host"],
        "HYPERVISOR_HOST_TITLE": host_data["host"].capitalize(),
        "HYPERVISOR_OS": host_data["os"]["pretty_name"],
        "HYPERVISOR_SPECS": f"{host_data['cpu']['total_cores']} Cores {host_data['cpu']['model']} | {host_data['memory']['total_gb']} GB RAM | KVM / libvirt",
        "HYPERVISOR_LAN_IP": host_data.get("hostname", host_data["host"]),
        "DEV_HOST": dev_host,
        "DEV_HOST_TITLE": dev_host.capitalize(),
        "GUEST_OS": bp.get("guest_os", "Ubuntu 24.04 LTS Minimal"),
        "GUEST_OS_DETAILS": bp.get("guest_os_details", "Pristine Noble Numbat cloud image"),
        "VCPU_CORES": str(vm_data["vcpu"]),
        "CPU_MODE": bp.get("cpu_mode", "host-passthrough"),
        "RAM_MIB": str(vm_data["memory_mib"]),
        "RAM_GB": str(vm_data["memory_gb"]),
        "BALLOON_DRIVER": bp.get("balloon_driver", "virtio-balloon"),
        "DISK_SIZE_GIB": str(bp.get("disk_size_gib", 50)),
        "DISK_FORMAT": bp.get("disk_format", "qcow2"),
        "UEFI_FIRMWARE": bp.get("uefi_firmware", "OVMF_CODE_4M.ms.fd"),
        "VM_IP": primary_ip,
        "SUBNET_CIDR": "/24",
        "VIRT_BRIDGE": bridge,
        "LIBVIRT_NETWORK": "default",
        "VM_MAC": primary_mac,
        "VM_PRIMARY_USER": bp.get("primary_user", "devbox"),
        "VM_SECONDARY_USER": bp.get("secondary_user", "mother"),
        "SSH_KEY_PATH": bp.get("ssh_key_path", f"~/.ssh/{bp.get('primary_user', 'devbox')}_vm_ed25519"),
        "APP_INTERNAL_PORT": str(bp.get("app_internal_port", 3000)),
        "PROXY_EXTERNAL_PORT": str(bp.get("proxy_external_port", 3001)),
        "PROXY_SOCKET_SERVICE": bp.get("proxy_socket_service", f"/etc/systemd/system/{vm_data['name']}-proxy.socket"),
        "REMOTE_CODE_DIR": bp.get("remote_code_dir", f"~/{bp.get('project_name', vm_data['name'])}/"),
        "SNAPSHOT_BASE_NAME": bp.get("snapshot_base_name", "snapshot-base-clean"),
        "SNAPSHOT_BASE_DESC": bp.get("snapshot_base_desc", "Pristine base setup with Docker and Python"),
    }

    rendered = template_content
    for key, val in replacements.items():
        pattern = r"\{\{\s*" + re.escape(key) + r"\s*\}\}"
        rendered = re.sub(pattern, val, rendered)

    return rendered


def validate_yaml_syntax(yaml_str: str, file_desc: str) -> None:
    """Verifies that the generated YAML string is syntactically valid."""
    if yaml is not None:
        try:
            yaml.safe_load(yaml_str)
        except Exception as e:
            raise ValueError(f"Generated YAML for {file_desc} failed validation: {e}")


def add_vmhost_to_inventory(
    host: str,
    user: str | None = None,
    port: int = 22,
    key: str | None = None,
    target_vm: str | None = None,
    inventory_root: Path = DEFAULT_INVENTORY_ROOT,
    dry_run: bool = False,
) -> dict:
    """Discovers vmhost and VMs, populating inventory/{vmhost}/vmhost.yaml and inventory/{vmhost}/{vm}/."""
    print(f"[*] Probing hypervisor host '{host}'...")
    host_data = probe_host(host, user=user, port=port, key=key)
    print(f"    [✓] Host connected: {host_data['hostname']} ({host_data['os']['pretty_name']}, {host_data['kernel']})")
    print(f"    [✓] Hardware: {host_data['cpu']['total_cores']} cores ({host_data['cpu']['model']}), {host_data['memory']['total_gb']} GB RAM")
    print(f"    [✓] Hypervisor: {host_data['libvirt']['type']} ({host_data['libvirt']['status']}), {len(host_data['domains'])} domains detected")

    # Determine which VMs to inventory
    vm_names_to_probe = []
    if target_vm:
        vm_names_to_probe = [target_vm]
    else:
        vm_names_to_probe = [d["name"] for d in host_data.get("domains", [])]

    # Probe VM records
    vm_records = []
    for vm_name in vm_names_to_probe:
        print(f"[*] Documenting VM '{vm_name}' on host '{host}'...")
        vm_details = probe_vm_details(vm_name, host_data, user=user, port=port, key=key)
        vm_records.append(vm_details)
        print(f"    [✓] VM '{vm_name}': {vm_details['state']} | {vm_details['vcpu']} vCPU | {vm_details['memory_mib']} MiB RAM | {len(vm_details['disks'])} disk(s)")

    # Render vmhost.yaml
    print(f"[*] Rendering host inventory using template '{VMHOST_TEMPLATE.name}'...")
    vmhost_yaml_content = render_vmhost_yaml(host_data, vm_records, VMHOST_TEMPLATE)
    validate_yaml_syntax(vmhost_yaml_content, f"host '{host}'")

    # Define paths
    host_dir = inventory_root / host
    vmhost_file = host_dir / "vmhost.yaml"

    if dry_run:
        print("\n[DRY-RUN] Would create directory:", host_dir)
        print("[DRY-RUN] Would write file:", vmhost_file)
        for v in vm_records:
            v_dir = host_dir / v["name"]
            v_file = v_dir / "vm.yaml"
            print(f"[DRY-RUN] Would create VM directory: {v_dir}")
            print(f"[DRY-RUN] Would write VM record: {v_file}")
        return {"host_data": host_data, "vm_records": vm_records}

    # Create host directory and write vmhost.yaml
    host_dir.mkdir(parents=True, exist_ok=True)
    vmhost_file.write_text(vmhost_yaml_content, encoding="utf-8")
    print(f"    [✓] Written: {vmhost_file}")

    # Write each VM record into inventory/{vmhost}/{vm}/
    devbox_configure_template = REPO_ROOT / ".agents" / "skills" / "devbox_configure" / "template" / "VM.md.template"

    for v in vm_records:
        vm_dir = host_dir / v["name"]
        vm_dir.mkdir(parents=True, exist_ok=True)
        vm_yaml_file = vm_dir / "vm.yaml"
        vm_yaml_content = render_vm_yaml(v, VM_TEMPLATE)
        validate_yaml_syntax(vm_yaml_content, f"VM '{v['name']}'")
        vm_yaml_file.write_text(vm_yaml_content, encoding="utf-8")
        print(f"    [✓] Written: {vm_yaml_file}")

        # Render VM operational runbook directly from the SSOT template
        if devbox_configure_template.exists():
            vm_runbook_file = vm_dir / "VM.md"
            vm_runbook_content = render_vm_runbook(v, host_data, devbox_configure_template)
            vm_runbook_file.write_text(vm_runbook_content, encoding="utf-8")
            print(f"    [✓] Written: {vm_runbook_file}")

    return {
        "host_data": host_data,
        "vm_records": vm_records,
        "vmhost_file": vmhost_file,
        "inventory_root": inventory_root,
    }


def main():
    parser = argparse.ArgumentParser(
        description="DevBox Inventory: Probes a hypervisor host and documents it in inventory/{vmhost}/vmhost.yaml and inventory/{vmhost}/{vm}/"
    )
    parser.add_argument("host", nargs="?", default="barkley", help="Hypervisor hostname, IP, or SSH alias (default: barkley)")
    parser.add_argument("--user", "-u", default=None, help="SSH user (if remote)")
    parser.add_argument("--port", "-p", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--key", "-i", default=None, help="Path to SSH private key")
    parser.add_argument("--vm", help="Target a specific VM name on the host instead of all domains")
    parser.add_argument("--inventory-dir", type=Path, default=DEFAULT_INVENTORY_ROOT, help="Base inventory directory")
    parser.add_argument("--dry-run", action="store_true", help="Inspect and preview without writing files")

    args = parser.parse_args()

    try:
        res = add_vmhost_to_inventory(
            host=args.host,
            user=args.user,
            port=args.port,
            key=args.key,
            target_vm=args.vm,
            inventory_root=args.inventory_dir,
            dry_run=args.dry_run,
        )

        print("\n" + "=" * 60)
        print(" [✓] DevBox Host Inventory Successfully Generated!")
        print("=" * 60)
        print(f" Host Record : inventory/{args.host}/vmhost.yaml")
        print(f" Total VMs   : {len(res['vm_records'])}")
        for v in res["vm_records"]:
            print(f"   • {v['name']} ({v['state']}) -> inventory/{args.host}/{v['name']}/vm.yaml")
        print("=" * 60 + "\n")
        sys.exit(0)

    except Exception as e:
        print(f"\n[-] Error adding host '{args.host}' to inventory: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
