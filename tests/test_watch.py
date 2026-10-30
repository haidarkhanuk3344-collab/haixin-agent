import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from personal_agent.app import AppError
from personal_agent.jobs import Store
from personal_agent.sources import Sources
from personal_agent.watch import Watch


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        state=self.root/'state';state.mkdir();ws=self.root/'workspace';ws.mkdir()
        (state/'openclaw.json').write_text(json.dumps({'agents':{'defaults':{'workspace':str(ws)}}}))
        self.c={'state':state};self.s=Sources(self.c);self.store=Store(self.c)
        self.t=1000.;self.w=Watch(self.c,clock=lambda:self.t)
        self.file=self.root/'notes.md';self.file.write_text('初版资料')
        self.sid=self.ingest(self.s.file_command(str(self.file)))['source_id']

    def tearDown(self):self.tmp.cleanup()

    def ingest(self,command):return self.s.ingest({'id':str(uuid.uuid4()),'attempts':1,'goal':json.dumps(command)})

    def subscribe(self):return self.w.add(self.sid,60)

    def due(self):self.t+=60;return self.w.cycle()

    def test_no_implicit_subscription_and_idempotent_add(self):
        self.assertEqual(self.w.list(),[]);a=self.subscribe();b=self.w.add(self.sid,900)
        self.assertEqual(a['interval_seconds'],b['interval_seconds']);self.assertEqual(len(self.w.list()),1)
        self.assertIsNone(self.w.cycle())

    def test_reject_text_missing_source_invalid_interval(self):
        text=self.ingest(self.s.text_command('静态片段'))['source_id']
        for sid,seconds in [(text,60),('missing',60),(self.sid,0),(self.sid,59),(self.sid,86401),(self.sid,True)]:
            with self.assertRaises(AppError):self.w.add(sid,seconds)
        self.assertEqual(self.w.list(),[])

    def test_unchanged_has_no_version_or_model_or_job(self):
        self.subscribe();run=self.due();self.assertEqual(run['status'],'unchanged')
        self.assertEqual(len(self.s.get(self.sid)['versions']),1);self.assertEqual(self.store.list(),[])
        self.assertFalse((self.c['state']/'usage-v1.jsonl').exists())

    def test_changed_keeps_old_version_and_provenance(self):
        self.subscribe();self.file.write_text('第二版资料');run=self.due()
        self.assertEqual(run['status'],'changed');self.assertEqual(run['version'],2)
        self.assertEqual(self.s.snapshot(self.sid,1)['content'],'初版资料')
        self.assertEqual(self.s.snapshot(self.sid)['content'],'第二版资料')
        provenance=json.loads(self.s.get(self.sid)['versions'][-1]['provenance'])
        self.assertIn('watch',provenance['authorization']);self.assertEqual(self.w.get(self.sid)['last_changed'],self.t)

    def test_explicit_subscription_prompts_without_memory_and_deduplicates(self):
        from personal_agent.proactive import Proactive
        p=Proactive(self.c,clock=lambda:self.t);p.discover()
        self.subscribe();self.file.write_text('第二版新材料');self.due();p.discover();p.discover()
        with self.store.db() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM proactive_candidates WHERE event_key LIKE 'watch:%'")]
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'pending')
        self.assertIsNone(rows[0]['topic_id']);self.assertEqual(rows[0]['source_version'],2)
        self.assertTrue(p.context(rows[0])['explicitly_subscribed'])
        self.assertEqual(rows[0]['topic_key'],'watch:'+self.sid)
        self.due();p.discover()
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM proactive_candidates WHERE event_key LIKE 'watch:%'").fetchone()[0],1)

    def test_concurrent_reserve_only_once(self):
        self.subscribe();self.t+=60
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(lambda _:self.w.reserve(),range(8)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_no_catchup_burst_after_long_sleep(self):
        self.subscribe();self.t+=86400;self.w.cycle()
        self.assertIsNone(self.w.cycle());self.assertEqual(self.w.get(self.sid)['next_due'],self.t+60)

    def test_pause_remove_and_resume_preserve_history(self):
        self.subscribe();self.due();self.w.control(self.sid,'pause');self.t+=600
        self.assertIsNone(self.w.cycle());self.w.control(self.sid,'resume')
        self.assertIsNone(self.w.cycle());self.due();self.w.control(self.sid,'remove')
        self.assertFalse(self.w.get(self.sid)['enabled']);self.assertEqual(len(self.w.get(self.sid)['runs']),2)
        self.assertEqual(len(self.s.get(self.sid)['versions']),1)

    def test_check_does_not_bypass_pause_and_is_coalesced(self):
        self.subscribe();self.w.control(self.sid,'check');self.w.control(self.sid,'check')
        self.w.cycle();self.assertEqual(len(self.w.get(self.sid)['runs']),1)
        self.w.control(self.sid,'pause')
        with self.assertRaises(AppError):self.w.control(self.sid,'check')

    def test_failure_backoff_and_third_failure_pauses(self):
        self.subscribe();self.file.unlink()
        for failure in range(1,4):
            self.t=self.w.get(self.sid)['next_due'];self.w.cycle();record=self.w.get(self.sid)
            self.assertEqual(record['failures'],failure)
            if failure<3:self.assertEqual(record['next_due'],self.t+60*2**failure)
        self.assertFalse(record['enabled']);self.assertEqual(len(record['runs']),3)
        self.assertEqual(self.s.snapshot(self.sid)['content'],'初版资料')

    def test_recovery_pauses_unknown_without_refetch(self):
        self.subscribe();self.t+=60;self.w.reserve();self.w.recover()
        record=self.w.get(self.sid);self.assertFalse(record['enabled']);self.assertEqual(record['runs'][0]['status'],'outcome_unknown')
        self.t+=1000;self.assertIsNone(self.w.cycle());self.w.control(self.sid,'resume');self.due()
        self.assertEqual(self.w.get(self.sid)['runs'][0]['status'],'unchanged')

    def test_user_job_has_priority_over_watch(self):
        self.subscribe();self.store.create('用户问题',kind='chat');self.t+=60
        self.assertIsNone(self.w.reserve());self.assertEqual(self.w.get(self.sid)['runs'],[])

    def test_file_symlink_replacement_not_read(self):
        self.subscribe();self.file.unlink();other=self.root/'private.md';other.write_text('不应读取')
        self.file.symlink_to(other);run=self.due()
        self.assertEqual(run['status'],'failed');self.assertEqual(len(self.s.get(self.sid)['versions']),1)

    def test_parent_symlink_replacement_not_read(self):
        folder=self.root/'allowed';folder.mkdir();file=folder/'a.md';file.write_text('授权内容')
        sid=self.ingest(self.s.file_command(str(file)))['source_id'];self.w.add(sid,60)
        folder.rename(self.root/'moved');folder.symlink_to(self.root/'moved',target_is_directory=True)
        self.t+=60;run=self.w.cycle();self.assertEqual(run['error_code'],'WATCH_PATH')

    def test_source_locator_mutation_pauses(self):
        self.subscribe()
        with self.store.db() as db:db.execute('UPDATE sources SET locator=? WHERE id=?',(str(self.root/'other.md'),self.sid))
        run=self.due();self.assertEqual(run['error_code'],'WATCH_SCOPE');self.assertFalse(self.w.get(self.sid)['enabled'])

    def test_pause_during_fetch_blocks_publish(self):
        cmd=self.s.url_command('https://example.com/')
        fetched={'raw':b'<main>one</main>','headers':{'content-type':'text/html'},'url':cmd['url'],'hops':[]}
        with patch('personal_agent.sources.fetch_url',return_value=fetched):sid=self.ingest(cmd)['source_id']
        self.w.add(sid,60);self.t+=60
        def fetch(*args):
            self.w.control(sid,'pause');return dict(fetched,raw=b'<main>two</main>')
        with patch('personal_agent.sources.fetch_url',side_effect=fetch):self.w.cycle()
        self.assertFalse(self.w.get(sid)['enabled']);self.assertEqual(self.s.snapshot(sid)['content'],'one')

    def test_pause_at_commit_boundary_blocks_publish(self):
        self.subscribe();self.file.write_text('新内容');self.t+=60
        original=self.s.write_blob
        def write(raw):
            self.w.control(self.sid,'pause');return original(raw)
        with patch('personal_agent.sources.Sources.write_blob',side_effect=write):self.w.cycle()
        self.assertEqual(self.s.snapshot(self.sid)['content'],'初版资料')

    def test_settings_and_history_survive_process(self):
        self.subscribe();self.due()
        code='from pathlib import Path;from personal_agent.watch import Watch;import sys,json;print(json.dumps(Watch({"state":Path(sys.argv[1])}).get(sys.argv[2])))'
        p=subprocess.run([sys.executable,'-c',code,str(self.c['state']),self.sid],capture_output=True,text=True,check=True)
        r=json.loads(p.stdout);self.assertEqual(r['interval_seconds'],60);self.assertEqual(len(r['runs']),1)

    def test_user_arrives_during_fetch_defers_without_publishing(self):
        self.subscribe();self.file.write_text('第二版');self.t+=60
        called=[]
        def tick():
            called.append(1)
            if len(called)==2:self.store.create('用户先处理',kind='chat')
        r=self.w.cycle(tick);self.assertEqual(r['status'],'deferred')
        self.assertEqual(self.s.get(self.sid)['current_version'],1);self.assertEqual(self.w.get(self.sid)['failures'],0)

    def test_strict_file_open_rejects_parent_symlink(self):
        folder=self.root/'folder';folder.mkdir();file=folder/'a.md';file.write_text('不应读取')
        link=self.root/'alias';link.symlink_to(folder,target_is_directory=True)
        with self.assertRaises(AppError):self.s.file_command(str(link/'a.md'),strict=True)


if __name__=='__main__':unittest.main()
