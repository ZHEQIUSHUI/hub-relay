import os
import tempfile
from pathlib import Path

from .base import Backend, Entry


def _status(e):
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None)


def _patch_mirror_lfs_completion():
    """hf-mirror rewrites the LFS multipart completion URL to the unresolvable host
    hf-mirror.org. xet uploads are unaffected, but if a client falls back to plain LFS
    the upload dies at the very last step — point it back at hf-mirror.com.
    Only possible on requests-based huggingface_hub (<1.0)."""
    try:
        import requests
        from huggingface_hub import configure_http_backend
    except ImportError:
        return

    class _Session(requests.Session):
        def request(self, method, url, *a, **kw):
            if isinstance(url, str) and "://hf-mirror.org" in url:
                url = url.replace("://hf-mirror.org", "://hf-mirror.com")
            return super().request(method, url, *a, **kw)

    try:
        configure_http_backend(backend_factory=_Session)
    except Exception:
        pass


class HFBackend(Backend):
    name = "hf"

    def __init__(self, endpoint, token=None):
        self.endpoint = endpoint.rstrip("/")
        # huggingface_hub reads HF_ENDPOINT at import time in a few code paths
        os.environ["HF_ENDPOINT"] = self.endpoint
        if "hf-mirror" in self.endpoint:
            _patch_mirror_lfs_completion()
        from huggingface_hub import HfApi

        self.token = token
        self.api = HfApi(endpoint=self.endpoint, token=token)

    def whoami(self):
        return self.api.whoami()["name"]

    def ensure_repo(self, repo):
        self.api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)

    def head(self, repo):
        return self.api.repo_info(repo, repo_type="dataset").sha

    def list(self, repo, prefix="", recursive=False):
        try:
            items = self.api.list_repo_tree(
                repo, path_in_repo=prefix.strip("/") or None, recursive=recursive, repo_type="dataset"
            )
            out = []
            for it in items:
                if it.__class__.__name__ == "RepoFolder":
                    out.append(Entry(it.path, is_dir=True))
                    continue
                lfs = getattr(it, "lfs", None)
                sha = None
                if lfs is not None:
                    sha = getattr(lfs, "sha256", None) or (lfs.get("sha256") if isinstance(lfs, dict) else None)
                out.append(Entry(it.path, size=it.size or 0, sha256=sha))
            return out
        except Exception as e:
            if _status(e) == 404 or "EntryNotFound" in type(e).__name__:
                return []
            raise

    def read_text(self, repo, path):
        with tempfile.TemporaryDirectory(prefix="relay-") as td:
            try:
                p = self.download(repo, path, td)
            except Exception as e:
                if _status(e) == 404 or "NotFound" in type(e).__name__:
                    return None
                raise
            return Path(p).read_text()

    def upload(self, repo, items, message):
        from huggingface_hub import CommitOperationAdd

        ops = [CommitOperationAdd(path_in_repo=dst, path_or_fileobj=src) for src, dst in items]
        self.api.create_commit(repo, operations=ops, commit_message=message, repo_type="dataset")

    def download(self, repo, path, dest_dir):
        return self.api.hf_hub_download(repo, path, repo_type="dataset", local_dir=dest_dir)

    def delete(self, repo, paths, message):
        from huggingface_hub import CommitOperationDelete

        ops = [CommitOperationDelete(path_in_repo=p, is_folder=p.endswith("/")) for p in paths]
        self.api.create_commit(repo, operations=ops, commit_message=message, repo_type="dataset")

    def squash(self, repo):
        self.api.super_squash_history(repo, repo_type="dataset", commit_message="relay gc: squash")
        return True

    def curl_hint(self, repo, path):
        return (f'curl -L --fail -C - -H "Authorization: Bearer $HF_TOKEN" '
                f'"{self.endpoint}/datasets/{repo}/resolve/main/{path}"')
