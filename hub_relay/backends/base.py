from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class Entry:
    path: str
    size: int = 0
    is_dir: bool = False
    sha256: Optional[str] = None  # known for LFS/xet files only


class Backend:
    """A hub repo seen as a flat key/value file store.

    `repo` arguments are full ids like "user/relay-tunnel"; all repos are dataset repos.
    """

    name = "?"

    def ensure_repo(self, repo: str) -> None:
        raise NotImplementedError

    def head(self, repo: str) -> str:
        """Cheap token that changes whenever the repo changes (used by `watch`)."""
        raise NotImplementedError

    def list(self, repo: str, prefix: str = "", recursive: bool = False) -> List[Entry]:
        """Entries under `prefix` (paths are repo-relative). Missing prefix -> []."""
        raise NotImplementedError

    def read_text(self, repo: str, path: str) -> Optional[str]:
        raise NotImplementedError

    def upload(self, repo: str, items: List[Tuple[object, str]], message: str) -> None:
        """items = [(local path or bytes, path_in_repo)], ideally one commit."""
        raise NotImplementedError

    def download(self, repo: str, path: str, dest_dir: str) -> str:
        """Fetch `path` into dest_dir (keeping its repo-relative path); return local file path."""
        raise NotImplementedError

    def delete(self, repo: str, paths: List[str], message: str) -> None:
        """Delete files or folders (folders given with a trailing '/')."""
        raise NotImplementedError

    def squash(self, repo: str) -> bool:
        """Drop history so deleted files stop counting against quota. False if unsupported."""
        return False

    def curl_hint(self, repo: str, path: str) -> str:
        return ""

    def whoami(self) -> str:
        raise NotImplementedError
