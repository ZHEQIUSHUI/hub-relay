"""Tunnel (one-shot transfers, short codes) and store (drive-like) operations.

Tunnel layout in the tunnel repo:
    t/<code>/.start           {"created": ...}   written first, lets gc reap stuck uploads
    t/<code>/d/<relpath>      files <= chunk size, uploaded as-is
    t/<code>/d/<relpath>.pNNNNN   chunks of larger files
    t/<code>/manifest.json    written LAST: its presence means "complete"
"""
import hashlib
import json
import os
import random
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as C

T = "t"
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
IO_BUF = 8 << 20


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def human(n):
    n = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def new_code(n=6):
    return "".join(random.choice(CODE_ALPHABET) for _ in range(n))


def sha256_file(path, upto=None):
    h = hashlib.sha256()
    left = upto
    with open(path, "rb") as f:
        while True:
            buf = f.read(IO_BUF if left is None else min(IO_BUF, left))
            if not buf:
                break
            h.update(buf)
            if left is not None:
                left -= len(buf)
    return h.hexdigest()


def collect(paths):
    """[(abs_path, relpath)] — a file keeps its basename, a dir keeps its own name as prefix."""
    out = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            base = p.resolve()
            for f in sorted(base.rglob("*")):
                if f.is_file():
                    out.append((str(f), f"{base.name}/{f.relative_to(base).as_posix()}"))
        elif p.is_file():
            out.append((str(p.resolve()), p.name))
        else:
            raise FileNotFoundError(str(p))
    rels = [r for _, r in out]
    dup = {r for r in rels if rels.count(r) > 1}
    if dup:
        raise ValueError(f"duplicate names in transfer: {sorted(dup)}")
    return out


# ---------------------------------------------------------------- resume state

def _pending_path():
    return C.CACHE_DIR / "pending.json"


def _pending_load():
    try:
        return json.loads(_pending_path().read_text())
    except Exception:
        return {}


def _pending_save(d):
    _pending_path().parent.mkdir(parents=True, exist_ok=True)
    _pending_path().write_text(json.dumps(d, indent=1))


def _transfer_key(backend, repo, files, to):
    sig = [(a, os.path.getsize(a), int(os.path.getmtime(a))) for a, _ in files]
    return hashlib.sha1(json.dumps([backend, repo, to, sig]).encode()).hexdigest()


def _already(remote, dst, size, sha):
    e = remote.get(dst)
    if not e or e.size != size:
        return False
    return e.sha256 is None or e.sha256 == sha


# ---------------------------------------------------------------- tunnel

def push(be, repo, paths, cfg, to=None, ttl_hours=None, code=None, keep=False, note=None):
    files = collect(paths)
    if not files:
        raise ValueError("nothing to send")
    chunk = int(cfg.get("chunk_mb", 1024)) << 20
    ttl = float(ttl_hours if ttl_hours is not None else cfg.get("ttl_hours", 72))

    key = _transfer_key(be.name, repo, files, to)
    pending = _pending_load()
    resumed = key in pending
    code = code or pending.get(key) or new_code()

    prefix = f"{T}/{code}/"
    remote = {e.path: e for e in be.list(repo, prefix, recursive=True) if not e.is_dir}
    if remote and not resumed:
        raise ValueError(f"code {code} already in use")
    pending[key] = code
    _pending_save(pending)
    if remote:
        log(f"[relay] resuming {code}: {len(remote)} object(s) already uploaded")
    if f"{prefix}.start" not in remote:
        be.upload(repo, [(json.dumps({"created": time.time()}).encode(), f"{prefix}.start")], f"relay {code} start")

    total = sum(os.path.getsize(a) for a, _ in files)
    log(f"[relay] push {code} via {be.name}: {len(files)} file(s), {human(total)}")
    tmpdir = C.CACHE_DIR / "tmp" / code
    tmpdir.mkdir(parents=True, exist_ok=True)

    batch, batch_bytes, entries, t0 = [], 0, [], time.time()

    def flush():
        nonlocal batch, batch_bytes
        if batch:
            be.upload(repo, batch, f"relay {code}: {len(batch)} file(s)")
            batch, batch_bytes = [], 0

    for src, rel in files:
        size = os.path.getsize(src)
        dst = f"{prefix}d/{rel}"
        if size <= chunk:
            sha = sha256_file(src)
            entries.append({"path": rel, "size": size, "sha256": sha})
            if _already(remote, dst, size, sha):
                continue
            batch.append((src, dst))
            batch_bytes += size
            if batch_bytes >= chunk or len(batch) >= 200:
                flush()
            continue

        flush()
        whole, parts = hashlib.sha256(), []
        with open(src, "rb") as f:
            i = 0
            while True:
                part_path, ph, n = tmpdir / f"{i:05d}", hashlib.sha256(), 0
                with open(part_path, "wb") as out:
                    while n < chunk:
                        buf = f.read(min(IO_BUF, chunk - n))
                        if not buf:
                            break
                        out.write(buf)
                        ph.update(buf)
                        whole.update(buf)
                        n += len(buf)
                if n == 0:
                    part_path.unlink()
                    break
                pdst = f"{dst}.p{i:05d}"
                parts.append({"name": pdst, "size": n, "sha256": ph.hexdigest()})
                if not _already(remote, pdst, n, ph.hexdigest()):
                    log(f"[relay]   {rel} part {i} ({human(n)})")
                    be.upload(repo, [(str(part_path), pdst)], f"relay {code}: {rel} part {i}")
                part_path.unlink()
                i += 1
        entries.append({"path": rel, "size": size, "sha256": whole.hexdigest(), "parts": parts})
    flush()
    shutil.rmtree(tmpdir, ignore_errors=True)

    manifest = {
        "v": 1, "code": code, "from": cfg.get("name"), "to": to, "created": time.time(),
        "ttl_hours": ttl, "keep": keep, "note": note, "size": total, "files": entries,
    }
    be.upload(repo, [(json.dumps(manifest, indent=1).encode(), f"{prefix}manifest.json")], f"relay {code} done")
    pending.pop(key, None)
    _pending_save(pending)
    dt = time.time() - t0
    log(f"[relay] done in {dt:.0f}s ({human(total / max(dt, 1e-3))}/s)")
    return manifest


