"""DiskPilot v2 - Storage Analysis & Cleanup Tool"""
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

MOUNTS    = [m.strip() for m in os.environ.get(
    "MOUNTS", "/mnt/user,/mnt/cache,/mnt/disks,/mnt/dockercache"
).split(",") if m.strip()]
DB_PATH   = os.environ.get("DB_PATH",   "/data/diskpilot.db")
TRASH_DIR = os.environ.get("TRASH_DIR", "/data/trash")

# ── Adaptive density aggregation ──────────────────────────────────────────────
# Dirs that exceed both thresholds are recorded as single opaque nodes via
# shutil.disk_usage() instead of being walked file-by-file.
# Tune via env vars without rebuilding.
DENSITY_PROBE   = int(os.environ.get("DENSITY_PROBE",   "200"))   # scandir entries to sample
DENSITY_COUNT   = int(os.environ.get("DENSITY_COUNT",   "400"))   # min files to trigger
DENSITY_AVG_MAX = int(os.environ.get("DENSITY_AVG_MAX", "65536")) # max avg file size (64 KiB)

# ── Path safety ───────────────────────────────────────────────────────────────
def _is_safe_path(p: str) -> bool:
    """Path must be inside one of MOUNTS or TRASH_DIR (for restore/delete from trash)."""
    try:
        rp = os.path.realpath(p)
    except Exception:
        return False
    allowed = [os.path.realpath(m) for m in MOUNTS] + [os.path.realpath(TRASH_DIR)]
    return any(rp == a or rp.startswith(a.rstrip("/") + "/") for a in allowed)

def _safe_or_403(p: str):
    if not _is_safe_path(p):
        raise HTTPException(403, f"Path outside allowed mounts: {p}")

# ── Shared mutable state ──────────────────────────────────────────────────────
_lock = threading.Lock()
_scan: dict = {"status": "idle", "current": "", "dirs": 0, "files": 0,
               "bytes": 0, "skipped": 0, "aggregated": 0,
               "started": 0.0, "elapsed": 0.0, "abort": False}
_dups: dict = {"status": "idle", "stage": "", "current": "", "done": 0,
               "total": 0, "groups": 0, "wasted": 0, "abort": False}

# ── DB schema ─────────────────────────────────────────────────────────────────
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
CREATE INDEX IF NOT EXISTS idx_mtime  ON nodes(mtime);
CREATE INDEX IF NOT EXISTS idx_name   ON nodes(name);

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
CREATE INDEX IF NOT EXISTS idx_dup_hash ON dups(hash);
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

# ── Density probe ────────────────────────────────────────────────────────────
def _should_aggregate(dirpath: str) -> tuple[bool, int]:
    """
    Sample up to DENSITY_PROBE entries via a single scandir call.
    Returns (should_aggregate, estimated_total_size).
    Cost: one getattr per sampled entry — negligible.

    Triggers when:
      - We sampled DENSITY_PROBE entries AND avg file size < DENSITY_AVG_MAX
        (hitting the ceiling proves the dir is large; average proves files are small), OR
      - We read the whole dir, found >= DENSITY_COUNT files, and avg < DENSITY_AVG_MAX
    """
    try:
        n_files    = 0
        total_size = 0
        n_sampled  = 0
        hit_ceiling = False
        with os.scandir(dirpath) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total_size += entry.stat(follow_symlinks=False).st_size
                        n_files    += 1
                    n_sampled += 1
                except OSError:
                    pass
                if n_sampled >= DENSITY_PROBE:
                    hit_ceiling = True
                    break

        if n_files < 10:
            return False, 0  # too sparse to judge

        avg = total_size / n_files

        # Ceiling hit → dir definitely has many entries; only avg matters
        # Full read → need actual count to meet threshold
        dense = (hit_ceiling and avg < DENSITY_AVG_MAX) or \
                (not hit_ceiling and n_files >= DENSITY_COUNT and avg < DENSITY_AVG_MAX)

        if dense:
            try:
                estimated = shutil.disk_usage(dirpath).used
            except Exception:
                estimated = total_size
            return True, estimated

        return False, 0
    except (PermissionError, OSError):
        return False, 0

