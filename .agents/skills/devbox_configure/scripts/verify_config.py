#!/usr/bin/env python3
"""
Verification & Validation Script for DevBox VM Documentation
Ensures all placeholders in template are accounted for and checks rendered docs.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
TEMPLATE_PATH = SKILL_DIR / "template" / "DEVELOPMENT_VM.md.template"
DEFAULTS_PATH = SKILL_DIR / "template" / "defaults.json"
REPO_ROOT = SKILL_DIR.parents[2].resolve()
DOCS_PATH = REPO_ROOT / "docs" / "DEVELOPMENT_VM.md"


def check_template_syntax(template_path: Path):
    print(f"Checking template syntax: {template_path}")
    content = template_path.read_text(encoding="utf-8")
    placeholders = re.findall(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", content)
    unique_placeholders = sorted(set(placeholders))
    print(f"Found {len(unique_placeholders)} unique placeholder variables in template.")
    return unique_placeholders


def check_defaults_coverage(placeholders: list[str], defaults_path: Path):
    print(f"Checking defaults coverage: {defaults_path}")
    defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
    missing = [p for p in placeholders if p not in defaults]
    if missing:
        print(f"WARNING: The following template variables are missing from defaults.json: {missing}")
        return False
    print("All template placeholders are defined in defaults.json.")
    return True


def check_rendered_doc(doc_path: Path):
    if not doc_path.exists():
        alt_path = doc_path.with_suffix(".md.ori")
        if alt_path.exists():
            print(f"INFO: {doc_path} not found, checking backup {alt_path}...")
            doc_path = alt_path
        else:
            print(f"NOTICE: Rendered document {doc_path} does not exist yet (run configure_vm.py to generate it).")
            return True
    content = doc_path.read_text(encoding="utf-8")
    leftovers = re.findall(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", content)
    if leftovers:
        print(f"ERROR: Unrendered template tags remain in {doc_path}: {set(leftovers)}")
        return False
    print(f"OK: {doc_path} has zero leftover template tags.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Verify DevBox template and docs")
    parser.add_argument("--doc", type=Path, default=DOCS_PATH, help="Path to rendered markdown document")
    args = parser.parse_args()

    placeholders = check_template_syntax(TEMPLATE_PATH)
    coverage_ok = check_defaults_coverage(placeholders, DEFAULTS_PATH)
    doc_ok = check_rendered_doc(args.doc)

    if coverage_ok and doc_ok:
        print("\nAll checks passed successfully!")
        sys.exit(0)
    else:
        print("\nVerification found issues.")
        sys.exit(1)


if __name__ == "__main__":
    main()
