# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TinyPNG-Unlimited is a Python tool for unlimited batch image compression via TinyPNG API. It auto-generates TinyPNG API keys by registering temporary email addresses on apihz.cn, cycling through keys to bypass the 500 compression/month limit.

It ships **two front ends over one engine**: a Tkinter GUI (default when run with no arguments) and an argparse CLI (`dir` / `file` / `tasks` / `apply` / `rearrange` / `add_key` / `gui`). All compression logic lives in `TinyImg`; neither front end duplicates it.

## Commands

**Install dependencies:**

```bash
pip install -r requirements.txt
```

**Run the GUI** (no arguments — this is also what double-clicking the binary does):

```bash
python bin/main.py
python bin/main.py gui
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
python bin/main.py --version                            # print version (from version.py)
```

**Build / release:**

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm TinyPNG-Unlimited.spec   # run from repo root
```

Bump `tinypng_unlimited/version.py`, commit, then push a matching tag (e.g. `v1.2.0`).
`.github/workflows/release.yml` builds Windows / macOS (arm64 + Intel) / Linux and
creates the GitHub Release. **PyInstaller cannot cross-compile** — each artifact must be
built on its own OS, which is exactly what the CI matrix does. There is no test suite or
linter; CI does a `--version` smoke test on both the source tree and the packaged binary.

Release notes for `--version`: it uses argparse's `version` action, so it exits during
`parse_args()` and never reaches the trailing `input('回车退出')` — that is what makes it
usable as a CI smoke test.

`gui` sets `no_pause=True` for the same reason: the GUI is a long-lived window and must not
wait for an extra Enter after it closes.

## Architecture

```text
bin/main.py                   # entry point: no args → GUI, otherwise argparse CLI
tinypng_unlimited/
  version.py                  # __version__ 唯一来源（pyproject 走 dynamic version）
  config.py                   # Config class, .env read/write, get_app_dir(), save_env_values()
  gui.py                      # Tkinter GUI（标准库实现；tkinterdnd2 可选，用于拖拽）
  gui_state.json              # 运行期生成：窗口几何 + 上次的选项开关（.gitignore）
  errors.py                   # Exception hierarchy (base: CustomException)
  apihz_mail.py               # 接口盒子临时邮箱客户端（限速 6s/次，普通会员）
  key_manager.py              # API key lifecycle: load/save keys.json, auto-apply, rotate
  tiny_img.py                 # Compression engine: tinify wrapper, thread pool, progress bars
  __init__.py                 # Logger setup (loguru → tqdm), exports TinyImg + KeyManager + __version__