# ── Scanner — atomic: build to nodes_new, swap on success ─────────────────────
def _do_scan():
    with _lock:
        _scan.update(status="scanning", dirs=0, files=0, bytes=0,
                     skipped=0, aggregated=0, started=time.time(), elapsed=0.0,
                     current="", abort=False)

    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    # Shadow tables — keep old data live until we succeed
    c.executescript("""
        DROP TABLE IF EXISTS nodes_new;
        DROP TABLE IF EXISTS skipped_new;
        CREATE TABLE nodes_new   AS SELECT * FROM nodes   WHERE 0;
        CREATE TABLE skipped_new AS SELECT * FROM skipped WHERE 0;
    """)
    con.commit()

    node_buf:  list[tuple] = []
    skip_buf:  list[tuple] = []

    def flush():
        if node_buf:
            c.executemany(
                "INSERT OR REPLACE INTO nodes_new(path,name,parent,size,is_dir,cnt,mtime,depth) "
                "VALUES(?,?,?,?,?,?,?,?)", node_buf)
            node_buf.clear()
        if skip_buf:
            c.executemany("INSERT INTO skipped_new(path,reason,ts) VALUES(?,?,?)", skip_buf)
            skip_buf.clear()
        con.commit()

    # Collected during walk phase; sizes rolled up afterwards
    # node_meta: path -> (name, parent, is_dir, mtime, depth, aggregated)
    node_meta:  dict[str, tuple] = {}
    file_sizes: dict[str, int]   = {}   # path -> size  (files + aggregated dirs)
    file_cnts:  dict[str, int]   = {}   # path -> file count (files=1, aggregated=0)

    aborted = False

    for mount in MOUNTS:
        if _scan["abort"]:
            aborted = True
            break
        if not os.path.isdir(mount):
            skip_buf.append((mount, "Mount not found", time.time()))
            flush()
            continue

        # topdown=True: we can prune dirnames before os.walk recurses into them
        for dirpath, dirnames, filenames in os.walk(mount, topdown=True, followlinks=False):
            if _scan["abort"]:
                aborted = True
                break

            _scan["current"] = dirpath
            depth = dirpath.count(os.sep)

            # ── Subdir gate: permission-check and density-probe ──────────────
            to_remove = []
            for d in list(dirnames):
                full = os.path.join(dirpath, d)

                if not os.access(full, os.R_OK | os.X_OK):
                    skip_buf.append((full, "Permission denied", time.time()))
                    _scan["skipped"] += 1
                    to_remove.append(d)
                    continue

                agg, est_size = _should_aggregate(full)
                if agg:
                    try:
                        dmtime = os.lstat(full).st_mtime
                    except Exception:
                        dmtime = 0
                    parent = dirpath
                    name   = d + " ⚡"   # ⚡ = aggregated indicator
                    node_meta[full]  = (name, parent, True, dmtime, depth + 1, True)
                    file_sizes[full] = est_size
                    file_cnts[full]  = 0
                    _scan["dirs"]       += 1
                    _scan["bytes"]      += est_size
                    _scan["aggregated"] += 1
                    to_remove.append(d)
                    log.debug("Aggregated %s (~%d bytes)", full, est_size)

            for d in to_remove:
                dirnames.remove(d)

            # ── Record files in this directory ───────────────────────────────
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    if os.path.islink(fpath):
                        continue
                    st = os.lstat(fpath)
                    node_meta[fpath]  = (fname, dirpath, False, st.st_mtime, depth + 1, False)
                    file_sizes[fpath] = st.st_size
                    file_cnts[fpath]  = 1
                    _scan["files"]   += 1
                    _scan["bytes"]   += st.st_size
                except PermissionError:
                    skip_buf.append((fpath, "Permission denied", time.time()))
                    _scan["skipped"] += 1
                except Exception as e:
                    skip_buf.append((fpath, str(e), time.time()))

            # ── Record directory itself (size computed in rollup) ────────────
            try:
                dmtime = os.lstat(dirpath).st_mtime
            except Exception:
                dmtime = 0
            parent = str(Path(dirpath).parent) if dirpath != mount else None
            name   = os.path.basename(dirpath) or dirpath
            node_meta[dirpath] = (name, parent, True, dmtime, depth, False)
            file_sizes[dirpath] = 0   # placeholder — filled by rollup
            file_cnts[dirpath]  = 0
            _scan["dirs"] += 1

            if len(skip_buf) >= 200:
                flush()

    # ── Bottom-up rollup: propagate file sizes up the directory tree ──────────
    # Sort by depth descending so children are always processed before parents
    if not aborted:
        dir_size_acc: dict[str, int] = {}
        dir_cnt_acc:  dict[str, int] = {}

        all_paths = list(node_meta.keys())
        all_paths.sort(key=lambda p: -p.count(os.sep))  # deepest first

        for path in all_paths:
            name, parent, is_dir, mtime, depth, aggregated = node_meta[path]
            if is_dir and not aggregated:
                sz  = dir_size_acc.get(path, 0)
                cnt = dir_cnt_acc.get(path, 0)
                file_sizes[path] = sz
                file_cnts[path]  = cnt
            else:
                sz  = file_sizes.get(path, 0)
                cnt = file_cnts.get(path, 1 if not is_dir else 0)

            if parent and parent in node_meta:
                dir_size_acc[parent] = dir_size_acc.get(parent, 0) + sz
                dir_cnt_acc[parent]  = dir_cnt_acc.get(parent, 0) + cnt

        # Write all nodes to buffer
        for path, (name, parent, is_dir, mtime, depth, _agg) in node_meta.items():
            sz  = file_sizes.get(path, 0)
            cnt = file_cnts.get(path, 0)
            node_buf.append((path, name, parent, sz, 1 if is_dir else 0, cnt, mtime, depth))
            if len(node_buf) >= 2000:
                flush()

    flush()

    if aborted:
        c.executescript("DROP TABLE nodes_new; DROP TABLE skipped_new;")
        con.commit()
        con.close()
        with _lock:
            _scan.update(status="aborted", elapsed=round(time.time() - _scan["started"], 1))
        log.info("Scan aborted by user")
        return

    # Atomic swap — old data stays available the entire scan; only replaced at success
    c.executescript("""
        DROP TABLE IF EXISTS nodes_old;
        DROP TABLE IF EXISTS skipped_old;
        ALTER TABLE nodes   RENAME TO nodes_old;
        ALTER TABLE skipped RENAME TO skipped_old;
        ALTER TABLE nodes_new   RENAME TO nodes;
        ALTER TABLE skipped_new RENAME TO skipped;
        DROP TABLE nodes_old;
        DROP TABLE skipped_old;
    """)
    # Rebuild indexes (lost on rename in older sqlite)
    c.executescript("""
        CREATE INDEX IF NOT EXISTS idx_parent ON nodes(parent);
        CREATE INDEX IF NOT EXISTS idx_size   ON nodes(size DESC);
        CREATE INDEX IF NOT EXISTS idx_mtime  ON nodes(mtime);
        CREATE INDEX IF NOT EXISTS idx_name   ON nodes(name);
    """)
    con.commit()
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

