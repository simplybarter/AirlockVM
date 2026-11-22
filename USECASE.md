# Use Case: Zero-Workload Workstation with Local Dedicated VM

## The Problem

When developing complex applications, running builds, dependencies, and testing suites directly on your local workstation leads to:
* **System Clutter**: Hundreds of gigabytes of global packages, Docker containers, caching layers, and Python virtual environments polluting your host OS.
* **Security & Stability Risks**: Untrusted scripts, browser automation loops, or third-party packages executing with full access to your personal filesystem and SSH keys.
* **Workstation Slowdowns**: Heavy CPU/RAM workloads locking up your desktop environment while you are trying to write code.

---

## The Solution: Local Two-Tier Execution

You author code on your workstation, but **zero application code runs directly on your host machine**. Instead, everything executes inside a dedicated local Linux KVM virtual machine.

```text
+------------------------------------+
| Local Workstation (Host OS)        |
| * Code Authoring & Git Commits     |
| * IDE (VS Code / Cursor / Neovim)  |
| * Web Browser (Testing UI)         |
| * ZERO RUNTIME WORKLOADS           |
+------------------------------------+
                  |
                  | rsync / SSH
                  v
+------------------------------------+
| Dedicated Local VM (airlock-vm)     |
| * Local KVM (/dev/kvm) on host     |
| * Docker & Container Fleets        |
| * Headless Browsers & Test Runners |
| * All Python/Node/Database Runtimes|
+------------------------------------+
```

---

## Daily Developer Workflow

### 1. Code Authoring on Host
Work inside your favorite IDE on your workstation as usual. You have full access to your desktop tools, extensions, and git workflows.

### 2. Fast Application Code Deployment
Deploy your application code into the local VM in milliseconds using the generated deployment script:
```bash
# Push application changes to the dedicated VM (skips caches, venvs, and docs)
./deploy_app_to_airlock.sh
```
*(Generated automatically by the `airlock_rsync` skill; alternatively, attach via VS Code Remote-SSH to edit directly inside the VM).*

### 3. Execution & Testing Inside the VM
Execute builds, migrations, and test suites over SSH:
```bash
# Run tests inside the isolated guest
ssh airlock-vm "docker compose up -d && pytest"
```

### 4. Viewing the Web App in Your Host Browser
The VM web server runs inside the guest (e.g. port `3000`). Access it directly from your workstation browser at:
```text
http://192.168.122.236:3000
```
*(Or forward via hypervisor socket so zero background tunnels are needed on your workstation).*

### 5. Instant Rollback & Clean Slate
If experimental packages or broken container builds clutter the guest environment, reset the VM back to a clean baseline snapshot in seconds:
```bash
sudo virsh snapshot-revert airlock-vm snapshot-base-clean
```
Your workstation host OS remains untouched, clean, and fast.

---

## How AirlockVM Automates This Setup

AirlockVM takes care of all setup friction automatically across a cohesive 4-skill lifecycle:

1. **Discovers Hypervisor & VMs**: Scans local or remote KVM hosts, documenting hardware, storage pools, networks, and existing VMs into structured YAML records (`airlock_inventory`).
2. **Detects Capabilities & Sizes Headroom**: Ensures `/dev/kvm` and `virbr0` are available, calculating proportional vCPU and RAM allocations so your workstation never freezes (`airlock_configure`).
3. **Generates the Exact Runbook**: Creates your step-by-step operational specification (`inventory/{vmhost}/{vm}/VM.md`) with zero leftover placeholders.
4. **Automated VM Provisioning & Inventory Sync**: Provisions domain, cloud-init seed, static DHCP, and socket proxying, auto-registering the VM in inventory (`airlock_deploy`).
5. **Intelligent Code Synchronization**: Excludes heavy local caches and copies only application files (`airlock_rsync`).

To set this up for your machine:
```bash
# Using the unified airlock CLI:
airlock probe localhost
airlock deploy --yes
airlock sync

# Or using underlying skill scripts:
# 1. Discover and document the hypervisor host:
python3 .agents/skills/airlock_inventory/scripts/add_vmhost.py localhost

# 2. Configure the runbook:
python3 .agents/skills/airlock_configure/scripts/configure_vm.py --probe localhost

# 3. Deploy the VM (auto-registers in inventory):
python3 .agents/skills/airlock_deploy/scripts/deploy_vm.py --yes

# 4. Generate the sync script:
python3 .agents/skills/airlock_rsync/scripts/generate_sync.py
```
