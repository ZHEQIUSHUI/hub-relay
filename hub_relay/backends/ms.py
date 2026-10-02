"""ModelScope backend (dataset repos, branch `master`).

ModelScope has no history squash, so `gc` only deletes; quota is reclaimed by
recreating the repo if it ever matters.
"""
import contextlib
import functools
import hashlib
import io
import os
import sys
import tempfile
import threading
from pathlib import Path

from .base import Backend, Entry

REV = "master"
# redirect_stdout/stderr swap process-wide globals, so concurrent calls (ls reads manifests
# from a thread pool) would restore each other's streams in the wrong order — serialize
_SDK_LOCK = threading.RLock()


def _quiet(fn):
    """The SDK prints to stdout (which must stay clean for `relay push` / --json) and draws
    tqdm bars; send prints to stderr and drop the bars when not on a terminal."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        bars_off = os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS") or not sys.stderr.isatty()
        with _SDK_LOCK, contextlib.ExitStack() as st:
            st.enter_context(contextlib.redirect_stdout(io.StringIO() if bars_off else sys.stderr))
            if bars_off:
                st.enter_context(contextlib.redirect_stderr(io.StringIO()))
            return fn(*a, **kw)
    return wrapper


class MSBackend(Backend):
    name = "ms"

    def __init__(self, token=None, endpoint=None):
        try:
            from modelscope.hub.api import HubApi
        except ImportError:
            raise RuntimeError("ModelScope backend needs `pip install modelscope` (or hub-relay[ms])")
        self.token = token or os.environ.get("MODELSCOPE_API_TOKEN")
        if not self.token:
            raise RuntimeError("no ModelScope token: set [ms] token_file in config or $MODELSCOPE_API_TOKEN")
        self.endpoint = endpoint
        import logging
        for lg in ("modelscope", "modelscope_hub"):
            logging.getLogger(lg).setLevel(logging.WARNING)
        self.api = HubApi()
        self.cookies = self.api.login(self.token)[1]

    def _kw(self):
        kw = {"token": self.token}
        if self.endpoint:
            kw["endpoint"] = self.endpoint
        return kw

    def whoami(self):
        import requests
        r = requests.get(f"{self.endpoint or 'https://modelscope.cn'}/api/v1/users/login/info",
                         cookies=self.cookies, timeout=20)
        r.raise_for_status()
        name = (r.json().get("Data") or {}).get("Name")
        if not name:
            raise RuntimeError("could not determine ModelScope user name")
        return name

    @_quiet
    def ensure_repo(self, repo):
        self.api.create_repo(repo, repo_type="dataset", visibility="private", exist_ok=True, **self._kw())

    @_quiet
    def _files(self, repo, root, recursive):
        out, page = [], 1
        while True:
            batch = self.api.get_dataset_files(
                repo, revision=REV, root_path=root or "/", recursive=recursive,
                page_number=page, page_size=500, **self._kw())
            out.extend(batch or [])
            if not batch or len(batch) < 500:
                return out
            page += 1

    def head(self, repo):
        h = hashlib.sha1()
        for e in sorted(self.list(repo, "t", recursive=False), key=lambda e: e.path):
            h.update(e.path.encode())
        return h.hexdigest()

    def list(self, repo, prefix="", recursive=False):
        try:
            raw = self._files(repo, prefix.strip("/"), recursive)
        except Exception as e:
            if "404" in str(e) or "not exist" in str(e).lower():
                return []
            raise
        out = []
        for f in raw:
            path = f.get("Path") or f.get("path")
            if f.get("Type") == "tree":
                out.append(Entry(path, is_dir=True))
            else:
                out.append(Entry(path, size=int(f.get("Size") or 0), sha256=f.get("Sha256") or None))
        return out

    def read_text(self, repo, path):
        with tempfile.TemporaryDirectory(prefix="relay-") as td:
            try:
                p = self.download(repo, path, td)
            except Exception as e:
                if "404" in str(e) or "not" in str(e).lower():
                    return None
                raise
            return Path(p).read_text()

    @_quiet
    def upload(self, repo, items, message):
        for src, dst in items:
            self.api.upload_file(repo_id=repo, path_or_fileobj=src, path_in_repo=dst,
                                 repo_type="dataset", commit_message=message, token=self.token)

    @_quiet
    def download(self, repo, path, dest_dir):
        from modelscope.hub.file_download import dataset_file_download
        return dataset_file_download(repo, path, local_dir=dest_dir, revision=REV, **self._kw())

    @_quiet
    def delete(self, repo, paths, message):
        files = []
        for p in paths:
            if p.endswith("/"):
                files += [e.path for e in self.list(repo, p, recursive=True) if not e.is_dir]
            else:
                files.append(p)
        if files:
            self.api.delete_files(repo, repo_type="dataset", file_paths=files, revision=REV,
                                  commit_message=message, **self._kw())

    def curl_hint(self, repo, path):
        # file downloads only accept the web session cookie, not a bearer token: log in first
        ep = self.endpoint or "https://modelscope.cn"
        login = (f"SID=$(curl -s -c - -X POST {ep}/api/v1/login -H 'Content-Type: application/json' "
                 "-d \"{\\\"AccessToken\\\":\\\"$MODELSCOPE_API_TOKEN\\\"}\" | awk '$6==\"m_session_id\"{print $7}')")
        return (f'{login}; curl -L --fail -C - -b "m_session_id=$SID" '
                f'"{ep}/api/v1/datasets/{repo}/repo?Revision={REV}&FilePath={path}"')
