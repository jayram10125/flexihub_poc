"""Resolve the SQLite file and check Render's actual persistent disk mount.

Setting an environment variable to an ephemeral path does not make a lease
durable. Local development keeps its existing repository database location.
"""
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def data_directory():
    return Path(os.environ.get('PORTWAY_DATA_DIR') or '/var/data').resolve()


def database_path(root):
    explicit = os.environ.get('REMOTE_USB_DB', '').strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    directory = data_directory()
    if os.environ.get('RENDER') and os.path.ismount(directory):
        return directory / 'portway.db'
    return Path(root) / 'remote_usb.db'


def hardware_storage_error(database):
    if not os.environ.get('RENDER'):
        return None
    directory = data_directory()
    if not os.path.ismount(directory):
        return ('Server storage is not configured. In Render Dashboard, attach a persistent disk '
                'at /var/data, set REMOTE_USB_DB=/var/data/portway.db and WEB_CONCURRENCY=1, '
                'then redeploy. This is a server setting; reinstalling either connector will not fix it.')
    try:
        Path(database).resolve().relative_to(directory)
    except ValueError:
        return ('The server database is outside its persistent disk. Set '
                'REMOTE_USB_DB=/var/data/portway.db (or a path inside PORTWAY_DATA_DIR), then redeploy.')
    return None


def preserve_existing_database(root, database):
    """Copy a surviving legacy DB to a new persistent path, never overwrite it.

    This cannot recover an ephemeral DB already removed by a Render redeploy.
    Operators must back up that DB before attaching the disk or redeploying.
    """
    source = Path(root) / 'remote_usb.db'
    target = Path(database)
    if (not os.environ.get('RENDER') or hardware_storage_error(target) or
            target.exists() or not source.is_file() or source.resolve() == target.resolve()):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.portway-migration-', suffix='.db', dir=target.parent)
    os.close(handle)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(temporary)) as copied:
                original.backup(copied)
                if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Database migration integrity verification failed')
        # Atomic, exclusive publication: an existing destination is never replaced.
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
    finally:
        Path(temporary).unlink(missing_ok=True)
