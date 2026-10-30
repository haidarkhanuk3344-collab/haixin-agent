"""Event-driven local inbox. One worker owns model calls; gates are persisted."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo

from .app import AppError, atomic_write, now
from .jobs import Store
from .memory import Memory, terms
from .sources import Sources

TZ = ZoneInfo('Asia/Shanghai')
TTL = 7 * 86400


def invalidate(db, memory_id=None, kind=None):
    """Called inside memory's transaction, including deletion-journal replay."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='proactive_candidates'").fetchone(): return
    where, args = 'topic_id IS NOT NULL', []
    if memory_id:
        where += ' AND topic_id=?'; args.append(memory_id)
    elif kind and kind != 'all':
        where += ' AND topic_id IN (SELECT id FROM memories WHERE kind=?)'; args.append(kind)
    ids = [r[0] for r in db.execute('SELECT id FROM proactive_candidates WHERE '+where, args)]
    for identity in ids:
        db.execute("UPDATE proactive_candidates SET status='revoked',reason='MEMORY_CHANGED',body=NULL WHERE id=?", (identity,))
        db.execute("UPDATE inbox_messages SET status='revoked',body=NULL WHERE candidate_id=?", (identity,))


def natural_control(text):
    if re.fullmatch(r'(?:请)?(?:关闭|停止|暂停)主动(?:消息|交流)[。！]?', text): return {'action':'disable'}
    if re.fullmatch(r'(?:请)?(?:开启|恢复)主动(?:消息|交流)[。！]?', text): return {'action':'resume' if '恢复' in text else 'enable'}
    if re.fullmatch(r'(?:请)?今天(?:先)?别打扰(?:我)?[。！]?', text): return {'action':'pause-today'}
    if re.fullmatch(r'(?:请)?(?:别催我|不要催我|先别打扰我)[。！]?', text): return {'action':'pause', 'hours':24}
    return None


