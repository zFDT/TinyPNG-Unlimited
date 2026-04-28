# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TinyPNG-Unlimited is a Python CLI tool for unlimited batch image compression via TinyPNG API. It auto-generates TinyPNG API keys by registering temporary SnapMail email addresses, cycling through keys to bypass the 500 compression/month limit.

## Commands

**Install dependencies:**

```bash
pip install -r requirements.txt
```

**Run the CLI:**

```bash
python bin/main.py <command> [options]
```

**Subcommands:**

```bash
python bin/main.py file "path/to/image.jpg"           # compress single file
python bin/main.py dir [-d DIR] [-p PROXY] [-r] [-l]  # compress directory
python bin/main.py tasks "tasks.json" [-r] [-l]        # batch from JSON
python bin/main.py apply [NUM]                          # generate N new API keys
python bin/main.py rearrange                            # sort keys by quota remaining
python bin/main.py add_key "api_key_here"              # manually add a key
```

No test framework, linter, or build system is configured.

## Architecture

```text
bin/main.py                   # argparse CLI entry point → 6 command handlers
tinypng_unlimited/
  config.py                   # Config class: loads .env + env vars, typed accessors
  errors.py                   # Exception hierarchy (base: CustomException)
  snapmail.py                 # SnapMail temp-email client (rate-limited to 10s intervals)
  key_manager.py              # API key lifecycle: load/save keys.json, auto-apply, rotate
  tiny_img.py                 # Compression engine: tinify wrapper, thread pool, progress bars
  __init__.py                 # Logger setup (loguru → tqdm), exports TinyImg + KeyManager
```

### Data Flow

1. **Startup:** `KeyManager.init()` loads keys from `config.env` or `bin/keys.json`; if fewer than `KEY_THRESHOLD` (default 3) available keys, auto-triggers `apply`
2. **Compression:** `TinyImg.compress_from_file_list()` runs a `ThreadPoolExecutor` (4 workers) over the file list; each worker checks quota via `check_compression_count()` under `RLock` and auto-rotates to the next key when count ≥ `KEY_USAGE_LIMIT` (490)
3. **Key generation:** `_apply_api_key()` creates a SnapMail inbox → registers at tinypng.com → waits 12s → fetches confirmation email → activates via token → calls `/api/keys`
4. **Idempotency:** Compressed files have `b'tiny'` appended as the last 4 bytes; re-runs skip them
5. **Error recovery:** Failed files retry up to `MAX_RETRY` times in-session; persistent failures are written to `error_files.json` and retried on the next run

### Key Design Points

- `TinyImg._lock` (RLock) guards key switching and quota checks across threads
- SnapMail enforces a 10-second minimum between API calls via `_ensure_rate_limit()`
- Key state persists across restarts in `bin/keys.json` (available + unavailable lists)
- loguru is wired to write through `tqdm.write()` to avoid clobbering progress bars

## Configuration

Copy `config.env.template` to `config.env`. Key variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TINYPNG_API_KEYS` | _(empty)_ | Pre-seeded API keys (comma-separated) |
| `SNAPMAIL_API_KEY` | _(empty)_ | **Required** for auto-applying keys — get from snapmail.cc "My Account" |
| `HTTP_PROXY` / `HTTPS_PROXY` | _(empty)_ | Proxy for all outbound requests |
| `THREAD_NUM` | `4` | Concurrent compression workers |
| `KEY_THRESHOLD` | `3` | Min available keys before auto-apply |
| `KEY_USAGE_LIMIT` | `490` | Compressions per key before rotation |
| `MAX_RETRY` | `3` | Per-file retry attempts |
| `UPLOAD_TIMEOUT` | `60` | Seconds for tinify upload |
| `DOWNLOAD_TIMEOUT` | `30` | Seconds for compressed image download |

Loading priority: environment variables → `config.env` → hardcoded defaults.
