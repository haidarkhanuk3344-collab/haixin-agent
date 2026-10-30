"""Local runtime adapter and CLI. Scheduling lives in the single worker."""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class AppError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

    def result(self):
        return {"error": {"code": self.code, "message": self.message}}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, text):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".agent-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def exclusive(state, name="personal-agent.lock"):
    fd = os.open(str(state / name), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppError("BUSY", "海辛正在处理另一个请求，请稍后再试。")
        yield fd
    finally:
        # A child inherits this fd so a killed parent cannot release the lock early.
        os.close(fd)


def load_config(path=None):
    p = Path(path or os.environ.get("PERSONAL_AGENT_CONFIG", ROOT / "config.local.json")).expanduser().resolve()
    try:
        c = json.loads(p.read_text())
        if c.get("schema_version") != 1:
            raise ValueError()
        for key in ("runtime", "node", "cli", "state", "price_file"):
            value = Path(c[key]).expanduser()
            c[key] = (p.parent / value).resolve() if not value.is_absolute() else value.resolve()
        if not c["runtime"].is_dir() or not c["state"].is_dir():
            raise ValueError()
        for path in (c["node"], c["cli"], c["state"] / "openclaw.json"):
            if not path.is_file():
                raise ValueError()
        if not os.access(c["node"], os.X_OK):
            raise ValueError()
        timeout = c.get("process_timeout_seconds", 120)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 600:
            raise ValueError()
        c["process_timeout_seconds"] = timeout
        if not all(isinstance(c.get(k), str) and c[k] for k in ("keychain_service", "keychain_account")):
            raise ValueError()
        c["_config_path"] = str(p)
        return c
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise AppError("CONFIG", "配置无效或运行文件缺失，请检查 config.local.json；现有数据未重置。")


def credential(c):
    try:
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", c["keychain_account"],
                            "-s", c["keychain_service"], "-w"], capture_output=True, text=True, timeout=15)
        if r.returncode or not r.stdout.strip():
            raise ValueError()
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise AppError("CREDENTIAL", "无法读取指定的 DeepSeek 钥匙串凭据，请检查钥匙串访问权限。")


def redact(text, secret=""):
    if secret:
        text = text.replace(secret, "[已隐藏凭据]")
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[已隐藏凭据]", text)


def session_id(state, new=False):
    path = state / "interactive-session.txt"
    if path.exists() and not new:
        value = path.read_text().strip()
        try:
            uuid.UUID(value)
        except ValueError:
            raise AppError("SESSION", "现有会话标识损坏，已保留原文件；请检查后再选择新会话。")
        return value
    value = str(uuid.uuid4())
    atomic_write(path, value + "\n")
    return value


def validate_usage(raw):
    keys = ("input", "cacheRead", "cacheWrite", "output", "total")
    if not isinstance(raw, dict) or not all(type(raw.get(k)) is int and raw[k] >= 0 for k in keys):
        return None
    if raw["total"] != sum(raw[k] for k in keys[:-1]):
        return None
    return {k: raw[k] for k in keys}


def pricing(c):
    try:
        p = json.loads(c["price_file"].read_text())
        if p["currency"] != "USD" or not all(isinstance(p[k], str) and p[k] for k in ("version", "source", "effective_date", "model")):
            return None
        for k in ("input", "cacheRead", "cacheWrite", "output"):
            v = p["per_million"][k]
            if k == "cacheWrite" and v is None:
                continue
            if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
                return None
        return p
    except (OSError, ValueError, KeyError, TypeError):
        return None


def usage_record(raw, price, model):
    usage = validate_usage(raw)
    estimate = None
    if usage is not None and price and price["model"] == model:
        keys = ("input", "cacheRead", "cacheWrite", "output")
        if all(price["per_million"][k] is not None or usage[k] == 0 for k in keys):
            estimate = sum(usage[k] * (price["per_million"][k] or 0) for k in keys) / 1_000_000
    return {"usage": usage, "usage_state": "known" if usage is not None else "unknown",
            "estimated_usd": estimate, "price": price if estimate is not None else None}


