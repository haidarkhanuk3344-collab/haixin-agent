"""Source ingestion, immutable versions and explicit task snapshots."""
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata
import uuid

from .app import AppError, atomic_write, now
from .fetch import canonical_url, fetch_url, MAX_BYTES
from .jobs import Store, workspace

TEXT_LIMIT = 262144


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def normalize(text):
    text = unicodedata.normalize("NFC", text.replace('\r\n', '\n').replace('\r', '\n'))
    return re.sub(r"\n{3,}", "\n\n", '\n'.join(line.rstrip() for line in text.splitlines())).strip()


class ArticleParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "section", "article", "main", "tr", "blockquote", "pre"}
    VOID = {"br", "img", "hr", "meta", "link", "input", "area", "base", "embed", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.all, self.main, self.title = [], [], [], []

    def hidden(self):
        return any(t in self.SKIP or t == "head" for t in self.stack)

    def add(self, text):
        self.all.append(text)
        if any(t in ("main", "article") for t in self.stack): self.main.append(text)

    def handle_starttag(self, tag, attrs):
        if len(self.stack) >= 128:
            raise AppError("SOURCE_HTML", "网页标签嵌套过深，未继续解析。")
        if not self.hidden() and tag in self.BLOCK: self.add('\n')
        if tag not in self.VOID: self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.hidden() and tag in self.BLOCK: self.add('\n')
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        if "title" in self.stack: self.title.append(data)
        if not self.hidden(): self.add(re.sub(r"[\t \f\v]+", " ", data))

    def result(self):
        primary = normalize(''.join(self.main))
        return normalize(''.join(self.title))[:200], primary or normalize(''.join(self.all))


def parse(raw, media="text/plain", title=""):
    if not raw or len(raw) > MAX_BYTES:
        raise AppError("SOURCE_SIZE", "材料为空或原文超过1MiB。")
    if b'\x00' in raw or raw.startswith((b'%PDF', b'PK\x03\x04', b'\x89PNG', b'GIF8', b'\xff\xd8')):
        raise AppError("SOURCE_BINARY", "材料是二进制格式；当前仅支持TXT、Markdown和静态网页。")
    base_type = media.split(';', 1)[0].strip().lower()
    if base_type not in ("text/plain", "text/markdown", "text/x-markdown", "text/html", "application/xhtml+xml"):
        raise AppError("SOURCE_TYPE", "网页内容类型不支持，当前仅接受文本和HTML。")
    charset = re.search(r'charset\s*=\s*["\']?([\w-]+)', media, re.I)
    encoding = charset[1].lower() if charset else "utf-8-sig"
    if not charset and base_type in ("text/html", "application/xhtml+xml"):
        match = re.search(br'charset\s*=\s*["\']?([\w-]+)', raw[:4096], re.I)
        if match: encoding = match[1].decode('ascii').lower()
    if encoding not in ("utf-8", "utf-8-sig", "utf8", "gbk", "gb2312", "gb18030", "iso-8859-1", "windows-1252", "ascii"):
        raise AppError("SOURCE_ENCODING", "材料声明的编码暂不支持。")
    try:
        text = raw.decode(encoding)
    except UnicodeError:
        raise AppError("SOURCE_ENCODING", "文本无法按声明编码读取；本地TXT/MD请使用UTF-8。")
    if base_type in ("text/html", "application/xhtml+xml"):
        parser = ArticleParser(); parser.feed(text); parser.close()
        parsed_title, text = parser.result(); title = parsed_title or title
    else:
        text = normalize(text)
    if not text or len(text.encode()) > TEXT_LIMIT:
        raise AppError("SOURCE_TEXT_SIZE", "未提取到正文，或正文超过256KiB。")
    return {"title": title[:200] or text.splitlines()[0][:120], "content": text,
            "text_hash": digest(text.encode()), "raw_hash": digest(raw), "media": media}


class Sources:
    def __init__(self, c):
        self.c, self.store = c, Store(c)
        self.root = self.store.root / "sources"
        for d in (self.root, self.root / "raw", self.root / "text"):
            d.mkdir(mode=0o700, exist_ok=True)
        with self.store.db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS source_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              INSERT OR IGNORE INTO source_meta VALUES('schema_version','1');
              CREATE TABLE IF NOT EXISTS sources(
                id TEXT PRIMARY KEY,kind TEXT NOT NULL,locator TEXT NOT NULL,canonical TEXT NOT NULL UNIQUE,
                current_version INTEGER,status TEXT NOT NULL,last_error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS source_versions(
                source_id TEXT NOT NULL,version INTEGER NOT NULL,raw_hash TEXT NOT NULL,text_hash TEXT NOT NULL,
                title TEXT NOT NULL,final_url TEXT,media TEXT NOT NULL,bytes INTEGER NOT NULL,
                acquired TEXT NOT NULL,provenance TEXT NOT NULL,PRIMARY KEY(source_id,version));
              CREATE INDEX IF NOT EXISTS source_content_hash ON source_versions(text_hash);
              CREATE TABLE IF NOT EXISTS source_attempts(
                request_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,status TEXT NOT NULL,
                error_code TEXT,error_message TEXT,version INTEGER,duplicate INTEGER,created TEXT NOT NULL,finished TEXT,
                raw_hash TEXT,text_hash TEXT,final_url TEXT,provenance TEXT);
            """)
            if db.execute("SELECT value FROM source_meta WHERE key='schema_version'").fetchone()[0] != "1":
                raise AppError("SOURCE_SCHEMA", "材料库版本不兼容，未重置。")

    def write_blob(self, raw):
        h = digest(raw); path = self.root / "raw" / (h + '.bin')
        if path.is_symlink() or (path.exists() and (path.stat().st_size > MAX_BYTES or digest(path.read_bytes()) != h)):
            raise AppError("SOURCE_CORRUPT", "已保存原文校验失败，未覆盖旧文件。")
        if not path.exists():
            fd, tmp = tempfile.mkstemp(prefix='.source-', dir=str(path.parent))
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(raw); f.flush(); os.fsync(f.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
        return h

    def file_command(self, value, strict=False):
        supplied = Path(value).expanduser().absolute()
        if supplied.is_symlink() or supplied.suffix.lower() not in ('.txt', '.md'):
            raise AppError("SOURCE_FILE", "仅接受明确指定的普通TXT/MD文件，不跟随文件符号链接。")
        try:
            if strict:
                # Follow no component during unattended refresh, including a parent
                # directory replaced after the preflight path check.
                parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
                try:
                    for part in supplied.parts[1:-1]:
                        child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                        os.close(parent);parent=child
                    fd=os.open(supplied.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                finally:os.close(parent)
            else:fd = os.open(str(supplied), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise AppError("SOURCE_FILE", "材料必须是普通文件。")
                raw = f.read(MAX_BYTES + 1)
        except OSError:
            raise AppError("SOURCE_FILE", "材料文件不存在或无读取权限。")
        if not raw or len(raw) > MAX_BYTES:
            raise AppError("SOURCE_SIZE", "材料为空或超过1MiB。")
        location = str(supplied.resolve())
        return {"kind": "file", "locator": location, "canonical": "file:" + location,
                "blob": self.write_blob(raw), "media": "text/markdown" if supplied.suffix.lower() == '.md' else "text/plain",
                "title": supplied.name, "authorization": "用户明确投递该文件的内容快照"}

    def text_command(self, content, title="文字片段"):
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > TEXT_LIMIT:
            raise AppError("SOURCE_SIZE", "文字片段须为非空文本，最多256KiB。")
        raw = content.encode(); h = digest(normalize(content).encode())
        return {"kind": "text", "locator": "text:" + h, "canonical": "text:" + h,
                "blob": self.write_blob(raw), "media": "text/plain", "title": title[:200],
                "authorization": "用户明确投递文字片段"}

    def url_command(self, url):
        # Syntax/private literal rejection occurs before any DNS or network operation.
        normalized = canonical_url(url)
        return {"kind": "url", "locator": url, "canonical": "url:" + normalized,
                "url": normalized, "authorization": "用户明确授权读取该公开URL及受限公开重定向"}

    def refresh_command(self, identity, strict=False):
        source = self.get(identity)
        if source["kind"] == "url": return self.url_command(source["locator"])
        if source["kind"] == "file": return self.file_command(source["locator"],strict=strict)
        if source['kind'] in ('twitter-user','twitter-search'):
            return self.twitter_command(source['locator'],source['kind']=='twitter-search')
        raise AppError("SOURCE_REFRESH", "文字片段不可原地刷新；请投递新的文字片段。")

    def twitter_command(self,value,search=False):
        from .twitter import target
        return dict(target(value,search),authorization='用户明确指定的公开推特账号或话题，只读最多20条')

    def recover(self):
        with self.store.db() as db:
            db.execute("UPDATE source_attempts SET status='outcome_unknown',error_code='INTERRUPTED',error_message=?,finished=? WHERE status='fetching'",
                       ("上次材料读取中断，未自动重发；可查看已有版本后显式刷新。", now()))
            db.execute("UPDATE sources SET status='outcome_unknown',last_error=? WHERE status='fetching' AND current_version IS NULL",
                       ("上次读取中断，结果未知。",))

    def ingest(self, job, cancel_check=lambda: None, publish_guard=None):
        command = json.loads(job["goal"])
        kind = command.get("kind")
        if kind not in ("file", "text", "url", 'twitter-user', 'twitter-search'):
            raise AppError("SOURCE_INPUT", "材料投递类型无效。")
        if kind == "url":
            # Ensure queued records cannot introduce an unchecked scheme on replay.
            command["url"] = canonical_url(command["url"])
        rid = job["id"] + ':' + str(job["attempts"])
        stamp = now()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM sources WHERE canonical=?", (command["canonical"],)).fetchone()
            sid = existing["id"] if existing else str(uuid.uuid4())
            if not existing:
                db.execute("INSERT INTO sources VALUES(?,?,?,?,NULL,'fetching',NULL,?,?)",
                           (sid, kind, command["locator"], command["canonical"], stamp, stamp))
            attempt = db.execute("SELECT * FROM source_attempts WHERE request_id=?", (rid,)).fetchone()
            if attempt and attempt["status"] == 'ready':
                return {"source_id": sid, "version": attempt["version"], "duplicate": bool(attempt["duplicate"]), "source_control": True}
            db.execute("INSERT OR REPLACE INTO source_attempts(request_id,source_id,status,created) VALUES(?,?,'fetching',?)", (rid, sid, stamp))
        try:
            if cancel_check(): raise AppError("SOURCE_CANCELLED", "材料投递已取消。")
            provenance = {"authorization": command["authorization"], "job_id": job["id"]}
            if kind == "url":
                fetched = fetch_url(command["url"], cancel_check)
                raw = fetched["raw"]; media = fetched["headers"].get('content-type', '')
                final_url = fetched["url"]; title = ''
                provenance["redirects"] = fetched["hops"]
            elif kind in ('twitter-user','twitter-search'):
                from .twitter import fetch
                fetched=fetch(self.c,command,cancel_check)
                raw=fetched['raw'];media=fetched['media'];title=fetched['title'];final_url=fetched['url']
                provenance.update(backend='twitter-cli 0.8.5',visible_posts=fetched['count'],coverage='最多20条快照，不保证穷尽')
            else:
                h = command.get("blob", "")
                if not re.fullmatch(r'[0-9a-f]{64}', h): raise AppError("SOURCE_BLOB", "投递快照标识无效。")
                path = self.root / "raw" / (h + '.bin')
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                    raise AppError("SOURCE_BLOB", "投递快照丢失或超限。")
                raw = path.read_bytes()
                if digest(raw) != h: raise AppError("SOURCE_CORRUPT", "材料快照校验失败，拒绝使用。")
                media = command["media"]; title = command["title"]; final_url = None
            parsed = parse(raw, media, title)
            if cancel_check(): raise AppError("SOURCE_CANCELLED", "材料读取结束时收到取消请求，未发布新版本。")
            self.write_blob(raw)
            text_path = self.root / "text" / (parsed["text_hash"] + '.txt')
            if text_path.is_symlink() or (text_path.exists() and (text_path.stat().st_size > TEXT_LIMIT or digest(text_path.read_bytes()) != parsed["text_hash"])):
                raise AppError("SOURCE_CORRUPT", "已保存正文校验失败，未发布新版本。")
            if not text_path.exists(): atomic_write(text_path, parsed["content"])
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                if publish_guard: publish_guard(db)
                current = db.execute("SELECT current_version FROM sources WHERE id=?", (sid,)).fetchone()[0]
                old = db.execute("SELECT text_hash FROM source_versions WHERE source_id=? AND version=?", (sid, current)).fetchone()
                duplicate = bool(old and old[0] == parsed["text_hash"])
                version = current if duplicate else (current or 0) + 1
                same = db.execute("SELECT source_id,version FROM source_versions WHERE text_hash=? AND source_id<>? LIMIT 1", (parsed["text_hash"], sid)).fetchone()
                if not duplicate:
                    db.execute("INSERT INTO source_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (sid, version, parsed["raw_hash"], parsed["text_hash"], parsed["title"], final_url, media,
                                len(raw), now(), json.dumps(provenance, ensure_ascii=False)))
                db.execute("UPDATE sources SET current_version=?,status='ready',last_error=NULL,updated=? WHERE id=?", (version, now(), sid))
                db.execute("UPDATE source_attempts SET status='ready',version=?,duplicate=?,finished=?,raw_hash=?,text_hash=?,final_url=?,provenance=? WHERE request_id=?",
                           (version, int(duplicate), now(), parsed["raw_hash"], parsed["text_hash"], final_url, json.dumps(provenance, ensure_ascii=False), rid))
            return {"source_id": sid, "version": version, "duplicate": duplicate,
                    "same_content_as": dict(same) if same else None, "title": parsed["title"],
                    "source_control": True, "texts": ["材料已入库；重复内容未新增版本。" if duplicate else "材料已入库，未自动创建任务或写入个人记忆。"]}
        except AppError as exc:
            with self.store.db() as db:
                db.execute("UPDATE source_attempts SET status='failed',error_code=?,error_message=?,finished=? WHERE request_id=?", (exc.code, exc.message, now(), rid))
                db.execute("UPDATE sources SET status=CASE WHEN current_version IS NULL THEN 'failed' ELSE 'ready' END,last_error=?,updated=? WHERE id=?",
                           (exc.message, now(), sid))
            return dict(exc.result(), source_id=sid, source_control=True)

    def get(self, identity, content=False, version=None):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (identity,)).fetchone()
            if not row: raise AppError("SOURCE_NOT_FOUND", "找不到该材料编号。")
            source = dict(row)
            source["versions"] = [dict(v) for v in db.execute("SELECT * FROM source_versions WHERE source_id=? ORDER BY version", (identity,))]
            source["attempts"] = [dict(a) for a in db.execute("SELECT * FROM source_attempts WHERE source_id=? ORDER BY created", (identity,))]
        if content:
            snap = self.snapshot(identity, version)
            source["content"] = snap["content"]
            source["selected_version"] = snap["version"]
        return source

    def list(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT s.*,v.title FROM sources s LEFT JOIN source_versions v ON v.source_id=s.id AND v.version=s.current_version ORDER BY s.updated DESC LIMIT 100")]

    def snapshot(self, identity, version=None):
        with self.store.db() as db:
            s = db.execute("SELECT * FROM sources WHERE id=?", (identity,)).fetchone()
            if not s: raise AppError("SOURCE_NOT_FOUND", "找不到该材料编号。")
            selected = version if version is not None else s["current_version"]
            v = db.execute("SELECT * FROM source_versions WHERE source_id=? AND version=?", (identity, selected)).fetchone()
            if not v: raise AppError("SOURCE_NOT_READY", "该材料没有指定的成功版本。")
        path = self.root / "text" / (v["text_hash"] + '.txt')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > TEXT_LIMIT:
            raise AppError("SOURCE_CORRUPT", "材料正文丢失或超限。")
        raw = path.read_bytes()
        if digest(raw) != v["text_hash"]: raise AppError("SOURCE_CORRUPT", "材料正文校验失败。")
        return {"source": "source:{}@{} | {}".format(identity, selected, s["locator"]), "source_id": identity,
                "version": selected, "title": v["title"], "uri": v["final_url"] or s["locator"],
                "content": raw.decode('utf-8'), "sha256": v["text_hash"]}

    def reference(self, value):
        if '@' not in value: return self.snapshot(value)
        identity, version = value.rsplit('@', 1)
        if not version.isdigit() or int(version) < 1:
            raise AppError("SOURCE_VERSION", "材料引用格式应为编号或编号@正整数版本。")
        return self.snapshot(identity, int(version))

    def inbox(self):
        p = workspace(self.c) / "inbox"
        if p.is_symlink(): raise AppError("SOURCE_INBOX", "收件目录不能是符号链接。")
        p.mkdir(mode=0o700, exist_ok=True)
        return p

    def inbox_commands(self):
        paths = sorted(p for p in self.inbox().iterdir() if p.suffix.lower() in ('.txt', '.md') and not p.is_symlink() and p.is_file())
        if len(paths) > 50: raise AppError("SOURCE_INBOX_LIMIT", "一次扫描最多50份文件，请分批投递。")
        return [self.file_command(str(p)) for p in paths]
