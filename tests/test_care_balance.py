import json,time,unittest,uuid
from unittest.mock import patch
from tests import test_affairs
from personal_agent.balance import Balance
from personal_agent.care import Care
from personal_agent.sources import Sources
from personal_agent.life_memory import LifeMemory
from personal_agent.app import AppError
class CareBalanceTests(unittest.TestCase):
 setUp=test_affairs.AffairsTests.setUp
 tearDown=test_affairs.AffairsTests.tearDown
 create=test_affairs.AffairsTests.create
 def job(self,text):
  j=self.s.create(text,kind='chat');self.s.claim();self.s.finish(j['id'],'succeeded',{});return j
 def test_balance_unknown_insufficient_stale_recharge(self):
  b=Balance(self.c,lambda:self.t,lambda:(_ for _ in ()).throw(ValueError()))
  with self.assertRaises(AppError):b.admit()
  self.assertEqual(b.cached()['status'],'unknown')
  b.fetch=lambda:{'is_available':False,'balance_infos':[{'currency':'CNY','total_balance':'0','granted_balance':'0','topped_up_balance':'0'}]};b.refresh()
  with self.assertRaises(AppError):b.admit()
  j=self.job('等待模型处理')
  with self.s.db() as db:db.execute("UPDATE jobs SET status='waiting_balance',attempts=1 WHERE id=?",(j['id'],))
  b.fetch=lambda:{'is_available':True,'balance_infos':[{'currency':'CNY','total_balance':'12.30','granted_balance':'0','topped_up_balance':'12.30'}]};b.refresh();self.assertEqual(b.resume_waiting(),1);self.assertEqual(self.s.get(j['id'])['attempts'],0)
  self.t+=61;self.assertEqual(b.cached()['status'],'stale');self.assertEqual(b.admit()['status'],'available')
 def test_reminder_runs_without_model_balance(self):
  j,r=self.create();b=Balance(self.c,lambda:self.t,lambda: (_ for _ in ()).throw(ValueError()));b.refresh();self.t=r['remind'];self.assertEqual(self.a.release(),1)
 def test_care_uses_same_durable_loop_and_pause_gate(self):
  care=Care(self.c,lambda:self.t);care.proactive.control({'action':'enable'});j,r=self.create();self.t=r['due']-86399
  jid=care.queue();self.assertIsNotNone(jid);job=self.s.get(jid);self.assertEqual(job['kind'],'care');self.assertEqual(job['session'],j['session']);self.assertTrue(care.allowed(jid))
  self.assertIsNone(care.queue());care.proactive.control({'action':'pause','hours':1});self.assertFalse(care.allowed(jid))
 def test_completed_affair_revokes_pending_care(self):
  care=Care(self.c,lambda:self.t);care.proactive.control({'action':'enable'});j,r=self.create();self.t=r['due']-86399;jid=care.queue();self.assertIsNotNone(jid)
  self.a.act(self.job('已完成'),'1','complete',identity=r['id'],version=r['version']);self.assertFalse(care.allowed(jid));care.discover()
  with self.s.db() as db:self.assertEqual(db.execute('SELECT status FROM care_events WHERE job_id=?',(jid,)).fetchone()[0],'revoked')
 def test_no_event_no_model_job_and_no_permission_escalation(self):
  care=Care(self.c,lambda:self.t);care.proactive.control({'action':'enable'});self.assertIsNone(care.queue())
 def test_memory_forget_revokes_queued_context(self):
  care=Care(self.c,lambda:self.t);care.proactive.control({'action':'enable'});m=care.life;j=self.job('我决定在苏州办展览，因为离家近。');e={'quote':j['goal'],'kind':'decision','scope':'long','certainty':'stated','subject':'展览','target':'new','occurred':'','expires':None};mid=m.save(e,j)
  self.t=time.time()+86500;jid=care.queue();self.assertIsNotNone(jid);m.forget(ids=[mid]);self.assertFalse(care.allowed(jid));self.assertNotIn('苏州',self.s.get(jid)['goal'])
if __name__=='__main__':unittest.main()
