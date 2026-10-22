# Physical Windows acceptance procedure

Use only an expendable USB stick with dummy files and two physical Windows 10/11 x64 PCs. Record OS build, connector version, driver signature, USB model and network conditions. Never test with valuable data.

1. Install signed connector and approved USB driver on A and B. Sign in to the same account. Confirm both appear online on the dashboard.
2. Insert the stick into A. Confirm vendor/product and stable device ID appear on B.
3. Click Connect on B. Confirm a new USB device in B Device Manager and a drive in File Explorer. A must lose writable access before B gets it.
4. Copy a dummy file A to B and verify SHA-256. Create and edit a dummy file on B and verify after reconnecting locally to A.
5. Safely eject/flush on B, then Disconnect. Confirm B loses the device and A regains it.
6. Attempt simultaneous connects from B and a third machine. Exactly one may own the device.
7. Repeat with cable/network loss and each machine reboot during a dummy write. Confirm no concurrent mounts; require recovery when flush/detach is uncertain.

**Current result:** Steps 1–7 are **not passed**. This repository has no signed USB driver, installer, or hardware transport. API/demo tests do not count as hardware acceptance.
