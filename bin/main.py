import os
import sys

# ==========================================================
# 标准流兜底（windowed 产物专用）
#
# Windows 下 GUI 打包成「无控制台」子系统（见 .spec 的 console 决策），
# 代价是解释器启动时 sys.stdin/stdout/stderr 全是 None。于是：
#   - loguru / tqdm 一往 stdout 写就 AttributeError（import 阶段就可能炸）；
#   - CLI 从裸终端跑时也拿不到输出。
# 这里分三层兜底：
#   第 0   层（_install_null_streams）：把所有 None 流接到 devnull（保证「不崩」）；
#   第 0.5 层（_harden_stdio）：放宽流的错误处理器（保证「打印中文也不崩」，
#            见该函数注释——Windows 的 ANSI 代码页会把 import 阶段直接干掉）；
#   第 1   层（_ensure_cli_streams）：CLI 分支把流真正接回终端/管道（保证「能看见」）。
# 只用标准库，不新增依赖。
# ==========================================================

#: 启动时标准流是否为 None。windowed 产物为 True，源码/有控制台运行为 False。
_STREAMS_WERE_NULL: bool = False

#: 三个标准流的名字。兜底逻辑对它们一视同仁，统一在这里列一份，避免三处各写一遍。
_STD_NAMES: tuple = ('stdin', 'stdout', 'stderr')


def _install_null_streams() -> None:
    """
    第 0 层兜底：把值为 None 的标准流接到 os.devnull。

    必须在「导入 loguru / tinypng_unlimited」之前调用——tinypng_unlimited 的
    __init__ 会挂一个 loguru→tqdm.write 的 sink，若此时 stdout 还是 None，
    导入阶段就会 AttributeError。

    幂等：只动真正的 None，真实流一律不碰；整体失败静默（最坏退回原来的 None）。
    """
    global _STREAMS_WERE_NULL
    try:
        if sys.stdout is None or sys.stderr is None or sys.stdin is None:
            _STREAMS_WERE_NULL = True
    except Exception:
        return
    for name in _STD_NAMES:
        try:
            if getattr(sys, name, None) is None:
                setattr(sys, name, open(
                    os.devnull, 'r' if name == 'stdin' else 'w',
                    encoding='utf-8', errors='replace'))
        except Exception:
            # 单个流接不上不影响别的：最坏就是它仍是 None（print 到 None 是空操作）
            continue


def _harden_stdio() -> None:
    """
    第 0.5 层：把标准流的错误处理器放宽成 ``backslashreplace``。

    为什么必须有这一层（这是 Windows 独有的坑）：
      stdout 不是控制台时（CI 里被 runner 接成管道、或被 ``>`` 重定向），
      sys.stdout 用的是系统 ANSI 代码页——英文 runner 上是 cp1252，中文机器上是 cp936。
      此时打印中文会直接抛 UnicodeEncodeError。偏偏 ``tinypng_unlimited.config``
      在**导入期**就会 print 一条中文提示（首次运行生成 config.env 时），
      于是连 ``python bin/main.py --version`` 这种纯 ASCII 的命令都会崩在 import 阶段，
      退出码 1。macOS / Linux 的 locale 是 UTF-8，同样的代码只在 Windows 上炸。

    只放宽 ``errors``、**不动** ``encoding``：本来正常输出的字符一个字节都不会变，
    只有「原本会抛异常」的少数字符才退化成 ``\\uXXXX`` 转义，
    进程永远不会被一条提示语带崩。任何一步失败都静默跳过。
    """
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, 'reconfigure', None)
        if not callable(reconfigure):
            # 不是 TextIOWrapper（比如被 IDE / 测试框架换成了别的流），别去动它
            continue
        try:
            reconfigure(errors='backslashreplace')
        except Exception:
            continue


