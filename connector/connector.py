"""Foreground Windows inventory connector; no USB forwarding or disk writes."""
import argparse
import getpass
import hashlib
import json
import platform
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def inventory():
    if platform.system() != "Windows":
        return []
    script = "Get-CimInstance Win32_DiskDrive | Where-Object {$_.InterfaceType -eq 'USB'} | Select-Object Model,PNPDeviceID,DeviceID | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "USB inventory failed")
    parsed = json.loads(result.stdout or "null")
    disks = parsed if isinstance(parsed, list) else ([parsed] if parsed else [])
    output = []
    for disk in disks:
        pnp = disk.get("PNPDeviceID") or disk.get("DeviceID") or ""
        if not pnp:
            continue
        identity = hashlib.sha256(pnp.encode()).hexdigest()
        import re
        vid = re.search(r"VID_([0-9A-Fa-f]{4})", pnp)
        pid = re.search(r"PID_([0-9A-Fa-f]{4})", pnp)
        output.append({"id": "usb-" + identity[:32], "name": disk.get("Model") or "USB mass storage", "vid": vid.group(1) if vid else None, "pid": pid.group(1) if pid else None, "serial_hash": identity, "kind": "mass-storage", "eligible": True})
    return output


def request(server, path, body, token=None):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(server + path, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description="Remote USB Hub Windows inventory connector")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    url = args.server.rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
        parser.error("Use HTTPS for remote servers; HTTP is allowed only on localhost")
    if platform.system() != "Windows":
        parser.error("This connector currently supports Windows only")
    config = Path.home() / ".remote_usb_hub_installation_id"
    if config.exists():
        machine_id = config.read_text().strip()
    else:
        machine_id = str(uuid.uuid4())
        config.write_text(machine_id)
    email = input("Account email: ")
    password = getpass.getpass("Password: ")
    token = request(url, "/api/auth/login", {"email": email, "password": password})["token"]
    del password
    print("Signed in. Reporting USB mass storage every 15 seconds. Ctrl+C to stop.")
    machine = {"id": machine_id, "name": platform.node() or "Windows PC", "os": platform.platform()[:100]}
    try:
        while True:
            try:
                devices = inventory()
                request(url, "/api/machines/heartbeat", {"machine": machine, "devices": devices}, token)
                print("Online; reported {} USB storage device(s).".format(len(devices)))
            except (urllib.error.URLError, RuntimeError, ValueError) as error:
                print("Connector error: {}".format(error))
            time.sleep(15)
    except KeyboardInterrupt:
        print("Stopped. Dashboard will show offline after 45 seconds.")


if __name__ == "__main__":
    main()
