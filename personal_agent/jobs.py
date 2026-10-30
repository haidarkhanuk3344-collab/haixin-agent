"""Persistent local jobs. Clients write transactions; one worker executes jobs."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .app import AppError, atomic_write, now

TERMINAL = {"succeeded", "failed", "cancelled", "outcome_unknown"}


def workspace(c):
    try:
        p = Path(json.loads((c["state"] / "openclaw.json").read_text())["agents"]["defaults"]["workspace"]).resolve()
        if not p.is_dir():
            raise ValueError()
        return p
    except (OSError, ValueError, KeyError, TypeError):
        raise AppError("WORKSPACE", "无法定位原工作区。")


def inside(root, path):
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise AppError("PATH", "文件必须位于授权工作区内。")
    return path


class Store:
    def __init__(self, c):
        self.c = c
        self.root = c["state"] / "personal-agent"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.path = self.root / "product.sqlite3"
        # Private directory protects SQLite journal files as well.
        self.root.chmod(0o700)
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise AppError("SCHEMA", "产品数据库版本不兼容，未重置数据。")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                  goal TEXT NOT NULL, session TEXT NOT NULL, category TEXT NOT NULL,
                  inputs TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  result TEXT, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS steps (
                  job_id TEXT NOT NULL, attempt INTEGER NOT NULL, name TEXT NOT NULL,
                  status TEXT NOT NULL, evidence TEXT, updated TEXT NOT NULL,
                  PRIMARY KEY(job_id,attempt,name));
                CREATE TABLE IF NOT EXISTS worker (
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1), token TEXT,
                  pid INTEGER, heartbeat REAL, stop_requested INTEGER NOT NULL DEFAULT 0);
                PRAGMA user_version=1;
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, goal, kind="task", inputs=(), session=None, category=None, sources=(), limits=None):
        if kind not in ("chat", "task", "memory", "source", "reply") or not isinstance(goal, str) or not goal.strip() or len(goal) > 24000:
            raise AppError("INPUT", "任务或消息需为 1–24000 字符。")
        if len(inputs) + len(sources) > 8:
            raise AppError("INPUT", "每次最多提供 8 份 TXT/MD 材料。")
        snapshots = []
        root = workspace(self.c)
        for name in inputs:
            p = Path(name).expanduser()
            p = inside(root, p if p.is_absolute() else root / p)
            if p.suffix.lower() not in (".txt", ".md") or not p.is_file():
                raise AppError("INPUT_FILE", "目前只支持工作区内已有的 TXT/MD 文件。")
            with p.open("rb") as f:
                raw = f.read(262145)
            if len(raw) > 262144:
                raise AppError("INPUT_SIZE", "单份材料不能超过 256 KiB。")
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                raise AppError("INPUT_ENCODING", "材料必须为 UTF-8 文本。")
            snapshots.append({"source": str(p.relative_to(root)) if not p.is_absolute() else str(p.resolve().relative_to(root)),
                              "content": text, "sha256": hashlib.sha256(raw).hexdigest()})
        if sources:
            from .sources import Sources
            source_store = Sources(self.c)
            snapshots.extend(source_store.reference(ref) for ref in sources)
        sid = session or str(uuid.uuid4())
        try:
            uuid.UUID(sid)
        except (ValueError, TypeError):
            raise AppError("SESSION", "会话标识必须为 UUID。")
        category = category or kind
        if category not in ("chat", "task", "verification", "memory", "source"):
            raise AppError("INPUT", "调用类别无效。")
        jid = str(uuid.uuid4())
        stamp = now()
        if kind=='task':
            from .task_guard import Limits
            task_limits=Limits(self.c).validate(limits or {})
        with self.db() as db:
            db.execute("INSERT INTO jobs(id,kind,status,goal,session,category,inputs,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                       (jid, kind, "queued", goal, sid, category, json.dumps(snapshots, ensure_ascii=False), stamp, stamp))
            if kind=='task': db.execute('INSERT INTO task_limits VALUES(?,?)',(jid,json.dumps(task_limits,ensure_ascii=False)))
        return self.get(jid)

    def get(self, jid, private=False):
        with self.db() as db:
            r = db.execute("SELECT rowid AS seq,* FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                raise AppError("NOT_FOUND", "找不到该任务。")
            job = dict(r)
            job["inputs"] = json.loads(job["inputs"])
            if not private:
                job["inputs"] = [{k: v for k, v in x.items() if k != "content"} for x in job["inputs"]]
            job["result"] = json.loads(job["result"]) if job["result"] else None
            job["steps"] = [dict(s) for s in db.execute("SELECT * FROM steps WHERE job_id=? ORDER BY attempt,rowid", (jid,))]
        if job['kind']=='task':
            from .task_guard import Limits
            job['task_guard']=Limits(self.c).metrics(jid,job['attempts'])
        return job

    def list(self, kind=None):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,kind,status,attempts,cancel_requested,created,updated,error FROM jobs WHERE (? IS NULL OR kind=?) ORDER BY rowid DESC LIMIT 50", (kind,kind))]

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY CASE WHEN kind='scheduled' THEN 1 ELSE 0 END,rowid LIMIT 1").fetchone()
            if not r:
                return None
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated=? WHERE id=?", (now(), r["id"]))
        return self.get(r["id"], private=True)

    def step(self, job, name, status, evidence=None):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO steps VALUES(?,?,?,?,?,?)",
                       (job["id"], job["attempts"], name, status,
                        json.dumps(evidence, ensure_ascii=False) if evidence is not None else None, now()))

    def finish(self, jid, status, result=None, error=None, memory_epoch=None):
        if status not in TERMINAL:
            raise ValueError("Invalid terminal status")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if memory_epoch is not None:
                current=db.execute("SELECT value FROM memory_meta WHERE key='epoch'").fetchone()
                if not current or int(current[0])!=memory_epoch:raise AppError('TOOL_CONTEXT_CHANGED','记忆刚刚变化，已停止发布旧上下文，请重新发送本次请求。')
            row = db.execute("SELECT status,cancel_requested FROM jobs WHERE id=?", (jid,)).fetchone()
            if row is None or row["status"] != "running":
                return
            if row["cancel_requested"]:
                status, error = "cancelled", "已取消；正在执行的调用可能已产生费用或文件，请检查结果。"
            db.execute("UPDATE jobs SET status=?,result=?,error=?,updated=? WHERE id=?",
                       (status, json.dumps(result, ensure_ascii=False) if result is not None else None, error, now(), jid))

    def cancel(self, jid):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                raise AppError("NOT_FOUND", "找不到该任务。")
            if r["status"] in ("queued","waiting_balance"):
                db.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,error=?,updated=? WHERE id=?",
                           ("排队任务已取消，未开始执行。", now(), jid))
            elif r["status"] == "running":
                db.execute("UPDATE jobs SET cancel_requested=1,updated=? WHERE id=?", (now(), jid))
        return self.get(jid)

    def resume(self, jid, acknowledge=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if row is None:
                raise AppError("NOT_FOUND", "找不到该任务。")
            if row["status"] not in ("failed", "outcome_unknown", "cancelled"):
                raise AppError("STATE", "该任务无需重试。")
            if not acknowledge:
                raise AppError("RETRY_ACK", "先核对已有文件和用量；确认可重复执行后使用 --acknowledge-possible-effects。")
            if row["attempts"] >= 2:
                raise AppError("RETRY_LIMIT", "该任务已达到最多两次执行限制。")
            db.execute("UPDATE jobs SET status='queued',cancel_requested=0,result=NULL,error=NULL,session=?,updated=? WHERE id=?",
                       (str(uuid.uuid4()), now(), jid))
        return self.get(jid)

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='outcome_unknown',error=?,updated=? WHERE status='running'",
                       ("上次后台在执行中退出；结果未知，未自动重试。请先核对已有文件和用量。", now()))
            db.execute("UPDATE steps SET status='outcome_unknown',updated=? WHERE status='running'", (now(),))


def prepare(c, job):
    root = workspace(c)
    directory = inside(root, root / "tasks" / job["id"] / ("attempt-" + str(job["attempts"])))
    # A fresh attempt cannot trust pre-existing directories placed by model tools.
    if directory.exists():
        raise AppError("ARTIFACT_EXISTS", "本次任务目录已存在，拒绝覆盖，请核对。")
    directory.mkdir(parents=True, mode=0o700)
    input_dir = directory / "inputs"
    input_dir.mkdir(mode=0o700)
    sources = []
    for i, item in enumerate(job["inputs"]):
        path = input_dir / (str(i + 1) + ".md")
        atomic_write(path, item["content"])
        sources.append({"path": str(path.relative_to(directory)), "source": item["source"], "sha256": item["sha256"]})
        sources[-1].update({k: item[k] for k in ('source_id','version','title','uri') if k in item})
    temp = directory / "result.pending.md"
    template = (Path(__file__).parent / "prompts/task.txt").read_text()
    message = template.format(goal=job["goal"], inputs=json.dumps(sources, ensure_ascii=False), output=temp.name)
    from .task_guard import Limits
    limits=Limits(c).get(job['id'])
    message += '\n程序验收约束：最少'+str(limits['min_chars'])+'个非空白字符；必需Markdown章节：'+json.dumps(limits['required_sections'],ensure_ascii=False)+'。当前只允许读取本任务目录、写入result.pending.md，输入不可改写。'
    return directory, message, sources


def verify_artifact(c, directory, job=None):
    root = workspace(c)
    inside(root, directory)
    pending = directory / "result.pending.md"
    # Reject symlinks in all generated path components, not only the leaf.
    for p in (directory, directory.parent, directory.parent.parent, pending):
        if p.is_symlink():
            raise AppError("ARTIFACT_PATH", "成果路径含符号链接，未发布。")
    if not pending.is_file():
        raise AppError("ARTIFACT_MISSING", "Agent 未生成约定成果文件，不能标记成功。")
    with pending.open("rb") as f:
        raw = f.read(1048577)
    if not raw or len(raw) > 1048576:
        raise AppError("ARTIFACT_SIZE", "成果为空或超过 1 MiB，未发布。")
    try:
        if not raw.decode("utf-8").strip():
            raise ValueError()
    except (UnicodeError, ValueError):
        raise AppError("ARTIFACT_CONTENT", "成果不是有效的非空 UTF-8 文本。")
    if job:
        import re
        from .task_guard import Limits
        limits=Limits(c).get(job['id']);text=raw.decode('utf-8')
        if len(re.sub(r'\s','',text))<limits['min_chars']:
            raise AppError('ARTIFACT_SHORT','文档未达到约定的最低字符数，保留临时成果供检查。')
        headings=re.findall(r'^#{1,6}\s+(.+)$',text,re.M)
        missing=[s for s in limits['required_sections'] if not any(s.casefold() in h.casefold() for h in headings)]
        if missing: raise AppError('ARTIFACT_SECTIONS','文档缺少约定章节：'+'、'.join(missing))
    target = directory / "result.md"
    if target.exists() or target.is_symlink():
        raise AppError("ARTIFACT_EXISTS", "正式成果已存在，拒绝覆盖。")
    os.replace(pending, target)
    return {"path": str(target), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "validation": "文件存在、路径、编码、非空与大小检查通过；不代表所有语义自动核验"}
