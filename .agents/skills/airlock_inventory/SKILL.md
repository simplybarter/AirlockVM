---
name: airlock_inventory
description: >-
  Discovers, documents, and registers hypervisors (vmhosts) and guest virtual machines into a structured
  inventory directory (inventory/{vmhost}/vmhost.yaml and inventory/{vmhost}/{vm}/). Uses defined YAML
  templates to document host specs, network topologies, storage pools, and guest virtual machine configurations.
---

# AirlockVM VM Host & Guest Inventory Skill (`airlock_inventory`)

The `airlock_inventory` skill automates the discovery, profiling, and documentation of virtualization hosts (hypervisors) and their virtual machines. It archives detailed hardware, network topology, storage pool, and guest VM configurations into a structured, version-controlled inventory repository using defined YAML templates.

---

## Inventory Structure

When a VM host is added or audited, the skill creates and maintains the following directory layout:

```text
inventory/
└── {vmhost}/
    ├── vmhost.yaml                   # Documented YAML record for the hypervisor host
    ├── {vm_1}/
    │   ├── vm.yaml                   # Documented YAML record + project blueprint
    │   └── VM.md                     # Fully rendered VM operational runbook
    ├── {vm_2}/
    │   ├── vm.yaml
    │   └── VM.md
    └── {vm_n}/
        ├── vm.yaml
        └── VM.md
```

---

## Directory Structure

```text
.agents/skills/airlock_inventory/
├── SKILL.md                          # Skill documentation & reference guide
├── template/
│   ├── vmhost.yaml.template          # Defined YAML template for the hypervisor host
│   └── vm.yaml.template              # Defined YAML template for individual virtual machines
└── scripts/
    ├── add_vmhost.py                 # Core discovery & inventory generation tool
    ├── inventory_manager.py          # Inventory listing, inspection, & reporting CLI
    └── verify_config.py              # Template syntax & inventory YAML validation linter
```

---

## Key Features

1. **Automated Host Discovery**:
   * Inspects OS distribution, kernel version, architecture, and uptime.
   * Collects hardware specs: CPU model, physical sockets, cores, threads, and hardware VT capability (`vmx`/`svm`, `/dev/kvm`).
   * Measures physical RAM capacity, available memory, and root filesystem disk utilization.
   * Audits Libvirt daemon status, QEMU version, active storage pools, and virtual bridge networks (including DHCP subnets, ranges, and static lease reservations).
   * Detects zero-`sudo` rootless hypervisor capability (`libvirt,kvm` group membership + active `qemu:///system` access) and records the resolved SSH identity file in `connection.ssh_key_path`.

2. **Per-VM Detailed Profiling**:
   * Inspects all defined libvirt domains on the target host.
   * Records execution state (`running`, `shut off`, etc.), vCPU allocation, and RAM allocation.
   * Catalogs disk topology (target devices, bus types, formats, backing source images).
   * Maps virtual network interfaces (MAC addresses, virtual bridges, and assigned IP addresses from static or dynamic DHCP leases).
   * Inspects security parameters (AppArmor confinement, QEMU guest agent channel status).

3. **Template-Driven Architecture**:
   * Uses [`template/vmhost.yaml.template`](./template/vmhost.yaml.template) to document the host in `inventory/{vmhost}/vmhost.yaml`.
   * Uses [`template/vm.yaml.template`](./template/vm.yaml.template) to document each VM in `inventory/{vmhost}/{vm}/vm.yaml`.
   * Enforces 100% syntactically valid YAML with zero leftover placeholders.

---

## Usage Guide

### 1. Adding / Updating a VM Host in Inventory

To discover and document a hypervisor host (e.g. `remotehost` or `localhost`):

```bash
# Add a remote hypervisor via SSH:
python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py remotehost

# Add local KVM hypervisor:
python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py localhost

# Target a specific SSH user, port, or key:
python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py 192.168.1.50 --user ubuntu --port 22 -i ~/.ssh/id_ed25519

# Dry-run preview without writing files:
python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py remotehost --dry-run
```

### 2. Targeting a Specific Virtual Machine

To document or refresh only a specific VM under a host:

```bash
python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py remotehost --vm airlock-vm
```

### 3. Querying & Inspecting Inventory

Use `inventory_manager.py` to inspect the documented environment:

```bash
# List all documented hosts and their virtual machines:
python3 .agents/skills/airlock_inventory/scripts/inventory_manager.py list

# View the full YAML record for a host:
python3 .agents/skills/airlock_inventory/scripts/inventory_manager.py show remotehost

# View the full YAML record for a specific VM:
python3 .agents/skills/airlock_inventory/scripts/inventory_manager.py show remotehost airlock-vm
```

### 4. Validating Inventory Records

Use `verify_config.py` to ensure all templates and inventory records are valid:

```bash
python3 .agents/skills/airlock_inventory/scripts/verify_config.py
```

---

## Integration with AirlockVM Skills Suite

The AirlockVM workflow integrates four complementary skills:

1. **`airlock_inventory`**: Discovers and documents existing hypervisors and VMs in `inventory/{vmhost}/{vm}/`.
2. **`airlock_configure`**: Generates a parameterized VM runbook (`inventory/{vmhost}/{vm}/VM.md`) tailored to the discovered hypervisor.
3. **`airlock_deploy`**: Provisions, launches, and proxies the dedicated development VM.
4. **`airlock_rsync`**: Deploys application source code and synchronizes development changes into the running VM.
