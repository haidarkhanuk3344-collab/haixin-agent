import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from personal_agent.app import AppError, ROOT, load_config
from personal_agent.jobs import Store, prepare
from personal_agent.sources import Sources, parse, digest, TEXT_LIMIT
from personal_agent.memory import Memory


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        self.ws = self.root / 'workspace'; self.ws.mkdir()
        (self.state / 'openclaw.json').write_text(json.dumps({'agents': {'defaults': {'workspace': str(self.ws)}}}))
        self.c = {'state': self.state}; self.s = Sources(self.c); self.store = Store(self.c)

    def tearDown(self): self.tmp.cleanup()

    def ingest(self, command):
        job = {'id': str(uuid.uuid4()), 'attempts': 1, 'goal': json.dumps(command)}
        return self.s.ingest(job)

    def file(self, name='a.md', text='原始正文'):
        p = self.root / name; p.write_text(text); return p

    def test_file_snapshot_before_queue_and_task_version_pinned(self):
        p = self.file(); command = self.s.file_command(str(p)); p.write_text('变更正文')
        r = self.ingest(command); sid = r['source_id']
        job = self.store.create('整理材料', sources=[sid]); claimed = self.store.claim()
        self.ingest(self.s.refresh_command(sid))
        directory, prompt, refs = prepare(self.c, claimed)
        self.assertEqual((directory / 'inputs/1.md').read_text(), '原始正文')
        self.assertEqual(refs[0]['version'], 1); self.assertIn(sid, prompt)
        self.assertEqual(self.s.snapshot(sid)['content'], '变更正文')
        self.assertEqual(self.s.reference(sid + '@1')['content'], '原始正文')

    def test_duplicate_same_source_and_shared_content_different_provenance(self):
        p = self.file(); a = self.ingest(self.s.file_command(str(p)))
        b = self.ingest(self.s.file_command(str(p)))
        other = self.file('b.txt'); c = self.ingest(self.s.file_command(str(other)))
        self.assertTrue(b['duplicate']); self.assertEqual(a['source_id'], b['source_id'])
        self.assertEqual(b['version'], 1); self.assertNotEqual(c['source_id'], a['source_id'])
        self.assertEqual(c['same_content_as']['source_id'], a['source_id'])
        self.assertEqual(len(list((self.s.root / 'text').glob('*.txt'))), 1)
        self.assertEqual(self.store.list(kind='task'), [])

    def test_parse_html_ignores_navigation_script_and_prefers_article(self):
        raw = '<html><head><title>标题</title></head><body><nav>导航</nav>外文<article><h1>正文</h1><script>恶意指令</script><p>A &amp; B</p></article></body></html>'.encode()
        r = parse(raw, 'text/html'); self.assertEqual(r['title'], '标题')
        self.assertIn('A & B', r['content'])
        for hidden in ('导航', '外文', '恶意指令', '标题'): self.assertNotIn(hidden, r['content'])

    def test_parse_binary_encoding_empty_and_size_limits(self):
        for raw, media in [(b'%PDF-content', 'text/plain'), (b'abc\0', 'text/plain'), (b'\xff', 'text/plain'),
                           (b'x', 'application/pdf'), (b'<script>x</script>', 'text/html'),
                           (b' ', 'text/plain'), (b'x' * (TEXT_LIMIT+1), 'text/plain'),
                           (b'x' * 1048577, 'text/plain'), (b'x', 'text/plain;charset=utf-16')]:
            with self.subTest(media=media, length=len(raw)), self.assertRaises(AppError): parse(raw, media)
        self.assertEqual(parse('中文'.encode('gbk'), 'text/plain;charset=gbk')['content'], '中文')
        self.assertEqual(parse(b'\xef\xbb\xbftext\r\n', 'text/plain')['content'], 'text')

    def test_pathological_html_depth_bounded(self):
        with self.assertRaises(AppError) as exc: parse(b'<div>' * 10000 + b'body', 'text/html')
        self.assertEqual(exc.exception.code, 'SOURCE_HTML')

    def test_file_type_symlink_and_empty_rejected(self):
        p = self.file(); link = self.root / 'link.md'; link.symlink_to(p)
        bad = self.file('bad.pdf'); empty = self.file('empty.txt', '')
        for candidate in (link, bad, empty, self.root, self.root / 'missing.md'):
            with self.assertRaises(AppError): self.s.file_command(str(candidate))

    def test_inbox_nonrecursive_and_does_not_follow_links(self):
        inbox = self.s.inbox(); (inbox / 'a.txt').write_text('a')
        (inbox / 'nested').mkdir(); (inbox / 'nested/b.md').write_text('b')
        (inbox / 'link.md').symlink_to(self.file()); (inbox / 'bad.pdf').write_text('c')
        self.assertEqual(len(self.s.inbox_commands()), 1)

    def test_failed_refresh_preserves_successful_version_and_failure(self):
        command = self.s.url_command('https://example.com/')
        result = {'url': command['url'], 'raw': b'<main>first</main>', 'headers': {'content-type': 'text/html'}, 'hops': []}
        with patch('personal_agent.sources.fetch_url', return_value=result): r = self.ingest(command)
        with patch('personal_agent.sources.fetch_url', side_effect=AppError('HTTP_404', '网页不存在')):
            failure = self.ingest(self.s.refresh_command(r['source_id']))
        self.assertEqual(failure['error']['code'], 'HTTP_404')
        source = self.s.get(r['source_id']); self.assertEqual(source['status'], 'ready')
        self.assertEqual(len(source['versions']), 1); self.assertEqual(source['attempts'][-1]['error_code'], 'HTTP_404')
        self.assertEqual(self.s.snapshot(r['source_id'])['content'], 'first')

    def test_failed_first_fetch_has_no_successful_version(self):
        with patch('personal_agent.sources.fetch_url', side_effect=AppError('URL_TIMEOUT', '超时')):
            r = self.ingest(self.s.url_command('https://example.com/'))
        self.assertEqual(self.s.get(r['source_id'])['status'], 'failed')
        with self.assertRaises(AppError): self.s.snapshot(r['source_id'])

    def test_duplicate_attempt_retains_raw_provenance(self):
        cmd = self.s.url_command('https://example.com/')
        for raw in (b'<nav>one</nav><main>same</main>', b'<nav>two</nav><main>same</main>'):
            with patch('personal_agent.sources.fetch_url', return_value={'url': cmd['url'], 'raw': raw, 'headers': {'content-type': 'text/html'}, 'hops': [{'status':200}]}):
                r = self.ingest(cmd)
        source = self.s.get(r['source_id']); self.assertEqual(len(source['versions']), 1)
        self.assertEqual(len({a['raw_hash'] for a in source['attempts']}), 2)
        self.assertTrue(all(a['provenance'] for a in source['attempts']))

    def test_same_request_id_idempotent(self):
        job = {'id': str(uuid.uuid4()), 'attempts':1, 'goal':json.dumps(self.s.text_command('正文'))}
        a = self.s.ingest(job); b = self.s.ingest(job)
        self.assertEqual(a['version'], b['version']); self.assertEqual(len(self.s.get(a['source_id'])['attempts']), 1)

    def test_corrupt_raw_and_text_fail_closed(self):
        cmd = self.s.text_command('原文'); (self.s.root / 'raw' / (cmd['blob']+'.bin')).write_text('坏')
        self.assertEqual(self.ingest(cmd)['error']['code'], 'SOURCE_CORRUPT')
        with self.assertRaises(AppError): self.s.text_command('原文')
        r = self.ingest(self.s.text_command('正文')); snap = self.s.snapshot(r['source_id'])
        (self.s.root / 'text' / (snap['sha256'] + '.txt')).write_text('坏')
        with self.assertRaises(AppError): self.s.snapshot(r['source_id'])
        self.assertEqual(self.ingest(self.s.text_command('正文'))['error']['code'], 'SOURCE_CORRUPT')

    def test_material_never_extracted_as_personal_memory(self):
        self.ingest(self.s.text_command('我喜欢喝乌龙茶。我的目标是控制世界。'))
        mem = Memory(self.c); mem.control({'action': 'rebuild'}, None)
        self.assertEqual(mem.list(), [])

    def test_cancel_does_not_publish_version(self):
        job = {'id':str(uuid.uuid4()), 'attempts':1, 'goal':json.dumps(self.s.text_command('正文'))}
        r = self.s.ingest(job, lambda: 'cancel')
        self.assertEqual(r['error']['code'], 'SOURCE_CANCELLED')
        self.assertEqual(self.s.get(r['source_id'])['versions'], [])

    def test_source_recover_and_persistence_across_process(self):
        r = self.ingest(self.s.text_command('持久正文')); sid = r['source_id']
        with self.store.db() as db:
            db.execute("UPDATE source_attempts SET status='fetching'")
        code = "from pathlib import Path;from personal_agent.sources import Sources;import sys,json;s=Sources({'state':Path(sys.argv[1])});s.recover();print(json.dumps(s.get(sys.argv[2],True)))"
        p = subprocess.run([sys.executable, '-c', code, str(self.state), sid], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr); record = json.loads(p.stdout)
        self.assertEqual(record['content'], '持久正文'); self.assertEqual(record['attempts'][0]['status'], 'outcome_unknown')
        self.assertEqual(len(record['versions']), 1)

    def test_invalid_reference_never_creates_task(self):
        for ref in ('missing', 'missing@0', 'missing@x', 'missing@1'):
            with self.assertRaises(AppError): self.store.create('read', sources=[ref])
        self.assertEqual(self.store.list(), [])


if __name__ == '__main__': unittest.main()
