"""Explicit source subscriptions, polled only by the existing idle worker."""
import json
from pathlib import Path
import time
import uuid

from .app import AppError
from .jobs import Store
from .sources import Sources


class Watch:
    def __init__(self,c,clock=time.time):
        self.c,self.store,self.clock=c,Store(c),clock
        self.sources=Sources(c)
        with self.store.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS watch_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              INSERT OR IGNORE INTO watch_meta VALUES('schema','1');
              CREATE TABLE IF NOT EXISTS source_watches(
                source_id TEXT PRIMARY KEY,kind TEXT NOT NULL,locator TEXT NOT NULL,canonical TEXT NOT NULL,
                enabled INTEGER NOT NULL,generation INTEGER NOT NULL,interval_seconds INTEGER NOT NULL,
                next_due REAL NOT NULL,last_checked REAL,last_changed REAL,failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,active_run TEXT,created REAL NOT NULL,updated REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS watch_runs(
                id TEXT PRIMARY KEY,source_id TEXT NOT NULL,generation INTEGER NOT NULL,status TEXT NOT NULL,
                started REAL NOT NULL,finished REAL,before_version INTEGER,version INTEGER,error_code TEXT,error_message TEXT);
              CREATE TABLE IF NOT EXISTS watch_changes(
                id TEXT PRIMARY KEY,source_id TEXT NOT NULL,action TEXT NOT NULL,value TEXT NOT NULL,created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS watch_due ON source_watches(enabled,next_due);
            ''')
            if db.execute("SELECT value FROM watch_meta WHERE key='schema'").fetchone()[0]!='1':
                raise AppError('WATCH_SCHEMA','来源订阅版本不兼容，未重置数据。')

    @staticmethod
    def interval(value):
        if type(value) is not int or not 60<=value<=86400:raise AppError('WATCH_INTERVAL','检查间隔须为60–86400秒；默认900秒。')
        return value

    def add(self,sid,every=900):
        every=self.interval(every);t=self.clock()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            source=db.execute('SELECT * FROM sources WHERE id=?',(sid,)).fetchone()
            if not source or source['kind'] not in ('file','url','twitter-user','twitter-search') or source['current_version'] is None:
                raise AppError('WATCH_SOURCE','请先成功导入指定文件、公开URL或推特来源，再订阅该材料编号；文字片段不可刷新。')
            if source['kind'].startswith('twitter-') and every<900:raise AppError('WATCH_INTERVAL','推特来源至少间隔15分钟检查，以控制外部请求频率。')
            if not db.execute('SELECT 1 FROM source_watches WHERE source_id=?',(sid,)).fetchone():
                db.execute('''INSERT INTO source_watches(source_id,kind,locator,canonical,enabled,generation,interval_seconds,next_due,created,updated)
                    VALUES(?,?,?,?,1,1,?,?,?,?)''',(sid,source['kind'],source['locator'],source['canonical'],every,t+every,t,t))
                self.log(db,sid,'add',{'interval_seconds':every,'scope':'仅此已导入来源，允许持续只读检查'},t)
        return self.get(sid)

    def log(self,db,sid,action,value,t):
        db.execute('INSERT INTO watch_changes VALUES(?,?,?,?,?)',(str(uuid.uuid4()),sid,action,json.dumps(value,ensure_ascii=False),t))

    def control(self,sid,action,every=None):
        if every is not None:self.interval(every)
        t=self.clock()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM source_watches WHERE source_id=?',(sid,)).fetchone()
            if not row:raise AppError('WATCH_NOT_FOUND','找不到此来源订阅。')
            if action in ('pause','remove'):
                db.execute('UPDATE source_watches SET enabled=0,generation=generation+1,updated=? WHERE source_id=?',(t,sid))
            elif action=='resume':
                interval=every if every is not None else row['interval_seconds']
                if row['kind'].startswith('twitter-') and interval<900:raise AppError('WATCH_INTERVAL','推特来源至少间隔15分钟检查。')
                db.execute('''UPDATE source_watches SET enabled=1,generation=generation+1,interval_seconds=?,
                    next_due=?,failures=0,last_error=NULL,updated=? WHERE source_id=?''',(interval,t+interval,t,sid))
            elif action=='check':
                if not row['enabled']:raise AppError('WATCH_PAUSED','此订阅已暂停，请先检查失败原因并显式resume。')
                if not row['active_run']:db.execute('UPDATE source_watches SET next_due=?,updated=? WHERE source_id=?',(t,t,sid))
            else:raise AppError('WATCH_ACTION','不支持的来源订阅操作。')
            self.log(db,sid,action,{'interval_seconds':every},t)
        return self.get(sid)

    def list(self):
        with self.store.db() as db:return [dict(r) for r in db.execute('SELECT * FROM source_watches ORDER BY created DESC')]

    def get(self,sid):
        with self.store.db() as db:
            row=db.execute('SELECT * FROM source_watches WHERE source_id=?',(sid,)).fetchone()
            if not row:raise AppError('WATCH_NOT_FOUND','找不到此来源订阅。')
            r=dict(row);r['runs']=[dict(x) for x in db.execute('SELECT * FROM watch_runs WHERE source_id=? ORDER BY rowid DESC LIMIT 30',(sid,))]
        return r

    def recover(self):
        # A process may have published a source version before stopping. Never replay that read.
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT * FROM watch_runs WHERE status='checking'").fetchall()
            for r in rows:
                db.execute("UPDATE watch_runs SET status='outcome_unknown',finished=?,error_code='INTERRUPTED',error_message=? WHERE id=?",
                           (self.clock(),'上次检查中断；保留已有版本，未自动重发。',r['id']))
                db.execute('''UPDATE source_watches SET enabled=0,generation=generation+1,active_run=NULL,last_error=?,updated=?
                    WHERE source_id=? AND active_run=?''',('上次检查结果未知，请核对材料版本后resume。',self.clock(),r['source_id'],r['id']))

    def reserve(self):
        t=self.clock()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1").fetchone():return None
            if db.execute("SELECT 1 FROM watch_runs WHERE status='checking' LIMIT 1").fetchone():return None
            w=db.execute('SELECT * FROM source_watches WHERE enabled=1 AND active_run IS NULL AND next_due<=? ORDER BY next_due,source_id LIMIT 1',(t,)).fetchone()
            if not w:return None
            s=db.execute('SELECT current_version FROM sources WHERE id=?',(w['source_id'],)).fetchone()
            rid=str(uuid.uuid4());version=s[0] if s else None
            db.execute("INSERT INTO watch_runs(id,source_id,generation,status,started,before_version) VALUES(?,?,?,'checking',?,?)",(rid,w['source_id'],w['generation'],t,version))
            db.execute('UPDATE source_watches SET active_run=?,updated=? WHERE source_id=?',(rid,t,w['source_id']))
            result=dict(w);result.update(id=rid,before_version=version)
            return result

    def valid(self,db,run):
        w=db.execute('SELECT * FROM source_watches WHERE source_id=?',(run['source_id'],)).fetchone()
        if not w or not w['enabled'] or w['generation']!=run['generation'] or w['active_run']!=run['id']:
            raise AppError('WATCH_CANCELLED','订阅已暂停或设置已变化，本次不发布新版本。')
        s=db.execute('SELECT kind,locator,canonical FROM sources WHERE id=?',(run['source_id'],)).fetchone()
        if not s or any(s[k]!=run[k] for k in ('kind','locator','canonical')):
            raise AppError('WATCH_SCOPE','来源位置已变化，停止订阅；请核对授权范围。')
        row=db.execute('SELECT stop_requested FROM worker WHERE singleton=1').fetchone()
        if row and row[0]:raise AppError('WATCH_INTERRUPTED','后台停止，未发布本次读取。')
        if db.execute("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1").fetchone():
            raise AppError('WATCH_YIELD','有用户请求，来源检查让出执行机会。')

    def cycle(self,tick=lambda:None):
        run=self.reserve()
        if run is None:return None
        reason={}
        def check():
            if tick():reason.update(code='WATCH_INTERRUPTED',message='后台停止，本次检查中断。');return True
            try:
                with self.store.db() as db:self.valid(db,run)
            except AppError as exc:reason.update(code=exc.code,message=exc.message);return True
            return False
        def publish(db):self.valid(db,run)
        result=None;error=None
        try:
            if check():raise AppError(**reason)
            if run['kind']=='file':
                path=Path(run['locator'])
                if not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):
                    raise AppError('WATCH_PATH','订阅文件路径出现符号链接或发生重定向，未读取。')
            command=self.sources.refresh_command(run['source_id'],strict=True)
            if command['canonical']!=run['canonical']:raise AppError('WATCH_SCOPE','来源范围变化，未读取。')
            command['authorization']='用户明确创建watch订阅，仅检查原来源；watch_run='+run['id']
            job={'id':run['id'],'attempts':1,'goal':json.dumps(command,ensure_ascii=False)}
            result=self.sources.ingest(job,check,publish_guard=publish)
            if result.get('error'):error=reason or result['error']
        except AppError as exc:error={'code':exc.code,'message':exc.message}
        except Exception:error={'code':'WATCH_UNKNOWN','message':'检查出现本地异常，结果未知；未自动重发。'}
        return self.finish(run,result,error)

    def finish(self,run,result,error):
        t=self.clock();status='unchanged' if result and result.get('duplicate') else 'changed'
        if error:
            status='cancelled' if error['code'] in ('WATCH_CANCELLED','SOURCE_CANCELLED') else 'deferred' if error['code']=='WATCH_YIELD' else 'outcome_unknown' if error['code'] in ('WATCH_UNKNOWN','WATCH_INTERRUPTED') else 'failed'
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE watch_runs SET status=?,finished=?,version=?,error_code=?,error_message=? WHERE id=?',
                       (status,t,result.get('version') if result else None,error['code'] if error else None,error['message'] if error else None,run['id']))
            row=db.execute('SELECT * FROM source_watches WHERE source_id=?',(run['source_id'],)).fetchone()
            if row and row['active_run']==run['id']:
                db.execute('UPDATE source_watches SET active_run=NULL WHERE source_id=?',(run['source_id'],))
                if row['generation']==run['generation']:
                    failures=row['failures']+1 if status=='failed' else 0 if not error else row['failures']
                    enabled=row['enabled'] and status!='outcome_unknown' and failures<3 and (not error or error['code'] not in ('WATCH_SCOPE','WATCH_PATH','TWITTER_AUTH','TWITTER_SETUP','TWITTER_TIMEOUT','TWITTER_RATE_LIMIT'))
                    delay=min(row['interval_seconds']*2**failures,86400) if status=='failed' else row['interval_seconds']
                    if status=='deferred':delay=60
                    db.execute('''UPDATE source_watches SET enabled=?,next_due=?,last_checked=?,last_changed=CASE WHEN ?='changed' THEN ? ELSE last_changed END,
                        failures=?,last_error=?,updated=? WHERE source_id=?''',
                               (int(enabled),t+delay,t,status,t,failures,error['message'] if error else None,t,run['source_id']))
            output=dict(db.execute('SELECT * FROM watch_runs WHERE id=?',(run['id'],)).fetchone())
        return output
