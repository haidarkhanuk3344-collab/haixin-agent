import json,tempfile,unittest,uuid,shutil
from pathlib import Path
from personal_agent.app import AppError
from personal_agent.jobs import Store
from personal_agent.memory import Memory
from personal_agent.life_memory import LifeMemory

class LifeMemoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.c={'state':self.root};self.t=1800000000
  (self.root/'ws').mkdir();(self.root/'openclaw.json').write_text(json.dumps({'agents':{'defaults':{'workspace':str(self.root/'ws')}}}))
  self.store=Store(self.c);self.legacy=Memory(self.c);self.m=LifeMemory(self.c,lambda:self.t)
 def tearDown(self):self.tmp.cleanup()
 def job(self,text):
  j=self.store.create(text,kind='chat');return self.store.get(j['id'])
 def entry(self,text,**kw):
  e=dict(quote=text,kind='relationship',scope='long',certainty='stated',subject='陈晓',target='new',occurred='',expires_at=None);e.update(kw)
  return self.m.validate(e,text,{r['id'] for r in self.m.list()})
 def test_persist_temporal_context_and_history(self):
  text='我这周和陈晓一起准备搬家。';j=self.job(text);mid=self.m.save(self.entry(text,scope='short',kind='context'),j)
  self.assertEqual(LifeMemory(self.c,lambda:self.t).search('陈晓')[0]['id'],mid)
  self.t+=8*86400;self.assertEqual(self.m.search('陈晓'),[]);self.assertEqual(self.m.list(True)[0]['status'],'expired')
  self.assertEqual(self.store.get(j['id'])['goal'],text)
 def test_correction_keeps_time_and_replaces_current(self):
  text='陈晓是我的同事。';mid=self.m.save(self.entry(text),self.job(text))
  newer='陈晓现在是我的合伙人。';self.m.correct(mid,newer,self.job(newer));self.assertEqual(self.m.search('陈晓')[0]['content'],newer)
  with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM life_versions WHERE memory_id=?',(mid,)).fetchone()[0],2)
 def test_forget_clears_all_versions_and_blocks_rephrased_entity(self):
  text='陈晓是我的同事。';mid=self.m.save(self.entry(text),self.job(text));self.m.correct(mid,'陈晓现在是合伙人。',self.job('陈晓现在是合伙人。'))
  self.m.forget(query='陈晓');self.assertEqual(self.m.search('陈晓'),[]);self.assertTrue(self.m.blocked('我和陈晓今天吃饭'))
  with self.store.db() as db:
   self.assertTrue(all(r[0] is None for r in db.execute('SELECT content FROM life_versions')))
  self.assertNotIn('陈晓',self.m.journal.read_text())
 def test_restore_replays_deletion_before_reading(self):
  text='陈晓是我的朋友。';self.m.save(self.entry(text),self.job(text));backup=self.root/'old.sqlite'
  with self.store.db() as db:
   import sqlite3
   with sqlite3.connect(backup) as dest:db.backup(dest)
  self.m.forget(query='陈晓');shutil.copy2(backup,self.store.path)
  with self.assertRaises(AppError):self.m.search('陈晓')
  self.m.replay();self.assertEqual(self.m.search('陈晓'),[])
 def test_quote_cannot_strip_negation_or_hypothesis(self):
  text='假设陈晓是我同事。';e=dict(quote='陈晓是我同事',kind='relationship',scope='long',certainty='stated',subject='陈晓',target='new',occurred='',expires_at=None)
  with self.assertRaises(ValueError):self.m.validate(e,text,set())
 def test_complete_sentence_in_multi_sentence_message(self):
  q='陈晓是我的同事。';whole=q+'最近在准备搬家。';e=dict(quote=q,kind='relationship',scope='long',certainty='stated',subject='陈晓',target='new',occurred='',expires_at=None)
  self.assertEqual(self.m.validate(e,whole,set())['quote'],q)
 def test_this_week_expiry_uses_current_week_not_model_guess(self):
  from datetime import datetime
  from zoneinfo import ZoneInfo
  self.t=datetime(2026,10,30,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
  e=self.entry('陈晓这周在出差。',scope='short',expires_at='2026-11-06T23:59:59+08:00')
  self.assertEqual(datetime.fromtimestamp(e['expires'],ZoneInfo('Asia/Shanghai')).isoformat(),'2026-11-02T00:00:00+08:00')
 def test_duplicate_legacy_decision_is_retired_on_correction(self):
  original='我决定把下一场展览放在苏州，因为离家近。';j=self.job(original)
  self.legacy.put({'slot':'goal.exhibition','kind':'goal','content':'用户明确表达：我决定把下一场展览放在苏州','quote':original},j)
  e=dict(quote=original,kind='decision',scope='long',certainty='stated',subject='展览',target='new',occurred='',expires=None)
  mid=self.m.save(e,j);self.assertEqual(self.legacy.search('苏州'),[])
  self.m.correct(mid,'我决定把展览放在杭州。',self.job('我决定把展览放在杭州。'));self.assertFalse(any('苏州' in r['content'] for r in self.m.list()))
 def test_extraction_repairs_invalid_quote_once(self):
  text='陈晓是我的同事。';j=self.job(text);calls=[]
  valid=dict(quote=text,kind='relationship',scope='long',certainty='stated',subject='陈晓',target='new',occurred='',expires_at=None)
  def runner(*args):
   calls.append(args);return {'texts':[json.dumps({'items':[dict(valid,quote='陈晓是朋友') if len(calls)==1 else valid]})]}
  self.assertEqual(len(self.m.learn(j,runner,{})),1);self.assertEqual(len(calls),2)
 def test_explicit_reauthorization_accepts_new_fact_never_old_source(self):
  text='陈晓是我的同事。';old=self.job(text);self.m.save(self.entry(text),old);self.m.forget(query='陈晓')
  with self.assertRaises(AppError):self.m.manage('allow',self.job('陈晓是谁'),'','陈晓')
  self.m.manage('allow',self.job('重新允许使用陈晓的相关信息'),'','陈晓');self.assertEqual(self.m.list(),[])
  self.assertIsNone(self.m.save(self.entry(text),old))
  new='陈晓现在是我的合伙人。';self.assertIsNotNone(self.m.save(self.entry(new),self.job(new)))
 def test_disable_and_delete_all(self):
  text='陈晓是我的同事。';self.m.save(self.entry(text),self.job(text));self.m.set_enabled(False)
  self.assertEqual(self.m.search('陈晓'),[]);self.assertIsNone(self.m.save(self.entry('陈晓是我的朋友。'),self.job('陈晓是我的朋友。')))
  self.m.set_enabled(True);self.assertEqual(self.m.forget(all_records=True)['count'],1)
 def test_promote_preserves_source(self):
  text='我和陈晓这周准备搬家。';j=self.job(text);mid=self.m.save(self.entry(text,scope='short',kind='context'),j)
  self.m.correct(mid,text,self.job('把这段经历长期记住'),scope='long')
  self.t+=30*86400;self.assertEqual(self.m.search('陈晓')[0]['scope'],'long')

if __name__=='__main__':unittest.main()
