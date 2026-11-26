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

    vm_name = str(vm_data.get("name", "airlock-vm"))
    ext_port = str(bp.get("proxy_external_port", 3001))
    default_proxy_service = f"/etc/systemd/system/{vm_name}-p{ext_port}-proxy.socket"
    disks = vm_data.get("disks", [])
    disk_sources = [
        str(d.get("source", "")).strip()
        for d in disks
        if isinstance(d, dict) and d.get("source") and str(d.get("source", "")).strip() not in ("", "none")
    ]

    params = {
        "VM_NAME": vm_name,
        "HYPERVISOR_HOST": str(hypervisor_host),
        "HYPERVISOR_LAN_IP": str(host_data.get("system", {}).get("network", {}).get("lan_ip", "")),
        "GUEST_OS": str(bp.get("guest_os", "Ubuntu 24.04 LTS Minimal")),
        "VCPU_CORES": str(resources.get("vcpu", 4)),
        "CPU_MODE": str(bp.get("cpu_mode", "host-passthrough")),
        "RAM_MIB": str(resources.get("memory_mib", 4096)),
        "DISK_SIZE_GIB": str(bp.get("disk_size_gib", 20)),
        "DISK_FORMAT": str(bp.get("disk_format", "qcow2")),
        "UEFI_FIRMWARE": str(bp.get("uefi_firmware", "OVMF_CODE_4M.ms.fd")),
        "VM_IP": str(primary_iface.get("ip", "192.168.122.200")),
        "VM_MAC": str(primary_iface.get("mac", "52:54:00:12:34:56")),
        "VIRT_BRIDGE": str(primary_iface.get("source", "virbr0")),
        "LIBVIRT_NETWORK": "default",
        "VM_PRIMARY_USER": str(bp.get("primary_user", "airlock")),
        "VM_SECONDARY_USER": str(bp.get("secondary_user", "")),
        "SSH_KEY_PATH": str(bp.get("ssh_key_path", "~/.ssh/airlock_vm_ed25519")),
        "PROXY_SOCKET_SERVICE": str(bp.get("proxy_socket_service", default_proxy_service)),
        "PROXY_EXTERNAL_PORT": ext_port,
        "APP_INTERNAL_PORT": str(bp.get("app_internal_port", 3000)),
        "SNAPSHOT_BASE_NAME": str(bp.get("snapshot_base_name", "snapshot-base-clean")),
        "SNAPSHOT_BASE_DESC": str(bp.get("snapshot_base_desc", "Pristine base setup with Docker and Python")),
        "REMOTE_CODE_DIR": str(bp.get("remote_code_dir", "~/AirlockVM/")),
        "REPOSITORIES": bp.get("repositories", []),
        "DISK_SOURCES": disk_sources,
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


_ROOTLESS_CACHE: dict[str, bool] = {}


def is_rootless_host(host: str) -> bool:
    """Returns True when the hypervisor user operates via rootless libvirt (qemu:///system) without passwordless sudo."""
    key = host or "localhost"
    if key in _ROOTLESS_CACHE:
        return _ROOTLESS_CACHE[key]
    rc_sudo, _, _ = run_cmd_status(["sudo", "-n", "true"], host=host)
    if rc_sudo == 0:
        _ROOTLESS_CACHE[key] = False
        return False
    rc_rl, _, _ = run_cmd_status(["virsh", "-c", "qemu:///system", "uri"], host=host)
    rootless = (rc_rl == 0)
    _ROOTLESS_CACHE[key] = rootless
    return rootless


def virsh_cmd(host: str, *args: str) -> list[str]:
    """Builds a virsh command using rootless qemu:///system or sudo depending on host privilege mode."""
    if is_rootless_host(host):
        return ["virsh", "-c", "qemu:///system", *args]
    return ["sudo", "virsh", *args]


def priv_cmd(host: str, *args: str) -> list[str]:
    """Prefixes a hypervisor command with sudo unless the host is operating in rootless mode."""
    if is_rootless_host(host):
        return list(args)
    return ["sudo", *args]


def virt_install_cmd(host: str, *args: str) -> list[str]:
    """Builds a virt-install command using rootless qemu:///system or sudo."""
    if is_rootless_host(host):
        return ["virt-install", "--connect", "qemu:///system", *args]
    return ["sudo", "virt-install", *args]


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
        "DISK_SIZE_GIB": extract(r"\|\s*\*\*Virtual Storage\*\*\s*\|\s*(\d+)\s*GiB", "20"),
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
        if re.search(rf"(?m)^\s*Host\s+{re.escape(params['VM_NAME'])}(?:\s|$)", existing_config):
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
    ret, out, _ = run_cmd_status(virsh_cmd(host, "dominfo", vm_name), host=host)
    if ret != 0:
        return False, "not_found"

    state_match = re.search(r"^State:\s+([^\n]+)", out, re.MULTILINE)
    state = state_match.group(1).strip() if state_match else "unknown"
    return True, state


def resolve_storage_dir(host: str) -> str:
    """Finds an optimal libvirt storage directory on hypervisor (preferring 'airlock', then 'vm', then 'default')."""
    for pool_name in ("airlock", "vm", "default"):
        ret, out, _ = run_cmd_status(virsh_cmd(host, "pool-dumpxml", pool_name), host=host)
        if ret == 0:
            path_match = re.search(r"<path>([^<]+)</path>", out)
            if path_match:
                return path_match.group(1).strip()

    # Fallback to standard libvirt images path
    return "/var/lib/libvirt/images"


GOLDEN_IMAGE_FILENAME = "airlock-golden-ubuntu-24.04.qcow2"
GOLDEN_PACKAGES = [
    "docker.io",
    "containerd",
    "docker-compose-v2",
    "qemu-guest-agent",
    "python3",
    "python3-venv",
    "python3-pip",
    "nodejs",
    "npm",
    "git",
    "curl",
    "wget",
    "jq",
    "tmux",
    "htop",
    "ca-certificates",
    "rsync",
]


def resolve_base_image_info(
    host: str,
    target_dir: str,
    dry_run: bool = False,
    prefer_golden: bool = True,
) -> tuple[str, bool]:
    """Resolves the optimal backing image, preferring a pre-baked golden image when present.

    Returns:
        tuple[str, bool]: (image_path, is_golden)
    """
    if prefer_golden:
        golden_candidates = [
            f"{target_dir}/{GOLDEN_IMAGE_FILENAME}",
            f"/var/lib/libvirt/images/{GOLDEN_IMAGE_FILENAME}",
        ]
        for g_cand in golden_candidates:
            ret, _, _ = run_cmd_status(["test", "-f", g_cand], host=host)
            if ret == 0:
                print(f"    [✓] Using pre-baked golden image on {host}: {g_cand} (fast-boot enabled)")
                return g_cand, True

    return resolve_base_cloud_image(host, target_dir, dry_run=dry_run), False


def bake_golden_image(
    host: str,
    target_dir: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Pre-bakes a golden Ubuntu 24.04 qcow2 base image with Docker, Node.js, and Python."""
    storage_dir = target_dir or resolve_storage_dir(host)
    golden_path = f"{storage_dir}/{GOLDEN_IMAGE_FILENAME}"
    tmp_path = f"{storage_dir}/.{GOLDEN_IMAGE_FILENAME}.tmp"

    print("============================================================")
    print(f" AirlockVM Golden Base Image Baker ({host})")
    print(f" Target Image: {golden_path}")
    print("============================================================")

    if not force:
        ret_exists, _, _ = run_cmd_status(["test", "-f", golden_path], host=host)
        if ret_exists == 0:
            print(f"[✓] Golden image already exists on {host}: {golden_path}")
            print("    Use --force to rebuild it from scratch.")
            return 0

    ret_vc, _, _ = run_cmd_status(["which", "virt-customize"], host=host)
    if ret_vc != 0 and not dry_run:
        print(f"[-] Error: 'virt-customize' is not installed on hypervisor '{host}'.", file=sys.stderr)
        print("    Install it with: sudo apt update && sudo apt install -y libguestfs-tools", file=sys.stderr)
        return 1

    base_img = resolve_base_cloud_image(host, storage_dir, dry_run=dry_run)
    pkg_csv = ",".join(GOLDEN_PACKAGES)
    # Install Node.js 22 LTS (with bundled npm) from NodeSource instead of Ubuntu's older v18 package
    base_apt_pkgs = [p for p in GOLDEN_PACKAGES if p not in ("nodejs", "npm")] + ["gnupg"]
    pkg_args = " ".join(shlex.quote(p) for p in base_apt_pkgs)

    rootless = is_rootless_host(host)
    sudo_p = "" if rootless else "sudo "
    chown_step = "" if rootless else f"sudo chown libvirt-qemu:kvm {shlex.quote(tmp_path)} || true; "
    guest_bake_cmd = (
        "set -eu; "
        'if ! ip -4 addr show eth0 2>/dev/null | grep -q "inet "; then '
        "ip link set eth0 up && ip addr add 169.254.2.15/16 dev eth0 && ip route add default via 169.254.2.2; "
        "fi; "
        'if [ ! -s /etc/resolv.conf ]; then rm -f /etc/resolv.conf && printf "nameserver 169.254.2.3\\n" > /etc/resolv.conf; fi; '
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -qq; "
        "apt-get upgrade -y -qq -o Dpkg::Options::=--force-confnew; "
        "apt-get clean; "
        f"apt-get install -y -qq -o Dpkg::Options::=--force-confnew {pkg_args}; "
        "apt-get clean; "
        "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -; "
        "apt-get install -y -qq -o Dpkg::Options::=--force-confnew nodejs; "
        "curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh; "
        "mkdir -p /etc/skel/.local/bin && ln -sf /usr/local/bin/uv /etc/skel/.local/bin/uv && ln -sf /usr/local/bin/uvx /etc/skel/.local/bin/uvx; "
        "systemctl enable docker qemu-guest-agent; "
        "apt-get clean; "
        "if [ -d /run/systemd/resolve ] || command -v resolvectl >/dev/null 2>&1; then "
        "ln -sf ../run/systemd/resolve/stub-resolv.conf /etc/resolv.conf; "
        "fi; "
        "cloud-init clean --logs || true"
    )
    bake_script = (
        f"set -euo pipefail; "
        f"{sudo_p}rm -f {shlex.quote(tmp_path)}; "
        f"{sudo_p}cp {shlex.quote(base_img)} {shlex.quote(tmp_path)}; "
        f"{sudo_p}LIBGUESTFS_BACKEND=direct virt-customize --network -a {shlex.quote(tmp_path)} "
        f"--run-command {shlex.quote(guest_bake_cmd)} "
        f"--truncate /etc/machine-id; "
        f"{chown_step}"
        f"{sudo_p}chmod 664 {shlex.quote(tmp_path)} || true; "
        f"{sudo_p}mv {shlex.quote(tmp_path)} {shlex.quote(golden_path)}"
    )

    if dry_run:
        print(f"    [PLAN] Base Cloud Image: {base_img}")
        print(f"    [PLAN] Packages ({len(GOLDEN_PACKAGES)}): {pkg_csv}")
        print(f"    [PLAN] Run virt-customize on temporary copy and publish atomically to {golden_path}")
        print("[✓] Dry-run complete. Re-run without --dry-run to bake golden image.")
        return 0

    print(f"[*] Baking golden image on {host} (pre-installing {len(GOLDEN_PACKAGES)} packages)...")
    ret, out, err = run_cmd_status(["bash", "-c", bake_script], host=host)
    if ret != 0:
        print(f"[-] Golden image baking failed: {err or out}", file=sys.stderr)
        run_cmd_status(priv_cmd(host, "rm", "-f", tmp_path), host=host)
        return 1

    print(f"[✓] Golden image successfully baked on {host}: {golden_path}")
    print("    New VMs on this hypervisor will now clone from this golden image and skip package installation.")
    return 0


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
    sudo_p = "" if is_rootless_host(host) else "sudo "
    dl_cmd = f"{sudo_p}curl -fsSL -o {shlex.quote(dest_img)} {shlex.quote(url)}"
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
    ret, out, _ = run_cmd_status(virsh_cmd(host, "net-dumpxml", net_name), host=host)
    if ret != 0:
        print(f"    [-] Warning: Could not read libvirt network '{net_name}'. Skipping reservation update.")
        return

    if mac.lower() in out.lower() or ip in out:
        print(f"    [✓] Reservation already active: MAC={mac} -> IP={ip} ({name})")
        return

    print(f"    -> Adding reservation: MAC={mac} -> IP={ip} ({name})")
    xml_snippet = f"<host mac='{mac}' name='{name}' ip='{ip}'/>"
    update_cmd = virsh_cmd(host, "net-update", net_name, "add-last", "ip-dhcp-host", xml_snippet, "--live", "--config")

    if dry_run:
        print(f"    [PLAN] {' '.join(update_cmd)}")
    else:
        ret, _, err = run_cmd_status(update_cmd, host=host)
        if ret != 0:
            print(f"    [-] Warning adding DHCP reservation: {err}")
        else:
            print(f"    [✓] Static DHCP reservation registered successfully.")


def generate_cloud_init_seed(
    params: dict,
    pub_key: str,
    seed_iso_path: str,
    host: str,
    dry_run: bool = False,
    is_golden: bool = False,
) -> None:
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

    packages_block = ""
    if not is_golden:
        pkg_lines = "\n".join(f"  - {pkg}" for pkg in GOLDEN_PACKAGES)
        packages_block = f"\npackages:\n{pkg_lines}\n"

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
{secondary_block}{packages_block}
runcmd:
  - systemctl enable --now docker
  - systemctl enable --now qemu-guest-agent
"""

    print(f"[*] Generating Cloud-Init NoCloud seed on {host}...")
    if dry_run:
        print(f"    [PLAN] Build seed ISO: {seed_iso_path} with primary user '{params['VM_PRIMARY_USER']}'")
        return

    # Write files to a temporary location on hypervisor and build ISO
    rootless = is_rootless_host(host)
    sudo_p = "" if rootless else "sudo "
    chown_line = "" if rootless else f"sudo chown libvirt-qemu:kvm '{seed_iso_path}' || true\n"
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

{sudo_p}cloud-localds -N "$TMP_DIR/network-config" '{seed_iso_path}' "$TMP_DIR/user-data" "$TMP_DIR/meta-data"
{chown_line}{sudo_p}chmod 664 '{seed_iso_path}' || true
"""
    ret, _, err = run_cmd_status(["bash", "-c", build_script], host=host)
    if ret != 0:
        raise RuntimeError(f"Failed to generate cloud-init seed: {err}")
    print(f"    [✓] Cloud-init seed generated at {seed_iso_path}")


def provision_domain(params: dict, disk_path: str, seed_iso_path: str, host: str, dry_run: bool = False) -> None:
    """Executes virt-install to launch the virtual machine."""
    cmd = virt_install_cmd(
        host,
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
    )

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

    rootless = is_rootless_host(host)
    for ext_port, int_port, label in port_mappings:
        unit_name = f"{vm_name}-p{ext_port}-proxy"
        if rootless:
            socket_path = f"$HOME/.config/systemd/user/{unit_name}.socket"
            service_path = f"$HOME/.config/systemd/user/{unit_name}.service"
            display_socket = f"~/.config/systemd/user/{unit_name}.socket"
            display_service = f"~/.config/systemd/user/{unit_name}.service"
        else:
            socket_path = f"/etc/systemd/system/{unit_name}.socket"
            service_path = f"/etc/systemd/system/{unit_name}.service"
            display_socket = socket_path
            display_service = service_path

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
            print(f"    [PLAN] Configure {display_socket} (ListenStream={listen_address}:{ext_port})")
            print(f"    [PLAN] Configure {display_service}")
            if rootless:
                print(f"    [PLAN] systemctl --user daemon-reload && systemctl --user enable --now {unit_name}.socket")
            else:
                print(f"    [PLAN] sudo systemctl daemon-reload && sudo systemctl enable --now {unit_name}.socket")
            continue

        if rootless:
            setup_script = f"""
set -euo pipefail
mkdir -p "$HOME/.config/systemd/user"
cat << 'EOF' > "{socket_path}"
{socket_content}
EOF

cat << 'EOF' > "{service_path}"
{service_content}
EOF

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user daemon-reload
systemctl --user enable --now '{unit_name}.socket'
"""
        else:
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
    ret, out, _ = run_cmd_status(virsh_cmd(host, "snapshot-list", vm_name), host=host)
    if ret == 0 and snap_name in out:
        print(f"    [✓] Baseline snapshot '{snap_name}' already exists.")
        return

    cmd = virsh_cmd(
        host, "snapshot-create-as",
        "--domain", vm_name,
        "--name", snap_name,
        "--disk-only",
        "--description", snap_desc,
    )

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
        print(f"          1. systemd-socket-proxyd is inactive on {host} (check: ssh {host} 'systemctl status {vm_name}-p{ext_port}-proxy.socket')")
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
    ret, snap_out, _ = run_cmd_status(virsh_cmd(host, "snapshot-list", vm_name), host=host)
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

    # Check Hypervisor Proxy Socket (system or user systemd)
    proxy_unit = f"{vm_name}-p{ext_port}-proxy.socket"
    ret, sock_out, _ = run_cmd_status(
        [
            "bash", "-c",
            f"if [ \"$(systemctl is-active {shlex.quote(proxy_unit)} 2>/dev/null)\" = \"active\" ]; then echo active; "
            f"elif [ \"$(XDG_RUNTIME_DIR=\"/run/user/$(id -u)\" systemctl --user is-active {shlex.quote(proxy_unit)} 2>/dev/null)\" = \"active\" ]; then echo active; "
            f"elif [ \"$(systemctl is-active airlock-proxy.socket 2>/dev/null)\" = \"active\" ]; then echo active; "
            f"else echo inactive; fi",
        ],
        host=host,
    )
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

        sync_script = REPO_ROOT / ".agents" / "skills" / "airlock_rsync" / "scripts" / "generate_sync.py"
        if sync_script.exists():
            print("    -> Regenerating sync dispatcher (~/.airlock/bin/deploy_app_to_airlock.sh)...")
            if not dry_run:
                res = subprocess.run([sys.executable, str(sync_script)], capture_output=True, text=True)
                if res.returncode == 0:
                    print("    [✓] Sync dispatcher regenerated.")
                else:
                    print(f"    [-] Warning regenerating sync dispatcher: {res.stderr.strip() or res.stdout.strip()}")
            else:
                print(f"    [PLAN] Regenerate sync dispatcher: python3 {sync_script}")

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
    cmd = virsh_cmd(
        host, "snapshot-create-as",
        "--domain", vm_name,
        "--name", snap_name,
        "--disk-only",
        "--description", snap_desc,
    )
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
    cmd = virsh_cmd(host, "snapshot-revert", "--domain", vm_name, "--snapshotname", snap_name)
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


def _parse_snapshot_overlay_paths(snap_xml: str) -> list[tuple[str, str]]:
    """Extracts (target_dev, file_path) pairs from `virsh snapshot-dumpxml` output."""
    pairs: list[tuple[str, str]] = []
    if not snap_xml:
        return pairs
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(snap_xml)
        for disk in root.findall("./disks/disk"):
            target = disk.attrib.get("name", "").strip()
            src = disk.find("source")
            fpath = src.attrib.get("file", "").strip() if src is not None else ""
            if target and fpath:
                pairs.append((target, fpath))
    except Exception:
        pass
    return pairs


def _list_active_disks(vm_name: str, host: str) -> list[dict[str, str]]:
    """Returns active block device paths as [{'target': 'vda', 'source': '/path/...'}]."""
    rc, out, _ = run_cmd_status(virsh_cmd(host, "domblklist", vm_name, "--details"), host=host)
    disks: list[dict[str, str]] = []
    if rc != 0 or not out:
        return disks
    for line in out.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4 and parts[3].startswith("/"):
            disks.append({"target": parts[2], "source": parts[3]})
    return disks


def _collect_backing_chain_paths(active_disks: list[dict[str, str]], host: str) -> tuple[set[str], bool]:
    """Collects every file referenced in the active qcow2 backing chains on the hypervisor.

    Returns (referenced_paths, all_inspections_succeeded). Callers must NOT unlink overlay
    files if inspection failed (fail-closed safety).
    """
    referenced: set[str] = set()
    all_ok = True
    for d in active_disks:
        src = d.get("source", "")
        if not src:
            continue
        referenced.add(src)
        rc, out, err = run_cmd_status(priv_cmd(host, "qemu-img", "info", "-U", "--backing-chain", src), host=host)
        if rc != 0:
            all_ok = False
            print(f"    [-] Warning: Failed to inspect backing chain for '{src}': {err or out}", file=sys.stderr)
            continue
        if out:
            for line in out.splitlines():
                if line.startswith("image:"):
                    img_p = line.split(":", 1)[1].strip()
                    if img_p:
                        referenced.add(img_p)
                elif line.startswith("backing file:"):
                    bf = line.split(":", 1)[1].strip()
                    # Strip optional '(actual path: ...)' suffix
                    actual_m = re.search(r"\(actual path:\s*([^)]+)\)", bf)
                    if actual_m:
                        referenced.add(actual_m.group(1).strip())
                    else:
                        referenced.add(bf)
    return referenced, all_ok


def _get_ordered_backing_chain(disk_path: str, host: str) -> list[str]:
    """Returns the ordered qcow2 backing chain [active_leaf, parent, ..., root] for a disk.

    Raises RuntimeError if `qemu-img info -U --backing-chain` fails so callers never
    silently treat an unreadable backing chain as flat.
    """
    if not disk_path:
        return []
    rc, out, err = run_cmd_status(priv_cmd(host, "qemu-img", "info", "-U", "--backing-chain", disk_path), host=host)
    if rc != 0:
        raise RuntimeError(f"Failed to inspect backing chain for '{disk_path}': {err or out}")
    chain: list[str] = []
    if out:
        for line in out.splitlines():
            if line.startswith("image:"):
                img_p = line.split(":", 1)[1].strip()
                if img_p and img_p not in chain:
                    chain.append(img_p)
    if not chain:
        chain.append(disk_path)
    return chain


def _find_vm_base_disk(chain: list[str], protected_basenames: set[str]) -> str | None:
    """Finds the VM's own base disk in an ordered backing chain, stopping before shared golden/cloud images."""
    vm_owned = [p for p in chain if os.path.basename(p) not in protected_basenames]
    return vm_owned[-1] if vm_owned else (chain[-1] if chain else None)


def _refresh_vm_yaml_disks(
    config_path: Path | None,
    active_disks: list[dict[str, str]],
    new_baseline_name: str = "",
) -> bool:
    """Updates vm.disks[].source (and optionally blueprint.snapshot_base_name) in vm.yaml."""
    if not config_path or not config_path.exists() or config_path.suffix not in (".yaml", ".yml") or yaml is None:
        return False
    if not active_disks and not new_baseline_name:
        return False
    try:
        doc = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        vm_data = doc.get("vm", {})
        disks = vm_data.get("disks", [])
        changed = False
        if isinstance(disks, list) and active_disks:
            by_target = {d["target"]: d["source"] for d in active_disks}
            for entry in disks:
                if isinstance(entry, dict):
                    tgt = entry.get("target")
                    if tgt in by_target and entry.get("source") != by_target[tgt]:
                        entry["source"] = by_target[tgt]
                        changed = True
        if new_baseline_name:
            bp = vm_data.setdefault("blueprint", {})
            if bp.get("snapshot_base_name") != new_baseline_name:
                bp["snapshot_base_name"] = new_baseline_name
                changed = True
        if changed:
            config_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return changed
    except Exception:
        return False


def execute_snapshot_delete(
    params: dict,
    host: str,
    snapshot_name: str,
    force: bool = False,
    dry_run: bool = False,
    config_path: Path | None = None,
) -> int:
    """Safely deletes an external disk-only snapshot, merging/pivoting the qcow2 chain and pruning orphaned overlays."""
    vm_name = params["VM_NAME"]
    baseline_name = params.get("SNAPSHOT_BASE_NAME", "snapshot-base-clean")
    snap_name = (snapshot_name or "").strip()

    # Gate 1: Explicit, non-empty snapshot name required (never default to baseline)
    if not snap_name or snap_name == "default":
        print("[-] Error: Explicit snapshot_name is required for snapshot deletion (will never default to baseline).", file=sys.stderr)
        return 1

    # Gate 2: Protect baseline snapshot unless --force is explicitly passed
    if snap_name == baseline_name and not force:
        print(
            f"[-] Error: Refusing to delete baseline snapshot '{baseline_name}' without --force (or force=True).",
            file=sys.stderr,
        )
        return 1

    # Gate 3: Verify snapshot exists on hypervisor
    rc_list, list_out, list_err = run_cmd_status(virsh_cmd(host, "snapshot-list", vm_name, "--name"), host=host)
    if rc_list != 0:
        print(f"[-] Error querying snapshots for '{vm_name}' on {host}: {list_err}", file=sys.stderr)
        return 1

    existing_snaps = [ln.strip() for ln in list_out.splitlines() if ln.strip()]
    if snap_name not in existing_snaps:
        avail = ", ".join(existing_snaps) if existing_snaps else "(none)"
        print(
            f"[-] Error: Snapshot '{snap_name}' not found on domain '{vm_name}'. Available snapshots: {avail}",
            file=sys.stderr,
        )
        return 1

    # Capture target snapshot's overlay disk paths before deletion for post-delete orphan verification
    rc_sxml, snap_xml, _ = run_cmd_status(virsh_cmd(host, "snapshot-dumpxml", vm_name, snap_name), host=host)
    snap_overlays = _parse_snapshot_overlay_paths(snap_xml) if rc_sxml == 0 else []

    print(f"[*] Deleting snapshot '{snap_name}' on domain '{vm_name}' ({host})...")
    del_cmd = virsh_cmd(host, "snapshot-delete", "--domain", vm_name, "--snapshotname", snap_name)
    if dry_run:
        print(f"    [PLAN] {' '.join(del_cmd)}")
        if snap_overlays:
            print(f"    [PLAN] Verify backing chain and unlink any unreferenced overlay(s): {[p for _, p in snap_overlays]}")
        payload = {
            "deleted_snapshot": snap_name,
            "remaining_snapshots": [s for s in existing_snaps if s != snap_name],
            "active_disks": _list_active_disks(vm_name, host),
            "removed_overlays": [],
            "vm_yaml_updated": False,
            "dry_run": True,
        }
        print(f"AIRLOCK_SNAPSHOT_DELETE_JSON:{json.dumps(payload)}")
        return 0

    # Primary path: native libvirt external snapshot deletion (libvirt >= 9.0, e.g. Ubuntu 24.04 libvirt 10.0)
    ret, out, err = run_cmd_status(del_cmd, host=host)

    if ret != 0:
        err_lower = (err or "").lower()
        unsupported = (
            "unsupported" in err_lower
            or "external disk snapshot" in err_lower
            or "not supported" in err_lower
        )
        if not unsupported:
            print(f"[-] Error deleting snapshot '{snap_name}': {err or out}", file=sys.stderr)
            return 1

        # Fallback path for older libvirt (< 9.0): blockcommit active/mid-chain overlay, then delete --metadata
        print(f"    [*] Native external snapshot delete unsupported on {host}; falling back to blockcommit + --metadata...")
        exists, state = check_domain_status(vm_name, host)
        if not exists or state.lower() != "running":
            print(
                f"[-] Error: Fallback blockcommit requires domain '{vm_name}' to be running (current state: {state}).",
                file=sys.stderr,
            )
            return 1

        active_before = {d["target"]: d["source"] for d in _list_active_disks(vm_name, host)}
        for target_dev, overlay_path in snap_overlays:
            if active_before.get(target_dev) == overlay_path:
                bc_cmd = virsh_cmd(host, "blockcommit", vm_name, target_dev, "--active", "--pivot", "--wait")
            else:
                bc_cmd = virsh_cmd(host, "blockcommit", vm_name, target_dev, "--top", overlay_path, "--wait")
            rc_bc, _, err_bc = run_cmd_status(bc_cmd, host=host)
            if rc_bc != 0:
                print(f"[-] Error during blockcommit for '{target_dev}': {err_bc}", file=sys.stderr)
                return 1

        rc_meta, _, err_meta = run_cmd_status(
            virsh_cmd(host, "snapshot-delete", "--domain", vm_name, "--snapshotname", snap_name, "--metadata"),
            host=host,
        )
        if rc_meta != 0:
            print(f"[-] Error removing snapshot metadata for '{snap_name}': {err_meta}", file=sys.stderr)
            return 1

    # Query remaining snapshots & active disks post-delete
    rc_rem, rem_out, _ = run_cmd_status(virsh_cmd(host, "snapshot-list", vm_name, "--name"), host=host)
    remaining_snaps = [ln.strip() for ln in rem_out.splitlines() if ln.strip()] if rc_rem == 0 else []
    active_disks = _list_active_disks(vm_name, host)

    # Safely unlink any unreferenced overlay files left behind by the deleted snapshot
    protected_basenames = {
        GOLDEN_IMAGE_FILENAME,
        "ubuntu-24.04-minimal-cloudimg.img",
        "noble-server-cloudimg-amd64.img",
    }
    referenced, chain_ok = _collect_backing_chain_paths(active_disks, host)
    for rem_snap in remaining_snaps:
        rc_rxml, rxml, _ = run_cmd_status(virsh_cmd(host, "snapshot-dumpxml", vm_name, rem_snap), host=host)
        if rc_rxml == 0 and rxml:
            for _, rpath in _parse_snapshot_overlay_paths(rxml):
                referenced.add(rpath)

    removed_overlays: list[str] = []
    if not chain_ok:
        print("    [-] Skipping overlay unlink because backing-chain inspection failed (fail-closed).", file=sys.stderr)
    else:
        for _, cand_path in snap_overlays:
            if not cand_path or cand_path in referenced:
                continue
            if os.path.basename(cand_path) in protected_basenames:
                continue
            rc_rm, out_rm, _ = run_cmd_status(
                priv_cmd(host, "bash", "-c", f"if [ -e {shlex.quote(cand_path)} ]; then rm -f -- {shlex.quote(cand_path)} && echo REMOVED; fi"),
                host=host,
            )
            if rc_rm == 0 and "REMOVED" in out_rm:
                removed_overlays.append(cand_path)
                print(f"    [✓] Removed unreferenced overlay: {cand_path}")

    # Refresh vm.yaml disk source entries if active overlay paths changed
    vm_yaml_updated = _refresh_vm_yaml_disks(config_path, active_disks)
    if vm_yaml_updated:
        print(f"    [✓] Updated vm.yaml disk sources to match active backing chain.")

    print(f"[✓] Snapshot '{snap_name}' deleted successfully. Remaining snapshots: {len(remaining_snaps)}")
    payload = {
        "deleted_snapshot": snap_name,
        "remaining_snapshots": remaining_snaps,
        "active_disks": active_disks,
        "removed_overlays": removed_overlays,
        "vm_yaml_updated": vm_yaml_updated,
        "dry_run": False,
    }
    print(f"AIRLOCK_SNAPSHOT_DELETE_JSON:{json.dumps(payload)}")
    return 0


def execute_snapshot_flatten(
    params: dict,
    host: str,
    snapshot_name: str = "",
    force: bool = False,
    dry_run: bool = False,
    config_path: Path | None = None,
) -> int:
    """Flattens a VM's external qcow2 snapshot chain into its VM-owned base disk, prunes all prior
    snapshot metadata and unreferenced overlays, and optionally creates one fresh baseline snapshot."""
    vm_name = params["VM_NAME"]
    new_snap = (snapshot_name or "").strip()
    if new_snap == "default":
        new_snap = ""

    protected_basenames = {
        GOLDEN_IMAGE_FILENAME,
        "ubuntu-24.04-minimal-cloudimg.img",
        "noble-server-cloudimg-amd64.img",
    }

    # Step 1: Inspect existing snapshots and active disk backing chains
    rc_list, list_out, list_err = run_cmd_status(virsh_cmd(host, "snapshot-list", vm_name, "--name"), host=host)
    if rc_list != 0:
        print(f"[-] Error querying snapshots for '{vm_name}' on {host}: {list_err}", file=sys.stderr)
        return 1

    existing_snaps = [ln.strip() for ln in list_out.splitlines() if ln.strip()]
    active_disks = _list_active_disks(vm_name, host)

    commit_plan: list[dict[str, Any]] = []
    candidate_overlays: list[str] = []
    seen_candidates: set[str] = set()

    def _add_candidate(path_str: str) -> None:
        if path_str and path_str not in seen_candidates and os.path.basename(path_str) not in protected_basenames:
            seen_candidates.add(path_str)
            candidate_overlays.append(path_str)

    for d in active_disks:
        src = d["source"]
        try:
            chain = _get_ordered_backing_chain(src, host)
        except RuntimeError as exc:
            print(f"[-] Error: {exc}. Aborting flatten to avoid corrupting active overlays.", file=sys.stderr)
            return 1
        vm_base = _find_vm_base_disk(chain, protected_basenames)
        if vm_base and src != vm_base:
            base_idx = chain.index(vm_base)
            disk_overlays = chain[:base_idx]
            commit_plan.append({
                "target": d["target"],
                "active": src,
                "base": vm_base,
                "overlays": disk_overlays,
            })
            for ov in disk_overlays:
                _add_candidate(ov)

    for snap in existing_snaps:
        rc_sxml, sxml, _ = run_cmd_status(virsh_cmd(host, "snapshot-dumpxml", vm_name, snap), host=host)
        if rc_sxml == 0 and sxml:
            for _, ov_path in _parse_snapshot_overlay_paths(sxml):
                _add_candidate(ov_path)

    # Early no-op if no snapshots and no overlays above VM base disk
    if not existing_snaps and not commit_plan:
        print(f"[✓] Domain '{vm_name}' has no snapshots or overlays to flatten (already flat).")
        payload = {
            "changed": False,
            "removed_snapshots": [],
            "unlinked_files": [],
            "active_disks": active_disks,
            "new_snapshot": "",
            "remaining_snapshots": [],
            "vm_yaml_updated": False,
            "dry_run": dry_run,
        }
        print(f"AIRLOCK_SNAPSHOT_FLATTEN_JSON:{json.dumps(payload)}")
        return 0

    print(f"[*] Flattening snapshot chain for domain '{vm_name}' on {host} ({len(existing_snaps)} snapshot(s), {len(commit_plan)} disk(s) with overlays)...")

    if dry_run:
        for item in commit_plan:
            print(
                f"    [PLAN] {' '.join(virsh_cmd(host, 'blockcommit', vm_name, item['target'], '--base', item['base'], '--active', '--pivot', '--wait'))}"
            )
        for snap in reversed(existing_snaps):
            print(f"    [PLAN] {' '.join(virsh_cmd(host, 'snapshot-delete', '--domain', vm_name, '--snapshotname', snap, '--metadata'))}")
        if candidate_overlays:
            print(f"    [PLAN] Verify unreferenced and unlink overlay(s): {candidate_overlays}")
        if new_snap:
            print(f"    [PLAN] {' '.join(virsh_cmd(host, 'snapshot-create-as', '--domain', vm_name, '--name', new_snap, '--disk-only'))}")
        payload = {
            "changed": True,
            "removed_snapshots": list(existing_snaps),
            "unlinked_files": candidate_overlays,
            "active_disks": active_disks,
            "new_snapshot": new_snap,
            "remaining_snapshots": [new_snap] if new_snap else [],
            "vm_yaml_updated": False,
            "dry_run": True,
        }
        print(f"AIRLOCK_SNAPSHOT_FLATTEN_JSON:{json.dumps(payload)}")
        return 0

    # Force gate: flattening discards all existing rollback points (including baseline)
    if not force:
        print(
            f"[-] Error: Flattening discards all {len(existing_snaps)} existing rollback point(s) "
            f"({', '.join(existing_snaps) or 'overlays'}). Re-run with --force (or force=True) to confirm, or --dry-run to preview.",
            file=sys.stderr,
        )
        return 1

    # Step 2: Live blockcommit requires domain to be running
    if commit_plan:
        exists, state = check_domain_status(vm_name, host)
        if not exists or state.lower() != "running":
            print(
                f"[-] Error: Live blockcommit flattening requires domain '{vm_name}' to be running "
                f"(current state: {state}). Start the VM first with 'airlock power {vm_name} start' or 'virsh start {vm_name}'.",
                file=sys.stderr,
            )
            return 1

    # Step 3: Collapse backing chain per disk into each VM-owned base disk (never touching shared golden/cloud images)
    for item in commit_plan:
        bc_cmd = virsh_cmd(
            host, "blockcommit", vm_name, item["target"],
            "--base", item["base"],
            "--active", "--pivot", "--wait",
        )
        print(f"    -> Committing '{item['target']}' overlays into base '{item['base']}'...")
        rc_bc, out_bc, err_bc = run_cmd_status(bc_cmd, host=host)
        if rc_bc != 0:
            print(f"[-] Error during blockcommit for '{item['target']}': {err_bc or out_bc}", file=sys.stderr)
            return 1
        print(f"    [✓] Pivoted '{item['target']}' onto '{item['base']}'.")

    # Step 4: Delete all old libvirt snapshot metadata records (leaf-first)
    removed_snapshots: list[str] = []
    for snap in reversed(existing_snaps):
        rc_meta, _, err_meta = run_cmd_status(
            virsh_cmd(host, "snapshot-delete", "--domain", vm_name, "--snapshotname", snap, "--metadata"),
            host=host,
        )
        if rc_meta != 0:
            print(f"[-] Warning: Failed to delete metadata for snapshot '{snap}': {err_meta}", file=sys.stderr)
        else:
            removed_snapshots.append(snap)
            print(f"    [✓] Removed snapshot metadata: {snap}")

    # Step 5: Verify active backing chain & unlink only verified-unreferenced overlay files
    post_commit_disks = _list_active_disks(vm_name, host)
    referenced, chain_ok = _collect_backing_chain_paths(post_commit_disks, host)
    unlinked_files: list[str] = []
    if not chain_ok:
        print("    [-] Skipping overlay unlink because post-commit backing-chain inspection failed (fail-closed).", file=sys.stderr)
    else:
        for cand_path in candidate_overlays:
            if not cand_path or cand_path in referenced or os.path.basename(cand_path) in protected_basenames:
                continue
            rc_rm, out_rm, _ = run_cmd_status(
                priv_cmd(host, "bash", "-c", f"if [ -e {shlex.quote(cand_path)} ]; then rm -f -- {shlex.quote(cand_path)} && echo REMOVED; fi"),
                host=host,
            )
            if rc_rm == 0 and "REMOVED" in out_rm:
                unlinked_files.append(cand_path)
                print(f"    [✓] Unlinked unreferenced overlay: {cand_path}")

    # Step 6: Optionally create one fresh baseline snapshot on top of the flattened base disk
    created_snap = ""
    if new_snap:
        snap_desc = f"AirlockVM flattened baseline snapshot '{new_snap}'"
        rc_new, out_new, err_new = run_cmd_status(
            virsh_cmd(
                host, "snapshot-create-as",
                "--domain", vm_name,
                "--name", new_snap,
                "--disk-only",
                "--description", snap_desc,
            ),
            host=host,
        )
        if rc_new != 0:
            print(f"[-] Error creating fresh baseline snapshot '{new_snap}': {err_new or out_new}", file=sys.stderr)
            return 1
        created_snap = new_snap
        print(f"    [✓] Created fresh baseline snapshot '{new_snap}'.")

    # Step 7: Refresh vm.yaml disk sources (and snapshot_base_name if a new baseline was created)
    final_disks = _list_active_disks(vm_name, host)
    rc_rem, rem_out, _ = run_cmd_status(virsh_cmd(host, "snapshot-list", vm_name, "--name"), host=host)
    remaining_snaps = [ln.strip() for ln in rem_out.splitlines() if ln.strip()] if rc_rem == 0 else []
    vm_yaml_updated = _refresh_vm_yaml_disks(config_path, final_disks, new_baseline_name=created_snap)
    if vm_yaml_updated:
        print("    [✓] Updated vm.yaml disk sources and baseline metadata.")

    print(
        f"[✓] Flattened '{vm_name}': removed {len(removed_snapshots)} snapshot(s), "
        f"unlinked {len(unlinked_files)} overlay(s), remaining snapshots: {len(remaining_snaps)}."
    )
    payload = {
        "changed": True,
        "removed_snapshots": removed_snapshots,
        "unlinked_files": unlinked_files,
        "active_disks": final_disks,
        "new_snapshot": created_snap,
        "remaining_snapshots": remaining_snaps,
        "vm_yaml_updated": vm_yaml_updated,
        "dry_run": False,
    }
    print(f"AIRLOCK_SNAPSHOT_FLATTEN_JSON:{json.dumps(payload)}")
    return 0


def execute_power_action(params: dict, action: str) -> None:
    """Controls VM lifecycle (start, stop, reboot, destroy)."""
    vm_name = params["VM_NAME"]
    host = params["HYPERVISOR_HOST"]

    action_map = {
        "start": virsh_cmd(host, "start", vm_name),
        "stop": virsh_cmd(host, "shutdown", vm_name),
        "destroy": virsh_cmd(host, "destroy", vm_name),
        "reboot": virsh_cmd(host, "reboot", vm_name),
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


def remove_static_dhcp(params: dict, host: str, dry_run: bool = False) -> None:
    """Removes the VM's static DHCP reservation from the libvirt network."""
    net_name = params.get("LIBVIRT_NETWORK", "default")
    mac = (params.get("VM_MAC") or "").strip()
    ip = (params.get("VM_IP") or "").strip()
    vm_name = params["VM_NAME"]

    print(f"    -> Removing static DHCP reservation in libvirt network '{net_name}' on {host}...")
    ret, out, _ = run_cmd_status(virsh_cmd(host, "net-dumpxml", net_name), host=host)
    matched_snippets: list[str] = []
    if ret == 0 and out:
        for m in re.finditer(r"<host\b[^>]*/>", out):
            tag = m.group(0)
            mac_m = re.search(r"mac=['\"]([^'\"]+)['\"]", tag, re.IGNORECASE)
            name_m = re.search(r"name=['\"]([^'\"]+)['\"]", tag)
            ip_m = re.search(r"ip=['\"]([^'\"]+)['\"]", tag)
            tag_mac = mac_m.group(1).lower() if mac_m else ""
            tag_name = name_m.group(1) if name_m else ""
            tag_ip = ip_m.group(1) if ip_m else ""
            if (mac and tag_mac == mac.lower()) or (tag_name == vm_name) or (ip and tag_ip == ip):
                matched_snippets.append(tag)

    if not matched_snippets:
        if dry_run and mac and ip:
            fallback_xml = f"<host mac='{mac}' name='{vm_name}' ip='{ip}'/>"
            print(f"    [PLAN] {' '.join(virsh_cmd(host, 'net-update', net_name, 'delete', 'ip-dhcp-host', fallback_xml, '--live', '--config'))}")
        else:
            print(f"    [✓] No static DHCP reservation found for '{vm_name}' in '{net_name}'.")
        return

    for xml_snippet in matched_snippets:
        cmd_live_cfg = virsh_cmd(host, "net-update", net_name, "delete", "ip-dhcp-host", xml_snippet, "--live", "--config")
        if dry_run:
            print(f"    [PLAN] {' '.join(cmd_live_cfg)}")
            continue
        rc, _, err = run_cmd_status(cmd_live_cfg, host=host)
        if rc != 0:
            # Fallback to --config only if network is inactive
            rc2, _, err2 = run_cmd_status(
                virsh_cmd(host, "net-update", net_name, "delete", "ip-dhcp-host", xml_snippet, "--config"),
                host=host,
            )
            if rc2 != 0:
                print(f"    [-] Warning removing DHCP reservation ({xml_snippet}): {err2 or err}")
                continue
        print(f"    [✓] Removed static DHCP reservation: {xml_snippet}")


def _remove_ssh_config_host_block(config_text: str, vm_name: str) -> tuple[str, bool]:
    """Removes the exact 'Host <vm_name>' block (and preceding AirlockVM comment) from SSH config text."""
    lines = config_text.splitlines(keepends=True)
    result: list[str] = []
    removed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s*Host\s+(.+)$", line)
        if m:
            hosts = m.group(1).strip().split()
            if hosts == [vm_name]:
                removed = True
                # Also drop an immediately preceding AirlockVM comment line (and one blank line before it)
                if result and result[-1].strip() == "# AirlockVM Execution Virtual Machine":
                    result.pop()
                if result and result[-1].strip() == "":
                    result.pop()
                # Skip all indented/blank lines belonging to this Host block until the next Host/Match directive
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    if re.match(r"^\s*(?:Host|Match)\s+", nxt):
                        break
                    if nxt.strip().startswith("#") and not nxt.startswith((" ", "\t")):
                        break
                    i += 1
                continue
        result.append(line)
        i += 1

    cleaned = "".join(result)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, removed


def _ssh_key_used_by_other_vms(key_path: Path, exclude_vm: str) -> bool:
    """Returns True if any other VM in inventory references the same SSH private key path."""
    inv_dir = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))
    if not inv_dir.exists() or yaml is None:
        return False
    target_resolved = key_path.expanduser().resolve()
    for yf in inv_dir.glob("*/*/vm.yaml"):
        if yf.parent.name == exclude_vm:
            continue
        try:
            doc = yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
            other_key = doc.get("vm", {}).get("blueprint", {}).get("ssh_key_path", "")
            if other_key and Path(os.path.expanduser(str(other_key))).resolve() == target_resolved:
                return True
        except Exception:
            continue
    return False


def cleanup_workstation_ssh(params: dict, purge_ssh_keys: bool = False, dry_run: bool = False) -> None:
    """Cleans up workstation known_hosts, ~/.ssh/config host block, and optionally the VM SSH keypair."""
    vm_name = params["VM_NAME"]
    vm_ip = params.get("VM_IP", "")
    ssh_dir = Path.home() / ".ssh"

    # 1. Clean up ~/.ssh/known_hosts
    print(f"    -> Cleaning up workstation ~/.ssh/known_hosts for '{vm_name}' and '{vm_ip}'...")
    if not dry_run:
        subprocess.run(["ssh-keygen", "-R", vm_name], capture_output=True)
        if vm_ip:
            subprocess.run(["ssh-keygen", "-R", vm_ip], capture_output=True)
        print("    [✓] Workstation known_hosts cleaned.")
    else:
        print(f"    [PLAN] ssh-keygen -R {vm_name} && ssh-keygen -R {vm_ip}")

    # 2. Remove Host <vm_name> block from ~/.ssh/config
    ssh_config_file = ssh_dir / "config"
    if ssh_config_file.exists():
        existing_text = ssh_config_file.read_text(encoding="utf-8")
        new_text, removed = _remove_ssh_config_host_block(existing_text, vm_name)
        if removed:
            print(f"    -> Removing 'Host {vm_name}' block from ~/.ssh/config...")
            if dry_run:
                print(f"    [PLAN] Remove 'Host {vm_name}' block from {ssh_config_file}")
            else:
                ssh_config_file.write_text(new_text, encoding="utf-8")
                ssh_config_file.chmod(0o600)
                print(f"    [✓] Removed 'Host {vm_name}' block from ~/.ssh/config.")

    # 3. Optionally remove dedicated VM SSH keypair
    raw_key = params.get("SSH_KEY_PATH", "")
    if not raw_key:
        return
    key_path = Path(os.path.expanduser(raw_key))
    pub_path = Path(f"{key_path}.pub") if not str(key_path).endswith(".pub") else key_path
    protected_names = {"id_ed25519", "id_rsa", "id_ecdsa", "id_dsa"}

    if not purge_ssh_keys:
        if key_path.exists() or pub_path.exists():
            print(f"    [*] Retaining workstation SSH keypair '{raw_key}' (re-run with --purge-ssh-keys to delete).")
        return

    if key_path.name in protected_names:
        print(f"    [-] Skipping deletion of standard workstation key '{key_path}'.")
        return

    if _ssh_key_used_by_other_vms(key_path, exclude_vm=vm_name):
        print(f"    [-] Retaining SSH keypair '{raw_key}' because another VM in inventory references it.")
        return

    for kp in (key_path, pub_path):
        if kp.exists():
            if dry_run:
                print(f"    [PLAN] Delete SSH key file: {kp}")
            else:
                kp.unlink(missing_ok=True)
                print(f"    [✓] Deleted SSH key file: {kp}")


def execute_purge(params: dict, host: str, dry_run: bool = False, purge_ssh_keys: bool = False) -> None:
    """Entirely removes the VM domain, nvram, snapshots, DHCP lease, proxies, SSH config, and disk storage."""
    vm_name = params["VM_NAME"]
    target_storage = resolve_storage_dir(host)

    print(f"[*] Entirely removing AirlockVM VM '{vm_name}' and its storage from '{host}'...")

    # Collect live block device paths before undefining so custom disk/snapshot paths are also purged
    protected_basenames = {
        GOLDEN_IMAGE_FILENAME,
        "ubuntu-24.04-minimal-cloudimg.img",
        "noble-server-cloudimg-amd64.img",
    }
    discovered_disks: list[str] = []
    exists, state = check_domain_status(vm_name, host)
    if exists:
        if not dry_run:
            rc_blk, blk_out, _ = run_cmd_status(virsh_cmd(host, "domblklist", vm_name, "--details"), host=host)
            if rc_blk == 0 and blk_out:
                for line in blk_out.splitlines()[2:]:
                    parts = line.split()
                    if len(parts) >= 4 and parts[3].startswith("/"):
                        p = parts[3]
                        if os.path.basename(p) not in protected_basenames:
                            discovered_disks.append(p)

        if state.lower() == "running":
            print(f"    -> Destroying running domain '{vm_name}'...")
            if not dry_run:
                run_cmd_status(virsh_cmd(host, "destroy", vm_name), host=host)
            else:
                print(f"    [PLAN] {' '.join(virsh_cmd(host, 'destroy', vm_name))}")

        print(f"    -> Undefining domain '{vm_name}' (NVRAM + snapshot metadata)...")
        if not dry_run:
            ret, _, err = run_cmd_status(virsh_cmd(host, "undefine", vm_name, "--nvram", "--snapshots-metadata"), host=host)
            if ret != 0:
                run_cmd_status(virsh_cmd(host, "undefine", vm_name, "--nvram"), host=host)
            print(f"    [✓] Domain undefined.")
        else:
            print(f"    [PLAN] {' '.join(virsh_cmd(host, 'undefine', vm_name, '--nvram', '--snapshots-metadata'))}")
    else:
        print(f"    -> Domain '{vm_name}' is not registered in libvirt.")

    # Include any disk paths documented in vm.yaml
    for src in params.get("DISK_SOURCES", []):
        if src.startswith("/") and os.path.basename(src) not in protected_basenames and src not in discovered_disks:
            discovered_disks.append(src)

    # Remove disk storage, seed ISOs, and snapshot overlays.
    # IMPORTANT: Execute glob expansion inside priv_cmd("bash", "-c", ...) so root (or rootless pool owner)
    # expands globs directly inside the storage directory.
    q_dir = shlex.quote(target_storage)
    q_vm = shlex.quote(vm_name)
    extra_paths_bash = " ".join(shlex.quote(p) for p in discovered_disks)
    storage_script = (
        f"shopt -s nullglob; "
        f"files=({q_dir}/{q_vm}.qcow2 {q_dir}/{q_vm}-seed.iso {q_dir}/{q_vm}.* {q_dir}/{q_vm}-seed.* {extra_paths_bash}); "
        f"removed=0; "
        f"declare -A seen=(); "
        f"for f in \"${{files[@]}}\"; do "
        f"  [ -n \"$f\" ] || continue; "
        f"  [ -z \"${{seen[$f]:-}}\" ] || continue; "
        f"  seen[\"$f\"]=1; "
        f"  if [ -e \"$f\" ] || [ -L \"$f\" ]; then "
        f"    rm -f -- \"$f\" && echo \"REMOVED:$f\" && removed=$((removed+1)); "
        f"  fi; "
        f"done; "
        f"leftover=(); "
        f"for f in {q_dir}/{q_vm}.qcow2 {q_dir}/{q_vm}-seed.iso {q_dir}/{q_vm}.* {q_dir}/{q_vm}-seed.*; do "
        f"  [ -e \"$f\" ] && leftover+=(\"$f\"); "
        f"done; "
        f"if [ ${{#leftover[@]}} -gt 0 ]; then "
        f"  echo \"LEFTOVER:${{leftover[*]}}\" >&2; exit 1; "
        f"fi; "
        f"echo \"TOTAL_REMOVED:$removed\""
    )

    print(f"    -> Deleting VM storage images and snapshots in {target_storage} for '{vm_name}'...")
    if not dry_run:
        ret, out, err = run_cmd_status(priv_cmd(host, "bash", "-c", storage_script), host=host)
        removed_files = [ln[len("REMOVED:"):] for ln in out.splitlines() if ln.startswith("REMOVED:")]
        if ret == 0:
            if removed_files:
                print(f"    [✓] Purged {len(removed_files)} storage/snapshot file(s):")
                for rf in removed_files:
                    print(f"        - {rf}")
            else:
                print("    [✓] No storage disk or snapshot files found to remove.")
        else:
            print(f"    [-] Warning: Storage cleanup incomplete: {err or out}")
    else:
        print(f"    [PLAN] {' '.join(priv_cmd(host, 'bash', '-c'))} \"shopt -s nullglob; rm -f {target_storage}/{vm_name}.qcow2 {target_storage}/{vm_name}-seed.iso {target_storage}/{vm_name}.* {target_storage}/{vm_name}-seed.*\"")

    # Remove libvirt static DHCP reservation
    remove_static_dhcp(params, host, dry_run=dry_run)

    # Clean up workstation known_hosts, ~/.ssh/config host block, and optionally SSH keys
    cleanup_workstation_ssh(params, purge_ssh_keys=purge_ssh_keys, dry_run=dry_run)

    # Clean up hypervisor proxy forwarders for this VM
    print(f"    -> Cleaning up hypervisor proxy forwarders for '{vm_name}'...")
    legacy_unit = " airlock-proxy.socket" if vm_name == "airlock-vm" else ""
    rootless = is_rootless_host(host)
    if not dry_run:
        if rootless:
            cleanup_proxy_script = (
                f"shopt -s nullglob; "
                f"export XDG_RUNTIME_DIR=\"/run/user/$(id -u)\"; "
                f"systemctl --user stop {q_vm}-p*-proxy.socket {q_vm}-p*-proxy.service {q_vm}-proxy.socket{legacy_unit} 2>/dev/null || true; "
                f"systemctl --user disable {q_vm}-p*-proxy.socket {q_vm}-proxy.socket{legacy_unit} 2>/dev/null || true; "
                f"rm -f \"$HOME\"/.config/systemd/user/{q_vm}-p*-proxy.* \"$HOME\"/.config/systemd/user/{q_vm}-proxy.* 2>/dev/null || true; "
                f"systemctl --user daemon-reload 2>/dev/null || true"
            )
            run_cmd_status(["bash", "-c", cleanup_proxy_script], host=host)
        else:
            cleanup_proxy_script = (
                f"shopt -s nullglob; "
                f"systemctl stop {q_vm}-p*-proxy.socket {q_vm}-p*-proxy.service {q_vm}-proxy.socket{legacy_unit} 2>/dev/null || true; "
                f"systemctl disable {q_vm}-p*-proxy.socket {q_vm}-proxy.socket{legacy_unit} 2>/dev/null || true; "
                f"rm -f /etc/systemd/system/{q_vm}-p*-proxy.* /etc/systemd/system/{q_vm}-proxy.* 2>/dev/null || true; "
                f"systemctl daemon-reload 2>/dev/null || true"
            )
            run_cmd_status(["sudo", "bash", "-c", cleanup_proxy_script], host=host)
        print(f"    [✓] Hypervisor proxy sockets cleaned.")
    else:
        if rootless:
            print(f"    [PLAN] systemctl --user stop {vm_name}-p*-proxy.socket && rm -f ~/.config/systemd/user/{vm_name}-p*-proxy.*")
        else:
            print(f"    [PLAN] sudo systemctl stop {vm_name}-p*-proxy.socket && rm -f /etc/systemd/system/{vm_name}-p*-proxy.*")

    # Clean up inventory record and regenerate sync dispatcher
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
    parser.add_argument("--purge", action="store_true", help="Entirely remove the VM domain, nvram, snapshots, DHCP reservation, proxies, SSH config, and disk storage")
    parser.add_argument("--purge-ssh-keys", action="store_true", help="When used with --purge, also delete the dedicated workstation SSH keypair (~/.ssh/<app>_vm_ed25519*)")
    parser.add_argument("--reboot", action="store_true", help="Reboot the VM domain")
    parser.add_argument("--snapshot", nargs="?", const="default", help="Create a snapshot (optional snapshot name)")
    parser.add_argument("--rollback", nargs="?", const="default", help="Revert to baseline or named snapshot")
    parser.add_argument("--delete-snapshot", type=str, metavar="SNAPSHOT_NAME", help="Safely delete a named external disk snapshot (requires --force for baseline snapshot)")
    parser.add_argument("--flatten-snapshots", nargs="?", const="", metavar="NEW_BASELINE_NAME", help="Consolidate external snapshot chain into VM base disk, remove old overlays/metadata, and optionally create a fresh baseline snapshot (requires --force)")
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-confirm deployment without interactive prompt")
    parser.add_argument("--vm", type=str, help="Target VM domain name in inventory (e.g. airlock-vm)")
    parser.add_argument("--config", type=Path, default=None, help="Path to VM specification YAML (default: auto-detected from inventory)")
    parser.add_argument("--doc", type=Path, default=None, help="Path to supplementary runbook markdown or custom specification")
    parser.add_argument("--boot-timeout", type=int, default=240, help="Seconds to wait for guest SSH readiness after provisioning (default: 240)")
    parser.add_argument("--bake-golden", action="store_true", help="Pre-bake a golden Ubuntu 24.04 qcow2 base image on the hypervisor")
    parser.add_argument("--host", type=str, default="localhost", help="Target hypervisor host for --bake-golden (default: localhost)")
    parser.add_argument("--force", action="store_true", help="Force overwrite when used with --bake-golden")
    parser.add_argument("--no-golden", action="store_true", help="Ignore pre-baked golden image and deploy from raw Ubuntu cloud image")
    args = parser.parse_args()

    # Handle standalone --bake-golden before requiring a vm.yaml
    if args.bake_golden:
        sys.exit(bake_golden_image(host=args.host, dry_run=args.dry_run, force=args.force))

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
        execute_purge(params, host, dry_run=args.dry_run, purge_ssh_keys=args.purge_ssh_keys)
        sys.exit(0)
    if args.reboot:
        execute_power_action(params, "reboot")
        sys.exit(0)

    # Handle snapshot / rollback / delete-snapshot actions
    if args.snapshot is not None:
        sys.exit(execute_snapshot(params, host, snapshot_name=args.snapshot, dry_run=args.dry_run))
    if args.rollback is not None:
        sys.exit(execute_rollback(params, host, snapshot_name=args.rollback, dry_run=args.dry_run))
    if args.delete_snapshot is not None:
        sys.exit(
            execute_snapshot_delete(
                params,
                host,
                snapshot_name=args.delete_snapshot,
                force=args.force,
                dry_run=args.dry_run,
                config_path=config_path,
            )
        )
    if args.flatten_snapshots is not None:
        sys.exit(
            execute_snapshot_flatten(
                params,
                host,
                snapshot_name=args.flatten_snapshots,
                force=args.force,
                dry_run=args.dry_run,
                config_path=config_path,
            )
        )

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
    base_image, is_golden = resolve_base_image_info(
        host,
        target_storage,
        dry_run=args.dry_run,
        prefer_golden=not args.no_golden,
    )

    disk_path = f"{target_storage}/{vm_name}.qcow2"
    seed_iso_path = f"{target_storage}/{vm_name}-seed.iso"

    print(f"[*] Preparing VM disk image: {disk_path} ({params['DISK_SIZE_GIB']} GiB)...")
    rootless = is_rootless_host(host)
    if args.dry_run:
        prefix = "" if rootless else "sudo "
        print(f"    [PLAN] {prefix}qemu-img create -f qcow2 -b '{base_image}' -F qcow2 '{disk_path}' {params['DISK_SIZE_GIB']}G")
    else:
        if rootless:
            create_disk_cmd = f"qemu-img create -f qcow2 -b '{base_image}' -F qcow2 '{disk_path}' {params['DISK_SIZE_GIB']}G && chmod 664 '{disk_path}'"
        else:
            create_disk_cmd = f"sudo qemu-img create -f qcow2 -b '{base_image}' -F qcow2 '{disk_path}' {params['DISK_SIZE_GIB']}G && sudo chown libvirt-qemu:kvm '{disk_path}' && sudo chmod 664 '{disk_path}'"
        ret, _, err = run_cmd_status(["bash", "-c", create_disk_cmd], host=host)
        if ret != 0:
            raise RuntimeError(f"Failed to create VM disk: {err}")
        print(f"    [✓] QCOW2 disk created backed by {base_image}")

    # Step 4: Cloud-Init NoCloud Seed Generation
    generate_cloud_init_seed(params, pub_key, seed_iso_path, host, dry_run=args.dry_run, is_golden=is_golden)

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
                rc_ip, ip_out, _ = run_cmd_status(virsh_cmd(host, "domifaddr", vm_name), host=host)
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
