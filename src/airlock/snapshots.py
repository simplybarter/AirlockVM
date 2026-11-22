"""AirlockVM Safety Checkpointing Engine.

Creates instant disk-only copy-on-write snapshot checkpoints before deployments
and allows seamless clean-slate rollbacks if experimental workloads crash.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any

from airlock.transport import get_python_bin, resolve_asset_path, validate_identifier

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = resolve_asset_path(".agents/skills/airlock_deploy/scripts/deploy_vm.py", AIRLOCK_ROOT)
PYTHON_BIN = get_python_bin(AIRLOCK_ROOT)


def create_pre_deploy_checkpoint(vm_name: str, tag_prefix: str = "snap-pre-deploy") -> dict[str, Any]:
    """Creates a timestamped snapshot baseline before running application deployments."""
    safe_vm = validate_identifier(vm_name, "VM name")
    safe_prefix = validate_identifier(tag_prefix, "snapshot tag prefix")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snapshot_name = f"{safe_prefix}-{timestamp}"

    cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", safe_vm, "--snapshot", snapshot_name]
    try:
        proc = subprocess.run(cmd, cwd=AIRLOCK_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        return {
            "success": proc.returncode == 0,
            "vm_name": vm_name,
            "snapshot_name": snapshot_name,
            "output": proc.stdout.strip(),
            "message": f"Checkpoint '{snapshot_name}' created successfully for VM '{vm_name}'." if proc.returncode == 0 else f"Failed to create checkpoint: {proc.stderr.strip()}",
        }
    except Exception as e:
        return {
            "success": False,
            "vm_name": vm_name,
            "snapshot_name": snapshot_name,
            "error": str(e),
        }


def rollback_checkpoint(vm_name: str, snapshot_name: str = "") -> dict[str, Any]:
    """Reverts a VM to a designated checkpoint or baseline snapshot."""
    safe_vm = validate_identifier(vm_name, "VM name")
    cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT), "--vm", safe_vm, "--rollback"]
    if snapshot_name:
        cmd.append(validate_identifier(snapshot_name, "snapshot name"))

    try:
        proc = subprocess.run(cmd, cwd=AIRLOCK_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        return {
            "success": proc.returncode == 0,
            "vm_name": vm_name,
            "snapshot_name": snapshot_name or "baseline",
            "output": proc.stdout.strip(),
            "message": f"VM '{vm_name}' successfully reverted to snapshot '{snapshot_name or 'baseline'}'." if proc.returncode == 0 else f"Rollback failed: {proc.stderr.strip()}",
        }
    except Exception as e:
        return {
            "success": False,
            "vm_name": vm_name,
            "error": str(e),
        }
