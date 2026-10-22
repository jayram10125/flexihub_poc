"""Foreground Windows inventory connector; no USB forwarding or disk writes."""
import argparse
import getpass
import hashlib
import json
import platform
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def inventory():
    """Return shareable storage and visible USB/Bluetooth PnP peripherals.

    This does not claim every discovered class can be forwarded. The connector
    sends the complete discovery list so users can see the same categories a
    USB-over-IP app normally shows, while only mass storage is eligible for
    the future USB forwarding path.
    """
    if platform.system() != "Windows":
        return []
    script = r"""
    Get-CimInstance Win32_PnPEntity |
      Where-Object {
        $_.PNPDeviceID -like 'USB*' -or
        $_.PNPDeviceID -like 'BTH*' -or
        $_.PNPDeviceID -match '^(USB|BTH|BTHENUM)\\' -or
        $_.PNPClass -eq 'Bluetooth'
      } |
      Select-Object Name,PNPDeviceID,PNPClass,Service,Status,Manufacturer |
      ConvertTo-Json -Compress
    """
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=creation_flags,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "USB inventory failed")
    parsed = json.loads(result.stdout or "null")
    devices = parsed if isinstance(parsed, list) else ([parsed] if parsed else [])
    output = []
    seen = set()
    for device in devices:
        pnp = device.get("PNPDeviceID") or ""
        if not pnp:
            continue
        normalized = pnp.upper()
        # Windows exposes some temporary composite nodes with no actual name.
        if normalized in seen or not device.get("Name"):
            continue
        seen.add(normalized)
        identity = hashlib.sha256(pnp.encode()).hexdigest()
        vid = re.search(r"VID_([0-9A-Fa-f]{4})", pnp)
        pid = re.search(r"PID_([0-9A-Fa-f]{4})", pnp)
        pnp_class = (device.get("PNPClass") or "").lower()
        display_name = device.get("Name").strip()
        if pnp_class == "diskdrive" or "mass storage" in display_name.lower() or "usb disk" in display_name.lower():
            kind, eligible = "mass-storage", True
        elif pnp_class == "bluetooth" or normalized.startswith(("BTH\\", "BTHENUM\\")):
            kind, eligible = "wireless-controller", False
        elif pnp_class == "net":
            kind, eligible = "network-adapter", False
        elif "hub" in display_name.lower():
            kind, eligible = "usb-hub", False
        else:
            kind, eligible = "usb-device", False
        output.append({
            "id": "pnp-" + identity[:32],
            "name": display_name,
            "vid": vid.group(1) if vid else None,
            "pid": pid.group(1) if pid else None,
            "serial_hash": identity,
            "kind": kind,
            "eligible": eligible,
        })
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
    print("Signed in. Reporting local USB and Bluetooth peripherals every 15 seconds. Ctrl+C to stop.")
    machine = {"id": machine_id, "name": platform.node() or "Windows PC", "os": platform.platform()[:100]}
    try:
        while True:
            try:
                devices = inventory()
                request(url, "/api/machines/heartbeat", {"machine": machine, "devices": devices}, token)
                print("Online; reported {} peripheral(s).".format(len(devices)))
            except (urllib.error.URLError, RuntimeError, ValueError) as error:
                print("Connector error: {}".format(error))
            time.sleep(15)
    except KeyboardInterrupt:
        print("Stopped. Dashboard will show offline after 45 seconds.")


if __name__ == "__main__":
    main()
