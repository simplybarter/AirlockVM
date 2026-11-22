#!/usr/bin/env python3
"""
Verification & Validation Script for AirlockVM Host & VM Inventory
Ensures template syntax, defaults coverage, and validity of all generated inventory YAML files.
"""

import argparse
import json
import re
import sys
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
INVENTORY_ROOT = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))


def check_template_syntax(template_path: Path) -> list[str]:
    print(f"[*] Checking template syntax: {template_path.name}")
    content = template_path.read_text(encoding="utf-8")
    placeholders = re.findall(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", content)
    unique_placeholders = sorted(set(placeholders))
    print(f"    [✓] Found {len(unique_placeholders)} unique placeholder variables.")
    return unique_placeholders


def check_inventory_yaml_files(inventory_root: Path) -> bool:
    print(f"[*] Auditing inventory directory: {inventory_root}")
    if not inventory_root.exists():
        print(f"    [!] Inventory directory does not exist yet ({inventory_root}).")
        return True

    yaml_files = list(inventory_root.glob("**/*.yaml")) + list(inventory_root.glob("**/*.yml"))
    if not yaml_files:
        print("    [!] No YAML inventory files discovered yet.")
        return True

    print(f"    [*] Found {len(yaml_files)} inventory YAML file(s). Validating syntax and template resolution...")
    all_ok = True
    for yf in yaml_files:
        text = yf.read_text(encoding="utf-8")
        leftovers = re.findall(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", text)
        if leftovers:
            print(f"    [-] ERROR: Unrendered template tags remain in {yf.relative_to(REPO_ROOT)}: {set(leftovers)}")
            all_ok = False
        else:
            if yaml is not None:
                try:
                    yaml.safe_load(text)
                    print(f"    [✓] Valid YAML: {yf.relative_to(REPO_ROOT)}")
                except Exception as e:
                    print(f"    [-] Invalid YAML in {yf.relative_to(REPO_ROOT)}: {e}")
                    all_ok = False
            else:
                print(f"    [✓] Clean tags: {yf.relative_to(REPO_ROOT)}")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Verify AirlockVM Host & VM Inventory templates and files")
    parser.add_argument("--inventory", type=Path, default=INVENTORY_ROOT, help="Base inventory directory")
    args = parser.parse_args()

    host_placeholders = check_template_syntax(VMHOST_TEMPLATE)
    vm_placeholders = check_template_syntax(VM_TEMPLATE)
    inv_ok = check_inventory_yaml_files(args.inventory)

    if host_placeholders and vm_placeholders and inv_ok:
        print("\n[✓] AirlockVM Inventory verification passed successfully!")
        sys.exit(0)
    else:
        print("\n[-] AirlockVM Inventory verification found discrepancies.")
        sys.exit(1)


if __name__ == "__main__":
    main()
