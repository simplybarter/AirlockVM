# Contributing to AirlockVM

Thank you for your interest in contributing to AirlockVM. We welcome improvements to the Python control plane (`src/airlock/`), MCP server tools, agent skills (`.agents/skills/`), test suites, and documentation.

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). For security vulnerabilities, please follow our [Security Policy](SECURITY.md) instead of opening a public issue or PR.

---

## Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/simplybarter/AirlockVM.git
   cd AirlockVM
   ```

2. **Create a Python 3.11+ virtual environment and install in editable mode:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   pip install -e .
   ```

---

## Running Verification Checks

Always run the same checks enforced by CI (`.github/workflows/ci.yml`) before opening a pull request:

1. **Deterministic unit & integration test suite:**
   ```bash
   .venv/bin/python -m unittest discover -s tests -v
   ```

2. **Runbook and inventory template validation:**
   ```bash
   .venv/bin/python .agents/skills/airlock_configure/scripts/verify_config.py
   .venv/bin/python .agents/skills/airlock_inventory/scripts/verify_config.py
   ```

3. **Portability invariant check (no hardcoded local home paths):**
   Run the same portability grep check defined in [`.github/workflows/ci.yml`](workflows/ci.yml) and confirm it returns zero matches.

---

## Coding & Portability Guidelines

* **Universal Portability:** Never hardcode absolute local home paths, private hostnames, or local developer usernames. Use relative paths, `localhost`/`remotehost`, and dynamic user resolution (`getpass.getuser()`).
* **Zero Upstream Inventory Pollution:** Never commit generated inventory records (`vmhost.yaml`, `vm.yaml`, `VM.md`) or private SSH keys. Persistent inventory lives under `~/.airlock/inventory/` (or `$AIRLOCK_INVENTORY_DIR`).
* **Minimal Dependencies:** Keep core dependencies minimal (`pyyaml`, `mcp`) and prefer the Python standard library inside skill scripts.
* **Documentation & Changelog:** Update relevant technical docs (`docs/`, `README.md`) and add notable user-facing changes under `## [Unreleased]` in [CHANGELOG.md](../CHANGELOG.md).

---

## Commit & Pull Request Process

1. Fork the repository and create a topic branch from `main`:
   ```bash
   git checkout -b feat/my-enhancement
   ```
2. Follow **Conventional Commits v1.0.0** (`<type>(<scope>): <subject>`), using types `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, or `chore`.
3. Run all verification checks above and confirm `git diff --cached` contains no secrets or local state.
4. Open a pull request against `main` using the [Pull Request template](pull_request_template.md).
