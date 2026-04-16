#!/usr/bin/env python3
"""Sniff Marstek JSON-RPC traffic and extract method calls.

This tool wraps tcpdump and parses ASCII payload output to recover JSON-RPC
objects even when they are split across multiple lines.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Hit:
    """Single extracted JSON-RPC payload."""

    method: str
    direction: str
    payload: dict
    timestamp: str


def parse_args() -> argparse.Namespace:
    """Parse CLI args."""
    parser = argparse.ArgumentParser(
        description="Sniff and extract Marstek JSON-RPC calls from tcpdump output."
    )
    parser.add_argument(
        "--iface",
        default="en0",
        help="Network interface for tcpdump (default: en0)",
    )
    parser.add_argument(
        "--packets",
        type=int,
        default=400,
        help="Stop after N packets (default: 400)",
    )
    parser.add_argument(
        "--device-ip",
        help="Target Marstek device IP (recommended, e.g. 10.1.1.26)",
    )
    parser.add_argument(
        "--peer-ip",
        help="Optional peer IP (e.g. phone or HA host) for narrow capture",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=30000,
        help="UDP port filter when --udp-only is set (default: 30000)",
    )
    parser.add_argument(
        "--udp-only",
        action="store_true",
        help="Limit capture to UDP traffic (default captures all protocols).",
    )
    parser.add_argument(
        "--bpf",
        help="Custom tcpdump BPF filter; overrides generated filter.",
    )
    parser.add_argument(
        "--show-non-set",
        action="store_true",
        help="Print non-Set methods too (default prints all, highlights Set).",
    )
    parser.add_argument(
        "--save-raw",
        help="Optional path to save raw tcpdump stdout.",
    )
    return parser.parse_args()


def build_bpf(args: argparse.Namespace) -> str:
    """Build tcpdump BPF expression from args."""
    if args.bpf:
        return args.bpf

    parts: list[str] = []
    if args.udp_only:
        parts.append("udp")
        if args.port:
            parts.append(f"port {args.port}")

    if args.device_ip and args.peer_ip:
        parts.append(f"host {args.device_ip} and host {args.peer_ip}")
    elif args.device_ip:
        parts.append(f"host {args.device_ip}")
    elif args.peer_ip:
        parts.append(f"host {args.peer_ip}")

    if not parts:
        return "ip"
    return " and ".join(parts)


def extract_json_objects(text: str) -> list[dict]:
    """Extract JSON objects from mixed text using brace matching."""
    results: list[dict] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for idx, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start : idx + 1]
                    try:
                        obj = json.loads(candidate)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and "method" in obj:
                        results.append(obj)
                    start = -1
    return results


def infer_direction(line: str, device_ip: str | None) -> str:
    """Infer packet direction from tcpdump header line."""
    if " > " not in line:
        return "unknown"
    left, right = line.split(" > ", 1)
    if not device_ip:
        return "unknown"
    if device_ip in left:
        return "device->peer"
    if device_ip in right:
        return "peer->device"
    return "unknown"


def main() -> int:
    """Run tcpdump and parse JSON-RPC payloads."""
    args = parse_args()
    bpf = build_bpf(args)

    cmd = [
        "tcpdump",
        "-l",
        "-n",
        "-A",
        "-i",
        args.iface,
        bpf,
        "-c",
        str(args.packets),
    ]

    print("Starting Marstek RPC sniffer...")
    print(f"Interface: {args.iface}")
    print(f"BPF: {bpf}")
    print("Now trigger actions in app/UI (e.g. 800/2200 toggle).")
    print("Running:", " ".join(shlex.quote(part) for part in cmd))
    print()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("Error: tcpdump not found.", file=sys.stderr)
        return 1

    current_header = ""
    payload_buffer = ""
    raw_lines: list[str] = []
    hits: list[Hit] = []
    method_counter: Counter[str] = Counter()
    set_counter: Counter[str] = Counter()

    assert proc.stdout is not None
    for line in proc.stdout:
        raw_lines.append(line)
        stripped = line.rstrip("\n")

        # Packet header lines start with a timestamp-like prefix.
        # Example: 21:45:43.172309 IP 10.1.1.44.30000 > 10.1.1.26.30000: UDP, length 53
        if stripped and stripped[0:2].isdigit() and " IP " in stripped:
            if payload_buffer:
                objs = extract_json_objects(payload_buffer)
                for obj in objs:
                    method = str(obj.get("method", "unknown"))
                    direction = infer_direction(current_header, args.device_ip)
                    ts = datetime.now().strftime("%H:%M:%S")
                    hit = Hit(method=method, direction=direction, payload=obj, timestamp=ts)
                    hits.append(hit)
                    method_counter[method] += 1
                    if ".Set" in method or method.endswith("SetMode"):
                        set_counter[method] += 1
                    if args.show_non_set or ".Set" in method or method.endswith("SetMode"):
                        print(
                            f"[{ts}] {direction} {method}: "
                            f"{json.dumps(obj, ensure_ascii=True)}"
                        )
            current_header = stripped
            payload_buffer = ""
            continue

        payload_buffer += stripped

    # Flush last buffer
    if payload_buffer:
        objs = extract_json_objects(payload_buffer)
        for obj in objs:
            method = str(obj.get("method", "unknown"))
            direction = infer_direction(current_header, args.device_ip)
            ts = datetime.now().strftime("%H:%M:%S")
            hit = Hit(method=method, direction=direction, payload=obj, timestamp=ts)
            hits.append(hit)
            method_counter[method] += 1
            if ".Set" in method or method.endswith("SetMode"):
                set_counter[method] += 1
            if args.show_non_set or ".Set" in method or method.endswith("SetMode"):
                print(f"[{ts}] {direction} {method}: {json.dumps(obj, ensure_ascii=True)}")

    return_code = proc.wait()
    print("\n--- Summary ---")
    print(f"tcpdump exit code: {return_code}")
    print(f"Extracted method payloads: {len(hits)}")
    if method_counter:
        print("Methods seen:")
        for method, count in method_counter.most_common():
            print(f"- {method}: {count}")
    else:
        print("- No JSON-RPC methods extracted.")

    if set_counter:
        print("Set-like methods seen:")
        for method, count in set_counter.most_common():
            print(f"- {method}: {count}")
    else:
        print("Set-like methods seen: none")

    if args.save_raw:
        with open(args.save_raw, "w", encoding="utf-8") as file:
            file.writelines(raw_lines)
        print(f"Raw output saved: {args.save_raw}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
