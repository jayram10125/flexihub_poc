import hashlib
import unittest
from unittest.mock import Mock, patch

from connector import native_usb as native


class NativeSafetyTests(unittest.TestCase):
    def test_external_binding_is_never_taken_over(self):
        device = {'BusId': '1-2', 'ClientIPAddress': None, 'PersistedGuid': 'another-app', 'InstanceId': 'physical'}
        intent = Mock()
        with patch.object(native, 'host_devices', return_value=[device]), patch.object(native, 'command') as execute:
            with self.assertRaises(RuntimeError):
                native.prepare_host('1-2', intent, 'hash')
            execute.assert_not_called()
            intent.assert_not_called()

    def test_replaced_disk_on_same_port_is_not_captured(self):
        device = {'BusId': '1-2', 'ClientIPAddress': None, 'PersistedGuid': None, 'InstanceId': 'parent'}
        disk = {'virtual': False, 'parents': ['parent'], 'instance': 'different-disk', 'volumes': [{'path': 'volume'}]}
        with patch.object(native, 'host_devices', return_value=[device]), patch.object(native, 'disks', return_value=[disk]), patch.object(native, 'command') as execute:
            with self.assertRaises(RuntimeError):
                native.prepare_host('1-2', Mock(), hashlib.sha256(b'original-disk').hexdigest())
            execute.assert_not_called()

    def test_busy_volume_never_reaches_driver_bind(self):
        device = {'BusId': '1-2', 'ClientIPAddress': None, 'PersistedGuid': None, 'InstanceId': 'parent'}
        disk = {'virtual': False, 'parents': ['parent'], 'instance': 'disk', 'volumes': [{'path': 'volume'}]}
        intent = Mock()
        with patch.object(native, 'host_devices', return_value=[device]), patch.object(native, 'disks', return_value=[disk]), patch.object(native, 'VolumeLocks', side_effect=RuntimeError('busy')), patch.object(native, 'command') as execute:
            with self.assertRaises(RuntimeError):
                native.prepare_host('1-2', intent, hashlib.sha256(b'disk').hexdigest())
            execute.assert_not_called()
            intent.assert_not_called()

    def test_release_cannot_unbind_replacement_device(self):
        with patch.object(native, 'host_devices', return_value=[{'BusId': '1-2', 'InstanceId': 'replacement'}]), patch.object(native, 'command') as execute:
            with self.assertRaises(RuntimeError):
                native.release_host('1-2', 'original')
            execute.assert_not_called()