def append_event(state, event):
    data = (json.dumps(dict(schema_version=1, time=now(), **event), ensure_ascii=False) + "\n").encode()
    fd = os.open(str(state / "usage-v1.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def execute(c, args, key, lock_fd):
    reason = c.get("_cancel_check", lambda: None)()
    if reason:
        raise AppError("CANCELLED_UNKNOWN" if reason == "cancel" else "OUTCOME_UNKNOWN",
                       "调用启动前收到停止或取消请求，未启动新的进程。")
    env = os.environ.copy()
    # Capability credentials exist only for the lifetime of this worker turn.
    for name in ('HAIXIN_TOOL_URL','HAIXIN_TOOL_TOKEN'):
        env.pop(name,None)
        if name in c.get('_tool_env',{}):env[name]=c['_tool_env'][name]
    env.update(DEEPSEEK_API_KEY=key, OPENCLAW_STATE_DIR=str(c["state"]),
               OPENCLAW_CONFIG_PATH=c.get("_runtime_config", str(c["state"] / "openclaw.json")), OPENCLAW_SKIP_CRON="1",
               PATH=str(c["node"].parent) + os.pathsep + env.get("PATH", ""))
    p = subprocess.Popen([str(c["node"]), str(c["cli"])] + args, cwd=str(c["runtime"]), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True, pass_fds=(lock_fd,))
    try:
        deadline = time.monotonic() + c["process_timeout_seconds"]
        while True:
            reason = c.get("_cancel_check", lambda: None)()
            if reason:
                raise AppError("CANCELLED_UNKNOWN" if reason == "cancel" else "OUTCOME_UNKNOWN",
                               "调用已中止，可能已产生用量或文件；未自动重试。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(p.args, c["process_timeout_seconds"])
            try:
                stdout, _ = p.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        return p.returncode, redact(stdout, key)
    except BaseException as exc:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.communicate()
        if isinstance(exc, AppError):
            raise
        if not isinstance(exc, (subprocess.TimeoutExpired, KeyboardInterrupt)):
            raise
        raise AppError("OUTCOME_UNKNOWN", "调用已中止，可能已产生用量或文件操作；未自动重试，请先核对结果。")


def parse_response(stdout):
    try:
        value = json.loads(stdout)
        body = value.get("result", value)
        payloads = body.get("payloads", [])
        meta = body.get("meta", {})
        agent = meta.get("agentMeta", {})
        if not isinstance(payloads, list) or not isinstance(agent, dict):
            raise ValueError()
        texts = []
        for p in payloads:
            if not isinstance(p, dict) or ("text" in p and not isinstance(p["text"], str)):
                raise ValueError()
            if p.get("text"):
                texts.append(p["text"])
        failed = bool(value.get("error") or body.get("error") or meta.get("aborted") or any(p.get("isError") for p in payloads))
        receipt = agent.get("terminalReceipt", {})
        tools = receipt.get("successfulToolNames", []) if isinstance(receipt, dict) else []
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            tools = []
        model = agent.get("model")
        if model is not None and not isinstance(model, str):
            raise ValueError()
        return {"texts": texts, "raw_usage": agent.get("usage"), "model": model,
                "tools": tools, "failed": failed or not texts}
    except (ValueError, TypeError, AttributeError):
        raise AppError("INVALID_RESPONSE", "模型未返回可识别的结果；未自动重试，用量可能未知。")


def chat(c, message, session=None, category="chat"):
    if not isinstance(message, str) or not message.strip() or len(message) > 32000:
        raise AppError("INPUT", "请输入 1–32000 字符的消息。")
    if category not in ("chat", "task", "verification", "proactive", "memory"):
        raise AppError("INPUT", "不支持的调用类别。")
    if session:
        try:
            uuid.UUID(session)
        except (ValueError, TypeError):
            raise AppError("SESSION", "会话标识必须为 UUID。")
    with (nullcontext(c["_owner_fd"]) if "_owner_fd" in c else exclusive(c["state"])) as lock_fd:
        sid = session or session_id(c["state"])
        key = credential(c)
        if c.get('_config_path'):
            from .balance import Balance
            Balance(c).admit()
        rid = c.get("_request_id") or str(uuid.uuid4())
        event = {"request_id": rid, "category": category, "session": sid}
        if c.get("_task_id"):
            event["task_id"] = c["_task_id"]
        if c.get('_candidate_id'):
            event['candidate_id'] = c['_candidate_id']
        append_event(c["state"], dict(event, status="started"))
        guard = None
        try:
            if c.get('_task_workspace'):
                from .task_guard import TaskGuard
                guard = TaskGuard(c,key)
            with (guard if guard else nullcontext(None)):
                local = dict(c, _runtime_config=str(guard.config_path)) if guard else c
                code, out = execute(local, ["agent", "--local", "--session-id", sid, "--message", message,
                                           "--thinking", "off", "--json"], guard.token if guard else key, lock_fd)
                if guard and guard.blocked:
                    raise AppError(**guard.blocked)
                result = parse_response(out)
            metrics = guard.limits.metrics(guard.jid,guard.attempt,guard.request_offset) if guard else None
            raw_usage = result.pop("raw_usage")
            usage = usage_record(metrics['usage'] if metrics else raw_usage, pricing(c), result["model"])
            response_failed = result.pop("failed")
            failed = code != 0 or response_failed
            append_event(c["state"], dict(event, status="failed" if failed else "succeeded", model=result["model"], **usage))
            answer = dict(result, **usage, request_id=rid, session=sid)
            if metrics: answer['task_guard'] = metrics
            if failed:
                answer["texts"] = []
                answer.update(AppError("RUNTIME", "本轮执行失败；已有用量已记录，请检查网络或模型配置后再试。").result())
            return answer
        except (AppError, OSError, UnicodeError) as exc:
            err = exc if isinstance(exc, AppError) else AppError("RUNTIME_IO", "运行或记录异常，结果可能未知；请核对状态，未自动重试。")
            if guard:
                metrics = guard.limits.metrics(guard.jid,guard.attempt,guard.request_offset)
                usage = usage_record(metrics['usage'],pricing(c),guard.model)
                append_event(c['state'],dict(event,status='failed' if metrics['usage'] is not None else 'outcome_unknown',error_code=err.code,model=guard.model,**usage))
                return dict(err.result(),request_id=rid,session=sid,task_guard=metrics,**usage)
            append_event(c["state"], dict(event, status="outcome_unknown", error_code=err.code,
                                          usage=None, usage_state="unknown", estimated_usd=None, price=None))
            return dict(err.result(), request_id=rid, session=sid)



def summary(c):
    requests, corrupt = {}, 0
    path = c["state"] / "usage-v1.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
                if e.get("schema_version") != 1 or not isinstance(e["request_id"], str):
                    raise ValueError()
                requests[e["request_id"]] = e
            except (ValueError, KeyError, TypeError, AttributeError):
                corrupt += 1
    groups = {}
    for e in requests.values():
        cat = e.get("category", "unknown")
        g = groups.setdefault(cat, {"requests": 0, "unknown_usage": 0, "unknown_cost": 0, "failed_or_unfinished": 0,
                                  "input": 0, "cacheRead": 0, "cacheWrite": 0, "output": 0, "total": 0, "known_estimated_usd": 0.0})
        g["requests"] += 1
        g["failed_or_unfinished"] += e.get("status") != "succeeded"
        u = validate_usage(e.get("usage"))
        if u is None:
            g["unknown_usage"] += 1
        else:
            for k, v in u.items():
                g[k] += v
        cost = e.get("estimated_usd")
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            g["known_estimated_usd"] += cost
        else:
            g["unknown_cost"] += 1
    return {"categories": groups, "unreadable_records": corrupt,
            "legacy_usage_preserved": (c["state"] / "usage.jsonl").exists(),
            "note": "仅汇总新入口记录；旧记录原位保留。started 未结束视为未知，勿盲目重试。费用是历史参考价估算，非官方账单。"}


def doctor(c):
    try:
        config = json.loads((c["state"] / "openclaw.json").read_text())
        package = json.loads((c["cli"].parent / "package.json").read_text())
        node = subprocess.run([str(c["node"]), "--version"], capture_output=True, text=True, timeout=10)
        defaults = config.get("agents", {}).get("defaults", {})
        from .worker import status
        return {"node": node.stdout.strip(), "openclaw": package.get("version"),
                "model": defaults.get("model", {}).get("primary"), "state": str(c["state"]),
                "workspace": defaults.get("workspace"), "session_exists": (c["state"] / "interactive-session.txt").exists(),
                "heartbeat": defaults.get("heartbeat", {}).get("every"), "cron_enabled": config.get("cron", {}).get("enabled"),
                "price_available": pricing(c) is not None, "keychain": "使用时读取，诊断不读取凭据", "background_worker": status(c)}
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        raise AppError("DIAGNOSTIC", "无法完成本地检查，请核对 OpenClaw 文件；未修改配置。")


def print_answer(r):
    if "error" in r:
        print("错误 [{}]：{}".format(r["error"]["code"], r["error"]["message"]))
        if r.get("job_id"):
            print("请求编号：" + r["job_id"] + "；可用 /task get 编号 查询。")
    else:
        for text in r["texts"]:
            print("\nAgent：" + text)
    if r.get("memory_notice"):
        print("记忆状态：" + r["memory_notice"])
    if r.get("memory_control") or r.get('proactive_control'):
        print("本次设置管理未调用模型。")
        return
    u = r.get("usage")
    if u is None:
        print("本轮用量未知，不能记为零费用。")
    else:
        print("用量：非缓存输入 {input}，缓存输入 {cacheRead}，缓存写入 {cacheWrite}，输出 {output}，合计 {total} tokens。".format(**u))
    cost = r.get("estimated_usd")
    print("历史参考价估算 ${:.6f}，以官方账单为准。".format(cost) if cost is not None else "本轮费用估算未知。")


def print_usage(c):
    s = summary(c)
    if not s["categories"]:
        print("新入口暂无调用记录。")
    for name, g in s["categories"].items():
        label = {"chat": "日常聊天", "task": "后台任务", "verification": "开发验证", "proactive":"主动交流"}.get(name, "其他调用")
        print("{}：{} 次请求，已知用量 {} tokens；未知用量 {} 次，未知费用 {} 次。".format(
            label, g["requests"], g["total"], g["unknown_usage"], g["unknown_cost"]))
        print("  非缓存输入 {input}，缓存输入 {cacheRead}，缓存写入 {cacheWrite}，输出 {output}；已知费用估算 ${known_estimated_usd:.6f}。".format(**g))
    if s["unreadable_records"]:
        print("有 {} 条记录无法读取，汇总可能不完整。".format(s["unreadable_records"]))
    print("按历史参考价估算，以官方账单为准；旧入口记录另行保留，未混入本表。")


def main(argv=None):
    from . import worker
    from .jobs import Store
    parser = argparse.ArgumentParser(description="海辛 · 本机入口")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command")
    for name in ("doctor", "usage", "new"):
        sub.add_parser(name)
    p = sub.add_parser("chat")
    p.add_argument("message")
    p.add_argument("--session")
    p.add_argument("--category", choices=("chat", "verification"), default="chat")
    p.add_argument("--json", action="store_true")
    w = sub.add_parser("worker")
    w.add_argument("action", choices=("start", "status", "stop", "run"))
    w.add_argument("--json", action="store_true")
    t = sub.add_parser("task")
    ts = t.add_subparsers(dest="action", required=True)
    for action in ("create", "get", "list", "cancel", "resume", "limits"):
        q = ts.add_parser(action)
        q.add_argument("--json", action="store_true")
        if action == "create":
            q.add_argument("goal")
            q.add_argument("--input", action="append", default=[])
            q.add_argument("--source", action="append", default=[])
            q.add_argument("--no-start", action="store_true")
            q.add_argument("--category", choices=("task", "verification"), default="task")
        elif action != "list":
            q.add_argument("id")
        if action == "resume":
            q.add_argument("--acknowledge-possible-effects", action="store_true")
        if action in ('create','limits'):
            q.add_argument('--max-model-requests',dest='model_requests',type=int)
            q.add_argument('--max-tool-calls',dest='tool_calls',type=int)
            q.add_argument('--max-output-tokens',dest='output_tokens',type=int)
            q.add_argument('--min-chars',type=int)
            q.add_argument('--require-section',dest='required_sections',action='append')
    m = sub.add_parser("memory")
    ms = m.add_subparsers(dest="action", required=True)
    for action in ("list", "search", "show", "settings", "correct", "forget", "enable", "disable", "allow", "rebuild", "replay-deletions"):
        q = ms.add_parser(action)
        q.add_argument("--json", action="store_true")
        if action in ("show", "correct", "forget", "allow"):
            q.add_argument("id")
        if action == "correct":
            q.add_argument("content")
        if action == "show":
            q.add_argument("--history", action="store_true")
        if action == "search":
            q.add_argument("query")
        if action in ("enable", "disable"):
            q.add_argument("kind", nargs="?", choices=("all", "preference", "goal", "topic"), default="all")
    source_parser = sub.add_parser("source")
    ss = source_parser.add_subparsers(dest="action", required=True)
    for action in ("file", "text", "url", "twitter", "x-search", "refresh", "list", "get", "inbox", "scan"):
        q = ss.add_parser(action); q.add_argument("--json", action="store_true")
        if action in ("file", "text", "url", "twitter", "x-search", "refresh", "get"):
            q.add_argument("value")
        if action == "text": q.add_argument("--title", default="文字片段")
        if action == "get":
            q.add_argument("--content", action="store_true")
            q.add_argument("--version", type=int)
    xp=sub.add_parser('twitter').add_subparsers(dest='action',required=True)
    for action in ('connect','status'):
        q=xp.add_parser(action);q.add_argument('--json',action='store_true')
        if action=='connect':
            q.add_argument('--browser',required=True,choices=('ego','chrome','edge','brave','arc'))
            q.add_argument('--profile',default='Default')
    wp = sub.add_parser('watch').add_subparsers(dest='action',required=True)
    for action in ('add','list','get','pause','resume','check','remove'):
        q=wp.add_parser(action);q.add_argument('--json',action='store_true')
        if action!='list':q.add_argument('id',help='已成功导入的材料编号')
        if action in ('add','resume'):q.add_argument('--every',type=int,help='检查间隔秒数，60–86400；新增默认900')
    pp = sub.add_parser('proactive').add_subparsers(dest='action', required=True)
    for action in ('status','enable','disable','set','pause','resume','candidates','followup','unmute','acknowledge-unknown'):
        q=pp.add_parser(action); q.add_argument('--json',action='store_true')
        if action=='set':
            q.add_argument('--daily-limit'); q.add_argument('--interval',type=int); q.add_argument('--quiet')
        if action=='pause': q.add_argument('--hours',type=float,default=24)
        if action in ('followup','unmute'): q.add_argument('id')
        if action=='followup': q.add_argument('--after',type=int,required=True,help='多少秒后回访')
    ip = sub.add_parser('inbox').add_subparsers(dest='action',required=True)
    for action in ('list','read','reply','feedback','task'):
        q=ip.add_parser(action); q.add_argument('--json',action='store_true')
        if action=='list': q.add_argument('--unread',action='store_true')
        else: q.add_argument('id')
        if action in ('reply','task'): q.add_argument('text')
        if action=='feedback': q.add_argument('feedback',choices=('interested','later','not-useful','stop-topic'))
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        # Backward-compatible administrator passthrough; no secrets or raw stderr displayed.
        if argv and argv[0] == "--raw":
            c = load_config()
            with exclusive(c["state"]) as fd:
                key = credential(c)
                code, out = execute(c, argv[1:], key, fd)
                print(out)
                if code:
                    print("管理员命令执行失败。", file=sys.stderr)
                return code
        a = parser.parse_args(argv)
        c = load_config(a.config)
        if a.command=='twitter':
            from . import twitter
            r=twitter.connect(c,a.browser,a.profile) if a.action=='connect' else twitter.status(c)
            print(json.dumps(r,ensure_ascii=False)) if a.json else print('推特连接已验证；尚未因此订阅任何账号或话题。')
            return 0
        if a.command=='watch':
            from .watch import Watch
            watcher=Watch(c)
            if a.action=='list':r=watcher.list()
            elif a.action=='get':r=watcher.get(a.id)
            elif a.action=='add':r=watcher.add(a.id,a.every if a.every is not None else 900)
            else:r=watcher.control(a.id,a.action,getattr(a,'every',None))
            if a.action in ('add','resume','check') and r['enabled']:worker.start(c)
            if a.json:print(json.dumps(r,ensure_ascii=False))
            else:print_watches(r)
            return 0
        if a.command=='proactive':
            from .proactive import Proactive
            engine=Proactive(c)
            if a.action=='status': r={'settings':engine.settings(),'note':'事件驱动；仅后台运行且电脑清醒时执行。无事件不调用模型；无每日限额不代表免费。'}
            elif a.action=='candidates': r=engine.candidates()
            elif a.action=='followup': r=engine.followup(a.id,a.after)
            elif a.action=='unmute': r=engine.unmute(a.id)
            else:
                command={'action':a.action}
                for field in ('hours','interval','quiet'):
                    if getattr(a,field,None) is not None: command[field]=getattr(a,field)
                if getattr(a,'daily_limit',None) is not None:
                    command['daily_limit']=None if a.daily_limit=='unlimited' else int(a.daily_limit)
                r=engine.control(command)
            if a.action in ('enable','followup','resume','acknowledge-unknown'): worker.start(c)
            if a.json: print(json.dumps(r,ensure_ascii=False))
            else: print_proactive(r)
            return 0
        if a.command=='inbox':
            from .proactive import Proactive
            engine=Proactive(c)
            if a.action=='list': r=engine.inbox(a.unread)
            elif a.action=='read': r=engine.read(a.id)
            elif a.action=='feedback': r=engine.feedback(a.id,a.feedback)
            elif a.action=='task':
                r=engine.task(a.id,a.text); worker.start(c)
            else:
                j=engine.reply(a.id,a.text); r=worker.wait_for_job(c,j,j['session'])
            if a.json: print(json.dumps(r,ensure_ascii=False))
            elif a.action=='reply': print_answer(r)
            elif a.action=='task': print_jobs(r)
            else: print_inbox(r)
            return int(isinstance(r,dict) and bool(r.get('error')))
        if a.command == "source":
            from .sources import Sources
            source_store = Sources(c)
            if a.action == "list": r = source_store.list()
            elif a.action == "get": r = source_store.get(a.value, a.content, a.version)
            elif a.action == "inbox": r = {"inbox": str(source_store.inbox())}
            elif a.action == "scan":
                r = [worker.send_source(c, command) for command in source_store.inbox_commands()]
            else:
                if a.action == "file": command = source_store.file_command(a.value)
                elif a.action == "text": command = source_store.text_command(a.value, a.title)
                elif a.action == "url": command = source_store.url_command(a.value)
                elif a.action in ('twitter','x-search'):command=source_store.twitter_command(a.value,a.action=='x-search')
                else: command = source_store.refresh_command(a.value)
                r = worker.send_source(c, command)
            if a.json: print(json.dumps(r, ensure_ascii=False))
            else: print_sources(r)
            return int(any(bool(x.get("error")) for x in (r if isinstance(r, list) else [r])))
        if a.command == "memory":
            from .memory import Memory
            mem = Memory(c)
            if a.action == "list":
                r = mem.list()
            elif a.action == "search":
                r = mem.search(a.query, limit=20, budget=3600)
            elif a.action == "show":
                r = mem.get(a.id, a.history)
            elif a.action == "settings":
                r = mem.settings()
            else:
                command = {"action": a.action}
                for name in ("id", "kind", "content"):
                    if hasattr(a, name): command[name] = getattr(a, name)
                r = worker.send_memory(c, command)
            if a.json:
                print(json.dumps(r, ensure_ascii=False))
            elif a.action in ("list", "search", "show"):
                print_memories(r)
            elif a.action == "settings":
                print("长期记忆开关：" + "，".join({"preference": "偏好", "goal": "目标", "topic": "未完话题"}[k] + ("开启" if v else "关闭") for k,v in r["enabled"].items()))
                for block in r["blocks"]:
                    if block["blocked"]: print("已忘记且禁止自动重提取：" + block["slot"])
            else:
                print_answer(r)
            return int(isinstance(r, dict) and "error" in r)
        if a.command == "worker":
            if a.action == "run":
                worker.serve(c)
                return 0
            r = getattr(worker, a.action)(c)
            print(json.dumps(r, ensure_ascii=False)) if a.json else print(r["message"])
            return 0
        if a.command == "task":
            store = Store(c)
            limits={k:getattr(a,k) for k in ('model_requests','tool_calls','output_tokens','min_chars','required_sections') if getattr(a,k,None) is not None}
            if a.action == "create":
                r = store.create(a.goal, inputs=a.input, category=a.category, sources=a.source,limits=limits)
            elif a.action=='limits':
                from .task_guard import Limits
                Limits(c).set(a.id,limits);r=store.get(a.id)
            elif a.action == "resume":
                r = store.resume(a.id, a.acknowledge_possible_effects)
            elif a.action == "list":
                r = store.list(kind="task")
            else:
                r = getattr(store, a.action)(a.id)
            if a.action == "resume" or (a.action == "create" and not a.no_start):
                try:
                    worker.start(c)
                except AppError as exc:
                    r["worker_notice"] = exc.message
            if a.json:
                print(json.dumps(r, ensure_ascii=False))
            else:
                print_jobs(r)
            return 0
        if a.command in ("doctor", "usage"):
            print(json.dumps(doctor(c) if a.command == "doctor" else summary(c), ensure_ascii=False, indent=2))
            return 0
        if a.command == "new":
            print(worker.new_session(c))
            return 0
        if a.command == "chat":
            r = worker.send_chat(c, a.message, a.session, a.category)
            print(json.dumps(r, ensure_ascii=False)) if a.json else print_answer(r)
            return int("error" in r)
        print("海辛。/exit 退出；/new 新会话；/usage 用量；/inbox list 消息；/help 帮助。")
        print("聊天、任务与主动交流共用后台；退出窗口后后台继续。没有事件时不调用模型。")
        from .proactive import Proactive
        engine=Proactive(c)
        from .watch import Watch
        if engine.settings()['enabled'] or any(w['enabled'] for w in Watch(c).list()): worker.start(c)
        notified=set()
        while True:
            message = inbox_input(engine, notified).strip()
            if message in ("/exit", "/quit"):
                return 0
            try:
                if message == "/new":
                    worker.new_session(c)
                    print("已开启新会话；原记录和工作区文件保留。")
                elif message == "/usage":
                    print_usage(c)
                elif message == "/status":
                    d = doctor(c)
                    print("当前模型：{}；OpenClaw {}。".format(d["model"], d["openclaw"]))
                    print("状态目录：" + d["state"])
                    print(d["background_worker"]["message"])
                elif message == "/help":
                    print('/twitter connect --browser ego：连接已授权的Ego会话；其他浏览器可明确指定；可加--profile "Profile 2"\n/twitter status：验证推特连接\n/source twitter @账号：读取最多20条公开动态\n/source x-search "话题"：读取最多20条话题结果；登录和接口可用性需实测')
                    print('/watch add 材料编号 --every 900：订阅此文件/网页的变化\n/watch list：检查状态\n/watch get 材料编号：检查历史\n/watch pause 材料编号：暂停读取\n/watch resume 材料编号：核对后恢复\n/watch check 材料编号：请求检查一次\n/watch remove 材料编号：停止订阅，保留来源和历史')
                    print('/task create "任务目标" --input "工作区相对路径.md"：提交文档任务\n/task list：查看任务\n/task get 编号：进展和成果\n/task cancel 编号：取消\n/worker start：启动后台\n/worker stop：停止后台\n/exit：仅退出聊天')
                    print('/memory list：记忆列表\n/memory show 编号 --history：来源与历史\n/memory correct 编号 "新内容"：更正\n/memory forget 编号：忘记\n/memory disable：关闭长期记忆\n/memory enable：开启长期记忆\n/memory rebuild：整理索引')
                    print('/source file "文件路径"：投递TXT/MD\n/source text "文字" --title "标题"：投递片段\n/source url "网址"：读取公开网页\n/source list：材料列表\n/source get 编号 --content：来源及正文\n/source refresh 编号：重新读取\n/source inbox：收件目录\n/source scan：导入收件目录第一层\n/task create "目标" --source 材料编号：引用材料生成文档')
                    print('/inbox list：主动消息\n/inbox read 编号：来源与上下文\n/inbox reply 编号 "回复"：接着聊\n/inbox task 编号 "目标"：接着做文档\n/inbox feedback 编号 later：稍后聊\n/proactive status：设置\n/proactive disable：关闭\n/proactive enable：开启\n/proactive pause --hours 24：暂停一天\n/proactive candidates：未发原因')
                elif message.startswith(("/task ", "/worker ", "/memory ", "/source ", "/inbox ", "/proactive ", "/watch ", "/twitter ")):
                    try:
                        main(["--config", c["_config_path"]] + shlex.split(message[1:]))
                    except SystemExit:
                        pass
                elif message:
                    print_answer(worker.send_chat(c, message))
            except AppError as exc:
                print(exc.message)
    except (EOFError, KeyboardInterrupt):
        print("\n已退出。")
        return 0
    except AppError as exc:
        print(json.dumps(exc.result(), ensure_ascii=False))
        return 1
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps(AppError("LOCAL_IO", "本地文件或运行环境异常；未自动重置数据。请检查配置与文件权限。").result(), ensure_ascii=False))
        return 1


def print_jobs(value):
    names = {"queued": "排队中", "running": "执行中", "succeeded": "已完成", "failed": "失败",
             "cancelled": "已取消", "outcome_unknown": "结果未知，需核对"}
    for j in value if isinstance(value, list) else [value]:
        print("{}  {}  已执行 {} 次".format(j["id"], names.get(j["status"], j["status"]), j["attempts"]))
        if j.get("cancel_requested") and j["status"] == "running":
            print("已请求取消，等待当前调用结束；已有操作无法撤销。")
        for s in j.get("steps", []):
            print("  {}：{}".format({"prepare": "准备材料", "agent": "Agent 执行", "verify": "成果核验"}.get(s["name"], s["name"]), names.get(s["status"], s["status"])))
        result = j.get("result") or {}
        if result.get("artifact"):
            print("成果：" + result["artifact"]["path"])
        for text in result.get("texts", []):
            print(text)
        if j.get("error"):
            print(j["error"])
        if j.get("worker_notice"):
            print(j["worker_notice"])
        if j.get('task_guard'):
            g=j['task_guard']; limits=g['limits']
            print('任务额度：模型请求 {}/{}，已放行工具 {}/{}，本次未知用量请求 {}。'.format(g['model_requests_used'],limits['model_requests'],g['tool_calls_admitted'],limits['tool_calls'],g['unknown_requests']))


def print_watches(value):
    rows=value if isinstance(value,list) else [value]
    if not rows:print('暂无来源订阅；先用source导入明确文件或URL，再用watch add订阅。')
    def stamp(value):return datetime.fromtimestamp(value).astimezone().strftime('%m-%d %H:%M:%S') if value is not None else '尚无'
    for row in rows:
        state='检查中' if row['active_run'] else '已开启' if row['enabled'] else '已暂停'
        print('{}  {}  每{}秒检查'.format(row['source_id'],state,row['interval_seconds']))
        print('来源：'+row['locator'])
        print('上次检查：{}；上次变化：{}；下次：{}'.format(stamp(row['last_checked']),stamp(row['last_changed']),stamp(row['next_due']) if row['enabled'] else '暂停'))
        if row['last_error']:print('最近原因：'+row['last_error'])
        labels={'checking':'检查中','changed':'发现新版本','unchanged':'正文未变化','failed':'读取失败','deferred':'已让用户请求先执行','cancelled':'已取消','outcome_unknown':'结果未知，已暂停'}
        for run in row.get('runs',[])[:5]:print('  {}  {}{}'.format(stamp(run['started']),labels.get(run['status'],run['status']),'：'+run['error_message'] if run['error_message'] else ''))
    print('检查本身不调用模型；相关新版本进入主动交流判断。暂停订阅只停止后续读取，已入库材料仍保留。')


def print_memories(value):
    if not value:
        print("暂无匹配的长期记忆。")
    for m in value if isinstance(value, list) else [value]:
        print("{}  {}  版本{}  {}".format(m["id"], m["slot"], m["version"], "有效" if m["status"] == "active" else "已忘记"))
        print(m["content"] or "内容已清理")
        if m.get("quote"):
            print("来源原句：" + m["quote"])
            print("来源请求：" + m["source_job"])
        for h in m.get("history", []):
            print("  历史版本{}：{}（{}）".format(h["version"], h["content"] or "内容已清理", h["status"]))


def print_sources(value):
    if not value: print("暂无材料。")
    for source in value if isinstance(value, list) else [value]:
        if "inbox" in source:
            print("请将TXT/MD放入此目录，再运行 /source scan：" + source["inbox"]); continue
        if source.get("error"): print("读取失败：" + source["error"]["message"])
        print("材料编号：{}；版本：{}".format(source.get("source_id", source.get("id", "未知")), source.get("version", source.get("current_version", "暂无"))))
        if source.get("title"): print(source["title"])
        if source.get("locator"): print("来源：" + source["locator"])
        if source.get("status"): print("状态：" + source["status"])
        if source.get("last_error"): print("最近读取错误：" + source["last_error"])
        if source.get("duplicate"): print("重复内容，未新增版本或文档任务。")
        if source.get("same_content_as"): print("与其他来源正文相同，已共用正文存储并保留独立来源。")
        for version in source.get("versions", []):
            print("  版本{}：{}；采集{}".format(version["version"], version["title"], version["acquired"]))
        if source.get("content"): print("\n" + source["content"])


def print_proactive(value):
    reasons={'UNRELATED':'与有效话题无明确关联','DISABLED':'主动交流已关闭','NOT_DUE':'尚未到时','QUIET':'勿扰时段',
             'PAUSED':'暂停期间','TOPIC_COOLDOWN':'该话题暂停中','AWAITING_REPLY':'已有未回应消息，不重复催问',
             'INTERVAL':'尚未到设定间隔','DAILY_LIMIT':'达到每日限额','UNKNOWN_USAGE':'有未知用量，请先核对','UNKNOWN_COST':'费用估算未知，请先核对价格配置',
             'EXPIRED':'已过期','MEMORY_CHANGED':'相关记忆已变化','MODEL_DECLINED':'模型判断暂不需要开口'}
    if isinstance(value,list):
        if not value: print('暂无候选事件。')
        for v in value: print(v['id']+'  '+v['status']+'  '+reasons.get(v.get('reason'),v.get('reason') or '已处理或等待评估'))
        return
    for text in value.get('texts',[]): print(text)
    if 'settings' in value:
        s=value['settings']; print('主动交流：'+('开启' if s['enabled'] else '关闭'))
        print('每天自动模型请求上限：'+('不设固定次数' if s['daily_limit'] is None else str(s['daily_limit'])))
        print('勿扰：'+(s['quiet_start']+'–'+s['quiet_end'] if s['quiet_start'] is not None else '未设定'))
        print('消息最小间隔：'+str(s['min_interval_seconds'])+'秒')
        if s['halt_reason']: print('自动调用已暂停：'+reasons.get(s['halt_reason'],s['halt_reason']))
    if 'candidate_id' in value: print('已保存回访候选：'+value['candidate_id'])
    if value.get('note'): print(value['note'])


def print_inbox(value):
    if not value: print('暂无消息。'); return
    if isinstance(value,dict) and value.get('proactive_control'): print_proactive(value); return
    for m in value if isinstance(value,list) else [value]:
        print('\n消息 '+m['id']+'（'+('已撤销' if m['status']!='active' else '已读' if m['read_at'] else '未读')+'）')
        print(m['body'] or '正文已清理。')
        source=m.get('context',{}).get('source')
        if source: print('来源：'+source['source_id']+'@'+str(source['version'])+' '+source['uri'])
        elif m.get('source_id'): print('来源：'+m['source_id']+'@'+str(m['source_version']))
        if m.get('topic_id'): print('关联话题：'+m['topic_id'])
        print('接着聊：/inbox reply '+m['id']+' "你的回复"')


def inbox_input(engine, notified):
    """Poll local inbox while the terminal waits for a line, without model calls."""
    def announce():
        changed=False
        for item in engine.inbox(unread=True):
            if item['id'] not in notified:
                print('\n新消息 '+item['id']+'：'+item['body'],flush=True)
                notified.add(item['id']); changed=True
        return changed
    announce(); print('\n你：',end='',flush=True)
    while True:
        ready,_,_=select.select([sys.stdin],[],[],1)
        if ready:
            line=sys.stdin.readline()
            if not line: raise EOFError()
            return line.rstrip('\n')
        if announce(): print('\n你：',end='',flush=True)
