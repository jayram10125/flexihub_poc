"""Explicit physical USB + two logical agents + real Portway relay test.

Runs entirely on one Windows PC. It does NOT substitute for two-PC/WAN tests.
Uses an isolated local backend database and one unique temporary USB file.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

from connector.agent import Agent
from connector.connector import request
from connector import native_usb as native


def run(busid):
    root = Path('native/logs') / ('agent-test-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    report = {'two_physical_computers_tested': False, 'result': 'starting'}
    report_path = root / 'result.json'

    def save():
        report_path.write_text(json.dumps(report, indent=2))

    runtime = native.runtime_status()
    if not runtime['guest_ready']:
        raise RuntimeError(runtime['error'])
    storage = next(d for d in native.storage_inventory() if d['usbip_busid'] == busid)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    server = 'http://127.0.0.1:' + str(port)
    env = dict(os.environ, REMOTE_USB_DB=str((root / 'backend.db').resolve()))
    output = (root / 'backend.log').open('w')
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                                '--port', str(port), '--workers', '1'], env=env, stdout=output, stderr=output,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    agents = []
    sid = None
    token = None
    released = False
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(server + '/api/capabilities', timeout=2).close()
                break
            except OSError:
                time.sleep(0.25)
        token = request(server, '/api/auth/register',
                        {'email': uuid.uuid4().hex + '@portway.test', 'password': secrets.token_urlsafe(24)})['token']
        host_id, guest_id = 'test-host-' + uuid.uuid4().hex, 'test-guest-' + uuid.uuid4().hex
        bodies = []
        for role, machine in (('host', host_id), ('guest', guest_id)):
            key = secrets.token_urlsafe(32)
            bodies.append({'machine': {'id': machine, 'name': 'Loopback ' + role, 'os': 'Windows'},
                           'machine_key': key, 'host_ready': True, 'guest_ready': True,
                           'devices': [storage] if role == 'host' else []})
            request(server, '/api/machines/heartbeat', bodies[-1], token)
            agents.append(Agent(root / role, server, token, machine, key))
        sid = request(server, '/api/sessions', {'device_id': host_id + ':' + storage['id'],
                                               'requester_machine_id': guest_id}, token)['id']
        report['session_id'] = sid
        deadline = time.monotonic() + 220
        disconnect_requested = False
        previous = None
        while time.monotonic() < deadline:
            for body in bodies:
                request(server, '/api/machines/heartbeat', body, token)
            active = []
            for agent in agents:
                active = agent.sync()
            session = next((s for s in active if s['id'] == sid), None)
            if session is None:
                released = True
                report['released'] = True
                break
            state = session['state']
            if state != previous:
                report.setdefault('states', []).append(state)
                previous = state
                report['session_error'] = session['error']
                save()
            if state == 'connected' and not disconnect_requested:
                guest_journal = agents[1].read(sid)
                disk = next(d for d in native.disks() if d['instance'] in guest_journal['instances'])
                volume = next(v for v in disk['volumes'] if v['letter'] and not v['readonly'])
                path = Path(volume['letter'] + ':\\') / ('Portway-relay-check-' + uuid.uuid4().hex + '.tmp')
                payload = secrets.token_bytes(262144)
                try:
                    with path.open('xb') as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if path.read_bytes() != payload:
                        raise RuntimeError('USB data read-back mismatch through relay')
                    report['bytes_verified'] = len(payload)
                    report['result'] = 'read-write-passed'
                finally:
                    if path.exists():
                        path.unlink()
                request(server, '/api/sessions/' + sid + '/disconnect', {}, token)
                disconnect_requested = True
            elif state == 'recovery-required' and not disconnect_requested:
                report['result'] = 'failed'
                disconnect_requested = True
            time.sleep(1)
        if not released:
            raise RuntimeError('Safe release was not confirmed before test timeout; inspect agent journals')
    except Exception as error:
        report.update(result='failed', error=str(error))
    finally:
        for agent in agents:
            agent.close()
        if released or not sid:
            process.terminate()
            process.wait(timeout=10)
        else:
            # Preserve the backend lease/journals on abnormal failure.
            report['backend_pid_recovery_required'] = process.pid
        output.close()
        save()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--busid', required=True)
    parser.add_argument('--capture-and-write-test', action='store_true', required=True)
    run(parser.parse_args().busid)
