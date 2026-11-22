"""Measure single-flow TCP delivery in both directions against a private test endpoint."""
import argparse
import concurrent.futures
import json
import socket
import time

PORT = 29572
CHUNK = bytes(65536)


def transmit(sock, duration):
    deadline = time.monotonic() + duration
    total = 0
    while time.monotonic() < deadline:
        sock.sendall(CHUNK)
        total += len(CHUNK)
    sock.shutdown(socket.SHUT_WR)
    return total


def receive(sock):
    buf = bytearray(len(CHUNK))
    first = None
    total = 0
    while True:
        count = sock.recv_into(buf)
        if not count:
            break
        if first is None:
            first = time.monotonic()
        total += count
    if first is None:
        raise ValueError("no payload received")
    elapsed = time.monotonic() - first
    return {"bytes": total, "seconds": round(elapsed, 3), "Mbps": round(8 * total / elapsed / 1e6, 2)}


def serve(conn):
    with conn:
        conn.settimeout(15)
        with conn.makefile("rb", buffering=0) as control:
            direction, value = control.readline(64).decode().split()
        duration = float(value)
        if not 1 <= duration <= 20:
            raise ValueError("duration outside 1..20 seconds")
        if direction == "download":
            transmit(conn, duration)
        elif direction == "upload":
            stats = receive(conn)
            conn.sendall(json.dumps(stats).encode() + b"\n")


def run_server():
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("", PORT))
        server.listen(8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            while True:
                conn, _ = server.accept()
                pool.submit(serve, conn)


def measure(target, interface, direction, duration):
    with socket.socket() as sock:
        sock.settimeout(15)
        if interface:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
        sock.connect((target, PORT))
        sock.sendall(f"{direction} {duration}\n".encode())
        if direction == "download":
            return receive(sock)
        sent = transmit(sock, duration)
        with sock.makefile("rb") as control:
            result = json.loads(control.readline(1024))
        if result["bytes"] != sent:
            raise RuntimeError("sender and receiver byte counts differ")
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="command", required=True)
    cmds.add_parser("serve")
    client = cmds.add_parser("measure")
    client.add_argument("target")
    client.add_argument("--interface")
    client.add_argument("--seconds", type=int, default=8, choices=range(1, 21))
    client.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.command == "serve":
        run_server()
    else:
        for i in range(args.runs):
            for direction in ("download", "upload"):
                stats = measure(args.target, args.interface, direction, args.seconds)
                print(json.dumps({"run": i + 1, "direction": direction, **stats}), flush=True)
