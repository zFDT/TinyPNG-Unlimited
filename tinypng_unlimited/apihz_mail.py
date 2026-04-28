import time

import requests
from loguru import logger
from requests import Session

from tinypng_unlimited.errors import TempMailException


class ApihzMail:
    """
    接口盒子（apihz.cn）临时邮箱服务

    文档:
      创建邮箱: https://www.apihz.cn/api/mailmailcache.html
      读取邮件: https://www.apihz.cn/template/miuu/getpost.php

    普通会员限制: 10次/分钟，最小请求间隔 6 秒。
    """

    CREATE_URL = 'https://cn.apihz.cn/api/mail/mailcache.php'
    READ_URL = 'https://www.apihz.cn/template/miuu/getpost.php'

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
    def create_new_mail(cls, session: Session = None) -> str:
        """
        调用接口盒子创建临时邮箱，返回邮箱地址。
        mail / pwd 同时缓存到类属性供 get_email_list 使用。
        """
        from tinypng_unlimited.config import Config

        cls._ensure_rate_limit()

        s = session or requests.Session()
        res = s.get(cls.CREATE_URL, params={
            'id': Config.APIHZ_ID,
            'key': Config.APIHZ_KEY,
        }, timeout=15)
        res.raise_for_status()

        data = res.json()
        if data.get('code') != 200:
            raise TempMailException('创建临时邮箱失败', data.get('msg', data))

        cls.mail = data['mail']
        cls.pwd = data['pwd']
        logger.debug('临时邮箱已创建: {} (有效至 {})', cls.mail, data.get('endtime', '?'))
        return cls.mail

    @classmethod
    def get_email_list(cls, session: Session, count: int = None) -> list:
        """
        读取临时邮箱中的邮件列表。

        :param session: requests Session
        :param count:   最多返回的邮件数，None 表示全部
        :return:        邮件列表，每封邮件为 dict，保证包含 'text' 键（兼容旧调用方）
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
                res = session.post(cls.READ_URL, json={
                    'id': Config.APIHZ_ID,
                    'key': Config.APIHZ_KEY,
                    'mail': cls.mail,
                    'pwd': cls.pwd,
                }, timeout=15)
                res.raise_for_status()

                data = res.json()
                code = data.get('code')

                if code != 200:
                    msg = data.get('msg', str(data))
                    # 邮箱暂无邮件不算网络错误，可重试
                    raise TempMailException('获取邮件列表失败: ' + msg, data)

                emails = data.get('data') or data.get('list') or []
                if isinstance(emails, list) and len(emails) == 0:
                    raise TempMailException('邮箱内暂无邮件', data)

                # 统一字段名：保证 'text' 键存在（兼容 key_manager 的正则提取）
                normalized = []
                for mail in emails:
                    normalized.append({
                        **mail,
                        'text': mail.get('text') or mail.get('content') or mail.get('body') or '',
                    })

                return normalized[:count] if count else normalized

            except TempMailException as e:
                last_err = e
                retry += 1
                if retry <= 3:
                    logger.error('{}', e.msg)
                    logger.info('等待 {:.0f}s 后进行第 {} 次重试', cls._min_interval, retry)
                    # _ensure_rate_limit 已在循环顶部处理，此处只补齐剩余等待
                    time.sleep(max(0, cls._min_interval - (time.time() - cls._last_request_time)))
            except Exception as e:
                last_err = e
                retry += 1
                if retry <= 3:
                    logger.error('请求异常: {}', e)
                    logger.info('等待 {:.0f}s 后进行第 {} 次重试', cls._min_interval, retry)

        raise TempMailException('超过重试次数', last_err)
