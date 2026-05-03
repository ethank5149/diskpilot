"""DiskPilot v2 - Storage Analysis & Cleanup Tool (Dynamic Heuristics Edition)"""
from __future__ import annotations
import hashlib, logging, os, shutil, sqlite3, sys, threading, time, queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("diskpilot")

app = FastAPI(title="DiskPilot")

MOUNTS    = [m.strip() for m in os.environ.get(
    "MOUNTS", "/mnt/user,/mnt/cache,/mnt/disks,/mnt/dockercache"
).split(",") if m.strip()]
DB_PATH   = os.environ.get("DB_PATH",   "/data/diskpilot.db")
TRASH_DIR = os.environ.get("TRASH_DIR", "/data/trash")
MIN_FILE_SIZE = int(os.environ.get("MIN_FILE_SIZE", str(1 * 1024 * 1024)))

def _is_safe_path(p: str) -> bool:
    try: rp = os.path.realpath(p)
    except Exception: return False
    allowed = [os.path.realpath(m) for m in MOUNTS] + [os.path.realpath(TRASH_DIR)]
    return any(rp == a or rp.startswith(a.rstrip("/") + "/") for a in allowed)

def _safe_or_403(p: str):
    if not _is_safe_path(p): raise HTTPException(403, f"Path outside allowed mounts: {p}")

_lock = threading.Lock()
_scan: dict = {"status": "idle", "current": "", "dirs": 0, "files": 0,
               "bytes": 0, "skipped": 0, "aggregated": 0,
               "started": 0.0, "elapsed": 0.0, "abort": False}
_dups: dict = {"status": "idle", "stage": "", "current": "", "done": 0,
               "total": 0, "groups": 0, "wasted": 0, "abort": False}

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    path    TEXT PRIMARY KEY, name TEXT NOT NULL, parent TEXT,
    size    INTEGER NOT NULL DEFAULT 0, is_dir INTEGER NOT NULL DEFAULT 0,
    cnt     INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
    depth   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_parent ON nodes(parent);
CREATE INDEX IF NOT EXISTS idx_size   ON nodes(size DESC);
CREATE INDEX IF NOT EXISTS idx_mtime  ON nodes(mtime);
CREATE INDEX IF NOT EXISTS idx_name   ON nodes(name);

