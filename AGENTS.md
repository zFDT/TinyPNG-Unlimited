## 环境变量配置规范
- **配置分离**：敏感信息（TinyPNG API Key、代理）和可变参数（超时、线程数）必须通过 `.env` 文件管理。
- **模板文件**：提供 `config.env.template` 作为配置示例，并在 `.gitignore` 中排除实际配置文件（如 `config.env`）。
- **加载机制**：使用 `python-dotenv` 在应用启动时加载环境变量，并提供默认值以防缺失。
- **优先级**：环境变量 > 默认配置 > 硬编码值。
- **Git 提交规范**：提交代码时需确保 `.env` 类文件未被纳入版本控制，仅提交模板文件和代码逻辑变更。
- **邮箱服务**：临时邮箱已从 SnapMail 换成**接口盒子 apihz.cn**（`tinypng_unlimited/apihz_mail.py`），
  需要 `APIHZ_ID` / `APIHZ_KEY` 两个凭据，普通会员限速 6s/次。仓库内若还有文档提到 SnapMail 属于历史遗留。
- **版本与发版**：版本号唯一来源是 `tinypng_unlimited/version.py`。发版流程为
  改版本号 → 提交 → 推同名 `v*` tag；`.github/workflows/release.yml` 会自动构建
  Windows / macOS(arm64+Intel) / Linux 三平台产物并创建 Release。
  PyInstaller 不支持交叉编译，不要试图在本机产出其他平台的二进制。
- **打包隔离**：`.spec` 只打包 `config.env.template` 与 `icon.ico`，**不得**打入 `config.env` / `keys.json`（含密钥）。
- **敏感文件必须全局忽略**：运行期文件位置统一由 `config.get_app_dir()` 决定（源码运行时是仓库根目录，
  打包后是 exe 同目录）。因此 `.gitignore` 必须按**文件名**忽略 `keys.json`、`error_files.json`、
  `old_error_files.json`、`log.json`、`debug_email.html`，而不是只忽略 `bin/` 下的那一份——
  历史上只挡了 `bin/`，工作目录改到根目录后这些文件就会裸露在仓库里，
  一次 `git add -A` 就可能把真实 API Key 推上公开仓库。
- **配置的读写入口唯一**：所有配置项的读取/写回都走 `tinypng_unlimited/config.py`。
  图形界面只是它的一个调用方，不允许在 `gui.py` 里自己解析或拼接 `config.env`
  （写回必须用 `save_env_values()`，它保留文件里的注释与顺序）。

## 图形界面（GUI）规范
- **依赖克制**：界面用 Python 标准库 Tkinter 实现，**必需依赖为零**。不要为了「更好看」引入
  customtkinter / PySide6，除非先确认接受产物体积增加（Tk 约占 +4.7MB，Qt 是 80MB 量级）。
  唯一允许的**可选**依赖是 `tkinterdnd2`（拖拽，≈2.8MB），且必须满足：
  用 `try/except` 导入、暴露 `gui.HAS_DND` 标记、缺少时优雅降级（而不是启动即崩），
  `.spec` 里用 `collect_data_files('tkinterdnd2')` 收原生库并写进 `hiddenimports`。
- **不复制引擎逻辑**：GUI 只做「收集参数 → 起工作线程 → 回调回报进度」，压缩一律调用
  `TinyImg.compress_from_file_list()`。CLI 与 GUI 共用同一套引擎，不得出现第二份实现。
- **界面上的「压缩率」是省下的比例**：引擎返回的是 `输出/输入`，GUI 必须换算成
  `(原大小-新大小)/原大小` 再显示，否则用户看到的数字与直觉相反（好结果会显示 29% 而非 70%）。
- **适配高 DPI**：字号会跟随系统缩放而控件内边距不会，所有 pad/width 必须走 `px()` 换算，
  否则在 150% 缩放下字大框小、排版发挤。
- **运行期状态不得入库**：`gui_state.json`（窗口几何 + 上次开关）与 `config.env`、`keys.json`
  同级别，必须进 `.gitignore`，且**不得**写进 `.spec` 的 `datas`。
- **线程安全**：所有网络/压缩在工作线程里跑，界面更新只能通过队列回主线程，
  禁止在工作线程里直接操作控件。
- **必须支持免手写配置**：用户不应被要求自己创建或编辑 `.env`。首次运行自动生成 `config.env`，
  设置页可直接编辑全部配置项并保存生效。
- **打包时 `tkinter` 不能出现在 `.spec` 的 `excludes` 里**，且延迟导入的
  `tinypng_unlimited.gui` 必须写进 `hiddenimports`，否则产物启动即报 `ModuleNotFoundError`。