class Proactive:
    def __init__(self, c, clock=time.time):
        self.c, self.store, self.clock = c, Store(c), clock
        self.memory, self.sources = Memory(c), Sources(c)
        with self.store.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS proactive_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS proactive_candidates(
                id TEXT PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,topic_id TEXT,topic_version INTEGER,
                source_id TEXT,source_version INTEGER,task_id TEXT,topic_key TEXT NOT NULL,
                due REAL NOT NULL,expires REAL NOT NULL,created REAL NOT NULL,status TEXT NOT NULL,
                reason TEXT,body TEXT,attempts INTEGER NOT NULL DEFAULT 0);
              CREATE INDEX IF NOT EXISTS proactive_due ON proactive_candidates(status,due);
              CREATE TABLE IF NOT EXISTS proactive_calls(
                candidate_id TEXT PRIMARY KEY,day TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,request_id TEXT);
              CREATE TABLE IF NOT EXISTS inbox_messages(
                id TEXT PRIMARY KEY,candidate_id TEXT UNIQUE NOT NULL,topic_key TEXT NOT NULL,body TEXT,
                created REAL NOT NULL,read_at REAL,feedback TEXT,status TEXT NOT NULL,session TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS proactive_feedback(
                id TEXT PRIMARY KEY,message_id TEXT,action TEXT NOT NULL,created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS proactive_mutes(topic_key TEXT PRIMARY KEY,until REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS inbox_replies(job_id TEXT PRIMARY KEY,message_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS proactive_settings_log(id TEXT PRIMARY KEY,action TEXT NOT NULL,value TEXT NOT NULL,created REAL NOT NULL);
            ''')
            defaults = {'schema':'1', 'since':now(), 'source_cursor':str(db.execute('SELECT COALESCE(MAX(rowid),0) FROM source_versions').fetchone()[0]),
                        'task_since':str(db.execute('SELECT COALESCE(MAX(rowid),0) FROM jobs').fetchone()[0]),
                        'settings':json.dumps({'enabled':False,'daily_limit':None,'quiet_start':None,'quiet_end':None,
                                               'min_interval_seconds':0,'pause_until':0,'halt_reason':None})}
            for k,v in defaults.items(): db.execute('INSERT OR IGNORE INTO proactive_meta VALUES(?,?)',(k,v))
            if db.execute("SELECT value FROM proactive_meta WHERE key='schema'").fetchone()[0] != '1':
                raise AppError('PROACTIVE_SCHEMA','主动消息数据版本不兼容，未重置。')

    def settings(self, db=None):
        if db is None:
            with self.store.db() as conn: return self.settings(conn)
        return json.loads(db.execute("SELECT value FROM proactive_meta WHERE key='settings'").fetchone()[0])

    def control(self, command):
        action = command.get('action'); t = self.clock()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE'); s = self.settings(db)
            if action in ('enable','disable'): s['enabled'] = action=='enable'
            elif action == 'pause-today':
                tomorrow = datetime.fromtimestamp(t,TZ).date()+timedelta(days=1)
                s['pause_until'] = datetime.combine(tomorrow,datetime.min.time(),TZ).timestamp()
            elif action == 'pause':
                hours = command.get('hours',24)
                if type(hours) not in (int,float) or not 0 < hours <= 168: raise AppError('PROACTIVE_INPUT','暂停时长须为0至168小时之间。')
                s['pause_until'] = t+hours*3600
            elif action == 'resume': s['pause_until'] = 0; s['enabled'] = True
            elif action == 'acknowledge-unknown': s['halt_reason'] = None
            elif action == 'set':
                if 'daily_limit' in command:
                    limit = command['daily_limit']
                    if limit is not None and (type(limit) is not int or not 0 <= limit <= 1000): raise AppError('PROACTIVE_INPUT','每日限额须为0至1000，或unlimited。')
                    s['daily_limit'] = limit
                if 'interval' in command:
                    interval = command['interval']
                    if type(interval) is not int or not 0 <= interval <= 86400: raise AppError('PROACTIVE_INPUT','间隔须为0至86400秒。')
                    s['min_interval_seconds'] = interval
                if 'quiet' in command:
                    quiet = command['quiet']
                    if quiet == 'off': s['quiet_start']=s['quiet_end']=None
                    elif isinstance(quiet,str) and re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d',quiet):
                        s['quiet_start'],s['quiet_end']=quiet.split('-')
                    else: raise AppError('PROACTIVE_INPUT','勿扰格式为23:00-09:00或off；相同起止时间表示全天勿扰。')
            else: raise AppError('PROACTIVE_INPUT','不支持的主动消息操作。')
            db.execute("UPDATE proactive_meta SET value=? WHERE key='settings'",(json.dumps(s),))
            db.execute('INSERT INTO proactive_settings_log VALUES(?,?,?,?)',(str(uuid.uuid4()),action,json.dumps(s),t))
        return {'proactive_control':True,'settings':s,'texts':['主动消息设置已生效。'+('暂停至 '+datetime.fromtimestamp(s['pause_until'],TZ).isoformat() if s['pause_until']>t else '')]}

    def add(self, db, key, kind, topic=None, source=None, task=None, due=None, status='pending', reason=None):
        t = self.clock(); identity = str(uuid.uuid4()); due = t if due is None else due
        db.execute('''INSERT OR IGNORE INTO proactive_candidates
          (id,event_key,kind,topic_id,topic_version,source_id,source_version,task_id,topic_key,due,expires,created,status,reason)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (identity,key,kind,topic['id'] if topic else None,topic['version'] if topic else None,
           source['source_id'] if source else None,source['version'] if source else None,task,
           topic['id'] if topic else 'task:'+task if task else key,due,due+TTL,t,status,reason))
        return db.execute('SELECT id FROM proactive_candidates WHERE event_key=?',(key,)).fetchone()[0]

    def discover(self):
        self.memory.ensure_ready()
        enabled = self.memory.settings()['enabled']
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            meta = dict(db.execute('SELECT key,value FROM proactive_meta'))
            topics = [dict(r) for r in db.execute("SELECT * FROM memories WHERE status='active' AND kind IN ('topic','goal')") if enabled[r['kind']]]
            versions = db.execute('SELECT rowid AS seq,* FROM source_versions WHERE rowid>? ORDER BY rowid LIMIT 50',(int(meta['source_cursor']),)).fetchall()
            for row in versions:
                v = dict(row)
                try:
                    snap = self.sources.snapshot(v['source_id'],v['version'])
                except AppError as exc:
                    self.add(db,'source:'+v['source_id']+':'+str(v['version']),'material',source=v,status='failed',reason=exc.code)
                    db.execute("UPDATE proactive_meta SET value=? WHERE key='source_cursor'",(str(v['seq']),))
                    continue
                matches=[]
                for topic in topics:
                    topic_text = re.sub(r'^(待续话题：|用户目标：)','',topic['content'])
                    overlap = terms(topic_text) & terms(snap['content'][:4000]+' '+snap['title'])
                    if len(overlap)>=2 or (len(topic_text)>=2 and topic_text in snap['content'][:4000]): matches.append((len(overlap),topic))
                matches.sort(key=lambda x:(x[0],x[1]['updated']),reverse=True)
                watched = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watch_runs'").fetchone() and db.execute(
                    "SELECT 1 FROM watch_runs WHERE source_id=? AND version=? AND status='changed'",(v['source_id'],v['version'])).fetchone()
                if watched:
                    cid=self.add(db,'watch:'+v['source_id']+':'+str(v['version']),'material',source=v)
                    db.execute('UPDATE proactive_candidates SET topic_key=? WHERE id=?',('watch:'+v['source_id'],cid))
                elif matches:
                    topic=matches[0][1]
                    self.add(db,'material:'+v['text_hash']+':'+topic['id']+':'+str(topic['version']),'material',topic=topic,source=v)
                else:
                    self.add(db,'source:'+v['source_id']+':'+str(v['version']),'material',source=v,status='suppressed',reason='UNRELATED')
                db.execute("UPDATE proactive_meta SET value=? WHERE key='source_cursor'",(str(v['seq']),))
            for topic in topics:
                if topic['updated'] >= meta['since']:
                    due = datetime.fromisoformat(topic['updated']).timestamp()+86400
                    self.add(db,'topic:'+topic['id']+':'+str(topic['version']),'followup',topic=topic,due=due)
            tasks = db.execute('''SELECT * FROM jobs j WHERE j.rowid>? AND kind='task' AND status IN ('succeeded','failed','cancelled','outcome_unknown')
                AND NOT EXISTS(SELECT 1 FROM proactive_candidates p WHERE p.event_key='task:'||j.id||':'||j.attempts) ORDER BY rowid LIMIT 50''',(int(meta['task_since']),)).fetchall()
            labels={'succeeded':'已完成','failed':'失败','cancelled':'已取消','outcome_unknown':'结果未知，需核对'}
            for j in tasks:
                cid=self.add(db,'task:'+j['id']+':'+str(j['attempts']),'task',task=j['id'])
                body='任务'+j['id']+'第'+str(j['attempts'])+'次执行：'+labels[j['status']]+'。可用 task get 查看当前进展与成果。'
                db.execute('UPDATE proactive_candidates SET body=? WHERE id=?',(body,cid))

    def followup(self, identity, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 30*86400: raise AppError('PROACTIVE_INPUT','回访时间须为1秒至30天。')
        m = self.memory.get(identity)
        if m['status']!='active' or m['kind'] not in ('topic','goal') or not self.memory.settings()['enabled'][m['kind']]:
            raise AppError('PROACTIVE_TOPIC','只能回访当前有效且已开启的目标或未完话题。')
        due=self.clock()+seconds
        with self.store.db() as db:
            cid = self.add(db,'scheduled:'+str(uuid.uuid4()),'followup',topic=m,due=due)
        return {'candidate_id':cid,'due':due}

    def valid(self, candidate, db):
        if candidate['topic_id']:
            m = db.execute('SELECT * FROM memories WHERE id=?',(candidate['topic_id'],)).fetchone()
            if not m or m['status']!='active' or m['version']!=candidate['topic_version']: return False
            if db.execute('SELECT value FROM memory_meta WHERE key=?',('enabled.'+m['kind'],)).fetchone()[0]!='1': return False
        return True

    def gate(self, candidate, db, delivering=False):
        t = self.clock(); s = self.settings(db)
        if not self.valid(candidate,db): return 'MEMORY_CHANGED'
        if t >= candidate['expires']: return 'EXPIRED'
        if not s['enabled']: return 'DISABLED'
        if t < candidate['due']: return 'NOT_DUE'
        if t < s['pause_until']: return 'PAUSED'
        if s['quiet_start'] is not None:
            minute = datetime.fromtimestamp(t,TZ).strftime('%H:%M'); start,end=s['quiet_start'],s['quiet_end']
            if (start<=minute<end if start<end else minute>=start or minute<end): return 'QUIET'
        if candidate['kind']=='task': return None
        mute=db.execute('SELECT until FROM proactive_mutes WHERE topic_key=?',(candidate['topic_key'],)).fetchone()
        if mute and (mute[0]<0 or mute[0]>t): return 'TOPIC_COOLDOWN'
        if db.execute("SELECT 1 FROM inbox_messages WHERE topic_key=? AND status='active' AND feedback IS NULL",(candidate['topic_key'],)).fetchone(): return 'AWAITING_REPLY'
        last=db.execute("SELECT MAX(i.created) FROM inbox_messages i JOIN proactive_candidates p ON p.id=i.candidate_id WHERE p.kind<>'task'").fetchone()[0]
        if last is not None and t-last<s['min_interval_seconds']: return 'INTERVAL'
        if s['halt_reason']: return s['halt_reason']
        if not delivering:
            day=datetime.fromtimestamp(t,TZ).date().isoformat()
            used=db.execute('SELECT COUNT(*) FROM proactive_calls WHERE day=?',(day,)).fetchone()[0]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='care_events'").fetchone():
                start=datetime.combine(datetime.fromtimestamp(t,TZ).date(),datetime.min.time(),tzinfo=TZ).timestamp()
                used+=db.execute("SELECT COUNT(*) FROM care_events WHERE job_id IS NOT NULL AND created>=?",(start,)).fetchone()[0]
            if s['daily_limit'] is not None and used>=s['daily_limit']: return 'DAILY_LIMIT'
        return None

    def recover(self):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            pending=db.execute("SELECT 1 FROM proactive_calls WHERE status='reserved'").fetchone()
            if pending:
                db.execute("UPDATE proactive_calls SET status='outcome_unknown' WHERE status='reserved'")
                db.execute("UPDATE proactive_candidates SET status='outcome_unknown',reason='INTERRUPTED',body=NULL WHERE status='generating'")
                s=self.settings(db); s['halt_reason']='UNKNOWN_USAGE'
                db.execute("UPDATE proactive_meta SET value=? WHERE key='settings'",(json.dumps(s),))

    def candidates(self):
        self.memory.ensure_ready()
        with self.store.db() as db: return [dict(r) for r in db.execute('SELECT * FROM proactive_candidates ORDER BY created DESC,rowid DESC LIMIT 100')]

    def select(self):
        self.memory.ensure_ready()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT * FROM proactive_candidates WHERE status IN ('pending','ready') ORDER BY CASE WHEN kind='task' THEN 0 ELSE 1 END,due,rowid LIMIT 200").fetchall()
            for row in rows:
                c=dict(row); reason=self.gate(c,db,delivering=c['status']=='ready')
                if reason:
                    status='revoked' if reason=='MEMORY_CHANGED' else 'expired' if reason=='EXPIRED' else c['status']
                    db.execute('UPDATE proactive_candidates SET reason=?,status=?,body=CASE WHEN ? THEN NULL ELSE body END WHERE id=?',(reason,status,status in ('revoked','expired'),c['id']))
                    continue
                if c['status']=='ready' or c['kind']=='task': return c
                day=datetime.fromtimestamp(self.clock(),TZ).date().isoformat()
                db.execute("INSERT INTO proactive_calls VALUES(?,?,'reserved',?,?)",(c['id'],day,self.clock(),c['id']+':1'))
                db.execute("UPDATE proactive_candidates SET status='generating',reason=NULL,attempts=1 WHERE id=?",(c['id'],))
                c['status']='generating'; return c
        return None

    def context(self, candidate):
        self.memory.ensure_ready()
        with self.store.db() as db:
            if not self.valid(candidate,db): raise AppError('INBOX_REVOKED','相关记忆已变化，旧消息上下文已撤销。')
        result={'trigger':candidate['kind'],'explicitly_scheduled':candidate['event_key'].startswith('scheduled:'),'explicitly_subscribed':candidate['event_key'].startswith('watch:')}
        if candidate['topic_id']:
            m=self.memory.get(candidate['topic_id']); result['topic']={'id':m['id'],'version':m['version'],'content':m['content']}
        if candidate['source_id']:
            snap=self.sources.snapshot(candidate['source_id'],candidate['source_version'])
            result['source']={k:snap[k] for k in ('source_id','version','title','uri')}
            result['source']['excerpt']=snap['content'][:1600]
        if candidate['task_id']:
            task=self.store.get(candidate['task_id']); result['task']={'id':task['id'],'status':task['status'],'artifact':(task['result'] or {}).get('artifact')}
        return result

    def publish(self, candidate):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM proactive_candidates WHERE id=?',(candidate['id'],)).fetchone()
            if row['status'] not in ('pending','ready'): return
            reason=self.gate(dict(row),db,delivering=True)
            if reason:
                db.execute('UPDATE proactive_candidates SET reason=? WHERE id=?',(reason,row['id'])); return
            body=row['body']
            if row['kind']=='task' and not body:
                task=db.execute('SELECT status FROM jobs WHERE id=?',(row['task_id'],)).fetchone()
                labels={'succeeded':'已完成','failed':'失败','cancelled':'已取消','outcome_unknown':'结果未知，需核对'}
                body='任务'+row['task_id']+'：'+labels.get(task[0],task[0])+'。可用 task get 查看进展与成果。'
            db.execute("INSERT OR IGNORE INTO inbox_messages VALUES(?,?,?,?,?,NULL,NULL,'active',?)",(str(uuid.uuid4()),row['id'],row['topic_key'],body,self.clock(),str(uuid.uuid4())))
            db.execute("UPDATE proactive_candidates SET status='delivered',reason=NULL,body=NULL WHERE id=?",(row['id'],))

    def runtime_config(self):
        target=Path(self.memory.runtime_config())
        cfg=json.loads(target.read_text())
        root=self.store.root/'proactive-workspace'; root.mkdir(mode=0o700,exist_ok=True)
        from .capabilities import IDENTITY
        atomic_write(root/'AGENTS.md',IDENTITY+'仅根据本轮给定资料表达，不使用工具，不保存记忆，不声称执行任何新行动。主动消息按用户要求返回JSON，用户回复则自然中文。')
        cfg['agents']['defaults']['workspace']=str(root)
        p=self.store.root/'proactive-openclaw.json'; atomic_write(p,json.dumps(cfg,ensure_ascii=False,indent=2))
        return str(p)

    def cycle(self, runner, owner_fd, tick):
        self.discover()
        with self.store.db() as db:
            if db.execute("SELECT 1 FROM jobs WHERE status='queued'").fetchone(): return
        c=self.select()
        if not c: return
        if c['kind']=='task' or c['status']=='ready': self.publish(c); return
        try:
            context=self.context(c)
            template=(Path(__file__).parent/'prompts/proactive.txt').read_text()
            message=template.replace('{context}',json.dumps(context,ensure_ascii=False))
            def cancel():
                stop=tick()
                if stop: return stop
                with self.store.db() as db:
                    return 'cancel' if self.gate(c,db,delivering=True) else None
            local=dict(self.c,_owner_fd=owner_fd,_cancel_check=cancel,_request_id=c['id']+':1',_task_id=c['id'],_candidate_id=c['id'],_runtime_config=self.runtime_config())
            if cancel(): raise AppError('PROACTIVE_CANCELLED','主动生成开始前设置已变化。')
            result=runner(local,message,str(uuid.uuid4()),'proactive')
            text=''.join(result.get('texts',[])).strip()
            if text.startswith('```'):
                text=re.sub(r'^```(?:json)?\s*|\s*```$','',text).strip()
            if result.get('error'): raise AppError(result['error']['code'],result['error']['message'])
            value=json.loads(text)
            if not isinstance(value,dict) or type(value.get('send')) is not bool or not isinstance(value.get('message'),str): raise ValueError()
            body=value['message'].strip()
            if value['send'] and (not body or len(body)>500 or not re.search(r'[?？]',body) or result.get('tools')): raise ValueError()
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE proactive_calls SET status='finished' WHERE candidate_id=?",(c['id'],))
                if result.get('usage') is None or result.get('estimated_usd') is None:
                    s=self.settings(db); s['halt_reason']='UNKNOWN_USAGE' if result.get('usage') is None else 'UNKNOWN_COST'
                    db.execute("UPDATE proactive_meta SET value=? WHERE key='settings'",(json.dumps(s),))
                db.execute("UPDATE proactive_candidates SET status=?,body=?,reason=? WHERE id=? AND status='generating'",('ready' if value['send'] else 'suppressed',body if value['send'] else None,None if value['send'] else 'MODEL_DECLINED',c['id']))
            self.publish(c)
        except Exception as exc:
            code=exc.code if isinstance(exc,AppError) else 'PROACTIVE_FORMAT' if isinstance(exc,(ValueError,TypeError)) else 'PROACTIVE_UNKNOWN'
            unknown = 'result' not in locals() or result.get('usage') is None
            state = 'outcome_unknown' if unknown else 'failed'
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE proactive_calls SET status=? WHERE candidate_id=?",(state,c['id']))
                db.execute("UPDATE proactive_candidates SET status=?,reason=?,body=NULL WHERE id=? AND status='generating'",(state,code,c['id']))
                # Never retry automatically; an unobserved result can already have incurred cost.
                if unknown or result.get('estimated_usd') is None:
                    s=self.settings(db); s['halt_reason']='UNKNOWN_USAGE' if unknown else 'UNKNOWN_COST'
                    db.execute("UPDATE proactive_meta SET value=? WHERE key='settings'",(json.dumps(s),))

    def inbox(self, unread=False):
        self.memory.ensure_ready()
        with self.store.db() as db:
            return [dict(r) for r in db.execute('''SELECT i.*,p.kind,p.source_id,p.source_version,p.topic_id,p.task_id
                FROM inbox_messages i JOIN proactive_candidates p ON p.id=i.candidate_id
                WHERE (?=0 OR (i.read_at IS NULL AND i.status='active')) ORDER BY i.created DESC,i.rowid DESC LIMIT 100''',(int(unread),))]

    def read(self, identity, mark=True):
        self.memory.ensure_ready()
        with self.store.db() as db:
            row=db.execute('SELECT * FROM inbox_messages WHERE id=?',(identity,)).fetchone()
            if not row: raise AppError('INBOX_NOT_FOUND','找不到该消息。')
            m=dict(row); candidate=dict(db.execute('SELECT * FROM proactive_candidates WHERE id=?',(m['candidate_id'],)).fetchone())
            if m['status']!='active' or not self.valid(candidate,db): raise AppError('INBOX_REVOKED','相关记忆已变化，消息正文与上下文已撤销。')
            if mark: db.execute('UPDATE inbox_messages SET read_at=COALESCE(read_at,?) WHERE id=?',(self.clock(),identity))
        m['context']=self.context(candidate)
        with self.store.db() as db:has_care=db.execute("SELECT 1 FROM sqlite_master WHERE name='care_events'").fetchone()
        if has_care:
            from .care import Care
            m['context'].update(Care(self.c).context(candidate))
        return m

    def feedback(self, identity, action):
        m=self.read(identity,False)
        if action not in ('interested','later','not-useful','stop-topic'): raise AppError('INBOX_FEEDBACK','不支持的反馈。')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE inbox_messages SET feedback=? WHERE id=?',(action,identity))
            db.execute('INSERT INTO proactive_feedback VALUES(?,?,?,?)',(str(uuid.uuid4()),identity,action,self.clock()))
            if action!='interested':
                until=-1 if action=='stop-topic' else self.clock()+(86400 if action=='later' else TTL)
                db.execute('INSERT OR REPLACE INTO proactive_mutes VALUES(?,?)',(m['topic_key'],until))
        return {'texts':['反馈已保存；稍后聊暂停该话题24小时，不相关暂停7天，不再聊持续关闭该话题。' if action!='interested' else '已记录感兴趣。'],'proactive_control':True}

    def unmute(self, topic):
        with self.store.db() as db: db.execute('DELETE FROM proactive_mutes WHERE topic_key=?',(topic,))
        return {'texts':['已解除该话题的冷却。'],'proactive_control':True}

    def reply(self, identity, text):
        m=self.read(identity,False)
        if not isinstance(text,str) or not text.strip() or len(text)>12000: raise AppError('INPUT','回复须为1–12000字符。')
        control=natural_control(text)
        if control: self.control(control)
        job=self.store.create(json.dumps({'message_id':identity,'text':text},ensure_ascii=False),kind='reply',session=m['session'],category='chat')
        with self.store.db() as db: db.execute('INSERT INTO inbox_replies VALUES(?,?)',(job['id'],identity))
        return job

    def replied(self, jid):
        with self.store.db() as db:
            row=db.execute('SELECT message_id FROM inbox_replies WHERE job_id=?',(jid,)).fetchone()
            if row: db.execute("UPDATE inbox_messages SET feedback='replied',read_at=COALESCE(read_at,?) WHERE id=?",(self.clock(),row[0]))

    def task(self, identity, goal):
        if not isinstance(goal,str) or not goal.strip() or len(goal)>12000: raise AppError('INPUT','任务目标须为1–12000字符。')
        m=self.read(identity,False); source=m['context'].get('source')
        refs=[source['source_id']+'@'+str(source['version'])] if source else []
        prior=(m['context'].get('task') or {}).get('artifact')
        inputs=[prior['path']] if prior and not refs else []
        job=self.store.create('用户基于这条主动消息明确委托：'+m['body']+'\n本次任务目标：'+goal,sources=refs,inputs=inputs)
        with self.store.db() as db: db.execute("UPDATE inbox_messages SET feedback='task_requested' WHERE id=?",(identity,))
        return job
