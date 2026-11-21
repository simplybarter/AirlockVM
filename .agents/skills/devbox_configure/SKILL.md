---
name: devbox_configure
description: >-
  Facilitates configuring, parameterizing, and customizing the DevBox execution virtual machine runbook
  (docs/VM.md) from a version-controlled template according to user preferences.
  Use when the user wants to customize VM topology, IP addresses, hypervisor settings, resource specs,
  ports, or credentials for their local or remote development VM.
---

# DevBox VM Configuration Skill (`devbox_configure`)

This skill facilitates the customization and regeneration of the DevBox VM runbook and specification ([`docs/VM.md`](../../../docs/VM.md)) using a version-controlled template ([`template/VM.md.template`](./template/VM.md.template)) and inventory schema validation.

---

## Directory Structure

```text
.agents/skills/devbox_configure/
├── SKILL.md                          # Skill definition and instructions
├── template/
│   └── VM.md.template                # Parameterized runbook template
└── scripts/
    ├── configure_vm.py               # Main CLI, interactive, & file-driven customization tool
    ├── probe_hypervisor.py           # Smart hypervisor hardware & network discovery tool
    └── verify_config.py              # Validation & template coverage linter
```

---

## Configuration Workflow

### Mode 1: Smart Auto-Discovery & Probing (Recommended)
Automatically queries any target physical or local hypervisor to inspect hardware specs, libvirt networks, existing domains, DHCP leases, and suggests collision-free IP/MAC parameters.

### Mode 1b: Inventory-Driven Offline Pre-Seeding
When hypervisors are already documented in `inventory/` (via `devbox_inventory`), you can generate the runbook completely offline without making live network connections:

```bash
# Pre-seed runbook from inventory records (offline mode):
python3 .agents/skills/devbox_configure/scripts/configure_vm.py --from-inventory barkley
```

* **Automated Pre-Flight Capability Checks**:
  Validates that the target host meets all essential virtualization prerequisites before attempting configuration:
  * `[✓] Host Connectivity & Sudo`: Verifies SSH key-based access and administrative privilege.
  * `[✓] Hardware Virtualization`: Checks `/dev/kvm` and CPU `vmx`/`svm` flags. Provides BIOS/nested virtualization guidance if missing.
  * `[✓] Hypervisor Daemon`: Verifies `virsh` installation and checks that `libvirtd` is active (`qemu:///system`).
  * `[✓] Virtual Network & Bridge`: Confirms active status of the `default` libvirt network and `virbr0` bridge device.
  * `[✓] Capacity Headroom`: Checks that the host has $\ge 4\text{ GB}$ total RAM and $\ge 10\text{ GB}$ free backing disk storage.
  * If any check fails, the tool stops immediately and displays specific remediation commands.

* **Proportional Resource Sizing**: Computes balanced vCPU and RAM allocations based on physical host capacity, reserving headroom for host OS tasks:
  * **vCPUs**: Allocates ~50% of host cores (up to 8 vCPUs max for a development VM).
  * **RAM**: Allocates ~25% of host physical RAM rounded to standard tiers (`2 GB`, `4 GB`, `8 GB`, `16 GB`, `32 GB`).
* **Automated Project & VM Naming**:
  * **Project Name**: Defaults automatically to the repository root directory name (e.g. `DevBox`) if not explicitly provided.
  * **VM Domain Name**: Derives `<project-slug>-vm` (e.g. `devbox-vm`), primary guest user (`<project-slug>`), SSH key paths, and systemd socket forwarder names directly from the project name, checking libvirt to prevent domain collisions.

```bash
# Auto-probe a remote hypervisor with proportional sizing:
python3 .agents/skills/devbox_configure/scripts/configure_vm.py --probe <hypervisor_host> --set PROJECT_NAME=MyProject

# Auto-probe local KVM hypervisor (proportional sizing + default VM naming):
python3 .agents/skills/devbox_configure/scripts/configure_vm.py --probe localhost --set PROJECT_NAME=Devbox

# Or run the probe tool standalone to export discovered JSON:
python3 .agents/skills/devbox_configure/scripts/probe_hypervisor.py --hypervisor <hypervisor_host> --project MyProject --save-config my_env.json
```

### Mode 2: Interactive Customization
To guide the user interactively through all configuration parameters (with discovered or default values shown in brackets):

```bash
python3 .agents/skills/devbox_configure/scripts/configure_vm.py --probe <hypervisor_host> --interactive
```

### Mode 3: Targeted Parameter Overrides (`--set`)
To override specific parameters directly from the command line:

```bash
# Example: Change VM Name, IP address, and RAM allocation
python3 .agents/skills/devbox_configure/scripts/configure_vm.py \
  --set VM_NAME=my-custom-vm \
  --set VM_IP=192.168.122.100 \
  --set RAM_MIB=8192
```

### Mode 3: Config-Driven Workflow (JSON File)
To persist and version-control custom user profiles:

1. **Export defaults to a custom config file**:
   ```bash
   python3 .agents/skills/devbox_configure/scripts/configure_vm.py --dump-defaults my_devbox.json
   ```

