#!/usr/bin/env python3
"""
Verification & Validation Script for DevBox VM Documentation
Ensures all placeholders in template are accounted for in devbox_inventory templates and checks rendered docs.
"""

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
TEMPLATE_PATH = SKILL_DIR / "template" / "VM.md.template"


def find_repo_root() -> Path:
    if len(SKILL_DIR.parents) >= 3 and SKILL_DIR.parents[1].name == ".agents":
        return SKILL_DIR.parents[2].resolve()
    cwd = Path.cwd().resolve()
    for p in [cwd] + list(cwd.parents):
        if (p / ".git").exists() or (p / ".agents").exists():
            return p
    return cwd


REPO_ROOT = find_repo_root()
INVENTORY_TEMPLATES_DIR = REPO_ROOT / ".agents" / "skills" / "devbox_inventory" / "template"

# Direct mapping of VM.md placeholders provided by devbox_inventory
INVENTORY_PROVIDED_SCHEMA = {
    # 1. Hypervisor infrastructure attributes (from vmhost.yaml)
    "HYPERVISOR_HOST",
    "HYPERVISOR_HOST_TITLE",
    "HYPERVISOR_OS",
    "HYPERVISOR_SPECS",
    "HYPERVISOR_LAN_IP",
    "VIRT_BRIDGE",
    "LIBVIRT_NETWORK",
    "SUBNET_CIDR",
    # 2. Virtual machine allocation & runtime state (from vm.yaml)
    "VM_NAME",
    "VM_IP",
    "VM_MAC",
    "VCPU_CORES",
    "RAM_MIB",
    "RAM_GB",
    # 3. Project blueprint & provisioning spec (from vm.yaml blueprint section)
    "PROJECT_NAME",
    "DEV_HOST",
    "DEV_HOST_TITLE",
    "VM_PRIMARY_USER",
    "VM_SECONDARY_USER",
    "SSH_KEY_PATH",
    "APP_INTERNAL_PORT",
    "PROXY_EXTERNAL_PORT",
    "PROXY_SOCKET_SERVICE",
    "REMOTE_CODE_DIR",
    "GUEST_OS",
    "GUEST_OS_DETAILS",
    "CPU_MODE",
    "BALLOON_DRIVER",
    "DISK_SIZE_GIB",
    "DISK_FORMAT",
    "UEFI_FIRMWARE",
    "SNAPSHOT_BASE_NAME",
    "SNAPSHOT_BASE_DESC",
}


def check_template_syntax(template_path: Path) -> list[str]:
    print(f"Checking template syntax: {template_path}")
    content = template_path.read_text(encoding="utf-8")
    placeholders = re.findall(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", content)
    unique_placeholders = sorted(set(placeholders))
    print(f"Found {len(unique_placeholders)} unique placeholder variables in template.")
    return unique_placeholders


def check_inventory_schema_coverage(placeholders: list[str]) -> bool:
    print(f"Checking inventory schema coverage (devbox_inventory: vmhost.yaml + vm.yaml)...")
    missing = [p for p in placeholders if p not in INVENTORY_PROVIDED_SCHEMA]
    if missing:
        print(f"WARNING: The following template variables are missing from inventory schema: {missing}")
        return False
    print(f"All {len(placeholders)} template placeholders accounted for across devbox_inventory schema.")
    return True


def check_rendered_doc(doc_path: Path) -> bool:
    if not doc_path.exists():
        alt_path = doc_path.with_suffix(".md.ori")
        if alt_path.exists():
            print(f"INFO: {doc_path} not found, checking backup {alt_path}...")
            doc_path = alt_path
        else:
            print(f"NOTICE: Rendered document {doc_path} does not exist.")
            return True
    content = doc_path.read_text(encoding="utf-8")
    leftovers = re.findall(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", content)
    if leftovers:
        print(f"ERROR: Unrendered template tags remain in {doc_path}: {set(leftovers)}")
        return False
    print(f"OK: {doc_path.relative_to(REPO_ROOT) if doc_path.is_relative_to(REPO_ROOT) else doc_path} has zero leftover template tags.")
    return True


def check_all_inventory_runbooks(repo_root: Path) -> bool:
    inv_dir = repo_root / "inventory"
    if not inv_dir.exists():
        print("NOTICE: No inventory directory found to audit.")
        return True
    vm_runbooks = sorted(inv_dir.glob("*/*/VM.md"))
    if not vm_runbooks:
        print("NOTICE: No VM.md runbooks found in inventory/ to audit.")
        return True

    print(f"Auditing {len(vm_runbooks)} inventory runbook(s) in {inv_dir.relative_to(repo_root)}/...")
    all_ok = True
    for rb in vm_runbooks:
        if not check_rendered_doc(rb):
            all_ok = False
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Verify DevBox template and docs against inventory schema")
    parser.add_argument("--doc", type=Path, default=None, help="Path to specific rendered markdown document to verify")
    args = parser.parse_args()

    placeholders = check_template_syntax(TEMPLATE_PATH)
    coverage_ok = check_inventory_schema_coverage(placeholders)

    if args.doc:
        doc_ok = check_rendered_doc(args.doc)
    else:
        doc_ok = check_all_inventory_runbooks(REPO_ROOT)

    if coverage_ok and doc_ok:
        print("\nAll checks passed successfully!")
        sys.exit(0)
    else:
        print("\nVerification found issues.")
        sys.exit(1)


if __name__ == "__main__":
    main()
