# Remote USB Hub — control-plane prototype

This is a runnable website, API, and Windows connector prototype. **It does not yet forward USB traffic or make a remote USB device appear in Windows Explorer.** See [the driver feasibility gate](docs/feasibility.md).

## Run locally

```powershell
python -m pip install -r requirements.txt
$env:REMOTE_USB_DB = "./remote_usb.db"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Create an account and use **Explore demo** to explore the UI. Demo sessions never touch USB hardware.

## Windows connector app

The site’s **Add a computer** dialog offers a downloadable `PortwayConnector.exe`. Double-click it on each Windows PC, enter the website URL and the same account credentials, then leave it open. It reports local USB mass-storage inventory and lists remote devices every 15 seconds. The **Connect selected device** action reports the real driver blocker until USB virtualization exists. The optional **Open connector when I sign in to Windows** setting adds a current-user startup entry. The sign-in token is saved with Windows DPAPI for the current Windows user; it expires after 24 hours and the app then asks for sign-in again. There is no second PowerShell window or Python installation required on the client PCs.

Build the portable executable on a Windows development machine with Python 3.9+:

```powershell
python -m pip install pyinstaller==6.16.0
powershell -ExecutionPolicy Bypass -File .\build-connector.ps1
```

The output is `dist/PortwayConnector.exe`. The API serves this exact file at `/download/connector`. This one executable is intentionally included in Git so Render receives it; commit the updated executable whenever connector code changes. A deployed site must use HTTPS with a valid certificate, and the app’s Server URL must be the deployed site origin. Plain HTTP is accepted only for localhost. This is an unsigned portable discovery app, not a signed installer, Windows service, USB driver or data-plane transport. Sign the executable before public distribution. Do not expose the development server to the public internet.

For developer debugging, the old command-line connector remains available at `connector/connector.py`, but normal users should use the GUI executable.

## What works

- Account register/login/logout using salted PBKDF2 password hashes and revocable bearer tokens.
- Connector registration, heartbeat and Windows USB mass-storage inventory through PowerShell/CIM.
- Per-account machine and device list, online/offline state and auto-refresh dashboard.
- Atomic session reservation that prevents two requesters reserving the same device; explicit demo connect/disconnect.
- Hardware Connect requests return `DRIVER_NOT_INTEGRATED` until a verified driver and data plane are supplied.

## Hardware acceptance

See [acceptance procedure](docs/acceptance.md). Real Windows USB pass-through remains untested and blocked.
