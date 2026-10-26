"""Crash-aware USB lifecycle worker used by the desktop connector.

The journal is written before capture/attach. A failed/closed relay retains the
backend lease and forced host binding until the guest has safely detached.
"""
import json
from pathlib import Path
import re
import threading
import time

from connector.connector import request
from connector import native_usb as native
from connector.relay import Relay


class Agent:
    def __init__(self, directory, server, token, machine, key):
        self.directory = Path(directory) / 'sessions'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.server, self.token, self.machine, self.key = server, token, machine, key
        self.identity = {'machine_id': machine, 'machine_key': key}
        self.running = set()
        self.relays = {}
        self.attach_lock = threading.Lock()
        self.stopping = threading.Event()

    def read(self, sid):
        if not re.fullmatch('[a-f0-9]{32}', sid):
            raise ValueError('Invalid hardware session identifier')
        path = self.directory / (sid + '.json')
        return json.loads(path.read_text()) if path.exists() else {}

    def save(self, sid, data):
        if not re.fullmatch('[a-f0-9]{32}', sid):
            raise ValueError('Invalid hardware session identifier')
        path = self.directory / (sid + '.json')
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, indent=2))
        temp.replace(path)

    def event(self, sid, event, evidence=None, error=''):
        return request(self.server, '/api/agent/sessions/' + sid + '/event',
                       dict(self.identity, event=event, evidence=evidence or {}, error=error[:500]), self.token)

    def sync(self):
        sessions = request(self.server, '/api/agent/poll', self.identity, self.token)['sessions']
        for session in sessions:
            sid = session['id']
            if sid not in self.running:
                self.running.add(sid)
                threading.Thread(target=self.work, args=(session,), daemon=True).start()
        return sessions

    def tunnel(self, sid, role, busid):
        relay = Relay(self.server, sid, role, self.machine, self.key, busid).start()
        self.relays[sid] = relay
        return relay

    def work(self, session):
        sid, state = session['id'], session['state']
        host = session['host_id'] == self.machine
        journal = self.read(sid)
        try:
            if self.stopping.is_set():
                return
            if host and state == 'host-preparing':
                if journal:
                    raise RuntimeError('Interrupted host capture requires recovery before reconnecting.')
                journal = {'role': 'host', 'busid': session['busid'], 'captured': False}
                self.save(sid, journal)
                def capture_intent(instance):
                    journal.update(capture_intent=True, instance=instance)
                    self.save(sid, journal)
                native.prepare_host(session['busid'], capture_intent, session['serial_hash'])
                journal['captured'] = True
                self.save(sid, journal)
                self.tunnel(sid, 'host', session['busid'])
                self.event(sid, 'host-ready')
            elif not host and state == 'host-ready':
                if journal.get('port') and journal.get('attaching') is False:
                    return  # Poll raced the mounted acknowledgement.
                if journal:
                    raise RuntimeError('Interrupted guest attach requires safe detach before retrying.')
                # Serializing guest attach lets PnP arrival be correlated to this
                # native port instead of an unrelated concurrent USB insertion.
                with self.attach_lock:
                    before = {d['instance'] for d in native.disks()}
                    journal = {'role': 'guest', 'port': None, 'instances': [], 'attaching': True}
                    self.save(sid, journal)
                    relay = self.tunnel(sid, 'guest', session['busid'])
                    port = native.attach_guest(session['busid'], relay.port)
                    journal['port'] = port
                    self.save(sid, journal)
                    deadline = time.monotonic() + 35
                    mounted = None
                    while time.monotonic() < deadline and not self.stopping.is_set():
                        candidates = [d for d in native.disks() if d['virtual'] and d['instance'] not in before]
                        journal['instances'] = [d['instance'] for d in candidates]
                        self.save(sid, journal)
                        if len(candidates) == 1 and any(v.get('letter') and not v['readonly'] for v in candidates[0]['volumes']):
                            mounted = candidates[0]
                            break
                        if relay.done.is_set():
                            break
                        time.sleep(1)
                    if not mounted:
                        raise RuntimeError('USB attach did not produce a verified File Explorer volume.')
                    journal['attaching'] = False
                    self.save(sid, journal)
                    writable = next(v for v in mounted['volumes'] if v.get('letter') and not v['readonly'])
                    self.event(sid, 'mounted', {'port': port, 'pnp_id': mounted['instance'],
                                              'volume': writable['path']})
            elif state in ('host-ready', 'connected'):
                relay = self.relays.get(sid)
                if journal and (not relay or relay.done.is_set()):
                    raise RuntimeError('USB data connection was interrupted; safe recovery is required.')
            elif not host and state in ('disconnecting', 'recovery-required'):
                if journal.get('port'):
                    native.detach_guest(journal['port'], journal.get('instances', []))
                    journal['port'] = None
                    journal['attaching'] = False
                    self.save(sid, journal)
                elif journal.get('attaching'):
                    # Crash after native attach but before port journal write is
                    # ambiguous: never detach an arbitrary device or release A.
                    raise RuntimeError('Attach was interrupted before USB port was recorded. Inspect imported USB ports before recovery.')
                relay = self.relays.pop(sid, None)
                if relay:
                    relay.close()
                self.event(sid, 'guest-detached')
                self.save(sid, {})
            elif host and state == 'host-releasing':
                relay = self.relays.pop(sid, None)
                if relay:
                    relay.close()
                    relay.done.wait(5)
                if journal.get('capture_intent'):  # Never unbind another application's device.
                    native.release_host(session['busid'], journal['instance'])
                self.event(sid, 'host-released')
                self.save(sid, {})
        except Exception as error:
            try:
                self.event(sid, 'failed', error=str(error))
            except Exception:
                pass  # Durable journal remains; never claim a safe release.
        finally:
            self.running.discard(sid)

    def close(self):
        self.stopping.set()
        for sid, relay in list(self.relays.items()):
            try:
                self.event(sid, 'failed', error='Connector closed while owning a USB session.')
            except Exception:
                pass
            relay.close()