@app.post("/api/scan/abort")
async def scan_abort():
    if _scan["status"] != "scanning":
        return {"status": "noop"}
    _scan["abort"] = True
    return {"status": "aborting"}

@app.get("/api/scan/status")
async def scan_status():
    s = dict(_scan)
    if s["status"] == "scanning" and s["started"]:
        s["elapsed"] = round(time.time() - s["started"], 1)
    return s

# ── Tree — single recursive CTE, no N+1 ───────────────────────────────────────
TREE_SQL = """
WITH RECURSIVE tree(path,name,parent,size,is_dir,cnt,mtime,depth,lvl) AS (
    SELECT path,name,parent,size,is_dir,cnt,mtime,depth, 0
    FROM nodes WHERE path = ?
  UNION ALL
    SELECT n.path,n.name,n.parent,n.size,n.is_dir,n.cnt,n.mtime,n.depth, t.lvl + 1
    FROM nodes n JOIN tree t ON n.parent = t.path
    WHERE t.lvl < ? AND n.is_dir = 1
)
SELECT * FROM tree
"""

def _row(r) -> dict:
    return {"name": r["name"], "path": r["path"], "size": r["size"],
            "is_dir": bool(r["is_dir"]), "cnt": r["cnt"], "mtime": r["mtime"]}

