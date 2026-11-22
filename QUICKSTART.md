# AirlockVM Quickstart Guide

A 5-minute walkthrough for new developers and AI coding agents getting started with **AirlockVM**.

---

## 1. What Problem Does AirlockVM Solve?

When working with modern AI coding agents (Antigravity, Cursor, Claude Code, VS Code Copilot):
* They run commands, install third-party dependencies, spin up servers, and execute test suites **directly on your personal computer**.
* If an agent runs buggy code, triggers an infinite loop, or tests untrusted packages, it can freeze your machine, pollute your local environment, or leak sensitive local credentials.

**AirlockVM solves this with an "airlock" isolation boundary:**
* You keep writing code in your preferred IDE on your development workstation.
* All runtime workloads (web servers, database containers, heavy evaluation tests) execute inside an **isolated Linux KVM virtual machine**.
* AirlockVM automatically proxies internal ports through hypervisor sockets—allowing you to open `http://localhost:3000` (or `http://remotehost:3002`) in your browser as usual, with **zero runtime workload and zero security risk** on your actual workstation.

---

## 2. Step-by-Step Quickstart

### Step 1: Install the Control Plane (1 Minute)

Clone the repository and run the automated user tool installer:

```bash
git clone https://github.com/simplybarter/AirlockVM.git
cd AirlockVM

# Install the airlock CLI and airlock-mcp server into ~/.local/bin/:
./install_airlock_mcp.sh
```

Verify that the control plane is online:

```bash
airlock ping
```

*Expected Output:*
```text
AirlockVM MCP Server is online (Python 3.14.x | ...)
```

---

### Step 2: Connect Your AI Agent & IDE (30 Seconds)

You do not need to edit complex JSON files manually. AirlockVM includes a multi-IDE client connector that wires up your AI agent in one command:

```bash
# Connect the current client project workspace:
./connect_airlock.sh

# OR connect a specific client project:
./connect_airlock.sh --target ../my-web-app

# OR connect globally across all projects for your user:
./connect_airlock.sh --global
```

*Supported Environments:*
* **Google Antigravity** (`.gemini/mcp_config.json`)
* **Cursor IDE** (`.cursor/mcp.json`)
* **VS Code** (`.vscode/mcp.json`)
* **Claude Desktop & Claude Code CLI** (`.mcp.json`)
* **Codeium Windsurf** (`.codeium/windsurf/mcp_config.json`)
* **JetBrains IDEs** (`.idea/mcp.json`)

---

### Step 3: Let Your Agent Isolate Your Application

Open your client application workspace in your IDE and prompt your AI coding agent:

> *"Analyze this project and set up a dedicated AirlockVM execution environment for it."*

The agent will autonomously use the `airlock-mcp` tools to:
1. **Detect Tech Stack & Ports**: Inspects `package.json`, `pyproject.toml`, `requirements.txt`, or `Dockerfile`.
2. **Select Hypervisor**: Probes available local or remote hypervisors for available vCPU and RAM headroom.
3. **Allocate Conflict-Free Networking**: Reserves a static IP on `virbr0` and an external host proxy port.
4. **Generate Runbook & SSOT**: Creates declarative `vm.yaml` and operational runbook `VM.md` in persistent inventory (`~/.airlock/inventory/` or via `$AIRLOCK_INVENTORY_DIR`).
5. **Sync & Launch**: Rsyncs application code (excluding `.git/` and local secrets) into the VM and starts the service.

---

### Step 4: Access Your Application

Once provisioned, access your live application via the hypervisor proxy URL:

```bash
# Example verification:
curl http://localhost:3001/
# or
curl http://remotehost:3002/
```

* **Live Health Check**: Run `airlock status <vm-name>` to inspect live domain state.
* **Safe Checkpoints**: Take a snapshot before risky refactoring via `airlock snapshot <vm-name> create baseline`, and revert anytime with `airlock snapshot <vm-name> revert baseline`.

---

## 3. Reference Test Application

To explore a working example, inspect the standalone reference project:
* **[DummyAirlockApp](https://github.com/simplybarter/DummyAirlockApp)**: A decoupled fullstack test fixture demonstrating automated stack detection, `.env` propagation, proxy forwarding, and health monitoring under AirlockVM.

---

## 4. Next Steps & Architecture

* Read **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** to understand the two-tier isolation model and resource sizing.
* Read **[docs/SECURITY.md](docs/SECURITY.md)** for proxy authentication mechanics, LAN exposure, and firewall hardening patterns.
* Read **[docs/MCP_SERVER.md](docs/MCP_SERVER.md)** for detailed specifications of all 21 Model Context Protocol tools.
* Read **[docs/INTEGRATION_TESTING.md](docs/INTEGRATION_TESTING.md)** to review the end-to-end 26-permutation verification suite.
