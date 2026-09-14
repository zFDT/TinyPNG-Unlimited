import json
import os
import re
import shutil
import sys
import time

import requests
from loguru import logger
from requests import Timeout

from tinypng_unlimited.config import Config
from tinypng_unlimited.errors import TempMailException, ApplyKeyException
from tinypng_unlimited.apihz_mail import ApihzMail


def _brief(text, limit: int = 300) -> str:
    """
    把上游响应裁短后再塞进异常消息。

    必要性：TinyPNG 出错时返回的是一整页 Next.js HTML（约 115KB），
    原样带进 exception 会淹没日志、也会把界面上的报错框撑爆。
    """
    if not text:
        return ''
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit] + ' …(已截断)'


class ManualSignup:
    """
    一次「人工过验证码」注册的上下文。

    begin 拿到邮箱 → 人在浏览器里填表、过验证码、提交 → finish 收邮件换 key。
    之所以要个句柄：requests.Session 必须前后是同一个（激活链路靠 cookie），
    不能让 finish 自己再建一个。
    """

    #: 注册入口（2026-09 起，旧的 tinypng.com/web/api 已 404）
    #:
    #: 用 /developers 而不是 /signup：实测抓过 https://tinify.com/developers 的 HTML，
    #: 页面里有两个表单 —— 页头的 login-card_form 是**登录**表单（只有 name="mail"
    #: 和「Send link」，旁边另行挂了 <a href="/signup">Sign Up</a>）；
    #: 而正文那张 `<form noValidate="">` 才是真正的注册表单：
    #:   <label>Name</label>        <input name="name">
    #:   <label>Email Address</label> <input name="email">
    #:   <button type="submit">Create API key</button>
    #: 同页 FAQ 也写明「sign up for the developer API here by entering your name and
    #: email address … activation email … The API key will then be available on your
    #: API dashboard」。所以这一个页面既能注册、又带着 Create API key 入口，少跳一次。
    #: 注意：注册后是否自动发 key 无法验证（要过验证码），所以流程仍然假定要人来复制。
    SIGNUP_URL = 'https://tinify.com/developers'
    #: 备用注册页（/developers 上的表单万一被改掉时，人工还能走这里）
    SIGNUP_URL_ALT = 'https://tinify.com/signup'
    #: 控制台：key 列表页
    DASHBOARD_URL = 'https://tinify.com/dashboard/api'
    #: 新账号没有 key 时，控制台的按钮会把人送到这里过验证码申请
    DEVELOPERS_URL = 'https://tinify.com/developers'

    def __init__(self, session, mail: str):
        self.session = session
        self.mail = mail
        #: 最近一次成功拿到的激活链接。记下来是为了让界面拿它去**人的浏览器**里再开一次：
        #: 程序是用自己的 requests.Session（内存 cookie jar）完成登录的，
        #: webbrowser.open() 打开的系统默认浏览器没有这个登录态，必须单独补一次。
        #: 这里不碰 webbrowser —— 开浏览器属于界面行为，留在 gui 层。
        self.activation_url: str = ''

    def activate(self, timeout: float = 180.0) -> None:
        """
        第 2 段：收激活邮件 → 点链接 → 建立登录态。**不生成 key**。

        拆开的原因：激活和「账号里有没有 key」是两件事。新账号激活完 key 列表
        是空的，还得到开发者页再过一次验证码才申请得到。合并成一步的话，
        空列表会被当成失败，人就没法继续了。
        """
        url = KeyManager._fetch_activation_url(self.session, timeout=timeout)
        KeyManager._activate(self.session, url)
        self.activation_url = url

    def fetch_keys(self) -> list:
        """
        第 3 段：列出当前账号已有的 API Key。

        :return: key 列表（dict），**可能为空**——空表示还得去开发者页申请
        """
        return KeyManager._list_keys(self.session)


