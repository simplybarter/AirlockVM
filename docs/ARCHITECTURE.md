# AirlockVM Architecture & Security Model

This document outlines the architectural principles, isolation boundaries, and security model enforced by AirlockVM.

---

## 1. Two-Tier Host Topology

AirlockVM enforces a strict separation of concerns between code authoring and workload execution:

### Tier 1: Development Workstation
* **Responsibility**: Software development, source code authoring, version control (`git`), documentation, and IDE tooling.
* **Invariant**: Zero execution of heavy or untrusted runtime workloads. Automated crawler loops, headless browser automation, untrusted test suites, and container fleets are never run directly on the development machine.

### Tier 2: Dedicated Execution Hypervisor
* **Responsibility**: Dedicated physical server or isolated local hypervisor executing hardware-accelerated Linux KVM (`/dev/kvm`).
* **Function**: Hosts the guest virtual machines, provides bridged network traffic, and isolates guest execution environments from local workstations.

---

## 2. Resource Isolation & Proportional Sizing

To prevent guest execution environments from overwhelming hypervisor resources:

* **vCPU Capacity**: The provisioning engine dynamically measures available host CPU threads and allocates balanced core counts (typically 50% of host capacity), leaving remaining cores reserved for the host OS.
* **Memory Allocation**: Guest RAM is budgeted proportionally (~25% of total host RAM) across standardized memory tiers (2 GB, 4 GB, 8 GB, 16 GB, 32 GB), preventing out-of-memory kernel panics on shared physical hosts.
* **Storage Thin Provisioning**: Virtual machine disk images use the `qcow2` format, enabling dynamic copy-on-write allocation and rapid baseline snapshots (`virsh snapshot-create-as`).

---

## 3. Network Architecture & Service Access

```text
Local Workstation                  Hypervisor Host               Dedicated Guest VM
(Browser / Client)                 (remote-host / localhost)         (airlock-vm)
       |                                  |                             |
       |  HTTP :3001                      |                             |
       +--------------------------------->|  systemd-socket-proxyd      |
       |                                  |  Forward :3001 -> :3000     |
       |                                  +---------------------------->|  App Server (:3000)
       |                                  |                             |
```

* **Internal Guest Network**: The guest VM resides on an isolated libvirt virtual network bridge (`virbr0`) with private static DHCP leasing.
* **Zero Workstation Process Forwarding**: Web applications running inside the guest VM (e.g. port `3000`) are made accessible to physical LAN devices via a persistent systemd socket forwarder (`systemd-socket-proxyd`) running on the hypervisor host on port `3001`. This eliminates the need for developers to run local background SSH tunnels on their laptops.

---

## 4. Security & Safety Invariants

1. **Ephemeral Execution**: Staging credentials, test API tokens, and headless browser sessions reside exclusively inside the execution memory of the virtual machine.
2. **Snapshot Baseline & Instant Rollback**: Developers can create clean disk baselines before running experimental container builds or crawler jobs:
   ```bash
   virsh snapshot-create-as --domain <vm-name> --name snapshot-base-clean --disk-only
   ```
   If testing produces clutter or modified dependencies, the environment can be restored to its pristine baseline state within seconds.

---

## 5. Agent Skills Lifecycle Architecture

AirlockVM provides four complementary, composable skills for autonomous AI agents and command-line automation. The workflow operates as an automated lifecycle:

```mermaid
flowchart TD
    Inv["1. airlock_inventory<br/>Discover & record hypervisors & VMs<br/>(inventory/{vmhost}/)"] --> Conf["2. airlock_configure<br/>Parameterize VM runbook from template<br/>(VM.md.template -> VM.md)"]
    Conf --> Deploy["3. airlock_deploy<br/>Enforce pre-flight gates, provision domain,<br/>cloud-init seed & socket proxy"]
    Deploy --> Rsync["4. airlock_rsync<br/>Evaluate project manifest & exclude rules,<br/>deploy app to VM (deploy_app_to_vm.sh)"]
```

### Stage 1: Discovery & Documentation (`airlock_inventory`)
* **Role**: Inspects physical or local hypervisors via SSH or local libvirt, documenting hardware specifications, libvirt virtual networks, storage pools, and existing virtual machine domains.
* **Artifacts**: Maintains structured YAML records (`inventory/{vmhost}/vmhost.yaml` and `inventory/{vmhost}/{vm}/vm.yaml`).

### Stage 2: Configuration & Sizing (`airlock_configure`)
* **Role**: Parameterizes the single source-of-truth template (`VM.md.template`) to produce an operational runbook (`VM.md`).
* **Heuristics**: Calculates proportional vCPU and RAM allocations, ensures collision-free static IP and MAC assignments, and maps proxy forwarding ports.

### Stage 3: Provisioning & Lifecycle Management (`airlock_deploy`)
* **Role**: Enforces mandatory verification gates before hypervisor execution, generates cloud-init NoCloud ISO seeds, provisions KVM domains via `virt-install`, configures static DHCP reservations, and establishes persistent systemd socket proxying.
* **Lifecycle**: Provides full lifecycle commands (`--start`, `--stop`, `--status`, `--snapshot`, `--rollback`, `--validate-proxy`).

### Stage 4: Workload Deployment (`airlock_rsync`)
* **Role**: Evaluates project manifests (`package.json`, `requirements.txt`, etc.) and dynamically builds an optimized application deployment script (`deploy_app_to_vm.sh`).
* **Boundary**: Strictly excludes local developer caches, IDE state, documentation, agent configs, and version control directories, ensuring only necessary runtime application code reaches the execution VM.

---

## 6. Standalone Control Plane & MCP Server

To decouple infrastructure logic from application repositories, AirlockVM operates as a standalone **Model Context Protocol (MCP)** server (`airlock-mcp`):
* AI coding agents access AirlockVM tools (`airlock_create_vm`, `airlock_attach_app`, `airlock_preflight`, `airlock_deploy_vm`, `airlock_deploy_app`) without installing virtualization scripts in client projects.
* **Dual Topology Model**:
  - **Option A (Dedicated Execution VM)**: Isolated KVM domain with dedicated static IP, MAC, operational runbook (`VM.md`), and independent proxy sockets (`airlock_create_vm`).
  - **Option B (Shared Staging Worker)**: Multiple decoupled application repositories mapped onto a shared worker VM with isolated remote code directories and independent proxy ports (`airlock_attach_app`).
* **Autonomous Intelligence Engines**:
  - **Deployability Pre-Flight** (`preflight.py`): Non-destructive pre-flight verification confirming SSH, `/dev/kvm`, `libvirtd`, IP silence, port clearance, and RAM safety headroom with confidence scoring.
  - **Configuration Drift & Reconciliation** (`drift.py`): Audits live hypervisor domain and systemd proxy units against inventory SSOT, detecting discrepancies in vCPUs, RAM, and proxy units.
  - **Workload Profiler** (`profiler.py`): Real-time guest CPU, RAM, and swap monitoring providing rightsizing guidance to reclaim hypervisor resources.
  - **Crash Diagnostics** (`diagnostics.py`): Multi-layer failure diagnosis across systemd journal logs, TCP port listeners, and proxy sockets with automated recovery guidance.
  - **Security & Workstation Invariant** (`security.py`): Automated secret leak scanning and verification of zero runtime workloads on the development workstation.
* See [MCP Server Documentation](MCP_SERVER.md) for full architecture and tool definitions, and [Integration Testing Guide](INTEGRATION_TESTING.md) for verification.
