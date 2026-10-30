import json,unittest,tempfile,uuid
from pathlib import Path
from personal_agent.jobs import Store
from personal_agent.memory import Memory
from personal_agent.memory_language import fragments,validate

class LanguageMemoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);state=self.root/'state';state.mkdir();ws=self.root/'ws';ws.mkdir();(state/'openclaw.json').write_text(json.dumps({'agents':{'defaults':{'workspace':str(ws)}}}));self.c={'state':state};self.store=Store(self.c);self.mem=Memory(self.c);self.calls=[]
 def tearDown(self):self.tmp.cleanup()
 def job(self,text):return self.store.create(text,kind='chat')
 def run_model(self,c,message,session,category):
  self.calls.append((message,category,c['_request_id']));payload=json.loads(message.split('\n',1)[1]);items=[]
  for i,part in enumerate(payload['sources']):
   target='communication.length' if '长篇' in part['text'] else 'new'
   items.append({'source':i,'quote':part['text'],'kind':part['allowed_kinds'][0],'target':target})
  return {'texts':[json.dumps({'items':items},ensure_ascii=False)],'tools':[],'request_id':c['_request_id']}
 def learn(self,text,runner=None):
  j=self.job(text);return j,self.mem.learn(j,runner or self.run_model,self.c)
 def control(self,action,**kwargs):
  cmd=dict(action=action,**kwargs);return self.mem.control(cmd,self.store.create(json.dumps(cmd),kind='memory',category='memory'))
 def test_known_statement_does_not_add_model_call(self):
  _,ids=self.learn('我喜欢直接一点，别写太长。我平时喜欢喝乌龙茶。');self.assertEqual(len(ids),3);self.assertEqual(self.calls,[])
 def test_varied_explicit_statements_single_call_grounded_sources(self):
  j,ids=self.learn('我习惯晚上运动。我不喜欢长篇解释。我今年的目标是学会日语。')
  self.assertEqual(len(ids),3);self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0][1],'memory')
  for m in self.mem.list():self.assertIn(m['quote'],j['goal']);self.assertEqual(m['source_job'],j['id'])
  self.assertIn('不喜欢',self.mem.get('communication.length')['content'])
  self.assertIn('日语',str(self.mem.search('今年的目标')))
 def test_mixed_known_and_new_both_persist(self):
  _,ids=self.learn('我喜欢喝乌龙茶。我习惯晚上运动。');self.assertEqual(len(ids),2);self.assertEqual(len(self.calls),1)
 def test_quoted_third_party_temporary_and_no_memory_never_call_model(self):
  for t in ['朋友说我习惯晚上运动','他说“我习惯晚上运动”','我今天想喝奶茶','我可能习惯晚上运动','假设我习惯晚上运动','我习惯晚上运动，不要长期保存']:
   self.learn(t)
  self.assertEqual(self.calls,[]);self.assertEqual(self.mem.list(),[])
 def test_no_history_or_external_material_in_prompt(self):
  self.learn('我习惯晚上运动。');prompt=self.calls[-1][0];self.assertNotIn('相关资料片段',prompt);self.assertNotIn('旧聊天',prompt)
 def test_model_rewrite_or_drop_negation_is_rejected(self):
  p=fragments('我不喜欢长篇解释',{'preference':True,'goal':True,'topic':True})
  for q in ['我喜欢长篇解释','不喜欢长篇解释','我不喜欢长篇解释。','用户讨厌所有文字']:
   with self.assertRaises(ValueError):validate({'texts':[json.dumps({'items':[{'source':0,'quote':q,'kind':'preference','target':'new'}]})]},p,[])
 def test_invalid_source_kind_target_and_tools_rejected(self):
  p=fragments('我习惯晚上运动',{'preference':True,'goal':True,'topic':True});item={'source':0,'quote':p[0]['text'],'kind':'preference','target':'new'}
  for change in [{'source':True},{'source':3},{'kind':'goal'},{'target':'guessed-id'}]:
   with self.assertRaises(ValueError):validate({'texts':[json.dumps({'items':[dict(item,**change)]})]},p,[])
  with self.assertRaises(ValueError):validate({'texts':['{"items":[]}'],'tools':['read']},p,[])
 def test_malformed_result_no_memory_no_retry_even_after_restart(self):
  def bad(*args):self.calls.append('bad');return {'texts':['已记住']}
  j,ids=self.learn('我习惯晚上运动。',bad);self.assertEqual(ids,[]);self.assertEqual(self.mem.last_learning['status'],'extraction_failed')
  self.mem=Memory(self.c);self.assertEqual(self.mem.learn(j,bad,self.c),[]);self.assertEqual(len(self.calls),1)
 def test_pending_extraction_never_replayed(self):
  j=self.job('我习惯晚上运动。')
  with self.store.db() as db:db.execute('INSERT INTO memory_observations VALUES(?,?,?)',(j['id'],j['seq'],'interpreting'))
  self.mem.learn(j,self.run_model,self.c);self.assertEqual(self.calls,[])
 def test_disabled_category_not_interpreted_or_backfilled(self):
  self.control('disable',kind='preference');j,ids=self.learn('我习惯晚上运动。');self.assertEqual(ids,[]);self.assertEqual(self.calls,[])
  self.control('enable',kind='preference');self.control('rebuild');self.mem.learn(j,self.run_model,self.c);self.assertEqual(self.mem.list(),[]);self.assertEqual(self.calls,[])
 def test_forget_restart_rebuild_and_same_statement_protected(self):
  j,ids=self.learn('我习惯晚上运动。');self.control('forget',id=ids[0]);self.mem=Memory(self.c);self.control('rebuild');self.mem.learn(j,self.run_model,self.c)
  self.learn('我习惯晚上运动。');self.assertEqual(self.mem.list(),[])
  with self.store.db() as db:
   self.assertEqual(db.execute('SELECT count(*) FROM memory_index').fetchone()[0],0)
   self.assertTrue(all(r[0] is None for r in db.execute('SELECT quote FROM memory_versions')))
 def test_same_slot_semantic_correction_preserves_version(self):
  _,ids=self.learn('我习惯晚上运动。')
  def correct(c,p,s,k):
   payload=json.loads(p.split('\n',1)[1]);self.assertTrue(any(x['key']==ids[0] for x in payload['targets']))
   return {'texts':[json.dumps({'items':[{'source':0,'quote':'我现在习惯早上运动','kind':'preference','target':ids[0]}]})]}
  self.learn('我现在习惯早上运动。',correct);m=self.mem.get(ids[0],True);self.assertEqual(m['version'],2);self.assertIn('早上',m['content']);self.assertEqual(m['history'][0]['status'],'superseded')
  self.control('forget',id=ids[0]);self.learn('我现在习惯早上运动。');self.assertEqual(self.mem.list(),[])
 def test_restore_keeps_deleted_corrected_expression_blocked(self):
  import sqlite3,shutil
  _,ids=self.learn('我习惯晚上运动。');backup=self.root/'backup.sqlite'
  with sqlite3.connect(backup) as out,self.store.db() as source:source.backup(out)
  def correct(*args):return {'texts':[json.dumps({'items':[{'source':0,'quote':'我现在习惯早上运动','kind':'preference','target':ids[0]}]})]}
  self.learn('我现在习惯早上运动。',correct);self.control('forget',id=ids[0]);shutil.copyfile(backup,self.store.path);self.mem=Memory(self.c);self.mem.replay_deletions()
  self.learn('我现在习惯早上运动。');self.assertEqual(self.mem.list(),[])
 def test_cancel_after_model_prevents_new_memory(self):
  self.mem.learn(self.job('我习惯晚上运动'),self.run_model,dict(self.c,_cancel_check=lambda:'cancel'));self.assertEqual(self.mem.list(),[])
 def test_unknown_relationship_answer_is_not_replaced_by_memory_notice(self):
  j=self.job('陈晓和我是什么关系？')
  for answer in ['我没有找到关于陈晓的关系信息。你可以重新告诉我，需要的话我会记住。','我尚未记住这件事，请你告诉我。']:
   r=self.mem.annotate_result({'texts':[answer]},j,[]);self.assertEqual(r['texts'],[answer]);self.assertNotIn('persistence_claim_corrected',r)
 def test_real_arrangement_receipt_is_not_misread_as_long_memory(self):
  answer='已记下了，明天四点交方案，三点半提醒。'
  r=self.mem.annotate_result({'texts':[answer],'agent_tools':[{'name':'haixin_affairs','status':'succeeded'}]},self.job('明天四点交方案'),[]);self.assertEqual(r['texts'],[answer])
 def test_failed_extraction_cannot_claim_saved(self):
  j,_=self.learn('我习惯晚上运动',lambda *a:{'error':{'code':'MODEL_ERROR'}})
  r=self.mem.annotate_result({'texts':['已经记住了']},j,[]);self.assertTrue(r['persistence_claim_corrected']);self.assertIn('未成功',r['memory_notice'])
 def test_partial_failure_does_not_claim_all_saved(self):
  j,ids=self.learn('我喜欢喝乌龙茶。我习惯晚上运动。',lambda *a:{'texts':['bad JSON']});self.assertEqual(len(ids),1)
  r=self.mem.annotate_result({'texts':['两件事都记下了']},j,ids);self.assertIn('只保存',r['texts'][0]);self.assertNotIn('晚上运动',str(self.mem.list()))
 def test_relative_time_has_source_timestamp(self):
  j,_=self.learn('我今年的目标是学会日语。');message,_,_=self.mem.context(j);self.assertIn('recorded_at',message);self.assertIn('不自动挪到现在',message)
 def test_extraction_workspace_tools_and_original_unchanged(self):
  before=(self.c['state']/'openclaw.json').read_bytes();cfg=json.loads(Path(self.mem.extraction_config()).read_text());self.assertEqual(cfg['tools']['deny'],['*']);self.assertFalse(cfg['tools']['toolSearch']);self.assertFalse(cfg['tools']['codeMode']['enabled']);self.assertEqual(before,(self.c['state']/'openclaw.json').read_bytes())
if __name__=='__main__':unittest.main()
