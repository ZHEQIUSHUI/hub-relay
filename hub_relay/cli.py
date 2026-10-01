import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config as C
from . import core
from .backends import get_backend, repos
from .core import human, log


def _probe(url, timeout=6):
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # got an HTTP answer -> reachable
    except Exception:
        return False


def _configured(cfg):
    return [b for b in ("hf", "ms") if cfg.get(b, {}).get("tunnel_repo")]


def _be(cfg, via):
    name = via or cfg.get("backend", "hf")
    tunnel, store = repos(cfg, name)
    return get_backend(cfg, name), tunnel, store


def _out(args, obj, text):
    if getattr(args, "json", False):
        print(json.dumps(obj, ensure_ascii=False, indent=1))
    elif text:
        print(text)


# ---------------------------------------------------------------- commands

def cmd_init(args, cfg):
    if args.name:
        cfg["name"] = args.name
    hf = cfg.setdefault("hf", {})
    ep = args.hf_endpoint
    if ep == "auto" or (ep is None and "endpoint" not in hf):
        ep = C.HF_OFFICIAL if _probe(C.HF_OFFICIAL + "/api/whoami-v2") else C.HF_MIRROR
        log(f"[init] huggingface.co {'reachable' if ep == C.HF_OFFICIAL else 'unreachable -> using hf-mirror'}")
    if ep:
        hf["endpoint"] = ep
    if args.hf_token_file:
        hf["token_file"] = str(Path(args.hf_token_file).expanduser())
    ms = cfg.setdefault("ms", {})
    if args.ms_token_file:
        ms["token_file"] = str(Path(args.ms_token_file).expanduser())
    elif "token_file" not in ms and (C.CONFIG_PATH.parent / "ms_token").exists():
        ms["token_file"] = str(C.CONFIG_PATH.parent / "ms_token")
    if args.backend:
        cfg["backend"] = args.backend
    if args.inbox:
        cfg["inbox"] = args.inbox

    ok = []
    for name, sec in (("hf", hf), ("ms", ms)):
        if name == "ms" and not (C.read_token(ms) or os.environ.get("MODELSCOPE_API_TOKEN")):
            log("[init] ms: no token (put one in ~/.config/relay/ms_token to enable) — skipped")
            continue
        try:
            be = get_backend(cfg, name)
            user = args.user or be.whoami()
            sec.setdefault("tunnel_repo", f"{user}/relay-tunnel")
            sec.setdefault("store_repo", f"{user}/relay-store")
            if not args.no_create:
                be.ensure_repo(sec["tunnel_repo"])
                be.ensure_repo(sec["store_repo"])
            ok.append(name)
            log(f"[init] {name}: user={user} tunnel={sec['tunnel_repo']} store={sec['store_repo']}")
        except Exception as e:
            log(f"[init] {name}: FAILED {type(e).__name__}: {str(e)[:300]}")
    if ok and cfg.get("backend") not in ok:
        cfg["backend"] = ok[0]
    C.save(cfg)
    log(f"[init] wrote {C.CONFIG_PATH} (name={cfg['name']}, default backend={cfg.get('backend')})")
    return 0 if ok else 1


def cmd_push(args, cfg):
    be, tunnel, _ = _be(cfg, args.via)
    m = core.push(be, tunnel, args.paths, cfg, to=args.to, ttl_hours=args.ttl,
                  code=args.code, keep=args.keep, note=args.note)
    code = m["code"]
    hint = f"relay pull {code}" + (f" --via {be.name}" if be.name != cfg.get("backend") else "")
    if not args.json:
        log(f"[relay] receive with:  {hint}" + (f"   (or `relay watch` on '{args.to}')" if args.to else ""))
        if args.curl:
            for f in m["files"]:
                names = [p["name"] for p in f.get("parts", [])] or [f"{core.T}/{code}/d/{f['path']}"]
                for n in names:
                    log("  " + be.curl_hint(tunnel, n) + f" -o '{Path(n).name}'")
                if f.get("parts"):
                    log(f"  cat '{Path(f['path']).name}'.p* > '{Path(f['path']).name}'   # then sha256 = {f['sha256']}")
    _out(args, m, code)


def cmd_pull(args, cfg):
    order = [args.via] if args.via else [cfg.get("backend", "hf")] + [b for b in _configured(cfg) if b != cfg.get("backend")]
    last = None
    for name in order:
        try:
            be, tunnel, _ = _be(cfg, name)
            if len(order) > 1 and core.get_manifest(be, tunnel, args.code) is None:
                last = LookupError(f"{args.code}: not on {name}")
                continue
            m, got = core.pull(be, tunnel, args.code, args.out or ".", keep=args.keep, force=args.force)
            _out(args, {"manifest": m, "files": got}, "\n".join(got))
            return 0
        except LookupError as e:
            last = e
        except C.ConfigError as e:
            last = e
    raise last


