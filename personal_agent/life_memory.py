"""Personal long/short memory: exact evidence, temporal validity and deletion replay."""
from datetime import datetime,timezone,timedelta
import hashlib,hmac,json,os,re,secrets,time,uuid
from pathlib import Path
from .app import AppError,atomic_write,now
from .jobs import Store

KINDS=('background','relationship','experience','decision','context')

def digest(text):return hashlib.sha256(re.sub(r'\s','',text).encode()).hexdigest()
def decode(result):
    if result.get('error') or result.get('tools'):raise ValueError('MODEL')
    value='\n'.join(result.get('texts',[])).strip()
    if value.startswith('```'):value=re.sub(r'^```(?:json)?\s*|\s*```$','',value)
    return json.loads(value)

class LifeMemory:
    def __init__(self,c,clock=time.time):
        self.c,self.store,self.clock=c,Store(c),clock
        self.root=self.store.root;self.journal=self.root/'life-deletions.jsonl'
        with self.store.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS life_memories(id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,
              content TEXT,subject TEXT,certainty TEXT NOT NULL,occurred TEXT,expires REAL,
              source_job TEXT,source_seq INTEGER,version INTEGER NOT NULL,status TEXT NOT NULL,
              created TEXT NOT NULL,updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS life_versions(memory_id TEXT,version INTEGER,content TEXT,subject TEXT,
              source_job TEXT,occurred TEXT,created TEXT,status TEXT,PRIMARY KEY(memory_id,version));
            CREATE TABLE IF NOT EXISTS life_observations(job_id TEXT PRIMARY KEY,fingerprint TEXT,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS life_blocks(fingerprint TEXT PRIMARY KEY,kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS life_journal_applied(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS life_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO life_settings VALUES('enabled','1');
            INSERT OR IGNORE INTO life_settings VALUES('short_ttl_days','7');
            ''')
        key=self.root/'life-delete-key'
        if not key.exists():atomic_write(key,secrets.token_hex(32))
        self.key=key.read_text().encode()
    def token(self,value):return hmac.new(self.key,re.sub(r'\s','',value).encode(),hashlib.sha256).hexdigest()
    def settings(self):
        with self.store.db() as db:d=dict(db.execute('SELECT key,value FROM life_settings'))
        return {'enabled':d['enabled']=='1','short_ttl_days':int(d['short_ttl_days'])}
    def ready(self):
        if not self.journal.exists():return
        try:events=[json.loads(line)['id'] for line in self.journal.read_text().splitlines()]
        except (ValueError,KeyError):raise AppError('MEMORY_JOURNAL','遗忘记录损坏，已停止记忆召回。')
        with self.store.db() as db:applied={r[0] for r in db.execute('SELECT id FROM life_journal_applied')}
        if not set(events)<=applied:raise AppError('MEMORY_RECOVERY_REQUIRED','请先重放删除记录再使用记忆。')
    def blocked(self,text,source=None):
        with self.store.db() as db:blocks={r[0] for r in db.execute('SELECT fingerprint FROM life_blocks')}
        return self.matches(text,blocks,source)
    def matches(self,text,blocks,source=None):
        if not blocks:return False
        if self.token(text) in blocks or (source and self.token('source:'+source) in blocks):return True
        # Only deletion subjects are stored as hashes, never recoverable plaintext.
        for chunk in re.findall(r'[\w\u4e00-\u9fff]+',text):
            for length in range(2,min(40,len(chunk))+1):
                if any(self.token(chunk[i:i+length]) in blocks for i in range(len(chunk)-length+1)):return True
        return False
    def expire(self):
        with self.store.db() as db:
            count=db.execute("UPDATE life_memories SET status='expired' WHERE status='active' AND scope='short' AND expires<=?",(self.clock(),)).rowcount
            if count:self.rotate(db)
        return count
    @staticmethod
    def rotate(db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_meta'").fetchone():
            from .memory import Memory
            Memory.rotate(db)
    def list(self,history=False):
        self.ready();self.expire()
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM life_memories WHERE status IN ('active','expired') AND content IS NOT NULL ORDER BY updated DESC" if history else "SELECT * FROM life_memories WHERE status='active' AND content IS NOT NULL ORDER BY updated DESC")]
    def search(self,query,limit=8):
        if not self.settings()['enabled']:return []
        from .memory import terms
        q=terms(query);rows=self.list();general=not query or bool(re.search('记忆|记得|近况|最近|经历|了解我',query))
        ranked=sorted(((len(q&terms(r['content']))+int(general)+int(r['scope']=='short'),r['updated'],r) for r in rows),key=lambda v:(v[0],v[1]),reverse=True)
        return [r for score,_,r in ranked if score][:limit]
    def validate(self,entry,text,known):
        fields={'quote','kind','scope','certainty','subject','target','occurred','expires_at'}
        if not isinstance(entry,dict) or set(entry)!=fields:raise ValueError('FIELDS')
        q=entry['quote']
        if not isinstance(q,str) or not 2<=len(q)<=1000 or q not in text:raise ValueError('QUOTE')
        # Exact sentence boundaries prevent truncating negation/hypothetical clauses.
        start=text.index(q);end=start+len(q)
        if start and text[start-1] not in '。！？\n；':raise ValueError('PARTIAL_QUOTE')
        if end<len(text) and q[-1] not in '。！？\n；' and text[end] not in '。！？\n；':raise ValueError('PARTIAL_QUOTE')
        if entry['kind'] not in KINDS or entry['scope'] not in ('long','short') or entry['certainty'] not in ('stated','uncertain'):raise ValueError('CLASS')
        if not isinstance(entry['subject'],str) or not 2<=len(entry['subject'])<=40 or entry['subject'] not in q:raise ValueError('SUBJECT')
        if not isinstance(entry['occurred'],str) or len(entry['occurred'])>120 or (entry['occurred'] and entry['occurred'] not in q):raise ValueError('TIME')
        if entry['target']!='new' and entry['target'] not in known:raise ValueError('TARGET')
        if entry['scope']=='long' and entry['expires_at'] is not None:raise ValueError('LONG_EXPIRY')
        expires=None
        if entry['scope']=='short':
            expires=self.clock()+self.settings()['short_ttl_days']*86400
            if re.search(r'这周|本周',q):
                from zoneinfo import ZoneInfo
                local=datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai'))
                expires=(local+timedelta(days=7-local.weekday())).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            elif entry['expires_at']:
                parsed=datetime.fromisoformat(entry['expires_at'])
                if parsed.tzinfo is None:raise ValueError('TIMEZONE')
                expires=parsed.timestamp()
                if not self.clock()<expires<=self.clock()+366*86400:raise ValueError('EXPIRY')
        return dict(entry,expires=expires)
    def save(self,entry,job,force=False):
        self.ready()
        if not self.settings()['enabled'] or self.blocked(entry['quote'],job['id']):return None
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM life_memories WHERE id=?',(entry['target'],)).fetchone() if entry['target']!='new' else None
            duplicate=db.execute("SELECT id FROM life_memories WHERE content=? AND status='active'",(entry['quote'],)).fetchone()
            if duplicate and (not old or (old['content']==entry['quote'] and old['scope']==entry['scope'])):return None
            if old and old['status']=='forgotten':return None
            if old and job.get('seq',0)<=old['source_seq'] and not force:return None
            mid=old['id'] if old else str(uuid.uuid4());version=old['version']+1 if old else 1;stamp=now()
            if old:
                db.execute("UPDATE life_versions SET status='superseded' WHERE memory_id=?",(mid,));self.rotate(db)
            db.execute('INSERT OR REPLACE INTO life_memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(mid,entry['scope'],entry['kind'],entry['quote'],entry['subject'],entry['certainty'],entry['occurred'],entry['expires'],job['id'],job.get('seq',0),version,'active',old['created'] if old else stamp,stamp))
            db.execute('INSERT INTO life_versions VALUES(?,?,?,?,?,?,?,?)',(mid,version,entry['quote'],entry['subject'],job['id'],entry['occurred'],stamp,'active'))
            # One fact has one current owner; preserve the older representation as history.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memories'").fetchone():
                for m in db.execute("SELECT * FROM memories WHERE status='active' AND kind IN ('goal','topic')").fetchall():
                    fact=re.sub(r'^(?:用户明确表达：|用户目标：|待续话题：)','',m['content']).strip('。')
                    if len(fact)>5 and any(fact in q for q in [entry['quote']]+[v[0] for v in db.execute("SELECT content FROM life_versions WHERE memory_id=? AND content IS NOT NULL",(mid,))]):
                        db.execute("UPDATE memories SET status='superseded' WHERE id=?",(m['id'],))
                        db.execute("UPDATE memory_versions SET status='superseded' WHERE memory_id=?",(m['id'],))
                        db.execute('DELETE FROM memory_index WHERE memory_id=?',(m['id'],))
                        from .proactive import invalidate
                        invalidate(db,memory_id=m['id']);self.rotate(db)

        return mid
    def learn(self,job,runner,local):
        from .memory import Memory,no_memory_request
        self.ready();self.last_learning={'status':'skipped','model_called':False}
        text=job['goal']
        if job['kind']!='chat' or job['category']!='chat':return []
        with self.store.db() as db:
            if db.execute('SELECT 1 FROM life_observations WHERE job_id=?',(job['id'],)).fetchone():return []
            db.execute('INSERT INTO life_observations VALUES(?,?,?)',(job['id'],digest(text),'started'))
        if not self.settings()['enabled'] or no_memory_request(text) or self.blocked(text,job['id']) or not 8<=len(text)<=6000:
            with self.store.db() as db:db.execute("UPDATE life_observations SET status='skipped' WHERE job_id=?",(job['id'],))
            return []
        known=self.list();prompt=(Path(__file__).parent/'prompts/life-memory.md').read_text()
        from zoneinfo import ZoneInfo
        prompt+='\n'+json.dumps({'now':datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai')).isoformat(),'user_message':text,'current_memories':[{k:r[k] for k in ('id','content','kind','scope','updated')} for r in known[:30]]},ensure_ascii=False)
        config=Memory(self.c).extraction_config();self.last_learning['model_called']=True
        saved=[]
        for attempt in range(2):
            try:
                result=runner(dict(local,_runtime_config=config,_request_id=job['id']+':life-memory:'+str(attempt+1)),prompt,str(uuid.uuid4()),'memory')
                body=decode(result)
                if not isinstance(body,dict) or set(body)!={'items'} or not isinstance(body['items'],list) or len(body['items'])>4:raise ValueError('FORMAT')
                entries=[self.validate(v,text,{r['id'] for r in known}) for v in body['items']]
                if local.get('_cancel_check',lambda:None)():raise AppError('CANCELLED','整理已停止。')
                saved=[mid for e in entries for mid in [self.save(e,job)] if mid]
                self.last_learning['status']='saved' if saved else 'no_new_memory'
                break
            except AppError:
                self.last_learning['status']='extraction_failed';break
            except (ValueError,TypeError,KeyError) as exc:
                self.last_learning['status']='extraction_failed'
                self.last_learning['validation_error']=str(exc)[:60]
                if attempt==0:
                    prompt+='\n上次输出未通过校验：'+str(exc)[:60]+'。请重新输出完整JSON。quote须从原消息整句复制，不能省略句首或句尾；subject是原句中2至40字的词；不要输出多余字段。上次输出：'+json.dumps(result.get('texts',[]),ensure_ascii=False)
        self.last_learning['attempts']=attempt+1
        with self.store.db() as db:db.execute('UPDATE life_observations SET status=? WHERE job_id=?',(self.last_learning['status'],job['id']))
        return saved
    def forget(self,ids=(),query='',all_records=False,extra_blocks=()):
        self.ready()
        with self.store.db() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM life_memories WHERE status<>'forgotten'")]
            selected=[r for r in rows if all_records or r['id'] in ids or (query and (query in (r['content'] or '') or query in (r['subject'] or '')))]
            versions=[dict(r) for m in selected for r in db.execute('SELECT * FROM life_versions WHERE memory_id=?',(m['id'],))]
        blocks={self.token(v) for v in extra_blocks if v};sources=set()
        for r in selected+versions:
            for field in ('content','subject'):
                if r.get(field):blocks.add(self.token(r[field]))
            if r.get('source_job'):
                token=self.token('source:'+r['source_job']);blocks.add(token);sources.add(token)
        if query:blocks.add(self.token(query))
        event={'id':str(uuid.uuid4()),'ids':[r['id'] for r in selected],'blocks':sorted(blocks),'sources':sorted(sources),'time':now()}
        fd=os.open(str(self.journal),os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
        with os.fdopen(fd,'a') as f:f.write(json.dumps(event)+'\n');f.flush();os.fsync(f.fileno())
        self.replay()
        return {'forgotten_ids':event['ids'],'count':len(selected),'scope':'长短期记忆及派生上下文已清理；原聊天、资料、事项和30天内备份独立保留，恢复前重放遗忘。'}
    def replay(self):
        if not self.journal.exists():return 0
        count=0
        for line in self.journal.read_text().splitlines():
            try:
                e=json.loads(line)
                if not {'id','ids','blocks','time'}<=set(e) or set(e)-{'id','ids','blocks','time','sources','release'} or any(not re.fullmatch('[0-9a-f]{64}',v) for v in e['blocks']+e.get('release',[])+e.get('sources',[])):raise ValueError()
            except (ValueError,KeyError,TypeError):raise AppError('MEMORY_JOURNAL','遗忘记录损坏，已停止召回。')
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM life_journal_applied WHERE id=?',(e['id'],)).fetchone():continue
                for mid in e['ids']:
                    db.execute("UPDATE life_memories SET content=NULL,subject=NULL,occurred=NULL,source_job=NULL,status='forgotten',updated=? WHERE id=?",(e['time'],mid))
                    db.execute("UPDATE life_versions SET content=NULL,subject=NULL,source_job=NULL,occurred=NULL,status='forgotten' WHERE memory_id=?",(mid,))
                for token in e['blocks']:db.execute('INSERT OR IGNORE INTO life_blocks VALUES(?,?)',(token,'forgotten'))
                for token in e.get('release',[]):db.execute('DELETE FROM life_blocks WHERE fingerprint=?',(token,))
                # Tool snapshots are derived context. Keep identifiers/status only.
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_tool_runs'").fetchone():
                    db.execute("UPDATE agent_tool_runs SET result=NULL")
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='care_events'").fetchone():
                    blocks={r[0] for r in db.execute('SELECT fingerprint FROM life_blocks')}
                    for r in db.execute('SELECT e.*,j.goal,j.result FROM care_events e LEFT JOIN jobs j ON j.id=e.job_id').fetchall():
                        if r['memory_id'] not in e['ids'] and not self.matches((r['goal'] or '')+' '+(r['result'] or ''),blocks):continue
                        db.execute("UPDATE proactive_candidates SET status='revoked',body=NULL WHERE id=?",(r['id'],))
                        db.execute("UPDATE inbox_messages SET status='revoked',body=NULL WHERE candidate_id=?",(r['id'],))
                        if r['job_id']:db.execute("UPDATE jobs SET goal='相关记忆已遗忘，跟进已撤销。',result=NULL,cancel_requested=1,status=CASE WHEN status IN ('queued','waiting_balance') THEN 'cancelled' ELSE status END WHERE id=?",(r['job_id'],))
                        db.execute("UPDATE care_events SET status='revoked',body=NULL WHERE id=?",(r['id'],))
                self.rotate(db);db.execute('INSERT INTO life_journal_applied VALUES(?)',(e['id'],));count+=1
        return count
    def history(self,identity):
        self.ready()
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT version,content,occurred,created,status,source_job FROM life_versions WHERE memory_id=? AND status<>'forgotten' ORDER BY version DESC",(identity,))]
    def set_enabled(self,enabled):
        with self.store.db() as db:db.execute("UPDATE life_settings SET value=? WHERE key='enabled'",('1' if enabled else '0',));self.rotate(db)
    def correct(self,identity,quote,job,scope=None):
        rows={r['id']:r for r in self.list(history=True)}
        if identity not in rows:raise AppError('MEMORY_NOT_FOUND','找不到可更正的记忆。')
        if scope is not None and scope not in ('long','short'):raise AppError('MEMORY_SCOPE','记忆范围应为长期或近期。')
        if not isinstance(quote,str) or not 2<=len(quote)<=1000:raise AppError('MEMORY_INPUT','请提供完整的新内容。')
        r=rows[identity];entry={'quote':quote,'kind':r['kind'],'scope':scope or r['scope'],'certainty':'stated','subject':r['subject'],'target':identity,'occurred':'用户于'+now()+'更正','expires':None if (scope or r['scope'])=='long' else self.clock()+self.settings()['short_ttl_days']*86400}
        return self.save(entry,job,force=True)
    def manage(self,action,job,target='',query='',content='',scope=None):
        from .memory import Memory
        legacy=Memory(self.c);text=job['goal']
        if action=='allow':
            if not query or len(query)<2 or query not in text or not re.search(r'重新允许|重新授权|允许重新|重新记住|恢复.*(?:访问|读取|记忆)',text):raise AppError('MEMORY_AUTH','需要用户明确重新授权这个人物或主题。')
            token=self.token(query);release={token}
            if self.journal.exists():
                for line in self.journal.read_text().splitlines():
                    event=json.loads(line)
                    if token in event['blocks'] and 'sources' in event:release.update(set(event['blocks'])-set(event['sources']))
            event={'id':str(uuid.uuid4()),'ids':[],'blocks':[],'release':sorted(release),'time':now()}
            fd=os.open(str(self.journal),os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
            with os.fdopen(fd,'a') as f:f.write(json.dumps(event)+'\n');f.flush();os.fsync(f.fileno())
            self.replay()
            return {'context_reset':True,'texts':['已重新允许使用该主题；已删记忆不会恢复。你可以重新提供当前情况，或明确指定要读取的原资料。']}
        if action=='forget':
            if not re.search(r'忘记|忘掉|遗忘|删除.*记忆|清空.*记忆|不再记',text):raise AppError('MEMORY_AUTH','需要用户明确提出遗忘范围。')
            clear=query=='*'
            if clear and not re.search(r'(?:清空|删除|忘掉|忘记).*(?:全部|所有)|(?:全部|所有).*(?:清空|删除|忘)',text):raise AppError('MEMORY_SCOPE','清空需要明确全部记忆。')
            if query and not clear and (len(query)<2 or query not in text):raise AppError('MEMORY_SCOPE','删除主题必须来自用户原话，范围不清时应先询问。')
            rows=self.list(True);old=legacy.list()
            if target:
                chosen=next((r for r in rows+old if r['id']==target),None)
                if not chosen:raise AppError('MEMORY_NOT_FOUND','找不到要忘记的记忆。')
                if target not in text and not query and not (chosen.get('subject') and chosen['subject'] in text):raise AppError('MEMORY_SCOPE','请明确要忘记哪条记忆或主题。')
            if not target and not query:raise AppError('MEMORY_SCOPE','请说明要忘记的主题或人物。')
            removed=self.forget(ids=[target] if target else [],query='' if clear else query,all_records=clear)
            oldids=[]
            for r in old:
                if clear or r['id']==target or (query and query in r['content']):
                    legacy.control({'action':'forget','id':r['id']},job);oldids.append(r['id'])
            return dict(removed,legacy_ids=oldids,count=removed['count']+len(oldids),context_reset=True,texts=['已忘记指定范围的记忆；原聊天、资料和真实事项保留。'])
        if action in ('correct','promote'):
            if action=='correct' and (not content or content not in text):raise AppError('MEMORY_AUTH','更正内容必须来自用户本轮原话。')
            if action=='promote' and not re.search('长期|记住|一直记',text):raise AppError('MEMORY_AUTH','转为长期记忆需用户明确要求。')
            r=next((x for x in self.list(True) if x['id']==target),None)
            if not r:raise AppError('MEMORY_NOT_FOUND','找不到这条个人记忆。')
            mid=self.correct(target,content if action=='correct' else r['content'],job,'long' if action=='promote' else scope)
            return {'updated_id':mid,'context_reset':True,'texts':['记忆已更新；之后按新内容接续。']}
        raise AppError('MEMORY_ACTION','不支持的记忆操作。')
