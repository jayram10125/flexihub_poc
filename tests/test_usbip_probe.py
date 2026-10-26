import struct
import unittest
from unittest.mock import patch

from poc.usbip import DEVICE, HEADER, list_exported, private_host


class Stream:
    def __init__(self, data):
        self.data = data
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def sendall(self, data):
        self.sent = data

    def recv(self, size):
        # Simulate TCP fragmentation instead of assuming one recv per structure.
        size = min(size, 3)
        result, self.data = self.data[:size], self.data[size:]
        return result


class ProbeTests(unittest.TestCase):
    def test_real_protocol_layout_and_fragmentation(self):
        device = DEVICE.pack(b'/device', b'1-2', 1, 2, 3, 0x781, 0x5567,
                             0x100, 0, 0, 0, 1, 1, 1)
        stream = Stream(HEADER.pack(0x111, 5, 0) + struct.pack('!I', 1)
                        + device + bytes((8, 6, 80, 0)))
        with patch('poc.usbip.socket.create_connection', return_value=stream):
            devices = list_exported('192.168.1.2')
        self.assertEqual(stream.sent, HEADER.pack(0x111, 0x8005, 0))
        self.assertTrue(devices[0]['mass_storage'])
        self.assertEqual(devices[0]['busid'], '1-2')

    def test_public_addresses_and_names_are_rejected(self):
        for host in ('8.8.8.8', 'example.com', '0.0.0.0', '::1'):
            with self.assertRaises(ValueError):
                private_host(host)

    def test_invalid_truncated_or_oversized_reply(self):
        replies = (b'', HEADER.pack(0x111, 5, 1),
                   HEADER.pack(0x111, 5, 0) + struct.pack('!I', 257))
        for reply in replies:
            with patch('poc.usbip.socket.create_connection', return_value=Stream(reply)):
                with self.assertRaises(RuntimeError):
                    list_exported('127.0.0.1')
