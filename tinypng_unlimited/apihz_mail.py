import secrets
import string
import time
import warnings

import requests
import urllib3
from loguru import logger
from requests import Session

from tinypng_unlimited.errors import TempMailException

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 通用请求配置（对应 getpost.php 教程模式）
_REQUEST_OPTIONS = {
    'timeout': (10, 30),    # (connect_timeout, read_timeout)
    'verify': False,         # 关闭 SSL 验证（与教程模式一致）
    'allow_redirects': True,
    'headers': {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/116.0.5845.97 Safari/537.36'
        ),
    },
}


class ApihzMail:
    """
    接口盒子（apihz.cn）临时邮箱服务

    文档:
      创建邮箱: https://www.apihz.cn/api/mailmailcache.html
      读取邮件: https://www.apihz.cn/api/mailmailgetlist.html
      请求模式: https://www.apihz.cn/template/miuu/getpost.php

    普通会员限制: 10次/分钟，最小请求间隔 6 秒。
    创建邮箱使用 GET，读取邮件使用 GET（均不需要 JSON body）。
    """

    CREATE_URL = 'https://cn.apihz.cn/api/mail/mailcache.php'
    READ_URL = 'https://cn.apihz.cn/api/mail/mailgetlist.php'

    # 当前邮箱凭据（每次 create_new_mail 后更新）
    mail: str = None
    pwd: str = None

    # 普通会员: 10次/分钟 → 6 秒最小间隔
    _min_interval: float = 6.0
    _last_request_time: float = 0.0

    @classmethod
    def _ensure_rate_limit(cls):
        elapsed = time.time() - cls._last_request_time
        if elapsed < cls._min_interval:
            wait = cls._min_interval - elapsed
            logger.debug('ApihzMail 频率限制：等待 {:.1f}s...', wait)
            time.sleep(wait)
        cls._last_request_time = time.time()

    @classmethod
    def _get(cls, session: Session, url: str, params: dict) -> dict:
        """统一 GET 入口，自动记录原始响应便于调试。"""
        res = session.get(
            url,
            params=params,
            headers=_REQUEST_OPTIONS['headers'],
            timeout=_REQUEST_OPTIONS['timeout'],
            verify=_REQUEST_OPTIONS['verify'],
            allow_redirects=_REQUEST_OPTIONS['allow_redirects'],
        )
        raw = res.text
        logger.debug('ApihzMail [{}] {}: {}', res.status_code, url, raw[:400])

        if not raw.strip():
            raise TempMailException(f'接口返回空响应 (HTTP {res.status_code})', url)

        try:
            return res.json()
        except Exception:
            raise TempMailException(f'响应非 JSON (HTTP {res.status_code}): {raw[:200]}', url)

    @classmethod
    def create_new_mail(cls, session: Session = None) -> str:
        """
        调用接口盒子创建临时邮箱，返回邮箱地址。
        mail / pwd 同时缓存到类属性供 get_email_list 使用。
        """
        from tinypng_unlimited.config import Config

        cls._ensure_rate_limit()

        s = session or requests.Session()
        # 主动传入随机密码，避免普通会员账号返回空 pwd 导致读邮件失败
        generated_pwd = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
        data = cls._get(s, cls.CREATE_URL, {
            'id': Config.APIHZ_ID,
            'key': Config.APIHZ_KEY,
            'pwd': generated_pwd,
        })

        if data.get('code') != 200:
            raise TempMailException('创建临时邮箱失败', data.get('msg', data))

        cls.mail = data['mail']
        cls.pwd = data.get('pwd') or generated_pwd  # 优先用接口返回值，空时回退到本地生成值
        logger.debug('临时邮箱已创建: {} (有效至 {})', cls.mail, data.get('endtime', '?'))
        return cls.mail

    @classmethod
    def get_email_list(cls, session: Session, count: int = None) -> list:
        """
        读取临时邮箱中的邮件列表（GET: id/key/mail/pwd/page）。

        接口返回字段: frommail, fromname, subject, time1, time2, content
        本方法会统一注入 'text' 键（= content），兼容 key_manager 的正则提取。

        :param session: requests Session
        :param count:   最多返回的邮件数，None 表示全部
        :raises TempMailException: 超出重试次数或邮箱无邮件
        """
        if cls.mail is None or cls.pwd is None:
            raise TempMailException('请先调用 create_new_mail() 创建邮箱', None)

        from tinypng_unlimited.config import Config

        retry = 0
        last_err = None
        while retry <= 3:
            cls._ensure_rate_limit()
            try:
                data = cls._get(session, cls.READ_URL, {
                    'id': Config.APIHZ_ID,
                    'key': Config.APIHZ_KEY,
                    'mail': cls.mail,
                    'pwd': cls.pwd,
                    'page': 1,
                })

                code = data.get('code')
                if code != 200:
                    raise TempMailException('获取邮件列表失败: ' + data.get('msg', str(data)), data)

                emails = data.get('data') or []
                if not isinstance(emails, list) or len(emails) == 0:
                    raise TempMailException('邮箱内暂无邮件', data)

                normalized = [{
                    **m,
                    'text': m.get('content') or m.get('text') or '',
                } for m in emails]

                return normalized[:count] if count else normalized

            except TempMailException as e:
                last_err = e
                retry += 1
                if retry <= 3:
                    logger.error('{}', e.msg)
                    logger.info('等待 {:.0f}s 后进行第 {} 次重试', cls._min_interval, retry)
            except Exception as e:
                last_err = e
                retry += 1
                if retry <= 3:
                    logger.error('请求异常: {}', e)
                    logger.info('等待 {:.0f}s 后进行第 {} 次重试', cls._min_interval, retry)

        raise TempMailException('超过重试次数', last_err)