2. **Edit `my_devbox.json`** with customized environment values (e.g., hypervisor IP, ports, user accounts).

3. **Render document from the configuration**:
   ```bash
   python3 .agents/skills/devbox_configure/scripts/configure_vm.py --config my_devbox.json
   ```

---

## Parameter Reference

DevBox partitions configuration parameters cleanly between **Project & Guest Blueprint Defaults** (defined in [`template/defaults.json`](./template/defaults.json)) and **Infrastructure & Hypervisor State** (provided dynamically by [`devbox_inventory`](../devbox_inventory/SKILL.md)):

### 1. Project & Guest Blueprint Defaults (`defaults.json`)
Parameters specific to the development workstation, application stack, and provisioning recipe:

| Parameter Name | Description | Example Value |
| :--- | :--- | :--- |
| `PROJECT_NAME` | Project display name | `MyProject` |
| `DEV_HOST` | Local development workstation hostname | `dev-workstation` |
| `DEV_HOST_TITLE` | Capitalized workstation hostname | `Dev-workstation` |
| `VM_PRIMARY_USER` | Primary guest user with passwordless sudo & docker | `devuser` |
| `VM_SECONDARY_USER` | Secondary administrative user with sudo access | `admin` |
| `SSH_KEY_PATH` | Path to dedicated SSH private key | `~/.ssh/myproject_vm_ed25519` |
| `APP_INTERNAL_PORT` | Guest container / application port | `3000` |
| `PROXY_EXTERNAL_PORT` | Hypervisor `systemd-socket-proxyd` forward port | `3001` |
| `PROXY_SOCKET_SERVICE` | Path to systemd socket forwarder unit on hypervisor | `/etc/systemd/system/myproject-proxy.socket` |
| `REMOTE_CODE_DIR` | Target path on guest VM for `rsync` code sync | `~/MyProject/` |
| `GUEST_OS` | Guest OS minimal cloud image name | `Ubuntu 24.04 LTS Minimal` |
| `GUEST_OS_DETAILS` | Guest OS image release details | `Pristine Noble Numbat cloud image` |
| `CPU_MODE` | KVM CPU mode | `host-passthrough` |
| `BALLOON_DRIVER` | Memory ballooning driver | `virtio-balloon` |
| `DISK_SIZE_GIB` | Virtual root disk size in GiB | `50` |
| `DISK_FORMAT` | Virtual disk image format | `qcow2` |
| `UEFI_FIRMWARE` | UEFI code file name | `OVMF_CODE_4M.ms.fd` |
| `SNAPSHOT_BASE_NAME` | Baseline snapshot name for rollback | `snapshot-base-clean` |
| `SNAPSHOT_BASE_DESC` | Description tag for the baseline snapshot | `Pristine base setup with Docker and Python` |

### 2. Infrastructure & Hypervisor Attributes (From `devbox_inventory`)
Parameters discovered and populated from [`inventory/{vmhost}/vmhost.yaml`](../devbox_inventory/template/vmhost.yaml.template) and [`inventory/{vmhost}/{vm}/vm.yaml`](../devbox_inventory/template/vm.yaml.template):

| Parameter Name | Description | Source in Inventory |
| :--- | :--- | :--- |
| `HYPERVISOR_HOST` | Hypervisor hostname / SSH alias | `vmhost.name` |
| `HYPERVISOR_HOST_TITLE` | Capitalized hypervisor hostname | `vmhost.name.capitalize()` |
| `HYPERVISOR_OS` | Operating system on hypervisor host | `vmhost.system.os.pretty_name` |
| `HYPERVISOR_SPECS` | Hypervisor hardware summary | `vmhost.hardware.cpu` + `memory` |
| `HYPERVISOR_LAN_IP` | Hypervisor physical LAN IP | `vmhost.connection.host` |
| `VIRT_BRIDGE` | libvirt bridge network device | `vmhost.virtualization.networks[].bridge` |
| `LIBVIRT_NETWORK` | libvirt virtual network name | `vmhost.virtualization.networks[].name` |
| `SUBNET_CIDR` | Subnet CIDR notation | `vmhost.virtualization.networks[].netmask` |
| `VM_NAME` | libvirt domain name / SSH alias | `vm.name` (collision-checked) |
| `VM_IP` | Static IP leased to the guest VM | `vm.network.interfaces[].ip` / DHCP |
| `VM_MAC` | Static MAC address reserved in libvirt | `vm.network.interfaces[].mac` |
| `VCPU_CORES` | Virtual CPU cores assigned to guest | Proportional sizing / `vm.resources.vcpu` |
| `RAM_MIB` | Guest RAM in MiB | Proportional sizing / `vm.resources.memory_mib` |
| `RAM_GB` | Guest RAM in GB | `RAM_MIB // 1024` |

---

## Validation & Verification

To verify that the template and rendered document are synchronized with zero missing parameters or leftover template tags:

```bash
python3 .agents/skills/devbox_configure/scripts/verify_config.py
```
