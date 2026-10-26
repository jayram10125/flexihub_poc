"""Mutually authenticated TLS 1.3 tunnel for the isolated USB/IP experiment.

Host: TLS listener -> local usbipd:3240.
Guest: loopback:3240 -> host TLS listener. The native USB/IP guest attaches to
127.0.0.1. This adds transport security, not leases, safe eject or NAT traversal.
Use a dedicated CA per two-machine test; never use the website session token.
"""
import argparse
import asyncio
import ssl


def context(cert, key, ca, server):
    purpose = ssl.Purpose.CLIENT_AUTH if server else ssl.Purpose.SERVER_AUTH
    ctx = ssl.create_default_context(purpose, cafile=ca)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_cert_chain(cert, key)
    return ctx


async def pump(reader, writer):
    while True:
        data = await reader.read(65536)
        if not data:
            return
        writer.write(data)
        await writer.drain()


async def bridge(reader, writer, target, port, tls=None, hostname=None):
    remote = None
    tasks = []
    try:
        upstream, remote = await asyncio.wait_for(
            asyncio.open_connection(target, port, ssl=tls, server_hostname=hostname,
                                    **({'ssl_handshake_timeout': 10} if tls else {})), 12)
        tasks = [asyncio.create_task(pump(reader, remote)),
                 asyncio.create_task(pump(upstream, writer))]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (OSError, asyncio.TimeoutError, ssl.SSLError) as error:
        print('Tunnel connection failed: {}'.format(error), flush=True)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for connection in (writer, remote):
            if connection is not None:
                connection.close()
                try:
                    await asyncio.wait_for(connection.wait_closed(), 3)
                except (OSError, asyncio.TimeoutError):
                    pass


async def serve(args):
    tls = context(args.cert, args.key, args.ca, args.mode == 'host')
    if args.mode == 'host':
        callback = lambda r, w: bridge(r, w, '127.0.0.1', args.usbip_port)
        server = await asyncio.start_server(callback, args.listen, args.port,
                                            ssl=tls, ssl_handshake_timeout=10)
    else:
        if not args.host or not args.server_name:
            raise ValueError('Guest requires --host and --server-name matching the host certificate')
        callback = lambda r, w: bridge(r, w, args.host, args.port, tls, args.server_name)
        # Never expose guest's decrypted USB/IP endpoint to other machines.
        server = await asyncio.start_server(callback, '127.0.0.1', args.usbip_port)
    async with server:
        print('Portway TLS tunnel listening: {}'.format(server.sockets[0].getsockname()), flush=True)
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('host', 'guest'))
    parser.add_argument('--cert', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--ca', required=True)
    parser.add_argument('--host')
    parser.add_argument('--server-name')
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=3241)
    parser.add_argument('--usbip-port', type=int, default=3240)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
