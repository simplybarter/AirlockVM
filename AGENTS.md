# Agent Guidelines (AGENTS.md)

Guidelines, operational standards, and architectural conventions for AI agents operating within the **AirlockVM** repository.

---

## 1. Universal Portability & Zero Machine Lock-In Mandates

AirlockVM is an open, universally portable control plane designed to run cleanly across any developer machine, CI/CD runner, or virtualization cluster. Agents must strictly adhere to the following four non-negotiable portability invariants:

1. **Relative Paths Invariant**:
   * All documentation files (`docs/*.md`, `README.md`, `USECASE.md`, etc.), scripts, agent skill configurations, test fixtures, and tool outputs must exclusively use **relative paths** (e.g., `docs/ARCHITECTURE.md`, `inventory/remotehost/vmhost.yaml`, `tests/test_integration_permutations.py`, `../<project_name>`).
   * **Never** hardcode absolute local paths (e.g., `/home/...`, `/Users/...`) or host-specific URI schemes (`file:///home/...`).
2. **Zero Private Hostnames Invariant**:
   * **Never** introduce or commit private local machine hostnames, internal network aliases, or custom workstation names (e.g., `workstation-01`, `corp-server-02`, `dev-rig`) into code, scripts, agent skills, tests, or documentation.
   * Always designate development machines as `localhost` and remote hypervisors as `remotehost` (or dynamically discover them from inventory).
3. **Dynamic User Resolution Invariant**:
   * **Never** hardcode local developer usernames (e.g., `alice`, `bob`, `developer`, `admin`) in script templates, default parameters, or inventory generators.
   * User accounts must always be resolved dynamically at runtime using `getpass.getuser()` or environment variables (`os.environ.get("USER", "developer")`).
4. **Zero Upstream Inventory Pollution Invariant**:
   * Machine inventory is persistently stored at `~/.airlock/inventory/` (or overridden via `$AIRLOCK_INVENTORY_DIR`). It represents local machine execution state and is decoupled from the git repository.
   * Agents must never track, stage, or commit generated host profiles, guest VM definitions (`vm.yaml`), runbooks (`VM.md`), or private SSH keys into the git repository.

---

## 2. Single Source of Truth (SSOT) Architecture

To prevent configuration drift, ambiguity, and redundancy, AirlockVM establishes authoritative Single Source of Truth (SSOT) boundaries across documentation and machine inventories:

### 2.1 Documentation SSOT Hierarchy

| Document | Authoritative Domain (SSOT) |
| :--- | :--- |
| **`QUICKSTART.md`** | **Newcomer & Agent Quickstart:** 5-minute onboarding walkthrough, installation, multi-IDE connector, and basic workflow. |
| **`docs/ARCHITECTURE.md`** | **System Architecture & Isolation Model:** Two-tier hypervisor topology, resource budgeting (vCPU/RAM), `qcow2` storage, socket proxying, and credential zero-leak boundaries. |
| **`docs/SECURITY.md`** | **Security & Proxy Isolation Model:** Layer 4 socket proxy mechanics, authentication boundaries, credential zero-leak invariants, and hypervisor hardening. |
| **`docs/MCP_SERVER.md`** | **MCP Protocol & Tooling Specification:** Detailed JSON schemas, tools, subcommands, arguments, and return types for the `airlock-mcp` Model Context Protocol control plane. |
| **`docs/INTEGRATION_TESTING.md`** | **End-to-End Verification Matrix:** 40-permutation verification lifecycle, multi-IDE connector validation, test fixtures (`DummyAirlockApp`), and automated rollback testing. |
| **`USECASE.md`** | **Operational Problem Space & Solutions:** Motivation, developer pain points, container escape risks, and real-world deployment patterns. |
| **`README.md`** | **Project Overview & Quickstart:** Architecture highlights, quick installation, CLI usage, and quick reference guide. |

* **Cross-Referencing Rule**: When documentation needs information outside its designated domain, it must link by reference to the authoritative document rather than redefining the concept.

### 2.2 Inventory SSOT Architecture & Two-Tier Discovery

AirlockVM resolves inventory using a two-tier discovery mechanism:
1. **Explicit Environment Override**: `$AIRLOCK_INVENTORY_DIR` (highest precedence; ideal for isolated CI / test runs or custom team locations).
2. **User Persistent Location**: `~/.airlock/inventory/` (standard user persistent location; decoupled from git clone paths and immune to `git clean`).

