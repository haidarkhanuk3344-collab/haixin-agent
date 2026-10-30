import json
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from personal_agent.app import AppError
from personal_agent.jobs import Store
from personal_agent.memory import Memory
from personal_agent.proactive import Proactive, TZ, TTL, natural_control
from personal_agent import worker


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        state=self.root/'state'; state.mkdir(); ws=self.root/'workspace'; ws.mkdir()
        (state/'openclaw.json').write_text(json.dumps({'agents':{'defaults':{'workspace':str(ws)}}}))
        self.c={'state':state}; self.t=[datetime(2026,10,29,12,0,tzinfo=TZ).timestamp()]
        self.p=Proactive(self.c,clock=lambda:self.t[0]); self.store=Store(self.c); self.mem=Memory(self.c)
        self.calls=[]
        self.p.control({'action':'enable'})

    def tearDown(self): self.tmp.cleanup()

    def topic(self,text='下次聊阳台种菜的灌溉安排。'):
        job=self.store.create(text,kind='chat',category='chat'); job=self.store.claim()
        ids=self.mem.observe(job); self.store.finish(job['id'],'succeeded',{'texts':['ok']})
        return self.mem.get(ids[0])

    def source(self,text='阳台种菜可以使用滴灌，定时灌溉可减少忘记浇水。'):
        cmd=self.p.sources.text_command(text)
        return self.p.sources.ingest({'id':str(uuid.uuid4()),'attempts':1,'goal':json.dumps(cmd)})

    def runner(self,c,message,session,category):
        self.calls.append((c,message,session,category))
        return {'texts':[json.dumps({'send':True,'message':'材料里提到滴灌，想先试哪种安排？'},ensure_ascii=False)],
                'tools':[],'usage':{'input':1,'output':1,'cacheRead':0,'cacheWrite':0,'total':2},'estimated_usd':0.000001}

    def cycle(self,runner=None): self.p.cycle(runner or self.runner,0,lambda:None)

    def material(self):
        m=self.topic(); s=self.source(); self.cycle()
        return m,s,self.p.inbox()[0]

    def test_default_off_and_no_empty_model_calls(self):
        self.p.control({'action':'disable'}); self.topic(); self.source(); self.cycle()
        self.assertEqual(self.calls,[])
        self.assertIn('DISABLED',[c['reason'] for c in self.p.candidates()])
        self.assertIsNone(self.p.settings()['daily_limit'])

    def test_related_material_question_and_duplicate_event(self):
        m,s,i=self.material(); self.assertEqual(len(self.calls),1)
        self.assertEqual(self.p.read(i['id'])['context']['source']['source_id'],s['source_id'])
        self.source(); self.cycle(); self.cycle()
        self.assertEqual(len(self.calls),1); self.assertEqual(len(self.p.inbox()),1)
        self.assertEqual(self.calls[0][3],'proactive')
        cfg=json.loads(Path(self.calls[0][0]['_runtime_config']).read_text())
        self.assertEqual(cfg['tools']['deny'],['*'])
        self.assertIn('阳台种菜',self.calls[0][1])

    def test_unrelated_source_is_suppressed_without_model(self):
        self.topic(); self.source('交响乐的乐章和配器。'); self.cycle()
        self.assertEqual(self.calls,[])
        self.assertIn('UNRELATED',[c['reason'] for c in self.p.candidates()])

    def test_same_topic_read_but_unanswered_not_repeated(self):
        m,s,i=self.material(); self.p.read(i['id']); self.source('阳台种菜灌溉还可以使用棉绳。'); self.cycle()
        self.assertEqual(len(self.calls),1)
        self.assertIn('AWAITING_REPLY',[c['reason'] for c in self.p.candidates()])

    def test_daily_limit_reservation_and_next_day(self):
        self.p.control({'action':'set','daily_limit':1}); m,s,i=self.material()
        self.p.feedback(i['id'],'interested'); self.source('阳台种菜灌溉还可以使用棉绳。'); self.cycle()
        self.assertEqual(len(self.calls),1); self.assertIn('DAILY_LIMIT',[c['reason'] for c in self.p.candidates()])
        self.t[0]+=86400; self.cycle(); self.assertEqual(len(self.calls),2)

    def test_quiet_spans_midnight_and_releases(self):
        self.p.control({'action':'set','quiet':'23:00-09:00'}); self.topic(); self.source()
        for hour in (23,0,8):
            self.t[0]=datetime(2026,10,29,hour,30,tzinfo=TZ).timestamp(); self.cycle()
        self.assertEqual(self.calls,[])
        self.t[0]=datetime(2026,10,30,9,0,tzinfo=TZ).timestamp(); self.cycle()
        self.assertEqual(len(self.calls),1)

    def test_interval_and_pause_rechecked(self):
        self.p.control({'action':'set','interval':7200}); m,s,i=self.material(); self.p.feedback(i['id'],'interested')
        self.source('阳台种菜灌溉与盆栽大小有关。'); self.cycle(); self.assertEqual(len(self.calls),1)
        self.t[0]+=7200; self.p.control({'action':'pause','hours':1}); self.cycle(); self.assertEqual(len(self.calls),1)
        self.t[0]+=3600; self.cycle(); self.assertEqual(len(self.calls),2)

    def test_feedback_cooldown_and_permanent_topic_stop(self):
        m,s,i=self.material(); self.p.feedback(i['id'],'later'); self.source('阳台种菜灌溉也要考虑天气。'); self.cycle()
        self.assertEqual(len(self.calls),1); self.assertIn('TOPIC_COOLDOWN',[c['reason'] for c in self.p.candidates()])
        self.p.feedback(i['id'],'stop-topic'); self.t[0]+=2*86400; self.cycle(); self.assertEqual(len(self.calls),1)
        self.p.unmute(m['id']); self.cycle(); self.assertEqual(len(self.calls),2)

    def test_expired_after_wake_does_not_burst(self):
        self.topic(); self.source(); self.p.discover(); self.t[0]+=TTL+3*86400; self.cycle()
        self.assertEqual(self.calls,[]); self.assertIn('expired',[c['status'] for c in self.p.candidates()])

    def test_scheduled_followup_persists_and_uses_real_due(self):
        m=self.topic(); r=self.p.followup(m['id'],2); self.cycle(); self.assertEqual(self.calls,[])
        self.p=Proactive(self.c,clock=lambda:self.t[0]); self.t[0]+=2; self.cycle(); self.assertEqual(len(self.calls),1)
        self.assertEqual(self.p.inbox()[0]['candidate_id'],r['candidate_id'])
        self.assertIn('"explicitly_scheduled": true',self.calls[0][1])

    def test_inflight_disable_prevents_publish_then_retains_draft(self):
        self.topic(); self.source()
        def disabling(*args):
            self.p.control({'action':'disable'}); return self.runner(*args)
        self.cycle(disabling); self.assertEqual(self.p.inbox(),[])
        self.assertIn('ready',[c['status'] for c in self.p.candidates()])
        self.p.control({'action':'enable'}); self.cycle()
        self.assertEqual(len(self.p.inbox()),1); self.assertEqual(len(self.calls),1)

    def test_malformed_model_result_no_retry(self):
        self.topic(); self.source()
        def bad(*args):
            r=self.runner(*args); r['texts']=['not-json']; return r
        self.cycle(bad); self.cycle(bad)
        self.assertEqual(len(self.calls),1); self.assertEqual(self.p.inbox(),[])
        self.assertIn('PROACTIVE_FORMAT',[c['reason'] for c in self.p.candidates()])

    def test_model_can_decline_without_retry(self):
        self.topic(); self.source()
        def decline(*args):
            r=self.runner(*args); r['texts']=['{"send": false, "message": ""}']; return r
        self.cycle(decline); self.cycle(decline)
        self.assertEqual(len(self.calls),1); self.assertEqual(self.p.inbox(),[])
        self.assertIn('MODEL_DECLINED',[c['reason'] for c in self.p.candidates()])

    def test_unknown_usage_halts_new_calls_until_ack(self):
        self.topic(); self.source()
        def unknown(*args):
            r=self.runner(*args); r['usage']=None; return r
        self.cycle(unknown); self.cycle()
        self.assertEqual(self.p.settings()['halt_reason'],'UNKNOWN_USAGE'); self.assertEqual(len(self.calls),1)
        self.p.control({'action':'acknowledge-unknown'}); self.cycle()
        self.assertEqual(len(self.p.inbox()),1); self.assertEqual(len(self.calls),1)

    def test_interrupted_reservation_recovered_not_reissued(self):
        self.topic(); self.source(); self.p.discover(); c=self.p.select()
        self.assertEqual(c['status'],'generating')
        self.p=Proactive(self.c,clock=lambda:self.t[0]); self.p.recover(); self.cycle()
        self.assertEqual(self.calls,[]); self.assertEqual(self.p.settings()['halt_reason'],'UNKNOWN_USAGE')
        self.assertIn('outcome_unknown',[c['status'] for c in self.p.candidates()])

    def test_timeout_records_unknown_and_stops_automatic_calls(self):
        self.topic(); self.source()
        self.cycle(lambda *args:AppError('OUTCOME_UNKNOWN','已超时').result())
        self.assertIn('outcome_unknown',[c['status'] for c in self.p.candidates()])
        self.assertEqual(self.p.settings()['halt_reason'],'UNKNOWN_USAGE')
        self.cycle(); self.assertEqual(self.calls,[])

    def test_unknown_price_stops_new_automatic_calls(self):
        self.topic(); self.source()
        def unpriced(*args):
            r=self.runner(*args); r['estimated_usd']=None; return r
        self.cycle(unpriced); self.cycle()
        self.assertEqual(self.p.settings()['halt_reason'],'UNKNOWN_COST')
        self.assertEqual(len(self.calls),1)

    def test_forget_scrubs_inbox_candidate_and_blocks_reply(self):
        m,s,i=self.material(); self.mem.control({'action':'forget','id':m['id']},None)
        rows=self.p.inbox(); self.assertIsNone(rows[0]['body']); self.assertEqual(rows[0]['status'],'revoked')
        self.assertTrue(all(c['body'] is None for c in self.p.candidates()))
        with self.assertRaises(AppError): self.p.reply(i['id'],'继续说')
        with self.assertRaises(AppError): self.p.task(i['id'],'整理文档')
        self.mem.control({'action':'rebuild'},None); self.cycle(); self.assertEqual(len(self.calls),1)

    def test_disable_memory_revokes_and_enable_does_not_resurrect(self):
        m,s,i=self.material(); self.mem.control({'action':'disable','kind':'topic'},None)
        self.assertIsNone(self.p.inbox()[0]['body'])
        self.mem.control({'action':'enable','kind':'topic'},None); self.cycle(); self.assertEqual(len(self.calls),1)

    def test_correction_revokes_old_context(self):
        m,s,i=self.material(); job=self.store.create('更正',kind='memory',category='memory'); job=self.store.claim()
        self.mem.control({'action':'correct','id':m['id'],'content':'待续话题：室内花卉照明'},job)
        self.store.finish(job['id'],'succeeded',{})
        with self.assertRaises(AppError): self.p.read(i['id'])
        self.assertIsNone(self.p.inbox()[0]['body'])

    def test_reply_only_user_text_eligible_for_memory(self):
        m,s,i=self.material(); job=self.p.reply(i['id'],'我喜欢喝普洱茶。'); job=self.store.claim()
        saved=self.mem.observe(job); self.assertEqual(self.mem.get(saved[0])['content'],'用户喜欢喝普洱茶。')
        self.assertEqual(len(self.mem.list()),2)
        self.p.replied(job['id']); self.assertEqual(self.p.inbox()[0]['feedback'],'replied')

    def test_followon_task_pins_original_source_version(self):
        m,s,i=self.material(); job=self.p.task(i['id'],'整理种菜计划')
        private=self.store.get(job['id'],private=True)
        self.assertEqual(private['inputs'][0]['source_id'],s['source_id'])
        self.assertEqual(private['inputs'][0]['version'],1)
        self.assertIn('滴灌',private['inputs'][0]['content'])

    def test_task_status_notice_without_model_or_duplicate(self):
        j=self.store.create('工作任务'); self.store.claim(); self.store.finish(j['id'],'failed',error='synthetic')
        self.cycle(); self.cycle(); self.assertEqual(self.calls,[])
        self.assertEqual(len(self.p.inbox()),1); self.assertIn('失败',self.p.inbox()[0]['body'])

    def test_task_notice_records_event_attempt_not_later_retry_state(self):
        j=self.store.create('工作任务'); self.store.claim(); self.store.finish(j['id'],'failed',error='synthetic')
        self.p.discover(); self.store.resume(j['id'],True)
        candidate=next(c for c in self.p.candidates() if c['kind']=='task')
        self.p.publish(candidate)
        self.assertIn('第1次执行：失败',self.p.inbox()[0]['body'])

    def test_settings_changes_are_audited(self):
        self.p.control({'action':'set','daily_limit':4})
        with self.store.db() as db:
            rows=db.execute('SELECT action,value FROM proactive_settings_log ORDER BY rowid').fetchall()
        self.assertEqual(rows[-1]['action'],'set'); self.assertEqual(json.loads(rows[-1]['value'])['daily_limit'],4)

    def test_user_queue_has_priority(self):
        self.topic(); self.source(); self.store.create('用户消息',kind='chat',category='chat'); self.cycle()
        self.assertEqual(self.calls,[])

    def test_settings_validation_and_natural_pause(self):
        for command in ({'action':'set','daily_limit':-1},{'action':'set','quiet':'99:00-00:00'},{'action':'set','interval':-1},{'action':'pause','hours':0}):
            with self.assertRaises(AppError): self.p.control(command)
        r=worker.send_chat(self.c,'别催我'); self.assertTrue(r['proactive_control'])
        self.assertEqual(natural_control('文章说别催我'),None)
        self.assertEqual(self.store.list(),[])

    def test_natural_resume_clears_pause_and_starts_worker(self):
        self.p.control({'action':'pause','hours':24}); self.p.control({'action':'disable'})
        with patch('personal_agent.worker.start') as start:
            result=worker.send_chat(self.c,'恢复主动交流')
        self.assertTrue(result['settings']['enabled']); self.assertEqual(result['settings']['pause_until'],0)
        start.assert_called_once_with(self.c)

    def test_deleted_journal_restore_also_scrubs_old_inbox(self):
        m,s,i=self.material()
        backup=self.root/'before.sqlite'; source=sqlite3.connect(str(self.store.path)); dest=sqlite3.connect(str(backup)); source.backup(dest); source.close(); dest.close()
        self.mem.control({'action':'forget','id':m['id']},None)
        self.store.path.write_bytes(backup.read_bytes())
        with self.assertRaises(AppError): self.p.inbox()
        self.mem.replay_deletions()
        self.assertIsNone(self.p.inbox()[0]['body'])
        self.assertTrue(all(c['status']=='revoked' for c in self.p.candidates()))


if __name__=='__main__': unittest.main()
