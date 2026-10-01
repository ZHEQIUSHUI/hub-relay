"""ModelScope backend (dataset repos, branch `master`).

ModelScope has no history squash, so `gc` only deletes; quota is reclaimed by
recreating the repo if it ever matters.
"""
import hashlib
import os
import tempfile
from pathlib import Path

from .base import Backend, Entry

REV = "master"


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
        self.api = HubApi()
        self.api.login(self.token)

    def _kw(self):
        kw = {"token": self.token}
        if self.endpoint:
            kw["endpoint"] = self.endpoint
        return kw

    def whoami(self):
        # login() succeeded; ModelScope's user name lives in the session cookies
        for c in self.api.get_cookies() or []:
            if getattr(c, "name", "") in ("user_name", "username"):
                return c.value
        return "?"

    def ensure_repo(self, repo):
        self.api.create_repo(repo, repo_type="dataset", visibility="private", exist_ok=True, **self._kw())

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

    def upload(self, repo, items, message):
        for src, dst in items:
            self.api.upload_file(repo_id=repo, path_or_fileobj=src, path_in_repo=dst,
                                 repo_type="dataset", commit_message=message, token=self.token)

    def download(self, repo, path, dest_dir):
        from modelscope.hub.file_download import dataset_file_download
        return dataset_file_download(repo, path, local_dir=dest_dir, revision=REV, **self._kw())

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
        return (f'curl -L --fail -C - -H "Authorization: Bearer $MODELSCOPE_API_TOKEN" '
                f'"https://modelscope.cn/api/v1/datasets/{repo}/repo?Revision={REV}&FilePath={path}"')
