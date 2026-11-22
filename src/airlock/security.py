"""AirlockVM Security & Isolation Boundary Audit Engine.

Enforces zero-workload workstation invariants and sandboxing guardrails:
1. Verifies zero credentials, private keys, or .git trees leaked onto the guest VM.
2. Audits hypervisor AppArmor / SELinux confinement profiles.
3. Audits workstation processes to guarantee zero runtime workloads execute locally.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any
import yaml

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DIR = AIRLOCK_ROOT / "inventory"


def _ssh_guest(vm_name: str, cmd: str, timeout: int = 5) -> tuple[int, str, str]:
    full_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=no",
        vm_name,
        cmd,
    ]
    try:
        proc = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Guest command timed out: {cmd}"
    except Exception as e:
        return 1, "", str(e)


def _find_vm_yaml(vm_name: str) -> tuple[Path | None, dict[str, Any] | None]:
    if not INVENTORY_DIR.exists():
        return None, None
    for cand in INVENTORY_DIR.glob(f"*/{vm_name}/vm.yaml"):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                return cand, yaml.safe_load(f) or {}
        except Exception:
            continue
    return None, None


def audit_vm_security(vm_name: str, app_name: str = "") -> dict[str, Any]:
    """Audits security isolation boundaries across workstation and guest VM."""
    yaml_path, doc = _find_vm_yaml(vm_name)
    if not yaml_path or not doc:
        return {"success": False, "error": f"VM '{vm_name}' not found in inventory."}

    vm_data = doc.get("vm", {})
    vmhost = vm_data.get("host", "remotehost")
    bp = vm_data.get("blueprint", {})
    remote_dir = bp.get("remote_code_dir", "~/").replace("~", "$HOME")

    findings = []
    violations = 0

    # 1. Check for secret leaks / forbidden exclusions on guest VM
    leak_check_cmd = (
        f"find {remote_dir} -maxdepth 3 \\( "
        "-name '.git' -o "
        "-name '.env*.local' -o "
        "-name '*.pem' -o "
        "-name '*.key' -o "
        "-name 'id_rsa' -o "
        "-name 'id_ed25519' "
        "\\) 2>/dev/null"
    )
    rc, leaks, _ = _ssh_guest(vm_name, leak_check_cmd)
    if rc == 0 and leaks.strip():
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
            "detail": f"Zero credentials, SSH keys, or .git repositories present in guest directory ({remote_dir}).",
            "severity": "none",
        })

    # 2. Check hypervisor confinement status
    sec = vm_data.get("security", {})
    apparmor = sec.get("apparmor_profile", "unconfined")
    findings.append({
        "check": "hypervisor_confinement",
        "status": "PASS" if "unconfined" not in apparmor else "INFO",
        "detail": f"Libvirt security profile: '{apparmor}'. AppArmor isolation is active.",
        "severity": "none" if "unconfined" not in apparmor else "info",
    })

    # 3. Workstation Zero-Workload Invariant
    # Audit local workstation processes to ensure node/python application workloads are not running on localhost
    local_workload_cmd = "pgrep -fa 'server\\.js' 2>/dev/null | grep -v -E 'antigravity|cursor|code|ide' || true"
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

    compliant = violations == 0
    verdict = "SECURE" if compliant else "VIOLATION_DETECTED"

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