```

`config.py:get_app_dir()` 决定运行期工作目录：源码运行时是仓库根目录，
PyInstaller 打包后是 **exe 所在目录**。不要改回用 `__file__` 推导——onefile 模式下
`__file__` 指向临时解包目录（`sys._MEIPASS`），用户放在 exe 旁边的 `config.env` 会读不到。
首次运行会由 `ensure_config_file()` 从模板自动生成 `config.env`。

`config.env`、`keys.json`、`error_files.json`、`tmp/` 全部落在 `get_app_dir()` 下，
**不再是 `bin/`**。`KeyManager._migrate_legacy_keys()` 会在源码运行时把老位置的
`bin/keys.json` 一次性（且非覆盖地）搬到新位置，避免老用户密钥「凭空消失」。

### Data Flow

1. **Startup:** `KeyManager.init()` loads keys from `config.env` or `<workdir>/keys.json`; if fewer than `KEY_THRESHOLD` (default 3) available keys, auto-triggers `apply`
2. **Compression:** `TinyImg.compress_from_file_list()` runs a `ThreadPoolExecutor` (THREAD_NUM workers) over the file list; each worker checks quota via `check_compression_count()` under `RLock` and auto-rotates to the next key when count ≥ `KEY_USAGE_LIMIT` (490)
3. **Key generation:** `_apply_api_key()` calls apihz.cn to create a temp mailbox → registers at tinypng.com → polls inbox (6s intervals) → extracts activation link → calls `/api/keys`
4. **Idempotency:** Compressed files have `b'tiny'` appended as the last 4 bytes; re-runs skip them
5. **Error recovery:** Failed files retry up to `MAX_RETRY` times in-session; persistent failures are written to `error_files.json` and retried on the next run

### GUI (gui.py)

- **Tkinter only.** The project deliberately has no *required* GUI dependency — do not add
  customtkinter/PySide without a discussion about binary size. Dark theme is hand-rolled
  via `ttk.Style('clam')`.
- **Drag-and-drop is optional.** `tkinterdnd2` (≈2.8MB, ships `tkdnd` native libs for
  win/mac/linux) is imported behind `try/except` and exposed as `gui.HAS_DND`.
  `create_root()` returns `(TkinterDnD.Tk(), True)` when available, otherwise
  `(tk.Tk(), False)` — the CLI path never touches it, and a missing dep degrades to
  "type/paste paths" instead of crashing. `gui.selftest`-style output and
  `python bin/main.py selftest` both print `drag_and_drop=<bool>` so a
  "dropped a file, nothing happened" report can be triaged in one line.
- **Settings page writes `config.env`.** `config.save_env_values()` replaces only the
  right-hand side of matching `KEY=` lines, preserving comments and ordering, so the file
  stays human-editable. `reload_config()` is required afterwards because `load_dotenv()`
  does **not** override already-set env vars by default.
- **Never render a `Config` attribute with bare `str()`.** `TINYPNG_API_KEYS` defaults to a
  list; `str([])` is `'[]'`, and writing that into `config.env` produces a bogus key `'[]'`
  that used to wipe `keys.json`. Use `GuiApp._default_text()`.
- **Threading:** all compression runs in a worker thread; every UI update goes through
  `self.ui_queue` and is drained by `_poll_queue()` via `root.after`. Never touch widgets
  from the worker.
- **Progress/log:** the engine's `on_progress(done, total, name, ok, ok_total, err_total)`
  callback and a loguru sink that posts to the same queue. `logger.remove()` in
  `_install_log_sink()` drops the `tqdm` sink installed by `__init__.py`.
- **Everything visual goes through `px()`.** Fonts follow the Windows DPI scale but Tk
  padding/width numbers do not, so at 150% (this dev box is 2560×1440 @150%) a naively
  laid-out window looks cramped while the text is oversized. `px(n) = round(n *
  min(2.0, max(1.0, dpi/96)))` is applied to every pad/width; `main()` calls
  `shcore.SetProcessDpiAwareness(1)` **before** `create_root()` so Tk learns the real DPI.
- **`gui_state.json`** next to the exe stores window geometry + the three option toggles +
  the active tab. Geometry is clamped back to the current desktop on load, otherwise a
  state file written on a 4K monitor makes the window invisible on a laptop. It is a
  runtime file: `.gitignore`d, and must never be bundled into the spec.
- **The displayed compression rate is `(old-new)/old`, not the engine's `new/old`.**
  The engine reports output÷input; users read the number as "how much smaller did this
  get", so the GUI inverts it (a good result shows ~70%, not ~29%). `test_gui_e2e.py`
  has a regression check that compares the on-screen figure against the files on disk.
- **Toggles are native `tk.Checkbutton`, not `ttk`.** Under the `clam` theme
  `indicatorcolor` / `indicatorrelief` / `lightcolor` / `darkcolor` are silently ignored,
  so an *unchecked* box renders as a solid light square and reads as "on". Use
  `selectcolor=SURFACE_3`; there is no styled alternative that actually works.
- **Checkbox `selectcolor` and the progress trough both needed explicit dark values** —
  the `clam` defaults are light-grey and were the two remaining "white blobs" in an
  otherwise dark UI.

### Stop semantics (non-obvious)

`should_stop` cancels only **not-yet-started** futures via
`pool.shutdown(wait=False, cancel_futures=True)`; in-flight images always finish, so stop is
safe and never leaves a half-written file. Two traps:

1. **`as_completed()` never yields cancelled futures** (verified on Python 3.13). Do not try
   to keep draining it after cancelling — it blocks forever, which looked like "UI hangs on
   stop until timeout". The code now breaks out and re-iterates `future_list` directly.
2. Conversely, cancelling too early would **discard results of images that were already
   compressed and written to disk**, showing "成功 0" while several files had in fact changed.
   The drain loop exists to keep the on-screen counters consistent with the filesystem.

Because `cancel_futures` races with workers picking items off the queue, a task may be
cancelled at the boundary and simply not run. That is benign (nothing is corrupted).

### Key Design Points

- `TinyImg._lock` (RLock) guards key switching and quota checks across threads
- apihz.cn enforces 10 req/min for regular members; `ApihzMail._min_interval = 6.0s` is enforced via `_ensure_rate_limit()`
- Key state persists across restarts in `bin/keys.json` (available + unavailable lists)
- loguru is wired to write through `tqdm.write()` to avoid clobbering progress bars
- **两条独立 HTTP 通道**：上传走 `tinify.get_client().session`，下载走 `TinyImg._session`。
  改超时/连接池/代理时**两条都要处理**——历史上就出现过「上传走代理、下载直连」的漏配。
- `ensure_session_pool()` 按 `max(16, THREAD_NUM×2)` 放大连接池，必须同时挂到上面两条通道上。
  requests 默认池只有 10，超限后 urllib3 会丢弃连接、每个请求退回完整 TLS 握手（实测 +1.3s/请求）。
  另外 `tinify.key` 与 `tinify.proxy` 的 setter **都会重建 client/session**，改完必须重新挂载。
- 代理池（`_proxies` / `_pick_proxy()` / `_note_proxy()`）：线程粘性绑定一条代理以保住长连接，
  连续失败 3 次隔离 60 秒。TinyPNG 按出口 IP 限流（实测单 IP ≈ 11.65 请求/秒），
  换密钥对提速无效（实测 4 key 并行只有 0.71×）。
- `to_file_save()` 的临时文件名带 `pid_tid_seq`：同名不同内容的文件（如 `icon_tip.png`
  在 `images/common` 与 `images/device` 下各有一份）并发处理时不能共用临时路径。
- `KeyManager.store_key()` 先写临时文件再 `os.replace()` 原子替换。旧实现直接
  `open(path,'w')` 会先把 `keys.json` 截断成 0 字节，中途被中断就永久丢密钥。
- `KEY_PATTERN` 会过滤掉格式非法的密钥记录。这类脏数据最典型的来源是表单把
  列表型默认值渲染成字符串写进了 `config.env`（见 GUI 一节），不过滤的话会白跑一轮
  联网验证，最后只报一句含糊的「所有密钥均无效」。
- `Config.TINYPNG_API_KEYS` 是**合并**进 `keys.json` 而不是覆盖。旧实现用环境变量整体
  覆盖并立即落盘，环境变量写错一个值就会把本地已申请到的密钥全部抹掉。

### Packaging gotchas

- **`tkinter` must not be in the spec's `excludes`.** It was there from the CLI-only era;
  leaving it in produces a binary that dies on startup with `ModuleNotFoundError`.
- `tinypng_unlimited.gui` is imported lazily inside `command_gui()`, so PyInstaller's static
  analysis cannot see it — it must be listed in `hiddenimports`.
- `icon.ico` is bundled via `datas` purely so the GUI can set its **window** icon at runtime;
  `EXE(icon=...)` only sets the icon shown by Explorer.
- The managed Python toolchain used for isolated builds may lack `_tkinter`; verify before
  blaming the spec (`python -c "import tkinter"`).
- `console=True` is intentional for both modes: the CLI needs stdout, and the GUI hides the
  freshly-created console window itself (`gui.hide_console_if_owned()`, which checks
  `GetConsoleProcessList` so it won't hide a terminal the user launched from).
- **`tkinterdnd2` needs its native libs, not just the `.py` files.** The spec calls
  `collect_data_files('tkinterdnd2')` (wrapped in try/except so a build box without the
  optional dep still produces a working no-drag binary) and adds `tkinterdnd2` to
  `hiddenimports`. Without the `datas` the app builds fine and then dies at
  `create_root()` with "can't find package tkdnd". `release.yml` asserts the import before
  packaging so this fails in CI instead of in a user's hands.

## Configuration

`config.env` is auto-generated from `config.env.template` on first run (both source and
packaged). The GUI's 设置 page edits it in place; you can also edit it by hand. Key variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TINYPNG_API_KEYS` | _(empty)_ | Pre-seeded API keys (comma-separated) |
| `APIHZ_ID` | _(empty)_ | **Required** for auto-applying keys — apihz.cn developer ID |
| `APIHZ_KEY` | _(empty)_ | **Required** for auto-applying keys — apihz.cn developer key |
| `HTTP_PROXY` / `HTTPS_PROXY` | _(empty)_ | Single proxy for all outbound requests (upload **and** download) |
| `PROXY_LIST` | _(empty)_ | Comma/semicolon/newline-separated proxy list; overrides the single proxy. Threads bind stickily to one entry each; 3 consecutive failures quarantine a proxy for 60s |
| `THREAD_NUM` | `4` | Concurrent compression workers; the connection pool auto-scales to `max(16, THREAD_NUM×2)`, 16–24 is the sweet spot |
| `KEY_THRESHOLD` | `3` | Min available keys before auto-apply |
| `KEY_USAGE_LIMIT` | `490` | Compressions per key before rotation |
| `MAX_RETRY` | `3` | Per-file retry attempts |
| `UPLOAD_TIMEOUT` | `60` | Seconds for tinify upload |
| `DOWNLOAD_TIMEOUT` | `30` | Seconds for compressed image download |

Loading priority: environment variables → `config.env` → hardcoded defaults.
`Config.load(override=True)` (used by the GUI after saving settings) flips the first two,
because `load_dotenv()` otherwise ignores changes to variables that are already in `os.environ`.
