"""Outbound WSS to Portway; raw USB/IP stays on loopback at both endpoints."""
import asyncio
import threading
import urllib.parse
import struct

from websockets.asyncio.client import connect


def relay_url(server, sid, role):
    parsed = urllib.parse.urlparse(server)
    if parsed.scheme != 'https' and parsed.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('USB relay requires HTTPS')
    scheme = 'wss' if parsed.scheme == 'https' else 'ws'
    return '{}://{}/api/relay/{}/{}'.format(scheme, parsed.netloc, sid, role)


class ImportGate:
    """Permit precisely one authorized USB/IP IMPORT, never DEVLIST/other ports."""
    def __init__(self, busid):
        self.busid = busid
        self.pending = bytearray()
        self.verified = False

    def accept(self, data):
        if self.verified:
            return data
        self.pending.extend(data)
        if len(self.pending) < 40:
            return b''
        version, operation, status = struct.unpack('!HHI', self.pending[:8])
        raw_busid = bytes(self.pending[8:40])
        busid = raw_busid.split(b'\0')[0].decode('ascii', errors='strict')
        if (version, operation, status) != (0x111, 0x8003, 0) or busid != self.busid:
            raise RuntimeError('USB stream requested an unauthorized physical device')
        self.verified = True
        result = bytes(self.pending)
        self.pending.clear()
        return result


class Relay:
    def __init__(self, server, sid, role, machine, key, busid):
        self.url = relay_url(server, sid, role)
        self.role = role
        self.import_gate = ImportGate(busid) if role == 'host' else None
        self.headers = {'Authorization': 'Bearer ' + key, 'X-Portway-Machine': machine}
        self.ready = threading.Event()
        self.done = threading.Event()
        self.error = ''
        self.port = None
        self.loop = None
        self.task = None
        self.closed = False

    async def copy(self, reader, writer):
        async with connect(self.url, additional_headers=self.headers, max_size=65536,
                           max_queue=4, compression=None, ping_interval=15, ping_timeout=20) as ws:
            if self.role == 'host':
                self.ready.set()

            async def upload():
                while True:
                    data = await reader.read(65536)
                    if not data:
                        return
                    await ws.send(data)

            async def download():
                async for data in ws:
                    if not isinstance(data, bytes):
                        raise RuntimeError('Non-binary USB data')
                    if self.import_gate:
                        data = self.import_gate.accept(data)
                        if not data:
                            continue
                    writer.write(data)
                    await writer.drain()

            tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                writer.close()
                await writer.wait_closed()

    async def run(self):
        if self.role == 'host':
            reader, writer = await asyncio.open_connection('127.0.0.1', 3240)
            await self.copy(reader, writer)
        else:
            complete = asyncio.Event()
            accepted = False

            async def handle(reader, writer):
                nonlocal accepted
                if accepted:
                    writer.close()
                    return
                accepted = True
                try:
                    await self.copy(reader, writer)
                except Exception as error:
                    self.error = str(error)
                finally:
                    complete.set()

            server = await asyncio.start_server(handle, '127.0.0.1', 0)
            self.port = server.sockets[0].getsockname()[1]
            self.ready.set()
            async with server:
                await complete.wait()

    def start(self):
        def worker():
            async def entry():
                self.loop = asyncio.get_running_loop()
                self.task = asyncio.current_task()
                try:
                    await self.run()
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    self.error = str(error)
                finally:
                    self.done.set()
                    self.ready.set()
            asyncio.run(entry())
        threading.Thread(target=worker, daemon=True).start()
        if not self.ready.wait(15) or self.done.is_set():
            self.close()
            raise RuntimeError(self.error or 'USB relay failed to start')
        return self

    def close(self):
        self.closed = True
        if self.loop and self.task and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.task.cancel)
