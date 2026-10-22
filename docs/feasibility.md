# Windows USB-over-IP feasibility (22 October 2026)

## Decision

**True USB attach is blocked at Phase 0.** This repository does not contain a signed virtual USB driver, host-side capture stack, or a proven end-to-end Windows-to-Windows data plane. The control plane and UI must not be described as a completed FlexiHub clone. A network file share or mapped drive would not satisfy the product requirement.

FlexiHub describes encapsulating USB traffic over TCP/IP and presenting the device through virtual USB ports on the remote machine: [FlexiHub FAQ](https://www.flexihub.com/flexihub-faq/). Microsoft's [USB Device Emulation architecture](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/usb-emulated-device--ude--architecture) confirms that Windows USB emulation uses a UDE client driver with WDF/UdeCx, not a plain web app.

## Candidate paths

| Path | Evidence | Open gate |
| --- | --- | --- |
| Build a UDE-based guest driver plus host capture service | [Microsoft UDE architecture](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/usb-emulated-device--ude--architecture) | Significant kernel engineering; prove mass-storage attach, exclusive host claim, crash recovery and transport before product work. |
| `usbip-win2` | [Project repository](https://github.com/vadimgrn/usbip-win2) calls itself a Windows USB/IP **client** and shows BSD-2-Clause licensing | Verify a compatible Windows **host** implementation, Windows 10/11 versions, signed installer, redistribution, write safety and WAN transport. Client alone does not establish this product. |
| VirtualHere | [Windows server](https://www.virtualhere.com/windows_server_software) and [Windows client](https://www.virtualhere.com/usb_client_software) claim Windows support and local-device behavior | Commercial licensing, OEM redistribution, automation API terms, integration and internet relay rights need written confirmation. Do not bundle it without rights. |

Microsoft states that kernel-mode drivers require signing on x64 Windows and Windows 10+ driver submissions use the Hardware Developer Center: [driver signing](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/driver-signing), [driver signing options](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/driver-signing-offerings). Signing and hardware testing must be planned as product work, not bypassed in production.

## Go/no-go experiment

1. Select a legally usable host and guest stack, with redistribution and automation terms in writing.
2. On two isolated physical Windows 10/11 x64 PCs, attach one disposable USB flash drive. Confirm B sees a USB device in Device Manager and a volume in Explorer.
3. Verify checksum of a dummy file read and write, A unavailable while B owns it, and safe return to A after B ejects.
4. Interrupt network and reboot each PC during a dummy write. Never permit simultaneous writable mounts; require operator recovery if safe ownership cannot be proven.
5. Repeat using signed binaries on clean Windows installs with Secure Boot enabled. Record exact supported builds and results.

Until all gates pass, the backend rejects hardware connection requests with `DRIVER_NOT_INTEGRATED`. Mock sessions are available only for explicitly marked demo devices.
