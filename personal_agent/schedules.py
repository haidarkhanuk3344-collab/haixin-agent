"""Explicit conversational appointments. Durable, single release, no model scheduler."""
from datetime import datetime
import json
import re
import time
import uuid
from zoneinfo import ZoneInfo
from .app import AppError, now
from .jobs import Store

TZ=ZoneInfo('Asia/Shanghai')

def guard_claim(result,text):
    """No appointment receipt means the model cannot promise a future action."""
    if result.get('affair_receipts'):return result
    body='\n'.join(result.get('texts',[]))
    intent=re.search(r'提醒我|定时|约定|(?:秒|分钟|小时|天)后|明天|后天',text)
    question=re.search(r'[?？]',text) or re.match(r'你能|能否|可以吗|能不能',text)
    claim=re.search(r'已(?:经)?(?:安排|设置|设好)|(?:我会|我将|到时|到点).{0,50}(?:提醒|发给|发送|交付)',body,re.S)
    if intent and not question and claim:
        result['texts']=['这次还没有保存约定。请直接告诉我，例如“两分钟后提醒我喝水”或“两分钟后给我一篇关于AI的文章”。']
        result['schedule_claim_corrected']=True
    return result

def number(raw):
    if raw.isdigit():return int(raw)
    digits={'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if raw in digits:return digits[raw]
    if re.fullmatch('[一二三四五六七八九]?十[一二三四五六七八九]?',raw):
        a,b=raw.split('十');return (digits[a] if a else 1)*10+(digits[b] if b else 0)
    return None

def parse(text):
    value=text.strip()
    if re.fullmatch(r'(?:请)?(?:取消(?:刚才|这次|最近)(?:的)?(?:约定|定时|提醒)|不用(?:发|提醒)了)[。！]?',value):return {'action':'cancel-latest'}
    m=re.fullmatch(r'(?:(?:行|好|好的)[，,\s]*)?(?:海辛[，,\s]*)?(?:请)?([0-9零一二两三四五六七八九十]+)(秒钟?|分钟|小时|天)后[，,\s]*(?:请)?(.+)',value,re.S)
    if not m:return None
    n=number(m[1]);seconds=(n or 0)*{'秒':1,'秒钟':1,'分钟':60,'小时':3600,'天':86400}[m[2]]
    goal=m[3].strip()
    if re.match(r'不要|不用|别给|取消',goal):return None
    if re.search(r'[?？]',value):raise AppError('SCHEDULE_CONFIRM','还没安排。请直接告诉我约定，例如“两分钟后提醒我喝水”。')
    if not 1<=seconds<=30*86400:raise AppError('SCHEDULE_TIME','约定时间支持1秒至30天。')
    if len(goal)>4000:raise AppError('SCHEDULE_INPUT','约定内容最多4000字。')
    if goal.startswith('提醒我') and goal[3:].strip('，。！ '):mode='reminder';goal=goal[3:].strip()
    elif re.match(r'(给我|发给我|写|帮我写|给我写|告诉我|问我)',goal) and re.search('文章|短文|故事|诗|问候|笑话|问题|你好',goal):mode='writing'
    else:raise AppError('SCHEDULE_UNSUPPORTED','这个约定还没接上执行能力。现在可以说“两分钟后提醒我喝水”或“两分钟后给我一篇关于AI的文章”。')
    if mode=='writing' and re.search(r'https?://|读文件|读取文件|执行命令|运行脚本|发邮件|发微信|搜索|联网|最新新闻',goal):
        raise AppError('SCHEDULE_UNSUPPORTED','约定写作目前只用对话与已有记忆，不会联网、读文件或向第三方发送。')
    return {'action':'create','seconds':seconds,'mode':mode,'goal':goal}

class Schedules:
    def __init__(self,c,clock=time.time):
        self.store=Store(c);self.clock=clock
        with self.store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS appointments(
              id TEXT PRIMARY KEY,session TEXT NOT NULL,origin_job TEXT UNIQUE NOT NULL,
              mode TEXT NOT NULL,goal TEXT NOT NULL,due REAL NOT NULL,status TEXT NOT NULL,
              delivery_job TEXT UNIQUE,created TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS appointment_due ON appointments(status,due);''')
    def accept(self,job,command):
        if command['action']=='cancel-latest':
            rows=self.list(job['session']);active=max((x for x in rows if x['status'] in ('pending','queued','running')),key=lambda x:x['created'],default=None)
            return self.cancel(active['id'],job['session']) if active else {'texts':['当前对话没有待执行的约定。']}
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            origin=db.execute('SELECT cancel_requested,status FROM jobs WHERE id=?',(job['id'],)).fetchone()
            if origin and (origin['cancel_requested'] or origin['status']=='cancelled'):raise AppError('SCHEDULE_CANCELLED','请求已取消，未安排。')
            row=db.execute('SELECT * FROM appointments WHERE origin_job=?',(job['id'],)).fetchone()
            if row is None:
                db.execute('INSERT INTO appointments VALUES(?,?,?,?,?,?,?,NULL,?)',(job['id'],job['session'],job['id'],command['mode'],command['goal'],self.clock()+command['seconds'],'pending',now()))
                row=db.execute('SELECT * FROM appointments WHERE id=?',(job['id'],)).fetchone()
        when=datetime.fromtimestamp(row['due'],TZ).strftime('%m月%d日 %H:%M:%S')
        return {'texts':['已安排，'+when+('提醒你：'+row['goal'] if row['mode']=='reminder' else '开始写，完成后发到这里。')], 'appointment_id':row['id'],'due':row['due']}
    def list(self,session=None):
        with self.store.db() as db:
            rows=db.execute('''SELECT a.*,j.status AS job_status FROM appointments a LEFT JOIN jobs j ON a.delivery_job=j.id
              WHERE (? IS NULL OR a.session=?) ORDER BY a.due DESC,a.rowid DESC LIMIT 100''',(session,session)).fetchall()
        return [dict(r,status=r['job_status'] or r['status']) for r in rows]
    def cancel(self,identity,session=None):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE');r=db.execute('SELECT * FROM appointments WHERE id=?',(identity,)).fetchone()
            if not r or (session is not None and r['session']!=session):raise AppError('NOT_FOUND','找不到该约定。')
            if r['status']=='pending':db.execute("UPDATE appointments SET status='cancelled' WHERE id=?",(identity,))
            elif r['delivery_job']:
                status=db.execute('SELECT status FROM jobs WHERE id=?',(r['delivery_job'],)).fetchone()[0]
                if status in ('queued','running'):
                    db.execute("UPDATE jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated=? WHERE id=?",(now(),r['delivery_job']))
                else:return {'texts':['该约定已经结束。']}
        return {'texts':['已取消约定；正在进行的请求会停止交付。']}
    def release(self):
        """Creation and release marker share one transaction, including across crashes."""
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT * FROM appointments WHERE status='pending' AND due<=? ORDER BY due LIMIT 20",(self.clock(),)).fetchall()
            for r in rows:
                jid=str(uuid.uuid5(uuid.NAMESPACE_URL,'haixin-appointment:'+r['id']));stamp=now()
                db.execute("INSERT OR IGNORE INTO jobs(id,kind,status,goal,session,category,inputs,created,updated) VALUES(?,'scheduled','queued',?,?,'chat','[]',?,?)",(jid,r['goal'],r['session'],stamp,stamp))
                db.execute("UPDATE appointments SET status='queued',delivery_job=? WHERE id=?",(jid,r['id']))
        return len(rows)
    def for_job(self,jid):
        with self.store.db() as db:r=db.execute('SELECT * FROM appointments WHERE delivery_job=?',(jid,)).fetchone()
        if not r:raise AppError('SCHEDULE_MISSING','约定记录缺失，未执行。')
        return dict(r)
