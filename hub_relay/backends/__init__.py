from .. import config as C


def get_backend(cfg, name=None):
    name = name or cfg.get("backend", "hf")
    if name == "hf":
        from .hf import HFBackend
        sec = cfg.get("hf", {})
        return HFBackend(sec.get("endpoint", C.HF_OFFICIAL), C.read_token(sec))
    if name == "ms":
        from .ms import MSBackend
        sec = cfg.get("ms", {})
        return MSBackend(C.read_token(sec), sec.get("endpoint"))
    raise ValueError(f"unknown backend {name!r} (hf|ms)")


def repos(cfg, name):
    """(tunnel_repo, store_repo) for backend `name`."""
    sec = cfg.get(name, {})
    t, s = sec.get("tunnel_repo"), sec.get("store_repo")
    if not t or not s:
        raise C.ConfigError(f"[{name}] tunnel_repo/store_repo not set — run `relay init`")
    return t, s