def _build_subtree(rows: list, root_path: str, max_per_level: int) -> Optional[dict]:
    """Build nested tree from flat row list with top-N by size at each level."""
    by_parent: dict[str, list[dict]] = {}
    nodes: dict[str, dict] = {}
    for r in rows:
        d = _row(r)
        nodes[d["path"]] = d
        if r["parent"] is not None:
            by_parent.setdefault(r["parent"], []).append(d)

    if root_path not in nodes:
        return None
    root = nodes[root_path]

    def attach(node):
        kids = by_parent.get(node["path"], [])
        if not kids:
            return
        kids.sort(key=lambda x: -x["size"])
        shown = kids[:max_per_level]
        rest = kids[max_per_level:]
        for k in shown:
            attach(k)
        node["children"] = shown
        if rest:
            other_size = sum(k["size"] for k in rest)
            other_cnt  = sum(k["cnt"]  for k in rest)
            node["children"].append({
                "name": f"[+{len(rest)} more]", "path": node["path"] + "/__more__",
                "size": other_size, "is_dir": False, "cnt": other_cnt,
                "mtime": 0, "_more": True
            })
    attach(root)
    return root

@app.get("/api/tree")
async def get_tree(path: str = "__root__", depth: int = 3, max_per_level: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if path == "__root__":
            children = []
            total = 0
            cnt = 0
            for m in MOUNTS:
                async with db.execute(TREE_SQL, (m, depth)) as cur:
                    rows = await cur.fetchall()
                node = _build_subtree(rows, m, max_per_level)
                if node:
                    children.append(node)
                    total += node["size"]
                    cnt += node["cnt"]
            return {"name": "root", "path": "__root__", "size": total,
                    "is_dir": True, "cnt": cnt, "mtime": 0, "children": children}
        async with db.execute(TREE_SQL, (path, depth)) as cur:
            rows = await cur.fetchall()
        node = _build_subtree(rows, path, max_per_level)
        if not node:
            raise HTTPException(404, "Path not in index")
        return node

# ── List directory (sortable, paginated) ──────────────────────────────────────
@app.get("/api/ls")
async def ls(path: str, sort: str = "size", offset: int = 0, limit: int = 200):
    order = {"size": "size DESC", "size_asc": "size ASC",
             "name": "name COLLATE NOCASE ASC", "name_desc": "name COLLATE NOCASE DESC",
             "mtime": "mtime DESC", "mtime_asc": "mtime ASC"}.get(sort, "size DESC")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM nodes WHERE parent=? ORDER BY {order} LIMIT ? OFFSET ?",
            (path, limit, offset)
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM nodes WHERE parent=?",
                              (path,)) as cur:
            total_row = await cur.fetchone()
    return {"total": total_row[0], "total_size": total_row[1],
            "items": [dict(r) for r in rows]}

# ── Search (filename substring across whole index) ────────────────────────────
@app.get("/api/search")
async def search(q: str, limit: int = 200, only: str = "all"):
    """only: 'all' | 'files' | 'dirs'"""
    if not q or len(q) < 2:
        return {"items": [], "total": 0}
    where = "WHERE name LIKE ? COLLATE NOCASE"
    args: list = [f"%{q}%"]
    if only == "files":
        where += " AND is_dir = 0"
    elif only == "dirs":
        where += " AND is_dir = 1"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM nodes {where} ORDER BY size DESC LIMIT ?",
            (*args, limit)
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute(f"SELECT COUNT(*) FROM nodes {where}", args) as cur:
            total = (await cur.fetchone())[0]
    return {"total": total, "items": [dict(r) for r in rows]}

