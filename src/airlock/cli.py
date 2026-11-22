"""AirlockVM Unified CLI Tool.

Provides unified command-line entrypoints for managing isolated execution
virtual machines, synchronizing code, and inspecting hypervisors.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Any

from airlock.mcp.server import (
    airlock_audit_drift,
    airlock_audit_security,
    airlock_diagnose,
    airlock_list,
    airlock_ping,
    airlock_preflight,
    airlock_snapshot,
    airlock_status,
)
from airlock.transport import get_python_bin, resolve_asset_path, validate_identifier

AIRLOCK_ROOT = Path(__file__).resolve().parents[2]
PYTHON_BIN = get_python_bin(AIRLOCK_ROOT)

DEPLOY_SCRIPT = resolve_asset_path(".agents/skills/airlock_deploy/scripts/deploy_vm.py", AIRLOCK_ROOT)
CONFIGURE_SCRIPT = resolve_asset_path(".agents/skills/airlock_configure/scripts/configure_vm.py", AIRLOCK_ROOT)
SYNC_DISPATCHER = resolve_asset_path("deploy_app_to_airlock.sh", AIRLOCK_ROOT)
if not SYNC_DISPATCHER.exists():
    legacy_sync = resolve_asset_path("deploy_app_to_vm.sh", AIRLOCK_ROOT)
    if legacy_sync.exists():
        SYNC_DISPATCHER = legacy_sync
CONNECT_SCRIPT = resolve_asset_path("connect_airlock.sh", AIRLOCK_ROOT)


def cmd_ping(args: argparse.Namespace) -> int:
    print(airlock_ping())
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    res = airlock_list()
    vms = res.get("vms", [])
    if not vms:
        print("No virtual machines currently documented in inventory.")
        return 0

    print(f"\nAirlockVM Inventory ({len(vms)} VMs registered):")
    print(f"{'VM NAME':<24} {'HOST':<12} {'STATE':<10} {'IP':<16} {'EXT PORT':<10} {'INT PORT':<10}")
    print("-" * 84)
    for v in vms:
        print(f"{v['vm_name']:<24} {v['host']:<12} {v['state']:<10} {v['ip']:<16} {v['external_port']:<10} {v['internal_port']:<10}")
    print()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    res = airlock_status(args.vm_name)
    if not res.get("success"):
        print(f"Error querying status for '{args.vm_name}': {res.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    print(res.get("stdout", ""))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    cmd = [str(PYTHON_BIN), str(CONFIGURE_SCRIPT), "--probe", args.host]
    if args.interactive:
        cmd.append("--interactive")
    if args.dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(cmd, cwd=AIRLOCK_ROOT)
    return proc.returncode


def cmd_deploy(args: argparse.Namespace) -> int:
    cmd = [str(PYTHON_BIN), str(DEPLOY_SCRIPT)]
    if args.vm:
        cmd.extend(["--vm", args.vm])
    if args.dry_run:
        cmd.append("--dry-run")
    if args.yes:
        cmd.append("--yes")
    proc = subprocess.run(cmd, cwd=AIRLOCK_ROOT)
    return proc.returncode


def cmd_sync(args: argparse.Namespace) -> int:
    if not SYNC_DISPATCHER.exists():
        print(f"Sync dispatcher script '{SYNC_DISPATCHER}' not found. Run generate_sync.py first.", file=sys.stderr)
        return 1

    cmd = [str(SYNC_DISPATCHER)]
    if args.vm:
        cmd.append(args.vm)
    if args.dry_run:
        cmd.append("--dry-run")
    if args.delete:
        cmd.append("--delete")
    proc = subprocess.run(cmd, cwd=AIRLOCK_ROOT)
    return proc.returncode


def cmd_connect(args: argparse.Namespace) -> int:
    if not args.vm:
        print("Error: Target VM domain name is required (e.g. 'airlock connect <vm_name>').", file=sys.stderr)
        return 1

    try:
        safe_vm = validate_identifier(args.vm, "VM name")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.ide == "vscode":
        cmd = ["code", "--remote", f"ssh-remote+{safe_vm}"]
    elif args.ide == "cursor":
        cmd = ["cursor", "--remote", f"ssh-remote+{safe_vm}"]
    else:
        cmd = ["ssh", safe_vm]

    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except FileNotFoundError:
        print(f"Error: Required executable '{cmd[0]}' not found in PATH.", file=sys.stderr)
        return 1


def cmd_preflight(args: argparse.Namespace) -> int:
    res = airlock_preflight(args.vm_name)
    deployable = bool(res.get("deployable"))
    verdict = res.get("verdict") or ("READY_TO_DEPLOY" if deployable else "BLOCKED")
    print(f"\nAirlockVM Pre-Flight Audit: {args.vm_name} on {res.get('host', 'unknown')}")
    print(f"Overall Status: {verdict}")
    print(f"Checks: {res.get('passed_checks', 0)}/{res.get('total_checks', 0)} passed\n")
    for name, c in res.get("checks", {}).items():
        status = c.get("status", "UNKNOWN")
        msg = c.get("message", "")
        symbol = "✓" if status == "PASS" else ("⚠" if status in ("WARN", "SKIPPED") else "✗")
        print(f"  {symbol} [{status}] {name:<22}: {msg}")
    if res.get("remediations"):
        print("\nActionable Remediations:")
        for rem in res.get("remediations", []):
            print(f"  - {rem}")
    print()
    return 0 if deployable else 1


def cmd_diagnose(args: argparse.Namespace) -> int:
    res = airlock_diagnose(args.vm_name, app_name=args.app_name or "")
    print(f"\nAirlockVM Diagnostics: {args.vm_name}")
    print(f"Root Cause: {res.get('root_cause')}")
    print(f"Suggested Action: {res.get('suggested_action')}\n")
    print("Findings:")
    for f in res.get("findings", []):
        print(f"  - {f}")
    print()
    return 0


def cmd_drift(args: argparse.Namespace) -> int:
    res = airlock_audit_drift(args.vm_name)
    if not res.get("success"):
        print(f"Error auditing drift for '{args.vm_name}': {res.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    has_drift = bool(res.get("has_drift"))
    print(f"\nAirlockVM Drift Audit: {args.vm_name}")
    print(f"Drift Detected: {'YES' if has_drift else 'NO'}")
    items = res.get("drift_items", [])
    if items:
        for item in items:
            attr = item.get("attribute", item.get("field", "unknown"))
            expected = item.get("inventory_value", item.get("expected", ""))
            live = item.get("live_value", item.get("live", ""))
            print(f"  - [{attr}]: Expected '{expected}' vs Live '{live}'")
    else:
        print("  Domain and hypervisor proxy state match inventory SSOT.")
    print()
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    action = "revert" if args.action == "rollback" else args.action
    res = airlock_snapshot(args.vm_name, action=action, snapshot_name=args.snapshot_name or "")
    if not res.get("success"):
        print(f"Error executing snapshot '{action}' on '{args.vm_name}': {res.get('error') or res.get('stderr', 'unknown error')}", file=sys.stderr)
        return 1
    if res.get("stdout"):
        print(res["stdout"])
    return 0


def cmd_audit_security(args: argparse.Namespace) -> int:
    res = airlock_audit_security(args.vm_name, app_name=args.app_name or "")
    if not res.get("success"):
        print(f"Error auditing security for '{args.vm_name}': {res.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    print(f"\nAirlockVM Security Audit: {args.vm_name} on {res.get('host', 'unknown')}")
    print(f"Verdict: {res.get('verdict', 'UNKNOWN')} (Violations: {res.get('violations_count', 0)})\n")
    for item in res.get("findings", []):
        status = item.get("status", "UNKNOWN")
        symbol = "✓" if status == "PASS" else ("⚠" if status in ("WARN", "INFO") else "✗")
        print(f"  {symbol} [{status}] {item.get('check', 'check'):<26}: {item.get('detail', '')}")
    print()
    return 0 if res.get("compliant") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="airlock",
        description="AirlockVM: Dedicated Execution Virtual Machine Control Plane & Isolation Platform",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # ping
    subparsers.add_parser("ping", help="Ping the AirlockVM runtime")

    # list
    subparsers.add_parser("list", help="List virtual machines in inventory")

    # status
    p_status = subparsers.add_parser("status", help="Query live VM status")
    p_status.add_argument("vm_name", help="Domain name of target VM")

    # probe
    p_probe = subparsers.add_parser("probe", help="Probe hypervisor and generate runbook")
    p_probe.add_argument("host", help="Hypervisor host ('localhost', or remote host name)")
    p_probe.add_argument("--interactive", action="store_true", help="Interactive guided configuration")
    p_probe.add_argument("--dry-run", action="store_true", help="Preview configuration without saving")

    # deploy / up
    p_deploy = subparsers.add_parser("deploy", help="Deploy execution VM defined in inventory")
    p_deploy.add_argument("--vm", help="Target VM domain name")
    p_deploy.add_argument("--dry-run", action="store_true", help="Dry-run deployment preview")
    p_deploy.add_argument("--yes", "-y", action="store_true", help="Non-interactive auto-approval")

    # sync
    p_sync = subparsers.add_parser("sync", help="Synchronize project application files to VM")
    p_sync.add_argument("vm", nargs="?", help="Target VM domain name (optional)")
    p_sync.add_argument("--dry-run", action="store_true", help="Preview rsync differential file sync")
    p_sync.add_argument("--delete", action="store_true", help="Delete extraneous files in target VM")

    # connect
    p_connect = subparsers.add_parser("connect", help="Connect to VM via SSH or launch IDE remote session")
    p_connect.add_argument("vm", nargs="?", help="Target VM domain name")
    p_connect.add_argument("--ide", choices=["vscode", "cursor"], help="Open remote IDE session directly")

    # preflight
    p_preflight = subparsers.add_parser("preflight", help="Validate pre-flight deployability gates")
    p_preflight.add_argument("vm_name", help="Domain name of target VM")

    # diagnose
    p_diag = subparsers.add_parser("diagnose", help="Diagnose VM workload or proxy socket issues")
    p_diag.add_argument("vm_name", help="Domain name of target VM")
    p_diag.add_argument("--app-name", help="Application display name")

    # drift
    p_drift = subparsers.add_parser("drift", help="Audit configuration drift against inventory SSOT")
    p_drift.add_argument("vm_name", help="Domain name of target VM")

    # snapshot
    p_snap = subparsers.add_parser("snapshot", help="Create, revert, or list VM disk snapshots")
    p_snap.add_argument("vm_name", help="Domain name of target VM")
    p_snap.add_argument("action", choices=["create", "revert", "rollback", "list"], help="Snapshot action")
    p_snap.add_argument("snapshot_name", nargs="?", default="", help="Optional snapshot name")

    # audit-security
    p_sec = subparsers.add_parser("audit-security", help="Audit VM credential isolation and workstation zero-workload invariant")
    p_sec.add_argument("vm_name", help="Domain name of target VM")
    p_sec.add_argument("--app-name", help="Optional application display name")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    commands = {
        "ping": cmd_ping,
        "list": cmd_list,
        "status": cmd_status,
        "probe": cmd_probe,
        "deploy": cmd_deploy,
        "sync": cmd_sync,
        "connect": cmd_connect,
        "preflight": cmd_preflight,
        "diagnose": cmd_diagnose,
        "drift": cmd_drift,
        "snapshot": cmd_snapshot,
        "audit-security": cmd_audit_security,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
