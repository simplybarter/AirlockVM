#!/usr/bin/env python3
"""
AirlockVM VM Configuration Script
Facilitates customizing inventory VM runbooks (inventory/<vmhost>/<vm>/VM.md) from a parameterized template.
"""

import argparse
import difflib
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_TEMPLATE_PATH = SKILL_DIR / "template" / "VM.md.template"

# Sensible blueprint defaults for standalone bootstrapping (when no inventory exists yet)
DEFAULT_BLUEPRINT = {
    "PROJECT_NAME": "",
    "DEV_HOST": "",
    "DEV_HOST_TITLE": "",
    "VM_PRIMARY_USER": "airlock",
    "VM_SECONDARY_USER": "",
    "SSH_KEY_PATH": "",
    "APP_INTERNAL_PORT": "3000",
    "PROXY_EXTERNAL_PORT": "3001",
    "PROXY_SOCKET_SERVICE": "",
    "REMOTE_CODE_DIR": "",
    "GUEST_OS": "Ubuntu 24.04 LTS Minimal",
    "GUEST_OS_DETAILS": "Pristine Noble Numbat cloud image",
    "CPU_MODE": "host-passthrough",
    "BALLOON_DRIVER": "virtio-balloon",
    "DISK_SIZE_GIB": "20",
    "DISK_FORMAT": "qcow2",
    "UEFI_FIRMWARE": "OVMF_CODE_4M.ms.fd",
    "SNAPSHOT_BASE_NAME": "snapshot-base-clean",
    "SNAPSHOT_BASE_DESC": "Pristine base setup with Docker and Python",
}


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

PARAM_DESCRIPTIONS = {
    "PROJECT_NAME": "Project Display Name (e.g. MyProject)",
    "VM_NAME": "VM Domain Name in libvirt (e.g. myproject-vm)",
    "HYPERVISOR_HOST": "Hypervisor Hostname/Alias or IP (e.g. hypervisor-host or localhost)",
    "HYPERVISOR_OS": "Hypervisor Operating System (e.g. Ubuntu 24.04 LTS)",
    "HYPERVISOR_SPECS": "Hypervisor Hardware Description (e.g. 8 Cores CPU | 16 GB RAM | KVM / libvirt)",
    "HYPERVISOR_LAN_IP": "Hypervisor Physical LAN IP (e.g. 192.168.1.100)",
    "DEV_HOST": "Development Hostname (e.g. dev-workstation)",
    "GUEST_OS": "Guest OS Minimal Image (e.g. Ubuntu 24.04 LTS Minimal)",
    "VCPU_CORES": "Virtual CPU Cores (e.g. 4)",
    "CPU_MODE": "vCPU Mode (e.g. host-passthrough)",
    "RAM_MIB": "Guest RAM in MiB (e.g. 4096)",
    "RAM_GB": "Guest RAM in GB (e.g. 4)",
    "BALLOON_DRIVER": "VirtIO Memory Ballooning Driver (e.g. virtio-balloon)",
    "DISK_SIZE_GIB": "Virtual Disk Size in GiB (e.g. 50)",
    "DISK_FORMAT": "Virtual Disk Format (e.g. qcow2)",
    "UEFI_FIRMWARE": "UEFI Firmware binary name (e.g. OVMF_CODE_4M.ms.fd)",
    "VM_IP": "Static Guest IP address (e.g. 192.168.122.200)",
    "SUBNET_CIDR": "Subnet CIDR notation (e.g. /24)",
    "VIRT_BRIDGE": "Virtual Network Bridge (e.g. virbr0)",
    "LIBVIRT_NETWORK": "libvirt Network Name (e.g. default)",
    "VM_MAC": "Static MAC Address (e.g. 52:54:00:xx:xx:xx)",
    "VM_PRIMARY_USER": "Primary User with sudo/docker access (e.g. devuser)",
    "VM_SECONDARY_USER": "Secondary Administrative User (e.g. admin)",
    "SSH_KEY_PATH": "Path to SSH Private Key (e.g. ~/.ssh/airlock_vm_ed25519)",
    "APP_INTERNAL_PORT": "Application Server Port inside VM (e.g. 3000)",
    "PROXY_EXTERNAL_PORT": "Hypervisor Forwarded Port (e.g. 3001)",
    "PROXY_SOCKET_SERVICE": "Path to systemd proxy socket file (e.g. /etc/systemd/system/airlock-proxy.socket)",
    "REMOTE_CODE_DIR": "Remote rsync code directory (e.g. ~/MyProject/)",
    "SNAPSHOT_BASE_NAME": "Pristine Snapshot Name (e.g. snapshot-base-clean)",
    "SNAPSHOT_BASE_DESC": "Pristine Snapshot Description (e.g. Pristine base setup with Docker and Python)",
}


