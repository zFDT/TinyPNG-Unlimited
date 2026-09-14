# TinyPNG-Unlimited

> 自动申请 API 密钥、多线程、带进度条的 TinyPNG 批量云压缩命令行工具
>
> 本项目仅供技术研究使用，请勿用于任何商业及非法用途，任何后果作者概不负责。

---

## 功能特性

1. 通过[接口盒子](https://www.apihz.cn)临时邮箱**自动申请 TinyPNG API 密钥**，实现无限制压缩
2. 可用密钥接近 500 次限额时**自动切换**到下一条密钥
3. 多线程并发上传/下载，**加速批量压缩**（线程数可配置）
4. 上传、下载、整体任务均有**进度条**
5. 已压缩的文件写入标记字节，**重复运行自动跳过**
6. 支持**递归子文件夹**及**正则匹配**文件名
7. 支持通过 JSON 配置文件**批量提交任务**
8. 支持配置**代理**，也支持**多代理列表**把上传/下载分散到多个出口 IP
9. 上传/下载带**超时保护**，失败自动重试，超限保存错误列表供下次继续
10. 按并发规模**自动放大 HTTP 连接池**，并发超过 10 时不会退化成每次重新握手

---

## 安装

### 方式一：源码运行

```bash
# 克隆仓库
git clone https://github.com/zFDT/TinyPNG-Unlimited.git
cd TinyPNG-Unlimited

# 安装依赖（需 Python 3.10+）
pip install -r requirements.txt
```

### 方式二：下载编译好的 Windows exe

前往 [Releases](https://github.com/zFDT/TinyPNG-Unlimited/releases/latest) 下载 `TinyPNG-Unlimited.exe`，无需安装 Python 环境，开箱即用。

---

## 配置

复制配置模板并填写参数：

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
| `THREAD_NUM` | `4` | 并发压缩线程数 |
| `KEY_THRESHOLD` | `3` | 可用密钥少于此数量时自动申请新密钥 |
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

所有命令均从项目根目录执行：

### 申请 API 密钥

首次使用前，或密钥不足时，手动申请：

```bash
python main.py apply        # 申请 4 个
python main.py apply 2      # 申请指定数量
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
├── main.py                    # 根目录入口（推荐使用）
├── bin/
│   ├── main.py                # 实际 CLI 逻辑（argparse）
│   ├── keys.json              # 密钥存储（自动生成）
│   └── debug_email.html       # 申请密钥时收到的确认邮件（用于调试）
├── tinypng_unlimited/
│   ├── __init__.py            # 日志初始化、导出 TinyImg / KeyManager
│   ├── config.py              # 配置加载（env 文件 + 环境变量）
│   ├── errors.py              # 异常类定义
│   ├── apihz_mail.py          # 接口盒子临时邮箱客户端
│   ├── key_manager.py         # 密钥生命周期管理
│   └── tiny_img.py            # 压缩引擎（tinify 封装 + 线程池）
├── config.env                 # 本地配置（不提交，需自行创建）
├── config.env.template        # 配置模板
└── requirements.txt
```

---

## 工作原理

```text
启动
 └─ KeyManager.init()
      ├─ 从 config.env 或 keys.json 加载密钥
      └─ 可用密钥 < KEY_THRESHOLD → 自动触发 apply

apply（申请密钥）
 └─ 对每个新密钥：
      ├─ ApihzMail.create_new_mail()  →  创建临时邮箱（apihz.cn API）
      ├─ POST tinypng.com/web/api     →  用临时邮箱注册 TinyPNG 账号
      ├─ ApihzMail.get_email_list()   →  轮询收件箱（6s 间隔）
      ├─ 提取激活链接（href 正则 + HTML 实体解码）
      └─ 访问激活链接 → 获取 Bearer Token → 创建并读取 API Key

压缩（dir / file / tasks）
 └─ ThreadPoolExecutor（THREAD_NUM 个工作线程）
      └─ 每个文件：
           ├─ 检查末尾 4 字节是否为 b'tiny'（已压缩则跳过）
           ├─ 加锁检查配额 → 配额 ≥ KEY_USAGE_LIMIT 时切换密钥
           ├─ 上传 → api.tinify.com/shrink（带进度条）
           ├─ 下载压缩后图片（带进度条）
           └─ 追加 b'tiny' 标记 → 覆盖原文件
```
