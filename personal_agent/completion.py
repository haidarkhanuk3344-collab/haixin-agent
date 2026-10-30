"""Evidence-based completion gate; supplements deterministic artifact validation."""
import json
from pathlib import Path
import uuid
from .app import AppError
from .memory import Memory


def review(c, job, result, capabilities, runner, saved):
    pending=capabilities.directory/'result.pending.md'
    content=pending.read_text() if pending.is_file() else None
    with capabilities.store.db() as db:
        rows=[dict(r) for r in db.execute('SELECT name,status,result FROM agent_tool_runs WHERE job_id=? AND attempt=? ORDER BY rowid',(job['id'],job['attempts']))]
    evidence=[]
    for row in rows:
        item=json.loads(row['result']) if row['result'] else {'error':'结果未知'}
        # Keep tool evidence bounded without discarding source facts or error states.
        evidence.append({'tool':row['name'],'state':row['status'],'result':item})
    from .life_memory import LifeMemory
    facts=[{k:r[k] for k in ('id','content','kind')} for r in Memory(c).list()+LifeMemory(c).list() if r['id'] in saved]
    data={'saved_memory_facts':facts,'memory_learning':result.get('memory_learning',{}),'user_request':job['goal'],'response':result.get('texts',[]),
          'artifact':content,'artifact_chars':len(content) if content is not None else None,
          'saved_memory_ids':list(saved),'tools':evidence}
    prompt=(Path(__file__).parent/'prompts/completion-review.md').read_text()+'\n待核验记录：\n'+json.dumps(data,ensure_ascii=False)
    if len(prompt)>31000:
        # Never silently approve a report whose evidence was cut off.
        raise AppError('REVIEW_SIZE','本轮完成核验材料超出窗口，成果已保留，请分段核对。')
    config=Memory(c).extraction_config()
    local={k:v for k,v in c.items() if k not in ('_task_workspace','_task_attempt','_agent_tools','_tool_env')}
    local.update(_runtime_config=config,_request_id=job['id']+':review:'+str(uuid.uuid4()))
    answer=runner(local,prompt,str(uuid.uuid4()),'verification')
    if answer.get('error'):raise AppError('REVIEW_FAILED','完成核验调用未成功，成果保留，不能标记全部完成。')
    text='\n'.join(answer.get('texts',[])).strip()
    if text.startswith('```'):text='\n'.join(text.splitlines()[1:-1]).strip()
    try:
        decision=json.loads(text)
        if type(decision.get('satisfied')) is not bool or not isinstance(decision.get('reason'),str):raise ValueError()
    except (ValueError,AttributeError):raise AppError('REVIEW_FORMAT','完成核验返回格式无效，未按成功发布。')
    return {'satisfied':decision['satisfied'],'reason':decision['reason'][:2000],'request_id':answer.get('request_id')}


def complete(c, job, result, capabilities, runner, saved, session, normalize=None):
    """A tool-free answer is still reviewed; a rejected answer resumes the same loop."""
    reviews=[]
    for correction in range(3):
        capabilities.active()
        result['agent_tools']=capabilities.receipts()
        result['agent_document_written']=(capabilities.directory/'result.pending.md').is_file()
        with capabilities.store.db() as db:
            result['affair_receipts']=[json.loads(r[0]) for r in db.execute("SELECT result FROM agent_tool_runs WHERE job_id=? AND name='haixin_affairs' AND status='succeeded'",(job['id'],)) if r[0] and json.loads(r[0]).get('saved')]
        if normalize:result=normalize(result)
        decision=review(c,job,result,capabilities,runner,saved)
        capabilities.active()
        reviews.append(decision)
        capabilities.store.step(job,'completion-review','succeeded' if decision['satisfied'] else 'running',decision)
        if decision['satisfied']:
            return dict(result,completion_reviews=reviews)
        if correction==2:
            raise AppError('GOAL_UNFINISHED','交付核验仍未通过，已保留工作记录和暂存成果：'+decision['reason'])
        capabilities.active()
        result=runner(dict(c,_request_id=job['id']+':correction:'+str(correction)),
            '可信交付核验发现尚未达到用户要求，请在本会话继续调用工具修正后交付，不只口头承诺。用户本轮请求：'+job['goal']+'\n核验反馈：'+decision['reason'],session,job['category'])
        if result.get('error'):
            return dict(result,completion_reviews=reviews)