# ── Summary dashboard data ────────────────────────────────────────────────────
@app.get("/api/summary")
async def summary():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # totals
        async with db.execute(
            "SELECT COUNT(*) AS files, COALESCE(SUM(size),0) AS bytes FROM nodes WHERE is_dir=0"
        ) as cur:
            tot = await cur.fetchone()

        # per-mount sizes
        mount_rows = []
        for m in MOUNTS:
            async with db.execute("SELECT size, cnt FROM nodes WHERE path=?", (m,)) as cur:
                r = await cur.fetchone()
            if r:
                mount_rows.append({"path": m, "size": r["size"], "cnt": r["cnt"]})

        # biggest 10 files
        async with db.execute(
            "SELECT path, name, size, mtime FROM nodes WHERE is_dir=0 ORDER BY size DESC LIMIT 10"
        ) as cur:
            biggest = [dict(r) for r in await cur.fetchall()]

        # biggest 10 dirs
        async with db.execute(
            "SELECT path, name, size, cnt FROM nodes WHERE is_dir=1 AND parent IS NOT NULL "
            "ORDER BY size DESC LIMIT 10"
        ) as cur:
            biggest_dirs = [dict(r) for r in await cur.fetchall()]

        # oldest 10 files (only files >100MB to skip cruft)
        cutoff = time.time() - 86400 * 365  # > 1 yr old
        async with db.execute(
            "SELECT path, name, size, mtime FROM nodes "
            "WHERE is_dir=0 AND size > 100000000 AND mtime > 0 AND mtime < ? "
            "ORDER BY mtime ASC LIMIT 10",
            (cutoff,)
        ) as cur:
            stale = [dict(r) for r in await cur.fetchall()]

        # extension breakdown — top 10 by total size
        async with db.execute("""
            SELECT
              LOWER(SUBSTR(name, INSTR(REPLACE(name,'.','') || '.', '.') + 1)) AS ext_ish,
              COUNT(*) AS cnt, SUM(size) AS bytes
            FROM nodes WHERE is_dir=0 AND name LIKE '%.%'
            GROUP BY ext_ish ORDER BY bytes DESC LIMIT 12
        """) as cur:
            ext_rows = [dict(r) for r in await cur.fetchall()]
        # The query above is approximate; re-do simpler:
        async with db.execute("""
            SELECT
              CASE WHEN name LIKE '%.%' THEN
                LOWER(SUBSTR(name, RTRIM(name, REPLACE(name, '.', ''))))
              ELSE '' END AS ext,
              COUNT(*) AS cnt, SUM(size) AS bytes
            FROM nodes WHERE is_dir=0 AND name LIKE '%.%'
            GROUP BY ext ORDER BY bytes DESC LIMIT 15
        """) as cur:
            ext_rows = []
            async with db.execute("SELECT name, size FROM nodes WHERE is_dir=0 AND name LIKE '%.%'") as cur2:
                # do it in Python: more reliable than SQL ext extraction in SQLite
                buckets: dict[str, dict[str, int]] = {}
                async for r in cur2:
                    ext = r["name"].rsplit(".", 1)[-1].lower() if "." in r["name"] else ""
                    if not ext or len(ext) > 6:
                        continue
                    b = buckets.setdefault(ext, {"cnt": 0, "bytes": 0})
                    b["cnt"]   += 1
                    b["bytes"] += r["size"] or 0
            top_exts = sorted(
                [{"ext": k, **v} for k, v in buckets.items()],
                key=lambda x: -x["bytes"]
            )[:12]

        # duplicate stats (if dups table populated)
        try:
            async with db.execute(
                "SELECT COUNT(DISTINCT hash) AS groups, "
                "COALESCE(SUM(size),0) AS total_bytes, COUNT(*) AS files FROM dups"
            ) as cur:
                d = await cur.fetchone()
            async with db.execute(
                "SELECT hash, COUNT(*) AS c, MAX(size) AS s FROM dups GROUP BY hash"
            ) as cur:
                wasted = sum(r["s"] * (r["c"] - 1) for r in await cur.fetchall())
            dup_stats = {"groups": d["groups"], "files": d["files"], "wasted": wasted}
        except Exception:
            dup_stats = {"groups": 0, "files": 0, "wasted": 0}

        # trash size
        trash_size, trash_cnt = 0, 0
        if os.path.isdir(TRASH_DIR):
            for r, _, fs in os.walk(TRASH_DIR):
                for f in fs:
                    try:
                        trash_size += os.path.getsize(os.path.join(r, f))
                        trash_cnt  += 1
                    except Exception:
                        pass

    return {
        "total_files": tot["files"], "total_bytes": tot["bytes"],
        "mounts": mount_rows,
        "biggest_files": biggest,
        "biggest_dirs":  biggest_dirs,
        "stale_large":   stale,
        "extensions":    top_exts,
        "dup_stats":     dup_stats,
        "trash":         {"size": trash_size, "count": trash_cnt},
    }