def _ensure_cli_streams() -> bool:
    """
    第 1 层：CLI 分支里把被 windowed 子系统剥掉的标准流接回终端 / 管道。

    :return: True 表示 sys.stdout 现在是一个可写的真实流。

    顺序至关重要（这是整个方案最容易踩的坑）：
      1. **先**复用「继承来的 OS 句柄」（GetStdHandle + msvcrt.open_osfhandle）。
         CI 的 `OUT=$(exe --version)` 就是把 stdout 接成一根管道再继承给子进程，
         此时句柄有效，直接拿来用。
      2. 第 1 步拿不到（从资源管理器双击、根本没有可继承的句柄）才去
         AttachConsole(父进程) 再开 CONOUT$ / CONIN$。
    若反过来先 AttachConsole，管道场景的输出会被导向控制台，CI 捕获到空字符串。

    仅 win32 且「启动时流曾为 None」才动作；非 win32 或流本来就好时立即返回，
    绝不改动。必须在 argparse.parse_args() 之前调用（--version 在解析阶段就 print 并退出）。
    """
    if sys.platform != 'win32':
        return sys.stdout is not None
    if not _STREAMS_WERE_NULL:
        return sys.stdout is not None

    # 先把原始流存下来：后面任何一步失败都原样退回去，绝不让 sys.std* 停在半吊子状态
    original = {name: getattr(sys, name, None) for name in _STD_NAMES}
    reopened = {}
    try:
        reopened = _reopen_std_streams()
    except Exception:
        reopened = {}

    # 只保留「探测过确实能用」的流，坏的一个都不装
    usable = {}
    for name, stream in reopened.items():
        try:
            if _stream_usable(name, stream):
                usable[name] = stream
        except Exception:
            continue
    for name, stream in reopened.items():
        if name not in usable:
            _close_quietly(stream)

    if not usable:
        _restore_streams(original)
        return sys.stdout is not None

    try:
        for name, stream in usable.items():
            setattr(sys, name, stream)
    except Exception:
        # 装到一半炸了：全部退回原来的流，也不能留下坏流
        _restore_streams(original)
        for stream in usable.values():
            _close_quietly(stream)
    return sys.stdout is not None


def _reopen_std_streams() -> dict:
    """
    真正去拿标准流，返回 {流名: 新流}；拿不到的一律不出现在结果里。

    :raises Exception: 只在 ctypes / msvcrt 都不可用时抛，由调用方兜住。
    """
    import ctypes
    import msvcrt

    kernel32 = ctypes.windll.kernel32
    # GetStdHandle 返回 HANDLE，64 位 Windows 上是 8 字节。不声明 restype 时 ctypes
    # 默认按 32 位 int 取值，句柄高位会被截断，后面 open_osfhandle 拿到的就是错句柄。
    kernel32.GetStdHandle.argtypes = (ctypes.c_uint32,)
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.AttachConsole.argtypes = (ctypes.c_uint32,)
    kernel32.AttachConsole.restype = ctypes.c_int
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.DuplicateHandle.argtypes = (
        ctypes.c_void_p,                  # hSourceProcessHandle
        ctypes.c_void_p,                  # hSourceHandle
        ctypes.c_void_p,                  # hTargetProcessHandle
        ctypes.POINTER(ctypes.c_void_p),  # lpTargetHandle
        ctypes.c_uint32,                  # dwDesiredAccess
        ctypes.c_int,                     # bInheritHandle
        ctypes.c_uint32,                  # dwOptions
    )
    kernel32.DuplicateHandle.restype = ctypes.c_int

    std_ids = (('stdin', -10, os.O_RDONLY), ('stdout', -11, os.O_WRONLY),
               ('stderr', -12, os.O_WRONLY))
    reopened = {}

    # --- 1. 先试继承来的句柄 ---
    for name, std_id, flags in std_ids:
        try:
            handle = kernel32.GetStdHandle(std_id)
        except Exception:
            continue
        # 返回 None（c_void_p 的空值）/ 0 / 0xFFFFFFFF 都是「无效句柄」的惯用表示
        if not handle or handle in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            continue
        try:
            # open_osfhandle 会**转移**句柄所有权：新 fd 关闭时会把传进去的那个句柄
            # 一起关掉。若直接拿 GetStdHandle 的结果，而这个句柄又正好被 CRT 的
            # fd 1/2 引用着（进程本身已有合法标准流时就会这样），关新流会把原 stdout
            # 的句柄也关掉 —— 之后任何 print 都炸，退出码还会变成 120（刷 stdout 失败）。
            # 所以先 DuplicateHandle 复制一份，只把副本的所有权交出去。
            dup = _dup_handle(kernel32, handle)
            if not dup:
                continue
            # 用 O_BINARY 避免 CRT 与 Python 文本层双重 CRLF 转换。
            fd = msvcrt.open_osfhandle(dup, flags | os.O_BINARY)
            reopened[name] = open(fd, 'r' if name == 'stdin' else 'w',
                                  encoding='utf-8', errors='replace')
        except Exception:
            continue

    # --- 2. 拿不到 stdout 才接回父控制台 ---
    if 'stdout' not in reopened:
        try:
            kernel32.AttachConsole(0xFFFFFFFF)  # ATTACH_PARENT_PROCESS == (DWORD)-1
        except Exception:
            pass
        for name, con in (('stdin', 'CONIN$'), ('stdout', 'CONOUT$'),
                          ('stderr', 'CONOUT$')):
            if name in reopened:
                continue
            try:
                reopened[name] = open(con, 'r' if name == 'stdin' else 'w',
                                      encoding='utf-8', errors='replace')
            except Exception:
                continue

    return reopened


