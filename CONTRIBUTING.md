# Contributing to DevBox

Thank you for your interest in contributing to DevBox. We welcome improvements to the hypervisor probing logic, template customization engine, and documentation runbooks.

---

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd DevBox
   ```

2. **Verify Python Environment**:
   Ensure you are using Python 3.10 or higher with standard libraries (`json`, `argparse`, `re`, `subprocess`, `pathlib`):
   ```bash
   python3 --version
   ```

---

## Running Verification Tests

DevBox includes a validation tool to verify template placeholders, schema coverage, and rendered markdown documents.

Always run the verification tool before opening a pull request:

```bash
python3 .agents/skills/devbox_configure/scripts/verify_config.py
```

### What `verify_config.py` Validates:
* Confirms that all unique `{{ PLACEHOLDER }}` tokens in `template/DEVELOPMENT_VM.md.template` are accounted for in `template/defaults.json`.
* Scans `docs/DEVELOPMENT_VM.md` to ensure zero leftover or unrendered template tags remain.

---

## Guidelines for Changes

* **Keep Scripts Dependency-Free**: Tooling inside `scripts/` must run using only the Python standard library, without requiring external package installations.
* **Maintain Portability**: Avoid hardcoding specific machine names, hostnames, user accounts, or private network IPs. Use dynamic detection or relative paths.
* **Test Both Local and Remote Probing**: When modifying `scripts/probe_hypervisor.py`, test discovery against `localhost` as well as remote SSH hosts.

---

## Submitting Pull Requests

1. Fork the repository and create your feature branch:
   ```bash
   git checkout -b feature/my-enhancement
   ```
2. Make your modifications and run `verify_config.py`.
3. Commit your changes with descriptive commit messages.
4. Open a pull request against the main branch with a clear description of your changes.
