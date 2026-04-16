#!/usr/bin/env python3
"""Read Marstek OpenAPI values directly via UDP JSON-RPC.

This script is intended for troubleshooting outside Home Assistant.
It can discover devices and poll a target device for:
- ES.GetMode
- ES.GetStatus
- PV.GetStatus
"""

from __future__ import annotations

import asyncio
import argparse
import json
import socket
import time
import sys
from typing import Any

try:
    from pymarstek import MarstekUDPClient, get_es_mode, get_pv_status
except ImportError:
    MarstekUDPClient = None  # type: ignore[assignment]
    get_es_mode = None  # type: ignore[assignment]
    get_pv_status = None  # type: ignore[assignment]


def send_udp_request(
    host: str,
    port: int,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = 4.0,
    request_id: int = 1,
    local_port: int | None = None,
) -> dict[str, Any]:
    """Send one UDP JSON-RPC request and return parsed response."""
    payload = {
        "id": request_id,
        "method": method,
        "params": params or {"id": 0},
    }
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    if local_port is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", local_port))
    try:
        sock.sendto(data, (host, port))
        response_data, src = sock.recvfrom(4096)
        response_text = response_data.decode("utf-8", errors="replace")
        response = json.loads(response_text)
        if not isinstance(response, dict):
            raise ValueError(f"Unexpected response type from {src}: {type(response)}")
        return response
    finally:
        sock.close()