def _dup_handle(kernel32, handle: int) -> int:
    """
    复制一个内核句柄，返回副本的句柄值；失败返回 0。

    见 _reopen_std_streams 里的说明：交给 msvcrt.open_osfhandle 的必须是副本，
    否则新流被关闭时会把原标准流的句柄一起关掉。
    """
    import ctypes
    try:
        dup = ctypes.c_void_p()
        ok = kernel32.DuplicateHandle(
            kernel32.GetCurrentProcess(), handle,
            kernel32.GetCurrentProcess(), ctypes.byref(dup),
            0,          # dwDesiredAccess：配合 DUPLICATE_SAME_ACCESS 忽略
            0,          # bInheritHandle
            0x00000002,  # DUPLICATE_SAME_ACCESS
        )
        if not ok or not dup.value:
            return 0
        return int(dup.value)
    except Exception:
        return 0


def _stream_usable(name: str, stream) -> bool:
    """
    探测一个新流是否真能用：先看 writable()/readable()，再对写流真的写一次空串并 flush。

    只建流不验证的话，无效句柄会一路装到 sys.stdout 上，等到真正 print 时才炸，
    那时候已经分不清是「流接错了」还是「业务代码写错了」。
    """
    try:
        if name == 'stdin':
            if not stream.readable():
                return False
            return True
        if not stream.writable():
            return False
        stream.write('')       # 空串不产生任何可见输出，但会走一遍编码/缓冲
        stream.flush()         # 句柄无效时 OSError 在这里就冒出来
        return True
    except Exception:
        return False


def _close_quietly(stream) -> None:
    """关掉一个不再使用的流，失败就算了——不能因为收尾失败把进程带崩。"""
    try:
        stream.close()
    except Exception:
        pass


def _restore_streams(original: dict) -> None:
    """把 sys.std* 退回给定的原始流（通常是 devnull），逐个失败互不影响。"""
    for name, stream in original.items():
        try:
            setattr(sys, name, stream)
        except Exception:
            continue


