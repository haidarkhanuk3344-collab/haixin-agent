"""Official prepaid balance, freshness and fail-closed model admission."""
from decimal import Decimal,InvalidOperation
import json,time,threading
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError,URLError
from .app import AppError,atomic_write,credential
from .jobs import Store
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
class Balance:
 lock=threading.Lock()
 def __init__(self,c,clock=time.time,fetch=None):self.c=c;self.clock=clock;self.path=Store(c).root/'official-balance.json';self.fetch=fetch or self.remote
 def remote(self):
  cfg=json.loads((self.c['state']/'openclaw.json').read_text());primary=cfg['agents']['defaults']['model']['primary'];provider=primary.split('/')[0];url=cfg['models']['providers'][provider]['baseUrl'].rstrip('/')
  if url not in ('https://api.deepseek.com','https://api.deepseek.com/v1'):raise AppError('BALANCE_PROVIDER','当前模型不是已核对的DeepSeek官方地址，未向其他地址发送凭据。')
  req=Request('https://api.deepseek.com/user/balance',headers={'Authorization':'Bearer '+credential(self.c),'Accept':'application/json'})
  with build_opener(NoRedirect).open(req,timeout=8) as r:
   raw=r.read(65537)
   if len(raw)>65536:raise ValueError('SIZE')
   return json.loads(raw)
 def cached(self):
  try:r=json.loads(self.path.read_text())
  except (OSError,ValueError):return {'status':'unknown','checked_at':None,'balances':[]}
  age=max(0,self.clock()-r.get('checked_at',0));r['age_seconds']=round(age)
  if age>60 and r['status'] in ('available','insufficient'):r['status']='stale'
  return r
 def refresh(self):
  with self.lock:
   try:
    value=self.fetch()
    if type(value.get('is_available')) is not bool or not isinstance(value.get('balance_infos'),list) or not value['balance_infos']:raise ValueError('FORMAT')
    balances=[]
    for v in value['balance_infos']:
     if v['currency'] not in ('CNY','USD'):raise ValueError('CURRENCY')
     amounts={k:Decimal(v[k]) for k in ('total_balance','granted_balance','topped_up_balance')}
     if not all(n.is_finite() for n in amounts.values()):raise ValueError('NUMBER')
     balances.append(dict(currency=v['currency'],**{k:str(n) for k,n in amounts.items()}))
    result={'status':'available' if value['is_available'] else 'insufficient','is_available':value['is_available'],'balances':balances,'checked_at':self.clock(),'source':'DeepSeek官方余额'}
   except (OSError,ValueError,TypeError,KeyError,InvalidOperation,AppError,HTTPError,URLError):
    result={'status':'unknown','is_available':None,'balances':[],'checked_at':self.clock(),'source':'DeepSeek官方余额','error':'余额暂时无法核对，模型请求已保留；普通提醒继续。'}
   atomic_write(self.path,json.dumps(result,ensure_ascii=False));return result
 def admit(self):
  r=self.cached()
  if r['status'] in ('stale','unknown'):
   # Avoid repeated failed checks in one loop; never interpret unknown as zero.
   if r.get('checked_at') is None or self.clock()-r['checked_at']>=15 or r['status']=='stale':r=self.refresh()
  if r['status']=='insufficient':raise AppError('BALANCE_INSUFFICIENT','API余额不足，本次模型请求尚未发出。充值后核对余额继续；已有普通提醒照常运行。')
  if r['status']!='available':raise AppError('BALANCE_UNKNOWN','暂时无法核对API余额，本次模型请求尚未发出。请稍后检查余额；已有普通提醒照常运行。')
  return r
 def exhausted(self):atomic_write(self.path,json.dumps({'status':'insufficient','is_available':False,'balances':[],'checked_at':self.clock(),'source':'DeepSeek官方402响应'}))

 def resume_waiting(self):
  if self.cached()['status']!='available':return 0
  with Store(self.c).db() as db:
   return db.execute("UPDATE jobs SET status='queued',attempts=MAX(0,attempts-1),error=NULL WHERE status='waiting_balance' AND cancel_requested=0").rowcount
