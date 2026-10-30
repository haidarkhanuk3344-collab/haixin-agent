"""Loopback UI adapter. Existing services remain the source of truth."""
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
import uuid
from urllib.parse import urlsplit,parse_qs

from .app import AppError,ROOT,atomic_write,now,summary
from .jobs import Store,workspace
from .sources import Sources
from .memory import Memory
from .proactive import Proactive,natural_control
from .watch import Watch
from .schedules import Schedules
from .life_memory import LifeMemory
from .affairs import Affairs
from .notifications import NativeNotifications
from . import worker


def identity(value):
    try:return str(uuid.UUID(value))
    except (ValueError,TypeError,AttributeError):raise AppError('INPUT','记录编号无效。')


def text(body,key,limit=12000):
    value=body.get(key)
    if not isinstance(value,str) or not value.strip() or len(value)>limit:raise AppError('INPUT','请填写有效内容（最多%d字）。'%limit)
    return value.strip()


class UI:
    def __init__(self,c,autostart=True):
        self.c,self.autostart=c,autostart
        self.store=Store(c);self.memory=Memory(c);self.sources=Sources(c);self.proactive=Proactive(c);self.watch=Watch(c)
        self.lock=threading.Lock()
        with self.store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS web_requests(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,status TEXT NOT NULL,result TEXT,created TEXT NOT NULL);
            UPDATE web_requests SET status='outcome_unknown' WHERE status='reserved';''')
        key=self.store.root/'web-cookie.txt'
        if not key.exists():atomic_write(key,secrets.token_urlsafe(32))
        self.cookie=key.read_text().strip()

    def job(self,jid):
        j=self.store.get(identity(jid));result=j['result'] or {}
        goal=j['goal']
        if j['kind']=='reply':
            try:goal=json.loads(goal)['text']
            except (ValueError,KeyError):goal='回复主动消息'
        if j['kind'] in ('source','memory'):goal='读取材料' if j['kind']=='source' else '更新记忆'
        if j['kind']=='scheduled' and j['status']!='succeeded':result={}
        mirrored=False
        if j['kind']=='scheduled':
            with self.store.db() as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='affairs'").fetchone():mirrored=bool(db.execute("SELECT 1 FROM appointments a JOIN affairs f ON f.id=a.id WHERE a.delivery_job=? AND a.mode='reminder'",(jid,)).fetchone())
        return {k:j[k] for k in ('id','kind','status','created','updated','error','cancel_requested')} | {'mirrored_reminder':mirrored,'goal':goal,'texts':result.get('texts',[]),'steps':[{'name':r['name'],'status':r['status']} for r in j['steps']], 'has_artifact':bool(result.get('artifact')),'inputs':j['inputs'],'session':j['session']}

    def state(self):
        jobs=[self.job(j['id']) for j in self.store.list() if j['kind'] in ('chat','reply','scheduled','task','memory','source','care')]
        jobs=[j for j in jobs if j['kind']!='care' or j['status']=='succeeded' and j['texts']]
        return {'affairs':Affairs(self.c).list(include_closed=True),'reminders':Affairs(self.c).notices(),'notifications':NativeNotifications(self.c).cached(),'balance':__import__('personal_agent.balance',fromlist=['Balance']).Balance(self.c).cached(),'personal_memories':LifeMemory(self.c).list(history=True),'personal_memory_settings':LifeMemory(self.c).settings(),'jobs':jobs,'appointments':Schedules(self.c).list(),'inbox':self.proactive.inbox(),'sources':self.sources.list(),'watches':self.watch.list(),'memories':self.memory.list(),'memory_settings':self.memory.settings(),'settings':self.proactive.settings(),'worker':worker.status(self.c),'usage':summary(self.c),'time':now()}

    def request(self,rid):
        with self.store.db() as db:r=db.execute('SELECT * FROM web_requests WHERE id=?',(identity(rid),)).fetchone()
        if not r:raise AppError('NOT_FOUND','尚未收到此请求，输入未自动重发。')
        return {'request_id':r['id'],'status':r['status'],'result':json.loads(r['result']) if r['result'] else None}

    def action(self,body):
        if not isinstance(body,dict):raise AppError('INPUT','请求格式无效。')
        rid=identity(body.get('id'));fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            with self.store.db() as db:
                r=db.execute('SELECT * FROM web_requests WHERE id=?',(rid,)).fetchone()
                if r:
                    if r['fingerprint']!=fingerprint:raise AppError('CONFLICT','同一个提交编号不能用于不同内容。')
                    return self.request(rid)
                db.execute('INSERT INTO web_requests VALUES(?,?,?,NULL,?)',(rid,fingerprint,'reserved',now()))
            try:
                result=self.dispatch(body);status='completed'
            except AppError as exc:result=exc.result();status='failed'
            except Exception:result={'error':{'code':'OUTCOME_UNKNOWN','message':'本次提交结果未知，请核对任务列表，未自动重发。'}};status='outcome_unknown'
            with self.store.db() as db:db.execute('UPDATE web_requests SET status=?,result=? WHERE id=?',(status,json.dumps(result,ensure_ascii=False),rid))
            if status=='completed' and self.autostart and result.get('job_id'):
                try:worker.start(self.c)
                except AppError:pass # The saved job stays visible and can be started explicitly.
            return self.request(rid)

    def dispatch(self,b):
        action=b.get('action');jid=None
        if action=='chat':
            message=text(b,'text');control=natural_control(message)
            if control:return self.proactive.control(control)
            jid=self.store.create(message,kind='chat',session=identity(b.get('session')),category='chat')['id']
        elif action=='reply':jid=self.proactive.reply(identity(b.get('message_id')),text(b,'text'))['id']
        elif action=='task':
            refs=b.get('sources',[])
            if not isinstance(refs,list) or len(refs)>8 or any(not isinstance(x,str) for x in refs):raise AppError('INPUT','最多选择8份材料。')
            jid=self.store.create(text(b,'text'),sources=refs)['id']
        elif action=='inbox_task':jid=self.proactive.task(identity(b.get('message_id')),text(b,'text'))['id']
        elif action=='source':
            kind=b.get('kind');value=text(b,'text')
            if kind=='twitter':cmd=self.sources.twitter_command(value)
            elif kind=='url':cmd=self.sources.url_command(value)
            elif kind=='text':cmd=self.sources.text_command(value,title=b.get('title','粘贴材料')[:200])
            else:raise AppError('INPUT','请选择推特账号、公开网页或文字材料。')
            jid=self.store.create(json.dumps(cmd,ensure_ascii=False),kind='source',category='source')['id']
        elif action=='memory':
            op=b.get('op')
            if op not in ('correct','forget','enable','disable'):raise AppError('INPUT','不支持的记忆操作。')
            if op=='forget' and b.get('confirmed') is not True:raise AppError('CONFIRM','请确认忘记此条记忆。')
            cmd={'action':op,'id':b.get('target'),'content':b.get('text'),'kind':b.get('kind','all')}
            jid=self.store.create(json.dumps(cmd,ensure_ascii=False),kind='memory',category='memory')['id']
        elif action=='personal_memory':
            op=b.get('op');life=LifeMemory(self.c)
            if op=='forget':
                if b.get('confirmed') is not True:raise AppError('CONFIRM','请确认忘记范围。')
                return life.forget(ids=[identity(b['target'])] if b.get('target') else [],query=b.get('query',''),all_records=b.get('all') is True)
            if op=='correct':
                value=text(b,'text',1000);cmd={'action':'personal-correct','content':value,'id':identity(b.get('target'))};jid=self.store.create(json.dumps(cmd,ensure_ascii=False),kind='memory',category='memory')['id'];return {'job_id':jid}
            raise AppError('INPUT','不支持的个人记忆操作。')
        elif action=='balance':
            from .balance import Balance
            balance=Balance(self.c);r=balance.refresh();r['resumed']=balance.resume_waiting();return r
        elif action=='reminder_read':
            import time
            with self.store.db() as db:db.execute('UPDATE affair_notices SET read_at=? WHERE id=?',(time.time(),identity(b.get('target'))))
            return {'read':True}
        elif action=='notifications':
            native=NativeNotifications(self.c)
            if b.get('op')=='authorize':return native.request()
            if b.get('op')=='check':return native.check()
            raise AppError('INPUT','不支持的通知操作。')
        elif action=='affair':
            op=b.get('op')
            if op not in ('complete','cancel','delete','stop_reminding'):raise AppError('INPUT','请在对话中补充或修改事项。')
            if op=='delete' and b.get('confirmed') is not True:raise AppError('CONFIRM','请确认删除事项。')
            return Affairs(self.c).act({'id':identity(b['id']),'goal':op,'session':identity(b.get('session'))},'ui',op,identity=identity(b.get('target')),version=b.get('version'))
        elif action=='schedule_cancel':return Schedules(self.c).cancel(identity(b.get('target')),identity(b.get('session')))
        elif action=='cancel':return self.job(self.store.cancel(identity(b.get('target')))['id'])
        elif action=='watch':
            op=b.get('op');sid=identity(b.get('target'))
            if op not in ('add','pause','resume','check','remove'):raise AppError('INPUT','不支持的订阅操作。')
            r=self.watch.add(sid) if op=='add' else self.watch.control(sid,op)
            if self.autostart and op in ('add','resume','check'):worker.start(self.c)
            return r
        elif action=='proactive':
            if b.get('op') not in ('enable','disable','resume'):raise AppError('INPUT','不支持的主动交流设置。')
            r=self.proactive.control({'action':b['op']})
            if self.autostart and b['op']!='disable':worker.start(self.c)
            return r
        elif action=='feedback':return self.proactive.feedback(identity(b.get('target')),b.get('feedback'))
        elif action=='read':return self.proactive.read(identity(b.get('target')))
        elif action=='worker':
            if b.get('op')=='start':return worker.start(self.c)
            if b.get('op')=='stop':return worker.stop(self.c)
            raise AppError('INPUT','不支持的后台操作。')
        else:raise AppError('INPUT','不支持的操作。')
        return {'job_id':jid}

    def artifact(self,jid):
        j=self.store.get(identity(jid));a=(j.get('result') or {}).get('artifact')
        if j['status']!='succeeded' or not a:raise AppError('NOT_FOUND','成果尚未通过核验。')
        path=Path(a['path']);root=workspace(self.c)/'tasks'/j['id']/('attempt-'+str(j['attempts']))
        if path.resolve()!=root.resolve()/'result.md' or path.is_symlink() or any(p.is_symlink() for p in (root,*root.parents)):raise AppError('PATH','成果路径已变化。')
        raw=path.read_bytes()
        if len(raw)>1048576 or hashlib.sha256(raw).hexdigest()!=a['sha256']:raise AppError('ARTIFACT_CHANGED','成果已变化，请先核对文件。')
        return raw


class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,ui,dist=None):
        self.ui=ui;self.dist=dist or ROOT/'frontend/dist';super().__init__(address,Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,code,data,kind='application/json; charset=utf-8',cookie=False):
        raw=json.dumps(data,ensure_ascii=False).encode() if kind.startswith('application/json') else data
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:self.send_header('Set-Cookie','agent_ui='+self.server.ui.cookie+'; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers();self.wfile.write(raw)
    def guard(self,api=False,write=False):
        host='127.0.0.1:'+str(self.server.server_port)
        if self.headers.get('Host')!=host or self.headers.get('Sec-Fetch-Site') in ('cross-site',):raise AppError('FORBIDDEN','仅允许从本机入口访问。')
        origin=self.headers.get('Origin')
        if origin and origin!='http://'+host:raise AppError('FORBIDDEN','请求来源不匹配。')
        if write and origin!='http://'+host:raise AppError('FORBIDDEN','请从本机页面提交。')
        if api:
            cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''));item=cookie.get('agent_ui')
            if not item or not secrets.compare_digest(item.value,self.server.ui.cookie) or self.headers.get('X-Agent-UI')!='1':raise AppError('FORBIDDEN','连接已过期，请重新打开个人Agent页面。')
    def do_GET(self):self.handle_request(False)
    def do_POST(self):self.handle_request(True)
    def handle_request(self,write):
        try:
            path=urlsplit(self.path);api=path.path.startswith('/api/');self.guard(api,write)
            ui=self.server.ui;params=parse_qs(path.query);rid=params.get('id',[''])[0]
            if write:
                if path.path!='/api/action' or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise AppError('INPUT','请求类型不支持。')
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=100000:raise AppError('INPUT','请求为空或过大。')
                result=ui.action(json.loads(self.rfile.read(size)));self.send(200,result);return
            if path.path=='/api/state':result=ui.state()
            elif path.path=='/api/job':result=ui.job(rid)
            elif path.path=='/api/request':result=ui.request(rid)
            elif path.path=='/api/personal_memory':result=LifeMemory(ui.c).history(identity(rid))
            elif path.path=='/api/source':result=ui.sources.snapshot(identity(rid))
            elif path.path=='/api/artifact':self.send(200,ui.artifact(rid),'text/plain; charset=utf-8');return
            elif path.path=='/health':result={'service':'personal-agent-ui','ready':True}
            elif api:raise AppError('NOT_FOUND','页面或记录不存在。')
            else:
                name='index.html' if path.path=='/' else path.path.lstrip('/')
                asset=(self.server.dist/name).resolve()
                if not asset.is_relative_to(self.server.dist.resolve()) or not asset.is_file():raise AppError('NOT_FOUND','页面不存在，请重新打开入口。')
                self.send(200,asset.read_bytes(),mimetypes.guess_type(str(asset))[0] or 'application/octet-stream',cookie=name=='index.html');return
            self.send(200,result)
        except AppError as exc:self.send(403 if exc.code=='FORBIDDEN' else 404 if exc.code=='NOT_FOUND' else 400,exc.result())
        except (ValueError,TypeError,KeyError):self.send(400,{'error':{'code':'INPUT','message':'请求内容无效。'}})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self.send(500,{'error':{'code':'LOCAL_ERROR','message':'本机处理未完成，请查看任务状态，不要重复提交。'}})
