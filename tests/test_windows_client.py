import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from win_collab.client import Client, KEYS, discovery_failure_message, pick_unique_creds, same_local_creds
from win_collab.protected_auth import save, load


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        self.server.received=dict(self.headers)
        if self.path=='/redirect':
            self.send_response(302);self.send_header('Location','http://example.com/');self.end_headers();return
        if self.path=='/private-error':
            self.send_response(401);self.end_headers();self.wfile.write(b'not-for-logs-secret');return
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
        self.wfile.write(b'{"healthy":true}')


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.server=HTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.creds=dict(zip(KEYS,['super-agent','synthetic-password','synthetic-local-key']))
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.client=Client(self.base,self.creds)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def test_signed_loopback_request_ignores_proxy(self):
        old=os.environ.get('HTTP_PROXY');os.environ['HTTP_PROXY']='http://127.0.0.1:1'
        try:self.assertEqual(self.client.call('GET','/health?value=a%20b'),{'healthy':True})
        finally:
            if old is None:os.environ.pop('HTTP_PROXY',None)
            else:os.environ['HTTP_PROXY']=old
        h=self.server.received
        payload='\n'.join(['local-v1','GET','/health?value=a%20b',h['X-Sa-Timestamp'],h['X-Sa-Nonce']])
        expected=base64.urlsafe_b64encode(hmac.new(b'synthetic-local-key',payload.encode(),hashlib.sha256).digest()).decode().rstrip('=')
        self.assertEqual(h['X-Sa-Signature'],expected)

    def test_no_redirect_of_credentials(self):
        with self.assertRaisesRegex(RuntimeError,'redirects are forbidden'):self.client.call('GET','/redirect')

    def test_error_body_is_never_exposed(self):
        with self.assertRaises(RuntimeError) as e:self.client.call('GET','/private-error')
        self.assertNotIn('not-for-logs',str(e.exception))

    def test_remote_endpoint_rejected(self):
        for url in ('http://example.com','http://127.0.0.1@evil.test','https://127.0.0.1:4397'):
            with self.assertRaises(ValueError):Client(url,self.creds)

    def test_identical_credential_sources_are_accepted(self):
        creds=dict(zip(KEYS,['super-agent','synthetic-password','synthetic-local-key']))
        self.assertTrue(same_local_creds(creds, dict(creds)))
        self.assertEqual(pick_unique_creds([(1,creds),(2,dict(creds))]), creds)

    def test_discovery_failure_message_follows_counts(self):
        stripped = discovery_failure_message(verified_candidates=5, vm_read_denied=0, keys_unavailable=5)
        self.assertIn('keys were not in the environment block', stripped)
        self.assertNotIn('higher-integrity', stripped)
        self.assertNotIn('VM_READ was denied', stripped)
        denied = discovery_failure_message(verified_candidates=4, vm_read_denied=4, keys_unavailable=0)
        self.assertIn('VM_READ was denied', denied)
        self.assertNotIn('higher-integrity', denied)
        missing = discovery_failure_message(verified_candidates=0, vm_read_denied=0, keys_unavailable=0)
        self.assertIn('no verified SAC/node runtime images were found', missing)

    def test_divergent_credential_sources_are_rejected(self):
        a=dict(zip(KEYS,['super-agent','synthetic-password','synthetic-local-key']))
        b=dict(zip(KEYS,['super-agent','other-password','synthetic-local-key']))
        with self.assertRaisesRegex(RuntimeError,'found 2'):
            pick_unique_creds([(1,a),(2,b)])

    @unittest.skipUnless(os.name=='nt','DPAPI is Windows-only')
    def test_dpapi_roundtrip_synthetic_local_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'auth.dpapi'
            save(path,{'base':self.base,'creds':self.creds})
            self.assertNotIn(b'synthetic-password',path.read_bytes())
            self.assertEqual(load(path),(self.base,self.creds))
