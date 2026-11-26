# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Dedicated AirlockVM Python control plane (`airlock` CLI) and FastMCP server (`airlock-mcp`) supporting dual topologies (dedicated execution VMs and shared worker attach) and multi-IDE onboarding (`connect_airlock.sh`, `install_airlock_mcp.sh`).
- Autonomous intelligence engines for stack detection, host placement, network/port allocation, 8-gate pre-flight checks, drift auditing & reconciliation, crash diagnostics, workload profiling, security auditing, health probes, and external `qcow2` snapshot management (`create`, `revert`, `list`, `delete`, `flatten`).
- Zero-`sudo` (rootless) hypervisor support with ACL storage pools, user-level systemd socket proxies, and explicit `connection.ssh_key_path` tracking in `vmhost.yaml`.
- Isolated remote command runner (`airlock_exec`), golden Ubuntu 24.04 `qcow2` base image baking (`airlock_bake_image`) with Node.js 22 LTS and `uv` pre-installed, and cross-hypervisor golden image seeding with SHA-256 verification (`airlock_seed_image`, `airlock_add_vmhost`, `airlock seed-image`, `airlock add-host --seed-image`).
- Two-tier persistent inventory discovery (`$AIRLOCK_INVENTORY_DIR` and `~/.airlock/inventory/`) decoupled from repository clones.
- Antigravity agent skills suite (`.agents/skills/airlock_inventory`, `airlock_configure`, `airlock_deploy`, `airlock_rsync`).
- GitHub Actions CI workflow (Python 3.11–3.14 unit/integration tests, template verification, portability checks, wheel smoke tests) and Dependabot configuration for `uv` and GitHub Actions.
- GitHub community health & governance suite: `.github/SECURITY.md`, `.github/CODE_OF_CONDUCT.md`, `.github/CONTRIBUTING.md`, `.github/SUPPORT.md`, `.github/CODEOWNERS`, `.github/release.yml`, and YAML Issue Forms.

### Fixed
- Resolved SSH config `HostName` lookup in live health check probes.
- Fixed rootless golden-image baking, live DHCP lease skipping during IP allocation, and systemd proxy stop/restart behavior on VM purge and update.
- Hardened `qemu-img info` backing-chain inspection (`-U` and fail-closed error handling), dotfile rsync include patterns, and remote SSH quoting.

[Unreleased]: https://github.com/simplybarter/AirlockVM/commits/main
