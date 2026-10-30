"""Bounded lexical retrieval over explicitly saved source versions; never a web crawl."""
import re
from .app import AppError
from .sources import Sources

def terms(text):
    words=set(re.findall(r'[a-zA-Z0-9_]{3,}',text.lower()))
    for run in re.findall(r'[\u4e00-\u9fff]+',text):words.update(run[i:i+2] for i in range(len(run)-1))
    return words-{'什么','怎么','这个','那个','可以','我的','你的','请问','告诉','一下','一下','资料','材料','内容','是否'}

def retrieve(c,query):
    source=Sources(c);q=terms(query);ranked=[]
    if len(q)<2:return []
    for row in source.list():
        if not row['current_version']:continue
        title=row.get('title') or row['locator'];title_score=len(q&terms(title))*4
        try:snap=source.snapshot(row['id'])
        except AppError:continue
        # Search chunks rather than supplying entire files or irrelevant preceding text.
        content=snap['content'];chunks=[content[i:i+600] for i in range(0,min(len(content),60000),450)]
        best=sorted(((len(q&terms(chunk)),chunk) for chunk in chunks),key=lambda x:x[0],reverse=True)[:2]
        if not best:continue
        score=title_score+best[0][0]
        if score<4 or (best[0][0]<2 and title_score<8):continue
        ranked.append((score,{'source_id':row['id'],'version':snap['version'],'title':title,'excerpt':'\n…\n'.join(x[1] for x in best if x[0]>0)[:1200]}))
    ranked.sort(key=lambda x:x[0],reverse=True)
    return [r[1] for r in ranked[:3]]


def search(c, query, offset=0):
    """Tool-facing retrieval covers full verified source bodies with pagination."""
    source = Sources(c)
    with source.store.db() as db:
        rows = [dict(r) for r in db.execute('SELECT id FROM sources WHERE current_version>0 ORDER BY updated DESC')]
    q = terms(query)
    ranked = []
    for row in rows:
        try: snap = source.snapshot(row['id'])
        except AppError: continue
        title = snap['title'] or row['id']
        title_score = len(q & terms(title)) * 4
        chunks = [(i, snap['content'][i:i+800]) for i in range(0,len(snap['content']),650)]
        best = sorted(((len(q & terms(chunk)),i,chunk) for i,chunk in chunks),reverse=True)[:2]
        score = title_score + (best[0][0] if best else 0)
        if q and not score:continue
        ranked.append((score,{'source_id':row['id'],'version':snap['version'],'title':title,
                             'matches':[{'offset':i,'excerpt':chunk} for n,i,chunk in best if n>0],
                             'total_chars':len(snap['content'])}))
    ranked.sort(key=lambda x:x[0],reverse=True)
    return {'sources':[r[1] for r in ranked[offset:offset+6]],'next_offset':offset+6 if offset+6<len(ranked) else None,
            'total':len(ranked),'method':'全文分块词项检索，可换词及分页补读；未使用向量语义检索。'}
