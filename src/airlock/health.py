"""AirlockVM Post-Deploy Health Verification & Diagnostic Engine.

Probes hypervisor proxy routes to confirm application availability and automatically
captures runtime crash logs from the guest VM if deployment fails.
"""

from __future__ import annotations

import http.client
import time
from typing import Any
import urllib.parse

from airlock.transport import run_guest_cmd


def verify_route_health(
    host: str,
    port: int,
    path: str = "/",
    retries: int = 4,
    delay_seconds: float = 1.0,
    vm_name: str | None = None,
) -> dict[str, Any]:
    """Probes an HTTP route and diagnoses crashes if unreachable."""
    url = f"http://{host}:{port}{path}"
    last_error = ""

    for attempt in range(1, retries + 1):
        try:
            start_time = time.time()
            conn = http.client.HTTPConnection(host, port, timeout=3)
            conn.request("GET", path)
            res = conn.getresponse()
            latency_ms = round((time.time() - start_time) * 1000, 2)
            conn.close()

            # Any valid HTTP response indicates the server is up and listening
            return {
                "healthy": True,
                "url": url,
                "status_code": res.status,
                "reason": res.reason,
                "latency_ms": latency_ms,
                "attempt": attempt,
                "message": f"Application is healthy and responding (HTTP {res.status}) on {url}",
            }
        except Exception as e:
            last_error = str(e)
            if attempt < retries:
                time.sleep(delay_seconds)

    # Health check failed -> Capture runtime diagnostics
    diagnostics = []
    if vm_name:
        try:
            # Query recent systemd/journal logs or dmesg inside the VM
            _, stdout, _ = run_guest_cmd(
                vm_name,
                "journalctl -n 25 --no-pager 2>/dev/null || dmesg | tail -n 25",
                timeout=5,
            )
            if stdout:
                diagnostics.append(stdout)
        except Exception:
            pass

    return {
        "healthy": False,
        "url": url,
        "error": f"Failed to connect after {retries} attempts: {last_error}",
        "diagnostics": "\n".join(diagnostics) if diagnostics else "No remote logs captured (VM may be powered off or unreachable).",
        "message": f"Application failed health check on {url}. Connection error: {last_error}",
    }