Within the resolved inventory hierarchy (`<inventory_root>/{vmhost}/{vm}/`):
* **`vmhost.yaml`**: The SSOT for hypervisor capabilities (CPU threads, total RAM, storage pools, bridge networking, libvirt/qemu versions).
* **`vm.yaml`**: The canonical declarative SSOT for an individual guest virtual machine domain (state, hardware resources, static IP/MAC, proxy ports, and attached repository blueprints).
* **`VM.md`**: The parameterized operational runbook rendered directly from the `vm.yaml` SSOT.
* **`deploy_app_to_airlock.sh`**: The root multi-VM dispatcher generated deterministically from all registered inventory records.

---

## 3. Autonomous Skills Suite (`.agents/skills/`)

AirlockVM provides specialized agent skills in `.agents/skills/` designed to be invoked by autonomous coding agents:

| Skill | Purpose & Scripts |
| :--- | :--- |
| **`airlock_inventory`** | Discovers, inspects, and registers hypervisors (`vmhost.yaml`) and guest virtual machines into the inventory structure (`add_vmhost.py`, `inventory_manager.py`, `verify_config.py`). |
| **`airlock_configure`** | Generates and validates parameterized operational runbooks (`VM.md`) from templates based on hypervisor hardware probes (`configure_vm.py`, `probe_hypervisor.py`). |
| **`airlock_deploy`** | Enforces 8-gate pre-flight checks, generates cloud-init user data, provisions execution VMs via `virt-install`, and configures hypervisor systemd proxy sockets (`deploy_vm.py`). |
| **`airlock_rsync`** | Evaluates client application manifests, computes differential file exclusion rules, and generates optimized synchronization scripts (`generate_sync.py`). |

---

## 4. MCP Control Plane Tooling (`airlock-mcp`)

The Model Context Protocol control plane exposes a complete tool suite for coding agents across six functional categories:

1. **Observability & Inspection**:
   * `airlock_ping`: Verifies control plane readiness and runtime version.
   * `airlock_list`: Enumerates all hypervisors and VMs in `inventory/`.
   * `airlock_list_hypervisors(live_probe)`: Lists registered hypervisors with CPU, RAM, storage pools, networks, `connection.ssh_key_path`, rootless capability, and VM counts.
   * `airlock_status(vm_name)`: Queries live libvirt domain status and hardware states.
   * `airlock_health_check(vm_name, app_name, path)`: Probes live HTTP proxy routes with retries and captures guest logs on failure.

2. **Application Stack Detection**:
   * `airlock_detect_app(local_path)`: Analyzes project manifests (`package.json`, `pyproject.toml`, `requirements.txt`, `Dockerfile`, `.env`) to auto-infer frameworks, internal ports, and startup commands.

3. **Hypervisor Placement & Networking**:
   * `airlock_select_host(required_vcpu, required_ram_mib, required_disk_gb)`: Audits registered hypervisors and selects the optimal host based on CPU, RAM, and disk headroom.
   * `airlock_alloc_network(vmhost, network_name)`: Automatically allocates the next clean static IP (skipping live DHCP leases) and collision-free MAC on `virbr0`.
   * `airlock_find_port(vm_name, internal_port, preferred_port, vmhost)`: Allocates collision-free host proxy ports.

4. **Lifecycle Topologies & Power**:
   * `airlock_create_vm(...)`: Provisions a dedicated virtual machine domain (`vm.yaml`, `VM.md`, deployment scripts).
   * `airlock_update_vm(...)`: Updates VM resources (`vcpu`, `memory_mib`, `disk_size_gib`), ports, or `post_deploy` in `vm.yaml`, re-rendering `VM.md` and sync scripts.
   * `airlock_attach_app(...)`: Attaches a repository to an existing shared worker VM with idempotent updates.
   * `airlock_power(vm_name, action)`: Issues power state transitions (`start`, `stop`, `reboot`, `destroy`).
   * `airlock_purge_vm(vm_name, dry_run, purge_ssh_keys)`: Permanently removes a VM domain, disk/snapshot images, DHCP reservation, proxies, SSH config block, and inventory record.

