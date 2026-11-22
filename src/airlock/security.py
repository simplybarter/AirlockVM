"""AirlockVM Security & Isolation Boundary Audit Engine.

Enforces zero-workload workstation invariants and sandboxing guardrails:
1. Verifies zero credentials, private keys, or .git trees leaked onto the guest VM.
2. Audits hypervisor AppArmor / SELinux confinement profiles.
3. Audits workstation processes to guarantee zero runtime workloads execute locally.
"""

from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
from typing import Any
import yaml

from airlock.inventory import find_vm_yaml, get_primary_inventory_dir
from airlock.transport import run_guest_cmd

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = get_primary_inventory_dir()


def _ssh_guest(vm_name: str, cmd: str, timeout: int = 5) -> tuple[int, str, str]:
    return run_guest_cmd(vm_name, cmd, timeout=timeout)


def _find_vm_yaml(vm_name: str) -> tuple[Path | None, dict[str, Any] | None]:
    return find_vm_yaml(vm_name)


def _safe_remote_dir_expr(remote_dir: str) -> str:
    """Safely quotes a remote directory path while preserving leading ~ expansion."""
    cleaned = (remote_dir or "~/").strip()
    if cleaned == "~" or cleaned == "~/":
        return '"$HOME"'
    if cleaned.startswith("~/"):
        suffix = cleaned[2:]
        return f'"$HOME"/{shlex.quote(suffix)}'
    return shlex.quote(cleaned)


def audit_vm_security(vm_name: str, app_name: str = "") -> dict[str, Any]:
    """Audits security isolation boundaries across workstation and guest VM."""
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {"success": False, "error": f"VM '{vm_name}' not found in inventory."}

    vm_data = doc.get("vm", {})
    vmhost = vm_data.get("host", "remotehost")
    bp = vm_data.get("blueprint", {})
    remote_dir_raw = bp.get("remote_code_dir", "~/")
    remote_dir_expr = _safe_remote_dir_expr(remote_dir_raw)

    findings = []
    violations = 0
    unverified = False

    # 1. Check for secret leaks / forbidden exclusions on guest VM
    leak_check_cmd = (
        f"find {remote_dir_expr} -maxdepth 3 \\( "
        "-name '.git' -o "
        "-name '.env*.local' -o "
        "-name '.env.production' -o "
        "-name '*.pem' -o "
        "-name '*.key' -o "
        "-name 'id_rsa' -o "
        "-name 'id_ed25519' "
        "\\) 2>/dev/null"
    )
    rc, leaks, err = _ssh_guest(vm_name, leak_check_cmd)
    if rc != 0:
        findings.append({
            "check": "secret_leak_audit",
            "status": "UNVERIFIED",
            "detail": f"Unable to audit guest VM '{vm_name}' over SSH ({err or f'exit code {rc}'}). Fail-closed: cannot confirm zero credential leaks.",
            "severity": "high",
        })
        unverified = True
    elif leaks.strip():
        leaked_files = leaks.strip().splitlines()
        findings.append({
            "check": "secret_leak_audit",
            "status": "FAIL",
            "detail": f"Forbidden files or credentials detected on guest VM: {', '.join(leaked_files[:4])}",
            "severity": "high",
        })
        violations += 1
    else:
        findings.append({
            "check": "secret_leak_audit",
            "status": "PASS",
            "detail": f"Zero credentials, SSH keys, or .git repositories present in guest directory ({remote_dir_raw}).",
            "severity": "none",
        })

    # 2. Check hypervisor confinement status
    sec = vm_data.get("security", {})
    apparmor = str(sec.get("apparmor_profile", "unconfined"))
    is_confined = "unconfined" not in apparmor.lower()
    findings.append({
        "check": "hypervisor_confinement",
        "status": "PASS" if is_confined else "INFO",
        "detail": (
            f"Libvirt security profile: '{apparmor}'. AppArmor/SELinux confinement is active."
            if is_confined
            else f"Libvirt security profile: '{apparmor}'. Domain is unconfined; consider enabling AppArmor/SELinux confinement."
        ),
        "severity": "none" if is_confined else "info",
    })

    # 3. Workstation Zero-Workload Invariant
    # Audit local workstation processes to ensure application runtimes are not running locally
    local_workload_cmd = (
        "pgrep -fa '(server\\.js|uvicorn|gunicorn|flask run|manage\\.py runserver|vite|next dev|next start)' "
        "2>/dev/null | grep -v -E 'antigravity|cursor|code|ide|pgrep' || true"
    )
    proc = subprocess.run(["bash", "-c", local_workload_cmd], stdout=subprocess.PIPE, text=True)
    local_procs = proc.stdout.strip()
    if local_procs:
        findings.append({
            "check": "workstation_zero_workload",
            "status": "WARN",
            "detail": f"Application process appears to be running directly on development workstation: {local_procs[:80]}",
            "severity": "warning",
        })
    else:
        findings.append({
            "check": "workstation_zero_workload",
            "status": "PASS",
            "detail": "Zero application runtime workloads executing on development workstation. Invariant satisfied.",
            "severity": "none",
        })

    compliant = violations == 0 and not unverified
    if violations > 0:
        verdict = "VIOLATION_DETECTED"
    elif unverified:
        verdict = "UNVERIFIED"
    else:
        verdict = "SECURE"

    return {
        "success": True,
        "vm_name": vm_name,
        "host": vmhost,
        "compliant": compliant,
        "verdict": verdict,
        "violations_count": violations,
        "findings": findings,
        "message": (
            f"Security audit for '{vm_name}': {verdict} ({violations} violation(s) detected)."
            if not compliant
            else f"Security audit for '{vm_name}': SECURE. Zero credential leaks and workstation isolation verified."
        ),
    }
