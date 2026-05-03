"""DiskPilot - Storage Analysis & Cleanup Tool"""
from __future__ import annotations
import hashlib, logging, os, shutil, sqlite3, threading, time
from pathlib import Path
from typing import Optional

import aiosqlite
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("diskpilot")

app = FastAPI(title="DiskPilot")

MOUNTS    = [m.strip() for m in os.environ.get("MOUNTS", "/mnt/user,/mnt/cache,/mnt/disks,/mnt/dockercache").split(",") if m.strip()]
DB_PATH   = os.environ.get("DB_PATH", "/data/diskpilot.db")
TRASH_DIR = os.environ.get("TRASH_DIR", "/data/trash")

# ── shared mutable state (protected by _lock) ─────────────────────────────────
_lock = threading.Lock()
_scan: dict = {"status": "idle", "current": "", "dirs": 0, "files": 0,
               "bytes": 0, "skipped": 0, "started": 0.0, "elapsed": 0.0}
_dups: dict = {"status": "idle", "current": "", "done": 0, "total": 0, "groups": 0}

# ── DB ─────────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    path    TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    parent  TEXT,
    size    INTEGER NOT NULL DEFAULT 0,
    is_dir  INTEGER NOT NULL DEFAULT 0,
    cnt     INTEGER NOT NULL DEFAULT 0,
    mtime   REAL    NOT NULL DEFAULT 0,
    depth   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_parent ON nodes(parent);
