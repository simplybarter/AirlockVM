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


    def test_deploy_vm_ssh_config_exact_host_boundary(self):
        deploy_mod = _load_module(
            "deploy_vm_ssh_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_deploy" / "scripts" / "deploy_vm.py",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            ssh_dir = fake_home / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "app_vm_ed25519").write_text("PRIVATE", encoding="utf-8")
            (ssh_dir / "app_vm_ed25519.pub").write_text("ssh-ed25519 AAAA app-vm", encoding="utf-8")
            # Existing entry for app-vm-02 must NOT prevent adding app-vm
            (ssh_dir / "config").write_text("Host app-vm-02\n    HostName 192.168.122.242\n", encoding="utf-8")

            params = {
                "VM_NAME": "app-vm",
                "VM_IP": "192.168.122.241",
                "VM_PRIMARY_USER": "airlock",
                "HYPERVISOR_HOST": "localhost",
                "SSH_KEY_PATH": str(ssh_dir / "app_vm_ed25519"),
            }
            with patch.object(deploy_mod.Path, "home", return_value=fake_home):
                deploy_mod.ensure_workstation_ssh(params, dry_run=False)
            updated = (ssh_dir / "config").read_text(encoding="utf-8")
            self.assertIn("Host app-vm\n", updated)

    def test_resolve_sync_dispatcher_prefers_user_bin(self):
        from airlock.transport import resolve_sync_dispatcher

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            user_disp = fake_home / ".airlock" / "bin" / "deploy_app_to_airlock.sh"
            user_disp.parent.mkdir(parents=True)
            user_disp.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            with patch("airlock.transport.Path.home", return_value=fake_home):
                resolved = resolve_sync_dispatcher(AIRLOCK_ROOT)
            self.assertEqual(resolved, user_disp)

    def test_airlock_exec_engine_and_cli(self):
        from airlock.executor import exec_in_vm

        with tempfile.TemporaryDirectory() as tmpdir:
            inv = Path(tmpdir)
            vm_dir = inv / "localhost" / "exec-vm"
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.yaml").write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "exec-vm",
                        "host": "localhost",
                        "blueprint": {"remote_code_dir": "~/my-app/"},
                    }
                }),
                encoding="utf-8",
            )
            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv)
            try:
                with patch("airlock.executor.run_guest_cmd", return_value=(0, "42 passed", "")) as mock_guest:
                    res = exec_in_vm("exec-vm", 'pytest -k "smoke" | head -n 5', timeout=30)
                self.assertTrue(res["success"])
                self.assertEqual(res["returncode"], 0)
                self.assertEqual(res["stdout"], "42 passed")
                self.assertEqual(res["workdir"], "~/my-app/")
                # Verify base64 transport is used and omitted workdir falls back to $HOME
                called_script = mock_guest.call_args[0][1]
                self.assertIn("base64 -d", called_script)
                self.assertIn('|| cd "$HOME"', called_script)

                # Explicit workdir must fail fast (no silent fallback to $HOME)
                with patch("airlock.executor.run_guest_cmd", return_value=(1, "", "Working directory not found")) as mock_explicit:
                    res_exp = exec_in_vm("exec-vm", "ls", workdir="/nonexistent/dir")
                self.assertFalse(res_exp["success"])
                exp_script = mock_explicit.call_args[0][1]
                self.assertNotIn('|| cd "$HOME"', exp_script)
                self.assertIn("exit 1", exp_script)

                # Invalid VM name rejected
                bad = exec_in_vm("bad;vm", "ls")
                self.assertFalse(bad["success"])

                # CLI airlock exec subcommand
                buf = io.StringIO()
                with patch("airlock.cli.airlock_exec", return_value={"success": True, "returncode": 0, "stdout": "ok", "stderr": ""}), \
                     redirect_stdout(buf):
                    rc = cli.main(["exec", "exec-vm", "--", "pytest", "-q"])
                self.assertEqual(rc, 0)
                self.assertIn("ok", buf.getvalue())
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

    def test_golden_image_fast_boot_and_baker(self):
        deploy_mod = _load_module(
            "deploy_vm_golden_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_deploy" / "scripts" / "deploy_vm.py",
        )
        # 1. Golden image detection prefers airlock-golden-ubuntu-24.04.qcow2
        with patch.object(deploy_mod, "run_cmd_status", return_value=(0, "", "")):
            img_path, is_golden = deploy_mod.resolve_base_image_info("localhost", "/var/lib/libvirt/images")
        self.assertTrue(is_golden)
        self.assertTrue(img_path.endswith(deploy_mod.GOLDEN_IMAGE_FILENAME))

        # 2. Cloud-init skips packages block when is_golden=True
        captured = {}

        def fake_run(cmd, host=None):
            captured["script"] = cmd[-1]
            return 0, "", ""

        params = {
            "VM_NAME": "fast-vm",
            "VM_PRIMARY_USER": "airlock",
            "VM_SECONDARY_USER": "",
        }
        with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_run):
            deploy_mod.generate_cloud_init_seed(params, "ssh-ed25519 AAAA fast", "/tmp/seed.iso", "localhost", dry_run=False, is_golden=True)
        self.assertNotIn("packages:", captured["script"])
        self.assertIn("systemctl enable --now docker", captured["script"])

        # 3. bake_golden_image dry-run succeeds cleanly
        buf = io.StringIO()
        with patch.object(deploy_mod, "run_cmd_status", return_value=(1, "", "")), \
             patch.object(deploy_mod, "resolve_storage_dir", return_value="/var/lib/libvirt/images"), \
             patch.object(deploy_mod, "resolve_base_cloud_image", return_value="/var/lib/libvirt/images/ubuntu-24.04-minimal-cloudimg.img"), \
             redirect_stdout(buf):
            rc = deploy_mod.bake_golden_image("localhost", dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("virt-customize", buf.getvalue())

    def test_deploy_vm_purge_storage_dhcp_ssh_and_dispatcher(self):
        deploy_mod = _load_module(
            "deploy_vm_purge_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_deploy" / "scripts" / "deploy_vm.py",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_home = root / "home"
            ssh_dir = fake_home / ".ssh"
            ssh_dir.mkdir(parents=True)
            inv_dir = root / "inventory"
            vm_dir = inv_dir / "localhost" / "demo-control-vm"
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.yaml").write_text("vm:\n  name: demo-control-vm\n", encoding="utf-8")

            key_priv = ssh_dir / "democontrol_vm_ed25519"
            key_pub = ssh_dir / "democontrol_vm_ed25519.pub"
            key_priv.write_text("PRIVATE", encoding="utf-8")
            key_pub.write_text("ssh-ed25519 AAAA demo-control-vm", encoding="utf-8")

            ssh_cfg = ssh_dir / "config"
            ssh_cfg.write_text(
                "Host other-vm\n"
                "    HostName 192.168.122.210\n\n"
                "# AirlockVM Execution Virtual Machine\n"
                "Host demo-control-vm\n"
                "    HostName 192.168.122.238\n"
                "    User airlock\n"
                f"    IdentityFile {key_priv}\n"
                "    StrictHostKeyChecking no\n\n"
                "Host demo-control-vm-02\n"
                "    HostName 192.168.122.239\n",
                encoding="utf-8",
            )

            params = {
                "VM_NAME": "demo-control-vm",
                "HYPERVISOR_HOST": "localhost",
                "VM_IP": "192.168.122.238",
                "VM_MAC": "52:54:00:aa:bb:cc",
                "LIBVIRT_NETWORK": "default",
                "SSH_KEY_PATH": str(key_priv),
                "DISK_SOURCES": [
                    "/var/lib/libvirt/images/demo-control-vm.snapshot-base-clean",
                    "/var/lib/libvirt/images/demo-control-vm-seed.snapshot-base-clean",
                ],
            }

            recorded_cmds: list[list[str]] = []
            net_xml = (
                "<network><ip address='192.168.122.1'><dhcp>"
                "<host mac='52:54:00:aa:bb:cc' name='demo-control-vm' ip='192.168.122.238'/>"
                "</dhcp></ip></network>"
            )

            def fake_run_cmd(cmd, host=None):
                recorded_cmds.append(list(cmd))
                if cmd[:3] == ["sudo", "virsh", "dominfo"]:
                    return 0, "State:          running\n", ""
                if cmd[:3] == ["sudo", "virsh", "domblklist"]:
                    return 0, " Type   Device   Target   Source\n-----\n file   disk     vda      /var/lib/libvirt/images/demo-control-vm.snapshot-base-clean\n", ""
                if cmd[:3] == ["sudo", "virsh", "net-dumpxml"]:
                    return 0, net_xml, ""
                if cmd[:3] == ["sudo", "bash", "-c"] and "TOTAL_REMOVED" in cmd[3]:
                    return 0, "REMOVED:/var/lib/libvirt/images/demo-control-vm.qcow2\nTOTAL_REMOVED:1\n", ""
                return 0, "", ""

            subproc_calls: list[list[str]] = []

            def fake_subproc(cmd, **kwargs):
                subproc_calls.append(list(cmd))
                return MagicMock(returncode=0, stdout="", stderr="")

            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv_dir)
            try:
                buf = io.StringIO()
                with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_run_cmd), \
                     patch.object(deploy_mod, "resolve_storage_dir", return_value="/var/lib/libvirt/images"), \
                     patch.object(deploy_mod.Path, "home", return_value=fake_home), \
                     patch.object(deploy_mod.subprocess, "run", side_effect=fake_subproc), \
                     redirect_stdout(buf):
                    deploy_mod.execute_purge(params, "localhost", dry_run=False, purge_ssh_keys=True)
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

            # 1. Storage cleanup runs glob expansion inside `sudo bash -c` (not unprivileged bash)
            storage_cmds = [c for c in recorded_cmds if c[:3] == ["sudo", "bash", "-c"] and "TOTAL_REMOVED" in c[3]]
            self.assertEqual(len(storage_cmds), 1)
            self.assertIn("shopt -s nullglob", storage_cmds[0][3])
            self.assertIn("LEFTOVER:", storage_cmds[0][3])

            # 2. Static DHCP reservation deleted via virsh net-update
            dhcp_del_cmds = [c for c in recorded_cmds if c[:5] == ["sudo", "virsh", "net-update", "default", "delete"]]
            self.assertEqual(len(dhcp_del_cmds), 1)
            self.assertIn("ip-dhcp-host", dhcp_del_cmds[0])
            self.assertIn("52:54:00:aa:bb:cc", dhcp_del_cmds[0][6])

            # 3. ~/.ssh/config block removed cleanly without touching other-vm or demo-control-vm-02
            updated_cfg = ssh_cfg.read_text(encoding="utf-8")
            self.assertNotIn("Host demo-control-vm\n", updated_cfg)
            self.assertIn("Host other-vm\n", updated_cfg)
            self.assertIn("Host demo-control-vm-02\n", updated_cfg)

            # 4. SSH keypair deleted when purge_ssh_keys=True
            self.assertFalse(key_priv.exists())
            self.assertFalse(key_pub.exists())

            # 5. Dispatcher regenerated via generate_sync.py
            self.assertTrue(any("generate_sync.py" in " ".join(c) for c in subproc_calls))

    def test_create_vm_renders_requested_ram_and_canonical_proxy_socket_name(self):
        from airlock.mcp.server import airlock_create_vm

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inv_dir = root / "inventory"
            host_dir = inv_dir / "localhost"
            host_dir.mkdir(parents=True)
            # Host with 32 GiB RAM would proportionally default to 8192 MiB if vm.yaml/overrides were ignored
            (host_dir / "vmhost.yaml").write_text(
                yaml.safe_dump({
                    "vmhost": {
                        "name": "localhost",
                        "connection": {"host": "localhost"},
                        "system": {"os": {"pretty_name": "Ubuntu 24.04 LTS"}},
                        "hardware": {
                            "cpu": {"total_cores": 16, "model": "TestCPU"},
                            "memory": {"total_mib": 32768, "total_gb": 32},
                        },
                        "virtualization": {
                            "networks": [{
                                "name": "default",
                                "bridge": "virbr0",
                                "ip": "192.168.122.1",
                                "netmask": "255.255.255.0",
                            }]
                        },
                    }
                }),
                encoding="utf-8",
            )
            app_dir = root / "sample-app"
            app_dir.mkdir()
            (app_dir / "pyproject.toml").write_text('[project]\nname = "sample-app"\n', encoding="utf-8")

            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv_dir)
            try:
                with patch("airlock.ports.get_inventory_used_ports", return_value=set()), \
                     patch("airlock.ports.get_hypervisor_live_ports", return_value=set()):
                    res = airlock_create_vm(
                        local_path=str(app_dir),
                        vmhost="localhost",
                        vm_name="sample-control-vm",
                        app_name="sample-control",
                        internal_port=8000,
                        external_port=8001,
                        vcpu=2,
                        memory_mib=2048,
                        disk_gib=25,
                    )
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

            self.assertTrue(res["success"], res)
            vm_yaml_path = host_dir / "sample-control-vm" / "vm.yaml"
            vm_md_path = host_dir / "sample-control-vm" / "VM.md"
            self.assertTrue(vm_yaml_path.exists())
            self.assertTrue(vm_md_path.exists())

            vm_doc = yaml.safe_load(vm_yaml_path.read_text(encoding="utf-8"))
            expected_socket = "/etc/systemd/system/sample-control-vm-p8001-proxy.socket"
            self.assertEqual(vm_doc["vm"]["blueprint"]["proxy_socket_service"], expected_socket)
            self.assertEqual(vm_doc["vm"]["resources"]["memory_mib"], 2048)

            vm_md = vm_md_path.read_text(encoding="utf-8")
            self.assertIn("2 vCPUs | 2048 MiB RAM | 25 GiB Disk", vm_md)
            self.assertNotIn("8192 MiB RAM", vm_md)
            self.assertIn("2048 MiB (2 GB)", vm_md)
            self.assertIn(expected_socket, vm_md)

    def test_list_hypervisors_mcp_and_cli(self):
        from airlock.mcp.server import airlock_list_hypervisors

        with tempfile.TemporaryDirectory() as tmpdir:
            inv_dir = Path(tmpdir)
            host_dir = inv_dir / "testhost"
            host_dir.mkdir(parents=True)
            (host_dir / "vmhost.yaml").write_text(
                yaml.safe_dump({
                    "vmhost": {
                        "name": "testhost",
                        "alias": "testhost",
                        "connection": {"type": "ssh", "host": "testhost"},
                        "system": {"hostname": "testhost", "os": {"pretty_name": "Ubuntu 24.04 LTS"}},
                        "hardware": {
                            "cpu": {"model": "TestCPU", "total_cores": 12, "kvm_present": True, "vt_enabled": True},
                            "memory": {"total_mib": 32768, "total_gb": 32, "available_mib": 24000},
                            "storage": {
                                "root_filesystem": {"total_gb": 500, "available_gb": 320},
                                "pools": [{"name": "default", "path": "/var/lib/libvirt/images", "state": "active"}],
                            },
                        },
                        "virtualization": {
                            "networks": [{"name": "default", "bridge": "virbr0", "ip": "192.168.122.1", "netmask": "255.255.255.0"}]
                        },
                        "vms": {"count": 2, "running_count": 1},
                    }
                }),
                encoding="utf-8",
            )
            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv_dir)
            try:
                with patch("airlock.inventory.get_inventory_search_dirs", return_value=[inv_dir]), \
                     patch("airlock.mcp.server.get_inventory_search_dirs", return_value=[inv_dir]):
                    res = airlock_list_hypervisors(live_probe=False)
                self.assertTrue(res["success"])
                self.assertEqual(res["total_hypervisors"], 1)
                h = res["hypervisors"][0]
                self.assertEqual(h["host"], "testhost")
                self.assertEqual(h["total_cores"], 12)
                self.assertEqual(h["available_ram_mib"], 24000)
                self.assertEqual(h["available_disk_gb"], 320)
                self.assertEqual(h["running_vms"], 1)
                self.assertEqual(h["vm_count"], 2)

                buf = io.StringIO()
                with patch("airlock.cli.airlock_list_hypervisors", return_value=res), redirect_stdout(buf):
                    rc = cli.main(["hosts"])
                self.assertEqual(rc, 0)
                self.assertIn("testhost", buf.getvalue())
                self.assertIn("24000/32768 MiB", buf.getvalue())
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

    def test_update_vm_mcp_and_cli(self):
        from airlock.mcp.server import airlock_update_vm

        with tempfile.TemporaryDirectory() as tmpdir:
            inv_dir = Path(tmpdir)
            host_dir = inv_dir / "localhost"
            vm_dir = host_dir / "editable-vm"
            vm_dir.mkdir(parents=True)
            (host_dir / "vmhost.yaml").write_text(
                yaml.safe_dump({
                    "vmhost": {
                        "name": "localhost",
                        "connection": {"host": "localhost"},
                        "system": {"os": {"pretty_name": "Ubuntu 24.04 LTS"}},
                        "hardware": {
                            "cpu": {"total_cores": 8, "model": "TestCPU"},
                            "memory": {"total_mib": 16384, "total_gb": 16},
                        },
                        "virtualization": {
                            "networks": [{"name": "default", "bridge": "virbr0", "ip": "192.168.122.1", "netmask": "255.255.255.0"}]
                        },
                    }
                }),
                encoding="utf-8",
            )
            (vm_dir / "vm.yaml").write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "editable-vm",
                        "host": "localhost",
                        "state": "shut off",
                        "resources": {"vcpu": 2, "memory_mib": 2048, "memory_gb": 2},
                        "network": {"interfaces": [{"ip": "192.168.122.240", "mac": "52:54:00:aa:bb:cc"}]},
                        "blueprint": {
                            "project_name": "EditableApp",
                            "primary_user": "airlock",
                            "app_internal_port": 3000,
                            "proxy_external_port": 3001,
                            "proxy_socket_service": "/etc/systemd/system/editable-vm-p3001-proxy.socket",
                            "disk_size_gib": 20,
                            "remote_code_dir": "~/editable-app/",
                            "repositories": [{
                                "name": "EditableApp",
                                "local_path": str(tmpdir),
                                "remote_path": "~/editable-app/",
                                "internal_port": 3000,
                                "external_port": 3001,
                                "post_deploy": "python3 app.py",
                            }],
                        },
                    }
                }),
                encoding="utf-8",
            )

            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv_dir)
            try:
                res = airlock_update_vm(
                    vm_name="editable-vm",
                    vcpu=4,
                    memory_mib=4096,
                    disk_size_gib=60,
                    external_port=8081,
                    post_deploy="uvicorn main:app --host 0.0.0.0 --port 3000",
                )
                self.assertTrue(res["success"], res)
                self.assertTrue(res["changed"])
                self.assertIn("vcpu", res["changes"])
                self.assertIn("memory_mib", res["changes"])
                self.assertIn("disk_size_gib", res["changes"])
                self.assertIn("proxy_external_port", res["changes"])
                self.assertIn("post_deploy", res["changes"])

                updated_doc = yaml.safe_load((vm_dir / "vm.yaml").read_text(encoding="utf-8"))
                self.assertEqual(updated_doc["vm"]["resources"]["vcpu"], 4)
                self.assertEqual(updated_doc["vm"]["resources"]["memory_mib"], 4096)
                self.assertEqual(updated_doc["vm"]["resources"]["memory_gb"], 4)
                self.assertEqual(updated_doc["vm"]["blueprint"]["disk_size_gib"], 60)
                self.assertEqual(updated_doc["vm"]["blueprint"]["proxy_socket_service"], "/etc/systemd/system/editable-vm-p8081-proxy.socket")
                self.assertEqual(updated_doc["vm"]["blueprint"]["repositories"][0]["post_deploy"], "uvicorn main:app --host 0.0.0.0 --port 3000")

                vm_md = (vm_dir / "VM.md").read_text(encoding="utf-8")
                self.assertIn("4 vCPUs | 4096 MiB RAM | 60 GiB Disk", vm_md)
                self.assertIn("/etc/systemd/system/editable-vm-p8081-proxy.socket", vm_md)

                # CLI airlock update subcommand
                buf = io.StringIO()
                with patch("airlock.cli.airlock_update_vm", return_value={"success": True, "message": "Updated editable-vm", "changes": {"vcpu": {"old": 4, "new": 6}}, "warnings": []}) as mock_upd, \
                     redirect_stdout(buf):
                    rc = cli.main(["update", "editable-vm", "--vcpu", "6"])
                self.assertEqual(rc, 0)
                mock_upd.assert_called_once()
                self.assertIn("vcpu: 4 -> 6", buf.getvalue())
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

    def test_purge_vm_mcp_and_cli(self):
        from airlock.mcp.server import airlock_purge_vm

        with tempfile.TemporaryDirectory() as tmpdir:
            inv_dir = Path(tmpdir)
            vm_dir = inv_dir / "localhost" / "disposable-vm"
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.yaml").write_text("vm:\n  name: disposable-vm\n  host: localhost\n", encoding="utf-8")

            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv_dir)
            try:
                with patch("airlock.mcp.server._run_command", return_value={"success": True, "returncode": 0, "stdout": "[PLAN] purge", "stderr": ""}) as mock_run:
                    res = airlock_purge_vm("disposable-vm", dry_run=True, purge_ssh_keys=True)
                self.assertTrue(res["success"])
                called_cmd = mock_run.call_args[0][0]
                self.assertIn("--purge", called_cmd)
                self.assertIn("--dry-run", called_cmd)
                self.assertIn("--purge-ssh-keys", called_cmd)

                # Invalid VM name rejected
                bad = airlock_purge_vm("bad;vm")
                self.assertFalse(bad["success"])

                # CLI airlock purge subcommand with --dry-run (skips confirmation prompt)
                buf = io.StringIO()
                with patch("airlock.cli.airlock_purge_vm", return_value={"success": True, "stdout": "[PLAN] purge", "stderr": ""}) as mock_cli_purge, \
                     redirect_stdout(buf):
                    rc = cli.main(["purge", "disposable-vm", "--dry-run", "--purge-ssh-keys"])
                self.assertEqual(rc, 0)
                mock_cli_purge.assert_called_once_with(vm_name="disposable-vm", dry_run=True, purge_ssh_keys=True)
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

    def test_defaults_disk_20gib_vcpu_2_and_host_cap(self):
        from airlock.mcp.server import airlock_create_vm

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inv_dir = root / "inventory"
            host_dir = inv_dir / "localhost"
            host_dir.mkdir(parents=True)
            (host_dir / "vmhost.yaml").write_text(
                yaml.safe_dump({
                    "vmhost": {
                        "name": "localhost",
                        "connection": {"host": "localhost"},
                        "system": {"os": {"pretty_name": "Ubuntu 24.04 LTS"}},
                        "hardware": {
                            "cpu": {"total_cores": 4, "model": "QuadCore"},
                            "memory": {"total_mib": 16384, "total_gb": 16},
                        },
                        "virtualization": {
                            "networks": [{"name": "default", "bridge": "virbr0", "ip": "192.168.122.1", "netmask": "255.255.255.0"}]
                        },
                    }
                }),
                encoding="utf-8",
            )
            app_dir = root / "tiny-app"
            app_dir.mkdir()
            (app_dir / "pyproject.toml").write_text('[project]\nname = "tiny-app"\n', encoding="utf-8")

            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv_dir)
            try:
                with patch("airlock.ports.get_inventory_used_ports", return_value=set()), \
                     patch("airlock.ports.get_hypervisor_live_ports", return_value=set()):
                    # Default call uses 2 vCPU and 20 GiB disk
                    res_def = airlock_create_vm(local_path=str(app_dir), vmhost="localhost", vm_name="tiny-default-vm")
                    # Explicit vcpu=4 on a 4-core host is capped to 2 vCPUs with a warning
                    res_cap = airlock_create_vm(local_path=str(app_dir), vmhost="localhost", vm_name="tiny-capped-vm", vcpu=4)
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

            self.assertTrue(res_def["success"])
            def_doc = yaml.safe_load((host_dir / "tiny-default-vm" / "vm.yaml").read_text(encoding="utf-8"))
            self.assertEqual(def_doc["vm"]["resources"]["vcpu"], 2)
            self.assertEqual(def_doc["vm"]["blueprint"]["disk_size_gib"], 20)

            self.assertTrue(res_cap["success"])
            cap_doc = yaml.safe_load((host_dir / "tiny-capped-vm" / "vm.yaml").read_text(encoding="utf-8"))
            self.assertEqual(cap_doc["vm"]["resources"]["vcpu"], 2)
            self.assertTrue(any("capped to 2 vCPU" in w for w in res_cap.get("warnings", [])))

    def test_detector_python_uv_and_no_phantom_main_py(self):
        from airlock.detector import detect_application

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # 1. uv project with pyproject.toml + uv.lock + .python-version, NO main.py
            uv_pkg = root / "uv-lib"
            uv_pkg.mkdir()
            (uv_pkg / "pyproject.toml").write_text('[project]\nname = "uv-lib"\ndependencies = ["httpx"]\n', encoding="utf-8")
            (uv_pkg / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (uv_pkg / ".python-version").write_text("3.14\n", encoding="utf-8")

            prof1 = detect_application(uv_pkg)
            self.assertTrue(prof1["success"])
            self.assertEqual(prof1["app_name"], "uv-lib")
            self.assertEqual(prof1["framework"], "Python (uv)")
            self.assertEqual(prof1["package_manager"], "uv")
            self.assertEqual(prof1["python_version"], "3.14")
            self.assertEqual(prof1["post_deploy"], "")

            # 2. uv FastAPI project WITH actual main.py entrypoint
            uv_api = root / "uv-api"
            uv_api.mkdir()
            (uv_api / "pyproject.toml").write_text('[project]\nname = "uv-api"\ndependencies = ["fastapi", "uvicorn"]\n', encoding="utf-8")
            (uv_api / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (uv_api / "main.py").write_text("app = object()\n", encoding="utf-8")

            prof2 = detect_application(uv_api)
            self.assertEqual(prof2["framework"], "Python (uv, FastAPI/Uvicorn)")
            self.assertEqual(prof2["post_deploy"], "uv run uvicorn main:app --host 0.0.0.0 --port 8000")

    def test_per_repo_include_exclude_sync_rules_preserved_and_applied(self):
        import subprocess

        sync_mod = _load_module(
            "generate_sync_rules_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_rsync" / "scripts" / "generate_sync.py",
        )
        add_mod = _load_module(
            "add_vmhost_rules_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_inventory" / "scripts" / "add_vmhost.py",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = root / "my-app"
            (app_dir / "docs" / "templates").mkdir(parents=True)
            (app_dir / "data" / "raw").mkdir(parents=True)
            (app_dir / "docs" / "templates" / "report.md").write_text("# Template\n", encoding="utf-8")
            (app_dir / "docs" / "README.md").write_text("# Ignored doc\n", encoding="utf-8")
            (app_dir / "data" / "raw" / "big.csv").write_text("a,b\n", encoding="utf-8")
            (app_dir / "app.py").write_text("print('hi')\n", encoding="utf-8")

            repos = [{
                "name": "my-app",
                "local_path": str(app_dir),
                "remote_path": "~/my-app/",
                "internal_port": 8000,
                "external_port": 8001,
                "post_deploy": "",
                "include": ["docs/templates/"],
                "exclude": ["data/raw/"],
            }]

            # 1. Verify add_vmhost preserves include/exclude in rendered YAML
            rendered_yaml = add_mod.render_repositories_yaml({"name": "demo-vm"}, {"repositories": repos})
            parsed = yaml.safe_load("repositories:\n" + rendered_yaml)
            self.assertEqual(parsed["repositories"][0]["include"], ["docs/templates/"])
            self.assertEqual(parsed["repositories"][0]["exclude"], ["data/raw/"])

            # 2. Verify generated script --offline actually includes docs/templates/report.md
            #    while excluding docs/README.md and data/raw/big.csv
            exclusions = sync_mod.evaluate_project_exclusions()
            script_path = root / "deploy_app_to_airlock.sh"
            script_path.write_text(sync_mod.generate_shell_script("demo-vm", repos, exclusions), encoding="utf-8")
            script_path.chmod(0o755)

            proc = subprocess.run([str(script_path), "--offline"], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("docs/templates/report.md", proc.stdout)
            self.assertIn("app.py", proc.stdout)
            self.assertNotIn("docs/README.md", proc.stdout)
            self.assertNotIn("data/raw/big.csv", proc.stdout)

    def test_antigravity_connector_workspace_plugin_idempotent_remove_and_global(self):
        import json
        import subprocess

        connector = str(AIRLOCK_ROOT / "connect_airlock.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ws = root / "my-workspace"
            ws.mkdir()
            fake_home = root / "home"
            (fake_home / ".gemini" / "config").mkdir(parents=True)
            global_cfg = fake_home / ".gemini" / "config" / "mcp_config.json"
            global_cfg.write_text(
                json.dumps({"mcpServers": {"existing-tool": {"command": "/usr/bin/existing"}}}, indent=2),
                encoding="utf-8",
            )
            env = {**os.environ, "HOME": str(fake_home), "NO_COLOR": "1"}

            # 1. Workspace install creates .agents/plugins/airlock/{plugin.json,mcp_config.json}
            p1 = subprocess.run(
                [connector, "--target", str(ws), "--antigravity", "-y"],
                cwd=str(AIRLOCK_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p1.returncode, 0, p1.stderr)
            plugin_dir = ws / ".agents" / "plugins" / "airlock"
            manifest_file = plugin_dir / "plugin.json"
            mcp_file = plugin_dir / "mcp_config.json"
            self.assertTrue(manifest_file.exists())
            self.assertTrue(mcp_file.exists())
            self.assertFalse((ws / ".gemini" / "mcp_config.json").exists())
            self.assertEqual(json.loads(manifest_file.read_text(encoding="utf-8")), {"name": "airlock"})
            mcp_doc = json.loads(mcp_file.read_text(encoding="utf-8"))
            self.assertIn("airlock", mcp_doc["mcpServers"])
            self.assertIn("command", mcp_doc["mcpServers"]["airlock"])

            # 2. Idempotent re-run preserves existing mcpServers entries
            mcp_doc["mcpServers"]["other-server"] = {"command": "/usr/bin/other"}
            mcp_file.write_text(json.dumps(mcp_doc, indent=2) + "\n", encoding="utf-8")
            p2 = subprocess.run(
                [connector, "--target", str(ws), "--antigravity", "-y"],
                cwd=str(AIRLOCK_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p2.returncode, 0, p2.stderr)
            mcp_doc2 = json.loads(mcp_file.read_text(encoding="utf-8"))
            self.assertIn("airlock", mcp_doc2["mcpServers"])
            self.assertEqual(mcp_doc2["mcpServers"]["other-server"], {"command": "/usr/bin/other"})

            # 3. Remove with another server present keeps other-server; remove when sole server deletes plugin dir
            p3 = subprocess.run(
                [connector, "--target", str(ws), "--antigravity", "--remove", "-y"],
                cwd=str(AIRLOCK_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p3.returncode, 0, p3.stderr)
            self.assertTrue(plugin_dir.exists())
            mcp_doc3 = json.loads(mcp_file.read_text(encoding="utf-8"))
            self.assertNotIn("airlock", mcp_doc3["mcpServers"])
            self.assertIn("other-server", mcp_doc3["mcpServers"])

            # Now remove other-server, re-install airlock alone, then remove -> entire plugin dir deleted
            mcp_file.write_text(json.dumps({"mcpServers": {}}, indent=2) + "\n", encoding="utf-8")
            subprocess.run([connector, "--target", str(ws), "--antigravity", "-y"], cwd=str(AIRLOCK_ROOT), env=env, check=True, capture_output=True)
            p4 = subprocess.run(
                [connector, "--target", str(ws), "--antigravity", "--remove", "-y"],
                cwd=str(AIRLOCK_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p4.returncode, 0, p4.stderr)
            self.assertFalse(plugin_dir.exists())

            # 4. Global Antigravity mode merges into ~/.gemini/config/mcp_config.json preserving existing entries
            p5 = subprocess.run(
                [connector, "--global", "--antigravity", "-y"],
                cwd=str(AIRLOCK_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p5.returncode, 0, p5.stderr)
            gdoc = json.loads(global_cfg.read_text(encoding="utf-8"))
            self.assertIn("airlock", gdoc["mcpServers"])
            self.assertEqual(gdoc["mcpServers"]["existing-tool"], {"command": "/usr/bin/existing"})

    def test_generate_sync_post_deploy_heredoc_quoting_proxy_host_and_mcp_delete(self):
        import subprocess
        from airlock.mcp.server import airlock_deploy_app

        sync_mod = _load_module(
            "generate_sync_post_deploy_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_rsync" / "scripts" / "generate_sync.py",
        )
        exclusions = sync_mod.evaluate_project_exclusions()
        script = sync_mod.generate_shell_script(
            "demo-control-vm",
            [{
                "name": "demo-app",
                "local_path": "../demo-app",
                "remote_path": "~/demo-app/",
                "post_deploy": 'uv run uvicorn main:app --host 0.0.0.0 --port 8000',
                "internal_port": 8000,
                "external_port": 8001,
            }],
            exclusions,
            hypervisor_host="localhost",
        )

        # 1. Generated bash script passes `bash -n` syntax validation
        with tempfile.TemporaryDirectory() as tmpdir:
            sp = Path(tmpdir) / "deploy_app_to_airlock.sh"
            sp.write_text(script, encoding="utf-8")
            chk = subprocess.run(["bash", "-n", str(sp)], capture_output=True, text=True)
            self.assertEqual(chk.returncode, 0, chk.stderr)

        # 2. Remote post_deploy uses single-quoted heredoc with properly quoted "$cmd_str" and graceful kill -TERM
        self.assertIn("<<'POST_DEPLOY_EOF'", script)
        self.assertIn('nohup bash -lc "$cmd_str" </dev/null > app.log 2>&1 &', script)
        self.assertIn("kill -TERM $old_pids", script)
        self.assertNotIn('bash -lc "\\$cmd_str"', script)

        # 3. Proxy URL uses HYPERVISOR_HOST (localhost), not VM_HOST
        self.assertIn('HYPERVISOR_HOST="localhost"', script)
        self.assertIn("http://$HYPERVISOR_HOST:$ext_port/", script)
        self.assertNotIn("http://$VM_HOST:$ext_port/", script)

        # 4. airlock_deploy_app forwards --delete when delete=True
        with patch("airlock.mcp.server._run_command", return_value={"success": True, "returncode": 0, "stdout": "ok", "stderr": ""}) as mock_run:
            res = airlock_deploy_app("demo-control-vm", delete=True, dry_run=True)
        self.assertTrue(res["success"])
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("--delete", called_cmd)
        self.assertIn("--dry-run", called_cmd)

    def test_dispatcher_resolves_vm_dynamically_from_cwd_and_refuses_default(self):
        import subprocess

        sync_mod = _load_module(
            "generate_sync_dispatcher_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_rsync" / "scripts" / "generate_sync.py",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inv_dir = root / "inventory"
            proj_a = root / "project-a"
            proj_a_sub = proj_a / "src"
            proj_a_sub.mkdir(parents=True)
            unregistered = root / "unregistered-project"
            unregistered.mkdir()

            vm_a_dir = inv_dir / "localhost" / "project-a-vm"
            vm_a_dir.mkdir(parents=True)
            (vm_a_dir / "vm.yaml").write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "project-a-vm",
                        "host": "localhost",
                        "blueprint": {
                            "repositories": [{
                                "name": "project-a",
                                "local_path": str(proj_a),
                                "remote_path": "~/project-a/",
                            }]
                        },
                    }
                }),
                encoding="utf-8",
            )
            vm_a_script = vm_a_dir / "deploy_app_to_airlock.sh"
            vm_a_script.write_text('#!/usr/bin/env bash\necho "DISPATCHED_TO=project-a-vm ARGS=$*"\n', encoding="utf-8")
            vm_a_script.chmod(0o755)

            dispatcher = root / "deploy_app_to_airlock.sh"
            disp_content = sync_mod.generate_dispatcher_script()
            self.assertNotIn("DEFAULT_VM=", disp_content)
            dispatcher.write_text(disp_content, encoding="utf-8")
            dispatcher.chmod(0o755)

            env = {**os.environ, "AIRLOCK_INVENTORY_DIR": str(inv_dir), "HOME": str(root / "empty_home")}

            # 1. Running from project-a (or subdirectory) resolves project-a-vm automatically
            p1 = subprocess.run([str(dispatcher), "--offline"], cwd=str(proj_a_sub), env=env, capture_output=True, text=True)
            self.assertEqual(p1.returncode, 0, p1.stderr)
            self.assertIn("DISPATCHED_TO=project-a-vm ARGS=--offline", p1.stdout)

            # 2. Running from an unregistered directory refuses to fall back to any default VM
            p2 = subprocess.run([str(dispatcher), "--offline"], cwd=str(unregistered), env=env, capture_output=True, text=True)
            self.assertNotEqual(p2.returncode, 0)
            self.assertIn("Refusing to deploy to an arbitrary default VM", p2.stderr)

            # 3. Ambiguous match (two VMs pointing to same project-a) fails fast with clear error
            vm_b_dir = inv_dir / "localhost" / "project-a-alt-vm"
            vm_b_dir.mkdir(parents=True)
            (vm_b_dir / "vm.yaml").write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "project-a-alt-vm",
                        "host": "localhost",
                        "blueprint": {
                            "repositories": [{
                                "name": "project-a",
                                "local_path": str(proj_a),
                                "remote_path": "~/project-a/",
                            }]
                        },
                    }
                }),
                encoding="utf-8",
            )
            p3 = subprocess.run([str(dispatcher), "--offline"], cwd=str(proj_a), env=env, capture_output=True, text=True)
            self.assertNotEqual(p3.returncode, 0)
            self.assertIn("Multiple VMs match current directory", p3.stderr)

    def test_include_expansion_preserves_dotfile_and_dotdir_prefixes(self):
        import subprocess

        sync_mod = _load_module(
            "generate_sync_dotdir_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_rsync" / "scripts" / "generate_sync.py",
        )

        # 1. Unit check on _expand_include_patterns for both ".agents/..." and "./.agents/..."
        expected = [
            ".agents/",
            ".agents/skills/",
            ".agents/skills/foo/",
            ".agents/skills/foo/scripts/",
            ".agents/skills/foo/scripts/***",
        ]
        self.assertEqual(
            sync_mod._expand_include_patterns([".agents/skills/foo/scripts/"]),
            expected,
        )
        self.assertEqual(
            sync_mod._expand_include_patterns(["./.agents/skills/foo/scripts/"]),
            expected,
        )

        # 2. End-to-end --offline rsync verification: targeted scripts under .agents/ are included
        #    while sibling files under .agents/ are excluded by .agents/**
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_dir = root / "dot-app"
            (app_dir / ".agents" / "skills" / "foo" / "scripts").mkdir(parents=True)
            (app_dir / ".agents" / "skills" / "bar").mkdir(parents=True)
            (app_dir / ".agents" / "skills" / "foo" / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
            (app_dir / ".agents" / "skills" / "bar" / "SKILL.md").write_text("# secret\n", encoding="utf-8")
            (app_dir / ".agents" / "notes.txt").write_text("ignore\n", encoding="utf-8")

            repos = [{
                "name": "dot-app",
                "local_path": str(app_dir),
                "remote_path": "~/dot-app/",
                "internal_port": 8000,
                "external_port": 8001,
                "post_deploy": "",
                "include": [".agents/skills/foo/scripts/"],
                "exclude": [".agents/**"],
            }]
            exclusions = sync_mod.evaluate_project_exclusions()
            sp = root / "deploy_app_to_airlock.sh"
            sp.write_text(sync_mod.generate_shell_script("dot-vm", repos, exclusions), encoding="utf-8")
            sp.chmod(0o755)

            proc = subprocess.run([str(sp), "--offline"], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(".agents/skills/foo/scripts/run.py", proc.stdout)
            self.assertNotIn(".agents/skills/bar/SKILL.md", proc.stdout)
            self.assertNotIn(".agents/notes.txt", proc.stdout)

    def test_snapshot_delete_gates_native_fallback_vm_yaml_sync_mcp_and_cli(self):
        import json
        from airlock.mcp.server import airlock_snapshot

        deploy_mod = _load_module(
            "deploy_vm_snap_del_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_deploy" / "scripts" / "deploy_vm.py",
        )

        params = {
            "VM_NAME": "snap-vm",
            "SNAPSHOT_BASE_NAME": "snapshot-base-clean",
        }

        # 1. Empty or 'default' snapshot_name rejected
        self.assertEqual(deploy_mod.execute_snapshot_delete(params, "localhost", ""), 1)
        self.assertEqual(deploy_mod.execute_snapshot_delete(params, "localhost", "default"), 1)

        # 2. Baseline snapshot protected unless force=True
        self.assertEqual(deploy_mod.execute_snapshot_delete(params, "localhost", "snapshot-base-clean", force=False), 1)

        # 3. Nonexistent snapshot rejected
        with patch.object(deploy_mod, "run_cmd_status", return_value=(0, "snapshot-base-clean\nsnap-1\n", "")):
            self.assertEqual(deploy_mod.execute_snapshot_delete(params, "localhost", "snap-missing"), 1)

        # 4. Native delete path: deletes snap-1, removes unreferenced overlay, updates vm.yaml, emits JSON
        with tempfile.TemporaryDirectory() as tmpdir:
            vm_yaml = Path(tmpdir) / "vm.yaml"
            vm_yaml.write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "snap-vm",
                        "host": "localhost",
                        "disks": [
                            {"device": "disk", "target": "vda", "bus": "virtio", "format": "qcow2", "source": "/var/lib/libvirt/images/snap-vm.snap-2"},
                        ],
                    }
                }),
                encoding="utf-8",
            )
            snap_xml = (
                "<domainsnapshot><disks>"
                "<disk name='vda' snapshot='external'><source file='/var/lib/libvirt/images/snap-vm.snap-1'/></disk>"
                "</disks></domainsnapshot>"
            )
            list_calls = {"n": 0}

            def fake_native_run(cmd, host=None):
                if cmd[:4] == ["sudo", "virsh", "snapshot-list", "snap-vm"]:
                    list_calls["n"] += 1
                    if list_calls["n"] == 1:
                        return 0, "snapshot-base-clean\nsnap-1\nsnap-2\n", ""
                    return 0, "snapshot-base-clean\nsnap-2\n", ""
                if cmd[:4] == ["sudo", "virsh", "snapshot-dumpxml", "snap-vm"]:
                    if cmd[4] == "snap-1":
                        return 0, snap_xml, ""
                    return 0, "<domainsnapshot><disks/></domainsnapshot>", ""
                if cmd[:3] == ["sudo", "virsh", "snapshot-delete"]:
                    return 0, "Domain snapshot snap-1 deleted", ""
                if cmd[:3] == ["sudo", "virsh", "domblklist"]:
                    return 0, " Type Device Target Source\n-----\n file disk vda /var/lib/libvirt/images/snap-vm.snapshot-base-clean\n", ""
                if cmd[:3] == ["sudo", "qemu-img", "info"]:
                    return 0, "image: /var/lib/libvirt/images/snap-vm.snapshot-base-clean\nbacking file: /var/lib/libvirt/images/snap-vm.qcow2\n", ""
                if cmd[:3] == ["sudo", "bash", "-c"]:
                    return 0, "REMOVED", ""
                return 0, "", ""

            buf = io.StringIO()
            with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_native_run), redirect_stdout(buf):
                rc = deploy_mod.execute_snapshot_delete(params, "localhost", "snap-1", config_path=vm_yaml)
            self.assertEqual(rc, 0)
            out_text = buf.getvalue()
            marker = "AIRLOCK_SNAPSHOT_DELETE_JSON:"
            json_line = next(ln for ln in out_text.splitlines() if ln.startswith(marker))
            payload = json.loads(json_line[len(marker):])
            self.assertEqual(payload["deleted_snapshot"], "snap-1")
            self.assertEqual(payload["remaining_snapshots"], ["snapshot-base-clean", "snap-2"])
            self.assertEqual(payload["removed_overlays"], ["/var/lib/libvirt/images/snap-vm.snap-1"])
            self.assertTrue(payload["vm_yaml_updated"])
            reloaded = yaml.safe_load(vm_yaml.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["vm"]["disks"][0]["source"], "/var/lib/libvirt/images/snap-vm.snapshot-base-clean")

        # 5. Fallback path on older libvirt (native returns unsupported -> blockcommit + --metadata)
        fb_cmds: list[list[str]] = []
        fb_list = {"n": 0}

        def fake_fallback_run(cmd, host=None):
            fb_cmds.append(list(cmd))
            if cmd[:4] == ["sudo", "virsh", "snapshot-list", "snap-vm"]:
                fb_list["n"] += 1
                return (0, "snapshot-base-clean\nsnap-2\n", "") if fb_list["n"] == 1 else (0, "snapshot-base-clean\n", "")
            if cmd[:4] == ["sudo", "virsh", "snapshot-dumpxml", "snap-vm"]:
                if cmd[4] == "snap-2":
                    return 0, "<domainsnapshot><disks><disk name='vda'><source file='/var/lib/libvirt/images/snap-vm.snap-2'/></disk></disks></domainsnapshot>", ""
                return 0, "<domainsnapshot><disks/></domainsnapshot>", ""
            if cmd[:3] == ["sudo", "virsh", "snapshot-delete"] and "--metadata" not in cmd:
                return 1, "", "error: unsupported configuration: deletion of external disk snapshots not supported"
            if cmd[:3] == ["sudo", "virsh", "dominfo"]:
                return 0, "State:          running\n", ""
            if cmd[:3] == ["sudo", "virsh", "domblklist"]:
                return 0, " Type Device Target Source\n-----\n file disk vda /var/lib/libvirt/images/snap-vm.snap-2\n", ""
            if cmd[:3] == ["sudo", "virsh", "blockcommit"]:
                return 0, "Successfully pivoted", ""
            if cmd[:3] == ["sudo", "qemu-img", "info"]:
                return 0, "image: /var/lib/libvirt/images/snap-vm.snapshot-base-clean\n", ""
            return 0, "", ""

        with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_fallback_run), redirect_stdout(io.StringIO()):
            rc_fb = deploy_mod.execute_snapshot_delete(params, "localhost", "snap-2")
        self.assertEqual(rc_fb, 0)
        self.assertTrue(any(c[:3] == ["sudo", "virsh", "blockcommit"] for c in fb_cmds))
        self.assertTrue(any("--metadata" in c for c in fb_cmds))

        # 6. MCP airlock_snapshot(action="delete") rejects empty name and parses structured output
        bad_mcp = airlock_snapshot("snap-vm", action="delete", snapshot_name="")
        self.assertFalse(bad_mcp["success"])
        mock_stdout = 'AIRLOCK_SNAPSHOT_DELETE_JSON:{"deleted_snapshot": "snap-1", "remaining_snapshots": ["snapshot-base-clean"], "active_disks": [], "removed_overlays": [], "vm_yaml_updated": true, "dry_run": false}'
        with patch("airlock.mcp.server._run_command", return_value={"success": True, "returncode": 0, "stdout": mock_stdout, "stderr": ""}) as mock_run:
            mcp_res = airlock_snapshot("snap-vm", action="delete", snapshot_name="snap-1", force=True, dry_run=True)
        self.assertTrue(mcp_res["success"])
        self.assertEqual(mcp_res["deleted_snapshot"], "snap-1")
        self.assertEqual(mcp_res["remaining_snapshots"], ["snapshot-base-clean"])
        called = mock_run.call_args[0][0]
        self.assertIn("--delete-snapshot", called)
        self.assertIn("--force", called)
        self.assertIn("--dry-run", called)

        # 7. CLI `airlock snapshot <vm> delete <snap> --force --dry-run`
        with patch("airlock.cli.airlock_snapshot", return_value={"success": True, "stdout": "deleted"}) as mock_cli_snap, \
             redirect_stdout(io.StringIO()):
            rc_cli = cli.main(["snapshot", "snap-vm", "delete", "snap-1", "--force", "--dry-run"])
        self.assertEqual(rc_cli, 0)
        mock_cli_snap.assert_called_once_with("snap-vm", action="delete", snapshot_name="snap-1", force=True, dry_run=True)

    def test_snapshot_flatten_noop_dry_run_force_golden_protection_and_cli(self):
        import json
        from airlock.mcp.server import airlock_snapshot

        deploy_mod = _load_module(
            "deploy_vm_snap_flatten_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_deploy" / "scripts" / "deploy_vm.py",
        )
        params = {"VM_NAME": "flat-vm", "SNAPSHOT_BASE_NAME": "snapshot-base-clean"}

        # 1. Early no-op when VM has no snapshots and no overlays above VM base disk
        def fake_noop(cmd, host=None):
            if cmd[:3] == ["sudo", "virsh", "snapshot-list"]:
                return 0, "", ""
            if cmd[:3] == ["sudo", "virsh", "domblklist"]:
                return 0, " Type Device Target Source\n-----\n file disk vda /var/lib/libvirt/images/flat-vm.qcow2\n", ""
            if cmd[:3] == ["sudo", "qemu-img", "info"]:
                return 0, "image: /var/lib/libvirt/images/flat-vm.qcow2\nimage: /var/lib/libvirt/images/airlock-golden-ubuntu-24.04.qcow2\n", ""
            return 0, "", ""

        buf_noop = io.StringIO()
        with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_noop), redirect_stdout(buf_noop):
            rc_noop = deploy_mod.execute_snapshot_flatten(params, "localhost")
        self.assertEqual(rc_noop, 0)
        marker = "AIRLOCK_SNAPSHOT_FLATTEN_JSON:"
        noop_payload = json.loads(next(ln for ln in buf_noop.getvalue().splitlines() if ln.startswith(marker))[len(marker):])
        self.assertFalse(noop_payload["changed"])

        # 2. Full 3-layer chain: dry-run, force=False rejection, and force=True execution
        chain_3 = (
            "image: /var/lib/libvirt/images/flat-vm.snap-2\n"
            "image: /var/lib/libvirt/images/flat-vm.snap-1\n"
            "image: /var/lib/libvirt/images/flat-vm.snapshot-base-clean\n"
            "image: /var/lib/libvirt/images/flat-vm.qcow2\n"
            "image: /var/lib/libvirt/images/airlock-golden-ubuntu-24.04.qcow2\n"
        )

        def fake_inspect(cmd, host=None):
            if cmd[:3] == ["sudo", "virsh", "snapshot-list"]:
                return 0, "snapshot-base-clean\nsnap-1\nsnap-2\n", ""
            if cmd[:3] == ["sudo", "virsh", "domblklist"]:
                return 0, " Type Device Target Source\n-----\n file disk vda /var/lib/libvirt/images/flat-vm.snap-2\n", ""
            if cmd[:3] == ["sudo", "qemu-img", "info"]:
                return 0, chain_3, ""
            if cmd[:3] == ["sudo", "virsh", "snapshot-dumpxml"]:
                return 0, "<domainsnapshot><disks/></domainsnapshot>", ""
            return 0, "", ""

        # 2a. dry_run=True succeeds without force and emits plan stopping at flat-vm.qcow2 (NOT golden image)
        buf_dry = io.StringIO()
        with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_inspect), redirect_stdout(buf_dry):
            rc_dry = deploy_mod.execute_snapshot_flatten(params, "localhost", snapshot_name="snapshot-base-clean", dry_run=True)
        self.assertEqual(rc_dry, 0)
        self.assertIn("--base /var/lib/libvirt/images/flat-vm.qcow2 --active --pivot --wait", buf_dry.getvalue())
        self.assertNotIn("--base /var/lib/libvirt/images/airlock-golden", buf_dry.getvalue())

        # 2b. force=False (non-dry-run) refuses to discard rollback points
        with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_inspect):
            rc_noforce = deploy_mod.execute_snapshot_flatten(params, "localhost", force=False, dry_run=False)
        self.assertEqual(rc_noforce, 1)

        # 2c. force=True commits into flat-vm.qcow2, deletes metadata, unlinks 3 overlays, creates fresh baseline, syncs vm.yaml
        with tempfile.TemporaryDirectory() as tmpdir:
            vm_yaml = Path(tmpdir) / "vm.yaml"
            vm_yaml.write_text(
                yaml.safe_dump({
                    "vm": {
                        "name": "flat-vm",
                        "host": "localhost",
                        "disks": [{"device": "disk", "target": "vda", "bus": "virtio", "format": "qcow2", "source": "/var/lib/libvirt/images/flat-vm.snap-2"}],
                        "blueprint": {"snapshot_base_name": "snapshot-base-clean"},
                    }
                }),
                encoding="utf-8",
            )
            recorded: list[list[str]] = []
            state_flags = {"committed": False, "created_new": False}

            def fake_flatten_exec(cmd, host=None):
                recorded.append(list(cmd))
                if cmd[:3] == ["sudo", "virsh", "snapshot-list"]:
                    if state_flags["created_new"]:
                        return 0, "snapshot-python314-uv\n", ""
                    return 0, "snapshot-base-clean\nsnap-1\nsnap-2\n", ""
                if cmd[:3] == ["sudo", "virsh", "domblklist"]:
                    if state_flags["created_new"]:
                        return 0, " Type Device Target Source\n-----\n file disk vda /var/lib/libvirt/images/flat-vm.snapshot-python314-uv\n", ""
                    if state_flags["committed"]:
                        return 0, " Type Device Target Source\n-----\n file disk vda /var/lib/libvirt/images/flat-vm.qcow2\n", ""
                    return 0, " Type Device Target Source\n-----\n file disk vda /var/lib/libvirt/images/flat-vm.snap-2\n", ""
                if cmd[:3] == ["sudo", "qemu-img", "info"]:
                    if state_flags["committed"]:
                        return 0, "image: /var/lib/libvirt/images/flat-vm.qcow2\nimage: /var/lib/libvirt/images/airlock-golden-ubuntu-24.04.qcow2\n", ""
                    return 0, chain_3, ""
                if cmd[:3] == ["sudo", "virsh", "snapshot-dumpxml"]:
                    return 0, "<domainsnapshot><disks/></domainsnapshot>", ""
                if cmd[:3] == ["sudo", "virsh", "dominfo"]:
                    return 0, "State:          running\n", ""
                if cmd[:3] == ["sudo", "virsh", "blockcommit"]:
                    state_flags["committed"] = True
                    return 0, "Successfully pivoted", ""
                if cmd[:3] == ["sudo", "virsh", "snapshot-delete"]:
                    return 0, "Deleted", ""
                if cmd[:3] == ["sudo", "bash", "-c"]:
                    return 0, "REMOVED", ""
                if cmd[:3] == ["sudo", "virsh", "snapshot-create-as"]:
                    state_flags["created_new"] = True
                    return 0, "Domain snapshot snapshot-python314-uv created", ""
                return 0, "", ""

            buf_exec = io.StringIO()
            with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_flatten_exec), redirect_stdout(buf_exec):
                rc_exec = deploy_mod.execute_snapshot_flatten(
                    params, "localhost", snapshot_name="snapshot-python314-uv", force=True, config_path=vm_yaml
                )
            self.assertEqual(rc_exec, 0)
            exec_payload = json.loads(next(ln for ln in buf_exec.getvalue().splitlines() if ln.startswith(marker))[len(marker):])
            self.assertTrue(exec_payload["changed"])
            self.assertEqual(len(exec_payload["removed_snapshots"]), 3)
            self.assertEqual(len(exec_payload["unlinked_files"]), 3)
            self.assertEqual(exec_payload["new_snapshot"], "snapshot-python314-uv")
            self.assertEqual(exec_payload["remaining_snapshots"], ["snapshot-python314-uv"])
            self.assertTrue(exec_payload["vm_yaml_updated"])

            # Verify blockcommit explicitly targeted --base /var/lib/libvirt/images/flat-vm.qcow2
            bc_cmds = [c for c in recorded if c[:3] == ["sudo", "virsh", "blockcommit"]]
            self.assertEqual(len(bc_cmds), 1)
            self.assertIn("--base", bc_cmds[0])
            self.assertIn("/var/lib/libvirt/images/flat-vm.qcow2", bc_cmds[0])

            # Verify every qemu-img info call passes -U (--force-share) for running QEMU domains
            qimg_cmds = [c for c in recorded if c[:3] == ["sudo", "qemu-img", "info"]]
            self.assertTrue(len(qimg_cmds) >= 2)
            for qc in qimg_cmds:
                self.assertIn("-U", qc)

            # Verify vm.yaml updated both disk source and snapshot_base_name
            reloaded = yaml.safe_load(vm_yaml.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["vm"]["disks"][0]["source"], "/var/lib/libvirt/images/flat-vm.snapshot-python314-uv")
            self.assertEqual(reloaded["vm"]["blueprint"]["snapshot_base_name"], "snapshot-python314-uv")

        # 2d. Fail-closed: if qemu-img info -U fails (e.g. lock error), flatten aborts instead of deleting metadata
        def fake_qimg_fail(cmd, host=None):
            if cmd[:3] == ["sudo", "virsh", "snapshot-list"]:
                return 0, "snapshot-base-clean\n", ""
            if cmd[:3] == ["sudo", "virsh", "domblklist"]:
                return 0, " Type Device Target Source\n-----\n file disk vda /var/lib/libvirt/images/flat-vm.snapshot-base-clean\n", ""
            if cmd[:3] == ["sudo", "qemu-img", "info"]:
                return 1, "", 'Failed to get shared "write" lock'
            return 0, "", ""

        with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_qimg_fail):
            rc_fail = deploy_mod.execute_snapshot_flatten(params, "localhost", force=True)
        self.assertEqual(rc_fail, 1)

        # 3. MCP airlock_snapshot(action="flatten") & CLI `airlock snapshot flat-vm flatten`
        mock_out = 'AIRLOCK_SNAPSHOT_FLATTEN_JSON:{"changed": true, "removed_snapshots": ["snap-1"], "unlinked_files": ["/var/lib/libvirt/images/flat-vm.snap-1"], "active_disks": [], "new_snapshot": "snapshot-base-clean", "remaining_snapshots": ["snapshot-base-clean"], "vm_yaml_updated": true, "dry_run": false}'
        with patch("airlock.mcp.server._run_command", return_value={"success": True, "returncode": 0, "stdout": mock_out, "stderr": ""}) as mock_run:
            res_mcp = airlock_snapshot("flat-vm", action="flatten", snapshot_name="snapshot-base-clean", force=True)
        self.assertTrue(res_mcp["success"])
        self.assertEqual(res_mcp["new_snapshot"], "snapshot-base-clean")
        self.assertEqual(res_mcp["removed_snapshots"], ["snap-1"])
        called = mock_run.call_args[0][0]
        self.assertIn("--flatten-snapshots", called)
        self.assertIn("snapshot-base-clean", called)
        self.assertIn("--force", called)

        with patch("airlock.cli.airlock_snapshot", return_value={"success": True, "stdout": "flattened"}) as mock_cli_flat, \
             redirect_stdout(io.StringIO()):
            rc_cli = cli.main(["snapshot", "flat-vm", "flatten", "snapshot-base-clean", "--force", "--dry-run"])
        self.assertEqual(rc_cli, 0)
        mock_cli_flat.assert_called_once_with("flat-vm", action="flatten", snapshot_name="snapshot-base-clean", force=True, dry_run=True)

    def test_rootless_hypervisor_mode_deploy_preflight_and_mcp(self):
        from airlock.mcp.server import airlock_create_vm

        deploy_mod = _load_module(
            "deploy_vm_rootless_unit",
            AIRLOCK_ROOT / ".agents" / "skills" / "airlock_deploy" / "scripts" / "deploy_vm.py",
        )
        deploy_mod._ROOTLESS_CACHE.clear()

        # 1. is_rootless_host detects rootless when sudo -n fails and virsh -c qemu:///system succeeds
        def fake_rootless_probe(cmd, host=None):
            if cmd == ["sudo", "-n", "true"]:
                return 1, "", "sudo: a password is required"
            if cmd == ["virsh", "-c", "qemu:///system", "uri"]:
                return 0, "qemu:///system", ""
            return 0, "", ""

        with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_rootless_probe):
            self.assertTrue(deploy_mod.is_rootless_host("rootless-hv"))
            self.assertEqual(deploy_mod.virsh_cmd("rootless-hv", "list"), ["virsh", "-c", "qemu:///system", "list"])
            self.assertEqual(deploy_mod.priv_cmd("rootless-hv", "qemu-img", "info"), ["qemu-img", "info"])
            self.assertEqual(
                deploy_mod.virt_install_cmd("rootless-hv", "--name", "demo"),
                ["virt-install", "--connect", "qemu:///system", "--name", "demo"],
            )

        # 2. setup_hypervisor_proxy uses ~/.config/systemd/user and systemctl --user in rootless mode
        captured_scripts: list[str] = []

        def fake_proxy_run(cmd, host=None):
            if cmd == ["sudo", "-n", "true"]:
                return 1, "", ""
            if cmd == ["virsh", "-c", "qemu:///system", "uri"]:
                return 0, "qemu:///system", ""
            if cmd[:2] == ["bash", "-c"]:
                captured_scripts.append(cmd[2])
                if "systemd-socket-proxyd" in cmd[2]:
                    return 0, "/usr/lib/systemd/systemd-socket-proxyd", ""
                return 0, "", ""
            return 0, "", ""

        with patch.object(deploy_mod, "run_cmd_status", side_effect=fake_proxy_run), redirect_stdout(io.StringIO()):
            deploy_mod.setup_hypervisor_proxy(
                {"VM_NAME": "rl-vm", "VM_IP": "192.168.122.240", "PROXY_EXTERNAL_PORT": "8001", "APP_INTERNAL_PORT": "8000", "REPOSITORIES": []},
                "rootless-hv",
                dry_run=False,
            )
        setup_script = next(s for s in captured_scripts if "systemctl --user" in s)
        self.assertIn("$HOME/.config/systemd/user/rl-vm-p8001-proxy.socket", setup_script)
        self.assertNotIn("sudo ", setup_script)

        # 3. preflight passes Gate 1 on ROOTLESS_OK and airlock_create_vm records user-systemd proxy path
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            inv_dir = root / "inventory"
            host_dir = inv_dir / "rootless-hv"
            host_dir.mkdir(parents=True)
            (host_dir / "vmhost.yaml").write_text(
                yaml.safe_dump({
                    "vmhost": {
                        "name": "rootless-hv",
                        "connection": {"type": "ssh", "host": "rootless-hv", "privilege_mode": "rootless"},
                        "system": {"os": {"pretty_name": "Ubuntu 24.04 LTS"}},
                        "hardware": {"cpu": {"total_cores": 8, "model": "CPU"}, "memory": {"total_mib": 8192, "total_gb": 8}},
                        "virtualization": {"networks": [{"name": "default", "bridge": "virbr0", "ip": "192.168.122.1", "netmask": "255.255.255.0"}]},
                    }
                }),
                encoding="utf-8",
            )
            app_dir = root / "rl-app"
            app_dir.mkdir()
            (app_dir / "pyproject.toml").write_text('[project]\nname = "rl-app"\n', encoding="utf-8")

            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            os.environ["AIRLOCK_INVENTORY_DIR"] = str(inv_dir)
            try:
                with patch("airlock.ports.get_inventory_used_ports", return_value=set()), \
                     patch("airlock.ports.get_hypervisor_live_ports", return_value=set()):
                    res_create = airlock_create_vm(local_path=str(app_dir), vmhost="rootless-hv", vm_name="rl-vm", internal_port=8000, external_port=8001)
                self.assertTrue(res_create["success"], res_create)
                vdoc = yaml.safe_load((host_dir / "rl-vm" / "vm.yaml").read_text(encoding="utf-8"))
                self.assertEqual(vdoc["vm"]["blueprint"]["proxy_socket_service"], "~/.config/systemd/user/rl-vm-p8001-proxy.socket")

                def fake_preflight_ssh(host, cmd, timeout=5):
                    if "ROOTLESS_OK" in cmd:
                        return 0, "ROOTLESS_OK", ""
                    if "KVM_OK" in cmd:
                        return 0, "KVM_OK", ""
                    if "virsh -c qemu:///system uri" in cmd:
                        return 0, "qemu:///system", ""
                    if "SILENT" in cmd:
                        return 0, "SILENT", ""
                    if "df -BG" in cmd:
                        return 0, "100", ""
                    if "MemAvailable" in cmd:
                        return 0, "6000", ""
                    if "GOLDEN" in cmd:
                        return 0, "CACHED", ""
                    return 0, "", ""

                with patch("airlock.preflight._ssh_cmd", side_effect=fake_preflight_ssh), \
                     patch("airlock.preflight.get_hypervisor_live_ports", return_value=set()):
                    pf = validate_deployment_preflight("rl-vm")
                self.assertEqual(pf["verdict"], "READY_TO_DEPLOY")
                self.assertIn("rootless libvirt", pf["checks"]["hypervisor_access"]["message"])
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)


if __name__ == "__main__":
    unittest.main()
