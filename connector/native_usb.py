"""Windows USB/IP runtime adapter. All disk mutations require a durable journal.

Only physical USB mass storage is exported. Internal disks, hubs, input devices,
network adapters and Bluetooth enumeration entries are never captured here.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
HOST = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'usbipd-win/usbipd.exe'
GUEST = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'USBip/usbip.exe'


def command(argv, timeout=30):
    result = subprocess.run([str(v) for v in argv], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout, creationflags=FLAGS)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'Native command failed').strip()[:500])
    return result.stdout.strip()


def powershell(script):
    return command(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                    '-Command', "$ErrorActionPreference='Stop'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + script])


DISKS_SCRIPT = r'''
$result = @(Get-CimInstance Win32_DiskDrive | Where-Object { $_.InterfaceType -eq 'USB' -or $_.PNPDeviceID -like 'USBSTOR\*' } | ForEach-Object {
    $disk = $_
    $parents = @($disk.PNPDeviceID)
    $virtual = $false
    $node = $disk.PNPDeviceID
    for ($i=0; $i -lt 12; $i++) {
        $ids = (Get-PnpDeviceProperty -InstanceId $node -KeyName 'DEVPKEY_Device_HardwareIds' -ErrorAction SilentlyContinue).Data
        if ($ids -contains 'ROOT\USBIP_WIN2\UDE') { $virtual = $true }
        $node = (Get-PnpDeviceProperty -InstanceId $node -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue).Data
        if (-not $node) { break }
        $parents += $node
    }
    $storageDisk = Get-Disk -Number $disk.Index -ErrorAction Stop
    if ($storageDisk.IsBoot -or $storageDisk.IsSystem) { throw 'Refusing system disk' }
    $volumes = @(Get-Partition -DiskNumber $disk.Index -ErrorAction Stop | Get-Volume -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.FileSystemType -ne 'Unknown' } |
        ForEach-Object { [pscustomobject]@{path=$_.Path;letter=[string]$_.DriveLetter} })
    [pscustomobject]@{instance=$disk.PNPDeviceID;name=$disk.Model;parents=$parents;virtual=$virtual;volumes=$volumes}
})
ConvertTo-Json -InputObject $result -Depth 5 -Compress
'''


def disks():
    result = json.loads(powershell(DISKS_SCRIPT) or '[]')
    for disk in result:
        for volume in disk['volumes']:
            volume['readonly'] = volume_readonly(volume['path'])
    return result


def volume_readonly(path):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                            ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    flags = wintypes.DWORD()
    if not kernel.GetVolumeInformationW(path, None, 0, None, None, ctypes.byref(flags), None, 0):
        return True  # Not yet ready: never advertise write access speculatively.
    return bool(flags.value & 0x80000)


def host_devices():
    return json.loads(command([HOST, 'state']))['Devices']


def runtime_status():
    result = {'host_ready': False, 'guest_ready': False, 'error': ''}
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError('Restart Portway as administrator to share/attach USB storage.')
        script = r'''
$files = @((Join-Path $env:ProgramFiles 'usbipd-win\usbipd.exe'), (Join-Path $env:ProgramFiles 'USBip\usbip.exe'))
foreach ($file in $files) {
    if ((Get-AuthenticodeSignature -LiteralPath $file).Status -ne 'Valid') { throw 'Untrusted USB runtime executable' }
}
$guest = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.HardwareID -contains 'ROOT\USBIP_WIN2\UDE' -and $_.Status -eq 'OK' })
if (-not $guest) { throw 'Signed virtual USB controller is not ready; install runtime or restart Windows' }
$signed = @(Get-CimInstance Win32_PnPSignedDriver | Where-Object { $guest.PNPDeviceID -contains $_.DeviceID })
if (-not $signed -or @($signed | Where-Object { -not $_.IsSigned -or $_.Signer -notlike '*Microsoft Windows Hardware Compatibility Publisher*' }).Count) { throw 'Microsoft-signed USB controller not found' }
if ((Get-Service usbipd).Status -ne 'Running') { throw 'USB host service is not running' }
if (@(Get-NetFirewallRule -Name usbipd -ErrorAction SilentlyContinue | Where-Object {$_.Enabled -eq 'True'}).Count) { throw 'Raw USB/IP firewall rule must be disabled by Portway installer' }
'ready'
'''
        powershell(script)
        result.update(host_ready=True, guest_ready=True)
    except Exception as error:
        result['error'] = str(error)
    return result


def storage_inventory():
    hosts = host_devices()
    output = []
    for disk in disks():
        if disk['virtual']:
            continue  # Never re-export an imported disk and create a USB loop.
        parent_ids = {s.upper() for s in disk['parents']}
        host = next((d for d in hosts if d['InstanceId'].upper() in parent_ids), None)
        if not host:
            continue
        identity = hashlib.sha256(disk['instance'].encode()).hexdigest()
        vidpid = re.search(r'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})', host['InstanceId'], re.I)
        output.append({'id': 'pnp-' + identity[:32], 'name': disk['name'],
                       'kind': 'mass-storage', 'eligible': any(not v['readonly'] for v in disk['volumes']),
                       'usbip_busid': host['BusId'], 'serial_hash': identity,
                       'vid': vidpid[1] if vidpid else None, 'pid': vidpid[2] if vidpid else None})
    return output


class VolumeLocks:
    """Retain writable-volume locks through native capture/eject.

    Lock failure (open file, system volume, busy antivirus) aborts the operation.
    No forced eject, filesystem traversal or user-file writes are performed.
    """
    def __init__(self, volumes):
        self.paths = [v['path'] for v in volumes]
        self.handles = []

    def __enter__(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                                ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
        self.kernel = kernel
        try:
            for path in self.paths:
                if not re.fullmatch(r'\\\\\?\\Volume\{[0-9a-fA-F-]+\}\\', path):
                    raise RuntimeError('Unrecognized Windows volume path')
                volume_flags = wintypes.DWORD()
                if not kernel.GetVolumeInformationW(path, None, 0, None, None, ctypes.byref(volume_flags), None, 0):
                    raise ctypes.WinError(ctypes.get_last_error())
                readonly = bool(volume_flags.value & 0x80000)  # FILE_READ_ONLY_VOLUME
                # A read-only EFI volume has no writable cache to flush. Its
                # filesystem may reject LOCK_VOLUME even for administrators.
                # Leave it read-only; Windows PnP still vetoes host replacement
                # if outstanding readers prevent safe removal. Writable data
                # volumes must all lock, flush and dismount successfully.
                if readonly:
                    continue
                handle = kernel.CreateFileW(path.rstrip('\\'), 0xC0000000, 3, None, 3, 0, None)
                if handle == wintypes.HANDLE(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles.append(handle)
                returned = wintypes.DWORD()
                if not kernel.DeviceIoControl(handle, 0x90018, None, 0, None, 0, ctypes.byref(returned), None):
                    code = ctypes.get_last_error()
                    raise RuntimeError('USB volume lock failed (Windows error {}, read-only={}). Close files and Explorer windows on this drive, then retry.'.format(code, readonly))
                if not kernel.FlushFileBuffers(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not kernel.DeviceIoControl(handle, 0x90020, None, 0, None, 0, ctypes.byref(returned), None):
                    raise ctypes.WinError(ctypes.get_last_error())
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for handle in self.handles:
            self.kernel.CloseHandle(handle)
        self.handles.clear()


def prepare_host(busid, capture_intent, expected_hash):
    host = next((d for d in host_devices() if d['BusId'] == busid), None)
    if not host or host['ClientIPAddress'] or host['PersistedGuid']:
        raise RuntimeError('Device is missing or already managed outside Portway. Release it first.')
    matches = [d for d in disks() if not d['virtual'] and
               host['InstanceId'].upper() in {p.upper() for p in d['parents']}]
    if len(matches) != 1 or not matches[0]['volumes']:
        raise RuntimeError('Only a uniquely identified, formatted USB pen drive can be shared.')
    if hashlib.sha256(matches[0]['instance'].encode()).hexdigest() != expected_hash:
        raise RuntimeError('A different USB disk now occupies this port. Refresh and select the intended device.')
    with VolumeLocks(matches[0]['volumes']):
        capture_intent(host['InstanceId'])
    # Release OUR volume handles before PnP changes the function driver. Keeping
    # them open vetoes driver replacement with PNP_VetoOutstandingOpen. If any
    # new opener races this handoff, Windows refuses removal; never force eject.
    command([HOST, 'bind', '--busid', busid, '--force'])
    exported = next((d for d in host_devices() if d['BusId'] == busid), None)
    if not exported or not exported['IsForced']:
        raise RuntimeError('Host did not confirm exclusive USB capture.')
    if any(not d['virtual'] and d['instance'] == matches[0]['instance'] for d in disks()):
        raise RuntimeError('Windows still exposes the local disk. Close host drive windows and retry; USB capture was not exclusive.')


def attach_guest(busid, tcp_port):
    output = command([GUEST, '-t', str(tcp_port), 'attach', '-r', '127.0.0.1',
                      '-b', busid, '--once', '--terse'], timeout=45)
    if not re.fullmatch(r'\d{1,3}', output) or not 1 <= int(output) <= 255:
        raise RuntimeError('Native client did not return a verified USB port: ' + output[:150])
    return int(output)


def detach_guest(port, disk_instances):
    all_virtual = [d for d in disks() if d['virtual']]
    virtual = [d for d in all_virtual if d['instance'] in disk_instances]
    if not disk_instances and all_virtual:
        raise RuntimeError('Guest disk identity was not journaled. Inspect imported USB ports before recovery.')
    volumes = [v for d in virtual for v in d['volumes']]
    try:
        command([GUEST, 'port', str(port)])
    except RuntimeError as error:
        if 'requested port(s) not found' in str(error) and not virtual:
            return  # Driver already removed this port after a network failure.
        raise
    with VolumeLocks(volumes):
        command([GUEST, 'detach', '-p', str(port)])


def release_host(busid, expected_instance):
    host = next((d for d in host_devices() if d['BusId'] == busid), None)
    if host and host['InstanceId'] != expected_instance:
        raise RuntimeError('A different USB device now occupies this port. Recovery needs inspection.')
    if host and host['ClientIPAddress']:
        raise RuntimeError('Host still reports a USB client; local ownership remains locked.')
    if host and host['PersistedGuid']:
        command([HOST, 'unbind', '--busid', busid])
    host = next((d for d in host_devices() if d['BusId'] == busid), None)
    if host and (host['ClientIPAddress'] or host['PersistedGuid'] or host['IsForced']):
        raise RuntimeError('Host USB ownership has not returned safely.')
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if any(not d['virtual'] and expected_instance.upper() in {p.upper() for p in d['parents']} for d in disks()):
            return
        time.sleep(1)  # Normal PnP return can take a few seconds after unbind.
    raise RuntimeError('The original local disk has not returned yet. Reconnect it after guest detach, then retry recovery.')