class KeyManager:
    working_dir: str

    #: 早期版本把 keys.json 放在 bin/ 下（CLI 用 sys.argv[0] 推断工作目录）。
    #: 现在统一走 config.get_app_dir()，这个目录名只用于一次性搬迁老文件。
    LEGACY_KEYS_DIR = 'bin'

    #: TinyPNG 的 API Key 是 32 位左右的字母数字串。用它挡掉明显被写坏的条目
    #: （典型来源：表单把列表型默认值 [] 渲染成字符串存进 config.env，
    #: 读回来就成了一条假密钥 "[]"）。不过滤的话会白跑一轮「逐条联网验证」，
    #: 最后只报一句含糊的「所有密钥均无效」，很难定位。
    KEY_PATTERN = re.compile(r'^[A-Za-z0-9_\-]{20,}$')

    class Keys:
        available: list
        unavailable: list

        @classmethod
        def load(cls, obj: dict):
            cls.available = obj['available'] if 'available' in obj else []
            cls.unavailable = obj['unavailable'] if 'unavailable' in obj else []

    @classmethod
    def init_working_dir(cls, working_dir: str = None) -> str:
        """
        只设定密钥文件所在目录，不做别的。

        和 init() 的区别：init() 在可用密钥少于阈值时会**直接联网申请**新密钥。
        界面上的「刷新密钥列表」只是看一眼当前状态，不该因为一次点击就跑起来
        一串带 12 秒间隔的网络请求，所以单独留了这个轻量入口。

        :param working_dir: 目录；None 表示用 get_app_dir()（exe 同目录 / 项目根目录）
        :return: 实际使用的目录
        """
        if working_dir is None:
            from tinypng_unlimited.config import get_app_dir
            working_dir = get_app_dir()
        cls.working_dir = working_dir
        cls._migrate_legacy_keys()
        return working_dir

    @classmethod
    def _migrate_legacy_keys(cls):
        """
        一次性把老位置的 keys.json 搬到新的工作目录。

        为什么需要：早期 CLI 用 sys.argv[0] 推断目录，源码运行时 keys.json 落在 bin/。
        现在统一到 get_app_dir()（项目根目录 / exe 同目录），如果不搬，
        老用户从源码运行时会「密钥凭空消失」，进而触发一轮没必要的联网申请。

        只在源码运行时搬（打包后 __file__ 在临时解包目录里，推出来的老路径没有意义），
        且只在「新位置还没有 keys.json」时搬——绝不覆盖任何已有文件。
        """
        if getattr(sys, 'frozen', False):
            return
        target = os.path.join(cls.working_dir, 'keys.json')
        if os.path.exists(target):
            return
        legacy_dir = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), os.pardir, cls.LEGACY_KEYS_DIR))
        if os.path.normcase(legacy_dir) == os.path.normcase(os.path.abspath(cls.working_dir)):
            return
        legacy = os.path.join(legacy_dir, 'keys.json')
        if not os.path.exists(legacy):
            return
        try:
            shutil.copyfile(legacy, target)
            logger.info('已把历史密钥文件迁移到新位置（原文件保留）: {}', target)
        except OSError as e:
            logger.warning('迁移历史密钥文件失败: {}', e)

    @classmethod
    def init(cls, working_dir):
        """
        密钥初始化，请在所有需要密钥的操作之前执行
        """
        cls.init_working_dir(working_dir)
        
        # 首先从环境变量加载 API Keys（如果配置了）
        if Config.TINYPNG_API_KEYS:
            logger.info('从环境变量加载 TinyPNG API Keys，共 {} 个', len(Config.TINYPNG_API_KEYS))
            # 合并而不是覆盖。旧实现是直接拿环境变量整体覆盖 keys.json 并立即落盘，
            # 后果是：只要 TINYPNG_API_KEYS 写错一个值，本地已申请到的密钥就会被
            # 静默抹掉（实测把列表型默认值 [] 误写成 "[]" 时，keys.json 直接被
            # 覆盖成 {"available": ["[]"]}，3 条可用密钥全丢）。
            cls.load_keys()
            env_keys = [key for key in Config.TINYPNG_API_KEYS
                        if cls.KEY_PATTERN.match(str(key))]
            invalid = len(Config.TINYPNG_API_KEYS) - len(env_keys)
            if invalid:
                logger.warning('环境变量 TINYPNG_API_KEYS 中有 {} 条格式非法，已忽略', invalid)
            # 环境变量里的密钥排到最前面（表示优先使用），本地已有的保留在后
            rest = [key for key in cls.Keys.available if key not in env_keys]
            cls.Keys.load({'available': env_keys + rest,
                           'unavailable': list(cls.Keys.unavailable)})
            cls.store_key()
        else:
            # 否则从本地文件加载
            cls.load_keys()
        
        # 检查密钥数量，不足时提示（但**不再尝试自动申请**）
        if len(cls.Keys.available) < Config.KEY_THRESHOLD:
            # 2026-09 起 TinyPNG 注册加了验证码，自动申请这条路已经彻底走不通：
            # 旧注册接口 404、/web/session 的 Bearer Token 404、
            # api.tinify.com 又不认网页 cookie（一律 401）。
            #
            # 这里如果还调 apply_store_key()，只会白跑 4 轮网络请求
            # 外加每轮 12s 的等待，最后打一句没用的失败日志——
            # 而且 init() 是**启动路径**上调的（GUI 的 _init_engine），
            # 表现为「每次启动都先卡一分钟」。所以只提示，不尝试。
            logger.warning(
                '当前可用密钥 {} 条，少于阈值 {} 条。'
                '注意：TinyPNG 注册已加验证码，自动申请不可用，'
                '请到「密钥」页用「手动注册（过验证码）」补密钥，'
                '或直接粘贴已有密钥。',
                len(cls.Keys.available), Config.KEY_THRESHOLD)

    @classmethod
    def load_keys(cls):
        """从工作目录下的 keys.json 加载密钥，文件不存在时初始化为空列表。"""
        path = os.path.abspath(os.path.join(cls.working_dir, 'keys.json'))
        if not os.path.exists(path):
            cls.Keys.load({})
        else:
            with open(path, 'r', encoding='utf-8') as f:
                cls.Keys.load(json.load(f))
        cls._drop_malformed_keys()
        logger.debug('加载密钥完成：可用 {} 条，不可用 {} 条',
                     len(cls.Keys.available), len(cls.Keys.unavailable))

    @classmethod
    def _drop_malformed_keys(cls):
        """剔除格式明显不对的密钥记录，避免它们污染「逐条验证」的流程。"""
        dropped = 0
        for name in ('available', 'unavailable'):
            keys = getattr(cls.Keys, name) or []
            kept = [k for k in keys if cls.KEY_PATTERN.match(str(k))]
            dropped += len(keys) - len(kept)
            setattr(cls.Keys, name, kept)
        if dropped:
            logger.warning('已丢弃 {} 条格式非法的密钥记录（不是有效的 TinyPNG Key）', dropped)
        return dropped

    @classmethod
    def store_key(cls):
        """
        密钥保存到本地。

        先写临时文件再 os.replace 原子替换：旧实现直接 open(path, 'w')，
        会先把 keys.json 截断成 0 字节再写内容，这中间一旦进程被中断
        （Ctrl+C、被杀、断电），密钥文件就彻底空了，而这是**无法自行恢复**的数据。
        """
        path = os.path.abspath(os.path.join(cls.working_dir, 'keys.json'))
        payload = json.dumps({
            "available": cls.Keys.available,
            "unavailable": cls.Keys.unavailable
        }, ensure_ascii=False, indent=4, separators=(',', ':'))

        tmp_path = f'{path}.{os.getpid()}.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except OSError:
            # 临时文件写失败时兜底直接写目标路径，保证功能不因此不可用
            with open(path, 'w', encoding='utf-8') as f:
                f.write(payload)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    @staticmethod
    def get_api_count(s, key):
        url = 'https://api.tinify.com/shrink'
        retry = 0
        logger.info('正在获取密钥可用性信息... : {}', key)
        while True:
            try:
                res = s.post(url, auth=('api', key))
                return int(res.headers.get('compression-count'))
            except Exception as e:
                retry += 1
                if retry > 3:  # 最多再重试3次（总共4次）
                    raise e
                time.sleep(1)

    @classmethod
    def rearrange_keys(cls):
        path = os.path.abspath(os.path.join(cls.working_dir, 'keys.json'))
        if not os.path.exists(path):
            keys = {"available": [], "unavailable": []}
        else:
            with open(path, 'r', encoding='utf-8') as f:
                keys = json.load(f)
        out = {"available": [], "unavailable": []}

        with requests.Session() as s:
            for type_name in ('available', 'unavailable'):
                for index, key in enumerate(keys[type_name]):
                    count = cls.get_api_count(s, key)
                    out['available' if count < 490 else 'unavailable'].append((keys[type_name][index], count))

        for type_name in ('available', 'unavailable'):
            out[type_name].sort(key=lambda item: item[1], reverse=True)
            logger.info('{}：{}', type_name, json.dumps(out[type_name], indent=2))
            out[type_name] = [x[0] for x in out[type_name]]

        cls.Keys.load(out)
        cls.store_key()
        logger.success('密钥已按统计信息重新排列')

    @classmethod
    def estimate_quota(cls):
        """
        逐条查可用密钥的剩余额度，用于「压缩前预检」。

        为什么值得花这几次请求：压到一半才发现额度耗尽是最糟的体验
        （任务已经跑了一半、界面还在跳进度，突然弹个窗）。提前算清楚，
        让人在开跑之前决定是去补密钥还是照常继续。

        查询本身不消耗配额：get_api_count 是 POST /shrink 不带 body，
        只读响应头里的 compression-count。

        :return: (剩余总次数, [(key, 已用次数), ...], 查询失败的条数)
        """
        limit = Config.KEY_USAGE_LIMIT
        rows, failed = [], 0
        with requests.Session() as s:
            proxy = Config.get_proxy()
            if proxy:
                s.proxies = {'http': proxy, 'https': proxy}
            for key in list(cls.Keys.available):
                try:
                    rows.append((key, int(cls.get_api_count(s, key))))
                except Exception as e:
                    failed += 1
                    logger.debug('额度查询失败 {}: {}', str(key)[:6] + '…', e)
        remain = sum(max(0, limit - used) for _, used in rows)
        return remain, rows, failed

    @classmethod
    def next_key(cls) -> str:
        """
        删除当前密钥并返回下一条
        """
        cls.load_keys()

        # 密钥不足时**只告警，不尝试申请**。
        #
        # next_key() 是**压缩线程**上调的（某条密钥配额用尽时切换下一条）。
        # 以前这里会调 apply_store_key()，而自动申请链路 2026-09 起已全线 404，
        # 结果就是压缩线程先白跑 4 轮网络请求、再每轮干等一通，卡住好几分钟后
        # 照样失败。阈值也统一用 Config.KEY_THRESHOLD，跟 init() 保持一致。
        if len(cls.Keys.available) < Config.KEY_THRESHOLD:
            logger.warning(
                '可用密钥仅剩 {} 条，少于阈值 {} 条。'
                '自动申请不可用（TinyPNG 注册已加验证码），'
                '请到「密钥」页用「手动注册（过验证码）」补密钥，'
                '或直接粘贴已有密钥。',
                len(cls.Keys.available), Config.KEY_THRESHOLD)

        if not len(cls.Keys.available):
            raise Exception(
                '无可用密钥。请通过 add_key 手动添加，'
                '或到图形界面「密钥」页用「手动注册（过验证码）」获取新密钥')
        cls.Keys.unavailable.append(cls.Keys.available.pop(0))
        cls.store_key()
        logger.debug('密钥已切换，等待载入')
        return cls.Keys.available[0]

    @classmethod
    def add_key(cls, key: str):
        """
        手动添加 TinyPNG API 密钥
        :param key: TinyPNG API 密钥字符串
        """
        cls.load_keys()
        if key not in cls.Keys.available and key not in cls.Keys.unavailable:
            cls.Keys.available.append(key)
            cls.store_key()
            logger.success('密钥已手动添加: {}', key[:8] + '...')
        else:
            logger.warning('密钥已存在，跳过添加')

    # --------------------------------------------------------------
    # 手动注册（人工过验证码）
    #
    # 为什么需要这条路：2026-09 起 TinyPNG 把注册迁到 tinify.com/signup，
    # 新端点 /backend/web/signup/submit 要求 captcha_challenge —— 自动申请
    # 卡死在验证码上。验证码只能人来过，但「过完验证码之后」的收邮件、
    # 点激活链接、取 API Key 全都是纯 HTTP，程序自己能做完。
    # 于是拆成 begin / finish 两段，中间留给人在浏览器里操作。
    # --------------------------------------------------------------
    @classmethod
    def begin_manual_signup(cls):
        """
        第 1 段：建好临时邮箱，把地址交给人去浏览器里注册。

        :return: ManualSignup 句柄，务必把它交给 finish_manual_signup()
        """
        if not Config.APIHZ_ID or not Config.APIHZ_KEY:
            raise ApplyKeyException(
                '未配置 APIHZ_ID / APIHZ_KEY，无法创建临时邮箱。'
                '请到「设置」页填写接口盒子的凭据。', None)

        session = cls._new_signup_session()
        try:
            mail = ApihzMail.create_new_mail(session)
        except Exception as e:
            raise ApplyKeyException('创建临时邮箱失败', e)
        logger.info('临时邮箱已就绪: {}', mail)
        return ManualSignup(session, mail)

    @classmethod
    def _new_signup_session(cls):
        """申请密钥用的会话：TinyPNG 侧走代理（绕过 IP 频率限制）。"""
        session = requests.Session()
        proxy = Config.get_proxy()
        if proxy:
            session.proxies = {'http': proxy, 'https': proxy}
            logger.debug('申请密钥使用代理: {}', proxy)
        return session

    @classmethod
    def _fetch_activation_url(cls, session, timeout: float = 180.0):
        """
        轮询临时邮箱，把 TinyPNG 发来的激活链接抠出来。

        普通会员 6s/次，这里按间隔轮询；超时前一直等，因为人的手速没法预测。
        """
        import time as _t
        deadline = _t.time() + timeout
        wait = ApihzMail._min_interval
        _t.sleep(wait)                      # 先等一个间隔，邮件不会瞬间到
        last = None
        while _t.time() < deadline:
            try:
                emails = ApihzMail.get_email_list(session, 1)
                text = emails[0].get('text', '') if emails else ''
                if text:
                    last = text
                    url = cls._extract_activation_url(text)
                    if url:
                        logger.info('激活链接提取成功: {}...', url[:60])
                        return url
            except TempMailException:
                raise
            except Exception as e:
                logger.debug('读邮箱未成功，继续等: {}', e)
            _t.sleep(wait)
        raise ApplyKeyException(
            f'等了 {timeout:.0f}s 还没收到激活邮件。'
            '请确认在注册页填的邮箱就是这个地址、并且已经提交。', None)

    @staticmethod
    def _extract_activation_url(text: str):
        """从确认邮件正文里抠激活链接（HTML href 优先，纯文本兜底）。"""
        m = re.search(r'href=["\']?(https://(?:tinypng|tinify)\.com/login\?token=[^"\'>\s]+)',
                      text)
        if m:
            return m.group(1).replace('&amp;', '&')
        m = re.search(r'(https://(?:tinypng|tinify)\.com/login\?token=\S+)', text)
        if m:
            return m.group(1).rstrip('.,)>').replace('&amp;', '&')
        return None

    @classmethod
    def _activate(cls, session, url: str):
        """
        点激活链接，把网页登录态建起来。**不生成 key**。

        实测（2026-09 用真账号跑通一次）：
          - 激活链接形如
            https://tinypng.com/login?token=...&new=true&redirect=/dashboard/overview
          - 访问后拿到 sess / sess.sig 两个 cookie，网页侧就算登录了
          - 但 **api.tinify.com 不吃这套 cookie**：拿它去请求会 401
            「Access token is invalid」。网页登录态和 API 鉴权不是一套东西。
            这正是上一版「激活成功却 KeyError('keys')」的根因。
        """
        # 1. 访问激活链接（cookie 在这一步设上）
        session.get(url, timeout=30)

        # 2. 再进一次控制台，把 tinify.com 域上的会话坐实（顺带确认没被登出）
        try:
            session.get(ManualSignup.DASHBOARD_URL, timeout=30)
        except Exception as e:
            logger.debug('进入控制台页面未成功（不致命，继续）: {}', e)

    @classmethod
    def _list_keys(cls, session) -> list:
        """
        尝试列出账号里已有的 API Key。**实测大概率返回空列表**。

        2026-09 用真账号从头跑通一轮的结论：
          - 控制台「Add API key」按钮调的确实是 https://api.tinify.com/api/keys
          - 但网页侧的 sess / sess.sig cookie 换不来 api.tinify.com 的授权，
            即使账号里已经有 key、或者刚点完「Create API key」，请求一律是
            401「Access token is invalid」
          - 控制台页面是纯客户端渲染：登录前后 HTML 完全一样（都是 54077 字节），
            token 也不在 HTML 里；站内 /backend/ 下的候选接口全 404
        也就是说「程序自动读 key」在协议层就走不通，只能人在网页上复制。
        这里保留尝试，是为了上游哪天放开时能自动生效；失败一律兜成空列表，
        由界面引导人去控制台复制 —— 这条兜底路不会随上游改动而失效。
        """
        res = session.get('https://api.tinify.com/api', timeout=20)
        if res.status_code in (401, 403):
            logger.debug('自动列出 key 被拒（HTTP {}），转人工复制', res.status_code)
            return []
        res.raise_for_status()
        return (res.json() or {}).get('keys') or []