def _age(ts):
    if not ts:
        return "?"
    s = time.time() - ts
    return f"{s / 60:.0f}m" if s < 3600 else f"{s / 3600:.1f}h" if s < 86400 else f"{s / 86400:.1f}d"


def cmd_ls(args, cfg):
    names = [args.via] if args.via else _configured(cfg)
    rows, objs = [], []
    for name in names:
        be, tunnel, _ = _be(cfg, name)
        for m in core.list_transfers(be, tunnel):
            m["backend"] = name
            objs.append(m)
            if m.get("partial"):
                rows.append(f"{m['code']:<8} {name:<3} {'(uploading / incomplete)':<40} {_age(m.get('created')):>6}")
                continue
            files = m.get("files", [])
            first = files[0]["path"] if files else ""
            what = first + (f" +{len(files) - 1}" if len(files) > 1 else "")
            route = f"{m.get('from')}->{m.get('to') or '*'}"
            rows.append(f"{m['code']:<8} {name:<3} {route:<18} {human(m.get('size', 0)):>7}  {what[:40]:<40} {_age(m.get('created')):>6}")
    _out(args, objs, "\n".join(rows) if rows else "(tunnel empty)")


def cmd_rm(args, cfg):
    be, tunnel, _ = _be(cfg, args.via)
    core.remove(be, tunnel, args.codes)
    log(f"[relay] removed {' '.join(args.codes)}")


def cmd_gc(args, cfg):
    for name in ([args.via] if args.via else _configured(cfg)):
        be, tunnel, _ = _be(cfg, name)
        dead, squashed = core.gc(be, tunnel, squash=not args.no_squash)
        log(f"[gc] {name}: removed {len(dead)} transfer(s) {' '.join(dead)}; history squash: "
            f"{'done' if squashed else 'skipped' if args.no_squash else 'unsupported'}")


def cmd_watch(args, cfg):
    be, tunnel, _ = _be(cfg, args.via)
    name = args.name or cfg["name"]
    out = args.out or cfg.get("inbox", "~/relay-inbox")
    try:
        core.watch(be, tunnel, name, os.path.expanduser(out), keep=args.keep, once=args.once)
    except KeyboardInterrupt:
        pass


def cmd_store(args, cfg):
    be, _, store = _be(cfg, args.via)
    a = args.store_cmd
    if a == "put":
        dsts = core.store_put(be, store, args.paths, args.dir or "", cfg)
        _out(args, dsts, "\n".join(dsts))
    elif a == "ls":
        es = [e for e in be.list(store, args.path or "", recursive=args.recursive) if e.path != ".gitattributes"]
        if args.json:
            _out(args, [e.__dict__ for e in es], None)
        else:
            print("\n".join(f"{'' if e.is_dir else human(e.size):>8}  {e.path}{'/' if e.is_dir else ''}" for e in es) or "(empty)")
    elif a == "get":
        got = core.store_get(be, store, args.path, args.out or ".")
        _out(args, got, "\n".join(got))
    elif a == "rm":
        core.store_rm(be, store, args.paths)
        log(f"[store] removed {' '.join(args.paths)}")
    elif a == "url":
        print(be.curl_hint(store, args.path.strip("/")))


def cmd_install_skill(args, cfg):
    src = Path(__file__).with_name("SKILL.md")
    dst = Path(args.dir).expanduser() / "relay" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text())
    log(f"[relay] skill installed: {dst}")


def cmd_doctor(args, cfg):
    print(f"config:  {C.CONFIG_PATH} ({'exists' if C.CONFIG_PATH.exists() else 'MISSING, run relay init'})")
    print(f"name:    {cfg.get('name')}   default backend: {cfg.get('backend')}   chunk: {cfg.get('chunk_mb')}MB")
    for name in ("hf", "ms"):
        sec = cfg.get(name, {})
        if not sec.get("tunnel_repo"):
            print(f"{name}:      not configured")
            continue
        try:
            be = get_backend(cfg, name)
            t0 = time.time()
            n = len(core.list_transfers(be, sec["tunnel_repo"]))
            print(f"{name}:      OK {getattr(be, 'endpoint', '') or ''} user={be.whoami()} "
                  f"tunnel={sec['tunnel_repo']} ({n} pending) store={sec['store_repo']} [{time.time() - t0:.1f}s]")
        except Exception as e:
            print(f"{name}:      ERROR {type(e).__name__}: {str(e)[:200]}")


