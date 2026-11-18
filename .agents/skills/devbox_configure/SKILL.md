---
name: devbox_configure
description: >-
  Facilitates configuring, parameterizing, and customizing the DevBox execution virtual machine runbook
  (docs/DEVELOPMENT_VM.md) from a version-controlled template according to user preferences.
  Use when the user wants to customize VM topology, IP addresses, hypervisor settings, resource specs,
  ports, or credentials for their local or remote development VM.
---

# DevBox VM Configuration Skill (`devbox_configure`)

This skill facilitates the customization and regeneration of the DevBox VM runbook and specification ([`docs/DEVELOPMENT_VM.md`](../../../docs/DEVELOPMENT_VM.md)) using a version-controlled template ([`template/DEVELOPMENT_VM.md.template`](./template/DEVELOPMENT_VM.md.template)) and default configuration parameters ([`template/defaults.json`](./template/defaults.json)).

---

## Directory Structure

```text
.agents/skills/devbox_configure/
├── SKILL.md                          # Skill definition and instructions
├── template/
│   ├── DEVELOPMENT_VM.md.template    # Parameterized runbook template
│   └── defaults.json                 # Unpopulated parameter schema (empty values)
└── scripts/
    ├── configure_vm.py               # Main CLI, interactive, & file-driven customization tool
    ├── probe_hypervisor.py           # Smart hypervisor hardware & network discovery tool
    └── verify_config.py              # Validation & template coverage linter
```

---

## Configuration Workflow

### Mode 1: Smart Auto-Discovery & Probing (Recommended)
Automatically queries any target physical or local hypervisor to inspect hardware specs, libvirt networks, existing domains, DHCP leases, and suggests collision-free IP/MAC parameters.

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

All keys are defined in [`template/defaults.json`](./template/defaults.json) with empty values (`""`), ready to be populated by the user.

| Parameter Name | Description | Example Value |
| :--- | :--- | :--- |
| `PROJECT_NAME` | Project display name | `MyProject` |
| `VM_NAME` | libvirt domain name / SSH host alias | `myproject-vm` |
| `HYPERVISOR_HOST` | Physical hypervisor hostname / SSH alias | `hypervisor-host` (or `localhost`) |
| `HYPERVISOR_OS` | Operating system on hypervisor host | `Ubuntu 24.04 LTS` |
| `HYPERVISOR_SPECS` | Hypervisor hardware summary | `8 Cores CPU \| 16 GB RAM \| KVM / libvirt` |
| `HYPERVISOR_LAN_IP` | Hypervisor physical LAN IP | `192.168.1.100` |
| `DEV_HOST` | Local development workstation hostname | `dev-workstation` |
| `GUEST_OS` | Guest OS minimal cloud image name | `Ubuntu 24.04 LTS Minimal` |
| `GUEST_OS_DETAILS` | Guest OS image release details | `Pristine Noble Numbat cloud image` |
| `VCPU_CORES` | Number of virtual CPU cores assigned to guest | `4` |
| `CPU_MODE` | KVM CPU mode | `host-passthrough` |
| `RAM_MIB` | Guest RAM in MiB | `4096` |
| `RAM_GB` | Guest RAM in GB (auto-calculated from RAM_MIB) | `4` |
| `BALLOON_DRIVER` | Memory ballooning driver | `virtio-balloon` |
| `DISK_SIZE_GIB` | Virtual root disk size in GiB | `50` |
| `DISK_FORMAT` | Virtual disk image format | `qcow2` |
| `UEFI_FIRMWARE` | UEFI code file name | `OVMF_CODE_4M.ms.fd` |
| `VM_IP` | Static IP leased to the guest VM | `192.168.122.200` |
| `SUBNET_CIDR` | Subnet CIDR notation | `/24` |
| `VIRT_BRIDGE` | libvirt bridge network device | `virbr0` |
| `LIBVIRT_NETWORK` | libvirt virtual network name | `default` |
| `VM_MAC` | Static MAC address reserved in libvirt DHCP | `52:54:00:xx:xx:xx` |
| `VM_PRIMARY_USER` | Primary guest user with passwordless sudo & docker | `devuser` |
| `VM_SECONDARY_USER` | Secondary administrative user with sudo access | `admin` |
| `SSH_KEY_PATH` | Path to dedicated SSH private key | `~/.ssh/myproject_vm_ed25519` |
| `APP_INTERNAL_PORT` | Guest container / application port | `3000` |
| `PROXY_EXTERNAL_PORT` | Hypervisor `systemd-socket-proxyd` forward port | `3001` |
| `PROXY_SOCKET_SERVICE` | Path to systemd socket forwarder unit on hypervisor | `/etc/systemd/system/myproject-proxy.socket` |
| `REMOTE_CODE_DIR` | Target path on guest VM for `rsync` code sync | `~/MyProject/` |
| `SNAPSHOT_BASE_NAME` | Baseline snapshot name for rollback | `snapshot-base-clean` |
| `SNAPSHOT_BASE_DESC` | Description tag for the baseline snapshot | `Pristine base setup with Docker and Python` |

---

## Validation & Verification

To verify that the template and rendered document are synchronized with zero missing parameters or leftover template tags:

```bash
python3 .agents/skills/devbox_configure/scripts/verify_config.py
```
