"""Explicit local physical-USB compatibility test, not a two-PC acceptance test.

Requires administrator. Captures only --busid mass storage, imports it into
this PC's virtual controller, writes/removes one uniquely named test file, then
returns the device to its original Windows driver. Never touches existing files.
Durable operation evidence is saved to native/logs/physical-loopback.json.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import uuid

from connector import native_usb as native


def test(busid):
    log = Path('native/logs/physical-loopback.json')
    data = {'busid': busid, 'captured': False, 'port': None, 'result': 'started',
            'two_physical_computers_tested': False}
    instances = []

    def save():
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(data, indent=2))

    def capture(instance):
        data.update(capture_intent=True, instance=instance)
        save()

    if not native.runtime_status()['guest_ready']:
        raise RuntimeError('Signed runtime and elevated process are required')
    save()
    try:
        physical = next(d for d in native.storage_inventory() if d['usbip_busid'] == busid)
        before = {d['instance'] for d in native.disks()}
        native.prepare_host(busid, capture, physical['serial_hash'])
        data['captured'] = True
        save()
        data['port'] = native.attach_guest(busid, 3240)
        save()
        deadline = time.monotonic() + 45
        mounted = None
        while time.monotonic() < deadline:
            candidates = [d for d in native.disks() if d['virtual'] and d['instance'] not in before]
            instances = [d['instance'] for d in candidates]
            data['guest_instances'] = instances
            save()
            if len(candidates) == 1 and any(v['letter'] and not v['readonly'] for v in candidates[0]['volumes']):
                mounted = candidates[0]
                break
            time.sleep(1)
        if not mounted:
            raise RuntimeError('Virtual USB disk did not mount')
        volume = next(v for v in mounted['volumes'] if v['letter'] and not v['readonly'])
        path = Path(volume['letter'] + ':\\') / ('Portway-USB-check-' + uuid.uuid4().hex + '.tmp')
        payload = (b'Portway physical USB read-write verification\n' * 4096)
        try:
            with path.open('xb') as stream:
                stream.write(payload)
                stream.flush()
                import os
                os.fsync(stream.fileno())
            if path.read_bytes() != payload:
                raise RuntimeError('Read-back mismatch')
            data['sha256'] = hashlib.sha256(payload).hexdigest()
            data['bytes_verified'] = len(payload)
            data['volume'] = volume['path']
        finally:
            if path.exists():
                path.unlink()
        data['result'] = 'read-write-passed'
    except Exception as error:
        data['result'] = 'failed'
        data['error'] = str(error)
    finally:
        save()
        if data['port']:
            try:
                native.detach_guest(data['port'], instances)
                data['guest_detached'] = True
                time.sleep(2)
            except Exception as error:
                data['cleanup_error'] = str(error)
        if data.get('capture_intent') and (not data['port'] or data.get('guest_detached')):
            try:
                native.release_host(busid, data['instance'])
                data['host_restored'] = True
            except Exception as error:
                data['cleanup_error'] = str(error)
        save()
    print(json.dumps(data, indent=2))
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--busid', required=True)
    parser.add_argument('--capture-and-write-test', action='store_true', required=True)
    args = parser.parse_args()
    if log_path := Path('native/logs/physical-loopback.json'):
        if log_path.exists():
            previous = json.loads(log_path.read_text())
            if previous.get('capture_intent') and not previous.get('host_restored'):
                raise SystemExit('Previous test needs ownership recovery before another test')
    test(args.busid)