def get_manifest(be, repo, code):
    txt = be.read_text(repo, f"{T}/{code}/manifest.json")
    return json.loads(txt) if txt else None


def _fetch_into(be, repo, rpath, stage, target, sha, size):
    lp = be.download(repo, rpath, str(stage))
    if os.path.getsize(lp) != size or sha256_file(lp) != sha:
        raise IOError(f"checksum mismatch for {rpath}")
    os.replace(lp, target)


def pull(be, repo, code, out_dir, keep=False, force=False):
    m = get_manifest(be, repo, code)
    if m is None:
        started = be.list(repo, f"{T}/{code}/")
        raise LookupError(f"{code}: " + ("still uploading (no manifest yet)" if started else "not found"))
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage = out / f".relay-{code}"
    log(f"[relay] pull {code} via {be.name}: {len(m['files'])} file(s), {human(m['size'])} from {m.get('from')}")
    t0, got = time.time(), []
    for f in m["files"]:
        target = out / f["path"]
        got.append(str(target))
        if target.exists():
            if target.stat().st_size == f["size"] and sha256_file(target) == f["sha256"]:
                log(f"[relay]   {f['path']} already present, skip")
                continue
            if not force:
                raise FileExistsError(f"{target} exists and differs (use --force)")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not f.get("parts"):
            _fetch_into(be, repo, f"{T}/{code}/d/{f['path']}", stage, target, f["sha256"], f["size"])
            continue
        # chunked: append parts into <target>.relay-partial; resume from the last whole part
        partial = target.with_name(target.name + ".relay-partial")
        have = partial.stat().st_size if partial.exists() else 0
        whole, done = hashlib.sha256(), 0
        with open(partial, "ab+") as w:
            for i, p in enumerate(f["parts"]):
                if done + p["size"] <= have:
                    w.seek(done)
                    left = p["size"]
                    while left:
                        buf = w.read(min(IO_BUF, left))
                        whole.update(buf)
                        left -= len(buf)
                    done += p["size"]
                    continue
                w.truncate(done)
                w.seek(done)
                log(f"[relay]   {f['path']} part {i + 1}/{len(f['parts'])} ({human(p['size'])})")
                lp = be.download(repo, p["name"], str(stage))
                ph = hashlib.sha256()
                with open(lp, "rb") as r:
                    while True:
                        buf = r.read(IO_BUF)
                        if not buf:
                            break
                        ph.update(buf)
                        whole.update(buf)
                        w.write(buf)
                if ph.hexdigest() != p["sha256"]:
                    raise IOError(f"checksum mismatch for {p['name']}")
                os.remove(lp)
                done += p["size"]
                have = done
        if whole.hexdigest() != f["sha256"]:
            partial.unlink()
            raise IOError(f"checksum mismatch for {f['path']} (partial discarded, retry)")
        os.replace(partial, target)
    shutil.rmtree(stage, ignore_errors=True)
    dt = time.time() - t0
    log(f"[relay] got {human(m['size'])} in {dt:.0f}s ({human(m['size'] / max(dt, 1e-3))}/s) -> {out}")
    if not keep and not m.get("keep"):
        be.delete(repo, [f"{T}/{code}/"], f"relay {code} received")
        log(f"[relay] removed {code} from {be.name}")
    return m, got


