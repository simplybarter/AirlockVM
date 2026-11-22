# Dedicated dummy-airlock-app Execution Virtual Machine (`dummy-airlock-app-vm`)

> **Infrastructure & Developer Execution Environment Runbook**  
> *Defines the topology, isolation boundary, hardware specification, networking, and operation of the dedicated dummy-airlock-app VM hosted on hypervisor `remotehost`.*

---

## 1. System Architecture & Topology

1. **Development Host (`localhost`)**: Exclusively designated for software engineering.
2. **Virtualization Hypervisor (`remotehost`)**: Dedicated physical server running KVM.
3. **Dedicated dummy-airlock-app Guest VM (`dummy-airlock-app-vm`) on `remotehost`**: IP: `192.168.122.230`, Proxy Port: `3002`.

---

## 2. Hardware & Virtual Machine Specifications

| Parameter | Specification | Details |
| :--- | :--- | :--- |
| **VM Name** | `dummy-airlock-app-vm` | Registered domain in libvirt on `remotehost` |
| **Target Hypervisor** | `remotehost` | Managed via KVM / libvirt / QEMU |
| **Virtual CPUs** | 4 Cores | Proportional core sizing |
| **Memory (RAM)** | 4096 MiB | Dedicated memory allocation |
| **Disk Storage** | 50 GiB (`qcow2`) | Sparse virtual disk image |
| **Static IP Address** | `192.168.122.230` | Static DHCP allocation on `virbr0` |
| **External Proxy Port** | `3002` | Mapped via `systemd-socket-proxyd` |
