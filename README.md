# AirlockVM

[![CI](https://github.com/simplybarter/AirlockVM/actions/workflows/ci.yml/badge.svg)](https://github.com/simplybarter/AirlockVM/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Linux KVM](https://img.shields.io/badge/Virtualization-Linux%20KVM-orange.svg?logo=linux&logoColor=white)](https://www.linux-kvm.org/)
[![libvirt](https://img.shields.io/badge/Hypervisor-libvirt%20%2F%20QEMU-red.svg?logo=qemu&logoColor=white)](https://libvirt.org/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

> **Dedicated Execution Virtual Machine Control Plane, Runbook Sizing, and MCP Server**

[📖 Zero-Workload Use Case](USECASE.md) • [🏗 Architecture](docs/ARCHITECTURE.md) • [🛡️ Security](docs/SECURITY.md) • [🔌 MCP Server](docs/MCP_SERVER.md) • [🧪 Integration Testing](docs/INTEGRATION_TESTING.md) • [⚡ Quickstart](#getting-started) • [🤝 Contributing](CONTRIBUTING.md)

AirlockVM provides automated tooling and a parameterized specification framework to configure, size, and deploy dedicated Linux KVM execution virtual machines for development teams and AI agents.

By strictly separating software engineering workstations from runtime execution environments, AirlockVM enforces an isolation boundary: all builds, crawler loops, browser automation, and untrusted code evaluation execute inside dedicated guest VMs hosted on local or remote hypervisors.

> 🚀 **New to AirlockVM? Check out the [5-Minute Quickstart Guide](QUICKSTART.md)!**

---

## Key Features

* **Universal & Zero-Assumption**: Operates seamlessly across local KVM installations or remote physical hypervisors via SSH, without hardcoded machine names or private IP addresses.
* **Dual Topology Support**: Enables both **Option A (Dedicated Execution VM)** via `airlock_create_vm` and **Option B (Shared Worker Attach)** via `airlock_attach_app` based on project isolation needs.
* **Autonomous Intelligence Engines**: Built-in Python engines for deployability pre-flight gates (`preflight.py`), live configuration drift & reconciliation (`drift.py`), multi-layer crash diagnostics (`diagnostics.py`), guest workload profiling & rightsizing (`profiler.py`), and workstation zero-workload invariant verification (`security.py`).
* **Smart Pre-Flight Capability Checks**: Automatically inspects target hypervisors for `/dev/kvm` hardware acceleration, CPU virtualization extensions (`vmx`/`svm`), active `libvirtd` daemons, and storage space before provisioning.
* **Proportional Resource Allocation**: Automatically discovers physical host CPU threads and total RAM, dynamically sizing guest vCPUs (e.g. 50% capacity) and memory (e.g. 25% capacity) to prevent host starvation.
* **Conflict-Free Network Discovery**: Queries libvirt networks (such as `default` / `virbr0`), inspects active DHCP leases, allocates the next unassigned static IP, and generates unique collision-free MAC addresses (`52:54:00:xx:xx:xx`).
* **Automated Project-to-VM Naming**: Derives the project name from the repository root folder, auto-generating domain names (`<project>-vm`), service units, and user accounts.
* **Automated Virtual Machine Deployment (`airlock_deploy`)**: Launches the execution VM using `vm.yaml` as the Single Source of Truth (SSOT)—discovered from persistent user inventory (`~/.airlock/inventory/`) or configured via `$AIRLOCK_INVENTORY_DIR`—with strict pre-requisite verification gates, automated cloud-init image seeding, static DHCP reservations, and systemd socket forwarding.
* **Intelligent Code Synchronization (`airlock_rsync`)**: Evaluates project manifests (Python, Node.js, Rust, Go) and generates an optimized `deploy_app_to_airlock.sh` script to deploy ONLY application code to the VM while ignoring local caches and agent files.
* **Multi-IDE Client Connector (`connect_airlock.sh`)**: One-command client onboarding across VS Code, Cursor, Claude Code, Antigravity, Windsurf, and JetBrains with zero manual JSON edits.
* **Antigravity AI Agent Skills**: Includes ready-to-use `.agents/skills/airlock_inventory`, `.agents/skills/airlock_configure`, `.agents/skills/airlock_deploy`, and `.agents/skills/airlock_rsync` skills for autonomous agent-driven hypervisor discovery, VM customization, provisioning, and workspace sync.

---

## System Architecture

```text
+------------------------------------+          SSH ProxyJump
|      Development Workstation       |-------------------------------+
| * Code Authoring & Git Commits     |                               |
| * Project Documentation & IDE      |                               |
| * NO RUNTIME WORKLOADS             |                               |
+------------------------------------+                               |
                                                                     v
+-----------------------------------------------------------------------------------------+
| Physical Hypervisor Host (Localhost or Remote Server)                                   |
| Linux KVM / QEMU / libvirt                                                              |
|                                                                                         |
|   +---------------------------------------------------------------------------------+   |
|   | Dedicated Guest VM (<project>-vm)                                               |   |
|   | Proportional vCPUs | Proportional RAM | 50 GiB Disk | Static DHCP               |   |
|   |                                                                                 |   |
|   |   [ Isolated Workhorse Runtime & Container Fleets ]                             |   |
|   +---------------------------------------------------------------------------------+   |
+-----------------------------------------------------------------------------------------+
```

---

## Directory Structure

```text
.
├── README.md                                # Project documentation
├── USECASE.md                               # Zero-workload local workstation use case guide
├── CONTRIBUTING.md                          # Contribution guidelines
├── LICENSE                                  # MIT License
├── pyproject.toml                           # Modern PEP 621 packaging & CLI entry points
├── requirements.txt                         # Package dependencies (pyyaml, mcp)
├── connect_airlock.sh                        # Multi-IDE onboarding & MCP registration script
├── deploy_app_to_airlock.sh                      # Multi-VM application deployment dispatcher
├── src/airlock/                             # AirlockVM Python package & Intelligence Engines
│   ├── __init__.py                          # Package initialization
│   ├── cli.py                               # Unified 'airlock' CLI entrypoint
│   ├── detector.py                          # Stack & port inference engine
│   ├── diagnostics.py                       # Multi-layer failure diagnosis & recovery
│   ├── drift.py                             # Configuration drift audit & reconciliation
│   ├── network.py                           # Conflict-free IP & MAC allocation engine
│   ├── placement.py                         # Multi-hypervisor host placement optimizer
│   ├── ports.py                             # Intelligent external proxy port allocator
│   ├── preflight.py                         # Live non-destructive deployability audit
│   ├── profiler.py                          # Live guest RAM/swap/CPU workload profiler
│   ├── security.py                          # Workstation zero-workload & secret leak audit
│   └── mcp/                                 # Model Context Protocol service
│       └── server.py                        # AirlockVM FastMCP Server (26 declarative tools)
├── tests/                                   # Integration & permutation test suites
│   └── test_integration_permutations.py     # 26-permutation end-to-end test suite
├── docs/                                    # Architecture, security & MCP documentation
│   ├── ARCHITECTURE.md                      # Security model and isolation topology
│   ├── MCP_SERVER.md                        # Model Context Protocol server reference
│   └── INTEGRATION_TESTING.md               # End-to-end integration testing guide & matrix
├── .agents/skills/                          # Core AirlockVM automation engines
│   ├── airlock_inventory/                    # Hypervisor discovery and inventory management
│   ├── airlock_configure/                    # Parameterized VM sizing and runbook generation
│   ├── airlock_deploy/                       # Declarative VM provisioning & socket proxying
│   └── airlock_rsync/                        # Differential application synchronization
└── .gitignore
```

---

## Standalone Control Plane & MCP Integration

AirlockVM operates as a standalone infrastructure control plane. Individual application projects (e.g. `AppSurfer`, `NoHumanRequired`) do **not** need to install or maintain virtualization scripts. Instead, AI coding agents and development environments connect to AirlockVM via the **Model Context Protocol (MCP)**:

```bash
# Connect a specific client application workspace:
./connect_airlock.sh --target /path/to/my-app

# Connect globally for the current user profile (~/.gemini/config/mcp_config.json):
./connect_airlock.sh --global
```

For manual MCP integration or agent harnesses, configure `.gemini/mcp_config.json`:

```json
{
  "mcpServers": {
    "airlock": {
      "command": "/path/to/AirlockVM/.venv/bin/airlock-mcp"
    }
  }
}
```

---

## Getting Started

### Prerequisites

* Python 3.11 or higher.
* Linux host with KVM/QEMU and `libvirtd` (either running locally or accessible over SSH).

### Installation

#### Option 1: Standard User Tool Installation (`~/.local/bin/`) — Recommended
Installs `airlock` and `airlock-mcp` into an isolated user tool environment via `uv tool` or `pipx`:

```bash
# Standard isolated installation:
./install_airlock_mcp.sh

# Or install in editable mode for active development:
./install_airlock_mcp.sh --editable

# Install and automatically configure all IDE agent connectors:
./install_airlock_mcp.sh --connect
```

#### Option 2: Local Repository Virtual Environment
```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

### Unified `airlock` CLI

AirlockVM provides a unified `airlock` command line interface for everyday developer workflows:

```bash
# List all registered execution VMs:
airlock list

# Ping the AirlockVM control plane:
airlock ping

# Probe a hypervisor host and size runbooks:
airlock probe localhost
airlock probe my-hypervisor --interactive

# Audit pre-flight deployability gates:
airlock preflight <vm-name>

# Deploy the execution VM to the target hypervisor:
airlock deploy --vm <vm-name>

# Push application files to the VM:
airlock sync <vm-name>

# Run tests or build commands inside the isolated VM:
airlock exec <vm-name> -- pytest -q

# Pre-bake a golden base image on a hypervisor for ~10-15s VM spin-up:
airlock bake-image localhost

# Connect via SSH or open in remote IDE (VS Code / Cursor):
airlock connect <vm-name> --ide vscode

# Diagnose crashes or audit drift:
airlock diagnose <vm-name>
airlock drift <vm-name>
```

---

### Step-by-Step Runbook Workflow

### 1. Document Hypervisors & VMs in Inventory

Profile any virtualization host and archive its hardware, networking, and virtual machines:

```bash
# Discover and document remote hypervisor:
python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py remotehost

# Discover and document local KVM host:
python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py localhost

# List all documented hosts and VMs:
python3 .agents/skills/airlock_inventory/scripts/inventory_manager.py list
```

### 2. Auto-Probe and Generate Runbook

Run the configuration script targeting either `localhost` (for local KVM) or a remote hypervisor hostname:

```bash
# For local KVM on your workstation:
python3 .agents/skills/airlock_configure/scripts/configure_vm.py --probe localhost

# For a remote physical hypervisor:
python3 .agents/skills/airlock_configure/scripts/configure_vm.py --probe my-hypervisor-host
```

### 3. Preview Changes (Dry-Run Mode)

To inspect differences before modifying inventory runbooks:

```bash
python3 .agents/skills/airlock_configure/scripts/configure_vm.py --probe localhost --dry-run
```

### 4. Interactive Guided Configuration

Step through prompts with discovered and default values shown in brackets:

```bash
python3 .agents/skills/airlock_configure/scripts/configure_vm.py --probe localhost --interactive
```

### 5. Direct Parameter Overrides

Override specific variables directly from the command line:

```bash
python3 .agents/skills/airlock_configure/scripts/configure_vm.py \
  --probe localhost \
  --set VM_NAME=custom-runner-vm \
  --set RAM_MIB=8192 \
  --set VCPU_CORES=4
```

### 6. Validate Configuration

Ensure that all placeholders in the template are accounted for and no unrendered template tags exist:

```bash
python3 .agents/skills/airlock_configure/scripts/verify_config.py
```

### 7. Deploy the Execution Virtual Machine

Launch the virtual machine defined in `inventory/{vmhost}/{vm}/vm.yaml` (SSOT) with pre-requisite enforcement:

```bash
# Preview deployment plan (dry-run):
python3 .agents/skills/airlock_deploy/scripts/deploy_vm.py --dry-run

# Deploy with interactive confirmation:
python3 .agents/skills/airlock_deploy/scripts/deploy_vm.py

# Deploy non-interactively (CI / automated):
python3 .agents/skills/airlock_deploy/scripts/deploy_vm.py --yes
```

### 8. Deploy Application Code to VM

Generate an optimized `deploy_app_to_airlock.sh` script to mirror your application files into the VM:

```bash
# Generate the deployment script tailored to your project stack:
python3 .agents/skills/airlock_rsync/scripts/generate_sync.py

# Perform differential deployment (default VM):
./deploy_app_to_airlock.sh

# Or dispatch deployment to a specific inventory VM:
./deploy_app_to_airlock.sh --list
./deploy_app_to_airlock.sh appsurfer-vm --dry-run
./deploy_app_to_airlock.sh --all
```

---

## Pre-Flight Capability Checks

When using `--probe <host>`, AirlockVM performs automated health checks:

1. **Host Connectivity & Sudo**: Confirms SSH access and administrative permissions.
2. **Hardware Virtualization**: Checks `/dev/kvm` and CPU virtualization flags (`vmx` or `svm`).
3. **Hypervisor Daemon**: Confirms `virsh` availability and checks that `libvirtd` is running.
4. **Virtual Network**: Verifies that the default virtual network (`virbr0`) is active.
5. **Storage & Memory**: Checks for sufficient host RAM and free disk space.

If any check fails, the tool stops immediately and displays actionable troubleshooting commands.

---

## Integration Testing & Permutation Matrix

AirlockVM includes a full 40-permutation integration test suite (plus deterministic unit tests) covering observability, stack detection, placement, dedicated VM lifecycles, shared attach idempotency, drift detection, diagnostics, security isolation, multi-IDE CLI connectors, and live hypervisor proxy reachability:

```bash
# Run the complete test suite:
.venv/bin/python -m unittest discover -s tests -v
```

See the [Integration Testing Guide](docs/INTEGRATION_TESTING.md) for the complete 40-permutation matrix and fixture details.

---

## AI Agent Integration

This repository includes skill definitions compatible with Antigravity AI coding assistants. Agents can trigger these workflows using slash commands:

* `/airlock_inventory` — Discover, profile, and document hypervisors and VMs in `inventory/{vmhost}/{vm}/`.
* `/airlock_configure` — Probe hypervisors and generate parameterized VM execution runbooks.
* `/airlock_deploy` — Deploy, provision, and manage execution VMs with strict validation gates.
* `/airlock_rsync` — Evaluate project structure and generate the optimized `deploy_app_to_airlock.sh` script.
