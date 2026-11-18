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
| Dedicated Local VM (devbox-vm)     |
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

### 2. Fast Code Synchronization
Sync your code into the local VM in milliseconds using the generated sync script:
```bash
# Push application changes to the dedicated VM (skips caches, venvs, and docs)
./sync_to_devbox.sh
```
*(Generated automatically by the `devbox_rsync` skill; alternatively, attach via VS Code Remote-SSH to edit directly inside the VM).*

### 3. Execution & Testing Inside the VM
Execute builds, migrations, and test suites over SSH:
```bash
# Run tests inside the isolated guest
ssh devbox-vm "docker compose up -d && pytest"
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
sudo virsh snapshot-revert devbox-vm snapshot-base-clean
```
Your workstation host OS remains untouched, clean, and fast.

---

## How DevBox Automates This Setup

DevBox takes care of all setup friction automatically:
1. **Detects Local KVM**: Probes your hardware to ensure `/dev/kvm` and `virbr0` are available (`devbox_configure`).
2. **Calculates Proportional Sizing**: Allocates 50% vCPUs and ~25% RAM to the VM so your workstation desktop never freezes.
3. **Generates the Exact Runbook**: Creates your step-by-step operational specification ([`docs/DEVELOPMENT_VM.md`](docs/DEVELOPMENT_VM.md)).
4. **Automated VM Provisioning**: Provisions domain, cloud-init seed, static DHCP, and socket proxying (`devbox_deploy`).
5. **Intelligent Code Synchronization**: Excludes heavy local caches and copies only application files (`devbox_rsync`).

To set this up for your machine:
```bash
# 1. Configure the runbook:
python3 .agents/skills/devbox_configure/scripts/configure_vm.py --probe localhost

# 2. Deploy the VM:
python3 .agents/skills/devbox_deploy/scripts/deploy_vm.py --yes

# 3. Generate the sync script:
python3 .agents/skills/devbox_rsync/scripts/generate_sync.py
```
