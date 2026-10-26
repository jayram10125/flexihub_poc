import os
from pathlib import Path
import tempfile
import sqlite3
from contextlib import closing
import unittest
from unittest.mock import patch

from app.storage import database_path, hardware_storage_error, preserve_existing_database


class StorageTests(unittest.TestCase):
    def test_existing_data_is_copied_once_without_overwriting_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'disk'
            with closing(sqlite3.connect(root / 'remote_usb.db')) as original:
                original.execute('CREATE TABLE preserved(value TEXT)')
                original.execute("INSERT INTO preserved VALUES('existing-account')")
                original.commit()
            env = {'RENDER': 'true', 'PORTWAY_DATA_DIR': str(directory)}
            target = directory / 'portway.db'
            with patch.dict(os.environ, env, clear=True), patch('app.storage.os.path.ismount', return_value=True):
                preserve_existing_database(root, target)
                with closing(sqlite3.connect(target)) as copied:
                    self.assertEqual(copied.execute('SELECT value FROM preserved').fetchone()[0], 'existing-account')
                    copied.execute("UPDATE preserved SET value='new-data'")
                    copied.commit()
                preserve_existing_database(root, target)
                with closing(sqlite3.connect(target)) as copied:
                    self.assertEqual(copied.execute('SELECT value FROM preserved').fetchone()[0], 'new-data')

    def test_local_database_location_is_unchanged(self):
        with patch.dict(os.environ, {}, clear=True):
            root = Path(tempfile.gettempdir())
            self.assertEqual(database_path(root), root / 'remote_usb.db')
            self.assertIsNone(hardware_storage_error(root / 'remote_usb.db'))

    def test_render_mount_is_detected_without_database_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            env = {'RENDER': 'true', 'PORTWAY_DATA_DIR': str(directory)}
            with patch.dict(os.environ, env, clear=True), patch('app.storage.os.path.ismount', return_value=True):
                self.assertEqual(database_path(Path(tmp) / 'app'), directory / 'portway.db')
                self.assertIsNone(hardware_storage_error(directory / 'portway.db'))

    def test_env_var_alone_does_not_make_storage_persistent(self):
        with patch.dict(os.environ, {'RENDER': 'true', 'REMOTE_USB_DB': '/tmp/pretend.db'}, clear=True), patch('app.storage.os.path.ismount', return_value=False):
            self.assertIsNotNone(hardware_storage_error(database_path(Path.cwd())))

    def test_database_outside_disk_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'disk'
            env = {'RENDER': 'true', 'PORTWAY_DATA_DIR': str(directory)}
            with patch.dict(os.environ, env, clear=True), patch('app.storage.os.path.ismount', return_value=True):
                self.assertIsNotNone(hardware_storage_error(Path(tmp) / 'elsewhere.db'))
                self.assertIsNone(hardware_storage_error(directory / 'portway.db'))
