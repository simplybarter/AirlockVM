"""AirlockVM MCP Control Plane Integration Permutations Test Suite.

Tests all permutations across the AirlockVM MCP Server and sub-engines:
- Permutation A: Core Observability & Inspection
- Permutation B: Stack Detection & Multi-Framework Inference
- Permutation C: Hypervisor Placement, Networking, & Port Allocation
- Permutation D: Dedicated VM Lifecycle (derived via MCP tools for DummyAirlockApp)
- Permutation E: Shared Attach Topology Lifecycle & Idempotency
- Permutation F: Drift Detection & Diagnostic Intelligence
- Permutation G: Security Isolation & Workstation Zero-Workload Invariant
- Permutation H: Multi-IDE Client Connector CLI (connect_airlock.sh)
- Permutation I: Live Deployment & Hypervisor Proxy Reachability (http://remotehost:3002/)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import urllib.request
import yaml

import airlock.mcp.server as srv
from airlock.detector import detect_application
from airlock.diagnostics import diagnose_workload_failure
from airlock.drift import audit_vm_drift
from airlock.inventory import get_inventory_search_dirs, get_primary_inventory_dir, find_vm_yaml
from airlock.network import allocate_vm_network
from airlock.placement import inspect_hypervisor, select_optimal_hypervisor
from airlock.ports import allocate_external_port, get_inventory_used_ports
from airlock.preflight import validate_deployment_preflight
from airlock.security import audit_vm_security


AIRLOCK_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_DIR = Path(os.environ.get("AIRLOCK_INVENTORY_DIR", Path.home() / ".airlock" / "inventory"))
DUMMY_APP_DIR = (AIRLOCK_ROOT / ".." / "DummyAirlockApp").resolve()
EXISTING_HOSTS = [p.name for p in INVENTORY_DIR.glob("*") if (p / "vmhost.yaml").exists()] if INVENTORY_DIR.exists() else []
REMOTE_HOST = "remotehost" if "remotehost" in EXISTING_HOSTS else (
    next((h for h in EXISTING_HOSTS if h != "localhost"), "remotehost")
)


class TestPermutationA_ObservabilityAndInspection(unittest.TestCase):
    """Permutation A: Verifies core ping, list, and status reporting."""

    def test_airlock_ping(self):
        result = srv.airlock_ping()
        self.assertIn("AirlockVM MCP Server is online", result)
        self.assertIn("Python", result)

    def test_airlock_list(self):
        result = srv.airlock_list()
        self.assertTrue(result.get("success"))
        self.assertGreaterEqual(result.get("total_vms", 0), 1)
        vm_names = [v["vm_name"] for v in result.get("vms", [])]
        self.assertIn("dummy-airlock-app-vm", vm_names)

    def test_airlock_status_unprovisioned(self):
        result = srv.airlock_status("dummy-airlock-app-vm")
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("vm_name"), "dummy-airlock-app-vm")
        self.assertIn("dummy-airlock-app-vm", result.get("stdout", ""))


class TestPermutationB_ApplicationDetection(unittest.TestCase):
    """Permutation B: Tests stack detection across Node.js, Python, and Docker."""

    def test_detect_dummy_airlock_app_nodejs(self):
        self.assertTrue(DUMMY_APP_DIR.exists(), f"DummyAirlockApp not found at {DUMMY_APP_DIR}")
        info = srv.airlock_detect_app(str(DUMMY_APP_DIR))
        self.assertTrue(info.get("success"))
        self.assertEqual(info.get("framework"), "Node.js")
        self.assertEqual(info.get("internal_port"), 3000)
        self.assertEqual(info.get("post_deploy"), "npm start")
        self.assertEqual(info.get("confidence"), "high")

    def test_detect_python_fastapi_app(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            req_file = tmppath / "requirements.txt"
            req_file.write_text("fastapi>=0.100.0\nuvicorn>=0.23.0\n", encoding="utf-8")
            main_py = tmppath / "main.py"
            main_py.write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")

            info = detect_application(tmppath)
            self.assertTrue(info.get("success"))
            self.assertTrue(info.get("framework", "").startswith("Python"))
            self.assertEqual(info.get("internal_port"), 8000)
            self.assertIn("uvicorn", info.get("post_deploy"))

    def test_detect_docker_app(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            dockerfile = tmppath / "Dockerfile"
            dockerfile.write_text("FROM alpine:latest\nEXPOSE 9000\nCMD ['sh']\n", encoding="utf-8")

            info = detect_application(tmppath)
            self.assertTrue(info.get("success"))
            self.assertEqual(info.get("framework"), "Docker")
            self.assertEqual(info.get("internal_port"), 9000)
            self.assertIn("docker run", info.get("post_deploy"))

    def test_detect_invalid_path(self):
        info = detect_application("/tmp/nonexistent-path-for-airlock-test-999")
        self.assertFalse(info.get("success"))
        self.assertIn("does not exist", info.get("error", ""))


class TestPermutationC_PlacementAndNetworking(unittest.TestCase):
    """Permutation C: Tests placement optimizer, network allocation, and port management."""

    def test_hypervisor_inspection(self):
        info = inspect_hypervisor(REMOTE_HOST)
        self.assertTrue(info.get("success"))
        self.assertEqual(info.get("vmhost"), REMOTE_HOST)
        self.assertGreater(info.get("available_ram_mib", 0), 1024)

    def test_host_placement_optimizer(self):
        result = srv.airlock_select_host(required_vcpu=2, required_ram_mib=2048, required_disk_gb=20)
        self.assertTrue(result.get("success"))
        self.assertIn("selected_host", result)
        self.assertIn("selected_hypervisor", result)
        self.assertIn(result["selected_host"], EXISTING_HOSTS or [REMOTE_HOST, "localhost"])
        self.assertIn(result["selected_hypervisor"], EXISTING_HOSTS or [REMOTE_HOST, "localhost"])

    def test_network_allocation_unique(self):
        alloc = srv.airlock_alloc_network(vmhost=REMOTE_HOST)
        self.assertTrue(alloc.get("success"))
        self.assertEqual(alloc.get("vmhost"), REMOTE_HOST)
        self.assertTrue(alloc.get("allocated_ip", "").startswith("192.168.122."))
        self.assertTrue(alloc.get("allocated_mac", "").startswith("52:54:00:"))

    def test_port_allocation_auto(self):
        port_info = srv.airlock_find_port(3000)
        self.assertTrue(port_info.get("success"))
        self.assertGreaterEqual(port_info.get("allocated_external_port", 0), 3001)

    def test_port_allocation_collision_avoidance(self):
        # 3002 is registered to dummy-airlock-app-vm; ensure allocation shifts to next free
        port, reason = allocate_external_port(vmhost=REMOTE_HOST, internal_port=3000, preferred_port=3002)
        self.assertNotEqual(port, 3002)
        self.assertGreaterEqual(port, 3001)


class TestPermutationD_DedicatedVMLifecycle(unittest.TestCase):
    """Permutation D: Validates dedicated VM lifecycle derived for DummyAirlockApp."""

    def test_dummy_airlock_app_vm_ssot_exists(self):
        yaml_path = INVENTORY_DIR / REMOTE_HOST / "dummy-airlock-app-vm" / "vm.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}

        vm_data = doc.get("vm", {})
        self.assertEqual(vm_data.get("name"), "dummy-airlock-app-vm")
        self.assertEqual(vm_data.get("host"), REMOTE_HOST)
        bp = vm_data.get("blueprint", {})
        self.assertEqual(bp.get("project_name"), "dummy-airlock-app")
        self.assertEqual(bp.get("app_internal_port"), 3000)
        self.assertEqual(bp.get("proxy_external_port"), 3002)

    def test_dummy_airlock_app_vm_runbook_clean(self):
        runbook_path = INVENTORY_DIR / REMOTE_HOST / "dummy-airlock-app-vm" / "VM.md"
        self.assertTrue(runbook_path.exists())
        content = runbook_path.read_text(encoding="utf-8")
        self.assertNotIn("{{", content)
        self.assertNotIn("}}", content)
        self.assertIn("dummy-airlock-app-vm", content)
        self.assertIn("192.168.122.231", content)
        self.assertIn("3002", content)

    def test_preflight_audit_gates_ready(self):
        pf = srv.airlock_preflight("nhr-workhorse-02")
        self.assertTrue(pf.get("success"))
        self.assertEqual(pf.get("verdict"), "READY_TO_DEPLOY")
        self.assertEqual(pf.get("confidence_score"), 1.0)
        self.assertIn("8/8", str(pf.get("checks_passed")))

    def test_preflight_audit_collision_detection(self):
        # dummy-airlock-app-vm is currently deployed and listening on 3002
        pf = srv.airlock_preflight("dummy-airlock-app-vm")
        self.assertTrue(pf.get("success"))
        self.assertEqual(pf.get("verdict"), "BLOCKED")
        self.assertIn("Static IP", pf.get("checks", {}).get("ip_silence", {}).get("message", ""))

    def test_deploy_vm_dry_run(self):
        res = srv.airlock_deploy_vm("dummy-airlock-app-vm", dry_run=True)
        self.assertTrue(res.get("success"))
        self.assertIn("pre-requisite gates passed", res.get("stdout", "").lower())

    def test_deploy_app_offline_filtering(self):
        res = srv.airlock_deploy_app("dummy-airlock-app-vm", offline=True)
        self.assertTrue(res.get("success"))
        self.assertIn("server.js", res.get("stdout", ""))
        self.assertIn("package.json", res.get("stdout", ""))
        self.assertNotIn(".git/", res.get("stdout", ""))


class TestPermutationE_SharedAttachTopology(unittest.TestCase):
    """Permutation E: Tests shared attach topology, idempotency, and overrides."""

    def setUp(self):
        self.test_vm = "test-shared-staging-vm"
        self.test_dir = INVENTORY_DIR / REMOTE_HOST / self.test_vm
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.test_yaml = self.test_dir / "vm.yaml"
        sample_doc = {
            "vm": {
                "name": self.test_vm,
                "host": REMOTE_HOST,
                "state": "shut off",
                "resources": {"vcpu": 2, "memory_mib": 2048},
                "network": {"interfaces": [{"ip": "192.168.122.249", "mac": "52:54:00:11:22:33"}]},
                "blueprint": {
                    "project_name": "StagingWorker",
                    "app_internal_port": 8000,
                    "proxy_external_port": 8001,
                    "repositories": [],
                },
            }
        }
        with open(self.test_yaml, "w", encoding="utf-8") as f:
            yaml.dump(sample_doc, f)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        # Regenerate sync script clean
        subprocess.run(
            [str(AIRLOCK_ROOT / ".venv/bin/python"), str(AIRLOCK_ROOT / "inventory/generate_deploy_script.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_attach_app_auto_inference(self):
        res = srv.airlock_attach_app(
            vm_name=self.test_vm,
            local_path=str(DUMMY_APP_DIR),
        )
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("app_name"), "dummy-airlock-app")
        self.assertEqual(res.get("internal_port"), 3000)
        self.assertEqual(res.get("post_deploy"), "npm start")

        with open(self.test_yaml, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        repos = doc.get("vm", {}).get("blueprint", {}).get("repositories", [])
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["name"], "dummy-airlock-app")

    def test_attach_app_idempotent_update(self):
        # Attach first time
        srv.airlock_attach_app(
            vm_name=self.test_vm,
            local_path=str(DUMMY_APP_DIR),
            app_name="dummy-airlock-app",
        )
        # Attach second time with override
        res2 = srv.airlock_attach_app(
            vm_name=self.test_vm,
            local_path=str(DUMMY_APP_DIR),
            app_name="dummy-airlock-app",
            internal_port=3000,
            post_deploy="node custom_start.js",
        )
        self.assertTrue(res2.get("success"))

        with open(self.test_yaml, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        repos = doc.get("vm", {}).get("blueprint", {}).get("repositories", [])
        self.assertEqual(len(repos), 1)  # Not duplicated
        self.assertEqual(repos[0]["post_deploy"], "node custom_start.js")


class TestPermutationF_DriftAndDiagnostics(unittest.TestCase):
    """Permutation F: Tests drift detection and root-cause diagnostics."""

    def test_audit_drift_live_vm(self):
        res = srv.airlock_audit_drift("dummy-airlock-app-vm")
        self.assertTrue(res.get("success"))
        self.assertTrue(res.get("domain_provisioned"))
        self.assertFalse(res.get("has_drift"))

    def test_audit_drift_unprovisioned(self):
        tmp_unprov = INVENTORY_DIR / REMOTE_HOST / "test-unprov-drift-vm"
        tmp_unprov.mkdir(parents=True, exist_ok=True)
        try:
            (tmp_unprov / "vm.yaml").write_text(f"vm:\n  name: test-unprov-drift-vm\n  host: {REMOTE_HOST}\n  state: shut off\n", encoding="utf-8")
            res = srv.airlock_audit_drift("test-unprov-drift-vm")
            self.assertTrue(res.get("success"))
            self.assertFalse(res.get("domain_provisioned"))
            self.assertGreaterEqual(res.get("total_drift_items", 0), 1)
            categories = [item.get("category") for item in res.get("drift_items", [])]
            self.assertIn("domain_presence", categories)
        finally:
            if tmp_unprov.exists():
                shutil.rmtree(tmp_unprov)

    def test_diagnose_nonexistent_vm(self):
        diag = srv.airlock_diagnose("nonexistent-test-vm-999")
        self.assertFalse(diag.get("success"))
        self.assertIn("not found in inventory", diag.get("error", ""))

    def test_diagnose_powered_off_vm(self):
        diag = srv.airlock_diagnose("nhr-workhorse-01")
        self.assertTrue(diag.get("success"))
        self.assertEqual(diag.get("root_cause"), "VM_POWERED_OFF")
        self.assertTrue("airlock_power" in diag.get("suggested_action", "") or "start" in diag.get("suggested_action", ""))

    def test_diagnose_unprovisioned_vm(self):
        tmp_unprov = INVENTORY_DIR / REMOTE_HOST / "test-unprov-diag-vm"
        tmp_unprov.mkdir(parents=True, exist_ok=True)
        try:
            (tmp_unprov / "vm.yaml").write_text(f"vm:\n  name: test-unprov-diag-vm\n  host: {REMOTE_HOST}\n  state: shut off\n", encoding="utf-8")
            diag = srv.airlock_diagnose("test-unprov-diag-vm")
            self.assertTrue(diag.get("success"))
            self.assertIn(diag.get("root_cause"), ("DOMAIN_NOT_FOUND", "DOMAIN_NOT_PROVISIONED"))
            self.assertTrue("deploy_vm" in diag.get("suggested_action", ""))
        finally:
            if tmp_unprov.exists():
                shutil.rmtree(tmp_unprov)

    def test_diagnose_live_running_vm(self):
        diag = srv.airlock_diagnose("dummy-airlock-app-vm")
        self.assertTrue(diag.get("success"))
        self.assertTrue(diag.get("can_rollback"))
        findings_str = " ".join(diag.get("findings", []))
        self.assertIn("running", findings_str)
        self.assertIn("3000", findings_str)


class TestPermutationG_SecurityIsolation(unittest.TestCase):
    """Permutation G: Verifies zero credential leaks and workstation zero-workload invariant."""

    def test_security_audit_dummy_vm(self):
        sec = srv.airlock_audit_security("dummy-airlock-app-vm", app_name="dummy-airlock-app")
        self.assertTrue(sec.get("success"))
        self.assertTrue(sec.get("compliant"))
        self.assertIn(sec.get("verdict"), ("PASS", "SECURE"))
        self.assertEqual(sec.get("violations_count"), 0)

    def test_proxy_socket_bind_localhost_vs_remote(self):
        """Verifies localhost proxy binds to 127.0.0.1 while remote binds to 0.0.0.0."""
        import io
        from contextlib import redirect_stdout
        import importlib.util

        deploy_script = AIRLOCK_ROOT / ".agents" / "skills" / "airlock_deploy" / "scripts" / "deploy_vm.py"
        spec = importlib.util.spec_from_file_location("deploy_vm_test", deploy_script)
        deploy_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deploy_mod)

        params = {
            "VM_NAME": "test-sec-vm",
            "VM_IP": "192.168.122.99",
            "PROXY_EXTERNAL_PORT": "3001",
            "APP_INTERNAL_PORT": "3000",
        }

        # Localhost must bind strictly to 127.0.0.1
        f_local = io.StringIO()
        with redirect_stdout(f_local):
            deploy_mod.setup_hypervisor_proxy(params, "localhost", dry_run=True)
        self.assertIn("ListenStream=127.0.0.1:3001", f_local.getvalue())

        # Remote hypervisor must bind to 0.0.0.0 for LAN/network access
        f_remote = io.StringIO()
        with redirect_stdout(f_remote):
            deploy_mod.setup_hypervisor_proxy(params, "remotehost", dry_run=True)
        self.assertIn("ListenStream=0.0.0.0:3001", f_remote.getvalue())


class TestPermutationH_ClientConnectorCLI(unittest.TestCase):
    """Permutation H: Tests multi-IDE client connector CLI script."""

    def test_connector_list(self):
        proc = subprocess.run(
            ["./connect_airlock.sh", "--list"],
            cwd=str(AIRLOCK_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("AirlockVM MCP Client Registration Status", proc.stdout)

    def test_connector_dry_run_all(self):
        proc = subprocess.run(
            ["./connect_airlock.sh", "--target", str(DUMMY_APP_DIR), "--dry-run", "--all"],
            cwd=str(AIRLOCK_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Completed. Total configs evaluated:", proc.stdout)
        self.assertIn("Antigravity", proc.stdout)
        self.assertIn("VS Code", proc.stdout)
        self.assertIn("Cursor", proc.stdout)

    def test_connector_dry_run_single_platform(self):
        proc = subprocess.run(
            ["./connect_airlock.sh", "--target", str(DUMMY_APP_DIR), "--dry-run", "--cursor"],
            cwd=str(AIRLOCK_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Cursor", proc.stdout)
        self.assertNotIn("JetBrains", proc.stdout)

    def test_connector_abort_on_confirmation_reject(self):
        proc = subprocess.run(
            ["./connect_airlock.sh", "--target", str(DUMMY_APP_DIR)],
            input="n\n",
            cwd=str(AIRLOCK_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Target Location:", proc.stdout)
        self.assertIn("Aborted by user.", proc.stdout)

    def test_connector_with_auto_yes(self):
        proc = subprocess.run(
            ["./connect_airlock.sh", "--target", str(DUMMY_APP_DIR), "-y", "--antigravity"],
            cwd=str(AIRLOCK_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Target Location:", proc.stdout)
        self.assertIn("Completed. Total configs updated:", proc.stdout)

    def test_connector_default_target_directory(self):
        proc = subprocess.run(
            [str(AIRLOCK_ROOT / "connect_airlock.sh"), "--dry-run", "--cursor"],
            cwd=str(DUMMY_APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Target Location:", proc.stdout)
        self.assertIn(str(DUMMY_APP_DIR), proc.stdout)


class TestPermutationI_LiveDeploymentAndProxyReachability(unittest.TestCase):
    """Permutation I: End-to-end live deployment and hypervisor proxy reachability verification."""

    def test_live_proxy_http_reachability(self):
        url = f"http://{REMOTE_HOST}:3002/"
        req = urllib.request.Request(url, headers={"User-Agent": "AirlockVM-Integration-Test/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(data.get("boundHost"), "0.0.0.0")
            self.assertEqual(data.get("boundPort"), 3000)
            self.assertIn("DummyAirlockApp", data.get("message"))
            self.assertTrue(data.get("envLoaded"))
            self.assertEqual(data.get("customConfig"), "AirlockVM-Isolated-VM-Environment")

    def test_live_mcp_health_check(self):
        res = srv.airlock_health_check("dummy-airlock-app-vm")
        self.assertTrue(res.get("healthy"))
        self.assertEqual(res.get("status_code"), 200)
        self.assertIn("3002", res.get("url", ""))

    def test_live_workload_profiler(self):
        res = srv.airlock_profile_workload("dummy-airlock-app-vm", duration_sec=1)
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("status"), "RUNNING")
        self.assertEqual(res.get("cpu", {}).get("status"), "NORMAL")
        proc_cmds = [p.get("command", "") for p in res.get("top_processes", [])]
        self.assertTrue(any("node" in cmd or "npm" in cmd for cmd in proc_cmds))

    def test_live_hypervisor_domain_running(self):
        status = srv.airlock_status("dummy-airlock-app-vm")
        self.assertTrue(status.get("success"))
        self.assertIn("running", status.get("stdout", "").lower())


class TestPermutationJ_HierarchicalInventoryResolution(unittest.TestCase):
    """Permutation J: Tests hierarchical inventory resolution (env var, ~/.airlock, repo)."""

    def test_env_var_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_inv = Path(tmpdir) / "inventory"
            vm_dir = custom_inv / "remotehost" / "custom-env-vm"
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.yaml").write_text("vm:\n  name: custom-env-vm\n  host: remotehost\n")

            import os
            old_env = os.environ.get("AIRLOCK_INVENTORY_DIR")
            try:
                os.environ["AIRLOCK_INVENTORY_DIR"] = str(custom_inv)
                search_dirs = get_inventory_search_dirs()
                self.assertEqual(search_dirs[0], custom_inv)
                path, doc = find_vm_yaml("custom-env-vm")
                self.assertIsNotNone(path)
                self.assertEqual(doc["vm"]["name"], "custom-env-vm")
            finally:
                if old_env is not None:
                    os.environ["AIRLOCK_INVENTORY_DIR"] = old_env
                else:
                    os.environ.pop("AIRLOCK_INVENTORY_DIR", None)

    def test_user_persistent_default(self):
        search_dirs = get_inventory_search_dirs()
        self.assertIn(Path.home() / ".airlock" / "inventory", search_dirs)
        self.assertNotIn(AIRLOCK_ROOT / "inventory", search_dirs)
        path, doc = find_vm_yaml("dummy-airlock-app-vm")
        self.assertIsNotNone(path)
        self.assertEqual(doc["vm"]["name"], "dummy-airlock-app-vm")


if __name__ == "__main__":
    unittest.main()
