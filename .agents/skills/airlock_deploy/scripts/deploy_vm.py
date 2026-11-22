#!/usr/bin/env python3
"""
AirlockVM Virtual Machine Deployment Engine (deploy_vm.py)
Deploys, provisions, and manages dedicated execution VMs using inventory/{vmhost}/{vm}/vm.yaml
as the Single Source of Truth (SSOT), with VM.md as supplementary documentation.

Enforces two strict pre-flight gates:
  1. VM specification (vm.yaml) MUST exist in inventory/.
  2. verify_config.py MUST exit cleanly (code 0).
"""

import argparse
import base64
import getpass
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent


def find_repo_root() -> Path:
    """Finds the root repository directory walking upward."""
    if len(SKILL_DIR.parents) >= 3 and SKILL_DIR.parents[1].name == ".agents":
        return SKILL_DIR.parents[2].resolve()
    cwd = Path.cwd().resolve()
    for p in [cwd] + list(cwd.parents):
        if (p / ".git").exists() or (p / ".agents").exists():
            return p
    return cwd


REPO_ROOT = find_repo_root()
VERIFY_SCRIPT = REPO_ROOT / ".agents" / "skills" / "airlock_configure" / "scripts" / "verify_config.py"


def find_default_config(vm_name: str | None = None) -> tuple[Path | None, Path | None]:
    """Finds the default VM specification (vm.yaml as SSOT) and optional runbook (VM.md)."""
    inv_dir = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))
    if not inv_dir.exists():
        return None, None

    yaml_candidates = []
    if vm_name:
        yaml_candidates.extend(inv_dir.glob(f"*/{vm_name}/vm.yaml"))

    proj_slug = re.sub(r"[^a-z0-9]+", "-", REPO_ROOT.name.lower()).strip("-")
    yaml_candidates.extend(inv_dir.glob(f"*/{proj_slug}-vm/vm.yaml"))
    yaml_candidates.extend(sorted(inv_dir.glob("*/*/vm.yaml")))

    for yf in yaml_candidates:
        if yf.exists():
            runbook = yf.parent / "VM.md"
            return yf, (runbook if runbook.exists() else None)

    return None, None


def load_vm_config(config_path: Path) -> dict[str, str]:
    """Loads VM and hypervisor parameters from inventory/{vmhost}/{vm}/vm.yaml (SSOT) or fallback VM.md."""
    if not config_path or not config_path.exists():
        return {}

    # If markdown file is passed, fall back to legacy parse_runbook
    if config_path.suffix == ".md":
        return parse_runbook(config_path)

    text = config_path.read_text(encoding="utf-8")
    if yaml is not None:
        try:
            doc = yaml.safe_load(text) or {}
        except Exception as e:
            raise ValueError(f"Failed to parse YAML from {config_path}: {e}")
    else:
        raise RuntimeError("PyYAML ('yaml') module is required to parse vm.yaml")

    vm_data = doc.get("vm", {})
    bp = vm_data.get("blueprint", {})
    resources = vm_data.get("resources", {})
    interfaces = vm_data.get("network", {}).get("interfaces", [])
    primary_iface = interfaces[0] if interfaces else {}

    # Check parent directory for vmhost.yaml
    host_data = {}
    vmhost_yaml = config_path.parent.parent / "vmhost.yaml"
    if vmhost_yaml.exists() and yaml is not None:
        try:
            hdoc = yaml.safe_load(vmhost_yaml.read_text(encoding="utf-8")) or {}
            host_data = hdoc.get("vmhost", {})
        except Exception:
            pass

    hypervisor_host = vm_data.get("host") or host_data.get("name") or host_data.get("alias") or "localhost"

    params = {
        "VM_NAME": str(vm_data.get("name", "airlock-vm")),
        "HYPERVISOR_HOST": str(hypervisor_host),
        "HYPERVISOR_LAN_IP": str(host_data.get("system", {}).get("network", {}).get("lan_ip", "")),
        "GUEST_OS": str(bp.get("guest_os", "Ubuntu 24.04 LTS Minimal")),
        "VCPU_CORES": str(resources.get("vcpu", 4)),
        "CPU_MODE": str(bp.get("cpu_mode", "host-passthrough")),
        "RAM_MIB": str(resources.get("memory_mib", 4096)),
        "DISK_SIZE_GIB": str(bp.get("disk_size_gib", 50)),
        "DISK_FORMAT": str(bp.get("disk_format", "qcow2")),
        "UEFI_FIRMWARE": str(bp.get("uefi_firmware", "OVMF_CODE_4M.ms.fd")),
        "VM_IP": str(primary_iface.get("ip", "192.168.122.200")),
        "VM_MAC": str(primary_iface.get("mac", "52:54:00:12:34:56")),
        "VIRT_BRIDGE": str(primary_iface.get("source", "virbr0")),
        "LIBVIRT_NETWORK": "default",
        "VM_PRIMARY_USER": str(bp.get("primary_user", "airlock")),
        "VM_SECONDARY_USER": str(bp.get("secondary_user", "")),
        "SSH_KEY_PATH": str(bp.get("ssh_key_path", "~/.ssh/airlock_vm_ed25519")),
        "PROXY_SOCKET_SERVICE": str(bp.get("proxy_socket_service", "/etc/systemd/system/airlock-proxy.socket")),
        "PROXY_EXTERNAL_PORT": str(bp.get("proxy_external_port", 3001)),
        "APP_INTERNAL_PORT": str(bp.get("app_internal_port", 3000)),
        "SNAPSHOT_BASE_NAME": str(bp.get("snapshot_base_name", "snapshot-base-clean")),
        "SNAPSHOT_BASE_DESC": str(bp.get("snapshot_base_desc", "Pristine base setup with Docker and Python")),
        "REMOTE_CODE_DIR": str(bp.get("remote_code_dir", "~/AirlockVM/")),
        "REPOSITORIES": bp.get("repositories", []),
    }
    return params


def run_cmd_status(cmd: list[str], host: str | None = None) -> tuple[int, str, str]:
    """Executes a command locally or over SSH and returns (returncode, stdout, stderr)."""
    if host and host not in ("localhost", "127.0.0.1"):
        remote_cmd = shlex.join(cmd)
        full_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, remote_cmd]
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


# ==============================================================================
# Mandatory Pre-requisite Gates
# ==============================================================================

