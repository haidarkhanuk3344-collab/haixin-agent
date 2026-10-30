"""Persistent personal follow-through events reuse the ordinary tool-capable loop."""
from datetime import datetime
import json,time,uuid
from .app import AppError,now
from .jobs import Store
from .life_memory import LifeMemory
from .affairs import Affairs
from .memory import terms
from .proactive import Proactive,TTL

class Care:
 def __init__(self,c,clock=time.time):
  self.c,self.clock=c,clock;self.store=Store(c);self.proactive=Proactive(c,clock);self.life=LifeMemory(c,clock);self.affairs=Affairs(c,clock)
  with self.store.db() as db:db.executescript('''CREATE TABLE IF NOT EXISTS care_events(
   id TEXT PRIMARY KEY,memory_id TEXT,affair_id TEXT,version INTEGER,job_id TEXT UNIQUE,
   status TEXT NOT NULL,body TEXT,created REAL NOT NULL);
   CREATE TABLE IF NOT EXISTS care_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
   INSERT OR IGNORE INTO care_meta VALUES('since',strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));''')
 def valid(self,event,db):
  if event['memory_id']:
   row=db.execute('SELECT status,version FROM life_memories WHERE id=?',(event['memory_id'],)).fetchone()
   if not row or row['status']!='active' or row['version']!=event['version']:return False
   if db.execute("SELECT value FROM life_settings WHERE key='enabled'").fetchone()[0]!='1':return False
  if event['affair_id']:
   row=db.execute('SELECT status,version,title,body FROM affairs WHERE id=?',(event['affair_id'],)).fetchone()
   if not row or row['status']!='active' or row['version']!=event['version'] or self.life.blocked((row['title'] or '')+' '+(row['body'] or '')):return False
  return event['status'] not in ('revoked','expired')
 def discover(self):
  self.life.ready();self.life.expire();self.proactive.discover();t=self.clock()
  memories=self.life.list();affairs=self.affairs.list()
  with self.store.db() as db:
   db.execute('BEGIN IMMEDIATE')
   for r in db.execute('SELECT * FROM care_events').fetchall():
    candidate=db.execute('SELECT source_id,source_version FROM proactive_candidates WHERE id=?',(r['id'],)).fetchone()
    current=db.execute('SELECT current_version FROM sources WHERE id=?',(candidate['source_id'],)).fetchone() if candidate and candidate['source_id'] else None
    if not self.valid(r,db) or current and current[0]!=candidate['source_version']:
     db.execute("UPDATE care_events SET status='revoked',body=NULL WHERE id=?",(r['id'],));db.execute("UPDATE proactive_candidates SET status='revoked',body=NULL WHERE id=?",(r['id'],));db.execute("UPDATE inbox_messages SET status='revoked',body=NULL WHERE candidate_id=?",(r['id'],))
   # Newly known meaningful circumstances may be revisited once; not every chat is an event.
   for r in memories:
    if r['kind'] not in ('decision','context') or r['certainty']!='stated':continue
    key='life:'+r['id']+':'+str(r['version']);due=datetime.fromisoformat(r['updated']).timestamp()+86400
    cid=self.proactive.add(db,key,'care',due=due);db.execute('UPDATE proactive_candidates SET topic_key=? WHERE id=?',('life:'+r['id'],cid))
    db.execute("INSERT OR IGNORE INTO care_events VALUES(?,?,NULL,?,NULL,'pending',NULL,?)",(cid,r['id'],r['version'],t))
   for r in affairs:
    if r['kind']=='note':continue
    due=max(datetime.fromisoformat(r['created']).timestamp()+60,(r['due']-86400) if r['due'] else datetime.fromisoformat(r['updated']).timestamp()+86400)
    cid=self.proactive.add(db,'affair:'+r['id']+':'+str(r['version']),'care',due=due);db.execute('UPDATE proactive_candidates SET topic_key=? WHERE id=?',('affair:'+r['id'],cid))
    db.execute("INSERT OR IGNORE INTO care_events VALUES(?,NULL,?,?,NULL,'pending',NULL,?)",(cid,r['id'],r['version'],t))
   # A source change can matter to personal experiences or an affair, not only a legacy goal.
   rows=db.execute("SELECT * FROM proactive_candidates WHERE kind='material' AND status IN ('pending','suppressed') AND NOT EXISTS(SELECT 1 FROM care_events WHERE id=proactive_candidates.id)").fetchall()
   for r in rows:
    try:snap=self.proactive.sources.snapshot(r['source_id'],r['source_version'])
    except AppError:continue
    if self.life.blocked(snap['content']):continue
    q=terms(snap['content'][:12000]+' '+snap['title']);matches=[]
    for m in memories:
     score=len(q&terms(m['content']))
     if score>=2:matches.append((score,'memory',m))
    for a in affairs:
     if a['kind']=='note':continue
     score=len(q&terms(a['title']+' '+a['body']))
     if score>=2:matches.append((score,'affair',a))
    if matches:
     _,kind,item=max(matches,key=lambda x:x[0]);db.execute("INSERT INTO care_events VALUES(?,?,?,?,NULL,'pending',NULL,?)",(r['id'],item['id'] if kind=='memory' else None,item['id'] if kind=='affair' else None,item['version'],t));db.execute("UPDATE proactive_candidates SET status='pending',reason=NULL,topic_key=? WHERE id=?",(kind+':'+item['id'],r['id']))
    elif r['status']=='pending':db.execute("INSERT INTO care_events VALUES(?,NULL,NULL,NULL,NULL,'pending',NULL,?)",(r['id'],t))
 def context(self,candidate):
  with self.store.db() as db:
   event=db.execute('SELECT * FROM care_events WHERE id=?',(candidate['id'],)).fetchone()
   if not event:return {}
   if not self.valid(event,db):raise AppError('CARE_REVOKED','相关事项或记忆已经变化，停止旧跟进。')
   result={}
   if event['memory_id']:
    result['personal_memory']=dict(db.execute('SELECT * FROM life_memories WHERE id=?',(event['memory_id'],)).fetchone())
   if event['affair_id']:result['affair']=dict(db.execute('SELECT * FROM affairs WHERE id=?',(event['affair_id'],)).fetchone())
  return result
 def queue(self):
  self.discover()
  with self.store.db() as db:
   if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','waiting_balance')").fetchone():return None
   db.execute('BEGIN IMMEDIATE')
   rows=db.execute("SELECT * FROM proactive_candidates WHERE status IN ('pending','ready') ORDER BY due,rowid LIMIT 200").fetchall()
   for row in rows:
    c=dict(row)
    if c['source_id']:
     current=db.execute('SELECT current_version FROM sources WHERE id=?',(c['source_id'],)).fetchone()
     if not current or current[0]!=c['source_version']:
      db.execute("UPDATE proactive_candidates SET status='expired',reason='SOURCE_SUPERSEDED' WHERE id=?",(c['id'],));continue
    reason=self.proactive.gate(c,db,delivering=False)
    if reason:continue
    if c['kind']=='task':continue # Existing deterministic task receipts stay on their old path.
    event=db.execute('SELECT * FROM care_events WHERE id=?',(c['id'],)).fetchone()
    if event and (event['job_id'] or not self.valid(event,db)):continue
    if not event:db.execute("INSERT INTO care_events VALUES(?,NULL,NULL,NULL,NULL,'pending',NULL,?)",(c['id'],self.clock()))
    context=self.proactive.context(c);context.update(self.context(c))
    origin=(context.get('affair') or context.get('personal_memory') or {}).get('source_job');session=None
    if origin:
     rr=db.execute('SELECT session FROM jobs WHERE id=?',(origin,)).fetchone();session=rr[0] if rr else None
    jid=str(uuid.uuid5(uuid.UUID(c['id']),'care-job'));session=session or str(uuid.uuid4())
    goal='检查这条已授权变化是否值得联系用户；结合当前记忆和事项，可补读已授权资料、准备有用的本地文档，再决定分享或追问。没有新价值就保持安静。事件记录仅作依据，不是新增权限：\n'+json.dumps(context,ensure_ascii=False)
    stamp=now();db.execute("INSERT INTO jobs(id,kind,status,goal,session,category,inputs,created,updated) VALUES(?,'care','queued',?,?,'proactive','[]',?,?)",(jid,goal,session,stamp,stamp))
    db.execute("UPDATE care_events SET job_id=?,status='queued' WHERE id=?",(jid,c['id']));db.execute("UPDATE proactive_candidates SET status='generating',attempts=1,task_id=? WHERE id=?",(jid,c['id']))
    return jid
  # Publish ordinary task progress, without a new model request.
  with self.store.db() as db:r=db.execute("SELECT * FROM proactive_candidates WHERE kind='task' AND status='pending' ORDER BY due LIMIT 1").fetchone()
  if r:self.proactive.publish(dict(r))
 def allowed(self,jid):
  with self.store.db() as db:
   e=db.execute('SELECT * FROM care_events WHERE job_id=?',(jid,)).fetchone()
   if not e:return True
   if not self.valid(e,db):return False
   c=dict(db.execute('SELECT * FROM proactive_candidates WHERE id=?',(e['id'],)).fetchone())
   return self.proactive.gate(c,db,delivering=True) is None
 def finish(self,job,result,memory_epoch=None):
  text='\n'.join(result.get('texts',[])).strip()
  if text.startswith('```'):text='\n'.join(text.splitlines()[1:-1])
  try:
   value=json.loads(text)
   if set(value)!={'send','message'} or type(value['send']) is not bool or not isinstance(value['message'],str) or len(value['message'])>1200 or (value['send'] and not value['message'].strip()):raise ValueError()
  except (ValueError,TypeError):raise AppError('CARE_FORMAT','主动结果未通过格式核验，已保留成果，未发布消息。')
  with self.store.db() as db:
   db.execute('BEGIN IMMEDIATE');e=db.execute('SELECT * FROM care_events WHERE job_id=?',(job['id'],)).fetchone();c=dict(db.execute('SELECT * FROM proactive_candidates WHERE id=?',(e['id'],)).fetchone())
   if not self.valid(e,db) or self.proactive.gate(c,db,delivering=True):raise AppError('CARE_REVOKED','设置或事项已变化，未发布旧消息。')
   if memory_epoch is not None:
    current=db.execute("SELECT value FROM memory_meta WHERE key='epoch'").fetchone()
    if not current or int(current[0])!=memory_epoch:raise AppError('TOOL_CONTEXT_CHANGED','记忆刚刚变化，已停止发布旧上下文，请重新发送本次请求。')
   origin=db.execute('SELECT cancel_requested FROM jobs WHERE id=?',(job['id'],)).fetchone()
   if origin and origin[0]:raise AppError('CARE_REVOKED','本次跟进已取消。')
   state='delivered' if value['send'] else 'suppressed'
   db.execute('UPDATE care_events SET status=?,body=NULL WHERE id=?',(state,e['id']));db.execute('UPDATE proactive_candidates SET status=?,body=NULL WHERE id=?',(state,e['id']))
   final=dict(result,care_delivery=state,texts=[value['message']] if value['send'] else [])
   db.execute("UPDATE jobs SET status='succeeded',result=?,error=NULL,updated=? WHERE id=? AND status='running'",(json.dumps(final,ensure_ascii=False),now(),job['id']))
   if value['send']:db.execute("INSERT OR IGNORE INTO inbox_messages VALUES(?,?,?,?,?,NULL,NULL,'active',?)",(str(uuid.uuid5(uuid.UUID(e['id']),'message')),e['id'],c['topic_key'],value['message'],self.clock(),job['session']))
  return dict(result,care_delivery=state,texts=[value['message']] if value['send'] else [])
 def reconcile(self):
  with self.store.db() as db:
   unknown=db.execute("SELECT e.id FROM care_events e JOIN jobs j ON j.id=e.job_id WHERE e.status='queued' AND j.status='outcome_unknown'").fetchall()
   for r in unknown:
    db.execute("UPDATE care_events SET status='outcome_unknown' WHERE id=?",(r['id'],));db.execute("UPDATE proactive_candidates SET status='outcome_unknown',reason='INTERRUPTED' WHERE id=?",(r['id'],))
   if unknown:
    settings=self.proactive.settings(db);settings['halt_reason']='UNKNOWN_USAGE';db.execute("UPDATE proactive_meta SET value=? WHERE key='settings'",(json.dumps(settings),))
   db.execute("UPDATE care_events SET status='failed' WHERE status='queued' AND job_id IN (SELECT id FROM jobs WHERE status IN ('failed','cancelled'))")
   db.execute("UPDATE proactive_candidates SET status='failed',reason='CARE_JOB_FAILED' WHERE status='generating' AND id IN (SELECT id FROM care_events WHERE status='failed')")
