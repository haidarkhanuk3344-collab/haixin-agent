import json,tempfile,unittest,uuid
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo
from personal_agent.affairs import Affairs
from personal_agent.app import AppError
from personal_agent.jobs import Store
class AffairsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name);(p/'ws').mkdir();(p/'openclaw.json').write_text(json.dumps({'agents':{'defaults':{'workspace':str(p/'ws')}}}));self.c={'state':p};self.t=datetime(2026,10,30,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp();self.a=Affairs(self.c,lambda:self.t);self.s=Store(self.c);self.stamp=patch('personal_agent.affairs.now',lambda:datetime.fromtimestamp(self.t,timezone.utc).isoformat());self.stamp.start()
 def tearDown(self):self.stamp.stop();self.tmp.cleanup()
 def job(self,text):return self.s.create(text,kind='chat')
 def create(self):
  j=self.job('明天下午三点我要交方案');r=self.a.act(j,'1','create',kind='schedule',title='交方案',time_iso='2026-10-31T15:00:00+08:00',time_quote='明天下午三点');return j,r['affair']
 def test_default_early_idempotent_and_restart(self):
  j,r=self.create();self.assertEqual(r['due']-r['remind'],1800);self.assertEqual(self.a.act(j,'1','create',kind='schedule',title='交方案')['affair']['id'],r['id'])
  self.t=r['remind']-1;self.assertEqual(self.a.release(),0);self.t+=1;self.assertEqual(self.a.release(),1);self.assertEqual(self.a.release(),0);self.assertEqual(Affairs(self.c,lambda:self.t).release(),0);self.assertEqual(len(self.a.notices()),1);self.assertLess(self.a.notices()[0]['created'],r['due'])
 def test_change_cancels_old_due_and_complete_cancels_new(self):
  j,r=self.create();j2=self.job('改到后天下午四点')
  r2=self.a.act(j2,'1','update',identity=r['id'],version=r['version'],time_iso='2026-11-01T16:00:00+08:00',time_quote=j2['goal'])['affair']
  self.t=r['remind'];self.assertEqual(self.a.release(),0)
  self.a.act(self.job('这件事已完成'),'1','complete',identity=r['id'],version=r2['version']);self.t=r2['due'];self.assertEqual(self.a.release(),0)
 def test_note_and_undated_todo_need_no_deadline(self):
  for k in ('note','todo'):
   r=self.a.act(self.job('记一下展览用暖光'),'1','create',kind=k,title='展览用暖光')['affair'];self.assertIsNone(r['due']);self.assertIsNone(r['remind'])
 def test_ambiguous_hypothetical_past_and_wrong_relative_date(self):
  for text,iso in [('明天要开会','2026-10-31T00:00:00+08:00'),('假设明天下午三点开会','2026-10-31T15:00:00+08:00'),('明天下午三点开会','2026-11-01T15:00:00+08:00'),('今天上午十点开会','2026-10-30T10:00:00+08:00')]:
   with self.assertRaises(AppError):self.a.act(self.job(text),'1','create',kind='schedule',title='开会',time_iso=iso,time_quote=text)
 def test_explicit_lead_and_late_recovery(self):
  j=self.job('后天下午三点开会，提前一天提醒');r=self.a.act(j,'1','create',kind='schedule',title='开会',time_iso='2026-11-01T15:00:00+08:00',time_quote='后天下午三点',reminder_minutes=1440)['affair'];self.assertEqual(r['due']-r['remind'],86400)
  self.t=r['due']+300;self.assertEqual(self.a.release(),1);self.assertIn('迟到',self.a.notices()[0]['body']);self.assertEqual(self.a.release(),0)
 def test_explicit_timezone_uses_its_local_relative_date(self):
  text='明天纽约上午十点开会';r=self.a.act(self.job(text),'tz','create',kind='schedule',title='纽约开会',time_iso='2026-10-31T10:00:00-04:00',time_quote=text,timezone='America/New_York')['affair']
  self.assertEqual(r['timezone'],'America/New_York');self.assertEqual(r['due']-r['remind'],1800)
  with self.assertRaises(AppError):self.a.act(self.job('明天十点开会'),'bad','create',kind='schedule',title='开会',time_iso='2026-10-31T10:00:00-04:00',time_quote='明天十点',timezone='America/New_York')
 def test_stale_revision_rejected(self):
  j,r=self.create();self.a.act(self.job('不用再提醒'),'1','stop_reminding',identity=r['id'],version=1)
  with self.assertRaises(AppError):self.a.act(self.job('完成'),'1','complete',identity=r['id'],version=1)
 def test_forgetting_does_not_cancel_real_affair_but_stops_context_use(self):
  from personal_agent.life_memory import LifeMemory
  j,r=self.create();LifeMemory(self.c).forget(query='方案');self.t=r['due'];self.assertEqual(self.a.release(),0);self.assertEqual(self.a.list(),[])
  with self.s.db() as db:self.assertEqual(db.execute('SELECT status FROM affairs').fetchone()[0],'active')
if __name__=='__main__':unittest.main()
