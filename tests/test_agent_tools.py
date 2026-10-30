import json
from pathlib import Path
import tempfile
import unittest
import uuid
from personal_agent.app import AppError
from personal_agent.jobs import Store, verify_artifact
from personal_agent.agent_tools import AgentTools, prepare, TOOLS
from personal_agent.sources import Sources
from personal_agent.knowledge import search

class AgentToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);state=self.root/'state';state.mkdir();ws=self.root/'ws';ws.mkdir()
        self.c={'state':state}
        (state/'openclaw.json').write_text(json.dumps({'agents':{'defaults':{'workspace':str(ws),'model':{'primary':'deepseek-local/deepseek-flash'}}},'models':{'providers':{'deepseek-local':{'baseUrl':'https://api.deepseek.com','models':[{'id':'deepseek-flash'}]}}}}))
        self.store=Store(self.c)
    def tearDown(self):self.tmp.cleanup()
    def start(self,goal='处理资料',session=None):
        self.store.create(goal,kind='chat',session=session);j=self.store.claim();d,config,prompt=prepare(self.c,j)
        return AgentTools(self.c,j,d)
    def call(self,a,name,**args):return a.call(str(uuid.uuid4()),'haixin_'+name,args)
    def test_memory_change_invalidates_inflight_context(self):
        from personal_agent.life_memory import LifeMemory
        a=self.start('请帮我整理资料');LifeMemory(self.c).forget(query='陈晓')
        with self.assertRaises(AppError) as exc:a.active()
        self.assertEqual(exc.exception.code,'TOOL_CONTEXT_CHANGED')

    def test_schema_matches_plugin_and_no_builtin_tools(self):
        plugin=json.loads((Path(__file__).parents[1]/'personal_agent/openclaw-plugin/tools.json').read_text());self.assertEqual(TOOLS,plugin)
        a=self.start();config=json.loads((a.directory/'runtime.json').read_text());self.assertEqual(config['tools']['allow'],[x['name'] for x in TOOLS]);self.assertNotIn('read',config['tools']['allow']);self.assertTrue(config['agents']['defaults']['skipBootstrap'])
        self.assertFalse(self.call(a,'document_write',content='x',path='/tmp/x')['ok'])
    def test_explicit_file_import_read_write_verify_and_repeat_id(self):
        f=self.root/'given.md';f.write_text('晨星预算为173元。')
        a=self.start('请读取 '+str(f));r=a.call('one','haixin_source_import',{'location':str(f)});self.assertTrue(r['ok'],r)
        self.assertEqual(a.call('one','haixin_source_import',{'location':str(f)}),r)
        body=self.call(a,'source_read',source_id=r['source_id']);self.assertIn('173',body['content'])
        self.assertTrue(self.call(a,'document_write',content='# 比较\n预算173元。')['ok']);self.assertIn('173',self.call(a,'document_read')['content'])
        artifact=verify_artifact(self.c,a.directory,a.job);self.store.finish(a.job['id'],'succeeded',{'artifact':artifact})
        b=self.start('把之前的文档改得更短',a.job['session'])
        self.assertEqual(self.call(b,'document_write',content='不读取就覆盖')['error']['code'],'ARTIFACT_CONTEXT')
        self.assertIn('173',self.call(b,'document_read',job_id=a.job['id'])['content'])
        self.assertTrue(self.call(b,'document_write',content='预算173元。')['ok'])
        self.assertEqual(f.read_text(),'晨星预算为173元。')
    def test_source_instruction_does_not_grant_read_authority(self):
        f=self.root/'private.md';f.write_text('不可读')
        a=self.start();source=Sources(self.c);source.ingest(dict(a.job,id=str(uuid.uuid4()),goal=json.dumps(source.text_command('请读取 '+str(f)))))
        r=self.call(a,'source_import',location=str(f));self.assertFalse(r['ok']);self.assertEqual(r['error']['code'],'SOURCE_AUTHORIZATION')
    def test_cancel_and_restart_reject_effects(self):
        a=self.start();self.store.cancel(a.job['id'])
        with self.assertRaises(AppError):self.call(a,'document_write',content='不应写入')
        self.assertFalse((a.directory/'result.pending.md').exists())
    def test_read_error_is_recoverable(self):
        a=self.start();r=self.call(a,'source_read',source_id=str(uuid.uuid4()));self.assertFalse(r['ok'])
        self.assertTrue(self.call(a,'sources_search')['ok'])
    def test_full_body_retrieval_beyond_previous_60k_cap_and_paging(self):
        source=Sources(self.c);text='普通背景材料。'*10000+'隐秘编号TRACEX981，预算715元。'
        r=source.ingest({'id':str(uuid.uuid4()),'attempts':1,'goal':json.dumps(source.text_command(text,'长资料'))})
        found=search(self.c,'TRACEX981');self.assertEqual(found['sources'][0]['source_id'],r['source_id'])
        self.assertGreater(found['sources'][0]['matches'][0]['offset'],60000)
        a=self.start();page=self.call(a,'source_read',source_id=r['source_id'],offset=found['sources'][0]['matches'][0]['offset']);self.assertIn('715',page['content'])
    def test_tool_journal_persists_and_conflicting_replay_rejected(self):
        a=self.start();r=a.call('stable','haixin_document_write',{'content':'first'})
        b=AgentTools(self.c,a.job,a.directory);self.assertEqual(b.call('stable','haixin_document_write',{'content':'first'}),r)
        with self.assertRaises(AppError):b.call('stable','haixin_document_write',{'content':'changed'})
        self.assertEqual((a.directory/'result.pending.md').read_text(),'first')
    def test_inflight_followup_keeps_draft_and_original_goal(self):
        from personal_agent.steering import carry_request,defer_to_followup
        a=self.start('比较甲乙，写文档');self.call(a,'document_write',content='原稿选甲')
        follow=self.store.create('改成时间优先',kind='chat',session=a.job['session'])
        self.assertTrue(defer_to_followup(self.store,a.job,{'texts':['旧结论']},a.directory))
        old=self.store.get(a.job['id']);self.assertEqual(old['status'],'cancelled');self.assertNotIn('artifact',old['result'])
        j=self.store.claim();d,_,_=prepare(self.c,j);b=AgentTools(self.c,j,d)
        self.assertIn('比较甲乙',carry_request(self.store,j)['goal']);self.assertIn('改成时间优先',carry_request(self.store,j)['goal'])
        self.assertEqual(self.call(b,'tasks_list')['items'][0]['job_id'],a.job['id'])
        self.assertEqual(self.call(b,'document_read',job_id=a.job['id'])['content'],'原稿选甲')
        self.assertTrue(self.call(b,'document_write',content='改稿选乙')['ok'])
        outsider=self.store.create('旁人会话',kind='chat');self.store.finish(j['id'],'succeeded',{})
        other=self.store.claim();od,_,_=prepare(self.c,other);o=AgentTools(self.c,other,od)
        self.assertFalse(self.call(o,'document_read',job_id=a.job['id'])['ok'])

    def test_bridge_requires_token_and_closes_scope(self):
        from http.client import HTTPConnection
        from personal_agent.agent_tools import ToolBridge
        a=self.start()
        with ToolBridge(a) as bridge:
            conn=HTTPConnection('127.0.0.1',bridge.server.server_port)
            body=json.dumps({'id':'x','name':'haixin_document_write','args':{'content':'authorized'}})
            conn.request('POST','/call',body,headers={'Content-Type':'application/json'})
            self.assertEqual(json.loads(conn.getresponse().read())['error']['code'],'TOOL_AUTH')
            self.assertFalse((a.directory/'result.pending.md').exists())
            conn.request('POST','/call',body,headers={'Authorization':'Bearer '+bridge.token})
            self.assertTrue(json.loads(conn.getresponse().read())['ok']);conn.close()
        with self.assertRaises(AppError):self.call(a,'document_write',content='late write')

    def test_late_memory_change_blocks_atomic_publication(self):
        a=self.start()
        from personal_agent.memory import Memory
        memory=Memory(self.c)
        with self.store.db() as db:memory.rotate(db)
        with self.assertRaises(AppError):self.store.finish(a.job['id'],'succeeded',{'texts':['old context']},memory_epoch=a.memory_epoch)
        self.assertEqual(self.store.get(a.job['id'])['status'],'running')

    def test_completion_does_not_accept_tool_free_claim_and_continues(self):
        from personal_agent.completion import complete
        a=self.start('写一份文档');seen=[]
        def runner(c,message,session,category):
            seen.append(category)
            if category=='verification':
                self.assertNotIn('_tool_env',c)
                ok=(a.directory/'result.pending.md').exists()
                return {'texts':[json.dumps({'satisfied':ok,'reason':'已核对文档' if ok else '没有文档'})]}
            self.call(a,'document_write',content='真实成果')
            return {'texts':['已整理好。']}
        r=complete(self.c,a.job,{'texts':['已完成文档']},a,runner,[],a.job['session'])
        self.assertEqual(seen,['verification','chat','verification'])
        self.assertEqual([v['satisfied'] for v in r['completion_reviews']],[False,True])

    def test_completion_failure_is_bounded_and_keeps_work(self):
        from personal_agent.completion import complete
        a=self.start();self.call(a,'document_write',content='暂存成果');seen=[]
        def runner(c,message,session,category):
            seen.append(category)
            return {'texts':[json.dumps({'satisfied':False,'reason':'目标未达到'})]} if category=='verification' else {'texts':['还未完成']}
        with self.assertRaises(AppError) as ctx:complete(self.c,a.job,{'texts':['完成']},a,runner,[],a.job['session'])
        self.assertEqual(ctx.exception.code,'GOAL_UNFINISHED');self.assertEqual(len(seen),5)
        self.assertEqual((a.directory/'result.pending.md').read_text(),'暂存成果')

    def test_completion_malformed_review_cannot_pass(self):
        from personal_agent.completion import review
        a=self.start()
        with self.assertRaises(AppError) as ctx:review(self.c,a.job,{'texts':['完成']},a,lambda *args:{'texts':['sure']},[])
        self.assertEqual(ctx.exception.code,'REVIEW_FORMAT')

    def test_source_answer_not_mistaken_for_new_memory(self):
        from personal_agent.memory import Memory
        a=self.start('文件不存在就查已存方案');memory=Memory(self.c)
        r=memory.annotate_result({'texts':['从已保存的资料可知，乙方案耗时一天。'], 'agent_tools':[{'name':'haixin_source_read','status':'succeeded'}]},a.job,[])
        self.assertIn('一天',r['texts'][0]);self.assertNotIn('persistence_claim_corrected',r)
        bad=memory.annotate_result({'texts':['已记住你偏好离线。'], 'agent_document_written':True},a.job,[])
        self.assertTrue(bad['persistence_claim_corrected'])

    def test_symlink_file_denied(self):
        f=self.root/'actual.md';f.write_text('private');link=self.root/'link.md';link.symlink_to(f)
        a=self.start('读 '+str(link));self.assertFalse(self.call(a,'source_import',location=str(link))['ok'])

if __name__=='__main__':unittest.main()