# ---------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(prog="relay", description="File tunnel / drive on top of HuggingFace & ModelScope repos")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, via=True, js=True):
        sp = sub.add_parser(name, help=help, description=help)
        sp.set_defaults(fn=fn)
        if via:
            sp.add_argument("--via", choices=["hf", "ms"], help="backend (default: config `backend`)")
        if js:
            sp.add_argument("--json", action="store_true", help="machine-readable output on stdout")
        return sp

    sp = add("init", cmd_init, "write ~/.config/relay/config.toml and create the private repos", via=False, js=False)
    sp.add_argument("--name", help="this machine's name (used by --to / watch); default hostname")
    sp.add_argument("--hf-endpoint", help="'auto' (probe huggingface.co, else hf-mirror) or a URL")
    sp.add_argument("--hf-token-file", help="default: huggingface_hub's own token ($HF_TOKEN / hf auth login)")
    sp.add_argument("--ms-token-file", help="default: ~/.config/relay/ms_token if present")
    sp.add_argument("--user", help="repo owner (default: token's user)")
    sp.add_argument("--backend", choices=["hf", "ms"], help="default backend")
    sp.add_argument("--inbox", help="default output dir for `watch`")
    sp.add_argument("--no-create", action="store_true", help="don't create repos")

    sp = add("push", cmd_push, "send files/dirs through the tunnel; prints a short code")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--to", help="recipient machine name (its `relay watch` picks it up)")
    sp.add_argument("--ttl", type=float, help="hours before `gc` may delete it (default config ttl_hours)")
    sp.add_argument("--code", help="choose the code instead of a random one")
    sp.add_argument("--keep", action="store_true", help="don't auto-delete after the first pull")
    sp.add_argument("--note")
    sp.add_argument("--curl", action="store_true", help="also print curl commands for machines without relay")

    sp = add("pull", cmd_pull, "receive a transfer by code (tries every configured backend)")
    sp.add_argument("code")
    sp.add_argument("-o", "--out", help="output dir (default .)")
    sp.add_argument("--keep", action="store_true", help="leave it in the tunnel after download")
    sp.add_argument("-f", "--force", action="store_true", help="overwrite differing local files")

    add("ls", cmd_ls, "list transfers waiting in the tunnel")

    sp = add("rm", cmd_rm, "delete transfers from the tunnel", js=False)
    sp.add_argument("codes", nargs="+")

    sp = add("gc", cmd_gc, "delete expired/stuck transfers and squash tunnel history (frees quota)", js=False)
    sp.add_argument("--no-squash", action="store_true")

    sp = add("watch", cmd_watch, "auto-receive transfers sent --to this machine", js=False)
    sp.add_argument("--name", help="recipient name (default config name)")
    sp.add_argument("-o", "--out", help="inbox dir (default config inbox)")
    sp.add_argument("--keep", action="store_true")
    sp.add_argument("--once", action="store_true", help="check once and exit")

    sp = add("store", cmd_store, "drive-like storage repo: put/ls/get/rm/url", via=False, js=False)
    ss = sp.add_subparsers(dest="store_cmd", required=True)

    def sadd(name, help):
        x = ss.add_parser(name, help=help)
        x.add_argument("--via", choices=["hf", "ms"])
        x.add_argument("--json", action="store_true")
        return x

    x = sadd("put", "upload files/dirs")
    x.add_argument("paths", nargs="+")
    x.add_argument("-d", "--dir", help="remote folder")
    x = sadd("ls", "list a remote folder")
    x.add_argument("path", nargs="?")
    x.add_argument("-r", "--recursive", action="store_true")
    x = sadd("get", "download a remote file or folder")
    x.add_argument("path")
    x.add_argument("-o", "--out")
    x = sadd("rm", "delete remote files/folders")
    x.add_argument("paths", nargs="+")
    x = sadd("url", "print a curl command for a remote file")
    x.add_argument("path")

    sp = add("install-skill", cmd_install_skill, "install the Claude Code skill for relay", via=False, js=False)
    sp.add_argument("--dir", default="~/.claude/skills")

    add("doctor", cmd_doctor, "show config and test each backend", via=False, js=False)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "json", False):
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    cfg = C.load()
    try:
        rc = args.fn(args, cfg)
    except KeyboardInterrupt:
        return 130
    except (C.ConfigError, LookupError, FileExistsError, FileNotFoundError, ValueError) as e:
        log(f"relay: {e}")
        return 1
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