def _set_console_title(title: str) -> None:
    """
    设置控制台窗口标题（win32）。其它平台 no-op。

    用 SetConsoleTitleW 而非 os.system('title=...')：后者会额外拉起一个 cmd.exe，
    在无控制台/快速闪过的场景里会多闪一下窗口。
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleTitleW.argtypes = (ctypes.c_wchar_p,)
        kernel32.SetConsoleTitleW.restype = ctypes.c_int
        kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def _beep() -> None:
    """终端蜂鸣。用写 '\\a' 代替 os.system('echo \\7')，避免拉起 cmd 造成窗口闪烁。"""
    try:
        sys.stdout.write('\a')
        sys.stdout.flush()
    except Exception:
        pass


# 第 0 层：必须在导入第三方包之前执行
_install_null_streams()
# 第 0.5 层：放宽标准流的错误处理器，同样要赶在任何第三方导入之前
# （tinypng_unlimited.config 在导入期就会 print 中文提示）
_harden_stdio()

import argparse  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from shutil import rmtree  # noqa: E402

from loguru import logger  # noqa: E402
from tqdm import tqdm  # noqa: E402

# 添加包路径进入环境变量
cur_file_path = sys.argv[0]
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(cur_file_path), '..')))

from tinypng_unlimited import KeyManager, TinyImg, __version__
from tinypng_unlimited.config import Config, get_app_dir


def init(proxy=None):
    logger.info('TinyPng正在初始化')
    # 统一用 get_app_dir()：源码运行时是项目根目录，打包后是 exe 所在目录。
    # 旧实现用 sys.argv[0] 推断，源码运行时 keys.json / tmp 会落在 bin/ 下，
    # 与 config.env.template 里「默认: 项目目录下」的说明对不上，也和 GUI 不一致。
    app_dir = get_app_dir()
    KeyManager.init(app_dir)

    tmp_dir = os.path.join(app_dir, 'tmp')
    if os.path.exists(tmp_dir):
        rmtree(tmp_dir)  # 清空之前的临时下载文件夹

    if not len(KeyManager.Keys.available):
        logger.error('无可用密钥，请稍后重试')
        exit()

    # 依次尝试可用密钥，跳过无效密钥并移入 unavailable
    for key in list(KeyManager.Keys.available):
        try:
            TinyImg.set_key(key)
            break
        except Exception as e:
            logger.warning('密钥无效，已跳过: {}... ({})', key[:8], e)
            KeyManager.Keys.available.remove(key)
            KeyManager.Keys.unavailable.append(key)
            KeyManager.store_key()
    else:
        logger.error('所有密钥均无效，请通过 add_key 手动添加密钥，'
                     '或到图形界面「密钥」页用「手动注册（过验证码）」获取新密钥')
        exit()

    # 代理优先级：命令行 --proxy > config.env 的 PROXY_LIST / HTTPS_PROXY / HTTP_PROXY。
    # 旧实现只在传了 --proxy 时才设置代理，config.env 里配的代理只对「申请密钥」生效，
    # 压缩本身其实是直连的，这里一并修掉。
    if proxy is None:
        proxy = Config.get_proxy_list()
    if proxy:
        TinyImg.set_proxy(proxy)

    logger.success('TinyPng初始化成功')


def compress_error_files(file_list):
    times = 0
    logger.warning('存在压缩失败图片({}):\n{}', len(file_list), file_list)
    while times < 5:
        times += 1
        logger.info('1s后对上述文件列表内文件进行压缩(第{}次)', times)
        time.sleep(1)
        res = TinyImg.compress_from_file_list(file_list)
        tqdm.write('')
        logger.debug('压缩报告基本信息:\n{}', json.dumps(res['basic'], ensure_ascii=False, indent=2))
        # 压缩失败文件不考虑输出日志到文件
        if res['basic']['error_count'] > 0:  # 仍然存在压缩失败的文件
            file_list = res['error_files']
            logger.warning('存在压缩失败图片({}):\n{}', len(file_list), file_list)
        else:
            return

    file_path = os.path.abspath(os.path.join(get_app_dir(), 'error_files.json'))
    try:
        with open(file_path, encoding='utf-8') as f:
            old_error_files = json.load(f)
    except Exception:
        old_error_files = []
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(old_error_files + file_list, f, ensure_ascii=False, indent=2)
    logger.error('超过压缩失败重试次数{}, 压缩失败图片路径已保存', times)


def compress_cover(input_type: str, file_list: list = None, dir_path: str = None,
                   proxy: str = None, log: bool = False):
    if input_type == 'dir':
        if not len(dir_path):
            return False
    elif input_type == 'file_list':
        if not len(file_list):
            return False
    else:
        raise Exception('input_type must be "dir" or "file_list"')

    if proxy:
        logger.info('配置: 使用代理上传图片: {}', proxy)
    if log:
        logger.info('配置: 压缩完成后输出压缩日志文件')

    try:
        if input_type == 'dir':
            logger.info('1s后开始对文件夹内图片进行压缩: {}', dir_path)
            time.sleep(1)
            res = TinyImg.compress_from_dir(dir_path)
        else:
            logger.info('1s后开始对图片列表进行压缩: {}', file_list)
            time.sleep(1)
            res = TinyImg.compress_from_file_list(file_list)
        tqdm.write('')
        logger.debug('压缩报告基本信息:\n{}', json.dumps(res['basic'], ensure_ascii=False, indent=2))

        # 仅文件夹才输出日志
        if input_type == 'dir' and log:
            log_path = os.path.abspath(os.path.join(dir_path, 'log.json'))
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
            logger.success('压缩日志已输出: {}', log_path)

        if res['basic']['error_count'] > 0:  # 存在压缩失败的文件
            compress_error_files(res['error_files'])

    except Exception as e:
        logger.error(e)
    return True


def compress_cover_dir(dir_path: str, proxy: str = None, log: bool = False):
    """
    压缩文件夹内图片（覆盖）
    :param dir_path: 文件夹路径
    :param proxy: 代理地址
    :param log: 是否输出日志到该文件夹
    :return: bool 是否进行了压缩
    """
    return compress_cover(input_type='dir', dir_path=dir_path, proxy=proxy, log=log)


def compress_cover_file_list(file_list: list, proxy: str = None):
    """
    压缩文件列表内图片（覆盖）
    :param file_list: 文件路径列表
    :param proxy: 代理地址
    :return: bool 是否进行了压缩
    """
    return compress_cover(input_type='file_list', file_list=file_list, proxy=proxy)


def character_drawing():
    tqdm.write(r'''
  _______             ____  _   ________   __  __      ___           _ __           __
 /_  __(_)___  __  __/ __ \/ | / / ____/  / / / /___  / (_)___ ___  (_) /____  ____/ /
  / / / / __ \/ / / / /_/ /  |/ / / __   / / / / __ \/ / / __ `__ \/ / __/ _ \/ __  / 
 / / / / / / / /_/ / ____/ /|  / /_/ /  / /_/ / / / / / / / / / / / / /_/  __/ /_/ /  
/_/ /_/_/ /_/\__, /_/   /_/ |_/\____/   \____/_/ /_/_/_/_/ /_/ /_/_/\__/\___/\__,_/   
            /____/                                                                    
    ''')


def check_error_files(proxy=None):
    path = os.path.abspath(os.path.join(get_app_dir(), 'error_files.json'))
    old_path = os.path.join(os.path.dirname(path), 'old_error_files.json')
    try:
        with open(path, encoding='utf-8') as f:
            file_list = json.load(f)
    except:
        return

    if isinstance(file_list, list) and len(file_list) > 0:
        if len(input('检测到压缩失败图片路径列表，是否对该列表进行压缩？(输入任意内容则压缩)')):
            os.rename(path, old_path)
            compress_cover_file_list(file_list, proxy)
            logger.success('文件列表压缩完成')
            character_drawing()
            os.remove(old_path)
            _beep()  # 输出到终端时可以发出蜂鸣作为一种提醒


def command_dir(args):
    if args.dir is None:
        args.dir = input('输入图片文件夹路径(为空则结束程序):').strip('"')

    character_drawing()
    init(proxy=args.proxy)
    check_error_files(args.proxy)

    while compress_cover_dir(args.dir, args.proxy, args.log):
        tqdm.write('=' * 60)
        if args.recur:
            # 递归子文件夹
            for root, dirs, files in os.walk(args.dir):
                for dir_path in dirs:
                    dir_path = os.path.join(root, dir_path)
                    logger.info('正在递归子文件夹: {}', dir_path)
                    compress_cover_dir(dir_path, args.proxy, args.log)
                    tqdm.write('=' * 60)
        character_drawing()
        _beep()  # 输出到终端时可以发出蜂鸣作为一种提醒
        args.dir = input('输入下一个图片文件夹路径(为空则结束程序):').strip('"')


def command_file(args):
    character_drawing()
    init(proxy=args.proxy)
    check_error_files(args.proxy)

    compress_cover_file_list([args.file], args.proxy)
    logger.success('文件压缩完成')


def command_tasks(args):
    if os.path.exists(args.path):
        with open(args.path, encoding='utf-8') as f:
            tasks = json.load(f)
    else:
        logger.error('{} does not exist', args.path)
        return

    character_drawing()
    init(proxy=args.proxy)
    check_error_files(args.proxy)

    length = 1 if 'file_tasks' in tasks else 0
    length += len(tasks['dir_tasks']) if 'dir_tasks' in tasks else 0
    with tqdm(desc='[总体进度]', unit='任务', total=length, file=sys.stdout, ascii=' ▇',
              colour='magenta', leave=False, ncols=120, position=5) as bar:
        if 'file_tasks' in tasks:
            compress_cover_file_list(tasks['file_tasks'], args.proxy)
            logger.success('文件列表压缩完成')
            bar.update()
            character_drawing()
            _beep()  # 输出到终端时可以发出蜂鸣作为一种提醒
        if 'dir_tasks' in tasks:
            for dir_task in tasks['dir_tasks']:
                compress_cover_dir(dir_task, args.proxy, args.log)
                if args.recur:
                    # 递归子文件夹
                    for root, dirs, files in os.walk(dir_task):
                        for dir_path in dirs:
                            dir_path = os.path.join(root, dir_path)
                            logger.info('正在递归子文件夹: {}', dir_path)
                            compress_cover_dir(dir_path, args.proxy, args.log)
                logger.success('文件夹列表压缩完成')
                tqdm.write('=' * 60)
                bar.update()
            character_drawing()
        _beep()  # 输出到终端时可以发出蜂鸣作为一种提醒
    tqdm.write('')


def command_apply(args):
    """
    保留 `apply` 子命令只是为了不破坏已有脚本，但它已经申请不到任何密钥。

    2026-09 起 TinyPNG 注册加了验证码，自动申请链路整体失效：旧注册接口 404、
    取 Token 接口 404、api.tinify.com 又不认网页 cookie（一律 401）。
    所以这里不 raise、不发网络请求，只打印一段可读的中文引导，然后以退出码 1 结束。
    """
    print('[已停用] TinyPNG 注册已加验证码，自动申请密钥不可用。')
    print('')
    print('请改用下面两种方式之一获取密钥：')
    print('  1. 图形界面：python bin/main.py gui')
    print('     → 「密钥」页 →「手动注册（过验证码）」，跟着向导走完即可。')
    print('  2. 命令行：python bin/main.py add_key <your_api_key>')
    print('     把你已经在网页上复制到的密钥直接粘进来。')
    sys.exit(1)


def command_rearrange(args):
    KeyManager.init_working_dir(get_app_dir())
    KeyManager.rearrange_keys()


def command_add_key(args):
    KeyManager.init_working_dir(get_app_dir())
    KeyManager.load_keys()
    KeyManager.add_key(args.key)


def command_gui(args):
    """启动图形界面。tkinter 是延迟导入的，纯命令行用户不会被牵连。"""
    from tinypng_unlimited.gui import main as gui_main
    gui_main()


def command_selftest(args):
    """
    CI 用：确认产物里 GUI 的依赖确实可用，但**不创建窗口**（runner 是无声卡的）。

    为什么需要：如果打包机上没有 tkinter，PyInstaller 不会报错，只是安静地产出一个
    「没有界面」的二进制——本地看不出来，等用户双击了才发现。所以让 CI 在打包后
    真的去 import 一次，把这种静默失败变成明确的构建失败。
    """
    import tkinter                      # noqa: F401  必要：缺了就该让它抛
    from tinypng_unlimited import gui    # noqa: F401

    # 拖拽是可选能力：缺了不致命，只是窗口不能拖文件进来。把它打印出来，
    # 用户报「拖进去没反应」时，这一行就能直接区分是依赖缺失还是用法问题。
    dnd_ok = bool(getattr(gui, 'HAS_DND', False))
    dnd_note = '' if dnd_ok else '（缺少 tkinterdnd2，已回退为无拖拽模式）'

    print(f'selftest OK: tkinter {tkinter.TkVersion}, gui module importable, '
          f'GuiApp={"GuiApp" in dir(gui)}, drag_and_drop={dnd_ok}{dnd_note}')


def main():
    # 不带任何参数（也就是在资源管理器里双击 exe）时，直接进图形界面。
    # 命令行用法完全不变：加了参数就还是原来的 CLI。
    if not sys.argv[1:]:
        command_gui(None)
        return

    # 只有真正走命令行时才去动控制台标题，避免 GUI 模式下多余的窗口操作
    # 第 1 层：windowed 产物下把标准流接回终端/管道。
    # 必须早于 parse_args()——--version 在解析阶段就会 print 并退出。
    _ensure_cli_streams()
    _set_console_title('TinyPng无限制压缩图片')

    # 命令行参数解析
    parser = argparse.ArgumentParser(description='Tinify Your Images Unlimited! '
                                                 'All compressed images will cover themselves.')
    # 版本号来自 tinypng_unlimited/version.py（唯一来源）。
    # 用 argparse 的 version action，会在 parse_args 阶段直接退出，
    # 不会走到末尾的 input('回车退出')，因此可以在 CI 里做打包冒烟测试。
    parser.add_argument('-V', '--version', action='version',
                        version=f'TinyPNG-Unlimited {__version__}',
                        help='Show the version number and exit.')
    subparsers = parser.add_subparsers(metavar='<command>')
    # dir
    dir_parser = subparsers.add_parser('dir', help='Compress images from dir or input the path later')
    dir_parser.add_argument('-d', '--dir', type=str, help='The dir where your images are.')
    dir_parser.set_defaults(func=command_dir)
    # file
    file_parser = subparsers.add_parser('file', help='Compress image from file')
    file_parser.add_argument('file', type=str, help='The path where the image is.')
    file_parser.add_argument('-p', '--proxy', type=str, help='The proxy used on uploading images.')
    file_parser.set_defaults(func=command_file)
    # tasks
    tasks_parser = subparsers.add_parser('tasks', help='Compress images from tasks.json')
    tasks_parser.add_argument('path', type=str, help='The path where the tasks.json is.')
    tasks_parser.set_defaults(func=command_tasks)

    for p in dir_parser, tasks_parser:
        p.add_argument('-p', '--proxy', type=str, help='The proxy used on uploading images.')
        p.add_argument('-r', '--recur', action='store_true', help='Whether to recurse the dir.')
        p.add_argument('-l', '--log', action='store_true', help='Whether to output compression log in images dir.')

    # apply
    # 命令保留（不破坏已有脚本），但只打印引导并以退出码 1 结束，见 command_apply。
    apply_parser = subparsers.add_parser(
        'apply', help='已停用：TinyPNG 注册加验证码后无法自动申请密钥，'
                      '请改用 gui 的「手动注册」或 add_key。')
    apply_parser.add_argument('num', type=int, nargs='?', default=4,
                              help='已忽略，仅为兼容旧脚本保留。')
    apply_parser.set_defaults(func=command_apply)

    # rearrange
    rearrange_parser = subparsers.add_parser('rearrange',
                                         help='Rearrange API keys in keys.json by compression count.')
    rearrange_parser.set_defaults(func=command_rearrange)

    # add_key
    add_key_parser = subparsers.add_parser('add_key', help='Manually add a TinyPNG API key.')
    add_key_parser.add_argument('key', type=str, help='The TinyPNG API key to add.')
    add_key_parser.set_defaults(func=command_add_key)

    # gui
    gui_parser = subparsers.add_parser('gui', help='Launch the graphical interface.')
    # no_pause：GUI 是长效窗口，结束后不该再等一次「回车退出」
    gui_parser.set_defaults(func=command_gui, no_pause=True)

    # selftest
    # 不隐藏它：对用户同样有用——可以自检「这个产物到底有没有带图形界面」。
    # （实测 argparse 的 SUPPRESS 对子解析器无效，会渲染出一行 `xxx ==SUPPRESS==`。）
    selftest_parser = subparsers.add_parser(
        'selftest', help='Check that this build includes GUI support, then exit.')
    selftest_parser.set_defaults(func=command_selftest, no_pause=True)

    args = parser.parse_args()
    if 'func' not in args:
        parser.print_help()
        return
    args.func(args)
    if not getattr(args, 'no_pause', False):
        input('回车退出')


if __name__ == '__main__':
    main()
