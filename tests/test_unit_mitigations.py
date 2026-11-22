"""Deterministic unit tests verifying mitigations from analysis/PROJECT_REVIEW.md."""

from __future__ import annotations

import importlib.util
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import yaml

from airlock import cli
from airlock.network import allocate_vm_network
from airlock.placement import select_optimal_hypervisor
from airlock.ports import allocate_external_port
from airlock.preflight import validate_deployment_preflight
from airlock.security import audit_vm_security
from airlock.transport import is_local_host, validate_identifier

AIRLOCK_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMitigationsUnit(unittest.TestCase):
    """Verifies all P0/P1/P2 mitigations deterministically without live hypervisors."""

    def test_transport_validate_identifier(self):
        self.assertEqual(validate_identifier("my-app-vm_1.0"), "my-app-vm_1.0")
        with self.assertRaises(ValueError):
            validate_identifier("vm; rm -rf /")
        with self.assertRaises(ValueError):
            validate_identifier("$(whoami)")
        with self.assertRaises(ValueError):
            validate_identifier("")
        self.assertTrue(is_local_host("localhost"))
        self.assertTrue(is_local_host("127.0.0.1"))
        self.assertFalse(is_local_host("remotehost"))

    def test_cli_preflight_returns_nonzero_when_blocked(self):
        blocked = {
            "success": True,
            "deployable": False,
            "verdict": "BLOCKED",
            "host": "remotehost",
            "passed_checks": 0,
            "total_checks": 8,
            "checks": {"hypervisor_access": {"status": "FAIL", "message": "SSH failed"}},
            "remediations": ["Fix SSH"],
        }
        buf = io.StringIO()
        with patch("airlock.cli.airlock_preflight", return_value=blocked), redirect_stdout(buf):
            rc = cli.main(["preflight", "demo-vm"])
        self.assertEqual(rc, 1)
        self.assertIn("Overall Status: BLOCKED", buf.getvalue())

    def test_cli_drift_reads_actual_schema_keys(self):
        drift_resp = {
            "success": True,
            "has_drift": True,
            "drift_items": [
                {"attribute": "vcpu", "inventory_value": 4, "live_value": 2},
            ],
        }
        buf = io.StringIO()
        with patch("airlock.cli.airlock_audit_drift", return_value=drift_resp), redirect_stdout(buf):
            rc = cli.main(["drift", "demo-vm"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Drift Detected: YES", out)
        self.assertIn("[vcpu]: Expected '4' vs Live '2'", out)

    def test_cli_connect_uses_ssh_and_ide_remote(self):
        mock_proc = MagicMock(returncode=0)
        with patch("airlock.cli.subprocess.run", return_value=mock_proc) as mock_run:
            rc = cli.main(["connect", "demo-vm"])
            self.assertEqual(rc, 0)
            mock_run.assert_called_with(["ssh", "demo-vm"])

            rc2 = cli.main(["connect", "demo-vm", "--ide", "vscode"])
            self.assertEqual(rc2, 0)
            mock_run.assert_called_with(["code", "--remote", "ssh-remote+demo-vm"])

    def test_cli_snapshot_and_audit_security_subcommands(self):
        with patch("airlock.cli.airlock_snapshot", return_value={"success": True, "stdout": "snap ok"}) as mock_snap:
            rc = cli.main(["snapshot", "demo-vm", "create", "baseline"])
            self.assertEqual(rc, 0)
            mock_snap.assert_called_once_with("demo-vm", action="create", snapshot_name="baseline")

        sec_resp = {
            "success": True,
            "host": "remotehost",
            "compliant": True,
            "verdict": "SECURE",
            "violations_count": 0,
            "findings": [{"check": "secret_leak_audit", "status": "PASS", "detail": "Clean"}],
        }
        with patch("airlock.cli.airlock_audit_security", return_value=sec_resp):
            rc2 = cli.main(["audit-security", "demo-vm"])
            self.assertEqual(rc2, 0)

    def test_security_audit_fails_closed_when_ssh_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inv = Path(tmpdir)
            vm_dir = inv / "remotehost" / "failclosed-vm"
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.yaml").write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "failclosed-vm",
                        "host": "remotehost",
                        "security": {"apparmor_profile": "unconfined"},
                        "blueprint": {"remote_code_dir": "~/app/"},
                    }
                }),
                encoding="utf-8",
            )
            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv)
            try:
                with patch("airlock.security._ssh_guest", return_value=(255, "", "Connection refused")):
                    res = audit_vm_security("failclosed-vm")
                self.assertTrue(res["success"])
                self.assertFalse(res["compliant"])
                self.assertEqual(res["verdict"], "UNVERIFIED")
                leak_check = next(f for f in res["findings"] if f["check"] == "secret_leak_audit")
                self.assertEqual(leak_check["status"], "UNVERIFIED")
                aa_check = next(f for f in res["findings"] if f["check"] == "hypervisor_confinement")
                self.assertNotIn("AppArmor isolation is active", aa_check["detail"])
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

    def test_preflight_skips_remote_checks_when_hypervisor_unreachable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inv = Path(tmpdir)
            vm_dir = inv / "remotehost" / "unreachable-vm"
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.yaml").write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "unreachable-vm",
                        "host": "remotehost",
                        "resources": {"vcpu": 2, "memory_mib": 2048},
                        "network": {"interfaces": [{"ip": "192.168.122.240", "mac": "52:54:00:aa:bb:cc"}]},
                        "blueprint": {"disk_size_gib": 20, "proxy_external_port": 3050},
                    }
                }),
                encoding="utf-8",
            )
            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv)
            try:
                with patch("airlock.preflight._ssh_cmd", return_value=(255, "", "Unreachable")):
                    res = validate_deployment_preflight("unreachable-vm")
                self.assertEqual(res["verdict"], "BLOCKED")
                self.assertFalse(res["deployable"])
                self.assertEqual(res["checks"]["ip_silence"]["status"], "SKIPPED")
                self.assertEqual(res["checks"]["proxy_port_free"]["status"], "SKIPPED")
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

    def test_ports_allocator_bounded_and_exhaustion(self):
        all_ports = set(range(1025, 65536))
        with patch("airlock.ports.get_inventory_used_ports", return_value=all_ports), \
             patch("airlock.ports.get_hypervisor_live_ports", return_value=set()):
            with self.assertRaises(RuntimeError):
                allocate_external_port("remotehost", internal_port=3000)

        # Wrap-around when upper range is full
        upper_full = set(range(3001, 65536))
        with patch("airlock.ports.get_inventory_used_ports", return_value=upper_full), \
             patch("airlock.ports.get_hypervisor_live_ports", return_value=set()):
            port, _ = allocate_external_port("remotehost", internal_port=3000)
            self.assertEqual(port, 1025)

    def test_network_allocator_uses_custom_subnet(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inv = Path(tmpdir)
            host_dir = inv / "customhost"
            host_dir.mkdir(parents=True)
            (host_dir / "vmhost.yaml").write_text(
                yaml.safe_dump({
                    "vmhost": {
                        "name": "customhost",
                        "virtualization": {
                            "networks": [
                                {
                                    "name": "default",
                                    "bridge": "virbr1",
                                    "ip": "10.42.0.1",
                                    "netmask": "255.255.255.0",
                                }
                            ]
                        },
                    }
                }),
                encoding="utf-8",
            )
            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv)
            try:
                res = allocate_vm_network("customhost")
                self.assertTrue(res["success"])
                self.assertTrue(res["allocated_ip"].startswith("10.42.0."))
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

    def test_placement_enforces_required_vcpu(self):
        small = {
            "success": True, "vmhost": "small-host", "total_cores": 2,
            "available_ram_mib": 16384, "available_disk_gb": 200, "running_vms": 0, "load_average_1m": 0.1,
        }
        large = {
            "success": True, "vmhost": "large-host", "total_cores": 8,
            "available_ram_mib": 8192, "available_disk_gb": 200, "running_vms": 0, "load_average_1m": 0.1,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            inv = Path(tmpdir)
            for h in ("small-host", "large-host"):
                (inv / h).mkdir()
                (inv / h / "vmhost.yaml").write_text("vmhost: {}\n", encoding="utf-8")
            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv)
            try:
                with patch("airlock.placement.inspect_hypervisor", side_effect=lambda h: small if h == "small-host" else large):
                    res = select_optimal_hypervisor(required_vcpu=4, required_ram_mib=4096, required_disk_gb=20)
                self.assertEqual(res["selected_host"], "large-host")
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

    def test_deploy_vm_snapshot_rollback_and_cloud_init(self):
        deploy_mod = _load_module(
            "deploy_vm_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_deploy" / "scripts" / "deploy_vm.py",
        )
        params = {
            "VM_NAME": "demo-vm",
            "SNAPSHOT_BASE_NAME": "snapshot-base-clean",
            "VM_PRIMARY_USER": "airlock",
            "VM_SECONDARY_USER": "",
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_snap = deploy_mod.execute_snapshot(params, "remotehost", "snap-1", dry_run=True)
            rc_rb = deploy_mod.execute_rollback(params, "remotehost", "snap-1", dry_run=True)
        self.assertEqual(rc_snap, 0)
        self.assertEqual(rc_rb, 0)
        self.assertIn("snapshot-create-as", buf.getvalue())
        self.assertIn("snapshot-revert", buf.getvalue())

        # Empty secondary user should not emit blank user entry
        captured = {}

        def fake_run(cmd, host=None):
            captured["script"] = cmd[-1]
            return 0, "", ""

        with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_run):
            deploy_mod.generate_cloud_init_seed(params, "ssh-ed25519 AAAA test", "/tmp/seed.iso", "remotehost", dry_run=False)
        self.assertNotIn("- name: \n", captured["script"])
        self.assertEqual(captured["script"].count("- name:"), 1)

    def test_generate_sync_exclusions_match_security_spec(self):
        sync_mod = _load_module(
            "generate_sync_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_rsync" / "scripts" / "generate_sync.py",
        )
        exclusions = set(sync_mod.evaluate_project_exclusions())
        for expected in (".env.production", "id_rsa", "id_ed25519", ".cursor/", ".codeium/", ".mcp.json", ".svn/", ".hg/"):
            self.assertIn(expected, exclusions)

        # Verify pipe/quote-safe base64 encoding of post_deploy and </dev/null on nohup
        script = sync_mod.generate_shell_script(
            "demo-vm",
            [{
                "name": "my-app",
                "local_path": "../my-app",
                "remote_path": "~/my-app/",
                "post_deploy": 'python3 -c "print(1)" | tee out.log',
                "internal_port": 8080,
                "external_port": 8081,
            }],
            list(exclusions),
        )
        self.assertIn("</dev/null", script)
        self.assertIn("base64 -d", script)
        # Ensure literal pipe inside post_deploy does not appear raw inside REPOSITORIES entry
        self.assertNotIn('| tee out.log|8080|8081', script)

    def test_add_vmhost_preserves_repositories_and_escapes_yaml(self):
        add_mod = _load_module(
            "add_vmhost_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_inventory" / "scripts" / "add_vmhost.py",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            inv = Path(tmpdir)
            vm_dir = inv / "remotehost" / "multi-app-vm"
            vm_dir.mkdir(parents=True)
            repos_orig = [
                {
                    "name": "api",
                    "local_path": "../api",
                    "remote_path": "~/api/",
                    "internal_port": 8000,
                    "external_port": 8001,
                    "post_deploy": 'python3 -c "print(1)"',
                },
                {
                    "name": "web",
                    "local_path": "../web",
                    "remote_path": "~/web/",
                    "internal_port": 3000,
                    "external_port": 3001,
                    "post_deploy": "npm start",
                },
            ]
            (vm_dir / "vm.yaml").write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "multi-app-vm",
                        "host": "remotehost",
                        "blueprint": {
                            "project_name": "MultiApp",
                            "repositories": repos_orig,
                        },
                    }
                }),
                encoding="utf-8",
            )
            bp = add_mod.derive_vm_blueprint("multi-app-vm", "remotehost", inv)
            self.assertEqual(len(bp.get("repositories", [])), 2)
            self.assertEqual(bp["repositories"][0]["post_deploy"], 'python3 -c "print(1)"')

            # Verify rendered repositories YAML parses cleanly with yaml.safe_load
            rendered_repos = add_mod.render_repositories_yaml({"name": "multi-app-vm"}, bp)
            parsed = yaml.safe_load("repositories:\n" + rendered_repos)
            self.assertEqual(len(parsed["repositories"]), 2)
            self.assertEqual(parsed["repositories"][0]["post_deploy"], 'python3 -c "print(1)"')

            # Verify remote SSH uses shlex.join
            with patch.object(add_mod.subprocess, "run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
                add_mod.run_cmd(["grep", "-E", "(vmx|svm)", "/proc/cpuinfo"], host="remotehost")
                called_cmd = mock_run.call_args[0][0]
                self.assertIn("'(vmx|svm)'", called_cmd[-1])

    def test_probe_hypervisor_quotes_remote_ssh_commands(self):
        probe_mod = _load_module(
            "probe_hypervisor_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_configure" / "scripts" / "probe_hypervisor.py",
        )
        with patch.object(probe_mod.subprocess, "run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            probe_mod.run_cmd_status(["grep", "-E", "-m1", "(vmx|svm)", "/proc/cpuinfo"], host="remotehost")
            called_cmd = mock_run.call_args[0][0]
            self.assertIn("StrictHostKeyChecking=accept-new", called_cmd)
            self.assertIn("'(vmx|svm)'", called_cmd[-1])

    def test_configure_vm_layers_ssot_resources_and_network(self):
        conf_mod = _load_module(
            "configure_vm_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_configure" / "scripts" / "configure_vm.py",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            inv = Path(tmpdir)
            host_dir = inv / "remotehost"
            vm_dir = host_dir / "custom-vm"
            vm_dir.mkdir(parents=True)
            (host_dir / "vmhost.yaml").write_text(
                yaml.safe_dump({
                    "vmhost": {
                        "hardware": {"cpu": {"total_cores": 16}, "memory": {"total_mib": 32768, "total_gb": 32}},
                        "virtualization": {"networks": [{"name": "default", "bridge": "virbr0", "ip": "10.99.0.1", "netmask": "255.255.255.0"}]},
                    }
                }),
                encoding="utf-8",
            )
            (vm_dir / "vm.yaml").write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "custom-vm",
                        "host": "remotehost",
                        "resources": {"vcpu": 3, "memory_mib": 6144},
                        "network": {"interfaces": [{"ip": "10.99.0.42", "mac": "52:54:00:de:ad:be"}]},
                        "blueprint": {"project_name": "CustomApp"},
                    }
                }),
                encoding="utf-8",
            )
            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv)
            try:
                data = conf_mod.load_from_inventory("remotehost", AIRLOCK_ROOT, target_vm="custom-vm")
                self.assertEqual(data["VCPU_CORES"], "3")
                self.assertEqual(data["RAM_MIB"], "6144")
                self.assertEqual(data["VM_IP"], "10.99.0.42")
                self.assertEqual(data["VM_MAC"], "52:54:00:de:ad:be")
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)


if __name__ == "__main__":
    unittest.main()
