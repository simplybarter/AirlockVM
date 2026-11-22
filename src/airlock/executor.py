"""AirlockVM Isolated Guest Command Execution Engine.

Runs build, test, migration, and diagnostic commands inside a dedicated guest VM
over SSH with safe working-directory resolution, base64 command transport (avoiding
shell quoting hazards), and bounded timeouts.
"""

from __future__ import annotations

import base64
import shlex
import time
from typing import Any

from airlock.inventory import find_vm_yaml
from airlock.transport import run_guest_cmd, validate_identifier

MAX_EXEC_TIMEOUT = 600


def _safe_workdir_expr(workdir: str) -> str:
    """Safely quotes a guest working directory while preserving leading ~ expansion."""
    cleaned = (workdir or "~").strip()
    if cleaned in ("~", "~/"):
        return '"$HOME"'
    if cleaned.startswith("~/"):
        suffix = cleaned[2:].strip("/")
        if not suffix:
            return '"$HOME"'
        return f'"$HOME"/{shlex.quote(suffix)}'
    return shlex.quote(cleaned)


def exec_in_vm(
    vm_name: str,
    command: str,
    workdir: str = "",
    timeout: int = 120,
) -> dict[str, Any]:
    """Executes a shell command inside a registered AirlockVM guest over SSH.

    Args:
        vm_name: Target VM domain name in inventory.
        command: Shell command to execute inside the guest VM.
        workdir: Optional working directory inside the guest (defaults to blueprint remote_code_dir or ~).
        timeout: Execution timeout in seconds (clamped to 1..600, default 120).
    """
    try:
        safe_vm = validate_identifier(vm_name, "VM name")
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if not command or not command.strip():
        return {"success": False, "vm_name": safe_vm, "error": "Command must be a non-empty string."}

    yaml_path, doc = find_vm_yaml(safe_vm)
    if not yaml_path or not doc:
        return {"success": False, "vm_name": safe_vm, "error": f"VM '{safe_vm}' not found in inventory."}

    bp = doc.get("vm", {}).get("blueprint", {})
    explicit_workdir = (workdir or "").strip()
    effective_workdir = explicit_workdir or str(bp.get("remote_code_dir", "~"))
    safe_timeout = max(1, min(int(timeout), MAX_EXEC_TIMEOUT))

    cmd_b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
    workdir_expr = _safe_workdir_expr(effective_workdir)
    if explicit_workdir:
        cd_prefix = (
            f"cd {workdir_expr} || "
            f"{{ echo {shlex.quote(f'Working directory not found: {explicit_workdir}')} >&2; exit 1; }}; "
        )
    else:
        cd_prefix = f"cd {workdir_expr} 2>/dev/null || cd \"$HOME\"; "

    remote_script = (
        f"{cd_prefix}"
        f"cmd_str=$(printf '%s' {shlex.quote(cmd_b64)} | base64 -d); "
        f"bash -lc \"$cmd_str\""
    )

    start = time.monotonic()
    rc, stdout, stderr = run_guest_cmd(safe_vm, remote_script, timeout=safe_timeout)
    duration_ms = round((time.monotonic() - start) * 1000, 1)

    return {
        "success": rc == 0,
        "vm_name": safe_vm,
        "workdir": effective_workdir,
        "command": command,
        "returncode": rc,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": duration_ms,
    }
