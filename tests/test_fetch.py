import json
import socket
import ssl
import time
import unittest
from unittest.mock import MagicMock, patch

from personal_agent import fetch
from personal_agent.app import AppError


class FetchTests(unittest.TestCase):
    def test_url_canonical_and_private_addresses(self):
        self.assertEqual(fetch.canonical_url('HTTPS://EXAMPLE.COM:443/a#fragment'), 'https://example.com/a')
        for value in ('file:///etc/passwd', 'https://u:p@example.com/', 'https://example.com:444/',
                      'http://127.0.0.1/', 'http://10.1.1.1/', 'http://169.254.169.254/',
                      'http://100.64.0.1/', 'http://192.168.1.1/', 'http://[::1]/',
                      'http://[::ffff:127.0.0.1]/', 'http://[fc00::1]/', 'http://224.0.0.1/',
                      'http://localhost/', 'http://x.local/', 'http://x.internal/', 'https://example.com/\nX'):
            with self.subTest(value=value), self.assertRaises(AppError): fetch.canonical_url(value)

    def test_mixed_dns_answers_rejected_before_connect(self):
        with patch.object(fetch, 'resolve_public', return_value=['93.184.216.34', '127.0.0.1']), patch.object(fetch, 'request_once') as req:
            with self.assertRaises(AppError): fetch.fetch_url('https://example.com')
            req.assert_not_called()

    def test_dns_subprocess_bounded_and_validates_answers(self):
        for result in (['127.0.0.1'], ['93.184.216.34', '192.168.0.1'], []):
            with patch.object(fetch.subprocess, 'run', return_value=MagicMock(returncode=0, stdout=json.dumps(result))) as call:
                with self.assertRaises(AppError): fetch.resolve_public('example.com', timeout=.5)
                self.assertEqual(call.call_args.kwargs['timeout'], .5)

    def test_redirect_to_private_rejected(self):
        with patch.object(fetch, 'resolve_public', return_value=['93.184.216.34']), patch.object(fetch, 'request_once', return_value=(302, {'location':'http://127.0.0.1/'}, b'')) as req:
            with self.assertRaises(AppError): fetch.fetch_url('https://example.com')
            self.assertEqual(req.call_count, 1)

    def test_dns_rebinding_revalidated_each_redirect(self):
        with patch.object(fetch, 'resolve_public', side_effect=[['93.184.216.34'], ['127.0.0.1']]), patch.object(fetch, 'request_once', return_value=(302, {'location':'/next'}, b'')) as req:
            with self.assertRaises(AppError): fetch.fetch_url('https://example.com')
            self.assertEqual(req.call_count, 1)
            self.assertEqual(req.call_args.args[1], '93.184.216.34')

    def test_redirect_limit_and_downgrade(self):
        for location, code, count in [('/next', 'URL_REDIRECT', 4), ('http://example.com/', 'URL_DOWNGRADE', 1)]:
            with patch.object(fetch, 'resolve_public', return_value=['93.184.216.34']), patch.object(fetch, 'request_once', return_value=(302, {'location':location}, b'')) as req:
                with self.assertRaises(AppError) as exc: fetch.fetch_url('https://example.com')
                self.assertEqual(exc.exception.code, code); self.assertEqual(req.call_count, count)

    def request(self, headers=(), chunks=(b'body', b''), status=200, peer='93.184.216.34', tls=False):
        sock = MagicMock(); sock.getpeername.return_value=(peer, 443 if tls else 80)
        response = MagicMock(status=status); response.getheaders.return_value=headers; response.read1.side_effect=chunks
        connection = MagicMock(); connection.getresponse.return_value=response
        with patch.object(fetch.socket, 'create_connection', return_value=sock) as connect, patch.object(fetch.http.client, 'HTTPConnection', return_value=connection), patch.object(fetch.ssl, 'create_default_context') as context:
            context.return_value.wrap_socket.return_value=sock
            r = fetch.request_once(('https' if tls else 'http')+'://example.com/', '93.184.216.34', time.monotonic()+2)
            self.assertEqual(connect.call_args.args[0], ('93.184.216.34', 443 if tls else 80))
            if tls: context.return_value.wrap_socket.assert_called_once_with(sock, server_hostname='example.com', do_handshake_on_connect=False)
            self.assertIs(connection.sock, sock)
            return r

    def test_numeric_connection_and_tls_hostname(self):
        self.assertEqual(self.request(tls=True)[2], b'body')

    def test_http_size_truncation_compression_and_peer_limits(self):
        cases = [({'status':404}, 'HTTP_404'), ({'peer':'127.0.0.1'}, 'URL_PEER'),
                 ({'headers':[('Content-Length','1048577')]}, 'SOURCE_SIZE'),
                 ({'headers':[('Content-Length','5')]}, 'URL_TRUNCATED'),
                 ({'headers':[('Content-Length','bad')]}, 'URL_LENGTH'),
                 ({'headers':[('Content-Encoding','gzip')]}, 'URL_ENCODING'),
                 ({'chunks':[b'x'*1048577]}, 'SOURCE_SIZE')]
        for kwargs, code in cases:
            with self.subTest(code=code), self.assertRaises(AppError) as exc: self.request(**kwargs)
            self.assertEqual(exc.exception.code, code)

    def test_connection_timeout_and_tls_failure(self):
        for failure, code in ((socket.timeout(), 'URL_TIMEOUT'), (ssl.SSLCertVerificationError(), 'URL_TLS')):
            with patch.object(fetch.socket, 'create_connection', side_effect=failure):
                with self.assertRaises(AppError) as exc: fetch.request_once('https://example.com/', '93.184.216.34', time.monotonic()+2)
                self.assertEqual(exc.exception.code, code)

    def test_total_deadline_and_cancel_before_network(self):
        with patch.object(fetch.socket, 'create_connection') as connect:
            with self.assertRaises(AppError): fetch.request_once('http://example.com/', '93.184.216.34', time.monotonic()-1)
            connect.assert_not_called()
        with patch.object(fetch, 'resolve_public') as resolve:
            with self.assertRaises(AppError): fetch.fetch_url('https://example.com', lambda: 'cancel')
            resolve.assert_not_called()

    def test_deadline_eof_without_length_is_not_success(self):
        sock = MagicMock(); sock.getpeername.return_value=('93.184.216.34',80)
        response = MagicMock(status=200); response.getheaders.return_value=[]; response.read1.return_value=b''
        connection = MagicMock(); connection.getresponse.return_value=response
        with patch.object(fetch.socket, 'create_connection', return_value=sock), patch.object(fetch.http.client, 'HTTPConnection', return_value=connection), patch.object(fetch.time, 'monotonic', side_effect=[0,0,0,11]):
            with self.assertRaises(AppError) as exc: fetch.request_once('http://example.com/', '93.184.216.34', 10)
            self.assertEqual(exc.exception.code, 'URL_TIMEOUT')


if __name__ == '__main__': unittest.main()
