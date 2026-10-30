"""Thirty-day snapshots; restore replays the current deletion ledgers before any recall."""
from datetime import datetime,timezone
import hashlib,json,os,shutil,sqlite3,time,uuid
from pathlib import Path
from .app import AppError,ROOT,atomic_write,exclusive
from .jobs import Store,workspace
class Backups:
 def __init__(self,c,clock=time.time):
  self.c,self.clock=c,clock;self.store=Store(c)
  default_root=ROOT/'backups/automatic' if c.get('_config_path')==str(ROOT/'config.local.json') else c['state']/'personal-backups'
  self.root=Path(c.get('_backup_root',default_root));self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
  self.identity=self.store.root/'backup-instance'
  if not self.identity.exists():atomic_write(self.identity,str(uuid.uuid4()))
 def create(self):
  name=datetime.fromtimestamp(self.clock(),timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8];dest=self.root/name;dest.mkdir(mode=0o700)
  product=dest/'product';product.mkdir(mode=0o700)
  with self.store.db() as db:
   with sqlite3.connect(product/'product.sqlite3') as copy:db.backup(copy)
  for name in ('memory-deletions.jsonl','life-deletions.jsonl','life-delete-key','backup-instance'):
   p=self.store.root/name
   if p.exists():shutil.copy2(p,product/name)
  if (self.store.root/'sources').exists():shutil.copytree(self.store.root/'sources',product/'sources')
  (dest/'workspace').mkdir()
  tasks=workspace(self.c)/'tasks'
  if tasks.exists():
   for p in tasks.rglob('*.md'):
    if p.is_symlink() or any(v.is_symlink() for v in p.parents):continue
    out=dest/'workspace'/p.relative_to(workspace(self.c));out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,out)
  files=[{'path':str(p.relative_to(dest)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in dest.rglob('*') if p.is_file()]
  atomic_write(dest/'manifest.json',json.dumps({'format':'haixin-backup-1','instance':self.identity.read_text(),'created':self.clock(),'files':files},ensure_ascii=False))
  for p in dest.rglob('*'):p.chmod(0o700 if p.is_dir() else 0o600)
  self.prune();return dest
 def prune(self):
  removed=[]
  for p in self.root.iterdir():
   if not p.is_dir() or p.is_symlink():continue
   try:m=json.loads((p/'manifest.json').read_text())
   except (OSError,ValueError):continue
   if m.get('format')=='haixin-backup-1' and m.get('instance')==self.identity.read_text() and m.get('created',self.clock())<self.clock()-30*86400:
    shutil.rmtree(p);removed.append(p.name)
  return removed
 def daily(self):
  latest=[]
  for p in self.root.glob('*/manifest.json'):
   try:m=json.loads(p.read_text())
   except (OSError,ValueError):continue
   if m.get('instance')==self.identity.read_text():latest.append(m['created'])
  if not latest or self.clock()-max(latest)>=86400:return self.create()
  return None
 def restore(self,path):
  """Use only with both product services stopped. A safety snapshot preserves newer data."""
  path=Path(path).resolve()
  if path.parent!=self.root.resolve():raise AppError('BACKUP_SCOPE','只能恢复本产品备份目录中的完整备份。')
  try:m=json.loads((path/'manifest.json').read_text())
  except (OSError,ValueError):raise AppError('BACKUP_INVALID','备份清单不可读。')
  if m.get('format')!='haixin-backup-1' or m.get('instance')!=self.identity.read_text():raise AppError('BACKUP_SCOPE','备份不属于当前个人Agent数据。')
  for item in m['files']:
   p=path/item['path']
   if not p.resolve().is_relative_to(path) or p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise AppError('BACKUP_INVALID','备份文件完整性核验失败。')
  with exclusive(self.c['state']),exclusive(self.c['state'],name='personal-agent-web.lock'):
   safety=self.create()
   # Keep current journals and HMAC key: a rollback must not roll back forgetting.
   latest={n:(self.store.root/n).read_bytes() for n in ('memory-deletions.jsonl','life-deletions.jsonl','life-delete-key') if (self.store.root/n).exists()}
   for p in (path/'product').rglob('*'):
    if p.is_file():
     out=self.store.root/p.relative_to(path/'product');out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,out)
   for n,v in latest.items():(self.store.root/n).write_bytes(v)
   for p in (path/'workspace').rglob('*'):
    if p.is_file():
     out=workspace(self.c)/p.relative_to(path/'workspace');out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,out)
   from .memory import Memory
   from .life_memory import LifeMemory
   Memory(self.c).replay_deletions();LifeMemory(self.c).replay()
   # A snapshot cannot restore ownership of a process. Both locks are held here.
   with self.store.db() as db:db.execute("DELETE FROM worker")
   self.store.recover()
  return {'restored':str(path),'safety_backup':str(safety),'deletions_replayed':True}
