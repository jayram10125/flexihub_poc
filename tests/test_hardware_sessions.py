import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient
import app.main as service


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        service.DB = Path(self.tmp.name) / 'hardware.db'
        self.client = TestClient(service.app).__enter__()
        auth = self.client.post('/api/auth/register', json={'email': 'usb@example.com', 'password': 'longpassword'}).json()
        self.headers = {'Authorization': 'Bearer ' + auth['token']}
        self.host, self.guest = 'host-computer', 'guest-computer'
        self.keys = {self.host: 'h' * 40, self.guest: 'g' * 40}
        for machine in (self.host, self.guest):
            data = {'machine': {'id': machine, 'name': machine, 'os': 'Windows 11'},
                    'machine_key': self.keys[machine], 'host_ready': True, 'guest_ready': True,
                    'devices': [{'id': 'physical-usb', 'name': 'Test disk', 'usbip_busid': '1-2'}] if machine == self.host else []}
            self.assertEqual(self.client.post('/api/machines/heartbeat', headers=self.headers, json=data).status_code, 200)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def reserve(self):
        return self.client.post('/api/sessions', headers=self.headers, json={
            'device_id': self.host + ':physical-usb', 'requester_machine_id': self.guest})

    def event(self, sid, machine, event, **extra):
        return self.client.post('/api/agent/sessions/' + sid + '/event', headers=self.headers,
                                json=dict(machine_id=machine, machine_key=self.keys[machine], event=event, **extra))

    def test_verified_mount_and_two_sided_release(self):
        reserved = self.reserve()
        self.assertEqual(reserved.status_code, 200)
        sid = reserved.json()['id']
        self.assertEqual(reserved.json()['state'], 'host-preparing')
        self.assertEqual(self.reserve().status_code, 409)
        self.assertEqual(self.event(sid, self.guest, 'mounted').status_code, 409)
        self.assertEqual(self.event(sid, self.host, 'host-ready').status_code, 200)
        self.assertEqual(self.event(sid, self.guest, 'mounted').status_code, 409)
        evidence = {'port': 1, 'pnp_id': 'USBSTOR\\TEST', 'volume': '\\\\?\\Volume{test}\\'}
        self.assertEqual(self.event(sid, self.guest, 'mounted', evidence=evidence).json()['state'], 'connected')
        self.client.post('/api/sessions/' + sid + '/disconnect', headers=self.headers)
        self.assertEqual(self.event(sid, self.host, 'host-released').status_code, 409)
        self.assertEqual(self.event(sid, self.guest, 'guest-detached').json()['state'], 'host-releasing')
        self.assertEqual(self.event(sid, self.host, 'host-released').json()['state'], 'released')
        self.assertEqual(self.reserve().status_code, 200)

    def test_failure_retains_ownership_until_recovered(self):
        sid = self.reserve().json()['id']
        self.event(sid, self.host, 'failed', error='Network failed')
        self.assertEqual(self.reserve().status_code, 409)
        self.assertEqual(self.event(sid, self.host, 'host-released').status_code, 409)
        self.event(sid, self.guest, 'guest-detached')
        self.event(sid, self.host, 'host-released')
        self.assertEqual(self.reserve().status_code, 200)

    def test_ephemeral_render_storage_and_multiple_workers_refuse_hardware(self):
        with patch.dict('os.environ', {'RENDER': 'true', 'REMOTE_USB_DB': ''}):
            self.assertEqual(self.reserve().json()['detail']['code'], 'PERSISTENT_STORAGE_REQUIRED')
        with patch.dict('os.environ', {'WEB_CONCURRENCY': '2'}):
            self.assertEqual(self.reserve().json()['detail']['code'], 'RELAY_DEPLOYMENT')

    def test_relay_authentication_and_binary_forwarding(self):
        sid = self.reserve().json()['id']
        def headers(machine):
            return {'Authorization': 'Bearer ' + self.keys[machine], 'X-Portway-Machine': machine}
        with self.client.websocket_connect('/api/relay/' + sid + '/host', headers=headers(self.host)) as host:
            with self.client.websocket_connect('/api/relay/' + sid + '/guest', headers=headers(self.guest)) as guest:
                guest.send_bytes(b'actual-usbip-test-frame')
                self.assertEqual(host.receive_bytes(), b'actual-usbip-test-frame')
                host.send_bytes(b'reply')
                self.assertEqual(guest.receive_bytes(), b'reply')
        poll = self.client.post('/api/agent/poll', headers=self.headers,
                               json={'machine_id': self.host, 'machine_key': 'invalid-key' * 4})
        self.assertEqual(poll.status_code, 403)

    def test_alias_of_same_bus_cannot_be_reserved_twice(self):
        self.reserve()
        with service.db() as c:
            original = c.execute('SELECT * FROM devices WHERE id=?', (self.host + ':physical-usb',)).fetchone()
            c.execute('''INSERT INTO devices(id,machine_id,name,kind,eligible,last_seen,usbip_busid)
                         VALUES(?,?,?,?,?,?,?)''', ('alias-device', self.host, 'Alias', 'mass-storage', 1,
                                                  original['last_seen'], '1-2'))
        reply = self.client.post('/api/sessions', headers=self.headers,
                                 json={'device_id': 'alias-device', 'requester_machine_id': self.guest})
        self.assertEqual(reply.status_code, 409)
