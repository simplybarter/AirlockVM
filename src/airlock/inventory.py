"""AirlockVM Inventory Resolution & SSOT Locator.

Provides hierarchical inventory discovery:
1. AIRLOCK_INVENTORY_DIR environment variable (highest precedence).
2. ~/.airlock/inventory/ (standard user persistent location).
3. <repo_root>/inventory/ (repo-local development and test fixture fallback).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
USER_INVENTORY_DIR = Path.home() / ".airlock" / "inventory"
REPO_INVENTORY_DIR = AIRLOCK_ROOT / "inventory"


def get_inventory_search_dirs() -> list[Path]:
    """Returns candidate inventory directories in priority order:
    1. AIRLOCK_INVENTORY_DIR environment variable (if set).
    2. ~/.airlock/inventory/ (if it exists).
    3. <repo_root>/inventory/ (if it exists).
    """
    dirs: list[Path] = []
    if "AIRLOCK_INVENTORY_DIR" in os.environ:
        custom_path = Path(os.environ["AIRLOCK_INVENTORY_DIR"]).expanduser().resolve()
        dirs.append(custom_path)

    if USER_INVENTORY_DIR.exists() and USER_INVENTORY_DIR not in dirs:
        dirs.append(USER_INVENTORY_DIR)

    if REPO_INVENTORY_DIR.exists() and REPO_INVENTORY_DIR not in dirs:
        dirs.append(REPO_INVENTORY_DIR)

    if not dirs:
        dirs.append(USER_INVENTORY_DIR)

    return dirs


def get_primary_inventory_dir(prefer_user: bool = False) -> Path:
    """Returns the primary target directory for writing new inventory records:
    1. AIRLOCK_INVENTORY_DIR environment variable (if set).
    2. ~/.airlock/inventory/ (if prefer_user is True or if it already exists).
    3. <repo_root>/inventory/ (if it exists, for repo development/tests).
    4. ~/.airlock/inventory/ (default for new user installs).
    """
    if "AIRLOCK_INVENTORY_DIR" in os.environ:
        return Path(os.environ["AIRLOCK_INVENTORY_DIR"]).expanduser().resolve()

    if prefer_user or USER_INVENTORY_DIR.exists():
        return USER_INVENTORY_DIR

    if REPO_INVENTORY_DIR.exists():
        return REPO_INVENTORY_DIR

    return USER_INVENTORY_DIR


def find_vm_yaml(vm_name: str) -> tuple[Path | None, dict[str, Any] | None]:
    """Locates vm.yaml for a given VM domain name across all active inventory search paths."""
    if not vm_name:
        return None, None

    name_str = str(vm_name).strip()
    if not name_str:
        return None, None

    for inv_dir in get_inventory_search_dirs():
        if not inv_dir.exists():
            continue
        # Direct path match: <host>/<name>/vm.yaml
        for candidate in inv_dir.glob(f"*/{name_str}/vm.yaml"):
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) if yaml else {}
                if isinstance(data, dict):
                    return candidate, data
            except Exception:
                pass
        # Fallback recursive match across all registered VMs
        for candidate in inv_dir.glob("*/*/vm.yaml"):
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) if yaml else {}
                if isinstance(data, dict):
                    if data.get("vm", {}).get("name") == name_str or candidate.parent.name == name_str:
                        return candidate, data
            except Exception:
                pass

    return None, None


def find_vmhost_yaml(host_name: str) -> tuple[Path | None, dict[str, Any] | None]:
    """Locates vmhost.yaml for a given hypervisor host across active inventory search paths."""
    if not host_name:
        return None, None

    h_str = str(host_name).strip()
    if not h_str:
        return None, None

    for inv_dir in get_inventory_search_dirs():
        if not inv_dir.exists():
            continue
        cand = inv_dir / h_str / "vmhost.yaml"
        if cand.exists():
            try:
                data = yaml.safe_load(cand.read_text(encoding="utf-8")) if yaml else {}
                if isinstance(data, dict):
                    return cand, data
            except Exception:
                pass

    return None, None


def list_inventory_hosts() -> list[str]:
    """Returns unique discovered hypervisor host names across all inventory paths."""
    hosts: set[str] = set()
    for inv_dir in get_inventory_search_dirs():
        if not inv_dir.exists():
            continue
        for p in inv_dir.iterdir():
            if p.is_dir() and (p / "vmhost.yaml").exists():
                hosts.add(p.name)
    return sorted(hosts)


def format_config_path(p: Path) -> str:
    """Safely formats a configuration path relative to AIRLOCK_ROOT or user home."""
    try:
        return str(p.relative_to(AIRLOCK_ROOT))
    except ValueError:
        try:
            return f"~/{p.relative_to(Path.home())}"
        except ValueError:
            return str(p)
