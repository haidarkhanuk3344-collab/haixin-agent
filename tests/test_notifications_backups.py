import json,unittest,time
from pathlib import Path
from tests import test_affairs
from personal_agent.notifications import ReminderService
from personal_agent.backups import Backups
from personal_agent.life_memory import LifeMemory
class FakeNative:
 def __init__(self):self.calls=[]
 def invoke(self,action,**args):self.calls.append((action,args));return {'status':'delivered' if action=='deliver' else 'removed' if action=='remove' else 'authorized','delivered_ids':[]}
class PersistenceTests(unittest.TestCase):
 setUp=test_affairs.AffairsTests.setUp
 tearDown=test_affairs.AffairsTests.tearDown
 job=test_affairs.AffairsTests.job
 create=test_affairs.AffairsTests.create
 def test_batched_notifications_and_removal(self):
  j,r=self.create();self.a.act(self.job('明天下午三点要联系画廊'),'1','create',kind='schedule',title='联系画廊',time_iso='2026-10-31T15:00:00+08:00',time_quote='明天下午三点')
  self.t=r['remind']+100;self.a.release();native=FakeNative();service=ReminderService(self.c,native);service.deliver();self.assertEqual(len(native.calls),1);self.assertIn('2件事',native.calls[0][1]['body']);service.deliver();self.assertEqual(len(native.calls),1)
  self.a.act(self.job('取消交方案'),'1','cancel',identity=r['id'],version=1);service.cleanup();self.assertEqual(native.calls[-1][0],'remove')
 def test_old_snapshot_does_not_send_previously_delivered_banner_again(self):
  j,r=self.create();self.t=r['remind'];self.a.release();b=Backups(self.c);snapshot=b.create()
  native=FakeNative();ReminderService(self.c,native).deliver();self.assertEqual(len(native.calls),1)
  b.restore(snapshot);restored=FakeNative();ReminderService(self.c,restored).deliver();self.assertEqual(restored.calls,[])
 def test_restore_clears_old_process_ownership(self):
  b=Backups(self.c)
  with self.s.db() as db:db.execute("INSERT OR REPLACE INTO worker VALUES(1,?,?,?,0)",('old',123,time.time()))
  snapshot=b.create();b.restore(snapshot)
  with self.s.db() as db:self.assertIsNone(db.execute('SELECT * FROM worker').fetchone())
 def test_notification_interruption_is_not_blindly_replayed(self):
  j,r=self.create();self.t=r['remind'];self.a.release()
  with self.s.db() as db:db.execute("UPDATE affair_notices SET mac_state='sending'")
  native=FakeNative();service=ReminderService(self.c,native);service.recover();service.deliver();self.assertEqual([x[0] for x in native.calls],['status']);self.assertEqual(self.a.notices()[0]['mac_state'],'outcome_unknown')
 def test_full_backup_restore_replays_latest_forget(self):
  m=LifeMemory(self.c,lambda:self.t);j=self.job('陈晓是我的同事。');e=dict(quote=j['goal'],kind='relationship',scope='long',certainty='stated',subject='陈晓',target='new',occurred='',expires=None);mid=m.save(e,j)
  b=Backups(self.c,lambda:self.t);snapshot=b.create();m.forget(ids=[mid]);result=b.restore(snapshot);self.assertTrue(result['deletions_replayed']);self.assertEqual(m.search('陈晓'),[]);self.assertEqual(self.s.get(j['id'])['goal'],j['goal']);self.assertTrue(Path(result['safety_backup']).exists())
 def test_only_owned_30day_backups_pruned(self):
  b=Backups(self.c,lambda:self.t);snapshot=b.create();other=b.root/'user-folder';other.mkdir();(other/'keep.txt').write_text('保留')
  self.t+=31*86400;self.assertIn(snapshot.name,b.prune());self.assertTrue((other/'keep.txt').exists())
if __name__=='__main__':unittest.main()
