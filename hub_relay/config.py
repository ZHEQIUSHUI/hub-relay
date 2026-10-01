"""Config at ~/.config/relay/config.toml (override with $RELAY_CONFIG)."""
import os
import socket
from pathlib import Path

try:
    import tomllib
except ImportError:  # py<3.11
    import tomli as tomllib

CONFIG_PATH = Path(os.environ.get("RELAY_CONFIG", "~/.config/relay/config.toml")).expanduser()
CACHE_DIR = Path(os.environ.get("RELAY_CACHE", "~/.cache/relay")).expanduser()

HF_OFFICIAL = "https://huggingface.co"
HF_MIRROR = "https://hf-mirror.com"

DEFAULTS = {
    "name": socket.gethostname(),
    "backend": "hf",
    "chunk_mb": 1024,
    "ttl_hours": 72,
    "inbox": "~/relay-inbox",
    "hf": {"endpoint": HF_OFFICIAL},
    "ms": {},
}


class ConfigError(Exception):
    pass


def load():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if isinstance(v, dict):
                cfg.setdefault(k, {}).update(v)
            else:
                cfg[k] = v
    env_ep = os.environ.get("RELAY_HF_ENDPOINT")
    if env_ep:
        cfg["hf"]["endpoint"] = env_ep
    return cfg


def read_token(section):
    """token = "..." wins, then token_file, else None (backend falls back to its own default)."""
    if section.get("token"):
        return section["token"].strip()
    tf = section.get("token_file")
    if tf:
        p = Path(tf).expanduser()
        if p.exists():
            return p.read_text().strip()
    return None


def _q(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(cfg):
    lines = ["# hub-relay config — see `relay --help`"]
    sections = []
    for k, v in cfg.items():
        if isinstance(v, dict):
            sections.append((k, v))
        else:
            lines.append(f"{k} = {_q(v)}")
    for name, sec in sections:
        if not sec:
            continue
        lines.append("")
        lines.append(f"[{name}]")
        for k, v in sec.items():
            lines.append(f"{k} = {_q(v)}")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("\n".join(lines) + "\n")
    os.chmod(CONFIG_PATH, 0o600)
