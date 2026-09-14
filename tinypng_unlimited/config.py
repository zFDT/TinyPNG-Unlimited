"""
配置管理模块
支持从环境变量和 .env 文件加载配置
"""
import os
import re
import shutil
import sys
from dotenv import load_dotenv
from typing import Optional, List


def get_app_dir() -> str:
    """
    返回「用户可见的工作目录」——放 config.env 的地方。

    - 源码运行：项目根目录
    - PyInstaller 打包后：**exe 所在目录**

    这里不能用 __file__ 推导：onefile 模式下 __file__ 指向运行时的临时解包目录
    （sys._MEIPASS），于是放在 exe 旁边的 config.env 永远读不到，
    而打包进产物的 config.env.template 又藏在临时目录里、用户改不到。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def safe_print(text: str) -> None:
    """
    往 stdout 打一行字，但**绝不让这一行把进程带崩**。

    为什么不能直接用 print：Windows 上 stdout 不是控制台（被 runner 接成管道、
    或被 ``>`` 重定向）时，sys.stdout 用的是系统 ANSI 代码页（英文机器 cp1252，
    中文机器 cp936）。中文在这种流上是打不出来的，print 会抛 UnicodeEncodeError。
    而本模块在**导入期**就会走一次 Config.load() 并打印首次生成 config.env 的提示，
    于是 ``python bin/main.py --version`` 这种纯 ASCII 命令也会崩在 import 阶段。

    降级策略：
      1. 先按原样 print（正常路径一个字节都不变）；
      2. 编码失败就把打不出的字符换成 ``\\uXXXX`` 再打一遍；
      3. 还失败（或 sys.stdout 是 None，windowed 产物常见）就彻底放弃。

    :param text: 要打印的一行文本
    """
    stream = sys.stdout
    if stream is None:
        return                      # windowed 产物：print 到 None 本来也是空操作
    try:
        print(text, file=stream)
        return
    except UnicodeEncodeError:
        pass
    except Exception:
        return
    try:
        encoding = getattr(stream, 'encoding', None) or 'utf-8'
        fallback = text.encode(encoding, 'backslashreplace').decode(encoding, 'replace')
        print(fallback, file=stream)
    except Exception:
        return


def ensure_config_file() -> Optional[str]:
    """
    确保工作目录下存在 config.env，不存在则从模板复制一份。

    这样打包后的产物开箱即可用：用户直接在 exe 旁边编辑 config.env，
    不需要自己去「生成 .env」。
    :return: 本次新建的 config.env 路径；无需创建或创建失败时返回 None
    """
    target = os.path.join(get_app_dir(), 'config.env')
    if os.path.exists(target):
        return None
    # 打包后模板在解包目录里；源码运行时模板就在工作目录
    for src in (os.path.join(getattr(sys, '_MEIPASS', '') or '', 'config.env.template'),
                os.path.join(get_app_dir(), 'config.env.template')):
        if src and os.path.exists(src):
            try:
                shutil.copyfile(src, target)
                return target
            except OSError:  # 例如 exe 被放在 Program Files 等不可写目录
                return None
    return None


def load_config(env_file: str = None, override: bool = False):
    """
    加载配置文件
    :param env_file: 环境变量文件路径，默认为工作目录下的 config.env
    :param override: 是否用文件里的值覆盖进程环境中已有的同名变量。
                     python-dotenv 默认不覆盖，这在「同一进程里改完配置要立刻生效」
                     （GUI 的设置页）场景下会读到旧值，所以那里必须传 True；
                     命令行默认 False，保持「真实环境变量 > config.env」的既有优先级。
    """
    if env_file is None:
        env_file = os.path.join(get_app_dir(), 'config.env')

    # 加载 .env 文件（如果存在）
    if os.path.exists(env_file):
        load_dotenv(env_file, override=override)


#: 本程序会写入 config.env 的键。GUI 保存设置后按这个清单把进程环境里的旧值清掉，
#: 否则「把某一项清空」这个操作在 override=True 下依然会读到上一次的残留值。
MANAGED_ENV_KEYS = (
    'TINYPNG_API_KEYS', 'APIHZ_ID', 'APIHZ_KEY',
    'HTTP_PROXY', 'HTTPS_PROXY', 'PROXY_LIST',
    'LOG_LEVEL', 'OUTPUT_COMPRESSION_LOG',
    'TEMP_DIR', 'KEYS_FILE', 'ERROR_FILES',
    'MAX_RETRY', 'UPLOAD_TIMEOUT', 'DOWNLOAD_TIMEOUT', 'THREAD_NUM',
    'KEY_THRESHOLD', 'KEY_USAGE_LIMIT',
)


def _flatten_value(value) -> str:
    """
    把界面传来的值整理成能安全写进 .env 的单行字符串。

    PROXY_LIST 在界面上是多行输入框，而 .env 的 `KEY=value` 语法下一行就是一项，
    换行会直接把文件写坏（dotenv 解析不出后面几行）。配置项本身按逗号/分号也等价，
    所以这里统一把换行折成逗号。
    """
    if value is None:
        return ''
    text = str(value).replace('\r\n', '\n').replace('\r', '\n').replace('\n', ',').strip()
    return text


def save_env_values(values: dict) -> str:
    """
    把若干配置项写回 config.env，**保留文件里的注释、空行和条目顺序**。

    做法是逐行扫描，只替换 `KEY=` 左边的键能对上的那一行的右值；
    文件里没有的键追加到末尾。这样用户在界面上改配置后，
    config.env 依然是一份带说明、可以手改的文件，而不是被机器重写成光秃秃的键值对。

    :param values: {键: 值} 字典
    :return: 实际写入的文件路径
    """
    ensure_config_file()
    path = os.path.join(get_app_dir(), 'config.env')
    pending = {k: _flatten_value(v) for k, v in values.items() if k in MANAGED_ENV_KEYS}

    lines = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()

    out = []
    pattern = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
    for line in lines:
        m = pattern.match(line)
        if m and m.group(1) in pending:
            key = m.group(1)
            out.append(f'{key}={pending.pop(key)}')
        else:
            out.append(line)

    # 文件里还没有的键（例如从旧版本升级上来）补到末尾
    if pending:
        if out and out[-1].strip():
            out.append('')
        out.append('# ===== 由设置界面补充的配置项 =====')
        for key in sorted(pending):
            out.append(f'{key}={pending[key]}')

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    return path


def reload_config(env_file: str = None):
    """
    重新从磁盘读取配置并在当前进程内生效（GUI 保存设置后调用）。
    """
    for key in MANAGED_ENV_KEYS:
        os.environ.pop(key, None)
    Config.load(env_file, override=True)



def get_env_str(key: str, default: str = None) -> Optional[str]:
    """
    获取字符串类型的环境变量
    :param key: 环境变量名
    :param default: 默认值
    :return: 环境变量值
    """
    return os.getenv(key, default)


def get_env_int(key: str, default: int = None) -> Optional[int]:
    """
    获取整数类型的环境变量
    :param key: 环境变量名
    :param default: 默认值
    :return: 环境变量值
    """
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def get_env_bool(key: str, default: bool = False) -> bool:
    """
    获取布尔类型的环境变量
    :param key: 环境变量名
    :param default: 默认值
    :return: 环境变量值
    """
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in ('true', '1', 'yes', 'on')


def get_env_list(key: str, default: List[str] = None, separator: str = ',') -> List[str]:
    """
    获取列表类型的环境变量（用分隔符分割）
    :param key: 环境变量名
    :param default: 默认值
    :param separator: 分隔符，默认为逗号
    :return: 环境变量值列表
    """
    if default is None:
        default = []
    
    value = os.getenv(key)
    if value is None or value.strip() == '':
        return default
    
    return [item.strip() for item in value.split(separator) if item.strip()]


# ============================================
# 配置项定义
# ============================================

class Config:
    """全局配置类"""
    
    # TinyPNG API Keys
    TINYPNG_API_KEYS: List[str] = []
    
    # 代理设置
    HTTP_PROXY: Optional[str] = None
    HTTPS_PROXY: Optional[str] = None
    # 多代理列表（原始字符串，分隔符见 Config.get_proxy_list）
    PROXY_LIST: str = ''
    
    # 日志配置
    LOG_LEVEL: str = 'INFO'
    OUTPUT_COMPRESSION_LOG: bool = False
    
    # 路径配置
    TEMP_DIR: Optional[str] = None
    KEYS_FILE: Optional[str] = None
    ERROR_FILES: Optional[str] = None
    
    # 性能配置
    MAX_RETRY: int = 3
    UPLOAD_TIMEOUT: int = 60
    DOWNLOAD_TIMEOUT: int = 30
    THREAD_NUM: int = 4
    
    # 密钥管理配置
    KEY_THRESHOLD: int = 3
    KEY_USAGE_LIMIT: int = 490

    # 接口盒子（apihz.cn）临时邮箱凭据（「手动注册」时建临时邮箱用；自动申请已停用）
    APIHZ_ID: str = ''
    APIHZ_KEY: str = ''
    
    @classmethod
    def load(cls, env_file: str = None, override: bool = False):
        """
        加载所有配置
        :param env_file: 环境变量文件路径
        :param override: 是否用 config.env 覆盖进程环境中已有的同名变量，见 load_config
        """
        # 打包产物 / 全新克隆首次运行时，把模板复制成 config.env，
        # 用户直接编辑即可，不必自己「生成 .env」
        created = ensure_config_file()
        if created:
            safe_print(f'[提示] 已根据模板生成配置文件，请按需修改后重新运行: {created}')

        load_config(env_file, override=override)
        
        # TinyPNG API Keys
        cls.TINYPNG_API_KEYS = get_env_list('TINYPNG_API_KEYS', [])
        
        # 代理设置
        cls.HTTP_PROXY = get_env_str('HTTP_PROXY')
        cls.HTTPS_PROXY = get_env_str('HTTPS_PROXY')
        cls.PROXY_LIST = get_env_str('PROXY_LIST', '')
        
        # 日志配置
        cls.LOG_LEVEL = get_env_str('LOG_LEVEL', 'INFO')
        cls.OUTPUT_COMPRESSION_LOG = get_env_bool('OUTPUT_COMPRESSION_LOG', False)
        
        # 路径配置
        cls.TEMP_DIR = get_env_str('TEMP_DIR')
        cls.KEYS_FILE = get_env_str('KEYS_FILE')
        cls.ERROR_FILES = get_env_str('ERROR_FILES')
        
        # 性能配置
        cls.MAX_RETRY = get_env_int('MAX_RETRY', 3)
        cls.UPLOAD_TIMEOUT = get_env_int('UPLOAD_TIMEOUT', 60)
        cls.DOWNLOAD_TIMEOUT = get_env_int('DOWNLOAD_TIMEOUT', 30)
        cls.THREAD_NUM = get_env_int('THREAD_NUM', 4)
        
        # 密钥管理配置
        cls.KEY_THRESHOLD = get_env_int('KEY_THRESHOLD', 3)
        cls.KEY_USAGE_LIMIT = get_env_int('KEY_USAGE_LIMIT', 490)

        # 接口盒子（apihz.cn）临时邮箱凭据
        cls.APIHZ_ID = get_env_str('APIHZ_ID', '')
        cls.APIHZ_KEY = get_env_str('APIHZ_KEY', '')
    
    @classmethod
    def get_proxy(cls) -> Optional[str]:
        """
        获取单条代理设置（优先使用 HTTPS_PROXY，其次 HTTP_PROXY）
        :return: 代理地址
        """
        return cls.HTTPS_PROXY or cls.HTTP_PROXY

    @classmethod
    def get_proxy_list(cls) -> List[str]:
        """
        获取代理列表，用于把上传/下载分散到多个出口 IP。

        PROXY_LIST 支持逗号、分号、换行、空格混合分隔，例如：
            PROXY_LIST=http://127.0.0.1:7891,http://127.0.0.1:7892
            PROXY_LIST=socks5://127.0.0.1:7891; http://127.0.0.1:7892

        未配置 PROXY_LIST 时，回退为单条代理（HTTPS_PROXY > HTTP_PROXY），
        以保持与旧配置的兼容。
        :return: 代理地址列表；空列表表示直连
        """
        raw = (cls.PROXY_LIST or '').replace('\r', '\n')
        for sep in (';', '\n', '\t', ' '):
            raw = raw.replace(sep, ',')
        items = [item.strip() for item in raw.split(',') if item.strip()]
        if items:
            return items
        single = cls.get_proxy()
        return [single] if single else []


#: 出厂默认值快照。必须在 Config.load() **之前**抓：load() 会用 config.env 里的值
#: 覆盖这些类属性，覆盖之后界面上的「恢复默认值」就没有地方可取原值了。
#: 这样取值不需要在任何地方手抄第二份默认值，源码里的类属性就是唯一来源。
DEFAULTS = {key: (list(value) if isinstance(value, list) else value)
            for key, value in vars(Config).items()
            if not key.startswith('_') and not callable(value) and not isinstance(value, classmethod)}

# 自动加载配置（在模块导入时执行）
Config.load()
