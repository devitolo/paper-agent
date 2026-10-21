"""Bounded private Mini ingress. No state mounts, credentials, or arbitrary upstream."""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import socket
import threading
from urllib.parse import urlsplit

UPSTREAM = 'paper-mini-nonprod-app-1'
UPSTREAM_PORT = 8000
MAX_BODY = 1024 * 1024
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade'}
APPROVED_AUTHORITIES = {'192.168.1.110:18080', '127.0.0.1:18080'}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, authority, cidr):
        host, port = authority.split(':')
        network = ipaddress.ip_network(cidr, strict=True)
        if authority not in APPROVED_AUTHORITIES:
            raise ValueError('Unapproved private authority')
        if not ipaddress.ip_address(host).is_private:
            raise ValueError('Private authority required')
        if not network.is_private or int(port) != 18080:
            raise ValueError('Private CIDR and dedicated port18080 required')
        self.authority, self.network = authority, network
        self.authorities = APPROVED_AUTHORITIES
        self.allow_loopback = True
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'PaperNonprod'
    sys_version = ''

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_message(self, *_args):
        pass  # No paper IDs, feedback, request paths or headers in ingress logs.

    def relay(self):
        client_ip = ipaddress.ip_address(self.client_address[0])
        if client_ip not in self.server.network and not (self.server.allow_loopback and client_ip.is_loopback):
            self.send_error(403)
            return
        target = urlsplit(self.path)
        if not self.path.startswith('/') or self.path.startswith('//') or target.scheme or target.netloc:
            self.send_error(400)
            return
        if self.headers.get('Host') not in self.server.authorities:
            self.send_error(400)
            return
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + authority for authority in self.server.authorities}:
            self.send_error(403)
            return
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.send_error(403)
            return
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) > 1:
            self.send_error(400)
            return
        try:
            length = int(lengths[0]) if lengths else 0
            if not 0 <= length <= MAX_BODY:
                raise ValueError()
        except ValueError:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_error(400)
            return
        excluded = HOP | {x.strip().lower() for x in self.headers.get('Connection', '').split(',')}
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in excluded and not k.lower().startswith('x-forwarded-')}
        headers['Connection'] = 'close'
        conn = http.client.HTTPConnection(UPSTREAM, UPSTREAM_PORT, timeout=30)
        started = False
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status)
            response_hop = HOP | {x.strip().lower() for x in (response.getheader('Connection') or '').split(',')}
            for k, v in response.getheaders():
                if k.lower() not in response_hop:
                    self.send_header(k, v)
            self.send_header('Connection', 'close')
            self.end_headers()
            started = True
            if self.command != 'HEAD':
                while block := response.read(64 * 1024):
                    self.wfile.write(block)
        except (OSError, http.client.HTTPException, socket.timeout):
            if not started:
                self.send_error(502)
        finally:
            conn.close()
            self.close_connection = True

    do_GET = do_HEAD = do_POST = relay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authority', required=True)
    parser.add_argument('--allow-cidr', required=True)
    args = parser.parse_args()
    with Server(('0.0.0.0', 8080), args.authority, args.allow_cidr) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