5. **Autonomous Intelligence Engines**:
   * `airlock_preflight(vm_name)`: Executes 8 pre-requisite safety gates (supporting both rootless `libvirt`/`kvm` and `sudo` hypervisors) before provisioning (verdict: `READY_TO_DEPLOY` or `BLOCKED`).
   * `airlock_deploy_vm(vm_name, dry_run)`: Provisions KVM domains, disk pools, cloud-init images, and host proxies (system or rootless user units).
   * `airlock_deploy_app(vm_name, offline)`: Rsyncs application code and launches workloads inside the VM.
   * `airlock_exec(vm_name, command, workdir, timeout)`: Executes build, test, or diagnostic shell commands inside the isolated guest VM.
   * `airlock_bake_image(vmhost, force, dry_run)`: Pre-bakes a golden Ubuntu 24.04 `qcow2` base image on the hypervisor for fast-boot VM cloning.
   * `airlock_seed_image(target_host, source_host, force, dry_run)`: Seeds a target hypervisor with an already-established golden `qcow2` base image streamed from an existing registered `vmhost` with SHA-256 verification.
   * `airlock_add_vmhost(host, user, port, key, seed_image, seed_from, force_seed, dry_run)`: Discovers and registers a hypervisor in inventory and optionally seeds it with the golden image from an existing `vmhost`.
   * `airlock_audit_drift(vm_name)`: Compares live hypervisor state against the `vm.yaml` SSOT to identify domain, network, or proxy drift.
   * `airlock_reconcile(vm_name, strategy)`: Reconciles detected drift between `vm.yaml` SSOT and the live hypervisor.
   * `airlock_diagnose(vm_name)`: Performs root-cause failure analysis on stalled workloads and recommends rollbacks.
   * `airlock_audit_security(vm_name, app_name)`: Audits for credential leaks (`.env` vs `.env.local`), sensitive key leakage, and host socket leakage.
   * `airlock_profile_workload(vm_name, duration_sec)`: Profiles guest CPU, RAM, and top processes.
   * `airlock_checkpoint(vm_name, action, snapshot_name)`: Creates or reverts pre-deployment safety checkpoints.
   * `airlock_snapshot(vm_name, action, snapshot_name, force, dry_run)`: Manages external disk snapshots (`create`, `revert`, `list`, `delete`, `flatten`).

---

## 5. Multi-IDE Client Connection Architecture

AirlockVM includes a unified client connector script (`connect_airlock.sh`) to onboard external repositories into the control plane across all major IDE agent environments:

```bash
# Onboard a project across all supported IDEs:
./connect_airlock.sh --target ../DummyAirlockApp --all

# Connect specific platforms:
./connect_airlock.sh --target ../DummyAirlockApp --antigravity
./connect_airlock.sh --target ../DummyAirlockApp --gemini
./connect_airlock.sh --target ../DummyAirlockApp --cursor
./connect_airlock.sh --target ../DummyAirlockApp --vscode
./connect_airlock.sh --target ../DummyAirlockApp --claude
./connect_airlock.sh --target ../DummyAirlockApp --windsurf
./connect_airlock.sh --target ../DummyAirlockApp --jetbrains

# Inspect registration status:
./connect_airlock.sh --list
```

* **Idempotency Rule**: Connector scripts must perform additive and non-destructive JSON updates, preserving other configured tools and servers.

---

## 6. Code & Testing Standards

* **Virtual Environment**: All development, testing, and tool execution must use the dedicated Python virtual environment (`>=3.11`) in `.venv`:
  ```bash
  .venv/bin/python -m unittest tests/test_integration_permutations.py
  ```
* **Deterministic Verification**: Any modification to the control plane, skills, or CLI must pass the integration permutation suite (`tests/test_integration_permutations.py`) and deterministic unit tests (`tests/test_unit_mitigations.py`).
* **Zero Host Pollution**: Temporary test files must be cleaned up in test fixture `tearDown()` blocks.

---

## 7. Version Control & Commit Standards

Follow the **Conventional Commits v1.0.0** standard for all commits:
`<type>(<scope>): <subject>`

* **Allowed Types**: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`.
* **Standard Scopes**:
  * `(cli)`: Core command-line interface (`src/airlock/cli.py`).
  * `(mcp)`: Model Context Protocol server tools (`src/airlock/mcp/server.py`).
  * `(detector)`: Application stack and framework detection (`src/airlock/detector.py`).
  * `(preflight)`: Deployment pre-flight safety gates (`src/airlock/preflight.py`).
  * `(drift)`: Configuration drift detection engine (`src/airlock/drift.py`).
  * `(diagnostics)`: Root-cause analysis and failure diagnostics (`src/airlock/diagnostics.py`).
  * `(security)`: Security isolation and credential leak audit (`src/airlock/security.py`).
  * `(profiler)`: Workload and resource profiler (`src/airlock/profiler.py`).
  * `(skills)`: Agent skills in `.agents/skills/`.
  * `(inventory)`: Hypervisor and VM inventory templates and scripts.
  * `(docs)`: Documentation in `docs/`, `README.md`, `USECASE.md`, `AGENTS.md`.
  * `(tests)`: Integration and permutation test suites.
* **Commit Invariant**: Always inspect `git diff --cached` before committing to ensure no secrets, credentials, or generated inventory files are staged.
