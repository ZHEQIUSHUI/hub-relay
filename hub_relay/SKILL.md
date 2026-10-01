---
name: relay
description: Move files between machines that can't reach each other directly, through private HuggingFace/ModelScope repos with the `relay` CLI. Use when a file must get to another machine, when someone gives you a relay code, or when asked to stash/fetch something in the personal store.
---

# relay — file tunnel over HF / ModelScope

`relay` is installed on this machine. Every machine has a name (`relay doctor` shows it, plus the backends and whether they work).

## Send to another machine
```bash
relay push <file-or-dir>... --to <machine>      # prints a short code on stdout, e.g. k7f3pq
```
- If the recipient runs `relay watch`, it lands in its inbox (default `~/relay-inbox`) automatically within ~1 min.
- Otherwise give the user the code and the receive command: `relay pull <code>`.
- Large files are chunked and resumable: if push dies, rerun the exact same command and it continues.
- `--keep` keeps it in the tunnel after the first pull; `--ttl H` sets expiry (default 72h).
- `--curl` additionally prints plain curl commands for machines without relay.

## Receive
```bash
relay pull <code> -o <dir>      # verifies sha256, then deletes the transfer from the tunnel
relay ls                        # what is waiting in the tunnel
```

## Persistent store (drive-like)
```bash
relay store put <paths>... -d <remote/dir>
relay store ls [remote/dir] [-r]
relay store get <remote/path> -o <dir>
relay store rm <remote/path>
```

## Notes
- `--via hf|ms` picks the backend; `pull` tries all configured backends automatically.
- `--json` gives machine-readable output on stdout; progress/logs go to stderr.
- Don't pass tokens on the command line; they live in `~/.config/relay/`.
- Don't use the tunnel for long-term storage — use `relay store`. `relay gc` cleans expired transfers.
