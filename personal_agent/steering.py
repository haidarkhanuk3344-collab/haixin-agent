"""Carry an in-flight conversation forward at a safe completion boundary."""
import hashlib
from pathlib import Path
from .jobs import Store


def followups(store, job):
    with store.db() as db:
        ids=[r[0] for r in db.execute("SELECT id FROM jobs WHERE session=? AND status='cancelled' ORDER BY rowid DESC LIMIT 30",(job['session'],))]
    return [j for j in (store.get(jid) for jid in ids) if (j.get('result') or {}).get('superseded_by')==job['id']]


def carry_request(store, job):
    previous=followups(store,job)
    if not previous:return job
    prior=previous[0]['result']['prior_request']
    return dict(job,goal='正在办理的原请求：\n'+prior+'\n用户在处理中补充的新要求（以此为准）：\n'+job['goal'])


def defer_to_followup(store, job, result, directory):
    """Never publish obsolete output after a newer message has been queued."""
    if job['kind'] not in ('chat','reply'):return False
    with store.db() as db:
        row=db.execute("SELECT id FROM jobs WHERE session=? AND kind='chat' AND status='queued' AND rowid>(SELECT rowid FROM jobs WHERE id=?) ORDER BY rowid LIMIT 1",(job['session'],job['id'])).fetchone()
    if not row:return False
    carried={'texts':['收到补充，按你新的要求继续。'],'superseded_by':row[0], 'prior_request':job['goal']}
    pending=Path(directory)/'result.pending.md'
    if pending.is_file() and not pending.is_symlink():
        raw=pending.read_bytes()
        carried['draft']={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    carried['prior_request_id']=result.get('request_id')
    store.finish(job['id'],'cancelled',carried)
    return True
