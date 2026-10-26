"""USB/IP 1.1.1 probe and native Windows guest experiment.

Uses the real USB/IP wire protocol for enumeration. Attach/detach is delegated
to usbip-win2's native client and installed signed driver, never emulated here.
This LAN-only engineering POC is deliberately separate from the web API.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess


HEADER = struct.Struct('!HHI')
DEVICE = struct.Struct('!256s32sIIIHHHBBBBBB')


def receive(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError('USB/IP host closed an incomplete response')
        data.extend(chunk)
    return bytes(data)


def private_host(host):
    # No DNS rebinding or accidental exposure of raw USB/IP over public WAN.
    address = ipaddress.ip_address(host)
    ranges = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8')
    if not any(address in ipaddress.ip_network(net) for net in ranges):
        raise ValueError('POC requires a literal private IPv4 LAN address')
    return str(address)


def list_exported(host):
    host = private_host(host)
    with socket.create_connection((host, 3240), timeout=8) as sock:
        sock.sendall(HEADER.pack(0x0111, 0x8005, 0))  # OP_REQ_DEVLIST
        version, operation, status = HEADER.unpack(receive(sock, HEADER.size))
        if (version, operation, status) != (0x0111, 0x0005, 0):
            raise RuntimeError('Incompatible USB/IP DEVLIST response')
        count, = struct.unpack('!I', receive(sock, 4))
        if count > 256:
            raise RuntimeError('Host returned excessive device count')
        devices = []
        for _ in range(count):
            fields = DEVICE.unpack(receive(sock, DEVICE.size))
            interfaces = [tuple(receive(sock, 4)[:3]) for _ in range(fields[-1])]
            devices.append({
                'host': host,
                'busid': fields[1].split(b'\0')[0].decode('ascii', errors='strict'),
                'vid': '{:04x}'.format(fields[5]),
                'pid': '{:04x}'.format(fields[6]),
                'interfaces': interfaces,
                'mass_storage': fields[8] == 8 or any(i[0] == 8 for i in interfaces),
            })
        return devices


def run_native(executable, expected_sha256, arguments):
    """Run an exact, explicitly selected client; never search PATH or use shell."""
    if os.name != 'nt':
        raise RuntimeError('Native USB attach is Windows only')
    binary = Path(executable).resolve(strict=True)
    if binary.suffix.lower() != '.exe':
        raise ValueError('Expected a native .exe client')
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if not re.fullmatch('[0-9a-fA-F]{64}', expected_sha256) or digest != expected_sha256.lower():
        raise ValueError('Native client SHA-256 mismatch')
    # Executable verification does not certify its installed kernel drivers.
    # Driver packages must separately pass Windows kernel-policy verification.
    script = ('if ((Get-AuthenticodeSignature -LiteralPath $env:PORTWAY_POC_BINARY).Status '
              '-ne "Valid") { exit 1 }')
    env = dict(os.environ, PORTWAY_POC_BINARY=str(binary))
    checked = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        env=env, capture_output=True, timeout=15,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if checked.returncode:
        raise RuntimeError('Native client Authenticode signature is not valid')
    result = subprocess.run(
        [str(binary)] + arguments, capture_output=True, text=True, timeout=40,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Native client failed')
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('list', 'attach', 'detach'))
    parser.add_argument('--host')
    parser.add_argument('--busid')
    parser.add_argument('--port', type=int)
    parser.add_argument('--client', help='Absolute path to verified usbip-win2 usbip.exe')
    parser.add_argument('--sha256', help='Expected SHA-256 of that exact native client')
    parser.add_argument('--isolated-lab', action='store_true',
                        help='Acknowledge USB/IP is unencrypted and this is an isolated test LAN')
    args = parser.parse_args()
    try:
        if not args.isolated_lab:
            raise ValueError('Use only in an isolated test LAN; pass --isolated-lab')
        if args.action == 'list':
            print(json.dumps(list_exported(args.host), indent=2))
            return
        if not args.client or not args.sha256:
            raise ValueError('--client and --sha256 are required')
        if args.action == 'attach':
            host = private_host(args.host)
            if not args.busid or not re.fullmatch(r'[0-9]+-[0-9]+(?:\.[0-9]+)*', args.busid):
                raise ValueError('Invalid USB/IP busid')
            devices = list_exported(host)
            device = next((d for d in devices if d['busid'] == args.busid), None)
            if not device or not device['mass_storage']:
                raise ValueError('Selected exported device is not USB mass storage')
            output = run_native(args.client, args.sha256, ['attach', '-r', host, '-b', args.busid, '--once'])
            print(json.dumps({'state': 'attach-command-completed-unverified', 'native_output': output,
                              'next': 'Verify PnP disk arrival and File Explorer volume before any success claim'}))
        else:
            if args.port is None or not 1 <= args.port <= 65535:
                raise ValueError('A specific positive --port is required; detach-all is prohibited')
            output = run_native(args.client, args.sha256, ['detach', '-p', str(args.port)])
            print(json.dumps({'state': 'detach-command-completed-unverified', 'native_output': output}))
    except (ValueError, TypeError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
