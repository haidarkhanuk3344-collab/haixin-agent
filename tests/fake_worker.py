"""Offline test fixture only. Not selectable through production configuration."""
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from personal_agent.app import load_config, AppError
from personal_agent.jobs import workspace
from personal_agent.worker import serve


def fake(c, message, session, category):
    if category == 'memory':
        return {'texts':[json.dumps({'items':[]})]}
    if category == 'verification':
        return {'texts':[json.dumps({'satisfied':True,'reason':'离线夹具；不作真实模型证据'})]}
    marker = c["state"] / "fake-invocations.txt"
    with marker.open("a") as f:
        f.write(c["_task_id"] + "\n")
    deadline = time.monotonic() + (15 if "SLOW" in message else 0.15)
    while time.monotonic() < deadline:
        reason = c["_cancel_check"]()
        if reason:
            return AppError("CANCELLED_UNKNOWN" if reason == "cancel" else "OUTCOME_UNKNOWN", "合成调用中止").result()
        time.sleep(0.03)
    if category == 'proactive':
        return {'texts':[json.dumps({'send':True,'message':'这份资料与你的话题有关，想先聊哪一点？'},ensure_ascii=False)],
                'tools':[], 'usage':{'input':1,'cacheRead':0,'cacheWrite':0,'output':1,'total':2},'estimated_usd':0.0}
    if "MISSING_OUTPUT" not in message:
        found = re.search(r"\n(tasks/[^\n]+/result.pending.md)\n", message)
        if found:
            p = workspace(c) / found.group(1)
            p.write_text("# 合成文档\n已读取指定输入。\n")
        elif '\nresult.pending.md\n' in message:
            p=Path(c['_task_workspace'])/'result.pending.md'
            p.write_text('# 合成文档\n已读取指定输入。\n')
    return {"texts": ["合成回复"], "tools": ["read", "write"], "request_id": c["_request_id"],
            "usage": None, "estimated_usd": None}


if __name__ == "__main__":
    try:
        serve(load_config(sys.argv[1]), runner=fake)
    except AppError as exc:
        print(json.dumps(exc.result(), ensure_ascii=False))
        raise SystemExit(1)