def list_transfers(be, repo):
    codes = [Path(e.path).name for e in be.list(repo, T) if e.is_dir]

    def one(code):
        try:
            m = get_manifest(be, repo, code)
        except Exception as e:
            m = {"error": str(e)}
        if m is None:
            st = be.read_text(repo, f"{T}/{code}/.start")
            m = {"partial": True, "created": json.loads(st)["created"] if st else None}
        m["code"] = code
        return m

    with ThreadPoolExecutor(8) as ex:
        return sorted(ex.map(one, codes), key=lambda m: m.get("created") or 0)


def remove(be, repo, codes):
    be.delete(repo, [f"{T}/{c}/" for c in codes], f"relay rm {' '.join(codes)}")


def gc(be, repo, squash=True, partial_hours=48):
    now, dead = time.time(), []
    for m in list_transfers(be, repo):
        created = m.get("created") or 0
        limit = partial_hours if m.get("partial") else m.get("ttl_hours", 72)
        if not created or now - created > float(limit) * 3600:
            dead.append(m["code"])
    if dead:
        remove(be, repo, dead)
    squashed = False
    if squash:
        squashed = be.squash(repo)
    return dead, squashed


def watch(be, repo, name, out_dir, keep=False, once=False):
    """Poll for transfers addressed to `name`; adaptive interval 10s (busy) / 60s / 300s (idle >1h)."""
    last_head, last_activity = None, 0.0
    log(f"[relay] watching {be.name}:{repo} for transfers to '{name}' -> {out_dir}")
    while True:
        try:
            head = be.head(repo)
            if head != last_head:
                for m in list_transfers(be, repo):
                    if m.get("partial") or m.get("error") or m.get("to") != name:
                        continue
                    try:
                        pull(be, repo, m["code"], out_dir, keep=keep)
                        last_activity = time.time()
                    except Exception as e:
                        log(f"[relay] pull {m['code']} failed: {e}")
                last_head = be.head(repo)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"[relay] poll error: {e}")
        if once:
            return
        idle = time.time() - last_activity
        time.sleep(10 if idle < 300 else 60 if idle < 3600 else 300)


# ---------------------------------------------------------------- store

def store_put(be, repo, paths, remote_dir="", cfg=None):
    files = collect(paths)
    limit = int((cfg or {}).get("chunk_mb", 1024)) << 20
    remote_dir = remote_dir.strip("/")
    batch, size, dsts = [], 0, []
    for src, rel in files:
        dsts.append(f"{remote_dir}/{rel}" if remote_dir else rel)
        batch.append((src, dsts[-1]))
        size += os.path.getsize(src)
        if size >= limit or len(batch) >= 200:
            be.upload(repo, batch, f"store put {len(batch)} file(s)")
            batch, size = [], 0
    if batch:
        be.upload(repo, batch, f"store put {len(batch)} file(s)")
    return dsts


def store_get(be, repo, rpath, out_dir):
    rpath = rpath.strip("/")
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage = out / ".relay-store"
    entries = [e for e in be.list(repo, rpath, recursive=True) if not e.is_dir]
    if not entries:  # a single file (or nothing)
        entries = [None]
    parent = rpath.rsplit("/", 1)[0] + "/" if "/" in rpath else ""
    got = []
    for e in entries:
        p = rpath if e is None else e.path
        lp = be.download(repo, p, str(stage))
        target = out / p[len(parent):]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(lp, target)
        got.append(str(target))
    shutil.rmtree(stage, ignore_errors=True)
    return got


def store_rm(be, repo, rpaths):
    paths = []
    for r in rpaths:
        r = r.strip("/")
        is_dir = any(e.path != r for e in be.list(repo, r))
        paths.append(r + "/" if is_dir else r)
    be.delete(repo, paths, f"store rm {' '.join(rpaths)}")
