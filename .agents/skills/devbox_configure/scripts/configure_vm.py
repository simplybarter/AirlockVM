#!/usr/bin/env python3
"""
DevBox VM Configuration Script
Facilitates customizing docs/DEVELOPMENT_VM.md from a parameterized template.
"""

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_TEMPLATE_PATH = SKILL_DIR / "template" / "DEVELOPMENT_VM.md.template"
DEFAULT_CONFIG_PATH = SKILL_DIR / "template" / "defaults.json"
# SCRIPT_DIR: .agents/skills/devbox_configure/scripts
# SKILL_DIR: .agents/skills/devbox_configure
# SKILL_DIR.parents[2] is the workspace root
REPO_ROOT = SKILL_DIR.parents[2].resolve()
DEFAULT_PROJECT_NAME = REPO_ROOT.name
DEFAULT_OUTPUT_PATH = REPO_ROOT / "docs" / "DEVELOPMENT_VM.md"

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
    "SSH_KEY_PATH": "Path to SSH Private Key (e.g. ~/.ssh/devbox_vm_ed25519)",
    "APP_INTERNAL_PORT": "Application Server Port inside VM (e.g. 3000)",
    "PROXY_EXTERNAL_PORT": "Hypervisor Forwarded Port (e.g. 3001)",
    "PROXY_SOCKET_SERVICE": "Path to systemd proxy socket file (e.g. /etc/systemd/system/devbox-proxy.socket)",
    "REMOTE_CODE_DIR": "Remote rsync code directory (e.g. ~/MyProject/)",
    "SNAPSHOT_BASE_NAME": "Pristine Snapshot Name (e.g. snapshot-base-clean)",
    "SNAPSHOT_BASE_DESC": "Pristine Snapshot Description (e.g. Pristine base setup with Docker and Python)",
}


def load_defaults(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


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


def auto_derive_helpers(params: dict) -> None:
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
    if not params.get("PROXY_SOCKET_SERVICE"):
        params["PROXY_SOCKET_SERVICE"] = f"/etc/systemd/system/{proj_slug}-proxy.socket"

    # Auto-derive RAM_GB if RAM_MIB is set
    if params.get("RAM_MIB") and not params.get("RAM_GB"):
        try:
            params["RAM_GB"] = str(int(params["RAM_MIB"]) // 1024)
        except ValueError:
            pass

    # Auto-derive title case hosts
    if params.get("DEV_HOST") and not params.get("DEV_HOST_TITLE"):
        params["DEV_HOST_TITLE"] = params["DEV_HOST"].capitalize()
    if params.get("HYPERVISOR_HOST") and not params.get("HYPERVISOR_HOST_TITLE"):
        params["HYPERVISOR_HOST_TITLE"] = params["HYPERVISOR_HOST"].capitalize()


def prompt_user_for_params(current_params: dict) -> dict:
    print("\n--- DevBox VM Interactive Configuration ---")
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
        description="Customize DevBox DEVELOPMENT_VM.md from template"
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
        default=DEFAULT_OUTPUT_PATH,
        help=f"Path to destination markdown file (default: {DEFAULT_OUTPUT_PATH})",
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
        nargs="?",
        const=DEFAULT_CONFIG_PATH,
        help="Dump default parameters to a JSON file and exit",
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
    params = load_defaults(DEFAULT_CONFIG_PATH)

    # 1b. Layer auto-discovered parameters if --probe is passed
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
    if args.set_vars:
        for item in args.set_vars:
            if "=" not in item:
                print(f"Error: Invalid --set format '{item}'. Use KEY=VALUE.", file=sys.stderr)
                sys.exit(1)
            k, v = item.split("=", 1)
            params[k.strip()] = v.strip()

    # 4. Interactive mode if requested
    if args.interactive:
        params = prompt_user_for_params(params)

    auto_derive_helpers(params)

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

    # 7. Dry run or write output
    original_content = ""
    if args.output.exists():
        with open(args.output, "r", encoding="utf-8") as f:
            original_content = f.read()

    if args.dry_run:
        print(f"\n--- Dry Run: Diff for {args.output} ---")
        show_diff(original_content, rendered, str(args.output) + " (original)", str(args.output) + " (rendered)")
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(rendered)

    print(f"Successfully generated {args.output}")
    if original_content and original_content != rendered:
        print("Updated document with customized preferences.")
    elif not original_content:
        print("Created new documentation file.")
    else:
        print("Document content is up-to-date (no changes).")


if __name__ == "__main__":
    main()
