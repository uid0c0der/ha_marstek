#!/usr/bin/env python3
"""Capture and extract Marstek local JSON-RPC traffic via tcpdump.

Usage example:
  sudo python3 scripts/marstek_rpc_sniffer.py --device-ip 10.1.1.26 --iface en0
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from collections import Counter
from datetime import datetime


JSON_OBJECT_RE = re.compile(r"\{[^{}]*\"method\"[^{}]*\}")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Sniff Marstek UDP traffic and extract JSON-RPC methods."
    )
    parser.add_argument("--device-ip", required=True, help="Marstek device IP")
    parser.add_argument("--iface", default="en0", help="Network interface (default: en0)")
    parser.add_argument("--port", type=int, default=30000, help="UDP port (default: 30000)")
    parser.add_argument(
        "--packets",
        type=int,
        default=200,
        help="Packet count limit for tcpdump (default: 200)",
    )
    parser.add_argument(
        "--save-raw",
        help="Optional path to save raw tcpdump output.",
    )
    return parser.parse_args()


def extract_json_objects(line: str) -> list[dict]:
    """Extract JSON objects that look like JSON-RPC payloads from a tcpdump line."""
    matches = JSON_OBJECT_RE.findall(line)
    payloads: list[dict] = []
    for match in matches:
        try:
            obj = json.loads(match)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "method" in obj:
            payloads.append(obj)
    return payloads


def main() -> int:
    """Run tcpdump and print extracted Marstek methods/payloads."""
    args = parse_args()

    bpf = f"udp and host {args.device_ip} and port {args.port}"
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

    print("Starting Marstek RPC sniff...")
    print(f"Device: {args.device_ip}:{args.port}")
    print(f"Interface: {args.iface}")
    print("Toggle settings in Marstek app now (e.g. 800/2200W).")
    print()
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
        print("Error: tcpdump not found in PATH.", file=sys.stderr)
        return 1

    methods = Counter()
    extracted = 0
    raw_lines: list[str] = []

    assert proc.stdout is not None
    for line in proc.stdout:
        raw_lines.append(line)
        payloads = extract_json_objects(line)
        for payload in payloads:
            method = str(payload.get("method", "unknown"))
            methods[method] += 1
            extracted += 1
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] {method}: {json.dumps(payload, ensure_ascii=True)}")

    return_code = proc.wait()

    print("\n--- Summary ---")
    print(f"tcpdump exit code: {return_code}")
    print(f"Extracted JSON-RPC payloads: {extracted}")
    if methods:
        for method, count in methods.most_common():
            print(f"- {method}: {count}")
    else:
        print("- No JSON-RPC payloads extracted.")
        print("  Tip: run with sudo and verify interface/IP.")

    if args.save_raw:
        with open(args.save_raw, "w", encoding="utf-8") as f:
            f.writelines(raw_lines)
        print(f"Raw output saved to: {args.save_raw}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
