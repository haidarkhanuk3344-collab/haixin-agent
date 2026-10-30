import concurrent.futures
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from personal_agent import app
from personal_agent.jobs import Store, prepare, verify_artifact
from personal_agent.task_guard import Limits, TaskGuard, task_config, inspect_response


def response(calls=(),usage=True,finish=None):
    data={'choices':[{'index':0,'message':{'content':'完成','tool_calls':[
        {'id':'call_'+str(i),'type':'function','function':{'name':name,'arguments':json.dumps(args)}} for i,(name,args) in enumerate(calls)]},'finish_reason':finish or ('tool_calls' if calls else 'stop')}]}
    if usage:data['usage']={'prompt_tokens':100,'prompt_cache_hit_tokens':20,'completion_tokens':30,'total_tokens':130}
    return 200,'application/json',json.dumps(data).encode()


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        state=self.root/'state';state.mkdir();ws=self.root/'workspace';ws.mkdir()
        original={'agents':{'defaults':{'workspace':str(ws),'model':{'primary':'deepseek-local/deepseek-flash'}}},
                  'models':{'providers':{'deepseek-local':{'baseUrl':'https://api.deepseek.com','models':[{'id':'deepseek-flash','maxTokens':768}]}}}}
        (state/'openclaw.json').write_text(json.dumps(original))
        self.c={'state':state,'price_file':app.ROOT/'config/prices.json'};self.store=Store(self.c)
        self.limits=Limits(self.c);self.original=(state/'openclaw.json').read_bytes()

    def tearDown(self):self.tmp.cleanup()

    def setup_job(self,**limits):
        self.store.create('整理文档',limits=limits);self.job=self.store.claim();self.jid=self.job['id']
        self.directory,_,_=prepare(self.c,self.job)
        self.c.update(_runtime_config=task_config(self.c,self.job,self.directory),_task_id=self.jid,
                      _task_attempt=self.job['attempts'],_task_workspace=str(self.directory))
        return TaskGuard(self.c,'test-secret',forward=lambda _:response())

    def send(self,guard):return guard.exchange(b'{"model":"deepseek-flash"}')

    def assert_code(self,code,fn):
        with self.assertRaises(app.AppError) as ctx:fn()
        self.assertEqual(ctx.exception.code,code)

    def test_correction_usage_is_delta_but_limits_remain_cumulative(self):
        first=self.setup_job(model_requests=3);self.send(first)
        second=TaskGuard(self.c,'test-secret',forward=lambda _:response());self.send(second)
        delta=self.limits.metrics(self.jid,1,second.request_offset)
        self.assertEqual(delta['usage']['total'],130)
        self.assertEqual(delta['model_requests_used'],2)
        self.assertEqual(self.limits.metrics(self.jid,1)['usage']['total'],260)

    def test_request_cap_before_forward_and_across_manual_retry(self):
        g=self.setup_job(model_requests=1);self.send(g)
        self.assert_code('BUDGET_MODEL',lambda:self.send(g))
        self.store.finish(self.jid,'failed');self.store.resume(self.jid,True);self.store.claim()
        self.assert_code('BUDGET_MODEL',lambda:self.limits.reserve(self.jid,2))
        self.assertEqual(self.limits.metrics(self.jid,1)['model_requests_used'],1)

    def test_concurrent_reservations_do_not_exceed_cap(self):
        self.setup_job(model_requests=2)
        def reserve(_):
            try:return self.limits.reserve(self.jid,1)
            except app.AppError:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool: results=list(pool.map(reserve,range(12)))
        self.assertEqual(sum(x is not None for x in results),2)

    def test_tool_batch_over_cap_not_forwarded_known_usage_retained(self):
        g=self.setup_job(tool_calls=1);g.forward=lambda _:response([('read',{'path':'AGENTS.md'}),('write',{'path':'result.pending.md','content':'x'})])
        self.assert_code('BUDGET_TOOLS',lambda:self.send(g))
        m=self.limits.metrics(self.jid,1);self.assertEqual(m['tool_calls_admitted'],0);self.assertEqual(m['usage']['total'],130)

    def test_tools_accumulate_across_requests(self):
        g=self.setup_job(tool_calls=1);g.forward=lambda _:response([('read',{'path':'AGENTS.md'})]);self.send(g)
        self.assert_code('BUDGET_TOOLS',lambda:self.send(g))
        self.assertEqual(self.limits.metrics(self.jid,1)['usage']['total'],260)

    def test_absolute_and_relative_current_paths_allowed(self):
        g=self.setup_job();g.forward=lambda _:response([('read',{'file_path':str(self.directory/'AGENTS.md')}),('write',{'path':'result.pending.md','content':'文档'})])
        self.send(g);self.assertEqual(self.limits.metrics(self.jid,1)['tool_calls_admitted'],2)

    def test_outside_path_denied(self):
        g=self.setup_job();g.forward=lambda _:response([('read',{'path':'../../other.md'})])
        self.assert_code('TOOL_PATH',lambda:self.send(g))

    def test_input_write_denied(self):
        g=self.setup_job();g.forward=lambda _:response([('edit',{'path':'inputs/1.md'})])
        self.assert_code('TOOL_WRITE',lambda:self.send(g))

    def test_network_tool_denied(self):
        g=self.setup_job();g.forward=lambda _:response([('web_fetch',{'url':'https://example.com'})])
        self.assert_code('TOOL_DENIED',lambda:self.send(g))

    def test_conflicting_path_aliases_denied(self):
        g=self.setup_job();g.forward=lambda _:response([('read',{'path':'AGENTS.md','file_path':'../../private'})])
        self.assert_code('TOOL_PATH',lambda:self.send(g))

    def test_policy_rejection_is_not_auth_error(self):
        g=self.setup_job(tool_calls=0);g.forward=lambda _:response([('read',{'path':'AGENTS.md'})])
        with g:
            conn=http.client.HTTPConnection('127.0.0.1',g.server.server_port,timeout=3)
            conn.request('POST','/v1/chat/completions',body=b'{"model":"deepseek-flash"}',headers={'Authorization':'Bearer '+g.token})
            r=conn.getresponse();body=json.loads(r.read());conn.close()
            self.assertEqual(r.status,400);self.assertEqual(body['error']['type'],'invalid_request_error')
            self.assertEqual(g.blocked['code'],'BUDGET_TOOLS')

    def test_symlink_inside_workspace_denied(self):
        g=self.setup_job();(self.directory/'link.md').symlink_to(self.directory/'AGENTS.md')
        g.forward=lambda _:response([('read',{'path':'link.md'})])
        self.assert_code('TOOL_PATH',lambda:self.send(g))

    def test_unknown_usage_stops_tools_and_new_calls(self):
        g=self.setup_job();g.forward=lambda _:response([('read',{'path':'AGENTS.md'})],usage=False)
        self.assert_code('GUARD_USAGE_UNKNOWN',lambda:self.send(g))
        self.assert_code('GUARD_USAGE_UNKNOWN',lambda:self.limits.reserve(self.jid,1))
        m=self.limits.metrics(self.jid,1);self.assertIsNone(m['usage']);self.assertEqual(m['tool_calls_admitted'],0)

    def test_truncated_response_stops_tools_preserves_usage(self):
        g=self.setup_job();g.forward=lambda _:response([('write',{'path':'result.pending.md'})],finish='length')
        self.assert_code('OUTPUT_TRUNCATED',lambda:self.send(g))
        self.assertEqual(self.limits.metrics(self.jid,1)['usage']['total'],130)

    def test_upstream_failure_reserved_unknown_and_not_retried(self):
        g=self.setup_job();g.forward=lambda _:(503,'application/json',b'private-provider-error')
        self.assert_code('PROVIDER_HTTP',lambda:self.send(g));self.assert_code('PROVIDER_HTTP',lambda:self.send(g))
        self.assertEqual(self.limits.metrics(self.jid,1)['model_requests_used'],1)

    def test_token_model_and_advertised_tools_restricted(self):
        g=self.setup_job(output_tokens=512);requests=[]
        def forward(body):requests.append(json.loads(body));return response()
        g.forward=forward
        g.exchange(json.dumps({'model':g.model,'max_tokens':999999,'n':5,'tools':[{'function':{'name':'exec'}},{'function':{'name':'read'}}]}).encode())
        self.assertEqual(requests[0]['max_tokens'],512);self.assertEqual(requests[0]['n'],1)
        self.assertEqual([t['function']['name'] for t in requests[0]['tools']],['read'])
        self.assert_code('PROVIDER_MODEL',lambda:g.exchange(b'{"model":"different"}'))

    def test_real_loopback_auth_path_nonce_and_cleanup(self):
        g=self.setup_job()
        with g:
            cfg=json.loads(g.config_path.read_text());self.assertNotIn('test-secret',g.config_path.read_text())
            self.assertIn('127.0.0.1',cfg['models']['providers'][g.provider]['baseUrl'])
            def post(token,path='/v1/chat/completions'):
                conn=http.client.HTTPConnection('127.0.0.1',g.server.server_port,timeout=3)
                conn.request('POST',path,body=b'{"model":"deepseek-flash"}',headers={'Authorization':'Bearer '+token})
                r=conn.getresponse();r.read();conn.close();return r.status
            self.assertEqual(post('bad'),401);self.assertEqual(post(g.token,'/wrong'),400)
            self.assertEqual(self.limits.metrics(self.jid,1)['model_requests_used'],0)
            self.assertEqual(post(g.token),200)
        self.assert_code('GUARD_STOPPED',lambda:self.send(g))
        self.assertEqual((self.c['state']/'openclaw.json').read_bytes(),self.original)

    def test_usage_survives_new_process(self):
        g=self.setup_job();self.send(g)
        command='from pathlib import Path;from personal_agent.task_guard import Limits;import json;print(json.dumps(Limits({"state":Path('+repr(str(self.c['state']))+')}).metrics('+repr(self.jid)+',1)))'
        result=subprocess.run([sys.executable,'-c',command],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)['usage']['total'],130)

    def test_cancel_prevents_new_upstream_request(self):
        g=self.setup_job();self.store.cancel(self.jid)
        self.assert_code('TASK_INACTIVE',lambda:self.send(g))
        self.assertEqual(self.limits.metrics(self.jid,1)['model_requests_used'],0)

    def test_cancel_while_model_running_prevents_tool_release(self):
        g=self.setup_job()
        def forward(_):
            self.store.cancel(self.jid)
            return response([('write',{'path':'result.pending.md','content':'x'})])
        g.forward=forward;self.assert_code('TASK_INACTIVE',lambda:self.send(g))
        m=self.limits.metrics(self.jid,1);self.assertEqual(m['tool_calls_admitted'],0);self.assertEqual(m['usage']['total'],130)

    def test_long_document_acceptance_rejects_short_and_missing_sections(self):
        self.setup_job(min_chars=100,required_sections=['总结','来源'])
        p=self.directory/'result.pending.md';p.write_text('# 总结\n太短')
        self.assert_code('ARTIFACT_SHORT',lambda:verify_artifact(self.c,self.directory,self.job))
        p.write_text('# 总结\n'+'内容'*100)
        self.assert_code('ARTIFACT_SECTIONS',lambda:verify_artifact(self.c,self.directory,self.job))
        p.write_text('# 总结\n'+'内容'*100+'\n## 来源\n合成资料')
        self.assertTrue(Path(verify_artifact(self.c,self.directory,self.job)['path']).is_file())

    def test_config_failure_closes_listener(self):
        g=self.setup_job()
        with patch('personal_agent.task_guard.atomic_write',side_effect=OSError('disk')):
            with self.assertRaises(OSError):g.__enter__()
        self.assertTrue(g.closed);self.assertEqual(g.server.fileno(),-1)

    def test_private_upstream_route_rejected(self):
        self.setup_job();p=Path(self.c['_runtime_config']);cfg=json.loads(p.read_text());cfg['models']['providers']['deepseek-local']['baseUrl']='https://127.0.0.1';p.write_text(json.dumps(cfg))
        self.assert_code('PROVIDER_ROUTE',lambda:TaskGuard(self.c,'test-secret'))

    def test_stream_fragments_and_usage(self):
        records=[{'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'name':'read','arguments':'{"path":'}}]}}]},
                 {'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':'"AGENTS.md"}'}}]},'finish_reason':'tool_calls'}]},
                 {'choices':[],'usage':{'prompt_tokens':100,'completion_tokens':30,'total_tokens':130}}]
        raw=('\n\n'.join('data: '+json.dumps(r) for r in records)+'\n\ndata: [DONE]\n\n').encode()
        usage,calls,truncated=inspect_response(raw,'text/event-stream')
        self.assertEqual(usage['total'],130);self.assertEqual(json.loads(calls[0]['arguments']),{'path':'AGENTS.md'});self.assertFalse(truncated)
        self.assert_code('PROVIDER_TRUNCATED',lambda:inspect_response(raw.replace(b'data: [DONE]',b''),'text/event-stream'))


if __name__=='__main__':unittest.main()