CREATE INDEX IF NOT EXISTS idx_size   ON nodes(size DESC);
CREATE TABLE IF NOT EXISTS skipped (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    path    TEXT NOT NULL,
    reason  TEXT NOT NULL,
    ts      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dups (
    hash    TEXT NOT NULL,
    path    TEXT NOT NULL,
    size    INTEGER NOT NULL,
    PRIMARY KEY (hash, path)
);
"""

async def _db_init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    os.makedirs(TRASH_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

@app.on_event("startup")
async def startup():
    await _db_init()

# ── Scanner ────────────────────────────────────────────────────────────────────
def _do_scan():
    with _lock:
        _scan.update(status="scanning", dirs=0, files=0, bytes=0,
                     skipped=0, started=time.time(), elapsed=0.0, current="")

    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.executescript("DELETE FROM nodes; DELETE FROM skipped;")
    con.commit()

    node_buf:  list[tuple] = []
    skip_buf:  list[tuple] = []

    def flush():
        if node_buf:
            c.executemany(
                "INSERT OR REPLACE INTO nodes(path,name,parent,size,is_dir,cnt,mtime,depth) "
                "VALUES(?,?,?,?,?,?,?,?)", node_buf)
            node_buf.clear()
        if skip_buf:
            c.executemany("INSERT INTO skipped(path,reason,ts) VALUES(?,?,?)", skip_buf)
            skip_buf.clear()
        con.commit()

    dir_sizes: dict[str, int] = {}
    dir_cnts:  dict[str, int] = {}

    for mount in MOUNTS:
        if not os.path.isdir(mount):
            skip_buf.append((mount, "Mount not found", time.time()))
            flush()
            continue

        # topdown=False → children processed before parents → correct bottom-up sizes
        for dirpath, dirnames, filenames in os.walk(mount, topdown=False, followlinks=False):
            _scan["current"] = dirpath
            depth = dirpath.count(os.sep)

            # check subdir access
            blocked = []
            for d in list(dirnames):
                full = os.path.join(dirpath, d)
                if not os.access(full, os.R_OK | os.X_OK):
                    skip_buf.append((full, "Permission denied", time.time()))
                    _scan["skipped"] += 1
                    blocked.append(d)
            for d in blocked:
                dirnames.remove(d)

            dir_size = 0
            file_cnt = 0

            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    if os.path.islink(fpath):
                        continue
                    st = os.lstat(fpath)
                    node_buf.append((fpath, fname, dirpath, st.st_size, 0, 1, st.st_mtime, depth + 1))
                    dir_size += st.st_size
                    file_cnt += 1
                    _scan["files"] += 1
                    _scan["bytes"] += st.st_size
                except PermissionError:
                    skip_buf.append((fpath, "Permission denied", time.time()))
                    _scan["skipped"] += 1
                except Exception as e:
                    skip_buf.append((fpath, str(e), time.time()))

            # aggregate child dir sizes
            for d in dirnames:
                full = os.path.join(dirpath, d)
                dir_size += dir_sizes.get(full, 0)
                file_cnt += dir_cnts.get(full, 0)

            dir_sizes[dirpath] = dir_size
            dir_cnts[dirpath]  = file_cnt

            parent = str(Path(dirpath).parent) if dirpath != mount else None
            name   = os.path.basename(dirpath) or dirpath
            node_buf.append((dirpath, name, parent, dir_size, 1, file_cnt, time.time(), depth))
            _scan["dirs"] += 1

            if len(node_buf) >= 1000 or len(skip_buf) >= 200:
                flush()

    flush()
    con.close()

    with _lock:
        _scan.update(status="complete", elapsed=round(time.time() - _scan["started"], 1))
    log.info(f"Scan complete: {_scan['files']} files, {_scan['dirs']} dirs, {_scan['skipped']} skipped")

@app.post("/api/scan/start")
async def scan_start():
    if _scan["status"] == "scanning":
        return _scan
    threading.Thread(target=_do_scan, daemon=True).start()
    return {"status": "started"}

@app.get("/api/scan/status")
async def scan_status():
    s = dict(_scan)
    if s["status"] == "scanning" and s["started"]:
        s["elapsed"] = round(time.time() - s["started"], 1)
    return s

# ── Tree ───────────────────────────────────────────────────────────────────────
def _row_to_dict(row) -> dict:
    return {"name": row["name"], "path": row["path"], "size": row["size"],
            "is_dir": bool(row["is_dir"]), "cnt": row["cnt"]}

async def _fetch_node(db, path: str, depth: int, max_depth: int) -> Optional[dict]:
    async with db.execute("SELECT * FROM nodes WHERE path=?", (path,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    nd = _row_to_dict(row)
    if nd["is_dir"] and depth < max_depth:
        async with db.execute(
            "SELECT * FROM nodes WHERE parent=? ORDER BY size DESC LIMIT 60", (path,)
        ) as cur:
            children_rows = await cur.fetchall()
        kids = []
        for cr in children_rows:
            child = await _fetch_node(db, cr["path"], depth + 1, max_depth)
            if child:
                kids.append(child)
        nd["children"] = kids
    return nd

@app.get("/api/tree")
async def get_tree(path: str = "__root__", depth: int = 3):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if path == "__root__":
            children = []
            total = 0
            for m in MOUNTS:
                n = await _fetch_node(db, m, 0, depth)
                if n:
                    children.append(n)
                    total += n["size"]
            return {"name": "root", "path": "__root__", "size": total,
                    "is_dir": True, "cnt": sum(c["cnt"] for c in children), "children": children}
        node = await _fetch_node(db, path, 0, depth)
        if not node:
            raise HTTPException(404, "Path not found in index")
        return node

@app.get("/api/ls")
async def ls(path: str, sort: str = "size", offset: int = 0, limit: int = 200):
    order = {"size": "size DESC", "name": "name ASC", "mtime": "mtime DESC"}.get(sort, "size DESC")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM nodes WHERE parent=? ORDER BY {order} LIMIT ? OFFSET ?",
            (path, limit, offset)
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute("SELECT COUNT(*) FROM nodes WHERE parent=?", (path,)) as cur:
            total = (await cur.fetchone())[0]
    return {"total": total, "items": [dict(r) for r in rows]}

@app.get("/api/biggest")
async def biggest(root: str = "", limit: int = 200):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if root and root != "__root__":
            q = "SELECT * FROM nodes WHERE is_dir=0 AND (path=? OR path LIKE ?) ORDER BY size DESC LIMIT ?"
            args = (root, root.rstrip("/") + "/%", limit)
        else:
            q = "SELECT * FROM nodes WHERE is_dir=0 ORDER BY size DESC LIMIT ?"
            args = (limit,)
        async with db.execute(q, args) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]

# ── Skipped ────────────────────────────────────────────────────────────────────
@app.get("/api/skipped")
async def get_skipped(limit: int = 500):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM skipped ORDER BY ts DESC LIMIT ?", (limit,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

# ── Trash & Delete ─────────────────────────────────────────────────────────────
class PathBody(BaseModel):
    path: str

async def _remove_from_index(path: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM nodes WHERE path=? OR path LIKE ?",
            (path, path.rstrip("/") + "/%")
        )
        await db.commit()

@app.post("/api/trash/move")
async def trash_move(body: PathBody):
    src = body.path
    if not os.path.exists(src):
        raise HTTPException(404, f"Not found: {src}")
    rel = src.lstrip("/")
    dst = os.path.join(TRASH_DIR, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        shutil.move(src, dst)
    except Exception as e:
        raise HTTPException(500, str(e))
    await _remove_from_index(src)
    return {"moved_to": dst}

@app.post("/api/trash/restore")
async def trash_restore(body: PathBody):
    tp = body.path
    if not os.path.exists(tp):
        raise HTTPException(404)
    orig = "/" + os.path.relpath(tp, TRASH_DIR)
    os.makedirs(os.path.dirname(orig), exist_ok=True)
    shutil.move(tp, orig)
    return {"restored_to": orig}

@app.post("/api/trash/empty")
async def trash_empty():
    shutil.rmtree(TRASH_DIR, ignore_errors=True)
    os.makedirs(TRASH_DIR, exist_ok=True)
    return {"status": "ok"}

@app.get("/api/trash/list")
async def trash_list():
    items = []
    if os.path.isdir(TRASH_DIR):
        for root, _, files in os.walk(TRASH_DIR):
            for f in files:
                fp = os.path.join(root, f)
                orig = "/" + os.path.relpath(fp, TRASH_DIR)
                try:
                    items.append({"path": fp, "original": orig, "size": os.path.getsize(fp)})
                except Exception:
                    pass
    return sorted(items, key=lambda x: -x["size"])

@app.post("/api/delete")
async def delete(body: PathBody):
    p = body.path
    if not os.path.exists(p):
        raise HTTPException(404)
    try:
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
    except Exception as e:
        raise HTTPException(500, str(e))
    await _remove_from_index(p)
    return {"status": "ok"}

# ── Duplicates ─────────────────────────────────────────────────────────────────
def _do_dups():
    with _lock:
        _dups.update(status="hashing", done=0, total=0, groups=0, current="")

    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.execute("DELETE FROM dups")
    con.commit()

    c.execute("SELECT path, size FROM nodes WHERE is_dir=0 AND size > 4096")
    rows = c.fetchall()

    by_size: dict[int, list[str]] = {}
    for path, size in rows:
        by_size.setdefault(size, []).append(path)

    candidates = [(sz, ps) for sz, ps in by_size.items() if len(ps) > 1]
    _dups["total"] = sum(len(p) for _, p in candidates)

    hashes: dict[str, list[tuple[str, int]]] = {}
    done = 0

    for size, paths in candidates:
        for path in paths:
            _dups["current"] = path
            try:
                h = hashlib.blake2b(digest_size=16)
                with open(path, "rb") as f:
                    while chunk := f.read(65536):
                        h.update(chunk)
                dig = h.hexdigest()
                hashes.setdefault(dig, []).append((path, size))
            except Exception:
                pass
            done += 1
            _dups["done"] = done

    dup_groups = {h: ps for h, ps in hashes.items() if len(ps) > 1}
    _dups["groups"] = len(dup_groups)

    for h, ps in dup_groups.items():
        c.executemany("INSERT INTO dups(hash,path,size) VALUES(?,?,?)",
                      [(h, p, s) for p, s in ps])
    con.commit()
    con.close()
    _dups["status"] = "complete"

@app.post("/api/dups/start")
async def dups_start():
    if _dups["status"] == "hashing":
        return _dups
    threading.Thread(target=_do_dups, daemon=True).start()
    return {"status": "started"}

@app.get("/api/dups/status")
async def dups_status():
    return dict(_dups)

@app.get("/api/dups/results")
async def dups_results(limit: int = 200):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT hash, size, GROUP_CONCAT(path,'||') AS paths, COUNT(*) AS cnt
               FROM dups GROUP BY hash ORDER BY size*(cnt-1) DESC LIMIT ?""",
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [{"hash": r["hash"], "size": r["size"],
             "paths": r["paths"].split("||"), "count": r["cnt"]} for r in rows]

@app.get("/api/mounts")
async def get_mounts():
    return [{"path": m, "exists": os.path.isdir(m)} for m in MOUNTS]

# Serve React SPA — must be last
app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")
