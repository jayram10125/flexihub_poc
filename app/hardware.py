"""Hardware session authorization and bounded, authenticated USB/IP relay.

The initial relay must run with ONE ASGI worker/replica. The SQLite lease is
durable; relay sockets are not. Lost sockets retain the lease for recovery.
WSS encrypts each leg to Portway; this is not end-to-end encryption from the
relay operator. Agents never expose local port 3240 on the public network.
"""
import asyncio
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

from fastapi import Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field


def fail(code, message, status=409):
    raise HTTPException(status, detail={'code': code, 'message': message})


def init(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS machine_runtime(
        machine_id TEXT PRIMARY KEY REFERENCES machines(id),
        key_hash TEXT NOT NULL, host_ready INTEGER NOT NULL,
        guest_ready INTEGER NOT NULL, updated_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS hardware_sessions(
        id TEXT PRIMARY KEY REFERENCES sessions(id), host_id TEXT NOT NULL,
        busid TEXT NOT NULL, updated_at INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '');
    ''')
    if 'usbip_busid' not in {r[1] for r in c.execute('PRAGMA table_info(devices)')}:
        c.execute('ALTER TABLE devices ADD COLUMN usbip_busid TEXT')


def runtime_heartbeat(c, body):
    runtime = c.execute('SELECT * FROM machine_runtime WHERE machine_id=?', (body.machine.id,)).fetchone()
    if runtime and (not body.machine_key or not hmac.compare_digest(
            runtime['key_hash'], hashlib.sha256(body.machine_key.encode()).hexdigest())):
        fail('MACHINE_KEY_REQUIRED', 'This computer must sign in with its own saved machine identity.', 403)
    if body.machine_key:
        c.execute('''INSERT INTO machine_runtime VALUES(?,?,?,?,?) ON CONFLICT(machine_id)
                     DO UPDATE SET host_ready=excluded.host_ready,guest_ready=excluded.guest_ready,
                     updated_at=excluded.updated_at''',
                  (body.machine.id, hashlib.sha256(body.machine_key.encode()).hexdigest(),
                   int(body.host_ready), int(body.guest_ready), int(time.time())))


def reserve(c, device, requester, user):
    if os.environ.get('RENDER') and not os.environ.get('REMOTE_USB_DB'):
        fail('PERSISTENT_STORAGE_REQUIRED', 'Configure REMOTE_USB_DB on a persistent Render disk before sharing real USB devices. Website and discovery remain available.')
    if os.environ.get('WEB_CONCURRENCY', '1') != '1':
        fail('RELAY_DEPLOYMENT', 'This Portway relay requires WEB_CONCURRENCY=1 and a single worker/replica.')
    now = int(time.time())
    for machine, capability in ((device['machine_id'], 'host_ready'), (requester['id'], 'guest_ready')):
        runtime = c.execute('SELECT * FROM machine_runtime WHERE machine_id=?', (machine,)).fetchone()
        if not runtime or not runtime[capability] or now - runtime['updated_at'] > 45:
            fail('CONNECTOR_UPDATE_REQUIRED', 'Install and open the current Portway installer on both computers. Signed USB runtime and administrator access are required.')
    busid = device['usbip_busid']
    if now - device['last_seen'] >= 45:
        fail('DEVICE_OFFLINE', 'The selected USB device is no longer present. Refresh the host inventory.')
    if not busid:
        fail('DEVICE_NOT_EXPORTABLE', 'The host has not mapped this device to a physical USB/IP port. Reconnect the pen drive and refresh.')
    # Serialize physical-device aliases (USBSTOR disk node and parent USB node).
    if c.execute('''SELECT 1 FROM hardware_sessions h JOIN sessions s ON s.id=h.id
                    WHERE h.host_id=? AND h.busid=? AND s.ended_at IS NULL''',
                 (device['machine_id'], busid)).fetchone():
        fail('DEVICE_BUSY', 'This physical USB device is already reserved.')
    sid = secrets.token_hex(16)
    c.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,NULL)',
              (sid, user['id'], device['id'], requester['id'], 'host-preparing', now))
    c.execute('INSERT INTO hardware_sessions(id,host_id,busid,updated_at) VALUES(?,?,?,?)',
              (sid, device['machine_id'], busid, now))
    return {'id': sid, 'state': 'host-preparing'}


class AgentPoll(BaseModel):
    machine_id: str
    machine_key: str = Field(min_length=32, max_length=128)


class AgentEvent(AgentPoll):
    event: str
    evidence: dict = Field(default_factory=dict)
    error: str = Field(default='', max_length=500)


def install(app, db, current_user):
    sockets = {}

    def identify(c, machine_id, key):
        row = c.execute('SELECT * FROM machine_runtime WHERE machine_id=?', (machine_id,)).fetchone()
        if not row or not hmac.compare_digest(row['key_hash'], hashlib.sha256(key.encode()).hexdigest()):
            fail('AGENT_IDENTITY', 'Computer identity not authorized.', 403)

    @app.post('/api/agent/poll')
    def poll(body: AgentPoll, user=Depends(current_user)):
        with db() as c:
            identify(c, body.machine_id, body.machine_key)
            rows = c.execute('''SELECT s.*,h.host_id,h.busid,h.error FROM sessions s
                   JOIN hardware_sessions h ON h.id=s.id WHERE s.user_id=? AND s.ended_at IS NULL
                   AND (h.host_id=? OR s.requester_id=?)''', (user['id'], body.machine_id, body.machine_id)).fetchall()
            now = int(time.time())
            for row in rows:
                if row['state'] in ('host-preparing', 'host-ready') and now - row['created_at'] > 90:
                    c.execute("UPDATE sessions SET state='recovery-required' WHERE id=?", (row['id'],))
            return {'sessions': [dict(row) for row in c.execute('''SELECT s.*,h.host_id,h.busid,h.error,d.serial_hash
                   FROM sessions s JOIN hardware_sessions h ON h.id=s.id JOIN devices d ON d.id=s.device_id WHERE s.user_id=?
                   AND s.ended_at IS NULL AND (h.host_id=? OR s.requester_id=?)''',
                   (user['id'], body.machine_id, body.machine_id))]}

    @app.post('/api/agent/sessions/{sid}/event')
    def event(sid: str, body: AgentEvent, user=Depends(current_user)):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            identify(c, body.machine_id, body.machine_key)
            row = c.execute('''SELECT s.*,h.host_id FROM sessions s JOIN hardware_sessions h ON h.id=s.id
                            WHERE s.id=? AND s.user_id=? AND s.ended_at IS NULL''', (sid, user['id'])).fetchone()
            if not row or body.machine_id not in (row['host_id'], row['requester_id']):
                fail('SESSION_NOT_FOUND', 'Hardware session not found.', 404)
            host = body.machine_id == row['host_id']
            state = row['state']
            if body.event == 'failed':
                target = 'recovery-required'
            elif host and body.event == 'host-ready' and state == 'host-preparing':
                target = 'host-ready'
            elif not host and body.event == 'mounted' and state == 'host-ready':
                if not body.evidence.get('port') or not body.evidence.get('volume') or not body.evidence.get('pnp_id'):
                    fail('MOUNT_NOT_VERIFIED', 'Windows PnP, USB port and mounted volume evidence are required.')
                target = 'connected'
            elif not host and body.event == 'guest-detached' and state in ('disconnecting', 'recovery-required'):
                target = 'host-releasing'
            elif host and body.event == 'host-released' and state == 'host-releasing':
                target = 'released'
            else:
                fail('INVALID_TRANSITION', 'Out-of-order USB lifecycle event.')
            c.execute('UPDATE sessions SET state=?,ended_at=? WHERE id=?',
                      (target, int(time.time()) if target == 'released' else None, sid))
            c.execute('UPDATE hardware_sessions SET updated_at=?,error=? WHERE id=?',
                      (int(time.time()), body.error, sid))
            return {'id': sid, 'state': target}

    @app.websocket('/api/relay/{sid}/{role}')
    async def relay(ws: WebSocket, sid: str, role: str):
        authorization = ws.headers.get('authorization', '')
        key = authorization[7:] if authorization.startswith('Bearer ') else ''
        machine = ws.headers.get('x-portway-machine', '')
        try:
            if role not in ('host', 'guest'):
                raise ValueError('role')
            with db() as c:
                identify(c, machine, key)
                row = c.execute('''SELECT s.*,h.host_id FROM sessions s JOIN hardware_sessions h ON h.id=s.id
                                WHERE s.id=? AND s.ended_at IS NULL''', (sid,)).fetchone()
                expected = row['host_id' if role == 'host' else 'requester_id'] if row else None
                if machine != expected or row['state'] not in ('host-preparing', 'host-ready', 'connected'):
                    raise ValueError('session')
        except (HTTPException, ValueError):
            await ws.close(code=1008)
            return
        slot = sockets.setdefault(sid, {})
        if role in slot:
            await ws.close(code=1008)
            return
        await ws.accept()
        own = {'socket': ws, 'ready': asyncio.Event()}
        slot[role] = own
        own['ready'].set()
        peer_role = 'guest' if role == 'host' else 'host'
        try:
            deadline = asyncio.get_running_loop().time() + 35
            while peer_role not in slot:
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError('Peer did not arrive')
                await asyncio.sleep(0.05)
            peer = slot[peer_role]['socket']
            while True:
                data = await ws.receive_bytes()
                if not data or len(data) > 65536:
                    raise ValueError('Invalid USB stream frame')
                await peer.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError, OSError, ValueError, TimeoutError):
            pass
        finally:
            slot.pop(role, None)
            other = slot.get(peer_role)
            if other:
                try:
                    await other['socket'].close(code=1011)
                except RuntimeError:
                    pass
            if not slot:
                sockets.pop(sid, None)
            with db() as c:
                c.execute("UPDATE sessions SET state='recovery-required' WHERE id=? AND state IN ('host-preparing','host-ready','connected') AND ended_at IS NULL", (sid,))

    return sockets