def send_with_retry(
    host: str,
    port: int,
    method: str,
    params: dict[str, Any] | None,
    timeout: float,
    request_id: int,
    local_port: int | None,
    retries: int,
    delay_s: float,
    debug: bool = False,
) -> dict[str, Any]:
    """Send one command with retries and optional spacing."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        if debug:
            print(f"[DEBUG] {method} attempt {attempt}/{retries}")
        try:
            response = send_udp_request(
                host=host,
                port=port,
                method=method,
                params=params,
                timeout=timeout,
                request_id=request_id,
                local_port=local_port,
            )
            if debug:
                print(f"[DEBUG] {method} response: {json.dumps(response, ensure_ascii=True)}")
            return response
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries:
                time.sleep(delay_s)
    assert last_err is not None
    raise last_err


def discover_devices(port: int, timeout: float) -> list[dict[str, Any]]:
    """Broadcast Marstek.GetDevice and collect responses."""
    payload = {
        "id": 1,
        "method": "Marstek.GetDevice",
        "params": {"id": 0},
    }
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    sock.bind(("0.0.0.0", 0))

    devices: list[dict[str, Any]] = []
    try:
        sock.sendto(data, ("255.255.255.255", port))
        while True:
            try:
                response_data, src = sock.recvfrom(4096)
            except TimeoutError:
                break
            response_text = response_data.decode("utf-8", errors="replace")
            try:
                response = json.loads(response_text)
            except json.JSONDecodeError:
                continue
            if isinstance(response, dict):
                result = response.get("result")
                if isinstance(result, dict):
                    result["_source_ip"] = src[0]
                    devices.append(result)
    finally:
        sock.close()

    # Deduplicate by best available identifier.
    deduped: dict[str, dict[str, Any]] = {}
    for dev in devices:
        key = (
            str(dev.get("ip"))
            or str(dev.get("ble_mac"))
            or str(dev.get("wifi_mac"))
            or str(dev.get("_source_ip"))
        )
        deduped[key] = dev
    return list(deduped.values())


def print_pretty(title: str, payload: dict[str, Any]) -> None:
    """Print title and JSON payload."""
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True))


def to_kwh_from_wh(value: Any) -> float | None:
    """Convert Wh to kWh."""
    if isinstance(value, (int, float)):
        return round(float(value) / 1000.0, 3)
    return None


def to_kwh_from_tenth_wh(value: Any) -> float | None:
    """Convert 0.1 Wh units to kWh."""
    if isinstance(value, (int, float)):
        return round(float(value) / 10000.0, 3)
    return None


def print_energy_summary(es_mode: dict[str, Any], es_status: dict[str, Any]) -> None:
    """Print normalized energy values for quick validation."""
    mode_result = es_mode.get("result", {}) if isinstance(es_mode, dict) else {}
    status_result = es_status.get("result", {}) if isinstance(es_status, dict) else {}

    if not isinstance(mode_result, dict) or not isinstance(status_result, dict):
        return

    print("\n=== Energy Summary (normalized to kWh) ===")
    print(f"total_pv_energy: {to_kwh_from_wh(status_result.get('total_pv_energy'))}")
    print(
        "total_grid_output_energy: "
        f"{to_kwh_from_wh(status_result.get('total_grid_output_energy'))}"
    )
    print(
        "total_grid_input_energy: "
        f"{to_kwh_from_wh(status_result.get('total_grid_input_energy'))}"
    )
    print(f"total_load_energy: {to_kwh_from_wh(status_result.get('total_load_energy'))}")
    print(f"input_energy (*0.1 Wh): {to_kwh_from_tenth_wh(mode_result.get('input_energy'))}")
    print(
        f"output_energy (*0.1 Wh): "
        f"{to_kwh_from_tenth_wh(mode_result.get('output_energy'))}"
    )


def print_pv_plausibility(pv_status: dict[str, Any]) -> None:
    """Show PV channel values and rough power plausibility."""
    result = pv_status.get("result", {}) if isinstance(pv_status, dict) else {}
    if not isinstance(result, dict):
        return

    print("\n=== PV Plausibility (power vs V*I) ===")
    for ch in range(1, 5):
        power = result.get(f"pv{ch}_power")
        voltage = result.get(f"pv{ch}_voltage")
        current = result.get(f"pv{ch}_current")
        if all(isinstance(v, (int, float)) for v in (power, voltage, current)):
            expected = float(voltage) * float(current)
            ratio = (float(power) / expected) if expected > 0 else None
            ratio_text = f"{ratio:.2f}" if ratio is not None else "n/a"
            print(
                f"PV{ch}: power={power} W, voltage={voltage} V, "
                f"current={current} A, V*I={expected:.1f} W, ratio={ratio_text}"
            )
        else:
            print(f"PV{ch}: missing numeric values")


async def run_with_pymarstek(args: argparse.Namespace) -> int:
    """Use pymarstek client to match integration behavior."""
    if MarstekUDPClient is None or get_es_mode is None or get_pv_status is None:
        print(
            "pymarstek not installed; use --raw-socket mode or install py-marstek.",
            file=sys.stderr,
        )
        return 2

    if args.local_port != 30000:
        print(
            "Hinweis: --local-port wird nur mit --raw-socket verwendet; "
            "pymarstek nutzt den API-Port.",
            file=sys.stderr,
        )

    client = MarstekUDPClient(port=args.port)
    await client.async_setup()
    try:
        if args.discover:
            devices = await client.discover_devices(use_cache=False)
            print_pretty("Discovery Results", {"count": len(devices), "devices": devices})
            return 0

        if not args.host:
            print("Error: --host is required unless --discover is used.", file=sys.stderr)
            return 2

        es_mode = await client.send_request(
            get_es_mode(0),
            args.host,
            args.port,
            timeout=args.timeout,
        )
        es_status = await client.send_request(
            json.dumps({"id": 2, "method": "ES.GetStatus", "params": {"id": 0}}),
            args.host,
            args.port,
            timeout=args.timeout,
        )
        pv_status = await client.send_request(
            get_pv_status(0),
            args.host,
            args.port,
            timeout=args.timeout,
        )
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as err:
        print(f"Request failed: {err}", file=sys.stderr)
        return 1
    finally:
        await client.async_cleanup()

    print_pretty("ES.GetMode", es_mode)
    print_pretty("ES.GetStatus", es_status)
    print_pretty("PV.GetStatus", pv_status)
    print_energy_summary(es_mode, es_status)
    print_pv_plausibility(pv_status)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read Marstek OpenAPI values directly over UDP."
    )
    parser.add_argument("--host", help="Target device IP address")
    parser.add_argument("--port", type=int, default=30000, help="UDP port (default: 30000)")
    parser.add_argument(
        "--local-port",
        type=int,
        default=30000,
        help="Local UDP source port for --raw-socket mode (default: 30000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=4.0,
        help="Request timeout in seconds (default: 4.0)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover devices via UDP broadcast and exit.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between requests/retries in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retries per request in raw-socket mode (default: 2)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print raw request/response debug output.",
    )
    parser.add_argument(
        "--raw-socket",
        action="store_true",
        help="Use internal raw UDP client instead of pymarstek.",
    )
    args = parser.parse_args()

    if not args.raw_socket:
        return asyncio.run(run_with_pymarstek(args))

    if args.discover:
        devices = discover_devices(args.port, args.timeout)
        print_pretty("Discovery Results", {"count": len(devices), "devices": devices})
        return 0

    if not args.host:
        print("Error: --host is required unless --discover is used.", file=sys.stderr)
        return 2

    try:
        es_mode = send_with_retry(
            args.host,
            args.port,
            "ES.GetMode",
            {"id": 0},
            args.timeout,
            1,
            args.local_port,
            args.retries,
            args.delay,
            args.debug,
        )
        time.sleep(args.delay)
        es_status = send_with_retry(
            args.host,
            args.port,
            "ES.GetStatus",
            {"id": 0},
            args.timeout,
            2,
            args.local_port,
            args.retries,
            args.delay,
            args.debug,
        )
        time.sleep(args.delay)
        pv_status = send_with_retry(
            args.host,
            args.port,
            "PV.GetStatus",
            {"id": 0},
            args.timeout,
            3,
            args.local_port,
            args.retries,
            args.delay,
            args.debug,
        )
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as err:
        if args.local_port == 30000:
            print(
                "Request failed with local port 30000, retrying without fixed local port...",
                file=sys.stderr,
            )
            try:
                es_mode = send_udp_request(
                    args.host, args.port, "ES.GetMode", {"id": 0}, args.timeout, 1
                )
                es_status = send_udp_request(
                    args.host, args.port, "ES.GetStatus", {"id": 0}, args.timeout, 2
                )
                pv_status = send_udp_request(
                    args.host, args.port, "PV.GetStatus", {"id": 0}, args.timeout, 3
                )
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as retry_err:
                print(f"Request failed: {retry_err}", file=sys.stderr)
                return 1
        else:
            print(f"Request failed: {err}", file=sys.stderr)
            return 1

    print_pretty("ES.GetMode", es_mode)
    print_pretty("ES.GetStatus", es_status)
    print_pretty("PV.GetStatus", pv_status)
    print_energy_summary(es_mode, es_status)
    print_pv_plausibility(pv_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