CREATE TABLE IF NOT EXISTS skipped (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, reason TEXT NOT NULL, ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dups (
    hash TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY (hash, path)
);
CREATE INDEX IF NOT EXISTS idx_dup_hash ON dups(hash);
"""

async def _db_init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    os.makedirs(TRASH_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

@app.on_event("startup")
async def startup(): await _db_init()

# ── Thread-Safe DB Writer ─────────────────────────────────────────────────────
def _db_writer_thread(q: queue.Queue):
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    node_buf, skip_buf = [], []

    def flush():
        if node_buf:
            c.executemany("INSERT OR REPLACE INTO nodes_new(path,name,parent,size,is_dir,cnt,mtime,depth) VALUES(?,?,?,?,?,?,?,?)", node_buf)
            node_buf.clear()
        if skip_buf:
            c.executemany("INSERT INTO skipped_new(path,reason,ts) VALUES(?,?,?)", skip_buf)
            skip_buf.clear()
        con.commit()

    while True:
        item = q.get()
        if item is None:
            flush(); q.task_done(); break
        msg_type, payload = item
        if msg_type == "node":
            node_buf.append(payload)
            if len(node_buf) >= 2000: flush()
        elif msg_type == "skip":
            skip_buf.append(payload)
            if len(skip_buf) >= 2000: flush()
        q.task_done()
    con.close()

# ── Single-Pass Dynamic Heuristic Scanner ─────────────────────────────────────
def _scan_single_mount(mount: str, q: queue.Queue):
    if _scan["abort"]: return
    if not os.path.isdir(mount):
        q.put(("skip", (mount, "Mount not found", time.time())))
        return

    # Deep directory structures need slightly higher recursion limits
    sys.setrecursionlimit(10000)
    
    local_stats = {"dirs": 0, "files": 0, "agg": 0, "bytes": 0}

    def walk(path: str) -> tuple[int, int]:
        """Returns (total_recursive_size, total_recursive_count)"""
        if _scan["abort"]: return 0, 0
        
        # NEW: Hyper-verbose real-time UI path reporting
        with _lock:
            _scan["current"] = path

        dir_size = 0
        dir_cnt = 0
        
        try:
            with os.scandir(path) as it: entries = list(it)
        except OSError as e:
            q.put(("skip", (path, "Permission denied or missing", time.time())))
            with _lock: _scan["skipped"] += 1
            return 0, 0

        # Separate files and directories
        files = [e for e in entries if e.is_file(follow_symlinks=False)]
        dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]

        # 1. Dynamically evaluate files in this directory
        for f in files:
            try:
                # Use OS cache via stat() - zero syscall overhead
                st = f.stat(follow_symlinks=False)
                sz = st.st_size
                dir_size += sz
                dir_cnt += 1
                
                # Dynamic Aggregation Heuristic
                if sz < MIN_FILE_SIZE:
                    local_stats["agg"] += 1
                else:
                    q.put(("node", (f.path, f.name, path, sz, 0, 1, st.st_mtime, path.count(os.sep) + 1)))
                    local_stats["files"] += 1
            except OSError:
                pass

        # 2. Recurse into subdirectories (Bottom-Up resolution)
        for d in dirs:
            c_size, c_cnt = walk(d.path)
            dir_size += c_size
            dir_cnt += c_cnt

        # 3. Emit rolled-up directory node
        try: mtime = os.stat(path, follow_symlinks=False).st_mtime
        except OSError: mtime = 0
        
        depth = path.count(os.sep)
        parent = os.path.dirname(path) if path != mount else None
        
        q.put(("node", (path, os.path.basename(path) or path, parent, dir_size, 1, dir_cnt, mtime, depth)))
        local_stats["dirs"] += 1
        
        # Flush UI numbers updates much more frequently
        if local_stats["dirs"] >= 10:
            with _lock:
                _scan["dirs"] += local_stats["dirs"]
                _scan["files"] += local_stats["files"]
                _scan["aggregated"] += local_stats["agg"]
            local_stats["dirs"] = local_stats["files"] = local_stats["agg"] = 0
            
        return dir_size, dir_cnt

    # Kick off the recursive single-pass walk
    root_size, _ = walk(mount)
    
    # Final cleanup flush
    with _lock:
        _scan["dirs"] += local_stats["dirs"]
        _scan["files"] += local_stats["files"]
        _scan["aggregated"] += local_stats["agg"]
        _scan["bytes"] += root_size

# ── Main Scan Orchestrator ────────────────────────────────────────────────────
def _do_scan():
    with _lock:
        _scan.update(status="scanning", dirs=0, files=0, bytes=0,
                     skipped=0, aggregated=0, started=time.time(), elapsed=0.0,
                     current="", abort=False)

    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.executescript("""
        DROP TABLE IF EXISTS nodes_new; DROP TABLE IF EXISTS skipped_new;
        CREATE TABLE nodes_new AS SELECT * FROM nodes WHERE 0;
        CREATE TABLE skipped_new AS SELECT * FROM skipped WHERE 0;
    """)
    con.commit(); con.close()

    q = queue.Queue()
    writer_thread = threading.Thread(target=_db_writer_thread, args=(q,))
    writer_thread.start()

    with ThreadPoolExecutor(max_workers=len(MOUNTS)) as executor:
        futures = [executor.submit(_scan_single_mount, mount, q) for mount in MOUNTS]
        for future in as_completed(futures): future.result()

    q.put(None); writer_thread.join()

    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    if _scan["abort"]:
        c.executescript("DROP TABLE nodes_new; DROP TABLE skipped_new;")
        con.commit(); con.close()
        with _lock: _scan.update(status="aborted", elapsed=round(time.time() - _scan["started"], 1))
        return

    c.executescript("""
        DROP TABLE IF EXISTS nodes_old; DROP TABLE IF EXISTS skipped_old;
        ALTER TABLE nodes RENAME TO nodes_old; ALTER TABLE skipped RENAME TO skipped_old;
        ALTER TABLE nodes_new RENAME TO nodes; ALTER TABLE skipped_new RENAME TO skipped;
        DROP TABLE nodes_old; DROP TABLE skipped_old;
        CREATE INDEX IF NOT EXISTS idx_parent ON nodes(parent);
        CREATE INDEX IF NOT EXISTS idx_size ON nodes(size DESC);
        CREATE INDEX IF NOT EXISTS idx_mtime ON nodes(mtime);
        CREATE INDEX IF NOT EXISTS idx_name ON nodes(name);
    """)
    con.commit(); con.close()

    with _lock: _scan.update(status="complete", elapsed=round(time.time() - _scan["started"], 1))
    log.info(f"Scan complete: {_scan['files']} files, {_scan['dirs']} dirs, {_scan['aggregated']} aggregated")

@app.post("/api/scan/start")
async def scan_start():
    if _scan["status"] == "scanning": return _scan
    threading.Thread(target=_do_scan, daemon=True).start()
    return {"status": "started"}

@app.post("/api/scan/abort")
async def scan_abort():
    with _lock: _scan["abort"] = True
    return {"status": "aborting"}

@app.get("/api/scan/status")
async def scan_status():
    with _lock: s = dict(_scan)
    if s["status"] == "scanning" and s["started"]: s["elapsed"] = round(time.time() - s["started"], 1)
    return s

# ── Tree & Summary Endpoints ──────────────────────────────────────────────────
TREE_SQL = """
WITH RECURSIVE tree(path,name,parent,size,is_dir,cnt,mtime,depth,lvl) AS (
    SELECT path,name,parent,size,is_dir,cnt,mtime,depth, 0 FROM nodes WHERE path = ?
  UNION ALL
    SELECT n.path,n.name,n.parent,n.size,n.is_dir,n.cnt,n.mtime,n.depth, t.lvl + 1
    FROM nodes n JOIN tree t ON n.parent = t.path WHERE t.lvl < ? AND n.is_dir = 1
)
SELECT * FROM tree
"""

def _build_subtree(rows, root_path, max_per_level):
    by_parent, nodes = {}, {}
    for r in rows:
        d = {"name": r["name"], "path": r["path"], "size": r["size"], "is_dir": bool(r["is_dir"]), "cnt": r["cnt"], "mtime": r["mtime"]}
        nodes[d["path"]] = d
        if r["parent"] is not None: by_parent.setdefault(r["parent"], []).append(d)

    if root_path not in nodes: return None
    root = nodes[root_path]

    def attach(node):
        kids = by_parent.get(node["path"], [])
        if not kids: return
        kids.sort(key=lambda x: -x["size"])
        shown, rest = kids[:max_per_level], kids[max_per_level:]
        for k in shown: attach(k)
        node["children"] = shown
        if rest:
            node["children"].append({
                "name": f"[+{len(rest)} more]", "path": node["path"] + "/__more__",
                "size": sum(k["size"] for k in rest), "is_dir": False, "cnt": sum(k["cnt"] for k in rest), "mtime": 0, "_more": True
            })
    attach(root)
    return root

@app.get("/api/tree")
async def get_tree(path: str = "__root__", depth: int = 3, max_per_level: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if path == "__root__":
            children, total, cnt = [], 0, 0
            for m in MOUNTS:
                async with db.execute(TREE_SQL, (m, depth)) as cur:
                    node = _build_subtree(await cur.fetchall(), m, max_per_level)
                if node:
                    children.append(node)
                    total += node["size"]; cnt += node["cnt"]
            return {"name": "root", "path": "__root__", "size": total, "is_dir": True, "cnt": cnt, "mtime": 0, "children": children}
        async with db.execute(TREE_SQL, (path, depth)) as cur: node = _build_subtree(await cur.fetchall(), path, max_per_level)
        if not node: raise HTTPException(404)
        return node

@app.get("/api/ls")
async def ls(path: str, sort: str = "size", offset: int = 0, limit: int = 200):
    order = {"size": "size DESC", "name": "name COLLATE NOCASE ASC", "mtime": "mtime DESC"}.get(sort, "size DESC")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT * FROM nodes WHERE parent=? ORDER BY {order} LIMIT ? OFFSET ?", (path, limit, offset)) as cur:
            rows = await cur.fetchall()
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM nodes WHERE parent=?", (path,)) as cur:
            t = await cur.fetchone()
    return {"total": t[0], "total_size": t[1], "items": [dict(r) for r in rows]}

@app.get("/api/search")
async def search(q: str, limit: int = 200, only: str = "all"):
    if not q or len(q) < 2: return {"items": [], "total": 0}
    where = "WHERE name LIKE ? COLLATE NOCASE" + (" AND is_dir = 0" if only == "files" else " AND is_dir = 1" if only == "dirs" else "")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT * FROM nodes {where} ORDER BY size DESC LIMIT ?", (f"%{q}%", limit)) as cur:
            rows = await cur.fetchall()
        async with db.execute(f"SELECT COUNT(*) FROM nodes {where}", (f"%{q}%",)) as cur: t = await cur.fetchone()
    return {"total": t[0], "items": [dict(r) for r in rows]}

@app.get("/api/summary")
async def summary():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) AS f, COALESCE(SUM(size),0) AS b FROM nodes WHERE is_dir=0") as c: tot = await c.fetchone()
        
        mount_rows = []
        for m in MOUNTS:
            async with db.execute("SELECT size, cnt FROM nodes WHERE path=?", (m,)) as c:
                if r := await c.fetchone(): mount_rows.append({"path": m, "size": r["size"], "cnt": r["cnt"]})

        async with db.execute("SELECT path, name, size, mtime FROM nodes WHERE is_dir=0 ORDER BY size DESC LIMIT 10") as c: biggest = [dict(r) for r in await c.fetchall()]
        async with db.execute("SELECT path, name, size, cnt FROM nodes WHERE is_dir=1 AND parent IS NOT NULL ORDER BY size DESC LIMIT 10") as c: biggest_dirs = [dict(r) for r in await c.fetchall()]
        
        cutoff = time.time() - 86400 * 365
        async with db.execute("SELECT path, name, size, mtime FROM nodes WHERE is_dir=0 AND size > 100000000 AND mtime > 0 AND mtime < ? ORDER BY mtime ASC LIMIT 10", (cutoff,)) as c: stale = [dict(r) for r in await c.fetchall()]

        buckets = {}
        async with db.execute("SELECT name, size FROM nodes WHERE is_dir=0 AND name LIKE '%.%'") as c:
            async for r in c:
                ext = r["name"].rsplit(".", 1)[-1].lower() if "." in r["name"] else ""
                if not ext or len(ext) > 6: continue
                b = buckets.setdefault(ext, {"cnt": 0, "bytes": 0})
                b["cnt"] += 1; b["bytes"] += r["size"] or 0
        top_exts = sorted([{"ext": k, **v} for k, v in buckets.items()], key=lambda x: -x["bytes"])[:12]

        try:
            async with db.execute("SELECT COUNT(DISTINCT hash) AS g, COALESCE(SUM(size),0) AS b, COUNT(*) AS f FROM dups") as c: d = await c.fetchone()
            async with db.execute("SELECT COUNT(*) AS c, MAX(size) AS s FROM dups GROUP BY hash") as c: wasted = sum(r["s"] * (r["c"] - 1) for r in await c.fetchall())
            dup_stats = {"groups": d["g"], "files": d["f"], "wasted": wasted}
        except Exception:
            dup_stats = {"groups": 0, "files": 0, "wasted": 0}

        trash_size, trash_cnt = 0, 0
        if os.path.isdir(TRASH_DIR):
            for r, _, fs in os.walk(TRASH_DIR):
                for f in fs:
                    try: trash_size += os.path.getsize(os.path.join(r, f)); trash_cnt += 1
                    except Exception: pass

    return {"total_files": tot["f"], "total_bytes": tot["b"], "mounts": mount_rows, "biggest_files": biggest, "biggest_dirs": biggest_dirs, "stale_large": stale, "extensions": top_exts, "dup_stats": dup_stats, "trash": {"size": trash_size, "count": trash_cnt}}

@app.get("/api/skipped")
async def get_skipped(limit: int = 1000):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM skipped ORDER BY ts DESC LIMIT ?", (limit,)) as cur: return [dict(r) for r in await cur.fetchall()]

# ── Actions (Trash/Delete) ────────────────────────────────────────────────────
class PathBody(BaseModel): path: str
class PathsBody(BaseModel): paths: list[str]

async def _remove_from_index(path: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM nodes WHERE path=? OR path LIKE ?", (path, path.rstrip("/") + "/%"))
        await db.execute("DELETE FROM dups WHERE path=? OR path LIKE ?", (path, path.rstrip("/") + "/%"))
        await db.commit()

def _move_safely(src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try: os.rename(src, dst)
    except OSError:
        if os.path.isdir(src): shutil.copytree(src, dst, symlinks=True); shutil.rmtree(src)
        else: shutil.copy2(src, dst); os.unlink(src)

@app.post("/api/trash/move")
async def trash_move(body: PathBody):
    _safe_or_403(body.path)
    if not os.path.exists(body.path): raise HTTPException(404)
    dst = os.path.join(TRASH_DIR, body.path.lstrip("/"))
    if os.path.exists(dst): dst = f"{dst}.{int(time.time())}"
    _move_safely(body.path, dst)
    await _remove_from_index(body.path)
    return {"moved_to": dst}

@app.post("/api/trash/move-many")
async def trash_move_many(body: PathsBody):
    results = []
    for p in body.paths:
        try:
            _safe_or_403(p)
            if not os.path.exists(p): continue
            dst = os.path.join(TRASH_DIR, p.lstrip("/"))
            if os.path.exists(dst): dst = f"{dst}.{int(time.time())}"
            _move_safely(p, dst)
            await _remove_from_index(p)
            results.append({"path": p, "ok": True})
        except Exception as e: results.append({"path": p, "ok": False, "error": str(e)})
    return {"results": results}

@app.post("/api/trash/restore")
async def trash_restore(body: PathBody):
    if not _is_safe_path(body.path) or not body.path.startswith(TRASH_DIR): raise HTTPException(403)
    orig = "/" + os.path.relpath(body.path, TRASH_DIR)
    _move_safely(body.path, orig)
    return {"restored_to": orig}

@app.post("/api/trash/empty")
async def trash_empty():
    shutil.rmtree(TRASH_DIR, ignore_errors=True); os.makedirs(TRASH_DIR, exist_ok=True)
    return {"status": "ok"}

@app.get("/api/trash/list")
async def trash_list():
    items = []
    if os.path.isdir(TRASH_DIR):
        for r, _, fs in os.walk(TRASH_DIR):
            for f in fs:
                fp = os.path.join(r, f)
                try: items.append({"path": fp, "original": "/" + os.path.relpath(fp, TRASH_DIR), "size": os.path.getsize(fp), "mtime": os.path.getmtime(fp)})
                except Exception: pass
    return sorted(items, key=lambda x: -x["size"])

@app.post("/api/delete")
async def delete(body: PathBody):
    _safe_or_403(body.path)
    if os.path.isdir(body.path): shutil.rmtree(body.path)
    else: os.unlink(body.path)
    await _remove_from_index(body.path)
    return {"status": "ok"}

@app.post("/api/delete-many")
async def delete_many(body: PathsBody):
    results = []
    for p in body.paths:
        try:
            _safe_or_403(p)
            if os.path.isdir(p): shutil.rmtree(p)
            else: os.unlink(p)
            await _remove_from_index(p)
            results.append({"path": p, "ok": True})
        except Exception as e: results.append({"path": p, "ok": False, "error": str(e)})
    return {"results": results}

# ── Multithreaded Duplicate Scanner ───────────────────────────────────────────
SAMPLE_BYTES = 65536
MIN_DUP_SIZE = 4096

def _sample_hash(path: str, size: int) -> str:
    h = hashlib.blake2b(digest_size=16)
    if size <= SAMPLE_BYTES * 3:
        with open(path, "rb") as f:
            while chunk := f.read(SAMPLE_BYTES): h.update(chunk)
    else:
        with open(path, "rb") as f:
            h.update(f.read(SAMPLE_BYTES))
            f.seek(size // 2); h.update(f.read(SAMPLE_BYTES))
            f.seek(-SAMPLE_BYTES, 2); h.update(f.read(SAMPLE_BYTES))
    h.update(size.to_bytes(8, "little"))
    return h.hexdigest()

def _full_hash(path: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20): h.update(chunk)
    return h.hexdigest()

def _do_dups():
    with _lock: _dups.update(status="scanning", stage="size-grouping", done=0, total=0, groups=0, wasted=0, current="", abort=False)

    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.execute("DELETE FROM dups"); con.commit()

    c.execute("SELECT path, size FROM nodes WHERE is_dir=0 AND size >= ?", (MIN_DUP_SIZE,))
    by_size = {}
    for path, size in c.fetchall(): by_size.setdefault(size, []).append(path)
    candidates = [(sz, ps) for sz, ps in by_size.items() if len(ps) > 1]

    _dups["stage"] = "sample-hashing"
    _dups["total"] = sum(len(p) for _, p in candidates)
    sample_groups = {}
    
    def hash_sample_task(p, s): return p, s, _sample_hash(p, s)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for size, paths in candidates:
            if _dups["abort"]: break
            for path in paths: futures.append(executor.submit(hash_sample_task, path, size))
        for future in as_completed(futures):
            if _dups["abort"]: break
            try:
                p, s, sh = future.result()
                sample_groups.setdefault(sh, []).append((p, s))
            except Exception: pass
            with _lock:
                _dups["done"] += 1; _dups["current"] = p

    if _dups["abort"]: con.close(); _dups["status"] = "aborted"; return

    sample_dupes = [(h, ps) for h, ps in sample_groups.items() if len(ps) > 1]

    _dups["stage"] = "verifying"
    _dups["total"] = sum(len(p) for _, p in sample_dupes)
    _dups["done"] = 0
    full_groups = {}

    def hash_full_task(p, s): return p, s, _full_hash(p)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for _, paths_with_size in sample_dupes:
            if _dups["abort"]: break
            for path, size in paths_with_size: futures.append(executor.submit(hash_full_task, path, size))
        for future in as_completed(futures):
            if _dups["abort"]: break
            try:
                p, s, fh = future.result()
                full_groups.setdefault(fh, []).append((p, s))
            except Exception: pass
            with _lock:
                _dups["done"] += 1; _dups["current"] = p

    if _dups["abort"]: con.close(); _dups["status"] = "aborted"; return

    final_groups = {h: ps for h, ps in full_groups.items() if len(ps) > 1}
    _dups["groups"] = len(final_groups)
    _dups["wasted"] = sum(ps[0][1] * (len(ps) - 1) for ps in final_groups.values())

    for h, ps in final_groups.items():
        c.executemany("INSERT INTO dups(hash,path,size) VALUES(?,?,?)", [(h, p, s) for p, s in ps])
    con.commit(); con.close()
    
    with _lock: _dups["status"] = "complete"

@app.post("/api/dups/start")
async def dups_start():
    if _dups["status"] == "scanning": return _dups
    threading.Thread(target=_do_dups, daemon=True).start()
    return {"status": "started"}

@app.post("/api/dups/abort")
async def dups_abort():
    with _lock: _dups["abort"] = True
    return {"status": "aborting"}

@app.get("/api/dups/status")
async def dups_status():
    with _lock: return dict(_dups)

@app.get("/api/dups/results")
async def dups_results(limit: int = 200):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT hash, size, GROUP_CONCAT(path,'||') AS paths, COUNT(*) AS cnt FROM dups GROUP BY hash ORDER BY size*(cnt-1) DESC LIMIT ?", (limit,)) as cur:
            rows = await cur.fetchall()
    return [{"hash": r["hash"], "size": r["size"], "paths": r["paths"].split("||"), "count": r["cnt"]} for r in rows]

@app.get("/api/mounts")
async def get_mounts(): return [{"path": m, "exists": os.path.isdir(m)} for m in MOUNTS]

app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")