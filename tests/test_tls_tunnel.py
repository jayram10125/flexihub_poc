import asyncio
from pathlib import Path
import tempfile
import unittest

from poc.lab_certificates import generate
from poc.tls_tunnel import bridge, context


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    async def test_mutual_tls_forwards_real_bidirectional_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            generate(tmp)
            root = Path(tmp)
            host_ctx = context(root / 'host.pem', root / 'host.key', root / 'ca.pem', True)
            guest_ctx = context(root / 'guest.pem', root / 'guest.key', root / 'ca.pem', False)

            async def echo(reader, writer):
                try:
                    while True:
                        data = await reader.read(65536)
                        if not data:
                            break
                        writer.write(data)
                        await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            echo_server = await asyncio.start_server(echo, '127.0.0.1', 0)
            echo_port = echo_server.sockets[0].getsockname()[1]
            tls_server = await asyncio.start_server(
                lambda r, w: bridge(r, w, '127.0.0.1', echo_port),
                '127.0.0.1', 0, ssl=host_ctx)
            tls_port = tls_server.sockets[0].getsockname()[1]
            try:
                reader, writer = await asyncio.open_connection(
                    '127.0.0.1', tls_port, ssl=guest_ctx, server_hostname='portway-host')
                payload = bytes(range(256)) * 2048
                writer.write(payload)
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readexactly(len(payload)), 5), payload)
                writer.close()
                await writer.wait_closed()
                with self.assertRaises(Exception):
                    await asyncio.open_connection('127.0.0.1', tls_port,
                                                  ssl=guest_ctx, server_hostname='wrong-host')
            finally:
                tls_server.close()
                echo_server.close()
                await tls_server.wait_closed()
                await echo_server.wait_closed()
