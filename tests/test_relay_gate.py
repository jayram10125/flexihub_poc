import struct
import unittest

from connector.relay import ImportGate, relay_url


class RelayGateTests(unittest.TestCase):
    def test_import_header_can_be_fragmented(self):
        gate = ImportGate('1-2')
        header = struct.pack('!HHI32s', 0x111, 0x8003, 0, b'1-2')
        self.assertEqual(gate.accept(header[:12]), b'')
        self.assertEqual(gate.accept(header[12:]), header)
        self.assertEqual(gate.accept(b'urb'), b'urb')

    def test_other_devices_and_devlist_cannot_use_session(self):
        for operation, busid in ((0x8003, b'1-3'), (0x8005, b'1-2')):
            with self.assertRaises(RuntimeError):
                ImportGate('1-2').accept(struct.pack('!HHI32s', 0x111, operation, 0, busid))

    def test_insecure_remote_websocket_is_refused(self):
        with self.assertRaises(ValueError):
            relay_url('http://example.com', 'session', 'guest')
        self.assertEqual(relay_url('https://example.com', 'sid', 'host'),
                         'wss://example.com/api/relay/sid/host')
