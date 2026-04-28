import json
import os
import re
import time

import requests
from loguru import logger
from requests import Timeout

from tinypng_unlimited.config import Config
from tinypng_unlimited.errors import TempMailException, ApplyKeyException
from tinypng_unlimited.apihz_mail import ApihzMail


class KeyManager:
    working_dir: str

    class Keys:
        available: list
        unavailable: list

        @classmethod
        def load(cls, obj: dict):
            cls.available = obj['available'] if 'available' in obj else []
            cls.unavailable = obj['unavailable'] if 'unavailable' in obj else []

    @classmethod
    def init(cls, working_dir):
        """
        密钥初始化，请在所有需要密钥的操作之前执行
        """
        cls.working_dir = working_dir
        
        # 首先从环境变量加载 API Keys（如果配置了）
        if Config.TINYPNG_API_KEYS:
            logger.info('从环境变量加载 TinyPNG API Keys，共 {} 个', len(Config.TINYPNG_API_KEYS))
            cls.Keys.load({'available': Config.TINYPNG_API_KEYS.copy(), 'unavailable': []})
            cls.store_key()
        else:
            # 否则从本地文件加载
            cls.load_keys()
        
        # 检查密钥数量，如果少于阈值则尝试申请新密钥
        if len(cls.Keys.available) < Config.KEY_THRESHOLD:
            logger.warning('当前可用密钥少于{}条，尝试申请新密钥', Config.KEY_THRESHOLD)
            cls.apply_store_key()

    @classmethod
    def load_keys(cls):
        """从 bin/keys.json 加载密钥，文件不存在时初始化为空列表。"""
        path = os.path.abspath(os.path.join(cls.working_dir, 'keys.json'))
        if not os.path.exists(path):
            cls.Keys.load({})
        else:
            with open(path, 'r', encoding='utf-8') as f:
                cls.Keys.load(json.load(f))
        logger.debug('加载密钥完成：可用 {} 条，不可用 {} 条',
                     len(cls.Keys.available), len(cls.Keys.unavailable))

    @classmethod
    def store_key(cls):
        """
        密钥保存到本地
        """
        path = os.path.abspath(os.path.join(cls.working_dir, 'keys.json'))
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({
                "available": cls.Keys.available,
                "unavailable": cls.Keys.unavailable
            }, f, ensure_ascii=False, indent=4, separators=(',', ':'))

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
    def next_key(cls) -> str:
        """
        删除当前密钥并返回下一条
        """
        cls.load_keys()

        if len(cls.Keys.available) < 3:
            logger.warning('可用密钥少于3条，尝试申请新密钥')
            cls.apply_store_key()

        if not len(cls.Keys.available):
            raise Exception('无可用密钥，请申请后重试或通过 add_key 手动添加密钥')
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

    @classmethod
    def _apply_api_key(cls) -> str:
        """
        申请新密钥：
        1. 通过接口盒子创建临时邮箱
        2. 向 TinyPNG 注册该邮箱（触发确认邮件）
        3. 轮询临时邮箱，提取激活链接
        4. 访问激活链接，生成并获取新 API Key
        """
        if not Config.APIHZ_ID or not Config.APIHZ_KEY:
            raise ApplyKeyException('未配置 APIHZ_ID / APIHZ_KEY，无法自动申请密钥', None)

        with requests.Session() as session:
            # 仅对 tinypng.com 的请求使用代理（绕过 IP 频率限制）
            # apihz.cn 为国内服务，直连即可
            proxy = Config.get_proxy()
            if proxy:
                session.proxies = {
                    'http': proxy,
                    'https': proxy,
                }
                logger.debug('申请密钥使用代理: {}', proxy)

            # 创建临时邮箱（消耗 1 次 apihz 调用）
            try:
                mail = ApihzMail.create_new_mail(session)
            except Exception as e:
                raise ApplyKeyException('创建临时邮箱失败', e)

            # 向 TinyPNG 注册，触发确认邮件
            res = session.post('https://tinypng.com/web/api', json={
                "fullName": mail[:mail.find('@')],
                "mail": mail
            })
            if res.status_code == 429:
                raise ApplyKeyException('新账号注册过于频繁', res.text)
            if res.status_code != 200 or res.text != '{}':
                raise ApplyKeyException('新账号注册未知错误', res.text)
            logger.info('注册邮件已发送至: {}', mail)

            # 等待邮件到达后轮询（普通会员 6s 间隔，最多等待 ~60s）
            logger.info('等待确认邮件到达（最多重试 {}s）...', ApihzMail._min_interval * 4)
            time.sleep(ApihzMail._min_interval)  # 先等一个间隔再开始读

            # 接收邮件，提取激活链接
            try:
                emails = ApihzMail.get_email_list(session, 1)
                text = emails[0].get('text', '')

                # 始终保存完整邮件便于调试（覆盖写入，保留最新一封）
                debug_path = os.path.abspath(os.path.join(cls.working_dir, 'debug_email.html'))
                with open(debug_path, 'w', encoding='utf-8') as _f:
                    _f.write(text)
                logger.debug('邮件完整内容已保存: {}', debug_path)

                url = None
                # TinyPNG 确认邮件中 href 格式：
                # href="https://tinypng.com/login?token=...&amp;new=true&amp;redirect=..."
                m = re.search(r'href=["\']?(https://(?:tinypng|tinify)\.com/login\?token=[^"\'>\s]+)', text)
                if m:
                    url = m.group(1).replace('&amp;', '&')
                if not url:
                    # 回退：纯文本格式
                    m = re.search(r'(https://(?:tinypng|tinify)\.com/login\?token=\S+)', text)
                    if m:
                        url = m.group(1).rstrip('.,)>').replace('&amp;', '&')
                if not url:
                    raise ApplyKeyException(
                        '激活链接提取失败，完整邮件已保存至 ' + debug_path, None
                    )
            except TempMailException as e:
                raise ApplyKeyException('确认邮件接收失败', e)
            logger.info('激活链接提取成功: {}...', url[:60])

            # 访问激活链接，生成密钥
            retry = 0
            while True:
                try:
                    session.get(url)
                    auth = session.get('https://tinify.com/web/session').json()['token']
                    headers = {'authorization': f'Bearer {auth}'}
                    session.post('https://api.tinify.com/api/keys', headers=headers)
                    res = session.get('https://api.tinify.com/api', headers=headers)
                    key = res.json()['keys'][-1]['key']
                    break
                except Exception as e:
                    retry += 1
                    if retry <= 3:
                        logger.error('密钥生成失败，3s 后第 {} 次重试: {}', retry, e)
                        time.sleep(3)
                    else:
                        raise ApplyKeyException(f'超出重试次数，密钥生成失败: {url}', e)

            logger.success('新密钥生成成功')
            return key

    @classmethod
    def apply_store_key(cls, times=None):
        """
        申请并保存密钥。

        普通会员每分钟限 10 次 API 调用（6s 间隔），
        批量申请时每两次之间等待一个额外间隔，避免触发频率限制。
        """

        # 允许申请次数（包括失败重试）
        times = 4 - len(cls.Keys.available) if times is None else times

        for i in range(times):
            try:
                logger.info('正在申请新密钥，进度: {}/{}', i + 1, times)
                key = cls._apply_api_key()
                cls.Keys.available.append(key)
                cls.store_key()
                logger.success('密钥申请成功，当前可用密钥数: {}', len(cls.Keys.available))

                if i < times - 1:
                    from tinypng_unlimited.apihz_mail import ApihzMail
                    wait_time = ApihzMail._min_interval * 2  # 两个间隔，保守策略
                    logger.info('等待 {:.0f}s 后继续申请下一个密钥...', wait_time)
                    time.sleep(wait_time)

            except Timeout as e:
                logger.error("请求超时: {} - {}({})", e.request.method, e.request.url, bytes.decode(e.request.content))
                if i < times - 1:
                    time.sleep(15)
            except Exception as e:
                logger.error('自动申请密钥失败: {}', e)
                logger.warning('提示：您可以手动添加 TinyPNG API 密钥')
                logger.warning('使用方法：python main.py add_key <your_api_key>')
                break