def check_prerequisites(
    config_path: Path | None = None,
    runbook_path: Path | None = None,
    verify_script: Path = VERIFY_SCRIPT,
) -> tuple[bool, str]:
    """Enforces the mandatory prerequisite gates before any deployment action.

    Gate 1: VM specification (inventory/{vmhost}/{vm}/vm.yaml) MUST exist as SSOT.
    Gate 2: Configuration must be syntactically valid with zero unrendered tags.
    """
    # Gate 1: Check document/config existence
    if config_path is None or not config_path.exists():
        return False, (
            f"[!] Pre-requisite Gate 1 FAILED: VM specification not found ({config_path}).\n\n"
            "An operational VM specification (vm.yaml) must exist in inventory/ before deployment.\n"
            "Remediation:\n"
            "  Run airlock_inventory to discover and document the hypervisor and VMs:\n"
            "    python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py <vmhost>\n"
            "  Or specify an existing configuration via --config <path> or --vm <vm_name>.\n"
        )

    # Gate 2: Check for unrendered template tags in config
    text = config_path.read_text(encoding="utf-8")
    leftovers = re.findall(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", text)
    if leftovers:
        return False, (
            f"[!] Pre-requisite Gate 2 FAILED: Unrendered template tags remain in {config_path}:\n"
            f"  {set(leftovers)}\n"
            "Remediation:\n"
            "  Fill in all template placeholders in vm.yaml before deploying."
        )

    # Validate YAML syntax
    if config_path.suffix in (".yaml", ".yml") and yaml is not None:
        try:
            yaml.safe_load(text)
        except Exception as e:
            return False, f"[!] Pre-requisite Gate 2 FAILED: Invalid YAML in {config_path}: {e}"

    # Verify supplementary runbook if present
    if runbook_path and runbook_path.exists() and verify_script.exists():
        res = subprocess.run(
            [sys.executable, str(verify_script), "--doc", str(runbook_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if res.returncode != 0:
            return False, (
                f"[!] Pre-requisite Gate 2 FAILED: Supplementary runbook verification reported errors.\n\n"
                f"Output from verify_config.py:\n"
                f"{res.stdout.strip()}\n"
                f"{res.stderr.strip()}"
            )

    return True, f"[✓] All pre-requisite gates passed ({config_path.relative_to(REPO_ROOT) if config_path.is_relative_to(REPO_ROOT) else config_path.name} verified as SSOT)."


# ==============================================================================
# Runbook Parameter Extraction
# ==============================================================================

def parse_runbook(doc_path: Path) -> dict[str, str]:
    """Extracts all virtual machine and hypervisor parameters from VM.md."""
    if not doc_path or not doc_path.exists():
        return {}

    content = doc_path.read_text(encoding="utf-8")

    def extract(pattern: str, default: str = "") -> str:
        m = re.search(pattern, content)
        return m.group(1).strip() if m else default

    params = {
        "VM_NAME": extract(r"\|\s*\*\*VM Name\*\*\s*\|\s*`([^`]+)`", "airlock-vm"),
        "HYPERVISOR_HOST": extract(r"\|\s*\*\*Target Hypervisor\*\*\s*\|\s*`([^`]+)`", "localhost"),
        "HYPERVISOR_LAN_IP": extract(r"\|\s*Physical LAN IP\s*\|\s*`([^`]+)`", ""),
        "GUEST_OS": extract(r"\|\s*\*\*Guest OS\*\*\s*\|\s*([^|]+)", "Ubuntu 24.04 LTS Minimal"),
        "VCPU_CORES": extract(r"\|\s*\*\*Virtual CPUs\*\*\s*\|\s*(\d+)", "4"),
        "CPU_MODE": extract(r"Mode:\s*`([^`]+)`", "host-passthrough"),
        "RAM_MIB": extract(r"\|\s*\*\*Memory \(RAM\)\*\*\s*\|\s*(\d+)\s*MiB", "4096"),
        "DISK_SIZE_GIB": extract(r"\|\s*\*\*Virtual Storage\*\*\s*\|\s*(\d+)\s*GiB", "50"),
        "DISK_FORMAT": extract(r"\|\s*\*\*Virtual Storage\*\*\s*\|[^\n]*`([a-z0-9]+)`", "qcow2"),
        "UEFI_FIRMWARE": extract(r"\|\s*\*\*Firmware\*\*\s*\|\s*([^\n|]+)", "UEFI (`OVMF_CODE_4M.ms.fd`)"),
        "VM_IP": extract(r"\|\s*\*\*Network IP\*\*\s*\|\s*`([^`/]+)", "192.168.122.200"),
        "VM_MAC": extract(r"\|\s*\*\*MAC Address\*\*\s*\|\s*`([0-9a-fA-F:]{17})`", "52:54:00:12:34:56"),
        "VIRT_BRIDGE": extract(r"virtual bridge\s*`([^`]+)`", "virbr0"),
        "LIBVIRT_NETWORK": extract(r"libvirt\s*`([^`]+)`\s*net", "default"),
        "VM_PRIMARY_USER": extract(r"\|\s*\*\*Primary User\*\*\s*\|\s*`([^`]+)`", "airlock"),
        "VM_SECONDARY_USER": extract(r"\|\s*\*\*Secondary User\*\*\s*\|\s*`([^`]+)`", getpass.getuser()),
        "SSH_KEY_PATH": extract(r"\*\*Private Key\*\*:\s*`([^`]+)`", "~/.ssh/airlock_vm_ed25519"),
        "PROXY_SOCKET_SERVICE": extract(r"\*\*Service Definition\*\*:\s*`([^`]+)`", "/etc/systemd/system/airlock-proxy.socket"),
        "PROXY_EXTERNAL_PORT": extract(r"received on port `(\d+)` to port", "3001"),
        "APP_INTERNAL_PORT": extract(r"to port `(\d+)` inside", "3000"),
        "SNAPSHOT_BASE_NAME": extract(r"snapshot-create-as[^\n]+--name\s+([a-zA-Z0-9_\-]+)", "snapshot-base-clean"),
        "SNAPSHOT_BASE_DESC": extract(r"--description\s+'([^']+)'", "Pristine base setup with Docker and Python"),
        "REMOTE_CODE_DIR": extract(r"rsync[^\n]+airlock-vm:([^\s\n]+)", "~/AirlockVM/"),
    }
    return params


# ==============================================================================
# Workstation Local Key & SSH Config Setup
# ==============================================================================

def ensure_workstation_ssh(params: dict, dry_run: bool = False) -> str:
    """Ensures local ED25519 keypair exists and ~/.ssh/config is configured."""
    key_path_str = os.path.expanduser(params["SSH_KEY_PATH"])
    key_path = Path(key_path_str)
    pub_key_path = key_path.with_suffix(".pub") if not key_path_str.endswith(".pub") else Path(key_path_str)

    print(f"[*] Checking local SSH credentials for '{params['VM_NAME']}'...")

    # 1. Generate SSH Key Pair if missing
    if not key_path.exists():
        print(f"    -> SSH private key not found at: {key_path}")
        if dry_run:
            print(f"    [PLAN] Generate ED25519 key: ssh-keygen -t ed25519 -f {key_path} -N '' -C '{params['VM_NAME']}'")
            fake_pub = f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI-MOCK-KEY {params['VM_NAME']}"
            return fake_pub
        else:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", params["VM_NAME"]],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise RuntimeError(f"Failed to generate SSH key: {res.stderr}")
            print(f"    [✓] Generated SSH key pair: {key_path}")

    pub_content = pub_key_path.read_text(encoding="utf-8").strip() if pub_key_path.exists() else ""
    print(f"    [✓] Public key ready for cloud-init injection ({pub_key_path.name})")

    # 2. Check ~/.ssh/config
    ssh_config_file = Path.home() / ".ssh" / "config"
    config_entry = f"""
# AirlockVM Execution Virtual Machine
Host {params['VM_NAME']}
    HostName {params['VM_IP']}
    User {params['VM_PRIMARY_USER']}"""

    if params["HYPERVISOR_HOST"] not in ("localhost", "127.0.0.1"):
        config_entry += f"\n    ProxyJump {params['HYPERVISOR_HOST']}"

    config_entry += f"""
    IdentityFile {params['SSH_KEY_PATH']}
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
"""

    if ssh_config_file.exists():
        existing_config = ssh_config_file.read_text(encoding="utf-8")
        if f"Host {params['VM_NAME']}" in existing_config:
            print(f"    [✓] ~/.ssh/config already contains entry for 'Host {params['VM_NAME']}'")
            return pub_content

    print(f"    -> Adding SSH config block for '{params['VM_NAME']}' to ~/.ssh/config...")
    if dry_run:
        print(f"    [PLAN] Append to ~/.ssh/config:\n{config_entry.strip()}")
    else:
        ssh_config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(ssh_config_file, "a", encoding="utf-8") as f:
            f.write(config_entry)
        ssh_config_file.chmod(0o600)
        print(f"    [✓] Updated ~/.ssh/config")

    return pub_content


# ==============================================================================
# Hypervisor Provisioning Pipeline
# ==============================================================================

def check_domain_status(vm_name: str, host: str) -> tuple[bool, str]:
    """Checks whether the domain exists in libvirt and its running state."""
    ret, out, _ = run_cmd_status(["sudo", "virsh", "dominfo", vm_name], host=host)
    if ret != 0:
        return False, "not_found"

    state_match = re.search(r"^State:\s+([^\n]+)", out, re.MULTILINE)
    state = state_match.group(1).strip() if state_match else "unknown"
    return True, state


def resolve_storage_dir(host: str) -> str:
    """Finds or creates an optimal libvirt storage directory on hypervisor."""
    # Check if pool 'vm' exists
    ret, out, _ = run_cmd_status(["sudo", "virsh", "pool-dumpxml", "vm"], host=host)
    if ret == 0:
        path_match = re.search(r"<path>([^<]+)</path>", out)
        if path_match:
            return path_match.group(1).strip()

    # Fallback to standard libvirt images path
    return "/var/lib/libvirt/images"


def resolve_base_cloud_image(host: str, target_dir: str, dry_run: bool = False) -> str:
    """Finds existing Noble cloud image or downloads it to the hypervisor."""
    candidates = [
        f"{target_dir}/ubuntu-24.04-minimal-cloudimg.img",
        "/var/lib/libvirt/images/ubuntu-24.04-minimal-cloudimg.img",
        "/var/lib/libvirt/images/noble-server-cloudimg-amd64.img",
    ]

    for candidate in candidates:
        ret, _, _ = run_cmd_status(["test", "-f", candidate], host=host)
        if ret == 0:
            print(f"    [✓] Using cached base cloud image on {host}: {candidate}")
            return candidate

    dest_img = f"{target_dir}/ubuntu-24.04-minimal-cloudimg.img"
    url = "https://cloud-images.ubuntu.com/minimal/releases/noble/release/ubuntu-24.04-minimal-cloudimg-amd64.img"
    print(f"    -> Base cloud image not found locally on {host}. Will download from Ubuntu Cloud Images:")
    print(f"       Source: {url}")
    print(f"       Dest:   {dest_img}")

    if dry_run:
        print(f"    [PLAN] curl -fsSL -o {dest_img} {url}")
        return dest_img

    print(f"    [*] Downloading Ubuntu 24.04 minimal cloud image to {host}...")
    dl_cmd = f"sudo curl -fsSL -o '{dest_img}' '{url}'"
    ret, _, err = run_cmd_status(["bash", "-c", dl_cmd], host=host)
    if ret != 0:
        raise RuntimeError(f"Failed to download cloud image: {err}")
    print(f"    [✓] Base cloud image downloaded.")
    return dest_img


def ensure_static_dhcp(params: dict, host: str, dry_run: bool = False) -> None:
    """Ensures static IP/MAC reservation is registered in libvirt network."""
    net_name = params["LIBVIRT_NETWORK"]
    mac = params["VM_MAC"]
    ip = params["VM_IP"]
    name = params["VM_NAME"]

    print(f"[*] Ensuring static DHCP reservation in libvirt network '{net_name}' on {host}...")
    ret, out, _ = run_cmd_status(["sudo", "virsh", "net-dumpxml", net_name], host=host)
    if ret != 0:
        print(f"    [-] Warning: Could not read libvirt network '{net_name}'. Skipping reservation update.")
        return

    if mac.lower() in out.lower() or ip in out:
        print(f"    [✓] Reservation already active: MAC={mac} -> IP={ip} ({name})")
        return

    print(f"    -> Adding reservation: MAC={mac} -> IP={ip} ({name})")
    xml_snippet = f"<host mac='{mac}' name='{name}' ip='{ip}'/>"
    update_cmd = ["sudo", "virsh", "net-update", net_name, "add-last", "ip-dhcp-host", xml_snippet, "--live", "--config"]

    if dry_run:
        print(f"    [PLAN] {' '.join(update_cmd)}")
    else:
        ret, _, err = run_cmd_status(update_cmd, host=host)
        if ret != 0:
            print(f"    [-] Warning adding DHCP reservation: {err}")
        else:
            print(f"    [✓] Static DHCP reservation registered successfully.")


def generate_cloud_init_seed(params: dict, pub_key: str, seed_iso_path: str, host: str, dry_run: bool = False) -> None:
    """Creates cloud-init user-data, meta-data, and builds the NoCloud seed ISO."""
    meta_data = f"""instance-id: {params['VM_NAME']}
local-hostname: {params['VM_NAME']}
"""

    secondary_user = (params.get("VM_SECONDARY_USER") or "").strip()
    secondary_block = ""
    if secondary_user and secondary_user != params["VM_PRIMARY_USER"]:
        secondary_block = f"""  - name: {secondary_user}
    gecos: AirlockVM Secondary User
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    groups: [sudo, docker]
    ssh_authorized_keys:
      - {pub_key}
"""

    user_data = f"""#cloud-config
hostname: {params['VM_NAME']}
users:
  - name: {params['VM_PRIMARY_USER']}
    gecos: AirlockVM Primary User
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    groups: [sudo, docker]
    ssh_authorized_keys:
      - {pub_key}
{secondary_block}
packages:
  - docker.io
  - containerd
  - docker-compose-v2
  - qemu-guest-agent
  - python3
  - python3-venv
  - python3-pip
  - nodejs
  - npm
  - git
  - curl
  - wget
  - jq
  - tmux
  - htop
  - ca-certificates
  - rsync

runcmd:
  - systemctl enable --now docker
  - systemctl enable --now qemu-guest-agent
"""

    print(f"[*] Generating Cloud-Init NoCloud seed on {host}...")
    if dry_run:
        print(f"    [PLAN] Build seed ISO: {seed_iso_path} with primary user '{params['VM_PRIMARY_USER']}'")
        return

    # Write files to a temporary location on hypervisor and build ISO
    build_script = f"""
set -euo pipefail
TMP_DIR="$(mktemp -d /tmp/airlock_cloudinit_XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

cat << 'USERDATA_EOF' > "$TMP_DIR/user-data"
{user_data}
USERDATA_EOF

cat << 'METADATA_EOF' > "$TMP_DIR/meta-data"
{meta_data}
METADATA_EOF

cat << 'NETCONFIG_EOF' > "$TMP_DIR/network-config"
version: 2
ethernets:
  default:
    match:
      name: "en*"
    dhcp4: true
NETCONFIG_EOF

sudo cloud-localds -N "$TMP_DIR/network-config" '{seed_iso_path}' "$TMP_DIR/user-data" "$TMP_DIR/meta-data"
sudo chown libvirt-qemu:kvm '{seed_iso_path}' || true
sudo chmod 664 '{seed_iso_path}' || true
"""
    ret, _, err = run_cmd_status(["bash", "-c", build_script], host=host)
    if ret != 0:
        raise RuntimeError(f"Failed to generate cloud-init seed: {err}")
    print(f"    [✓] Cloud-init seed generated at {seed_iso_path}")


def provision_domain(params: dict, disk_path: str, seed_iso_path: str, host: str, dry_run: bool = False) -> None:
    """Executes virt-install to launch the virtual machine."""
    cmd = [
        "sudo", "virt-install",
        "--name", params["VM_NAME"],
        "--vcpus", params["VCPU_CORES"],
        "--memory", params["RAM_MIB"],
        "--cpu", params.get("CPU_MODE", "host-passthrough"),
        "--disk", f"path={disk_path},format={params['DISK_FORMAT']},bus=virtio",
        "--disk", f"path={seed_iso_path},device=disk,bus=virtio",
        "--network", f"network={params['LIBVIRT_NETWORK']},mac={params['VM_MAC']},model=virtio",
        "--os-variant", "ubuntu24.04",
        "--boot", "uefi",
        "--graphics", "none",
        "--noautoconsole",
        "--import",
    ]

    print(f"[*] Provisioning VM domain '{params['VM_NAME']}' on {host} via virt-install...")
    if dry_run:
        print(f"    [PLAN] {' '.join(cmd)}")
    else:
        ret, out, err = run_cmd_status(cmd, host=host)
        if ret != 0:
            raise RuntimeError(f"virt-install failed: {err}\n{out}")
        print(f"    [✓] Domain successfully provisioned and started.")


def setup_hypervisor_proxy(params: dict, host: str, dry_run: bool = False) -> None:
    """Sets up systemd-socket-proxyd forwarders on the hypervisor for all mapped application ports."""
    vm_name = params["VM_NAME"]
    vm_ip = params["VM_IP"]
    repos = params.get("REPOSITORIES", [])

    # Collect port mappings: list of (ext_port, int_port, label)
    port_mappings = []
    seen_ext = set()

    if isinstance(repos, list) and repos:
        for r in repos:
            if not isinstance(r, dict):
                continue
            rname = r.get("name", vm_name)
            ext_p = str(r.get("external_port", "") or params.get("PROXY_EXTERNAL_PORT", "3001"))
            int_p = str(r.get("internal_port", "") or params.get("APP_INTERNAL_PORT", "3000"))
            if ext_p not in seen_ext:
                seen_ext.add(ext_p)
                port_mappings.append((ext_p, int_p, rname))

    if not port_mappings:
        default_ext = str(params.get("PROXY_EXTERNAL_PORT", "3001"))
        default_int = str(params.get("APP_INTERNAL_PORT", "3000"))
        port_mappings.append((default_ext, default_int, vm_name))

    for ext_port, int_port, label in port_mappings:
        unit_name = f"{vm_name}-p{ext_port}-proxy"
        socket_path = f"/etc/systemd/system/{unit_name}.socket"
        service_path = f"/etc/systemd/system/{unit_name}.service"

        # Determine listen address: if running directly on localhost, bind strictly to 127.0.0.1 (loopback)
        # to prevent exposing the port to the physical LAN. Remote hypervisors bind to 0.0.0.0 so clients can reach it.
        listen_address = "127.0.0.1" if host in ("localhost", "127.0.0.1") else "0.0.0.0"

        print(f"[*] Configuring hypervisor proxy for '{label}' on {host} (port {listen_address}:{ext_port} -> {vm_ip}:{int_port})...")

        socket_content = f"""[Unit]
Description=AirlockVM Proxy Socket for {label} ({vm_name})
PartOf={unit_name}.service

[Socket]
ListenStream={listen_address}:{ext_port}

[Install]
WantedBy=sockets.target
"""

        # Resolve systemd-socket-proxyd binary path across distros (/usr/lib vs /lib)
        proxyd_bin = "/usr/lib/systemd/systemd-socket-proxyd"
        if not dry_run:
            ret_p, out_p, _ = run_cmd_status(
                ["bash", "-c", "for p in /usr/lib/systemd/systemd-socket-proxyd /lib/systemd/systemd-socket-proxyd; do [ -x \"$p\" ] && echo \"$p\" && exit 0; done; echo /lib/systemd/systemd-socket-proxyd"],
                host=host,
            )
            if ret_p == 0 and out_p:
                proxyd_bin = out_p.splitlines()[0].strip()

        service_content = f"""[Unit]
Description=AirlockVM Proxy Service for {label} ({vm_name})
After=network.target

[Service]
ExecStart={proxyd_bin} {vm_ip}:{int_port}
"""

        if dry_run:
            print(f"    [PLAN] Configure {socket_path} (ListenStream={listen_address}:{ext_port})")
            print(f"    [PLAN] Configure {service_path}")
            print(f"    [PLAN] sudo systemctl daemon-reload && sudo systemctl enable --now {unit_name}.socket")
            continue

        setup_script = f"""
set -euo pipefail
sudo tee '{socket_path}' >/dev/null << 'EOF'
{socket_content}
EOF

sudo tee '{service_path}' >/dev/null << 'EOF'
{service_content}
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now '{unit_name}.socket'
"""
        ret, _, err = run_cmd_status(["bash", "-c", setup_script], host=host)
        if ret != 0:
            print(f"    [-] Warning: Could not configure socket proxy ({unit_name}): {err}")
        else:
            print(f"    [✓] systemd-socket-proxyd active on {host}:{ext_port} -> {vm_ip}:{int_port}")


def create_baseline_snapshot(params: dict, host: str, dry_run: bool = False) -> None:
    """Takes the initial pristine baseline snapshot once the VM is online."""
    vm_name = params["VM_NAME"]
    snap_name = params.get("SNAPSHOT_BASE_NAME", "snapshot-base-clean")
    snap_desc = params.get("SNAPSHOT_BASE_DESC", "Pristine base setup with Docker and Python")

    print(f"[*] Checking pristine baseline snapshot '{snap_name}' on {host}...")
    ret, out, _ = run_cmd_status(["sudo", "virsh", "snapshot-list", vm_name], host=host)
    if ret == 0 and snap_name in out:
        print(f"    [✓] Baseline snapshot '{snap_name}' already exists.")
        return

    cmd = [
        "sudo", "virsh", "snapshot-create-as",
        "--domain", vm_name,
        "--name", snap_name,
        "--disk-only",
        "--description", snap_desc,
    ]

    if dry_run:
        print(f"    [PLAN] {' '.join(cmd)}")
    else:
        ret, _, err = run_cmd_status(cmd, host=host)
        if ret != 0:
            print(f"    [-] Snapshot notice: {err}")
        else:
            print(f"    [✓] Pristine baseline snapshot '{snap_name}' created successfully.")


# ==============================================================================
# End-to-End Proxy Validation
# ==============================================================================

def validate_proxy_connection(params: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Validates end-to-end communication by requesting the root URL via the hypervisor proxy.

    Full Roundtrip Topology:
      Workstation -> Hypervisor (port 3001) -> systemd-socket-proxyd -> Guest VM (port 3000) -> HTTP 200 OK
    """
    import urllib.request
    import urllib.error

    vm_name = params["VM_NAME"]
    host = params["HYPERVISOR_HOST"]
    ext_port = params.get("PROXY_EXTERNAL_PORT", "3001")
    int_port = params.get("APP_INTERNAL_PORT", "3000")
    lan_ip = params.get("HYPERVISOR_LAN_IP", "")

    proxy_url = f"http://{host}:{ext_port}/"
    alt_url = f"http://{lan_ip}:{ext_port}/" if lan_ip and lan_ip != host else None

    print(f"[*] Validating end-to-end proxy connectivity (Workstation -> {host}:{ext_port} -> {vm_name}:{int_port})...")

    if dry_run:
        print(f"    [PLAN] Workstation HTTP GET request via proxy: {proxy_url}")
        print(f"    [PLAN] Verify HTTP 200 OK and response payload")
        return True, "Dry-run plan verified."

    urls_to_try = [proxy_url]
    if alt_url:
        urls_to_try.append(alt_url)

    success = False
    content_received = ""
    used_url = proxy_url
    start_time = time.time()
    latency_ms = 0
    page_title = "Online"

    for url in urls_to_try:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AirlockVM-Deploy-Validator/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                if resp.status in (200, 204, 301, 302):
                    content_received = resp.read().decode("utf-8", errors="replace")
                    success = True
                    used_url = url
                    latency_ms = int((time.time() - start_time) * 1000)
                    title_match = re.search(r"<title>([^<]+)</title>", content_received, re.IGNORECASE)
                    if title_match:
                        page_title = title_match.group(1).strip()
                    break
        except Exception:
            continue

    if success:
        print(f"    [✓] Proxy Validation SUCCESSFUL!")
        print(f"        Validated URL: {used_url}")
        print(f"        HTTP Status:   200 OK")
        print(f"        Roundtrip:     {latency_ms} ms")
        print(f"        Page Title:    {page_title}")
        return True, "Proxy validation successful."
    else:
        print(f"    [-] Proxy Validation WARNING: Could not connect to {proxy_url}.")
        print("        Possible causes:")
        print(f"          1. systemd-socket-proxyd is inactive on {host} (check: ssh {host} 'systemctl status airlock-proxy.socket')")
        print(f"          2. External port {ext_port} is blocked by a firewall on {host}")
        print(f"          3. Virtual network bridge cannot route to {params['VM_IP']}:{int_port}")
        return False, "Proxy validation failed."


# ==============================================================================
# Live Management Actions (status, start, stop, snapshot, ssh)
# ==============================================================================

def execute_status(params: dict) -> None:
    """Queries and displays live status of the VM across hypervisor and network."""
    vm_name = params["VM_NAME"]
    host = params["HYPERVISOR_HOST"]
    vm_ip = params["VM_IP"]
    ext_port = params.get("PROXY_EXTERNAL_PORT", "3001")

    print("============================================================")
    print(f" AirlockVM Virtual Machine Status: {vm_name}")
    print(f" Hypervisor: {host} | Configured IP: {vm_ip}")
    print("============================================================")

    exists, state = check_domain_status(vm_name, host)
    if not exists:
        print(f"Domain '{vm_name}' is NOT defined on hypervisor '{host}'.")
        print("Run 'python3 deploy_vm.py' to deploy the VM.")
        return

    print(f"[*] Libvirt Domain State: {state.upper()}")

    # Check snapshots
    ret, snap_out, _ = run_cmd_status(["sudo", "virsh", "snapshot-list", vm_name], host=host)
    if ret == 0:
        print("\n[*] Snapshots:")
        print(snap_out if snap_out else "    (No snapshots found)")

    # Check SSH Reachability from Workstation
    print("\n[*] SSH Reachability from Workstation:")
    ssh_test = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", vm_name, "echo online"],
        capture_output=True,
        text=True,
    )
    if ssh_test.returncode == 0:
        print(f"    [✓] SSH to '{vm_name}' is ONLINE.")
        # Probe Docker
        doc_test = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", vm_name, "docker --version"],
            capture_output=True,
            text=True,
        )
        if doc_test.returncode == 0:
            print(f"    [✓] Docker Daemon: {doc_test.stdout.strip()}")
    else:
        print(f"    [!] SSH to '{vm_name}' is currently UNREACHABLE (VM may be booting or shut off).")

    # Check Hypervisor Proxy Socket
    proxy_unit = f"{vm_name}-p{ext_port}-proxy.socket"
    ret, sock_out, _ = run_cmd_status(["systemctl", "is-active", proxy_unit], host=host)
    if ret != 0 or sock_out != "active":
        ret, sock_out, _ = run_cmd_status(["systemctl", "is-active", "airlock-proxy.socket"], host=host)
    proxy_status = "ACTIVE" if sock_out == "active" else "INACTIVE"
    print(f"\n[*] Hypervisor Forwarder: {proxy_status} (http://{host}:{ext_port}/)")

    # Check Inventory Record
    inv_dir = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))
    inv_vm_yaml = inv_dir / host / vm_name / "vm.yaml"
    if inv_vm_yaml.exists():
        print(f"[*] Inventory Record: {inv_vm_yaml} [RECORDED]")
    else:
        print(f"[*] Inventory Record: Not registered (run add_vmhost.py {host} to register)")
    print("============================================================\n")


def sync_inventory_record(params: dict, action: str = "update", dry_run: bool = False) -> None:
    """Synchronizes VM record with airlock_inventory skill."""
    host = params.get("HYPERVISOR_HOST", "localhost")
    vm_name = params.get("VM_NAME")
    if not vm_name:
        return

    inventory_script = REPO_ROOT / ".agents" / "skills" / "airlock_inventory" / "scripts" / "add_vmhost.py"
    inventory_root = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))

    if action == "purge":
        vm_dir = inventory_root / host / vm_name
        if vm_dir.exists():
            print(f"    -> Removing inventory record in {vm_dir}...")
            if not dry_run:
                shutil.rmtree(vm_dir, ignore_errors=True)
                print(f"    [✓] Inventory directory removed.")
            else:
                print(f"    [PLAN] Remove directory {vm_dir}")

        if inventory_script.exists():
            print(f"    -> Refreshing host inventory in inventory/{host}/vmhost.yaml...")
            if not dry_run:
                subprocess.run([sys.executable, str(inventory_script), host], capture_output=True, text=True)
                print(f"    [✓] Host inventory refreshed.")
            else:
                print(f"    [PLAN] Refresh inventory: python3 {inventory_script} {host}")

    elif action == "update":
        if inventory_script.exists():
            print(f"\n[*] Updating AirlockVM inventory (inventory/{host}/{vm_name}/vm.yaml)...")
            if not dry_run:
                res = subprocess.run(
                    [sys.executable, str(inventory_script), host, "--vm", vm_name],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    print(f"    [✓] Inventory record synchronized successfully.")
                else:
                    print(f"    [-] Warning: Inventory sync returned code {res.returncode}")
            else:
                print(f"    [PLAN] python3 {inventory_script} {host} --vm {vm_name}")


def execute_snapshot(params: dict, host: str, snapshot_name: str = "default", dry_run: bool = False) -> int:
    """Creates a disk-only snapshot on the target VM domain."""
    vm_name = params["VM_NAME"]
    snap_name = params.get("SNAPSHOT_BASE_NAME", "snapshot-base-clean") if snapshot_name in ("", "default") else snapshot_name
    snap_desc = f"AirlockVM snapshot '{snap_name}'"
    cmd = [
        "sudo", "virsh", "snapshot-create-as",
        "--domain", vm_name,
        "--name", snap_name,
        "--disk-only",
        "--description", snap_desc,
    ]
    print(f"[*] Creating snapshot '{snap_name}' for domain '{vm_name}' on {host}...")
    if dry_run:
        print(f"    [PLAN] {' '.join(cmd)}")
        return 0
    ret, out, err = run_cmd_status(cmd, host=host)
    if ret != 0:
        print(f"[-] Error creating snapshot '{snap_name}': {err}", file=sys.stderr)
        return 1
    print(f"[✓] Snapshot '{snap_name}' created successfully: {out or 'OK'}")
    return 0


def execute_rollback(params: dict, host: str, snapshot_name: str = "default", dry_run: bool = False) -> int:
    """Reverts a VM domain to a named or baseline snapshot."""
    vm_name = params["VM_NAME"]
    snap_name = params.get("SNAPSHOT_BASE_NAME", "snapshot-base-clean") if snapshot_name in ("", "default") else snapshot_name
    cmd = ["sudo", "virsh", "snapshot-revert", "--domain", vm_name, "--snapshotname", snap_name]
    print(f"[*] Reverting domain '{vm_name}' on {host} to snapshot '{snap_name}'...")
    if dry_run:
        print(f"    [PLAN] {' '.join(cmd)}")
        return 0
    ret, out, err = run_cmd_status(cmd, host=host)
    if ret != 0:
        print(f"[-] Error reverting to snapshot '{snap_name}': {err}", file=sys.stderr)
        return 1
    print(f"[✓] Domain '{vm_name}' reverted to snapshot '{snap_name}': {out or 'OK'}")
    return 0


def execute_power_action(params: dict, action: str) -> None:
    """Controls VM lifecycle (start, stop, reboot, destroy)."""
    vm_name = params["VM_NAME"]
    host = params["HYPERVISOR_HOST"]

    action_map = {
        "start": ["sudo", "virsh", "start", vm_name],
        "stop": ["sudo", "virsh", "shutdown", vm_name],
        "destroy": ["sudo", "virsh", "destroy", vm_name],
        "reboot": ["sudo", "virsh", "reboot", vm_name],
    }

    cmd = action_map.get(action)
    if not cmd:
        print(f"Unknown power action: {action}")
        return

    print(f"[*] Executing '{action}' on domain '{vm_name}' on {host}...")
    ret, out, err = run_cmd_status(cmd, host=host)
    if ret != 0:
        print(f"[-] Error: {err}")
    else:
        print(f"[✓] Successfully executed '{action}': {out or 'OK'}")


def execute_purge(params: dict, host: str, dry_run: bool = False) -> None:
    """Entirely removes the VM domain, nvram, snapshots, and disk storage from the hypervisor."""
    vm_name = params["VM_NAME"]
    target_storage = resolve_storage_dir(host)

    print(f"[*] Entirely removing AirlockVM VM '{vm_name}' and its storage from '{host}'...")

    exists, state = check_domain_status(vm_name, host)
    if exists:
        if state.lower() == "running":
            print(f"    -> Destroying running domain '{vm_name}'...")
            if not dry_run:
                run_cmd_status(["sudo", "virsh", "destroy", vm_name], host=host)
            else:
                print(f"    [PLAN] sudo virsh destroy {vm_name}")

        print(f"    -> Undefining domain '{vm_name}' (NVRAM + snapshot metadata)...")
        if not dry_run:
            ret, _, err = run_cmd_status(["sudo", "virsh", "undefine", vm_name, "--nvram", "--snapshots-metadata"], host=host)
            if ret != 0:
                run_cmd_status(["sudo", "virsh", "undefine", vm_name, "--nvram"], host=host)
            print(f"    [✓] Domain undefined.")
        else:
            print(f"    [PLAN] sudo virsh undefine {vm_name} --nvram --snapshots-metadata")
    else:
        print(f"    -> Domain '{vm_name}' is not registered in libvirt.")

    # Remove disk storage and seed ISOs
    print(f"    -> Deleting VM storage images in {target_storage}/{vm_name}*...")
    if not dry_run:
        cleanup_cmd = f"sudo rm -f '{target_storage}/{vm_name}'*.qcow2 '{target_storage}/{vm_name}'*.iso '{target_storage}/{vm_name}'.snapshot* '{target_storage}/{vm_name}'-seed.snapshot*"
        ret, _, err = run_cmd_status(["bash", "-c", cleanup_cmd], host=host)
        if ret == 0:
            print(f"    [✓] Storage disks purged.")
        else:
            print(f"    [-] Warning during storage cleanup: {err}")
    else:
        print(f"    [PLAN] sudo rm -f '{target_storage}/{vm_name}'*.qcow2 '{target_storage}/{vm_name}'*.iso '{target_storage}/{vm_name}'.snapshot*")

    # Clean up workstation known_hosts to prevent key mismatch on redeploy
    print(f"    -> Cleaning up workstation ~/.ssh/known_hosts for '{vm_name}' and '{params['VM_IP']}'...")
    if not dry_run:
        subprocess.run(["ssh-keygen", "-R", vm_name], capture_output=True)
        subprocess.run(["ssh-keygen", "-R", params["VM_IP"]], capture_output=True)
        print(f"    [✓] Workstation known_hosts cleaned.")
    else:
        print(f"    [PLAN] ssh-keygen -R {vm_name} && ssh-keygen -R {params['VM_IP']}")

    # Clean up hypervisor proxy forwarders for this VM
    print(f"    -> Cleaning up hypervisor proxy forwarders for '{vm_name}'...")
    if not dry_run:
        cleanup_proxy_cmd = f"sudo systemctl stop '{vm_name}'-p*-proxy.socket '{vm_name}'-p*-proxy.service '{vm_name}'-proxy.socket airlock-proxy.socket 2>/dev/null || true; sudo systemctl disable '{vm_name}'-p*-proxy.socket '{vm_name}'-proxy.socket airlock-proxy.socket 2>/dev/null || true; sudo rm -f /etc/systemd/system/'{vm_name}'-p*-proxy.* /etc/systemd/system/'{vm_name}'-proxy.* 2>/dev/null || true; sudo systemctl daemon-reload 2>/dev/null || true"
        run_cmd_status(["bash", "-c", cleanup_proxy_cmd], host=host)
        print(f"    [✓] Hypervisor proxy sockets cleaned.")
    else:
        print(f"    [PLAN] sudo systemctl stop {vm_name}-p*-proxy.socket && rm -f /etc/systemd/system/{vm_name}-p*-proxy.*")

    # Clean up inventory record
    sync_inventory_record(params, action="purge", dry_run=dry_run)

    print(f"[✓] AirlockVM VM '{vm_name}' has been entirely removed from '{host}'.\n")


# ==============================================================================
# Main Orchestrator
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="AirlockVM VM Deployment Engine — Deploys and manages the execution VM using inventory/{vmhost}/{vm}/vm.yaml (SSOT)"
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="Simulate deployment plan without modifying hypervisor")
    parser.add_argument("--check-only", action="store_true", help="Verify pre-requisite gates and hypervisor connectivity only")
    parser.add_argument("--status", action="store_true", help="Display live status of the VM, libvirt domain, and services")
    parser.add_argument("--validate-proxy", action="store_true", help="Validate end-to-end connectivity by fetching textfile via hypervisor proxy")
    parser.add_argument("--start", action="store_true", help="Power on the VM domain")
    parser.add_argument("--stop", action="store_true", help="Gracefully shut down the VM domain")
    parser.add_argument("--destroy", action="store_true", help="Force terminate the VM domain")
    parser.add_argument("--purge", action="store_true", help="Entirely remove the VM domain, nvram, snapshots, and disk storage")
    parser.add_argument("--reboot", action="store_true", help="Reboot the VM domain")
    parser.add_argument("--snapshot", nargs="?", const="default", help="Create a snapshot (optional snapshot name)")
    parser.add_argument("--rollback", nargs="?", const="default", help="Revert to baseline or named snapshot")
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-confirm deployment without interactive prompt")
    parser.add_argument("--vm", type=str, help="Target VM domain name in inventory (e.g. airlock-vm)")
    parser.add_argument("--config", type=Path, default=None, help="Path to VM specification YAML (default: auto-detected from inventory)")
    parser.add_argument("--doc", type=Path, default=None, help="Path to supplementary runbook markdown or custom specification")
    parser.add_argument("--boot-timeout", type=int, default=240, help="Seconds to wait for guest SSH readiness after provisioning (default: 240)")
    args = parser.parse_args()

    if args.config:
        config_path = args.config
        runbook_path = args.doc or (config_path.parent / "VM.md" if (config_path.parent / "VM.md").exists() else None)
    elif args.doc and args.doc.suffix in (".yaml", ".yml"):
        config_path = args.doc
        runbook_path = config_path.parent / "VM.md" if (config_path.parent / "VM.md").exists() else None
    elif args.doc and args.doc.suffix == ".md":
        runbook_path = args.doc
        yaml_cand = runbook_path.parent / "vm.yaml"
        config_path = yaml_cand if yaml_cand.exists() else runbook_path
    else:
        config_path, runbook_path = find_default_config(args.vm)

    if not config_path or not config_path.exists():
        print("============================================================")
        print(" AirlockVM VM Deployment Engine")
        print("============================================================")
        print("[!] Pre-requisite Gate 1 FAILED: No VM specification found in inventory/.\n\n"
              "Remediation:\n"
              "  Run airlock_inventory to discover and document the hypervisor and VMs:\n"
              "    python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py <vmhost>\n"
              "  Or specify an existing configuration via --config <path> or --vm <vm_name>.")
        sys.exit(1)

    print("============================================================")
    print(" AirlockVM VM Deployment Engine")
    print(f" SSOT Config: {config_path.relative_to(REPO_ROOT) if config_path.is_relative_to(REPO_ROOT) else config_path}")
    if runbook_path and runbook_path.exists():
        print(f" Runbook:     {runbook_path.relative_to(REPO_ROOT) if runbook_path.is_relative_to(REPO_ROOT) else runbook_path} (Supplementary)")
    print("============================================================")

    # --------------------------------------------------------------------------
    # MANDATORY GATE 1 & 2: SSOT validation & verify_config.py
    # --------------------------------------------------------------------------
    prereq_ok, prereq_msg = check_prerequisites(config_path=config_path, runbook_path=runbook_path, verify_script=VERIFY_SCRIPT)
    print(prereq_msg)
    if not prereq_ok:
        sys.exit(1)

    # Parse parameters from verified SSOT specification
    params = load_vm_config(config_path)
    vm_name = params["VM_NAME"]
    host = params["HYPERVISOR_HOST"]

    # Handle status action
    if args.status:
        execute_status(params)
        sys.exit(0)

    # Handle power actions
    if args.start:
        execute_power_action(params, "start")
        sys.exit(0)
    if args.stop:
        execute_power_action(params, "stop")
        sys.exit(0)
    if args.destroy:
        execute_power_action(params, "destroy")
        sys.exit(0)
    if args.purge:
        execute_purge(params, host, dry_run=args.dry_run)
        sys.exit(0)
    if args.reboot:
        execute_power_action(params, "reboot")
        sys.exit(0)

    # Handle snapshot / rollback actions
    if args.snapshot is not None:
        sys.exit(execute_snapshot(params, host, snapshot_name=args.snapshot, dry_run=args.dry_run))
    if args.rollback is not None:
        sys.exit(execute_rollback(params, host, snapshot_name=args.rollback, dry_run=args.dry_run))

    # Handle validate-proxy action
    if args.validate_proxy:
        ok, _ = validate_proxy_connection(params, dry_run=args.dry_run)
        sys.exit(0 if ok else 1)

    # Handle check-only
    if args.check_only:
        print(f"\n[*] Probing connectivity to hypervisor '{host}'...")
        ret, out, err = run_cmd_status(["echo", "connected"], host=host)
        if ret != 0:
            print(f"[-] Error: Could not connect to hypervisor '{host}': {err}")
            sys.exit(1)
        print(f"[✓] Hypervisor '{host}' is reachable via SSH.")
        exists, state = check_domain_status(vm_name, host)
        print(f"[*] Domain '{vm_name}' state on {host}: {state.upper()}")
        print("\n[✓] Check-only complete: Prerequisites and hypervisor connectivity validated.")
        sys.exit(0)

    # --------------------------------------------------------------------------
    # Full Deployment Execution Pipeline
    # --------------------------------------------------------------------------
    print("\n------------------------------------------------------------")
    print(f" Target VM Specification:")
    print(f"   Domain Name:      {params['VM_NAME']}")
    print(f"   Hypervisor:       {params['HYPERVISOR_HOST']}")
    print(f"   vCPUs / RAM:      {params['VCPU_CORES']} Cores / {params['RAM_MIB']} MiB")
    print(f"   Disk Storage:     {params['DISK_SIZE_GIB']} GiB ({params['DISK_FORMAT']})")
    print(f"   Network IP / MAC: {params['VM_IP']} / {params['VM_MAC']}")
    print(f"   Primary User:     {params['VM_PRIMARY_USER']}")
    print(f"   Web Proxy Port:   http://{params['HYPERVISOR_HOST']}:{params['PROXY_EXTERNAL_PORT']}/")
    print("------------------------------------------------------------\n")

    # Check if VM already exists
    exists, state = check_domain_status(vm_name, host)
    if exists:
        print(f"[*] Notice: Domain '{vm_name}' already exists on hypervisor '{host}' (State: {state.upper()}).")
        if state.lower() == "shut off":
            print(f"    -> Domain is currently powered off.")
            if not args.dry_run:
                execute_power_action(params, "start")
            else:
                print(f"    [PLAN] sudo virsh start {vm_name}")
        # Ensure all configured repository proxy forwarders are active on the hypervisor
        setup_hypervisor_proxy(params, host, dry_run=args.dry_run)
        execute_status(params)
        sys.exit(0)

    # Interactive confirmation if not dry-run and not auto-confirmed
    if not args.dry_run and not args.yes:
        confirm = input(f"Proceed with deploying '{vm_name}' to hypervisor '{host}'? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Deployment cancelled by user.")
            sys.exit(0)

    # Step 1: Workstation SSH Credential Setup
    pub_key = ensure_workstation_ssh(params, dry_run=args.dry_run)

    # Step 2: Libvirt Static DHCP Reservation
    ensure_static_dhcp(params, host, dry_run=args.dry_run)

    # Step 3: Hypervisor Storage & Base Image Preparation
    target_storage = resolve_storage_dir(host)
    base_image = resolve_base_cloud_image(host, target_storage, dry_run=args.dry_run)

    disk_path = f"{target_storage}/{vm_name}.qcow2"
    seed_iso_path = f"{target_storage}/{vm_name}-seed.iso"

    print(f"[*] Preparing VM disk image: {disk_path} ({params['DISK_SIZE_GIB']} GiB)...")
    if args.dry_run:
        print(f"    [PLAN] sudo qemu-img create -f qcow2 -b '{base_image}' -F qcow2 '{disk_path}' {params['DISK_SIZE_GIB']}G")
    else:
        create_disk_cmd = f"sudo qemu-img create -f qcow2 -b '{base_image}' -F qcow2 '{disk_path}' {params['DISK_SIZE_GIB']}G && sudo chown libvirt-qemu:kvm '{disk_path}' && sudo chmod 664 '{disk_path}'"
        ret, _, err = run_cmd_status(["bash", "-c", create_disk_cmd], host=host)
        if ret != 0:
            raise RuntimeError(f"Failed to create VM disk: {err}")
        print(f"    [✓] QCOW2 disk created backed by {base_image}")

    # Step 4: Cloud-Init NoCloud Seed Generation
    generate_cloud_init_seed(params, pub_key, seed_iso_path, host, dry_run=args.dry_run)

    # Step 5: Provision Domain with virt-install
    provision_domain(params, disk_path, seed_iso_path, host, dry_run=args.dry_run)

    # Step 6: Systemd Socket Proxy Setup
    setup_hypervisor_proxy(params, host, dry_run=args.dry_run)

    # Step 7: Wait for VM Boot & SSH Probe
    if not args.dry_run:
        poll_interval = 4
        max_attempts = max(1, args.boot_timeout // poll_interval)
        print(f"\n[*] Waiting for VM '{vm_name}' to boot and respond to SSH (timeout: {args.boot_timeout}s)...")
        ssh_ready = False
        lease_seen = False
        for attempt in range(1, max_attempts + 1):
            if not lease_seen and (attempt == 1 or attempt % 5 == 0):
                rc_ip, ip_out, _ = run_cmd_status(["sudo", "virsh", "domifaddr", vm_name], host=host)
                if rc_ip == 0 and params["VM_IP"] in ip_out:
                    lease_seen = True
                    print(f"\n    [✓] DHCP lease active for '{vm_name}': {params['VM_IP']}")
            lease_label = "IP active" if lease_seen else "waiting for DHCP"
            sys.stdout.write(f"\r    -> Probing SSH (Attempt {attempt}/{max_attempts} | {lease_label})...")
            sys.stdout.flush()
            ret, _, _ = run_cmd_status(["echo", "online"], host=vm_name)
            if ret == 0:
                ssh_ready = True
                print("\n    [✓] VM is reachable over SSH!")
                break
            time.sleep(poll_interval)

        if not ssh_ready:
            print("\n[-] Notice: VM did not respond to SSH within timeout. It may still be completing cloud-init installation.")
            print(f"    Check status with: ssh {host} 'sudo virsh console {vm_name}'")
        else:
            print(f"[*] Waiting for cloud-init package installation inside '{vm_name}'...")
            run_cmd_status(["cloud-init", "status", "--wait"], host=vm_name)
            print("    [✓] Cloud-init completed successfully.")

            # Step 8: Create Baseline Pristine Snapshot
            create_baseline_snapshot(params, host, dry_run=False)

            # Step 9: Auto-sync inventory
            sync_inventory_record(params, action="update", dry_run=False)
    else:
        # Dry-run plan for post-boot steps
        print(f"\n[*] Waiting for VM '{vm_name}' to boot and respond to SSH...")
        print(f"    [PLAN] Probe 'ssh {vm_name}' until online")
        # Step 8: Pristine baseline snapshot (dry-run plan)
        create_baseline_snapshot(params, host, dry_run=True)
        # Step 9: Auto-sync inventory (dry-run plan)
        sync_inventory_record(params, action="update", dry_run=True)

    if args.dry_run:
        print("\n============================================================")
        print(" [✓] Dry-run complete. All steps and plans evaluated.")
        print(f" Run 'python3 deploy_vm.py' to execute deployment on '{host}'.")
        print("============================================================\n")
    else:
        print("\n============================================================")
        print(f" [✓] AirlockVM VM '{vm_name}' successfully deployed to '{host}'!")
        print("============================================================")
        print("Next steps:")
        print(f"  1. Connect interactively:   ssh {vm_name}")
        print(f"  2. Synchronize application: ./deploy_app_to_airlock.sh")
        print(f"  3. Access web application:  http://{host}:{params['PROXY_EXTERNAL_PORT']}/")
        print("============================================================\n")


if __name__ == "__main__":
    main()
