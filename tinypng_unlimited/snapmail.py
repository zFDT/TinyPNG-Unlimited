import time
from random import sample
from loguru import logger
from requests import Session

from tinypng_unlimited.errors import SnapMailException


class SnapMail:
    BASE_URL = 'https://www.snapmail.cc/'
    mail: str = None

    @classmethod
    def create_new_mail(cls) -> str:
        cls.mail = ''.join(sample('zyxwvutsrqponmlkjihgfedcba', 16)) + '@snapmail.cc'
        return cls.mail

    @classmethod
    def get_email_list(cls, session: Session, count: int = None) -> list:
        """
        使用新的 POST API 获取邮件列表
        :param session: requests Session 对象
        :param count: 需要获取的邮件数量
        :return: 邮件列表
        """
        if cls.mail is None:
            cls.create_new_mail()

        retry = 0
        while True:
            try:
                # 使用新的 POST /emailList/filter API
                res = session.post(
                    cls.BASE_URL + 'emailList/filter',
                    json={'email': cls.mail}
                )
                
                if res.status_code != 200:
                    try:
                        err = res.json().get('error', '')
                        if err.find('Email was not found') > -1:
                            raise SnapMailException('邮箱内无任何邮件', err)
                        elif err.find('Please try again') > -1:
                            raise SnapMailException('邮箱请求过频繁', err)
                        # 其他错误
                        logger.error(err)
                    except SnapMailException as e:
                        # 明确错误
                        err = e
                        logger.error(err)
                    except Exception:
                        # 未知错误
                        err = res.text
                        logger.error('未知邮箱请求错误 {}', err)

                    retry += 1
                    if retry <= 3:
                        logger.info(f'等待10s后进行第{retry}次重试')
                        time.sleep(10)
                    else:
                        raise SnapMailException('超过重试次数', 3)
                else:
                    # 状态码200则返回
                    result = res.json()
                    # 如果指定了 count，只返回最近的 count 封邮件
                    if count and isinstance(result, list):
                        return result[:count]
                    return result
            except SnapMailException:
                raise
            except Exception as e:
                retry += 1
                if retry <= 3:
                    logger.error(f'请求异常: {e}')
                    logger.info(f'等待10s后进行第{retry}次重试')
                    time.sleep(10)
                else:
                    raise SnapMailException('超过重试次数', 3)
