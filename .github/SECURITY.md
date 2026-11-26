# Security Policy

## Supported Versions

Security fixes are applied to the latest minor release on the `main` branch:

| Version | Supported |
| :--- | :--- |
| `0.3.x` (`main`) | :white_check_mark: |
| `< 0.3.0` | :x: |

---

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub Issues, Discussions, or Pull Requests.**

Instead, please submit a report privately via **GitHub Private Vulnerability Reporting**:

1. Open the repository's **Security** tab and click **Report a vulnerability**, or go directly to:  
   https://github.com/simplybarter/AirlockVM/security/advisories/new
2. Include as much detail as possible to help reproduce and assess the issue:
   - Affected component (e.g., `airlock-mcp`, CLI, `airlock_deploy`, `airlock_rsync`, hypervisor proxy)
   - Hypervisor privilege mode (`rootless` or `sudo`) and host OS / libvirt version
   - Clear reproduction steps or proof-of-concept
   - Potential impact and any suggested remediation

### What to Expect

- **Acknowledgment**: Within **3 business days** of submission.
- **Triage & Updates**: We will confirm reproducibility, assess severity, and coordinate a fix privately within the GitHub Security Advisory.
- **Disclosure**: Once a patch is released, we will publish the advisory and credit the reporter (unless anonymity is requested).

---

## Security Architecture & Hardening

For AirlockVM's 5-layer isolation model, zero-`sudo` rootless hypervisor setup, `systemd-socket-proxyd` Layer 4 behavior, and firewall/VPN hardening patterns, see [docs/SECURITY.md](../docs/SECURITY.md).
