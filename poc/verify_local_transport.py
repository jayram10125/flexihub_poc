"""Probe the installed local USB/IP host through an actual mutual TLS tunnel.

Read-only: does not bind devices, attach disks, mount volumes or write USB data.
"""
import asyncio
import json
from pathlib import Path
import struct

from poc.tls_tunnel import bridge, context
from poc.usbip import HEADER


async def verify():
    root = Path('native/certificates')
    host_ctx = context(root / 'host.pem', root / 'host.key', root / 'ca.pem', True)
    guest_ctx = context(root / 'guest.pem', root / 'guest.key', root / 'ca.pem', False)
    server = await asyncio.start_server(
        lambda r, w: bridge(r, w, '127.0.0.1', 3240), '127.0.0.1', 0, ssl=host_ctx)
    writer = None
    try:
        reader, writer = await asyncio.open_connection(
            '127.0.0.1', server.sockets[0].getsockname()[1],
            ssl=guest_ctx, server_hostname='portway-host')
        writer.write(HEADER.pack(0x111, 0x8005, 0))
        await writer.drain()
        reply = HEADER.unpack(await asyncio.wait_for(reader.readexactly(8), 8))
        if reply != (0x111, 5, 0):
            raise RuntimeError('Native host returned incompatible DEVLIST header')
        count, = struct.unpack('!I', await asyncio.wait_for(reader.readexactly(4), 8))
        result = {'native_host_tls_probe': 'passed', 'tls': writer.get_extra_info('ssl_object').version(),
                  'exported_devices': count, 'hardware_attach_test': 'not-performed'}
        Path('native/logs/local-transport.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


if __name__ == '__main__':
    asyncio.run(verify())
