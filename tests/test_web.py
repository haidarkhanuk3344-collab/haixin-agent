import concurrent.futures
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from personal_agent.app import AppError
from personal_agent.web import UI,Server

class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve();state=self.root/'state';state.mkdir();ws=self.root/'workspace';ws.mkdir()
        (state/'openclaw.json').write_text(json.dumps({'agents':{'defaults':{'workspace':str(ws)}}}))
        self.c={'state':state};self.ui=UI(self.c,autostart=False);self.dist=self.root/'dist';self.dist.mkdir();(self.dist/'index.html').write_text('<html>测试</html>')
        self.server=Server(('127.0.0.1',0),self.ui,self.dist);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.temp.cleanup()
    def call(self,path,body=None,headers=None):
        port=self.server.server_port;connection=HTTPConnection('127.0.0.1',port,timeout=3)
        h={'X-Agent-UI':'1','Cookie':'agent_ui='+self.ui.cookie,'Origin':'http://127.0.0.1:'+str(port),'Content-Type':'application/json'};h.update(headers or {})
        connection.request('GET' if body is None else 'POST',path,body=json.dumps(body) if body is not None else None,headers=h)
        r=connection.getresponse();data=r.read();result=(r.status,dict(r.getheaders()),data);connection.close();return result
    def chat(self):return {'id':str(uuid.uuid4()),'action':'chat','text':'这是界面测试','session':str(uuid.uuid4())}
    def test_session_cookie_http_only_and_state_is_real(self):
        code,headers,_=self.call('/');self.assertEqual(code,200);self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        code,_,data=self.call('/api/state');self.assertEqual(code,200);self.assertEqual(json.loads(data)['jobs'],[])
    def test_foreign_origins_hosts_and_missing_auth_rejected(self):
        for h in ({'Origin':'https://evil.example'},{'Host':'evil.example'},{'Cookie':''},{'X-Agent-UI':''},{'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.call('/api/state',headers=h)[0],403)
        self.assertEqual(self.call('/api/action',self.chat(),{'Origin':''})[0],403)
    def test_duplicate_request_creates_exactly_one_job(self):
        b=self.chat()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:results=list(pool.map(lambda _:self.ui.action(b),range(5)))
        self.assertEqual(len({r['result']['job_id'] for r in results}),1);self.assertEqual(len(self.ui.store.list()),1)
        other=dict(b,text='不同内容')
        with self.assertRaises(AppError):self.ui.action(other)
    def test_requests_persist_restart_and_unknown_never_replays(self):
        b=self.chat();one=self.ui.action(b);again=UI(self.c,autostart=False);self.assertEqual(one,again.action(b))
        with again.store.db() as db:db.execute("UPDATE web_requests SET status='reserved',result=NULL WHERE id=?",(b['id'],))
        restart=UI(self.c,autostart=False);self.assertEqual(restart.action(b)['status'],'outcome_unknown');self.assertEqual(len(restart.store.list()),1)
    def test_error_does_not_expose_traceback_or_create_job(self):
        code,_,raw=self.call('/api/action',dict(self.chat(),action='shell'))
        self.assertEqual(code,200);r=json.loads(raw);self.assertEqual(r['status'],'failed');self.assertIn('error',r['result']);self.assertNotIn(b'Traceback',raw);self.assertEqual(self.ui.store.list(),[])
    def test_cancel_is_backend_cancel(self):
        b=self.chat();r=self.ui.action(b);jid=r['result']['job_id'];self.ui.action({'id':str(uuid.uuid4()),'action':'cancel','target':jid});self.assertEqual(self.ui.job(jid)['status'],'cancelled')
    def test_cancelled_scheduled_output_is_not_delivered(self):
        from personal_agent.schedules import Schedules,parse
        clock=[1000.];schedules=Schedules(self.c,clock=lambda:clock[0])
        origin=self.ui.store.create('两秒后提醒我喝水',kind='chat');self.ui.store.claim()
        self.ui.store.finish(origin['id'],'succeeded',schedules.accept(origin,parse(origin['goal'])))
        clock[0]+=2;schedules.release();j=self.ui.store.claim();self.ui.store.cancel(j['id'])
        self.ui.store.finish(j['id'],'succeeded',{'texts':['取消后返回的模型内容']})
        self.assertEqual(self.ui.job(j['id'])['status'],'cancelled');self.assertEqual(self.ui.job(j['id'])['texts'],[])
    def test_path_escape_and_arbitrary_file_download_rejected(self):
        (self.root/'secret.txt').write_text('PRIVATE')
        self.assertEqual(self.call('/../secret.txt')[0],404);self.assertNotIn(b'PRIVATE',self.call('/api/artifact?id=../secret.txt')[2])
    def test_forgotten_memory_requires_explicit_confirmation(self):
        r=self.ui.action({'id':str(uuid.uuid4()),'action':'memory','op':'forget','target':str(uuid.uuid4())})
        self.assertEqual(r['result']['error']['code'],'CONFIRM');self.assertEqual(self.ui.store.list(),[])
    def test_artifact_must_match_verified_hash(self):
        j=self.ui.store.create('测试文档');self.ui.store.claim();directory=self.root/'workspace/tasks'/j['id']/'attempt-1';directory.mkdir(parents=True);path=directory/'result.md';path.write_text('成果')
        self.ui.store.finish(j['id'],'succeeded',{'artifact':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}})
        self.assertEqual(self.ui.artifact(j['id']),'成果'.encode());path.write_text('改变')
        with self.assertRaises(AppError):self.ui.artifact(j['id'])

if __name__=='__main__':unittest.main()
