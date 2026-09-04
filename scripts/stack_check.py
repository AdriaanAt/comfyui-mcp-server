#!/usr/bin/env python3
"""Probe every service in the home platform and print one report.

Standard library only, so it runs on a bare Windows Python install with no
pip step. Point it at a host with --host, or set the per-service URLs via
environment variables, then paste the output back into a session to show
what is actually up.

    python scripts/stack_check.py
    python scripts/stack_check.py --host 192.168.1.50
    python scripts/stack_check.py --json

Exit status is 0 when every enabled check passes, 1 otherwise, so it can be
used as a service health gate or a scheduled task.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

DEFAULT_TIMEOUT = 5.0

# Talk to the LAN directly. Both Windows and Linux may have proxy variables
# set for outbound internet, and urllib would otherwise route local requests
# through them and report a false failure.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    target: str
    elapsed_ms: int = 0
    skipped: bool = False

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        return "OK" if self.ok else "FAIL"


@dataclass
class Check:
    name: str
    target: str
    run: Callable[[], tuple[bool, str]]
    required: bool = True
    skip_reason: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def _http(
    url: str,
    *,
    timeout: float,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[dict[str, str]] = None,
    insecure: bool = False,
) -> tuple[int, bytes]:
    """Fetch a URL and return (status, body). Raises on transport failure."""
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    opener = _OPENER
    if insecure and url.lower().startswith("https"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )

    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        # An HTTP error is still a reachable service; let callers judge it.
        return exc.code, exc.read(65536)


def check_http(
    url: str,
    *,
    timeout: float,
    expect: tuple[int, ...] = (200,),
    contains: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    insecure: bool = False,
) -> tuple[bool, str]:
    try:
        status, body = _http(
            url, timeout=timeout, headers=headers, insecure=insecure
        )
    except urllib.error.URLError as exc:
        return False, f"unreachable: {exc.reason}"
    except (TimeoutError, socket.timeout):
        return False, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, f"unreachable: {exc}"

    if status not in expect:
        return False, f"HTTP {status}"
    if contains and contains.encode() not in body:
        return False, f"HTTP {status} but response missing {contains!r}"
    return True, f"HTTP {status}"


def check_comfyui(url: str, timeout: float) -> tuple[bool, str]:
    """ComfyUI reports queue and device info at /system_stats."""
    endpoint = url.rstrip("/") + "/system_stats"
    try:
        status, body = _http(endpoint, timeout=timeout)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"unreachable: {reason}"

    if status != 200:
        return False, f"HTTP {status}"
    try:
        stats = json.loads(body)
    except json.JSONDecodeError:
        return True, "HTTP 200 (unrecognised body)"

    devices = stats.get("devices") or []
    if devices:
        device = devices[0]
        free = device.get("vram_free")
        total = device.get("vram_total")
        if isinstance(free, int) and isinstance(total, int) and total:
            return True, (
                f"{device.get('name', 'device')} "
                f"{free // 1048576}/{total // 1048576} MB VRAM free"
            )
        return True, str(device.get("name", "up"))
    return True, "up"


def check_mcp(url: str, timeout: float) -> tuple[bool, str]:
    """Send a JSON-RPC initialize to a streamable-HTTP MCP server."""
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stack_check", "version": "1.0"},
            },
        }
    ).encode()

    try:
        status, body = _http(
            url,
            timeout=timeout,
            method="POST",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"unreachable: {reason}"

    if status not in (200, 202):
        return False, f"HTTP {status}"

    text = body.decode("utf-8", errors="replace")
    # Streamable HTTP may answer as SSE; the JSON sits after a `data:` prefix.
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        info = (message.get("result") or {}).get("serverInfo") or {}
        if info:
            name = info.get("name", "server")
            version = info.get("version", "?")
            return True, f"{name} v{version}"
        if "result" in message:
            return True, "initialized"
    return True, f"HTTP {status} (no serverInfo in response)"


def check_mqtt(
    host: str,
    port: int,
    timeout: float,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> tuple[bool, str]:
    """Perform a real MQTT 3.1.1 CONNECT and read the CONNACK.

    An open TCP port only proves something is listening; the handshake proves
    a broker is answering, and distinguishes auth failures from being down.
    """

    def field(value: bytes) -> bytes:
        return len(value).to_bytes(2, "big") + value

    flags = 0x02  # clean session
    payload = field(b"stack_check")
    if username:
        flags |= 0x80
        payload += field(username.encode())
        if password:
            flags |= 0x40
            payload += field(password.encode())

    variable = field(b"MQTT") + bytes([0x04, flags]) + (60).to_bytes(2, "big")
    body = variable + payload

    remaining = bytearray()
    length = len(body)
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        remaining.append(byte)
        if not length:
            break

    packet = bytes([0x10]) + bytes(remaining) + body

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(packet)
            response = sock.recv(4)
    except (socket.timeout, TimeoutError):
        return False, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, f"unreachable: {exc}"

    if len(response) < 4 or response[0] != 0x20:
        return False, "no CONNACK (not an MQTT broker?)"

    codes = {
        0: "connection accepted",
        1: "refused: unacceptable protocol version",
        2: "refused: client id rejected",
        3: "refused: server unavailable",
        4: "refused: bad username or password",
        5: "refused: not authorised",
    }
    code = response[3]
    if code == 0:
        return True, codes[0]
    # The broker answered, so it is up; the credentials are the problem.
    return False, codes.get(code, f"refused: code {code}")


def check_tcp(host: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        start = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except (socket.timeout, TimeoutError):
        return False, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, f"unreachable: {exc}"
    return True, f"port open ({(time.monotonic() - start) * 1000:.0f} ms)"


def build_checks(args: argparse.Namespace) -> list[Check]:
    host = args.host
    timeout = args.timeout
    checks: list[Check] = []

    open_webui = os.getenv("OPEN_WEBUI_URL", f"http://{host}:3000")
    checks.append(
        Check(
            name="Open WebUI",
            target=open_webui,
            run=lambda: check_http(
                open_webui.rstrip("/") + "/health", timeout=timeout
            ),
        )
    )

    comfyui = os.getenv("COMFYUI_URL", f"http://{host}:8188")
    checks.append(
        Check(
            name="ComfyUI",
            target=comfyui,
            run=lambda: check_comfyui(comfyui, timeout),
        )
    )

    mcp_url = os.getenv("COMFY_MCP_URL", f"http://{host}:9000/mcp")
    checks.append(
        Check(
            name="ComfyUI MCP server",
            target=mcp_url,
            run=lambda: check_mcp(mcp_url, timeout),
        )
    )

    ha_url = os.getenv("HA_URL", f"http://{args.ha_host or host}:8123")
    ha_token = os.getenv("HA_TOKEN")
    if ha_token:
        checks.append(
            Check(
                name="Home Assistant",
                target=ha_url + "/api/",
                run=lambda: check_http(
                    ha_url.rstrip("/") + "/api/",
                    timeout=timeout,
                    contains="API running",
                    headers={"Authorization": f"Bearer {ha_token}"},
                ),
            )
        )
        mcp_ha = ha_url.rstrip("/") + "/mcp_server/sse"
        checks.append(
            Check(
                name="HA MCP server",
                target=mcp_ha,
                required=False,
                run=lambda: check_http(
                    mcp_ha,
                    timeout=timeout,
                    expect=(200, 405, 400),
                    headers={"Authorization": f"Bearer {ha_token}"},
                ),
                notes=["needs the Model Context Protocol Server integration added"],
            )
        )
    else:
        checks.append(
            Check(
                name="Home Assistant",
                target=ha_url,
                run=lambda: check_http(ha_url, timeout=timeout, expect=(200, 302)),
                notes=["set HA_TOKEN for an authenticated API check"],
            )
        )

    mqtt_host = os.getenv("MQTT_HOST", args.ha_host or host)
    mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
    checks.append(
        Check(
            name="Mosquitto (MQTT)",
            target=f"{mqtt_host}:{mqtt_port}",
            run=lambda: check_mqtt(
                mqtt_host,
                mqtt_port,
                timeout,
                os.getenv("MQTT_USERNAME"),
                os.getenv("MQTT_PASSWORD"),
            ),
        )
    )

    z2m_url = os.getenv("Z2M_URL", f"http://{args.ha_host or host}:8080")
    checks.append(
        Check(
            name="Zigbee2MQTT",
            target=z2m_url,
            required=False,
            run=lambda: check_http(z2m_url, timeout=timeout, expect=(200, 302, 401)),
            notes=["frontend must be enabled in configuration.yaml"],
        )
    )

    postgres_host = os.getenv("POSTGRES_HOST", host)
    postgres_port = int(os.getenv("POSTGRES_PORT", "5432"))
    checks.append(
        Check(
            name="PostgreSQL",
            target=f"{postgres_host}:{postgres_port}",
            required=False,
            run=lambda: check_tcp(postgres_host, postgres_port, timeout),
            notes=["phase 3 - not expected to be up yet"],
        )
    )

    paperless = os.getenv("PAPERLESS_URL", f"http://{host}:8010")
    checks.append(
        Check(
            name="Paperless-ngx",
            target=paperless,
            required=False,
            run=lambda: check_http(paperless, timeout=timeout, expect=(200, 302)),
            notes=["phase 3 - not expected to be up yet"],
        )
    )

    if args.only:
        wanted = [name.lower() for name in args.only]
        checks = [
            check
            for check in checks
            if any(want in check.name.lower() for want in wanted)
        ]
    return checks


def run_checks(checks: list[Check]) -> list[Result]:
    results: list[Result] = []
    for check in checks:
        if check.skip_reason:
            results.append(
                Result(check.name, True, check.skip_reason, check.target, skipped=True)
            )
            continue
        start = time.monotonic()
        try:
            ok, detail = check.run()
        except Exception as exc:  # a probe bug must not sink the whole report
            ok, detail = False, f"check error: {exc}"
        elapsed = int((time.monotonic() - start) * 1000)
        if not ok and check.notes:
            detail = f"{detail} ({'; '.join(check.notes)})"
        results.append(Result(check.name, ok, detail, check.target, elapsed))
    return results


def render(results: list[Result], checks: list[Check], use_colour: bool) -> str:
    required = {check.name: check.required for check in checks}
    width = max((len(r.name) for r in results), default=10)

    def paint(text: str, colour: str) -> str:
        if not use_colour:
            return text
        codes = {"green": "32", "red": "31", "yellow": "33", "dim": "2"}
        return f"\033[{codes[colour]}m{text}\033[0m"

    lines = ["", "Home platform status", "=" * 60]
    for result in results:
        if result.skipped:
            badge = paint("SKIP", "dim")
        elif result.ok:
            badge = paint(" OK ", "green")
        elif required.get(result.name, True):
            badge = paint("FAIL", "red")
        else:
            badge = paint("WARN", "yellow")
        lines.append(f"  [{badge}] {result.name.ljust(width)}  {result.detail}")
        lines.append(f"         {paint(result.target, 'dim')}")

    hard_failures = [
        r for r in results if not r.ok and required.get(r.name, True) and not r.skipped
    ]
    soft_failures = [
        r
        for r in results
        if not r.ok and not required.get(r.name, True) and not r.skipped
    ]

    lines.append("=" * 60)
    passed = sum(1 for r in results if r.ok and not r.skipped)
    total = sum(1 for r in results if not r.skipped)
    summary = f"{passed}/{total} checks passed"
    if hard_failures:
        summary += f" - {len(hard_failures)} required service(s) down"
    if soft_failures:
        summary += f", {len(soft_failures)} optional not up yet"
    lines.append("  " + summary)
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe the home platform services and print a status report."
    )
    parser.add_argument(
        "--host",
        default=os.getenv("STACK_HOST", "127.0.0.1"),
        help="host running the AI stack (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--ha-host",
        default=os.getenv("HA_HOST"),
        help="host running Home Assistant / MQTT / Zigbee2MQTT, if different",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("STACK_TIMEOUT", DEFAULT_TIMEOUT)),
        help=f"per-check timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="NAME",
        help="only run checks whose name contains one of these substrings",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead")
    parser.add_argument("--no-colour", action="store_true", help="disable colour")
    args = parser.parse_args(argv)

    checks = build_checks(args)
    results = run_checks(checks)

    if args.json:
        print(
            json.dumps(
                {
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "results": [
                        {
                            "name": r.name,
                            "status": r.status,
                            "target": r.target,
                            "detail": r.detail,
                            "elapsed_ms": r.elapsed_ms,
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    else:
        use_colour = not args.no_colour and sys.stdout.isatty()
        print(render(results, checks, use_colour))

    required = {check.name: check.required for check in checks}
    failed = [
        r for r in results if not r.ok and required.get(r.name, True) and not r.skipped
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
