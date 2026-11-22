# Contributing to AirlockVM

Thank you for your interest in contributing to AirlockVM. We welcome improvements to the hypervisor probing logic, template customization engine, and documentation runbooks.

---

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd AirlockVM
   ```

2. **Setup Python Virtual Environment**:
   Ensure you are using Python 3.14 or higher, initialize the project virtual environment, and install in editable mode:
   ```bash
   python3.14 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

---

## Running Verification Tests

AirlockVM includes a validation tool to verify template placeholders, schema coverage, and rendered markdown documents.

Always run the verification tool before opening a pull request:

```bash
python3 .agents/skills/airlock_configure/scripts/verify_config.py
```

### What `verify_config.py` Validates:
* Confirms that all unique `{{ PLACEHOLDER }}` tokens in `template/VM.md.template` are accounted for in `airlock_inventory` templates.
* Audits rendered `VM.md` files in `inventory/` to ensure zero leftover or unrendered template tags remain.

---

## Guidelines for Changes

* **Keep Dependencies Minimal**: Tooling inside `scripts/` should rely only on the Python standard library and `PyYAML` (`pyyaml`) for inventory YAML parsing.
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
