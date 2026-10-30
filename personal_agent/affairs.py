"""Personal affairs and durable local reminders, independent of model availability."""
from datetime import datetime,timedelta
import json,re,time,uuid
from zoneinfo import ZoneInfo
from .app import AppError,now
from .jobs import Store
TZ=ZoneInfo('Asia/Shanghai')

def stamp(value):return datetime.fromtimestamp(value,TZ).strftime('%Y年%m月%d日 %H:%M') if value is not None else None

class Affairs:
 def __init__(self,c,clock=time.time):
  self.c,self.store,self.clock=c,Store(c),clock
  with self.store.db() as db:
   db.executescript('''CREATE TABLE IF NOT EXISTS affairs(
    id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT,body TEXT,due REAL,remind REAL,
    timezone TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL,
    session TEXT NOT NULL,source_job TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS affairs_due ON affairs(status,remind);
    CREATE TABLE IF NOT EXISTS affair_actions(action_key TEXT PRIMARY KEY,result TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS affair_notices(id TEXT PRIMARY KEY,affair_id TEXT NOT NULL,version INTEGER NOT NULL,
     session TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,late INTEGER NOT NULL,
     state TEXT NOT NULL,mac_state TEXT NOT NULL,mac_error TEXT,read_at REAL,
     UNIQUE(affair_id,version));''')
   if 'reminder_minutes' not in {r[1] for r in db.execute('PRAGMA table_info(affairs)')}:db.execute('ALTER TABLE affairs ADD COLUMN reminder_minutes INTEGER')
 def list(self,query='',include_closed=False):
  from .life_memory import LifeMemory
  life=LifeMemory(self.c);life.ready()
  with self.store.db() as db:
   rows=[dict(r) for r in db.execute("SELECT * FROM affairs WHERE status<>'deleted' ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END,COALESCE(due,9e99),updated DESC")]
  return [r for r in rows if (include_closed or r['status']=='active') and (not query or query in (r['title'] or '')+' '+(r['body'] or '')) and not life.blocked((r['title'] or '')+' '+(r['body'] or ''))][:100]
 def time_value(self,value,quote,text,timezone="Asia/Shanghai"):
  if not value:return None
  if not quote or quote not in text:raise AppError('AFFAIR_TIME','时间依据必须来自本轮用户原话。')
  # A calendar date alone is not a clock time. Ask instead of silently choosing midnight.
  if not re.search(r'\d{1,2}[:：]\d{2}|[0-9一二两三四五六七八九十]+[点时]|[0-9一二两三四五六七八九十]+(?:分钟|小时|秒)后|中午|午夜',quote):raise AppError('AFFAIR_TIME_NEEDED','还缺具体时间，请问清几点后再安排提醒。')
  try:
   d=datetime.fromisoformat(value)
   zone=ZoneInfo(timezone)
   if d.tzinfo is None or d.utcoffset()!=d.astimezone(zone).utcoffset():raise ValueError()
  except (ValueError,TypeError,KeyError):raise AppError('AFFAIR_TIME','请使用所指定时区的明确日期与正确UTC偏移。')
  local=datetime.fromtimestamp(self.clock(),zone)
  for word,days in [('大后天',3),('后天',2),('明天',1),('今天',0)]:
   if word in quote:
    if d.date()!=(local+timedelta(days=days)).date():raise AppError('AFFAIR_TIME','解析的日期与用户的相对日期不符，请按当前北京时间修正。')
    break
  if d.timestamp()<=self.clock():raise AppError('AFFAIR_PAST','该时刻已经过去，请确认新的时间。')
  return d.timestamp()
 def act(self,job,call_id,action,identity='',kind=None,title=None,body=None,time_iso=None,time_quote=None,reminder_minutes=None,version=None,timezone=None):
  if action=='list':return {'items':self.list(query=title or '',include_closed=True),'now':datetime.fromtimestamp(self.clock(),TZ).isoformat()}
  if action not in ('create','update','complete','cancel','delete','stop_reminding'):raise AppError('AFFAIR_ACTION','不支持的事项操作。')
  key=job['id']+':'+call_id
  with self.store.db() as db:
   prior=db.execute('SELECT result FROM affair_actions WHERE action_key=?',(key,)).fetchone()
   if prior:return json.loads(prior[0])
  text=job['goal'];old=None
  if action!='create':
   with self.store.db() as db:r=db.execute('SELECT * FROM affairs WHERE id=?',(identity,)).fetchone()
   if not r or r['status']=='deleted':raise AppError('AFFAIR_NOT_FOUND','没有找到这件事，请先查询。')
   old=dict(r)
   from .life_memory import LifeMemory
   if LifeMemory(self.c).blocked((old['title'] or '')+' '+(old['body'] or '')):raise AppError('MEMORY_FORGOTTEN','这件事含已遗忘内容，请用户重新提供相关信息。')
   if version!=old['version']:raise AppError('AFFAIR_VERSION','这件事已有新变化，请重新查询后操作。')
  if action in ('create','update'):
   if kind is not None and kind not in ('schedule','todo','note'):raise AppError('AFFAIR_KIND','事项类型应为日程、待办或笔记。')
   if title is not None and (not isinstance(title,str) or not 1<=len(title)<=200):raise AppError('AFFAIR_INPUT','事项标题须为1–200字。')
   if body is not None and (not isinstance(body,str) or len(body)>6000):raise AppError('AFFAIR_INPUT','笔记正文最多6000字。')
   if action=='create' and (not kind or not title):raise AppError('AFFAIR_INPUT','请提供事项类型和内容。')
   if action=='create' and re.search('假设|虚构|写作设定|举个例子|比如说|如果我|朋友说|他说|她说',text):raise AppError('AFFAIR_HYPOTHETICAL','这条消息含假设或转述，需确认是用户实际安排再登记。')
   tz=timezone or (old['timezone'] if old else 'Asia/Shanghai')
   aliases={'Asia/Shanghai':('北京','上海','中国'),'Asia/Tokyo':('东京','日本'),'America/New_York':('纽约','美东'),'America/Los_Angeles':('洛杉矶','美西'),'Europe/London':('伦敦','英国'),'UTC':('UTC','GMT','协调世界时')}
   if timezone and timezone!='Asia/Shanghai' and not any(v in text for v in (timezone,)+aliases.get(timezone,())):raise AppError('AFFAIR_TIMEZONE','非默认时区需来自用户明确说明。')
   try:ZoneInfo(tz)
   except (KeyError,ValueError):raise AppError('AFFAIR_TIMEZONE','无法识别这个时区，请使用明确城市或IANA时区。')
   due=self.time_value(time_iso,time_quote,text,tz) if time_iso is not None else old['due'] if old else None
   k=kind or old['kind']
   if k=='schedule' and due is None:raise AppError('AFFAIR_TIME_NEEDED','日程还缺具体日期和时间，请只询问缺失信息。')
   if k=='note' and (due is not None or reminder_minutes is not None):raise AppError('AFFAIR_NOTE','笔记不自动设置提醒；需要提醒时应登记独立事项。')
   if reminder_minutes is not None and (type(reminder_minutes) is not int or not 0<=reminder_minutes<=525600):raise AppError('AFFAIR_REMINDER','提前量应为0至525600分钟。')
   lead=reminder_minutes if reminder_minutes is not None else old['reminder_minutes'] if old else 30
   remind=max(self.clock(),due-lead*60) if due is not None and lead is not None else None
  with self.store.db() as db:
   db.execute('BEGIN IMMEDIATE')
   prior=db.execute('SELECT result FROM affair_actions WHERE action_key=?',(key,)).fetchone()
   if prior:return json.loads(prior[0])
   if old:
    fresh=db.execute('SELECT * FROM affairs WHERE id=?',(identity,)).fetchone()
    if fresh['version']!=version:raise AppError('AFFAIR_VERSION','事项刚刚变化，请重新查询。')
   if action=='create':
    # A repeated model call in the same human turn must not create another arrangement.
    duplicate=db.execute("SELECT * FROM affairs WHERE source_job=? AND kind=? AND title=? AND due IS ? AND status='active'",(job['id'],kind,title,due)).fetchone()
    if duplicate:identity=duplicate['id']
    else:
     identity=str(uuid.uuid5(uuid.UUID(job['id']),call_id))
     db.execute("INSERT INTO affairs VALUES(?,?,?,?,?,?,?,'active',1,?,?,?,?,?)",(identity,kind,title,body or '',due,remind,tz,job['session'],job['id'],now(),now(),lead if due else None))
   elif action=='update':
    if old['status']!='active':raise AppError('AFFAIR_CLOSED','该事项已经结束；要再次安排请新建。')
    db.execute('UPDATE affairs SET kind=?,title=?,body=?,due=?,remind=?,reminder_minutes=?,timezone=?,version=version+1,source_job=?,updated=? WHERE id=?',(k,title if title is not None else old['title'],body if body is not None else old['body'],due,remind,lead,tz,job['id'],now(),identity))
   elif action=='stop_reminding':db.execute('UPDATE affairs SET remind=NULL,reminder_minutes=NULL,version=version+1,updated=? WHERE id=?',(now(),identity))
   else:
    state={'complete':'done','cancel':'cancelled','delete':'deleted'}[action]
    db.execute('UPDATE affairs SET status=?,remind=NULL,version=version+1,updated=? WHERE id=?',(state,now(),identity))
    if action=='delete':db.execute('UPDATE affairs SET title=NULL,body=NULL WHERE id=?',(identity,))
   if action!='create':
    db.execute("UPDATE affair_notices SET state='revoked',mac_state=CASE WHEN mac_state='pending' THEN 'cancelled' ELSE mac_state END WHERE affair_id=?",(identity,))
   row=dict(db.execute('SELECT * FROM affairs WHERE id=?',(identity,)).fetchone())
   result={'affair':row,'action':action,'date':(stamp(row['due'])+' 北京时间') if row['due'] else None,'reminder':(stamp(row['remind'])+' 北京时间') if row['remind'] else None,'saved':True}
   db.execute('INSERT INTO affair_actions VALUES(?,?)',(key,json.dumps(result,ensure_ascii=False)))
  return result
 def release(self):
  """Durable app delivery in one transaction, even while the model worker is busy."""
  from .life_memory import LifeMemory
  life=LifeMemory(self.c);life.ready();t=self.clock();count=0
  with self.store.db() as db:
   db.execute('BEGIN IMMEDIATE')
   rows=db.execute("SELECT * FROM affairs WHERE status='active' AND remind IS NOT NULL AND remind<=?",(t,)).fetchall()
   for r in rows:
    if life.blocked((r['title'] or '')+' '+(r['body'] or '')):continue
    identity=str(uuid.uuid5(uuid.UUID(r['id']),'notice:'+str(r['version'])))
    late=t-r['remind']>60
    body=('迟到的提醒：' if late else '提醒你：')+r['title']+'，时间是 '+stamp(r['due'])+'。'
    count+=db.execute("INSERT OR IGNORE INTO affair_notices VALUES(?,?,?,?,?,?,?,'active','pending',NULL,NULL)",(identity,r['id'],r['version'],r['session'],body,t,int(late))).rowcount
  return count
 def notices(self):
  from .life_memory import LifeMemory
  life=LifeMemory(self.c);life.ready()
  with self.store.db() as db:return [dict(r) for r in db.execute("SELECT * FROM affair_notices WHERE state='active' ORDER BY created DESC LIMIT 100") if not life.blocked(r['body'])]
