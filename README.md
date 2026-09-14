# TinyPNG-Unlimited

> 半自动申请 API 密钥、多线程、带进度条的 TinyPNG 批量云压缩工具（图形界面 + 命令行）
>
> 本项目仅供技术研究使用，请勿用于任何商业及非法用途，任何后果作者概不负责。

---

## 功能特性

1. **图形界面（GUI）**：双击即用，图片/文件夹可以**直接拖进窗口**；配置项直接在「设置」页填，**不需要自己创建或编辑 `.env` 文件**；带实时进度条、统计与运行日志，可随时「停止」；窗口大小与上次的选项会被记住
2. 通过[接口盒子](https://www.apihz.cn)临时邮箱 + **手动注册向导**（只需人工过一次验证码，程序自动收信激活）申请 TinyPNG API 密钥，实现无限制压缩
3. 可用密钥接近 500 次限额时**自动切换**到下一条密钥
4. 多线程并发上传/下载，**加速批量压缩**（线程数可配置）
5. 上传、下载、整体任务均有**进度条**
6. 已压缩的文件写入标记字节，**重复运行自动跳过**
7. 支持**递归子文件夹**及**正则匹配**文件名
8. 支持通过 JSON 配置文件**批量提交任务**
9. 支持配置**代理**，也支持**多代理列表**把上传/下载分散到多个出口 IP
10. 上传/下载带**超时保护**，失败自动重试，超限保存错误列表供下次继续
11. 按并发规模**自动放大 HTTP 连接池**，并发超过 10 时不会退化成每次重新握手

---

## 安装

### 方式一：下载预编译产物（推荐）

前往 [Releases](https://github.com/zFDT/TinyPNG-Unlimited/releases/latest) 按平台下载，无需 Python 环境：

| 平台 | 文件 |
| --- | --- |
| Windows x64 | `TinyPNG-Unlimited-windows-x64.exe` |
| macOS Apple Silicon（M 系列） | `TinyPNG-Unlimited-macos-arm64` |
| macOS Intel | `TinyPNG-Unlimited-macos-x64` |
| Linux x64 | `TinyPNG-Unlimited-linux-x64` |

**首次运行会在可执行文件旁边自动生成 `config.env`**（从模板复制）；也可以用图形界面直接配置，见下文。
`keys.json`、`tmp/` 同样落在可执行文件所在目录。查看版本：`./TinyPNG-Unlimited-<平台> --version`。

> **双击 = 图形界面，带参数 = 命令行**，同一个文件两种用法。
> Windows 产物按 **windowed 子系统**打包（PE `Subsystem=2`），双击时**根本不会弹出控制台窗口**（不是「弹出来再隐藏」）；
> 从已有终端带参数启动时，程序会重新挂回父控制台（`AttachConsole`），
> 所以输出、管道和退出码都和正常的命令行程序一样。

> ⚠️ 产物未做代码签名：
> - **macOS** 未签名，需要先去掉隔离属性：
>   ```bash
>   chmod +x TinyPNG-Unlimited-macos-arm64
>   xattr -dr com.apple.quarantine TinyPNG-Unlimited-macos-arm64
>   ./TinyPNG-Unlimited-macos-arm64          # 不加参数 = 图形界面
>   ```
>   图形界面需要系统里有 Tk；若提示缺少 `tkinter`，用命令行模式即可，或 `brew install python-tk`。
> - **Windows** 未签名，SmartScreen 可能拦截，点「更多信息 → 仍要运行」。
> - **Linux** `chmod +x` 后直接运行；图形界面需要 `python3-tk`（`sudo apt install python3-tk`）。

### 方式二：源码运行

```bash
git clone https://github.com/zFDT/TinyPNG-Unlimited.git
cd TinyPNG-Unlimited

# 安装依赖（需 Python 3.10+）
pip install -r requirements.txt

python main.py          # 打开图形界面
```

### 方式三：自行打包

PyInstaller 不支持交叉编译，**要哪个平台的产物就得在哪个平台上打包**；
推 `v*` tag 后 GitHub Actions 会自动构建三平台产物并创建 Release
（见 `.github/workflows/release.yml`）。本地构建：

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm TinyPNG-Unlimited.spec
```

---

## 配置

首次运行时会自动在可执行文件（或源码根目录）旁边生成 `config.env`，按需修改即可；
也可以手动复制模板：

```bash
cp config.env.template config.env
```

`config.env` 关键配置项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TINYPNG_API_KEYS` | 空 | 手动预置的 API 密钥（逗号分隔），优先使用 |
| `APIHZ_ID` | **必填** | 接口盒子开发者 ID（登录 apihz.cn 后在个人中心获取） |
| `APIHZ_KEY` | **必填** | 接口盒子开发者 KEY |
| `HTTP_PROXY` / `HTTPS_PROXY` | 空 | 单条代理地址，如 `http://127.0.0.1:7890`，上传与下载都走它 |
| `PROXY_LIST` | 空 | 代理列表（逗号/分号/换行分隔），设置后覆盖单条代理，见下文 |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `THREAD_NUM` | `4` | 并发压缩线程数。连接池会按 `max(16, THREAD_NUM×2)` 自动放大，建议 16~24 |
| `KEY_THRESHOLD` | `3` | 可用密钥少于此数量时给出提示；**该阈值当前不再触发自动申请**（自动申请链路已失效，只会打一条告警并提示手动添加，见下文） |
| `KEY_USAGE_LIMIT` | `490` | 单密钥使用次数上限（达到后切换，TinyPNG 限 500/月） |
| `UPLOAD_TIMEOUT` | `60` | 上传超时时间（秒） |
| `DOWNLOAD_TIMEOUT` | `30` | 下载超时时间（秒） |
| `MAX_RETRY` | `3` | 单文件最大重试次数 |

> `config.env` 已加入 `.gitignore`，不会提交到版本控制。

### 关于代理

TinyPNG 对同一 IP 短时间内注册账号有频率限制。如遇到"注册频繁"或邮件始终收不到的情况，建议在 `config.env` 中配置本地代理：

```ini
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

代理会同时作用于**上传**（`api.tinify.com/shrink`）和**下载**（返回的图片链接）两条通道。

#### 多代理：把请求分散到多个出口 IP

TinyPNG 的压缩接口是**按出口 IP 限流**的（本机实测单 IP 约 11.65 请求/秒，而每个文件要上传+下载两次请求，所以端到端上限约 5~6 文件/秒）。换密钥没有用——实测 4 个密钥并行只有 0.71 倍，限流认的是 IP 不是密钥。

如果同一台机器上有多个出口 IP 可用，用 `PROXY_LIST` 配进去即可：

```ini
PROXY_LIST=http://127.0.0.1:7891,http://127.0.0.1:7892,http://127.0.0.1:7893
```

工作方式：

- 每个工作线程**粘性绑定**其中一条代理，尽量一直走同一出口，从而保住该出口上的长连接复用（复用连接比重新握手快约 1.3 秒）；
- 某条代理连续失败 3 次会被**隔离 60 秒**，绑定它的线程自动改走其他代理；
- 命令行参数 `--proxy` 同样支持逗号分隔的多代理写法。

> ⚠️ 注意：代理软件的**一个混合端口只对应一个出口节点**，只填一个端口并不会带来多个出口 IP。要拿到多个出口 IP，需要让不同本地端口走不同节点。以 Clash/Mihomo 为例，可以加多个 `listeners` 再用 `IN-PORT` 规则分流：
>
> ```yaml
> listeners:
>   - { name: tp-a, type: mixed, port: 7891, listen: 127.0.0.1 }
>   - { name: tp-b, type: mixed, port: 7892, listen: 127.0.0.1 }
>   - { name: tp-c, type: mixed, port: 7893, listen: 127.0.0.1 }
>
> rules:
>   - IN-PORT,7891,节点A
>   - IN-PORT,7892,节点B
>   - IN-PORT,7893,节点C
> ```
>
> （`listeners` 本身没有"绑定出口节点"的字段，分流要靠规则实现。）
>
> 另外提醒一句：把并发拉满去冲击对方的限流并不划算——TinyPNG 一旦封掉你的出口 IP，换 IP 的成本比省下的时间高得多。多代理更适合用在"节点不稳、需要自动切换"的场景。

---

## 使用

### 图形界面（推荐）

不加任何参数运行（双击产物，或 `python main.py`）即打开图形界面：

```bash
python main.py          # 等价于 python main.py gui
```

三个页签：

| 页签 | 作用 |
| --- | --- |
| **压缩** | 添加图片/文件夹（可递归）、选择是否输出到 `_compressed` 子目录、开始/停止压缩；下方实时显示进度条、成功/失败/压缩率/速度统计与运行日志 |
| **设置** | 图形化编辑全部 `config.env` 配置项，点「保存配置」即刻生效，**无需手动创建 `.env`**。密钥类字段默认以掩码显示，可勾选「显示密钥明文」查看；另有「恢复默认值」一键填回出厂配置 |
| **密钥** | 查看可用/已用尽密钥列表，申请新密钥、按用量重新排序、手动添加；双击某条可复制 |

#### 常用操作

| 操作 | 方式 |
| --- | --- |
| 添加任务 | **把图片或文件夹拖进窗口**；或点「添加图片 / 添加文件夹」 |
| 开始 / 停止 | 「开始压缩」/「停止」，或 `F5` / `Esc` |
| 只改列表 | `Delete` 移除选中项、`Ctrl+A` 全选；右键列表可打开所在位置、移除、清空 |
| 查看某个文件 | 双击列表项，在文件管理器里定位它 |
| 快捷键一览 | `F1`，或菜单「帮助 → 快捷键」 |

#### 几点说明

- 添加任务后，列表标题右侧会**先算出「多少张图片、约多大」**，开始前就知道这次要处理多少内容；
- 跑完时进度区会出现一行结论（成功/失败/省下多少），旁边直接给「打开输出目录」的入口；
- **「压缩率」是「压掉了多少」**：显示 70% 表示体积降到原来的 30%，与「省下空间」是同一口径；
- 设置写回 `config.env` 时会**保留文件里的注释与条目顺序**，所以那份文件依然可读、可手改；
- 「停止」只会取消**尚未开始**的任务；正在压缩中的图片会先跑完（这是为了保证不留下半截文件），因此停止不是瞬时的；
- 界面统计与磁盘实际改动是一致的——停止时已经压好的部分会如实计入；
- 窗口大小/位置和上次勾选的选项记在可执行文件旁边的 `gui_state.json` 里，删掉它就恢复默认。

#### 压缩前的额度预检

点「开始压缩」后，程序会先在后台**算一遍额度够不够**，再决定要不要真的开跑
（压到一半才发现额度耗尽是最糟的体验，所以提前算清楚）：

1. 按 **文件数 × 1.1 向上取整** 估算本次所需额度（预留 10% 余量）；
2. **逐条查询**每个可用密钥的剩余次数：发 `POST https://api.tinify.com/shrink` **不带 body**，
   只读响应头里的 `compression-count`——**不消耗配额**；
3. 剩余总额度低于所需时弹窗，三选一：

   | 选项 | 行为 |
   | --- | --- |
   | **去注册新密钥** | 跳到「密钥」页的手动注册向导 |
   | **仍要继续** | 按原计划开跑（可能中途因额度耗尽而失败） |
   | **取消** | 不开跑，回到压缩页待命 |

> 离线或接口异常导致**查不出来**时不会拦你，按原计划直接开跑。

> 界面用 Python 标准库的 Tkinter 实现。唯一的额外依赖是 `tkinterdnd2`（拖拽支持），
> 它**缺失时界面会自动降级**为「只用按钮添加」，其余功能不受影响。
> 极少数精简版 Python 发行版不带 Tk，此时用下方命令行模式即可。

### 命令行

所有命令均从项目根目录执行。把 `python main.py` 换成产物文件名（如 `./TinyPNG-Unlimited-linux-x64`）即可用预编译产物执行同样操作。

### 申请 API 密钥

> ⚠️ **命令行 `apply` 已失效，不要再用了。**
> TinyPNG 在 2026-09 改版了注册链路，用真账号完整实测确认：
> 旧的**自动注册接口**与**取 Token 接口**现在都返回 **404，已死**；
> `https://api.tinify.com` 又**不吃网页登录 cookie**——即使账号里已经有 key，
> 请求也一律 401 `{"error":"unauthorized","message":"Access token is invalid"}`；
> 而控制台 `https://tinify.com/dashboard/api` 是**纯客户端渲染**（登录前后 HTML 字节数一样），key 不在 HTML 里。
> 结论：**程序无法自动读取 API Key，必须人在网页上复制**，旧的「自动申请密钥」路径整体失效。

现在请在**图形界面的「密钥」页**操作：点「**手动注册（过验证码）**」，跟着向导走完即可
（完整流程见下方「工作原理」里的「手动注册」流程图）。

如果你**已经从网页上复制到了 key**，直接用「其他命令」里的 `add_key` 粘进去：

```bash
python main.py add_key "your_api_key"
```

### 压缩单个文件

```bash
python main.py file "path/to/image.png"
python main.py file "path/to/image.jpg" -p http://127.0.0.1:7890
```

### 压缩文件夹

```bash
python main.py dir -d "path/to/images"          # 只压缩当前目录
python main.py dir -d "path/to/images" -r        # 递归压缩所有子目录
python main.py dir -d "path/to/images" -r -l     # 递归压缩并输出 log.json
python main.py dir                               # 不传 -d 则运行时交互输入路径
```

### 批量任务（JSON 配置）

```bash
python main.py tasks "path/to/tasks.json"
python main.py tasks "path/to/tasks.json" -r -l
```

`tasks.json` 格式：

```json
{
    "file_tasks": ["D:\\img1.jpg", "D:\\img2.png"],
    "dir_tasks":  ["D:\\folder1",  "D:\\folder2"]
}
```

### 其他命令

```bash
python main.py rearrange            # 按剩余配额重新排列密钥顺序
python main.py add_key "your_key"   # 手动添加一个 API 密钥
python main.py --help               # 查看全部命令帮助
python main.py dir --help           # 查看子命令帮助
```

---

## 目录结构

```text
TinyPNG-Unlimited/
├── main.py                    # 源码入口（转发到 bin/main.py）
├── bin/
│   └── main.py                # CLI/GUI 分发 + argparse 子命令
├── tinypng_unlimited/
│   ├── __init__.py            # 日志初始化、导出 TinyImg / KeyManager
│   ├── version.py             # 版本号唯一来源
│   ├── config.py              # 配置读写（env 文件 + 环境变量）
│   ├── gui.py                 # 图形界面（Tkinter；拖拽依赖 tkinterdnd2，可选）
│   ├── errors.py              # 异常类定义
│   ├── apihz_mail.py          # 接口盒子临时邮箱客户端
│   ├── key_manager.py         # 密钥生命周期管理
│   └── tiny_img.py            # 压缩引擎（tinify 封装 + 线程池）
├── config.env                 # 本地配置（不提交，首次运行自动生成）
├── config.env.template        # 配置模板
├── keys.json                  # 密钥存储（不提交，自动生成）
├── gui_state.json             # 界面偏好：窗口位置 + 上次勾选的选项（不提交，自动生成）
└── requirements.txt
```

> 运行时产生的文件（`config.env`、`keys.json`、`error_files.json`、`tmp/`、`log.json`、`gui_state.json`）
> 统一放在**工作目录**：源码运行时是项目根目录，打包后是**可执行文件所在目录**。
> 这些文件都已在 `.gitignore` 里排除——`keys.json` 含真实 API Key，千万不要提交。

---

## 工作原理

```text
启动（不带参数）
 └─ 打开图形界面（Windows 下按 windowed 子系统运行，不弹控制台窗口）
      ├─ 「设置」页读写 config.env（保留注释与原顺序）
      └─ 「压缩」页收集任务 → 起工作线程 → 回调驱动进度/统计/日志

启动（带参数 = 命令行）
 └─ argparse 分发到 dir / file / tasks / apply / rearrange / add_key / gui

首次运行
 └─ Config.load() → 工作目录没有 config.env 时从模板复制一份

压缩（dir / file / tasks / GUI）
 └─ KeyManager.init()
      ├─ 从 config.env 或 keys.json 加载密钥
      └─ 可用密钥 < KEY_THRESHOLD → 仅告警提示（自动申请已失效，不会真的去申请）
 └─ ThreadPoolExecutor（THREAD_NUM 个工作线程）
      └─ 每个文件：
           ├─ 检查末尾 4 字节是否为 b'tiny'（已压缩则跳过）
           ├─ 加锁检查配额 → 配额 ≥ KEY_USAGE_LIMIT 时切换密钥
           ├─ 上传 → api.tinify.com/shrink（带进度条）
           ├─ 云端无收益时跳过下载（避免白跑一次往返）
           ├─ 下载压缩后图片（带进度条，先写唯一命名的 .part 再替换）
           └─ 追加 b'tiny' 标记 → 覆盖原文件
```

### 手动注册（GUI 密钥页 → 「手动注册（过验证码）」）

```text
手动注册（GUI 密钥页 → 「手动注册（过验证码）」）
 ├─ 1. ApihzMail.create_new_mail()   →  程序建临时邮箱，把地址交给你
 ├─ 2. 打开 https://tinify.com/developers
 │       └─ 用页面正文那个注册表单（Name / Email Address / Create API key）提交
 │          注意：同页另有一个 login-card_form 是登录表单，不是注册
 ├─ 3. 你填邮箱、过验证码、提交
 ├─ 4. 点「我已提交，等激活邮件」
 │       ├─ ApihzMail.get_email_list()  →  轮询收件箱（6s 间隔）
 │       ├─ 提取激活链接（形如 https://tinypng.com/login?token=...）
 │       ├─ 程序访问激活链接完成登录（拿到 sess / sess.sig cookie）
 │       └─ 同时在你的浏览器里打开该链接，让你的浏览器也进入登录态
 ├─ 5. 点「打开开发者页」→ Create API key → 再过一次验证码
 ├─ 6. 点「打开控制台」→ https://tinify.com/dashboard/api 复制 key
 └─ 7. 粘回程序 → get_api_count()（POST /shrink 不带 body，不消耗配额）验证后保存
```

> **第 6-7 步的人工复制无法省略。** 控制台 `https://tinify.com/dashboard/api` 是**纯客户端渲染**的，
> key 不在 HTML 里；而 `api.tinify.com` 又不认网页登录 cookie（一律 401）。
> 这两条路都堵死了，程序拿不到 key，**只能人在页面上复制**。
