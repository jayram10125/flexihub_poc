"""Safely finish a journaled local hardware test; never capture a new device."""
import json
from pathlib import Path
import time

from connector import native_usb as native


if __name__ == '__main__':
    path = Path('native/logs/physical-loopback.json')
    data = json.loads(path.read_text())
    try:
        if data.get('port') and not data.get('guest_detached'):
            native.detach_guest(data['port'], data.get('guest_instances', []))
            data['guest_detached'] = True
            path.write_text(json.dumps(data, indent=2))
            time.sleep(2)
        if data.get('capture_intent') and not data.get('host_restored'):
            native.release_host(data['busid'], data['instance'])
            data['host_restored'] = True
        data.pop('cleanup_error', None)
    except Exception as error:
        data['cleanup_error'] = str(error)
    path.write_text(json.dumps(data, indent=2))
    print(json.dumps(data, indent=2))
