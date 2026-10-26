# Portway-owned Windows USB-over-IP implementation

## Product rule

Customers install only a signed `PortwayConnectorSetup.exe`. The installer may contain Portway's own UI, service, driver package and transport libraries. It must not depend on users downloading another USB-sharing product or manually configuring a second app.

## What already works

The existing API is the control plane: account authentication, connector presence, discovery and device ownership metadata. The connector discovers Plug-and-Play devices. It intentionally has no path that claims a disk was mounted, because a normal Python process cannot create a USB device in Windows Explorer.

## Open-source assessment

`usbip-win2` is a BSD-2-Clause Windows USB/IP **client**, with a UDE-based virtual-device driver. It does not provide the complete signed Windows host capture, relay, installer and mass-storage safety system Portway needs. The older `usbip-win` stub workflow documents test-signing paths, which is unsuitable for Secure-Boot production Windows. They can be studied as protocol/driver references, but neither may be represented as a finished Portway data plane.

References: [usbip-win2](https://github.com/vadimgrn/usbip-win2), [usbip-win](https://github.com/cezanne/usbip-win), [Microsoft USB Device Emulation](https://learn.microsoft.com/windows-hardware/drivers/usbcon/usb-emulated-device--ude--architecture).

## Portway-owned architecture

```text
Portway UI (per user)
        |
Portway Agent Service (LocalSystem)
        |-- control-plane session/lease API (existing backend)
        |-- mTLS transport and reconnection logic
        |-- host safe-share coordinator
        |-- guest attach coordinator
        |
PortwayUde.sys (KMDF + UdeCx, Microsoft-signed)
        |
Windows PnP -> disk class driver -> mount manager -> File Explorer
```

The host agent must capture and forward USB request/response traffic from the physical device. The guest driver must emulate a USB host controller/device using KMDF/UdeCx and deliver those requests over the authenticated transport. The backend authorizes and leases sessions only; it must never proxy bulk USB traffic through the current Render web process.

## Required safety behavior for a pen drive

1. The backend grants a single, expiring lease for a device and two agents acknowledge it.
2. Computer A flushes and locks the volume. If any process prevents a safe lock, sharing is refused.
3. A's agent marks the device unavailable locally before exposing it to B.
4. B reports success only after Windows PnP and Mount Manager report the new disk/volume.
5. On disconnect, B ejects/detaches and waits for write completion; A reclaims the device only after the host driver reports no client owns it.
6. A crash, B crash or network loss enters `recovery-required`. No computer gets a writable mount until driver callbacks establish exclusive ownership.

## Signing and installation

The installer contains an INF, CAT and `PortwayUde.sys`, and verifies their publisher/signature before installation. Kernel drivers for modern x64 Windows must be signed through Microsoft Hardware Dev Center; production work needs an EV certificate, a Partner Center account and Windows hardware/driver validation. Secure Boot and Windows driver-signing protections must remain enabled.

References: [Microsoft kernel-mode signing](https://learn.microsoft.com/windows-hardware/drivers/install/kernel-mode-code-signing-requirements--windows-vista-and-later-), [driver package components](https://learn.microsoft.com/windows-hardware/drivers/install/components-of-a-driver-package).

## Delivery phases

1. Define a Windows 10 22H2 / Windows 11 x64 support matrix and build a disposable-flash-drive test lab.
2. Build the signed Portway Agent service, per-device lease protocol and host safe-share state machine.
3. Develop `PortwayUde.sys` plus a loopback-only USB request test harness; pass PnP, suspend/resume and reboot tests.
4. Add mTLS data transport, NAT traversal/relay infrastructure controlled by Portway, and replay/authorization protection.
5. Package every component in the Portway installer and validate install, update, rollback and uninstall on clean Secure-Boot Windows machines.
6. Test actual pen-drive read/write/checksum, local-host exclusion, abrupt disconnect and recovery on two physical PCs.

No hardware `Connect` endpoint may report success until phase 6 has passed.
