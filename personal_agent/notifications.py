"""Local app outbox and macOS delivery; never consumes model quota."""
import json,os,subprocess,tempfile,threading,time
from pathlib import Path
from .app import AppError,atomic_write
from .affairs import Affairs
from .jobs import Store

class NativeNotifications:
 def __init__(self,c):self.c=c;self.root=Store(c).root;self.app=Path.home()/'Applications/海辛通知.app'
 def invoke(self,action,**values):
  if not self.app.is_dir():return {'status':'unavailable'}
  with tempfile.TemporaryDirectory(prefix='notification-',dir=self.root) as temp:
   base=Path(temp);p=base/'in.json';out=base/'out.json';atomic_write(p,json.dumps(dict(action=action,**values),ensure_ascii=False))
   try:
    proc=subprocess.Popen(['/usr/bin/open','-W','-n',str(self.app),'--args',str(p),str(out)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    end=time.monotonic()+(56 if action=='authorize' else 8)
    while time.monotonic()<end:
     if out.is_file():
      value=json.loads(out.read_text());proc.wait(timeout=2);return value
     if proc.poll() is not None:break
     time.sleep(.1)
    # No resend after an uncertain submission. A status check can later reconcile it.
    return {'status':'outcome_unknown'}
   except (OSError,ValueError,subprocess.TimeoutExpired):return {'status':'outcome_unknown'}
 def cached(self):
  p=self.root/'notification-status.json'
  try:return json.loads(p.read_text())
  except (OSError,ValueError):return {'status':'unchecked'}
 def check(self,authorize=False):
  result=self.invoke('authorize' if authorize else 'status')
  if result.get('status')=='authorized':
   with Store(self.c).db() as db:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='affair_notices'").fetchone():db.execute("UPDATE affair_notices SET mac_state='pending' WHERE state='active' AND mac_state IN ('denied','not_determined','unavailable')")
  result['checked_at']=time.time();atomic_write(self.root/'notification-status.json',json.dumps(result));return result
 def request(self):
  if self.cached().get('status')=='requesting':return self.cached()
  atomic_write(self.root/'notification-status.json',json.dumps({'status':'requesting','checked_at':time.time()}))
  threading.Thread(target=self.check,args=(True,),daemon=True).start();return {'status':'requesting'}

class ReminderService:
 def __init__(self,c,native=None):
  self.c=c;self.affairs=Affairs(c);self.store=self.affairs.store;self.native=native or NativeNotifications(c);self.stop=threading.Event()
  with self.store.db() as db:db.execute('CREATE TABLE IF NOT EXISTS notification_batches(notice_id TEXT PRIMARY KEY,native_id TEXT NOT NULL)')
 def delivery_log(self):
  path=self.store.root/'notification-deliveries.jsonl';latest={}
  if path.exists():
   for line in path.read_text().splitlines():
    value=json.loads(line)
    for identity in value['ids']:latest[identity]=value['status']
  return latest
 def record_delivery(self,ids,status):
  path=self.store.root/'notification-deliveries.jsonl'
  fd=os.open(str(path),os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
  with os.fdopen(fd,'a') as f:f.write(json.dumps({'ids':ids,'status':status,'at':time.time()})+'\n');f.flush();os.fsync(f.fileno())
 def deliver(self):
  from .life_memory import LifeMemory
  life=LifeMemory(self.c)
  with self.store.db() as db:
   rows=[dict(r) for r in db.execute("SELECT * FROM affair_notices WHERE state='active' AND mac_state='pending' ORDER BY created LIMIT 20")]
  previous=self.delivery_log();eligible=[]
  for r in rows:
   state=previous.get(r['id'])
   if state in ('sending','delivered','accepted','outcome_unknown'):
    with self.store.db() as db:db.execute('UPDATE affair_notices SET mac_state=? WHERE id=?',('outcome_unknown' if state=='sending' else state,r['id']))
   elif not life.blocked(r['body']):eligible.append(r)
  if not eligible:return
  # When waking after missed deadlines, one system banner summarizes the batch.
  ids=[r['id'] for r in eligible];identity=ids[0];body=eligible[0]['body'] if len(ids)==1 else '有'+str(len(ids))+'件事需要你留意。'+('其中有延迟提醒。' if any(r['late'] for r in eligible) else '')+'打开海辛查看。'
  with self.store.db() as db:
   db.execute('BEGIN IMMEDIATE')
   for r in eligible:
    fresh=db.execute("SELECT state,mac_state FROM affair_notices WHERE id=?",(r['id'],)).fetchone()
    if tuple(fresh)!=('active','pending'):return
   for i in ids:
    db.execute("UPDATE affair_notices SET mac_state='sending' WHERE id=?",(i,));db.execute('INSERT OR REPLACE INTO notification_batches VALUES(?,?)',(i,identity))
  self.record_delivery(ids,'sending')
  result=self.native.invoke('deliver',id=identity,body=body)
  status=result.get('status','outcome_unknown');self.record_delivery(ids,status)
  with self.store.db() as db:
   for i in ids:db.execute('UPDATE affair_notices SET mac_state=?,mac_error=? WHERE id=?',(status,None if status in ('delivered','accepted') else status,i))
  # A cancellation racing submission removes the notification after acceptance.
  with self.store.db() as db:revoked=db.execute("SELECT 1 FROM affair_notices WHERE id=? AND state='revoked'",(identity,)).fetchone()
  if revoked:self.native.invoke('remove',ids=[identity])
 def recover(self):
  with self.store.db() as db:rows=[r[0] for r in db.execute("SELECT id FROM affair_notices WHERE mac_state='sending'")]
  if rows:
   result=self.native.invoke('status');delivered=set(result.get('delivered_ids',[]))
   with self.store.db() as db:
    for i in rows:
     batch=db.execute('SELECT native_id FROM notification_batches WHERE notice_id=?',(i,)).fetchone()
     db.execute('UPDATE affair_notices SET mac_state=? WHERE id=?',('delivered' if (batch[0] if batch else i) in delivered else 'outcome_unknown',i))
 def legacy(self):
  # Preserve the old appointment records while giving ordinary reminders the independent clock.
  from .schedules import Schedules
  Schedules(self.c)
  with self.store.db() as db:
   rows=db.execute("SELECT a.*,j.status AS delivered_state,j.cancel_requested FROM appointments a LEFT JOIN jobs j ON j.id=a.delivery_job WHERE a.mode='reminder'").fetchall()
   for r in rows:
    if r['status']=='pending' or r['status']=='queued' and r['delivered_state'] in ('queued','running'):
     db.execute("INSERT OR IGNORE INTO affairs VALUES(?,'schedule',?,'',?,?,'Asia/Shanghai','active',1,?,?,?,?,0)",(r['id'],r['goal'],r['due'],r['due'],r['session'],r['origin_job'],r['created'],r['created']))
    if r['status']=='cancelled' or r['cancel_requested']:
     db.execute("UPDATE affairs SET status='cancelled',remind=NULL WHERE id=?",(r['id'],));db.execute("UPDATE affair_notices SET state='revoked' WHERE affair_id=?",(r['id'],))
 def cleanup(self):
  from .life_memory import LifeMemory
  life=LifeMemory(self.c)
  with self.store.db() as db:
   for row in db.execute("SELECT id,body FROM affair_notices WHERE state='active'").fetchall():
    if life.blocked(row['body']):db.execute("UPDATE affair_notices SET state='revoked',body='' WHERE id=?",(row['id'],))
  with self.store.db() as db:ids=[r[0] for r in db.execute("SELECT id FROM affair_notices WHERE state='revoked' AND mac_state IN ('accepted','delivered')")]
  if ids:
   with self.store.db() as db:native_ids=[(db.execute('SELECT native_id FROM notification_batches WHERE notice_id=?',(i,)).fetchone() or [i])[0] for i in ids]
   result=self.native.invoke('remove',ids=list(set(native_ids)))
   if result.get('status')=='removed':
    with self.store.db() as db:
     for i in ids:db.execute("UPDATE affair_notices SET mac_state='removed' WHERE id=?",(i,))
 def send_loop(self):
  self.recover()
  while not self.stop.is_set():
   try:self.cleanup();self.deliver()
   except Exception:atomic_write(self.store.root/'notification-health.json',json.dumps({'status':'retrying','at':time.time()}))
   self.stop.wait(.5)
 def run(self):
  while not self.stop.is_set():
   try:
    self.legacy();self.affairs.release()
    atomic_write(self.store.root/'reminder-health.json',json.dumps({'status':'running','at':time.time()}))
   except Exception:
    # Persist only a code, never private reminder contents or a traceback.
    atomic_write(self.store.root/'reminder-health.json',json.dumps({'status':'retrying','at':time.time()}))
   self.stop.wait(.5)
 def start(self):
  self.thread=threading.Thread(target=self.run,name='haixin-reminders',daemon=True);self.sender=threading.Thread(target=self.send_loop,name='haixin-notifications',daemon=True);self.thread.start();self.sender.start();return self
 def close(self):self.stop.set();self.thread.join(timeout=2);self.sender.join(timeout=10)
