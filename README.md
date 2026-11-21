# DevBox

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Linux KVM](https://img.shields.io/badge/Virtualization-Linux%20KVM-orange.svg?logo=linux&logoColor=white)](https://www.linux-kvm.org/)
[![libvirt](https://img.shields.io/badge/Hypervisor-libvirt%20%2F%20QEMU-red.svg?logo=qemu&logoColor=white)](https://libvirt.org/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

> **Automated Execution Virtual Machine Sizing, Runbook Generator, and Agent Skill**

[📖 Zero-Workload Use Case](USECASE.md) • [🏗 Architecture](docs/ARCHITECTURE.md) • [⚡ Quickstart](#getting-started) • [🤝 Contributing](CONTRIBUTING.md)

DevBox provides automated tooling and a parameterized specification framework to configure, size, and deploy dedicated Linux KVM execution virtual machines for development teams and AI agents.

By strictly separating software engineering workstations from runtime execution environments, DevBox enforces an isolation boundary: all builds, crawler loops, browser automation, and untrusted code evaluation execute inside dedicated guest VMs hosted on local or remote hypervisors.

---

## Key Features

* **Universal & Zero-Assumption**: Operates seamlessly across local KVM installations or remote physical hypervisors via SSH, without hardcoded machine names or private IP addresses.
* **Smart Pre-Flight Capability Checks**: Automatically inspects target hypervisors for `/dev/kvm` hardware acceleration, CPU virtualization extensions (`vmx`/`svm`), active `libvirtd` daemons, and storage space before provisioning.
* **Proportional Resource Allocation**: Automatically discovers physical host CPU threads and total RAM, dynamically sizing guest vCPUs (e.g. 50% capacity) and memory (e.g. 25% capacity) to prevent host starvation.
* **Conflict-Free Network Discovery**: Queries libvirt networks (such as `default` / `virbr0`), inspects active DHCP leases, allocates the next unassigned static IP, and generates unique collision-free MAC addresses (`52:54:00:xx:xx:xx`).
* **Automated Project-to-VM Naming**: Derives the project name from the repository root folder, auto-generating domain names (`<project>-vm`), service units, and user accounts.
* **Automated Virtual Machine Deployment (`devbox_deploy`)**: Launches the execution VM from `docs/VM.md` with strict pre-requisite verification gates (validates runbook existence and `verify_config.py` health), automated cloud-init image seeding, static DHCP reservations, and systemd socket forwarding.
* **Intelligent Code Synchronization (`devbox_rsync`)**: Evaluates project manifests (Python, Node.js, Rust, Go) and generates an optimized, git-ignored `sync_to_devbox.sh` script to sync ONLY application code to the VM while ignoring local caches and agent files.
* **Antigravity AI Agent Skills**: Includes ready-to-use `.agents/skills/devbox_configure`, `.agents/skills/devbox_deploy`, and `.agents/skills/devbox_rsync` skills for autonomous agent-driven VM customization, provisioning, and workspace sync.

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
├── install_skill.sh                         # Skill installer & updater script
├── sync_to_devbox.sh                        # Generated application sync script (git-ignored)
├── inventory/                               # Documented hypervisors and VMs (git-ignored)
│   └── {vmhost}/                            # VM host directory
│       ├── vmhost.yaml                      # Documented host YAML record
│       └── {vm}/                            # Virtual machine directory
│           ├── vm.yaml                      # Documented VM record + blueprint
│           └── VM.md                        # Rendered VM operational runbook
├── docs/                                    # Generated runbooks (git-ignored)
│   ├── ARCHITECTURE.md                      # Security model and isolation topology
│   └── VM.md                                # Generated VM execution runbook
├── .agents/skills/
│   ├── devbox_inventory/
│   │   ├── SKILL.md                         # Inventory discovery & management skill
│   │   ├── template/
│   │   │   ├── vmhost.yaml.template         # Defined host YAML template
│   │   │   ├── vm.yaml.template             # Defined VM YAML template
│   │   │   └── defaults.json                # Schema and validation defaults
│   │   └── scripts/
│   │       ├── add_vmhost.py                # Hypervisor & VM discovery engine
│   │       ├── inventory_manager.py         # Inventory inspection and query CLI
│   │       └── verify_config.py             # Inventory syntax and validation linter
│   ├── devbox_configure/
│   │   ├── SKILL.md                         # Agent skill instructions and reference
│   │   ├── template/
│   │   │   └── VM.md.template               # Parameterized runbook template
│   │   └── scripts/
│   │       ├── configure_vm.py              # Primary customization and rendering engine
│   │       ├── probe_hypervisor.py          # Hypervisor discovery and capability checker
│   │       └── verify_config.py             # Template coverage and syntax validator
│   ├── devbox_deploy/
│   │   ├── SKILL.md                         # Virtual machine deployment skill
│   │   └── scripts/
│   │       └── deploy_vm.py                 # VM provisioning and lifecycle engine
│   └── devbox_rsync/
│       ├── SKILL.md                         # Application code synchronization skill
│       └── scripts/
│           └── generate_sync.py             # Sync script generator & project evaluator
└── .gitignore
```

---

## Installation & Skill Setup

You can install or update the DevBox agent skills (`devbox_inventory`, `devbox_configure`, `devbox_deploy`, `devbox_rsync`) into any repository or globally across your development environment using `install_skill.sh`:

```bash
# Option A: Install all skills into current repository (.agents/skills/*)
./install_skill.sh

# Option B: One-line install/update directly from GitHub
curl -fsSL https://raw.githubusercontent.com/SimplyBarter/DevBox/main/install_skill.sh | bash

# Option C: Install globally for all projects (~/.agents/skills/*)
./install_skill.sh --global
```

---

## Getting Started

### Prerequisites

* Python 3.10 or higher.
* Linux host with KVM/QEMU and `libvirtd` (either running locally or accessible over SSH).

### 1. Document Hypervisors & VMs in Inventory

Profile any virtualization host and archive its hardware, networking, and virtual machines:

```bash
# Discover and document remote hypervisor:
python3 .agents/skills/devbox_inventory/scripts/add_vmhost.py barkley

# Discover and document local KVM host:
python3 .agents/skills/devbox_inventory/scripts/add_vmhost.py localhost

# List all documented hosts and VMs:
python3 .agents/skills/devbox_inventory/scripts/inventory_manager.py list
```

### 2. Auto-Probe and Generate Runbook

Run the configuration script targeting either `localhost` (for local KVM) or a remote hypervisor hostname:

```bash
# For local KVM on your workstation:
python3 .agents/skills/devbox_configure/scripts/configure_vm.py --probe localhost

# For a remote physical hypervisor:
python3 .agents/skills/devbox_configure/scripts/configure_vm.py --probe my-hypervisor-host
```

### 3. Preview Changes (Dry-Run Mode)

To inspect differences before modifying `docs/VM.md`:

```bash
python3 .agents/skills/devbox_configure/scripts/configure_vm.py --probe localhost --dry-run
```

### 4. Interactive Guided Configuration

Step through prompts with discovered and default values shown in brackets:

```bash
python3 .agents/skills/devbox_configure/scripts/configure_vm.py --probe localhost --interactive
```

### 5. Direct Parameter Overrides

Override specific variables directly from the command line:

```bash
python3 .agents/skills/devbox_configure/scripts/configure_vm.py \
  --probe localhost \
  --set VM_NAME=custom-runner-vm \
  --set RAM_MIB=8192 \
  --set VCPU_CORES=4
```

### 6. Validate Configuration

Ensure that all placeholders in the template are accounted for and no unrendered template tags exist:

```bash
python3 .agents/skills/devbox_configure/scripts/verify_config.py
```

### 7. Deploy the Execution Virtual Machine

Launch the virtual machine defined in `docs/VM.md` with pre-requisite enforcement:

```bash
# Preview deployment plan (dry-run):
python3 .agents/skills/devbox_deploy/scripts/deploy_vm.py --dry-run

# Deploy with interactive confirmation:
python3 .agents/skills/devbox_deploy/scripts/deploy_vm.py

# Deploy non-interactively (CI / automated):
python3 .agents/skills/devbox_deploy/scripts/deploy_vm.py --yes
```

### 8. Synchronize Application Code

Generate an optimized `sync_to_devbox.sh` script to mirror your project files into the VM:

```bash
# Generate the sync script tailored to your project stack:
python3 .agents/skills/devbox_rsync/scripts/generate_sync.py

# Perform differential synchronization:
./sync_to_devbox.sh
```

---

## Pre-Flight Capability Checks

When using `--probe <host>`, DevBox performs automated health checks:

1. **Host Connectivity & Sudo**: Confirms SSH access and administrative permissions.
2. **Hardware Virtualization**: Checks `/dev/kvm` and CPU virtualization flags (`vmx` or `svm`).
3. **Hypervisor Daemon**: Confirms `virsh` availability and checks that `libvirtd` is running.
4. **Virtual Network**: Verifies that the default virtual network (`virbr0`) is active.
5. **Storage & Memory**: Checks for sufficient host RAM and free disk space.

If any check fails, the tool stops immediately and displays actionable troubleshooting commands.

---

## AI Agent Integration

This repository includes skill definitions compatible with Antigravity AI coding assistants. Agents can trigger these workflows using slash commands:

* `/devbox_inventory` — Discover, profile, and document hypervisors and VMs in `inventory/{vmhost}/{vm}/`.
* `/devbox_configure` — Probe hypervisors and generate parameterized VM execution runbooks.
* `/devbox_deploy` — Deploy, provision, and manage execution VMs with strict validation gates.
* `/devbox_rsync` — Evaluate project structure and generate the optimized `sync_to_devbox.sh` script.
