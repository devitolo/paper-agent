from contextlib import closing
import http.client
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

from paper_agents import nonprod_proxy as proxy


class Upstream(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length','5')
        self.end_headers()
        self.wfile.write(b'paper')
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.send_response(200)
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        self.proxy=proxy.Server(('127.0.0.1',0),'127.0.0.1:18080','127.0.0.0/8')
        self.patch_host=patch.object(proxy,'UPSTREAM','127.0.0.1')
        self.patch_port=patch.object(proxy,'UPSTREAM_PORT',self.upstream.server_port)
        self.patch_host.start(); self.patch_port.start()
        for server in (self.upstream,self.proxy):
            threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(self.close)

    def close(self):
        for server in (self.proxy,self.upstream):
            server.shutdown();server.server_close()
        self.patch_host.stop();self.patch_port.stop()

    def request(self,method='GET',path='/',body=None,headers=None):
        with closing(http.client.HTTPConnection('127.0.0.1',self.proxy.server_port,timeout=5)) as conn:
            conn.request(method,path,body,{'Host':'127.0.0.1:18080',**(headers or {})})
            result=conn.getresponse()
            return result.status,result.read()

    def test_fixed_upstream_get_and_feedback_post(self):
        self.assertEqual(self.request(),(200,b'paper'))
        self.assertEqual(self.request('POST','/feedback',b'notes=local'),(200,b'notes=local'))
        self.assertEqual(self.request(headers={'Host':'192.168.1.110:18080'}),(200,b'paper'))
        self.assertEqual(self.request(headers={'Host':'127.0.0.1:18080', 'Origin':'http://127.0.0.1:18080'}),(200,b'paper'))

    def test_open_proxy_and_external_origin_refused(self):
        self.assertEqual(self.request(path='http://example.org/')[0],400)
        self.assertEqual(self.request('CONNECT',path='example.org:443')[0],501)
        self.assertEqual(self.request(headers={'Origin':'https://example.org'})[0],403)
        self.assertEqual(self.request(headers={'Host':'example.org'})[0],400)
        self.assertEqual(self.request(headers={'Sec-Fetch-Site':'cross-site'})[0],403)

    def test_body_limit_and_client_network_refused(self):
        self.assertEqual(self.request('POST',headers={'Content-Length':str(proxy.MAX_BODY+1)})[0],413)
        self.proxy.network=ipaddress.ip_network('192.168.1.0/24')
        self.proxy.allow_loopback=False
        self.assertEqual(self.request()[0],403)
