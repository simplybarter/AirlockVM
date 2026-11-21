---
name: devbox_rsync
description: >-
  Evaluates project source code and dependencies to generate an optimized 'sync_to_devbox.sh'
  script in the project root. The generated script synchronizes ONLY the application files
  necessary to run and test workloads inside the dedicated DevBox virtual machine, excluding
  version control, documentation, agent configs, and local build artifacts.
---

# DevBox Rsync Generator Skill (`devbox_rsync`)

This skill evaluates your project directory to determine the exact tech stack (Python, Node.js, Go, Rust, Docker, static assets) and generates an optimized, executable [`sync_to_devbox.sh`](../../../sync_to_devbox.sh) in the project root.

The generated script ensures that **ONLY code needed to run the application on the VM is synchronized**, while excluding heavy local caches, local dependencies, virtual environments, documentation, and agent configurations.

---

## Directory Structure

```text
.agents/skills/devbox_rsync/
├── SKILL.md                          # Skill definition and instructions
└── scripts/
    └── generate_sync.py              # Project evaluation and script generator
```

---

## How It Works

1. **VM Target Discovery**:
   * Inspects `docs/VM.md` or repository defaults to identify the target VM domain name (e.g. `devbox-vm`) and destination directory (e.g. `~/DevBox/`).
2. **Intelligent Stack Evaluation**:
   * Analyzes project manifests (`requirements.txt`, `package.json`, `Cargo.toml`, `go.mod`, `Dockerfile`).
   * Determines files that must **never** be copied to the VM (such as `.git/`, `.agents/`, local `node_modules/`, `venv/`, `.env*.local`, and documentation).
3. **Generates `sync_to_devbox.sh`**:
   * Writes a ready-to-run, executable shell script in the project root.
   * Ensures that `sync_to_devbox.sh` is ignored in `.gitignore`.

---

## Usage

### 1. Generate or Update `sync_to_devbox.sh`

Run the generator from your repository:

```bash
python3 .agents/skills/devbox_rsync/scripts/generate_sync.py
```

To specify custom VM host or remote directory overrides:

```bash
python3 .agents/skills/devbox_rsync/scripts/generate_sync.py \
  --vm-host my-custom-vm \
  --remote-dir ~/my-app/
```

### 2. Using `sync_to_devbox.sh`

Once generated in your project root, synchronize code to your VM at any time:

```bash
# Offline candidate inventory (0 network connections made):
./sync_to_devbox.sh --offline

# Preview local inventory & test remote differential without modifying anything:
./sync_to_devbox.sh --dry-run

# Perform synchronization:
./sync_to_devbox.sh

# Sync and delete files on the VM that were deleted locally:
./sync_to_devbox.sh --delete

# Verbose progress output:
./sync_to_devbox.sh --verbose
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