def format_path(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        try:
            return f"~/{p.relative_to(Path.home())}"
        except ValueError:
            return str(p)


def load_defaults(path: Path | None = None) -> dict:
    if path and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return dict(DEFAULT_BLUEPRINT)


def load_from_inventory(host: str, repo_root: Path, target_vm: str | None = None) -> dict:
    """Pre-seeds hypervisor parameters and avoids collisions from inventory/{host}/vmhost.yaml."""
    inv_root = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))
    host_dir = inv_root / host
    yaml_file = host_dir / "vmhost.yaml"
    if not yaml_file.exists():
        print(f"Error: Inventory record for host '{host}' not found at {yaml_file}", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Pre-seeding configuration from inventory: {yaml_file}")
    content = yaml_file.read_text(encoding="utf-8")
    data = {}
    try:
        import yaml
        parsed = yaml.safe_load(content)
        vmhost = parsed.get("vmhost", {})
        sys_info = vmhost.get("system", {})
        hw_info = vmhost.get("hardware", {})
        cpu_info = hw_info.get("cpu", {})
        mem_info = hw_info.get("memory", {})
        virt_info = vmhost.get("virtualization", {})
        nets = virt_info.get("networks", [])
        primary_net = nets[0] if nets else {}

        total_cores = cpu_info.get("total_cores", 4)
        total_ram_mb = mem_info.get("total_mib", 16384)
        cpu_model = cpu_info.get("model", "CPU")
        ram_gb_str = str(mem_info.get("total_gb", 16))

        # Proportional sizing calculations
        if total_cores <= 2:
            vcpu = 1
        elif total_cores <= 4:
            vcpu = 2
        elif total_cores <= 8:
            vcpu = 4
        elif total_cores <= 16:
            vcpu = 6
        else:
            vcpu = 8

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

        # Collision prevention from recorded VMs, sibling vm.yaml files & DHCP reservations
        existing_domains = [d.get("name") for d in vmhost.get("vms", {}).get("domains", [])]
        assigned_ips = set()
        for n in nets:
            for sh in n.get("static_reservations", []):
                if sh.get("ip"):
                    assigned_ips.add(str(sh["ip"]).strip())
        for sibling_yaml in host_dir.glob("*/vm.yaml"):
            try:
                sdoc = yaml.safe_load(sibling_yaml.read_text(encoding="utf-8")) or {}
                for iface in sdoc.get("vm", {}).get("network", {}).get("interfaces", []):
                    if iface.get("ip"):
                        assigned_ips.add(str(iface["ip"]).strip())
            except Exception:
                pass

        proj_slug = re.sub(r"[^a-z0-9]+", "-", DEFAULT_PROJECT_NAME.lower()).strip("-")
        cand_vm_name = target_vm or f"{proj_slug}-vm"
        if not target_vm and cand_vm_name in existing_domains:
            idx = 1
            while f"{proj_slug}-vm-{idx:02d}" in existing_domains:
                idx += 1
            cand_vm_name = f"{proj_slug}-vm-{idx:02d}"

        subnet_base = primary_net.get("ip", "192.168.122.1")
        netmask = primary_net.get("netmask", "255.255.255.0")
        net_cidr = ipaddress.IPv4Network(f"{subnet_base}/{netmask}", strict=False)
        cidr_suffix = f"/{net_cidr.prefixlen}"
        assigned_ips.add(subnet_base)
        all_hosts = [str(ip) for ip in net_cidr.hosts()]
        preferred_ips = all_hosts[235:250] if len(all_hosts) >= 250 else all_hosts[-16:]
        proposed_ip = next((ip for ip in preferred_ips if ip not in assigned_ips), None)
        if not proposed_ip:
            proposed_ip = next((ip for ip in all_hosts if ip not in assigned_ips), f"{subnet_base}")

        priv_mode = str(vmhost.get("connection", {}).get("privilege_mode", "")).strip().lower()
        proxy_dir = "~/.config/systemd/user" if priv_mode == "rootless" else "/etc/systemd/system"
        dev_host = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip() or "workstation"
        data = {
            "PROJECT_NAME": DEFAULT_PROJECT_NAME,
            "DEV_HOST": dev_host,
            "DEV_HOST_TITLE": dev_host.capitalize(),
            "VM_SECONDARY_USER": subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip() or "devuser",
            "HYPERVISOR_HOST": host,
            "HYPERVISOR_HOST_TITLE": host.capitalize(),
            "HYPERVISOR_OS": sys_info.get("os", {}).get("pretty_name", "Linux"),
            "HYPERVISOR_SPECS": f"{total_cores} Cores {cpu_model} | {ram_gb_str} GB RAM | KVM / libvirt",
            "HYPERVISOR_LAN_IP": vmhost.get("connection", {}).get("host", host),
            "GUEST_OS": "Ubuntu 24.04 LTS Minimal",
            "GUEST_OS_DETAILS": "Pristine Noble Numbat cloud image",
            "CPU_MODE": "host-passthrough",
            "BALLOON_DRIVER": "virtio-balloon",
            "DISK_SIZE_GIB": "20",
            "DISK_FORMAT": "qcow2",
            "UEFI_FIRMWARE": "OVMF_CODE_4M.ms.fd",
            "VIRT_BRIDGE": primary_net.get("bridge", "virbr0"),
            "LIBVIRT_NETWORK": primary_net.get("name", "default"),
            "SUBNET_CIDR": cidr_suffix,
            "VM_IP": proposed_ip,
            "VM_MAC": f"52:54:00:{os.urandom(3).hex(':')}",
            "VM_NAME": cand_vm_name,
            "VM_PRIMARY_USER": proj_slug.replace("-", ""),
            "SSH_KEY_PATH": f"~/.ssh/{proj_slug.replace('-', '')}_vm_ed25519",
            "APP_INTERNAL_PORT": "3000",
            "PROXY_EXTERNAL_PORT": "3001",
            "PROXY_SOCKET_SERVICE": f"{proxy_dir}/{cand_vm_name}-p3001-proxy.socket",
            "REMOTE_CODE_DIR": f"~/{DEFAULT_PROJECT_NAME}/",
            "SNAPSHOT_BASE_NAME": "snapshot-base-clean",
            "SNAPSHOT_BASE_DESC": "Pristine base setup with Docker and Python",
            "VCPU_CORES": str(vcpu),
            "RAM_MIB": str(ram_mib),
            "RAM_GB": str(ram_mib // 1024),
        }

        # If a specific VM is targeted and has a blueprint in inventory, load its blueprint values
        effective_vm = target_vm or (f"{proj_slug}-vm" if (host_dir / f"{proj_slug}-vm" / "vm.yaml").exists() else None)
        if effective_vm and (host_dir / effective_vm / "vm.yaml").exists():
            vm_yaml_path = host_dir / effective_vm / "vm.yaml"
            parsed_vm = yaml.safe_load(vm_yaml_path.read_text(encoding="utf-8"))
            vm_obj = parsed_vm.get("vm", {})
            # Preserve SSOT hardware resources and network identity from vm.yaml
            vm_res = vm_obj.get("resources", {})
            if vm_res.get("vcpu"):
                data["VCPU_CORES"] = str(vm_res["vcpu"])
            if vm_res.get("memory_mib"):
                data["RAM_MIB"] = str(vm_res["memory_mib"])
                data["RAM_GB"] = str(max(1, int(vm_res["memory_mib"]) // 1024))
            vm_ifaces = vm_obj.get("network", {}).get("interfaces", [])
            if vm_ifaces:
                if vm_ifaces[0].get("ip"):
                    data["VM_IP"] = str(vm_ifaces[0]["ip"])
                if vm_ifaces[0].get("mac"):
                    data["VM_MAC"] = str(vm_ifaces[0]["mac"])
            bp = vm_obj.get("blueprint", {})
            if bp:
                print(f"[*] Layering blueprint from {format_path(vm_yaml_path)}")
                if bp.get("project_name"): data["PROJECT_NAME"] = bp["project_name"]
                if bp.get("primary_user"): data["VM_PRIMARY_USER"] = bp["primary_user"]
                if bp.get("secondary_user"): data["VM_SECONDARY_USER"] = bp["secondary_user"]
                if bp.get("ssh_key_path"): data["SSH_KEY_PATH"] = bp["ssh_key_path"]
                if bp.get("app_internal_port"): data["APP_INTERNAL_PORT"] = str(bp["app_internal_port"])
                if bp.get("proxy_external_port"): data["PROXY_EXTERNAL_PORT"] = str(bp["proxy_external_port"])
                if bp.get("proxy_socket_service"): data["PROXY_SOCKET_SERVICE"] = bp["proxy_socket_service"]
                if bp.get("remote_code_dir"): data["REMOTE_CODE_DIR"] = bp["remote_code_dir"]
                if bp.get("guest_os"): data["GUEST_OS"] = bp["guest_os"]
                if bp.get("guest_os_details"): data["GUEST_OS_DETAILS"] = bp["guest_os_details"]
                if bp.get("cpu_mode"): data["CPU_MODE"] = bp["cpu_mode"]
                if bp.get("balloon_driver"): data["BALLOON_DRIVER"] = bp["balloon_driver"]
                if bp.get("disk_size_gib"): data["DISK_SIZE_GIB"] = str(bp["disk_size_gib"])
                if bp.get("disk_format"): data["DISK_FORMAT"] = bp["disk_format"]
                if bp.get("uefi_firmware"): data["UEFI_FIRMWARE"] = bp["uefi_firmware"]
                if bp.get("snapshot_base_name"): data["SNAPSHOT_BASE_NAME"] = bp["snapshot_base_name"]
                if bp.get("snapshot_base_desc"): data["SNAPSHOT_BASE_DESC"] = bp["snapshot_base_desc"]

    except Exception as e:
        print(f"Warning: Failed to load inventory from {yaml_file}: {e}", file=sys.stderr)

    return data


def render_template(template_str: str, params: dict) -> tuple[str, list[str]]:
    # Match {{ KEY }} or {{KEY}}
    pattern = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
    unresolved = []

    def replace_match(match):
        key = match.group(1)
        val = params.get(key)
        if val is not None and str(val).strip() != "":
            return str(val)
        unresolved.append(key)
        return match.group(0)

    rendered = pattern.sub(replace_match, template_str)
    return rendered, sorted(list(set(unresolved)))


def auto_derive_helpers(params: dict, explicit_keys: set[str] | None = None) -> None:
    explicit_keys = explicit_keys or set()
    # Default PROJECT_NAME to repository root folder name if not specified
    if not params.get("PROJECT_NAME"):
        params["PROJECT_NAME"] = DEFAULT_PROJECT_NAME

    # Auto-derive VM naming and paths from PROJECT_NAME
    proj_slug = re.sub(r"[^a-z0-9]+", "-", str(params["PROJECT_NAME"]).lower()).strip("-")
    if not params.get("VM_NAME"):
        params["VM_NAME"] = f"{proj_slug}-vm"
    if not params.get("VM_PRIMARY_USER"):
        params["VM_PRIMARY_USER"] = proj_slug.replace("-", "")
    if not params.get("SSH_KEY_PATH"):
        params["SSH_KEY_PATH"] = f"~/.ssh/{params['VM_PRIMARY_USER']}_vm_ed25519"
    if not params.get("REMOTE_CODE_DIR"):
        params["REMOTE_CODE_DIR"] = f"~/{params['PROJECT_NAME']}/"
    ext_port = params.get("PROXY_EXTERNAL_PORT") or "3001"
    if not params.get("PROXY_SOCKET_SERVICE"):
        params["PROXY_SOCKET_SERVICE"] = f"/etc/systemd/system/{params['VM_NAME']}-p{ext_port}-proxy.socket"

    # Auto-derive RAM_GB if RAM_MIB is set (and recompute if RAM_MIB was explicitly overridden without RAM_GB)
    if params.get("RAM_MIB") and (not params.get("RAM_GB") or ("RAM_MIB" in explicit_keys and "RAM_GB" not in explicit_keys)):
        try:
            params["RAM_GB"] = str(max(1, int(params["RAM_MIB"]) // 1024))
        except ValueError:
            pass

    # Auto-derive title case hosts
    if params.get("DEV_HOST") and not params.get("DEV_HOST_TITLE"):
        params["DEV_HOST_TITLE"] = params["DEV_HOST"].capitalize()
    if params.get("HYPERVISOR_HOST") and not params.get("HYPERVISOR_HOST_TITLE"):
        params["HYPERVISOR_HOST_TITLE"] = params["HYPERVISOR_HOST"].capitalize()


def prompt_user_for_params(current_params: dict) -> dict:
    print("\n--- AirlockVM VM Interactive Configuration ---")
    print("Press [Enter] to keep the current/default value shown in brackets.\n")
    updated = dict(current_params)
    for key, desc in PARAM_DESCRIPTIONS.items():
        curr_val = updated.get(key, "")
        suggested_default = ""
        if not curr_val:
            if key == "PROJECT_NAME":
                suggested_default = DEFAULT_PROJECT_NAME
                curr_val = suggested_default
            elif key == "VM_NAME":
                proj_name = updated.get("PROJECT_NAME") or DEFAULT_PROJECT_NAME
                slug = re.sub(r"[^a-z0-9]+", "-", proj_name.lower()).strip("-")
                suggested_default = f"{slug}-vm"
                curr_val = suggested_default

        prompt_text = f"{key} [{desc}]\n  Current: ({curr_val}): "
        try:
            val = input(prompt_text).strip()
            if val:
                updated[key] = val
            elif suggested_default and not updated.get(key):
                updated[key] = suggested_default
        except (KeyboardInterrupt, EOFError):
            print("\nConfiguration cancelled.")
            sys.exit(1)
    auto_derive_helpers(updated)
    return updated


def show_diff(original: str, new: str, original_name: str, new_name: str) -> None:
    diff = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=original_name,
            tofile=new_name,
        )
    )
    if not diff:
        print("No changes detected. Output is identical to current file.")
    else:
        sys.stdout.writelines(diff)


def main():
    parser = argparse.ArgumentParser(
        description="Customize AirlockVM VM.md from template"
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE_PATH,
        help=f"Path to template file (default: {DEFAULT_TEMPLATE_PATH})",
    )
    parser.add_argument(
        "--probe",
        type=str,
        metavar="HYPERVISOR_HOST",
        help="Auto-discover hardware, libvirt network, next free IP, and collision-free MAC from hypervisor host/IP (use 'localhost' for local KVM)",
    )
    parser.add_argument(
        "--from-inventory",
        type=str,
        metavar="VMHOST",
        help="Pre-seed configuration from existing inventory/{vmhost}/vmhost.yaml (offline mode)",
    )
    parser.add_argument(
        "--vm",
        type=str,
        metavar="VM_NAME",
        help="Target a specific virtual machine domain when configuring from inventory",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip pre-flight virtualization capability checks when probing",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="JSON file containing configuration parameters",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Prompt interactively for all configuration parameters",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Path to destination markdown file (default: inventory/<vmhost>/<vm>/VM.md)",
    )
    parser.add_argument(
        "--set",
        dest="set_vars",
        action="append",
        metavar="KEY=VALUE",
        help="Override a specific parameter, e.g. --set VM_NAME=custom-vm (can be used multiple times)",
    )
    parser.add_argument(
        "--save-config",
        type=Path,
        help="Save resolved configuration parameters to a JSON file",
    )
    parser.add_argument(
        "--dump-defaults",
        type=Path,
        metavar="PATH",
        help="Dump default blueprint parameters to a JSON file and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes as a unified diff without writing to destination",
    )

    args = parser.parse_args()

    if args.dump_defaults:
        defaults = load_defaults()
        with open(args.dump_defaults, "w", encoding="utf-8") as f:
            json.dump(defaults, f, indent=2)
        print(f"Dumped default configuration to {args.dump_defaults}")
        return

    # 1. Start with defaults
    params = load_defaults()

    # 1b. Layer inventory parameters if --from-inventory is passed
    if args.from_inventory:
        inv_params = load_from_inventory(args.from_inventory, REPO_ROOT, target_vm=args.vm)
        params.update(inv_params)

    # 1c. Layer auto-discovered parameters if --probe is passed
    if args.probe:
        try:
            from probe_hypervisor import probe_all, HypervisorCapabilityError
            project_hint = DEFAULT_PROJECT_NAME
            if args.set_vars:
                for item in args.set_vars:
                    if item.startswith("PROJECT_NAME="):
                        project_hint = item.split("=", 1)[1]
            probed_params = probe_all(args.probe, project_name=project_hint, skip_preflight=args.skip_preflight)
            params.update(probed_params)
        except HypervisorCapabilityError as e:
            print(f"\n{e}", file=sys.stderr)
            sys.exit(2)
        except Exception as e:
            print(f"Warning: Probing hypervisor '{args.probe}' failed ({e}). Falling back to manual input.", file=sys.stderr)

    # 2. Layer custom config file if supplied
    if args.config:
        if not args.config.exists():
            print(f"Error: Config file '{args.config}' not found.", file=sys.stderr)
            sys.exit(1)
        with open(args.config, "r", encoding="utf-8") as f:
            user_config = json.load(f)
            params.update(user_config)

    # 3. Layer command-line overrides (--set KEY=VALUE)
    explicit_keys: set[str] = set()
    if args.set_vars:
        for item in args.set_vars:
            if "=" not in item:
                print(f"Error: Invalid --set format '{item}'. Use KEY=VALUE.", file=sys.stderr)
                sys.exit(1)
            k, v = item.split("=", 1)
            key_clean = k.strip()
            params[key_clean] = v.strip()
            explicit_keys.add(key_clean)

    # 4. Interactive mode if requested
    if args.interactive:
        params = prompt_user_for_params(params)

    auto_derive_helpers(params, explicit_keys=explicit_keys)

    # 5. Read template
    if not args.template.exists():
        print(f"Error: Template '{args.template}' not found.", file=sys.stderr)
        sys.exit(1)

    with open(args.template, "r", encoding="utf-8") as f:
        template_content = f.read()

    rendered, unresolved = render_template(template_content, params)

    if unresolved:
        print(f"Warning: Unresolved placeholders in template: {', '.join(unresolved)}", file=sys.stderr)

    # 6. Save config if requested
    if args.save_config:
        args.save_config.parent.mkdir(parents=True, exist_ok=True)
        with open(args.save_config, "w", encoding="utf-8") as f:
            json.dump(params, f, indent=2)
        print(f"Configuration saved to {args.save_config}")

    # 7. Determine output path and write/dry-run
    inv_root = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))
    output_path = args.output
    if not output_path:
        target_host = args.from_inventory or (args.probe if args.probe and args.probe not in ("localhost", "127.0.0.1") else "localhost") or params.get("HYPERVISOR_HOST") or "localhost"
        vm_name = params.get("VM_NAME") or f"{DEFAULT_PROJECT_NAME.lower()}-vm"
        output_path = inv_root / target_host / vm_name / "VM.md"

    original_content = ""
    if output_path.exists():
        with open(output_path, "r", encoding="utf-8") as f:
            original_content = f.read()

    if args.dry_run:
        print(f"\n--- Dry Run: Diff for {output_path} ---")
        show_diff(original_content, rendered, str(output_path) + " (original)", str(output_path) + " (rendered)")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(rendered)

    print(f"Successfully generated {output_path}")

    # Also update VM runbook in inventory if targeted from inventory
    if args.from_inventory and params.get("VM_NAME"):
        vm_inv_dir = inv_root / args.from_inventory / params["VM_NAME"]
        if vm_inv_dir.exists():
            vm_runbook_path = vm_inv_dir / "VM.md"
            if vm_runbook_path != output_path:
                vm_runbook_path.write_text(rendered, encoding="utf-8")
                print(f"Updated inventory VM runbook: {vm_runbook_path}")
    if original_content and original_content != rendered:
        print("Updated document with customized preferences.")
    elif not original_content:
        print("Created new documentation file.")
    else:
        print("Document content is up-to-date (no changes).")


if __name__ == "__main__":
    main()
