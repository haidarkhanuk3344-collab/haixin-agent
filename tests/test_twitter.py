import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from personal_agent.app import AppError
from personal_agent.twitter import target,render,fetch,settings,connect,status,invoke,ego_credentials,backend_environment
from personal_agent.sources import Sources
from personal_agent.watch import Watch


def post(tid='123',text='新的种菜记录'):
    return {'id':tid,'text':text,'author':{'screenName':'Test_Account','name':'名字'},'createdAtISO':'2026-10-29T00:00:00Z','metrics':{'likes':1}}


class TwitterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        state=self.root/'state';state.mkdir();ws=self.root/'workspace';ws.mkdir()
        (state/'openclaw.json').write_text(json.dumps({'agents':{'defaults':{'workspace':str(ws)}}}))
        self.c={'state':state};self.s=Sources(self.c)

    def tearDown(self):self.tmp.cleanup()

    def ingest(self,cmd):return self.s.ingest({'id':str(uuid.uuid4()),'attempts':1,'goal':json.dumps(cmd)})

    def test_handle_canonicalization_and_scope(self):
        self.assertEqual(target('@OpenAI'),target('https://x.com/OpenAI'))
        for value in ('https://x.com/home','https://x.com/test/status/123','https://evil.com/test','https://user@x.com/test','https://x.com:443/test','-x','a/b','A'*16):
            with self.subTest(value=value),self.assertRaises(AppError):target(value)

    def test_query_canonicalization_and_validation(self):
        self.assertEqual(target('人工智能',True)['locator'],'人工智能')
        for value in ('','x\nprivate','x'*161):
            with self.assertRaises(AppError):target(value,True)

    def test_metrics_order_and_name_do_not_create_changes(self):
        a,b=post('123'),post('456','第二条');cmd=target('@openai')
        expected=render(cmd,[a,b])['raw'];a['metrics']['likes']=100;a['author']['name']='另一个显示名'
        self.assertEqual(render(cmd,[b,a])['raw'],expected)

    def test_real_text_or_post_id_changes_are_detected(self):
        cmd=target('@openai');original=render(cmd,[post()])['raw']
        self.assertNotEqual(original,render(cmd,[post(text='修订')])['raw'])
        self.assertNotEqual(original,render(cmd,[post('999')])['raw'])

    def test_duplicate_and_conflicting_ids(self):
        cmd=target('@openai');self.assertEqual(render(cmd,[post(),post()])['count'],1)
        with self.assertRaises(AppError):render(cmd,[post(),post(text='冲突')])

    def test_untrusted_author_id_and_oversize_rejected(self):
        for data in ([dict(post(),id='../etc')],[dict(post(),author={'screenName':'bad/author'})],[dict(post(),text='x'*30001)],[post()]*21,{'tweets':[]}):
            with self.assertRaises(AppError):render(target('@openai'),data)

    def test_render_keeps_links_and_qualifies_empty_results(self):
        cmd=target('种菜',True);out=render(cmd,[post()]);self.assertIn(b'https://x.com/test_account/status/123',out['raw'])
        self.assertIn('不代表',render(cmd,[])['raw'].decode());self.assertIn('search?q=',out['url'])

    def test_no_connection_does_not_attempt_browser(self):
        with patch('personal_agent.twitter.subprocess.Popen') as process:
            with self.assertRaises(AppError):invoke(self.c,'user','openai')
            process.assert_not_called()

    def test_connection_explicit_profile_and_no_write_commands(self):
        with patch('personal_agent.twitter.invoke',return_value={'authenticated':True}):
            result=connect(self.c,'edge','Profile 2')
        self.assertTrue(result['authenticated']);self.assertEqual(settings(self.c)['profile'],'Profile 2')
        for mode in ('post','follow','feed','bookmarks','delete'):
            with self.assertRaises(AppError):invoke(self.c,mode,'text')
        with self.assertRaises(AppError):connect(self.c,'chrome','../Default')

    def test_ego_credentials_are_read_only_from_keychain(self):
        from types import SimpleNamespace
        secret={'auth_token':'a'*40,'ct0':'b'*64}
        out=base64.b64encode(json.dumps(secret).encode())
        with patch('personal_agent.twitter.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=out)) as keychain:
            self.assertEqual(ego_credentials(),secret)
            self.assertEqual(keychain.call_args[0][0][-1],'-w')
            self.assertNotIn(secret['auth_token'],str(keychain.call_args))
        for raw in (b'invalid',base64.b64encode(b'{}'),base64.b64encode(json.dumps(dict(secret,ct0='bad\nvalue')).encode())):
            with patch('personal_agent.twitter.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=raw)):
                with self.assertRaises(AppError) as error:ego_credentials()
                self.assertEqual(error.exception.code,'TWITTER_AUTH')

    def test_ego_connection_never_saves_credentials(self):
        with patch('personal_agent.twitter.invoke',return_value={'authenticated':True}):
            self.assertTrue(connect(self.c,'ego')['authenticated'])
        data=(self.c['state']/'personal-agent/twitter/connection.json').read_text()
        self.assertNotIn('auth_token',data);self.assertNotIn('ct0',data)
        self.assertEqual(settings(self.c)['browser'],'ego')

    def test_backend_uses_only_existing_local_system_proxy(self):
        from types import SimpleNamespace
        with patch.dict('os.environ',{},clear=True),patch('personal_agent.twitter.subprocess.run',return_value=SimpleNamespace(stdout=' HTTPSEnable : 1\n HTTPSProxy : 127.0.0.1\n HTTPSPort : 17891\n')):
            self.assertEqual(backend_environment()['TWITTER_PROXY'],'http://127.0.0.1:17891')
        with patch.dict('os.environ',{},clear=True),patch('personal_agent.twitter.subprocess.run',return_value=SimpleNamespace(stdout=' HTTPSEnable : 1\n HTTPSProxy : untrusted.example\n HTTPSPort : 80\n')):
            self.assertNotIn('TWITTER_PROXY',backend_environment())
        with patch.dict('os.environ',{'TWITTER_PROXY':'http://localhost:1234'},clear=True),patch('personal_agent.twitter.subprocess.run') as system:
            self.assertEqual(backend_environment()['TWITTER_PROXY'],'http://localhost:1234');system.assert_not_called()

    def test_auth_false_is_not_connected(self):
        with patch('personal_agent.twitter.invoke',return_value={'authenticated':False}):
            with self.assertRaises(AppError):status(self.c)

    def test_source_ingestion_refresh_and_watch_preserve_identity(self):
        cmd=self.s.twitter_command('@OpenAI')
        with patch('personal_agent.twitter.invoke',return_value=[post()]):
            first=self.ingest(cmd);sid=first['source_id'];second=self.ingest(self.s.refresh_command(sid))
        self.assertTrue(second['duplicate']);self.assertEqual(first['version'],1)
        self.assertIn('https://x.com/test_account/status/123',self.s.snapshot(sid)['content'])
        w=Watch(self.c,clock=lambda:1000)
        with self.assertRaises(AppError):w.add(sid,60)
        w.add(sid,900)
        with self.assertRaises(AppError):w.control(sid,'resume',60)

    def test_auth_failure_pauses_watch_and_keeps_old_source(self):
        with patch('personal_agent.twitter.invoke',return_value=[post()]):sid=self.ingest(self.s.twitter_command('@OpenAI'))['source_id']
        w=Watch(self.c,clock=lambda:1000);w.add(sid,900);w.control(sid,'check')
        with patch('personal_agent.twitter.invoke',side_effect=AppError('TWITTER_AUTH','请重新登录')):run=w.cycle()
        self.assertEqual(run['error_code'],'TWITTER_AUTH');self.assertFalse(w.get(sid)['enabled']);self.assertEqual(self.s.get(sid)['current_version'],1)

    def test_fetch_rejects_mutated_canonical_before_backend(self):
        cmd=target('@openai');cmd['canonical']='twitter-user:other'
        with patch('personal_agent.twitter.invoke') as native:
            with self.assertRaises(AppError):fetch(self.c,cmd)
            native.assert_not_called()


if __name__=='__main__':unittest.main()
