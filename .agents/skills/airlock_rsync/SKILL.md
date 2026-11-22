---
name: airlock_rsync
description: >-
  Evaluates project source code and dependencies to generate an optimized 'deploy_app_to_vm.sh'
  script in the project root. The generated script synchronizes ONLY the application files
  necessary to run and test workloads inside the dedicated AirlockVM virtual machine, excluding
  version control, documentation, agent configs, and local build artifacts.
---

# AirlockVM Rsync Generator Skill (`airlock_rsync`)

This skill evaluates your project directory to determine the exact tech stack (Python, Node.js, Go, Rust, Docker, static assets) and generates an optimized, executable [`deploy_app_to_vm.sh`](../../../deploy_app_to_vm.sh) in the project root.

The generated script ensures that **ONLY code needed to run the application on the VM is synchronized**, while excluding heavy local caches, local dependencies, virtual environments, documentation, and agent configurations.

---

## Directory Structure

```text
.agents/skills/airlock_rsync/
├── SKILL.md                          # Skill definition and instructions
└── scripts/
    └── generate_sync.py              # Project evaluation and script generator
```

---

## How It Works

1. **VM Target Discovery**:
   * Inspects `inventory/{vmhost}/{vm}/VM.md` or repository defaults to identify the target VM domain name (e.g. `airlock-vm`) and destination directory (e.g. `~/AirlockVM/`).
2. **Intelligent Stack Evaluation**:
   * Analyzes project manifests (`requirements.txt`, `package.json`, `Cargo.toml`, `go.mod`, `Dockerfile`).
   * Determines files that must **never** be copied to the VM (such as `.git/`, `.agents/`, local `node_modules/`, `venv/`, `.env*.local`, and documentation).
3. **Generates `deploy_app_to_vm.sh`**:
   * Writes a ready-to-run, executable shell script in the project root.
   * Dispatches deployments to dedicated VM scripts in `inventory/<vmhost>/<vm>/deploy_app_to_vm.sh`.

---

## Usage

### 1. Generate or Update `deploy_app_to_vm.sh`

Run the generator from your repository:

```bash
python3 .agents/skills/airlock_rsync/scripts/generate_sync.py
```

To specify custom VM host or remote directory overrides:

```bash
python3 .agents/skills/airlock_rsync/scripts/generate_sync.py \
  --vm-host my-custom-vm \
  --remote-dir ~/my-app/
```

### 2. Using `deploy_app_to_vm.sh` (Multi-VM Dispatcher & Deployment)

When `inventory/` is present, `./deploy_app_to_vm.sh` acts as a multi-VM dispatcher that forwards commands to each VM's dedicated deployment script (`inventory/<vmhost>/<vm>/deploy_app_to_vm.sh`).

```bash
# List all discovered inventory VMs and their deployment scripts:
./deploy_app_to_vm.sh --list

# Deploy to default VM (e.g. airlock-vm):
./deploy_app_to_vm.sh

# Deploy to a specific VM:
./deploy_app_to_vm.sh appsurfer-vm
./deploy_app_to_vm.sh rugbyasssistant-vm

# Pass rsync options (e.g., --offline, --dry-run, --delete, --no-restart):
./deploy_app_to_vm.sh --offline
./deploy_app_to_vm.sh appsurfer-vm --dry-run
./deploy_app_to_vm.sh airlock-vm --delete
./deploy_app_to_vm.sh --no-restart     # Sync files only without running post-deploy commands

# Deploy application to all inventory VMs sequentially:
./deploy_app_to_vm.sh --all
```

### 3. Dynamic Post-Deploy Hooks (`post_deploy`)

In `inventory/<vmhost>/<vm>/vm.yaml`, each repository entry can declare an optional `post_deploy:` command:

```yaml
repositories:
  - name: AppSurfer
    local_path: /path/to/AppSurfer
    remote_path: ~/AppSurfer/
    post_deploy: "pip install -e . && python3 -m appsurfer.server" # or "docker compose up -d"
```

* When `post_deploy` is configured, `deploy_app_to_vm.sh` automatically executes the command inside the repository's remote path on the VM after rsync finishes.
* Use `--dry-run` to preview the planned post-deploy command without running it.
* Use `--no-restart` or `--sync-only` to sync code without triggering the post-deploy command.

You can also run any VM's deployment script directly:
```bash
./inventory/barkley/appsurfer-vm/deploy_app_to_vm.sh --offline
```

### Dry-Run Itemized Change Legend

When `--dry-run` is used, rsync itemizes each file transfer with an status prefix:
* `>f+++++++++`: New file to be copied to the VM
* `>f.st......`: Existing file whose size or timestamp has changed
* `*deleting  `: Remote file that will be removed (when `--delete` is enabled)
* `.d..t......`: Directory attributes updated

---

## Exclusion Invariants

The generator enforces strict exclusion rules:
* **Version Control**: `.git/`, `.github/`, `.gitignore`
* **Agent Frameworks**: `.agents/`, `.gemini/`, `.claude/`
* **Local Runtimes & Caches**: `venv/`, `.venv/`, `node_modules/`, `__pycache__/`, `.pytest_cache/`, `dist/`, `.next/` *(these must be built natively inside the VM)*
* **Secrets & Local Configs**: `.env*.local`, `*.pem`, `*.key`, `my_*.json`
* **Documentation, Inventory & Runbooks**: `docs/`, `inventory/`, `analysis/`, `*.md`, `LICENSE`
