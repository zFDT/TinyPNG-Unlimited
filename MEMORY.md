# MEMORY.md — TinyPNG-Unlimited 设计备忘

> 本文件记录**当前有效**的设计事实与实测数据，供后续维护参考。
> 2026-09 之前的内容基于已被替换的 SnapMail 实现，已整体重写。

## 版本与发版

- 版本号唯一来源：`tinypng_unlimited/version.py` 的 `__version__`。
  `pyproject.toml` 通过 `dynamic = ["version"]` 引用它，CLI 用 `--version` 输出。
- 发版流程：改版本号 → 提交 → 推同名 `v*` tag。
  `.github/workflows/release.yml` 自动构建 Windows x64 / macOS arm64 / macOS x64 / Linux x64
  四份产物并创建 GitHub Release。
- **PyInstaller 不支持交叉编译**，每份产物必须在对应操作系统上构建——这就是 CI 用 matrix 的原因。
- 产物未做代码签名。macOS 需 `xattr -dr com.apple.quarantine`，Windows 会被 SmartScreen 拦一次。
  macOS 产物是裸可执行文件（没有 `.app` bundle），双击不启动，需在终端运行。

## 运行期工作目录

`config.py:get_app_dir()` 是唯一权威：

- 源码运行 → 仓库根目录
- PyInstaller 打包后 → **可执行文件所在目录**

**不要改回用 `__file__` 推导。** onefile 模式下 `__file__` 指向临时解包目录（`sys._MEIPASS`），
放在 exe 旁边的 `config.env` 会永远读不到。首次运行由 `ensure_config_file()` 从模板
自动生成 `config.env`；`keys.json`、`tmp/` 也都在同一目录。

优先级：环境变量 > `config.env` > 代码默认值（`python-dotenv` 的 `load_dotenv` 不覆盖已有环境变量）。

## 临时邮箱与密钥申请

- 邮箱服务商已从 SnapMail 换成**接口盒子 apihz.cn**（`tinypng_unlimited/apihz_mail.py`）。
  需要 `APIHZ_ID` / `APIHZ_KEY`；普通会员限速 **6 秒/次**，由 `_ensure_rate_limit()` 统一拦截。
- 密钥状态持久化在 `<工作目录>/keys.json`（`available` / `unavailable` 两个列表）。
  老版本的 `bin/keys.json` 由 `KeyManager._migrate_legacy_keys()` 一次性搬过去（非覆盖）。
- 可用密钥数少于 `KEY_THRESHOLD`（默认 3）时**只打提醒，不再自动申请**
  （2026-09 起注册加验证码，自动申请链路已删：`_apply_api_key()` / `apply_store_key()`）。
- 单密钥用量达到 `KEY_USAGE_LIMIT`（默认 490，TinyPNG 上限 500/月）时切换到下一条。
- **配额按内容计，不按请求数计**：同一份字节重复上传不增加 `compression-count`（服务端按内容缓存），
  不同内容才 +1。

## 压缩引擎要点（`tiny_img.py`）

### 两条独立 HTTP 通道

| 通道 | 会话 | 用途 |
| --- | --- | --- |
| 上传 | `tinify.get_client().session` | `POST /shrink` |
| 下载 | `TinyImg._session` | 拉取压缩结果 |

改超时、连接池、代理时**两条都必须处理**。历史上出现过「上传走代理、下载直连」的漏配。

### 连接池

- `ensure_session_pool()` 按 `max(16, THREAD_NUM × 2)` 放大，同时挂到上面两条通道。
- requests 默认池只有 10，并发超限后 urllib3 会丢弃连接、每个请求退回完整 TCP+TLS 握手。
  实测同一请求：复用连接 **0.449s** vs 新建连接 **1.747s**。
- `tinify.key` 与 `tinify.proxy` 的 setter **都会重建 client/session**，改完必须重新挂载池。

### 代理池

- `PROXY_LIST` 支持逗号/分号/换行分隔的多个代理，逐个线程**粘性绑定**以保住长连接复用。
- 某条代理连续失败 3 次 → 隔离 60 秒，绑定它的线程自动改走其他代理。
- 一个代理混合端口只对应一个出口节点；要多出口需多个本地端口绑不同节点。
  Mihomo 的 `listeners` 没有绑定出口的字段，要靠 `IN-PORT` 规则分流。

### 幂等标记

- 压缩后的文件尾部追加 4 字节 `b'tiny'`，重复运行默认跳过。
- 云端无收益（`output.size >= input.size`）且原地覆盖时**跳过下载**，直接保留原文件——
  既省一次往返，也避免把标记重复追加成 `tinytiny`。
  副作用：这类文件不会被打上标记，下次运行会重新上传（上传本身不消耗配额，见上文）。

### 并发文件名

`to_file_save()` 的临时文件名带 `pid_tid_seq` 后缀。同一目录树下存在同名不同内容的文件
（如 `images/common/icon_tip.png` 与 `images/device/icon_tip.png`），
用时间戳做后缀会在并发时互相覆盖。

## 实测性能基线（本机网络环境）

| 项目 | 数值 |
| --- | --- |
| 服务端限流 | 按**出口 IP**，单 IP ≈ 11.65 请求/秒 |
| 端到端上限 | ≈ 5~6 文件/秒（每文件需上传+下载两次请求） |
| 多密钥对提速 | 无效果，4 key 并行只有 **0.71×** |
| 6 线程 | 2.10 → 2.70 文件/秒（连接池优化前后） |
| 24 线程 | 3.45 → 4.66 文件/秒 |

## 图形界面（`gui.py`，v1.2.0 起）

### 依赖与降级

- 必需依赖为零（标准库 Tkinter）。唯一可选依赖是 **`tkinterdnd2`**（拖拽，≈2.8MB）。
- 用 `try/except` 导入并暴露 `gui.HAS_DND`；`create_root()` 可用时返回
  `(TkinterDnD.Tk(), True)`，否则回退 `(tk.Tk(), False)`，即「不能拖但能用」。
- `python bin/main.py selftest` 会打印 `drag_and_drop=True/False`。
  用户报「拖进去没反应」时，这一行即可区分是依赖缺失还是用法问题。

### 界面语义（容易做反的地方）

| 项 | 正确做法 | 反例与后果 |
| --- | --- | --- |
| 压缩率 | `(原大小-新大小)/原大小` | 引擎返回的是 `输出/输入`；直接用会显示 29% 而真实省了 70% |
| 复选框 | 原生 `tk.Checkbutton` + `selectcolor` | clam 主题忽略 `indicatorcolor`，未选中会渲染成实心浅色方块，看着像已勾选 |
| 进度条槽 | 显式深色 `#363b42` | 沿用 `SURFACE_2` 会与卡片同色，0% 时整条轨道「消失」 |
| 尺寸换算 | 所有 pad/width 走 `px()` | 字号跟 DPI 缩放、内边距不跟，150% 下排版发挤 |

### 状态与清理

- `gui_state.json`（工作目录下）存窗口几何 + 三个选项开关 + 当前标签页，
  加载时会把越界几何夹回当前桌面尺寸（否则在 4K 上保存的状态会让笔记本上窗口跑到屏幕外）。
- 该文件是运行期产物：已进 `.gitignore`，且**不得**写进 `.spec` 的 `datas`。
