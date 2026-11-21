"""DevBox MCP Control Plane Integration Permutations Test Suite.

Tests all permutations across the DevBox MCP Server and sub-engines:
- Permutation A: Core Observability & Inspection
- Permutation B: Stack Detection & Multi-Framework Inference
- Permutation C: Hypervisor Placement, Networking, & Port Allocation
- Permutation D: Dedicated VM Lifecycle (derived via MCP tools for DummyDevBoxApp)
- Permutation E: Shared Attach Topology Lifecycle & Idempotency
- Permutation F: Drift Detection & Diagnostic Intelligence
- Permutation G: Security Isolation & Workstation Zero-Workload Invariant
- Permutation H: Multi-IDE Client Connector CLI (connect_devbox.sh)
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml

import devbox.mcp.server as srv
from devbox.detector import detect_application
from devbox.diagnostics import diagnose_workload_failure
from devbox.drift import audit_vm_drift
from devbox.network import allocate_vm_network
from devbox.placement import inspect_hypervisor, select_optimal_hypervisor
from devbox.ports import allocate_external_port, get_inventory_used_ports
from devbox.preflight import validate_deployment_preflight
from devbox.security import audit_vm_security


DEVBOX_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_DIR = DEVBOX_ROOT / "inventory"
DUMMY_APP_DIR = (DEVBOX_ROOT / ".." / "DummyDevBoxApp").resolve()


class TestPermutationA_ObservabilityAndInspection(unittest.TestCase):
    """Permutation A: Verifies core ping, list, and status reporting."""

    def test_devbox_ping(self):
        result = srv.devbox_ping()
        self.assertIn("DevBox MCP Server is online", result)
        self.assertIn("Python", result)

    def test_devbox_list(self):
        result = srv.devbox_list()
        self.assertTrue(result.get("success"))
        self.assertGreaterEqual(result.get("total_vms", 0), 5)
        vm_names = [v["vm_name"] for v in result.get("vms", [])]
        self.assertIn("dummy-devbox-app-vm", vm_names)
        self.assertIn("devbox-vm", vm_names)
        self.assertIn("appsurfer-vm", vm_names)

    def test_devbox_status_unprovisioned(self):
        result = srv.devbox_status("dummy-devbox-app-vm")
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("vm_name"), "dummy-devbox-app-vm")
        self.assertIn("dummy-devbox-app-vm", result.get("stdout", ""))


class TestPermutationB_ApplicationDetection(unittest.TestCase):
    """Permutation B: Tests stack detection across Node.js, Python, and Docker."""

    def test_detect_dummy_devbox_app_nodejs(self):
        self.assertTrue(DUMMY_APP_DIR.exists(), f"DummyDevBoxApp not found at {DUMMY_APP_DIR}")
        info = srv.devbox_detect_app(str(DUMMY_APP_DIR))
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
        info = detect_application("/tmp/nonexistent-path-for-devbox-test-999")
        self.assertFalse(info.get("success"))
        self.assertIn("does not exist", info.get("error", ""))


class TestPermutationC_PlacementAndNetworking(unittest.TestCase):
    """Permutation C: Tests placement optimizer, network allocation, and port management."""

    def test_hypervisor_inspection(self):
        info = inspect_hypervisor("barkley")
        self.assertTrue(info.get("success"))
        self.assertEqual(info.get("vmhost"), "barkley")
        self.assertGreater(info.get("available_ram_mib", 0), 1024)

    def test_host_placement_optimizer(self):
        result = srv.devbox_select_host(required_vcpu=2, required_ram_mib=2048, required_disk_gb=20)
        self.assertTrue(result.get("success"))
        self.assertIn("selected_host", result)
        self.assertIn("selected_hypervisor", result)
        self.assertEqual(result["selected_host"], "barkley")
        self.assertEqual(result["selected_hypervisor"], "barkley")

    def test_network_allocation_unique(self):
        alloc = srv.devbox_alloc_network(vmhost="barkley")
        self.assertTrue(alloc.get("success"))
        self.assertEqual(alloc.get("vmhost"), "barkley")
        self.assertTrue(alloc.get("allocated_ip", "").startswith("192.168.122."))
        self.assertTrue(alloc.get("allocated_mac", "").startswith("52:54:00:"))

    def test_port_allocation_auto(self):
        port_info = srv.devbox_find_port(3000)
        self.assertTrue(port_info.get("success"))
        self.assertGreaterEqual(port_info.get("allocated_external_port", 0), 3001)

    def test_port_allocation_collision_avoidance(self):
        port, reason = allocate_external_port(vmhost="barkley", internal_port=3000, preferred_port=3001)
        # 3001 is already registered to devbox-vm; ensure allocation shifts to next free
        self.assertNotEqual(port, 3001)
        self.assertGreater(port, 3001)


class TestPermutationD_DedicatedVMLifecycle(unittest.TestCase):
    """Permutation D: Validates dedicated VM lifecycle derived for DummyDevBoxApp."""

    def test_dummy_devbox_app_vm_ssot_exists(self):
        yaml_path = INVENTORY_DIR / "barkley" / "dummy-devbox-app-vm" / "vm.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}

        vm_data = doc.get("vm", {})
        self.assertEqual(vm_data.get("name"), "dummy-devbox-app-vm")
        self.assertEqual(vm_data.get("host"), "barkley")
        bp = vm_data.get("blueprint", {})
        self.assertEqual(bp.get("project_name"), "dummy-devbox-app")
        self.assertEqual(bp.get("app_internal_port"), 3000)
        self.assertEqual(bp.get("proxy_external_port"), 3002)

    def test_dummy_devbox_app_vm_runbook_clean(self):
        runbook_path = INVENTORY_DIR / "barkley" / "dummy-devbox-app-vm" / "VM.md"
        self.assertTrue(runbook_path.exists())
        content = runbook_path.read_text(encoding="utf-8")
        self.assertNotIn("{{", content)
        self.assertNotIn("}}", content)
        self.assertIn("dummy-devbox-app-vm", content)
        self.assertIn("192.168.122.231", content)
        self.assertIn("3002", content)

    def test_preflight_audit_gates(self):
        pf = srv.devbox_preflight("dummy-devbox-app-vm")
        self.assertTrue(pf.get("success"))
        self.assertEqual(pf.get("verdict"), "READY_TO_DEPLOY")
        self.assertEqual(pf.get("confidence_score"), 1.0)
        self.assertIn("8/8", str(pf.get("checks_passed")))

    def test_deploy_vm_dry_run(self):
        res = srv.devbox_deploy_vm("dummy-devbox-app-vm", dry_run=True)
        self.assertTrue(res.get("success"))
        self.assertIn("pre-requisite gates passed", res.get("stdout", "").lower())

    def test_deploy_app_offline_filtering(self):
        res = srv.devbox_deploy_app("dummy-devbox-app-vm", offline=True)
        self.assertTrue(res.get("success"))
        self.assertIn("server.js", res.get("stdout", ""))
        self.assertIn("package.json", res.get("stdout", ""))
        self.assertNotIn(".git/", res.get("stdout", ""))


class TestPermutationE_SharedAttachTopology(unittest.TestCase):
    """Permutation E: Tests shared attach topology, idempotency, and overrides."""

    def setUp(self):
        self.test_vm = "test-shared-staging-vm"
        self.test_dir = INVENTORY_DIR / "barkley" / self.test_vm
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.test_yaml = self.test_dir / "vm.yaml"
        sample_doc = {
            "vm": {
                "name": self.test_vm,
                "host": "barkley",
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
            [str(DEVBOX_ROOT / ".venv/bin/python"), str(DEVBOX_ROOT / "inventory/generate_deploy_script.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_attach_app_auto_inference(self):
        res = srv.devbox_attach_app(
            vm_name=self.test_vm,
            local_path=str(DUMMY_APP_DIR),
        )
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("app_name"), "dummy-devbox-app")
        self.assertEqual(res.get("internal_port"), 3000)
        self.assertEqual(res.get("post_deploy"), "npm start")

        with open(self.test_yaml, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        repos = doc.get("vm", {}).get("blueprint", {}).get("repositories", [])
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["name"], "dummy-devbox-app")

    def test_attach_app_idempotent_update(self):
        # Attach first time
        srv.devbox_attach_app(
            vm_name=self.test_vm,
            local_path=str(DUMMY_APP_DIR),
            app_name="dummy-devbox-app",
        )
        # Attach second time with override
        res2 = srv.devbox_attach_app(
            vm_name=self.test_vm,
            local_path=str(DUMMY_APP_DIR),
            app_name="dummy-devbox-app",
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

    def test_audit_drift_unprovisioned(self):
        res = srv.devbox_audit_drift("dummy-devbox-app-vm")
        self.assertTrue(res.get("success"))
        self.assertFalse(res.get("domain_provisioned"))
        self.assertGreaterEqual(res.get("total_drift_items", 0), 1)
        items = res.get("drift_items", [])
        categories = [item.get("category") for item in items]
        self.assertIn("domain_presence", categories)

    def test_diagnose_nonexistent_vm(self):
        diag = srv.devbox_diagnose("nonexistent-test-vm-999")
        self.assertFalse(diag.get("success"))
        self.assertIn("not found in inventory", diag.get("error", ""))

    def test_diagnose_unprovisioned_vm(self):
        diag = srv.devbox_diagnose("dummy-devbox-app-vm")
        self.assertTrue(diag.get("success"))
        self.assertIn(diag.get("root_cause"), ("DOMAIN_NOT_FOUND", "DOMAIN_NOT_PROVISIONED"))
        self.assertTrue("deploy_vm" in diag.get("suggested_action", ""))


class TestPermutationG_SecurityIsolation(unittest.TestCase):
    """Permutation G: Verifies zero credential leaks and workstation zero-workload invariant."""

    def test_security_audit_dummy_vm(self):
        sec = srv.devbox_audit_security("dummy-devbox-app-vm", app_name="dummy-devbox-app")
        self.assertTrue(sec.get("success"))
        self.assertTrue(sec.get("compliant"))
        self.assertIn(sec.get("verdict"), ("PASS", "SECURE"))
        self.assertEqual(sec.get("violations_count"), 0)


class TestPermutationH_ClientConnectorCLI(unittest.TestCase):
    """Permutation H: Tests multi-IDE client connector CLI script."""

    def test_connector_list(self):
        proc = subprocess.run(
            ["./connect_devbox.sh", "--list"],
            cwd=str(DEVBOX_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("DevBox MCP Client Registration Status", proc.stdout)

    def test_connector_dry_run_all(self):
        proc = subprocess.run(
            ["./connect_devbox.sh", "--target", str(DUMMY_APP_DIR), "--dry-run", "--all"],
            cwd=str(DEVBOX_ROOT),
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
            ["./connect_devbox.sh", "--target", str(DUMMY_APP_DIR), "--dry-run", "--cursor"],
            cwd=str(DEVBOX_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Cursor", proc.stdout)
        self.assertNotIn("JetBrains", proc.stdout)


if __name__ == "__main__":
    unittest.main()