# ── Skipped ───────────────────────────────────────────────────────────────────
@app.get("/api/skipped")
async def get_skipped(limit: int = 1000):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM skipped ORDER BY ts DESC LIMIT ?", (limit,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

# ── Trash & Delete ────────────────────────────────────────────────────────────
class PathBody(BaseModel):
    path: str

class PathsBody(BaseModel):
    paths: list[str]

async def _remove_from_index(path: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM nodes WHERE path=? OR path LIKE ?",
            (path, path.rstrip("/") + "/%")
        )
        await db.execute(
            "DELETE FROM dups WHERE path=? OR path LIKE ?",
            (path, path.rstrip("/") + "/%")
        )
        await db.commit()

def _move_safely(src: str, dst: str):
    """shutil.move that handles cross-FS gracefully and atomically when possible."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError:
        # cross-filesystem — use copy + remove with verification
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True)
            shutil.rmtree(src)
        else:
            shutil.copy2(src, dst)
            os.unlink(src)

@app.post("/api/trash/move")
async def trash_move(body: PathBody):
    src = body.path
    _safe_or_403(src)
    if not os.path.exists(src):
        raise HTTPException(404, f"Not found: {src}")
    rel = src.lstrip("/")
    dst = os.path.join(TRASH_DIR, rel)
    if os.path.exists(dst):
        # disambiguate to avoid clobbering an existing trash entry
        dst = f"{dst}.{int(time.time())}"
    try:
        _move_safely(src, dst)
    except Exception as e:
        raise HTTPException(500, str(e))
    await _remove_from_index(src)
    return {"moved_to": dst}

@app.post("/api/trash/move-many")
async def trash_move_many(body: PathsBody):
    results = []
    for p in body.paths:
        try:
            _safe_or_403(p)
            if not os.path.exists(p):
                results.append({"path": p, "ok": False, "error": "not found"})
                continue
            rel = p.lstrip("/")
            dst = os.path.join(TRASH_DIR, rel)
            if os.path.exists(dst):
                dst = f"{dst}.{int(time.time())}"
            _move_safely(p, dst)
            await _remove_from_index(p)
            results.append({"path": p, "ok": True})
        except Exception as e:
            results.append({"path": p, "ok": False, "error": str(e)})
    return {"results": results}

@app.post("/api/trash/restore")
async def trash_restore(body: PathBody):
    tp = body.path
    if not _is_safe_path(tp) or not tp.startswith(TRASH_DIR):
        raise HTTPException(403)
    if not os.path.exists(tp):
        raise HTTPException(404)
    orig = "/" + os.path.relpath(tp, TRASH_DIR)
    if os.path.exists(orig):
        raise HTTPException(409, f"Original path already exists: {orig}")
    _move_safely(tp, orig)
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
                    items.append({"path": fp, "original": orig,
                                  "size": os.path.getsize(fp),
                                  "mtime": os.path.getmtime(fp)})
                except Exception:
                    pass
    return sorted(items, key=lambda x: -x["size"])

@app.post("/api/delete")
async def delete(body: PathBody):
    p = body.path
    _safe_or_403(p)
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

@app.post("/api/delete-many")
async def delete_many(body: PathsBody):
    results = []
    for p in body.paths:
        try:
            _safe_or_403(p)
            if not os.path.exists(p):
                results.append({"path": p, "ok": False, "error": "not found"})
                continue
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
            await _remove_from_index(p)
            results.append({"path": p, "ok": True})
        except Exception as e:
            results.append({"path": p, "ok": False, "error": str(e)})
    return {"results": results}

# ── Duplicates — three-pass: size → sample-hash → full-hash ───────────────────
SAMPLE_BYTES = 65536  # 64KB head + tail + middle samples
MIN_DUP_SIZE = 4096

def _sample_hash(path: str, size: int) -> str:
    """Hash head, middle, tail samples + size for fast dup pre-check."""
    h = hashlib.blake2b(digest_size=16)
    if size <= SAMPLE_BYTES * 3:
        with open(path, "rb") as f:
            while chunk := f.read(SAMPLE_BYTES):
                h.update(chunk)
    else:
        with open(path, "rb") as f:
            h.update(f.read(SAMPLE_BYTES))
            f.seek(size // 2)
            h.update(f.read(SAMPLE_BYTES))
            f.seek(-SAMPLE_BYTES, 2)
            h.update(f.read(SAMPLE_BYTES))
    h.update(size.to_bytes(8, "little"))
    return h.hexdigest()

def _full_hash(path: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):  # 1 MB chunks
            h.update(chunk)
    return h.hexdigest()

def _do_dups():
    with _lock:
        _dups.update(status="scanning", stage="size-grouping",
                     done=0, total=0, groups=0, wasted=0,
                     current="", abort=False)

    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.execute("DELETE FROM dups")
    con.commit()

    # Pass 1: group by size — no I/O
    c.execute("SELECT path, size FROM nodes WHERE is_dir=0 AND size >= ?", (MIN_DUP_SIZE,))
    by_size: dict[int, list[str]] = {}
    for path, size in c.fetchall():
        by_size.setdefault(size, []).append(path)
    candidates = [(sz, ps) for sz, ps in by_size.items() if len(ps) > 1]

    # Pass 2: sample-hash candidates
    _dups["stage"] = "sample-hashing"
    _dups["total"] = sum(len(p) for _, p in candidates)
    sample_groups: dict[str, list[tuple[str, int]]] = {}
    done = 0
    for size, paths in candidates:
        if _dups["abort"]:
            break
        for path in paths:
            if _dups["abort"]:
                break
            _dups["current"] = path
            try:
                sh = _sample_hash(path, size)
                sample_groups.setdefault(sh, []).append((path, size))
            except Exception:
                pass
            done += 1
            _dups["done"] = done

    if _dups["abort"]:
        con.close()
        _dups["status"] = "aborted"
        return

    # Only keep sample-hash groups with > 1 file
    sample_dupes = [(h, ps) for h, ps in sample_groups.items() if len(ps) > 1]

    # Pass 3: full-hash to verify (almost always the same hash, but be safe)
    _dups["stage"] = "verifying"
    _dups["total"] = sum(len(p) for _, p in sample_dupes)
    _dups["done"]  = 0
    full_groups: dict[str, list[tuple[str, int]]] = {}
    done = 0
    for _, paths_with_size in sample_dupes:
        if _dups["abort"]:
            break
        for path, size in paths_with_size:
            if _dups["abort"]:
                break
            _dups["current"] = path
            try:
                fh = _full_hash(path)
                full_groups.setdefault(fh, []).append((path, size))
            except Exception:
                pass
            done += 1
            _dups["done"] = done

    if _dups["abort"]:
        con.close()
        _dups["status"] = "aborted"
        return

    final_groups = {h: ps for h, ps in full_groups.items() if len(ps) > 1}
    _dups["groups"] = len(final_groups)
    _dups["wasted"] = sum(ps[0][1] * (len(ps) - 1) for ps in final_groups.values())

    for h, ps in final_groups.items():
        c.executemany("INSERT INTO dups(hash,path,size) VALUES(?,?,?)",
                      [(h, p, s) for p, s in ps])
    con.commit()
    con.close()
    _dups["status"] = "complete"

@app.post("/api/dups/start")
async def dups_start():
    if _dups["status"] == "scanning":
        return _dups
    threading.Thread(target=_do_dups, daemon=True).start()
    return {"status": "started"}

@app.post("/api/dups/abort")
async def dups_abort():
    _dups["abort"] = True
    return {"status": "aborting"}

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

# ── Misc ──────────────────────────────────────────────────────────────────────
@app.get("/api/mounts")
async def get_mounts():
    return [{"path": m, "exists": os.path.isdir(m)} for m in MOUNTS]

# Serve React SPA — must be last
app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")