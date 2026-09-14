import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import count
from shutil import move
from threading import RLock, get_ident

import tinify
from loguru import logger
from requests import Session
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from tqdm.utils import CallbackIOWrapper

import tinypng_unlimited  # 不使用 from import 防止循环引用
from tinypng_unlimited.errors import CompressException


class TinyImg:
    _lock: RLock = RLock()
    _session: Session = Session()
    _pool_size: int = 0        # 已挂载的连接池大小，0 表示还是 requests 的默认值
    _tmp_seq = count()         # 临时文件唯一后缀，避免同名文件并发写入互相覆盖
    tmp_dir: str

    @classmethod
    def ensure_session_pool(cls, size: int = None) -> int:
        """
        按并发规模放大 HTTP 连接池。

        requests 默认挂载的 HTTPAdapter 是 pool_connections=10 / pool_maxsize=10。
        并发数一旦超过 10，urllib3 就会不断打印
        「Connection pool is full, discarding connection」并丢弃连接，
        导致每个请求都退回完整的 TCP + TLS 握手（实测单请求多出约 1.3s），
        于是出现「线程越多、吞吐越低」的反常现象。

        本程序里有两条独立的 HTTP 通道，两者都要放大：
          - 下载走 cls._session
          - 上传走 tinify 库自己的 client.session（tinify.key 每次变更都会重建
            Client 与 Session，所以每次都要按对象身份重新挂载）

        :param size: 池大小，默认按 THREAD_NUM 推算
        :return: 实际生效的池大小
        """
        from tinypng_unlimited.config import Config
        # 每个线程同一时刻至少占用 1 条连接，留 2 倍余量应对建连/回收的瞬时重叠
        size = size or max(16, (Config.THREAD_NUM or 4) * 2)

        def _mount(session):
            if session is None or getattr(session, '_tinypng_pool_size', None) == size:
                return
            adapter = HTTPAdapter(pool_connections=size, pool_maxsize=size, max_retries=0)
            session.mount('https://', adapter)
            session.mount('http://', adapter)
            session._tinypng_pool_size = size

        _mount(cls._session)
        try:
            _mount(tinify.get_client().session)
        except Exception:  # 尚未设置 key 时 get_client 会抛错，属正常情况
            pass

        if cls._pool_size != size:
            cls._pool_size = size
            logger.debug('HTTP 连接池已调整为: {}', size)
        return size

    @classmethod
    def set_key(cls, key):
        """
        设置新密钥并进行验证
        """

        with cls._lock:  # 加锁避免多个线程尝试切换密钥
            cls.tmp_dir = os.path.abspath(os.path.join(tinypng_unlimited.KeyManager.working_dir, 'tmp'))
            logger.debug('正在载入密钥: {}', key)
            tinify.key = key
            # 必须放在 tinify.key 之后：key 变更会重建 tinify 的 client 与 session
            cls.ensure_session_pool()
            tinify.validate()
            logger.success('密钥已载入，当前密钥可用性: [{}/500]', cls.compression_count())
            cls.check_compression_count()

    @classmethod
    def set_proxy(cls, proxy):
        tinify.proxy = proxy

    @classmethod
    def to_file_save(cls, path, url, timeout=30):
        """
        安全的下载文件并保存到指定路径
        :param path: 路径
        :param url: 图片下载链接
        :param timeout: 下载超时
        """
        file_name = os.path.basename(path)
        if not os.path.exists(cls.tmp_dir):
            os.makedirs(cls.tmp_dir, exist_ok=True)
        # 临时文件名必须全局唯一。同一目录树下存在同名文件（例如 images/common/icon_tip.png
        # 与 images/device/icon_tip.png 内容并不相同），旧实现用 round(time.time()) 当后缀，
        # 同一秒内被两个线程处理时会算出同一个临时路径并互相覆盖，最终把 A 的图片写进 B 的文件。
        # 并发越高、文件越多，撞上的概率越大，所以这是提高并发前必须先修的一处。
        token = f'{os.getpid()}_{get_ident()}_{next(cls._tmp_seq)}'
        tmp_path = os.path.abspath(os.path.join(cls.tmp_dir, f'{file_name}.{token}.part'))
        try:
            res = cls._session.get(url, stream=True, timeout=timeout)
            file_size = int(res.headers.get('content-length', 0))
            with tqdm(file=sys.stdout, desc=f'[下载进度]: {file_name}', colour='red', ncols=120, leave=False,
                      ascii=' ▇', total=file_size, unit="B", unit_scale=True, unit_divisor=1024) as bar:
                with open(tmp_path, 'wb') as f:
                    wrapped_file = CallbackIOWrapper(bar.update, f, 'write')
                    for data in res.iter_content(2048):
                        wrapped_file.write(data)
                    f.write(b'tiny')
                    logger.info('已为图片添加压缩标记tiny: {}', file_name)
            move(tmp_path, path)
        finally:
            # 下载/写入失败时清掉残留临时文件，避免 tmp 目录堆积
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @classmethod
    def compression_count(cls) -> int:
        """
        api调用次数
        """
        with cls._lock:
            if tinify.compression_count is None:
                tinify.validate()
            return tinify.compression_count

    @classmethod
    def check_compression_count(cls):
        """
        检测密钥是否限额，限额则替换为下一条
        """
        count = cls.compression_count()
        if count >= 490:  # 提前 10 次切换，为多线程并发留出余量
            logger.warning('当前密钥即将达到限额: [{}/500], 正在切换新密钥', count)
            cls.set_key(tinypng_unlimited.KeyManager.next_key())

    @classmethod
    def check_if_compressed(cls, path) -> bool:
        """
        检验图片是否被本程序标记为压缩
        """
        if os.path.getsize(path) < 4:  # 空文件/超短文件 seek(-4, 2) 会直接抛 OSError
            return False
        with open(path, 'rb') as f:
            f.seek(-4, 2)
            return f.read(4) == b'tiny'

    @classmethod
    def shrink_upload(cls, f, timeout=60) -> tuple:
        """
        上传图片交由云端压缩，返回 (下载链接, 响应元信息)。

        TinyPNG 的 /shrink 响应体是 JSON，包含 input.size 与 output.size，例如：
          {"input":{"size":71924,"type":"image/png"},
           "output":{"size":48952,"type":"image/png","ratio":0.6806,"url":"https://..."}}
        调用方据此可以判断本次压缩是否有收益，从而跳过纯属浪费的下载。

        :param f: 文件对象
        :param timeout: 服务器响应超时时间，注意此时间在每次服务器做出任何响应时重置，所以不是整个请求和响应的时间
        :return: (location, meta) —— meta 为解析后的 dict，解析失败时为空 dict
        """
        s: Session = tinify.get_client().session
        res = s.post('https://api.tinify.com/shrink', data=f, timeout=timeout)
        count = res.headers.get('compression-count')
        if count is not None:  # 4xx 等异常响应可能不带该头，避免 int(None) 直接抛 TypeError
            tinify.compression_count = int(count)
        try:
            meta = json.loads(res.text) if res.text else {}
        except ValueError:
            meta = {}
        return res.headers.get('location'), meta

    @classmethod
    def upload_from_file(cls, f, timeout=60) -> str:
        """
        重写库方法添加超时参数，上传图片，返回云端压缩后图片链接
        :param timeout: 服务器响应超时时间，注意此时间在每次服务器做出任何响应时重置，所以不是整个请求和响应的时间
        :param f: 文件对象
        """
        # 保留原签名以兼容外部调用；需要响应元信息请直接用 shrink_upload
        return cls.shrink_upload(f, timeout=timeout)[0]

    @classmethod
    def compress_from_file(cls, path, new_path, check_compressed=True,
                           upload_timeout=None, download_timeout=None) -> tuple:
        """
        压缩图片文件
        :param path: 文件路径
        :param new_path: 新文件路径
        :param check_compressed: 是否检查压缩标记
        :param upload_timeout: 上传响应超时时间，默认60s
        :param download_timeout: 下载响应超时时间，默认30s
        :return: (旧大小，新大小，压缩到原来的百分比)
        """
        old_size = os.path.getsize(path)
        file_name = os.path.basename(path)
        if check_compressed and cls.check_if_compressed(path):
            logger.info('图片已带有压缩标记，不做压缩处理: {}', file_name)
            time.sleep(0.5)  # 似乎返回值太快会对多线程任务造成影响
            return file_name, old_size, old_size, '100.0%'

        retry = 0
        while True:
            try:
                # 加锁保证同一时刻只有一个线程检查配额并可能切换密钥。
                # tinify 库内部共享单一 client，密钥切换会中断正在上传的其他线程请求，
                # 因此先获取当前密钥值，上传完成后对比是否已被其他线程切换（见下方刷新逻辑）。
                with cls._lock:
                    cls.check_compression_count()
                    old_key = tinify.key
                with tqdm(file=sys.stdout, desc=f'[上传进度]: {file_name}', colour='green', ncols=120, leave=False,
                          ascii=' ▇', total=old_size, unit="B", unit_scale=True, unit_divisor=1024) as bar:
                    logger.info('正在上传图片至云端压缩[{}]: {}', cls._byte_converter(old_size), file_name)
                    with open(path, "rb") as f:
                        wrapped_file = CallbackIOWrapper(bar.update, f, "read")
                        url, meta = cls.shrink_upload(wrapped_file, timeout=upload_timeout)
                    # 上传完成得到图片链接与响应元信息，并更新了api调用次数
                    with cls._lock:
                        # 若上传过程中其他线程已切换了密钥，旧密钥的响应头会覆盖新密钥的
                        # compression_count，需重新 validate 以获取正确计数。
                        if tinify.key != old_key:
                            tinify.validate()
                        logger.info('当前密钥可用性: [{}/500]', cls.compression_count())

                # 云端无收益时跳过下载：TinyPNG 对无法减小的图片会原样返回上传内容，
                # 此时下载再覆写不仅白费一次往返，还会把文件尾部已有的 b'tiny' 标记
                # 再追加一次（累积成 tinytiny）。直接保留原文件语义完全等价且更省。
                # 仅在原地覆盖（new_path 就是 path）时适用；输出到其他目录时必须落盘。
                out_size = (meta.get('output') or {}).get('size')
                in_size = (meta.get('input') or {}).get('size')
                if (out_size is not None and in_size is not None and out_size >= in_size
                        and os.path.abspath(new_path) == os.path.abspath(path)):
                    logger.info('云端无法进一步减小，跳过下载并保持原文件: {}', file_name)
                    return file_name, old_size, old_size, '100.0%'

                logger.success('云端压缩成功，正在下载: {}', file_name)
                cls.to_file_save(new_path, url, timeout=download_timeout)
                new_size = os.path.getsize(new_path)
                return file_name, old_size, new_size, f'{round(100 * new_size / old_size, 2)}%'
            except Exception as e:
                retry += 1
                if retry <= 3:
                    logger.warning('重试压缩图片(第{}次): {}, 错误信息: {}', retry, file_name, e)
                else:
                    raise CompressException('超出压缩重试次数', {'path': path, 'err': e})

    @classmethod
    def compress_from_file_list(cls, file_list, new_dir=None, upload_timeout=None, download_timeout=None) -> dict:
        """
        批量压缩多个文件
        :param file_list: 文件路径列表
        :param new_dir: 输出文件夹
        :param upload_timeout: 上传响应超时时间，默认60s
        :param download_timeout: 下载响应超时时间，默认30s
        :return: 压缩情况报告
        """

        if new_dir and not os.path.exists(new_dir):
            os.makedirs(new_dir)

        success_count = 0
        old_size = new_size = 0  # python不用担心大数运算溢出问题
        error_files, success_files = [], []
        file_num = len(file_list)

        logger.info('待压缩图片数量: {}', file_num)

        from tinypng_unlimited.config import Config
        thread_num = Config.THREAD_NUM
        # 按实际并发数放大 HTTP 连接池（默认池只有 10，超过就会被 urllib3 丢弃连接，
        # 每个请求退回完整 TLS 握手，表现为「线程越多越慢」）。不传参即按 THREAD_NUM 推算。
        cls.ensure_session_pool()
        with ThreadPoolExecutor(thread_num) as pool:
            with tqdm(desc='[任务进度]', unit='份', total=file_num, file=sys.stdout, ascii=' ▇',
                      colour='yellow', leave=False, ncols=120, position=thread_num) as bar:
                future_list = []
                for old_path in file_list:
                    file_name = os.path.basename(old_path)
                    # 默认下覆盖原文件
                    new_path = os.path.abspath(os.path.join(new_dir, file_name)) if new_dir else old_path
                    future_list.append(pool.submit(cls.compress_from_file, old_path, new_path, True,
                                                   upload_timeout, download_timeout))

                for future in as_completed(future_list):
                    try:
                        info = future.result()
                        # 压缩成功则统计信息
                        old_size += info[1]
                        new_size += info[2]
                        success_count += 1
                        success_files.append(
                            (info[0], cls._byte_converter(info[1]), cls._byte_converter(info[2]), info[3])
                        )
                        logger.success('图片压缩完成: {}', info[0])
                    except CompressException as e:
                        error_files.append(e.detail['path'])
                        logger.error('压缩图片失败: {} {}', os.path.basename(e.detail['path']), e)
                    except Exception as e:
                        logger.error('压缩图片未知错误 {}', e)
                    bar.update()
                bar_info = bar.format_dict

        compression = f'{round(100 * new_size / old_size, 2)}%' if old_size else '100%'
        return {
            'basic': {
                'file_num': file_num, 'success_count': success_count,
                'error_count': len(error_files),
                'time': '{:.2f} s'.format(bar_info['elapsed']), 'speed': '{:.2f} 份/s'.format(bar_info['rate']),
                'output_size': cls._byte_converter(new_size), 'input_size': cls._byte_converter(old_size),
                'compression': compression, 'output_dir': '覆盖原文件' if new_dir is None else new_dir,
            },
            'error_files': error_files,
            'success_files': success_files,
        }

    @classmethod
    def compress_from_dir(cls, dir_path, new_dir=None, reg=r'.*\.(jpe?g|png|svga)$') -> dict:
        """
        压缩文件夹内图片
        :param dir_path: 文件夹路径
        :param new_dir: 输出路径(None则覆盖原文件)
        :param reg: 文件名正则匹配
        :return: 压缩情况报告
        """
        if not os.path.exists(dir_path):
            raise CompressException('源文件夹不存在', dir_path)

        # 默认覆盖原文件
        if new_dir and not os.path.exists(new_dir):
            os.makedirs(new_dir)

        file_list = [os.path.abspath(os.path.join(dir_path, f)) for f in os.listdir(dir_path) if
                     re.match(reg, f, re.IGNORECASE)]

        if not len(file_list):
            raise CompressException('文件夹内无任何匹配文件', dir_path)

        res = cls.compress_from_file_list(file_list, new_dir)
        res['input_dir'] = dir_path
        return res

    @staticmethod
    def _byte_converter(byte_num) -> str:
        if byte_num < 1024:  # 比特
            return '{:.2f} B'.format(byte_num)  # 字节
        elif 1024 <= byte_num < 1024 * 1024:
            return '{:.2f} KB'.format(byte_num / 1024)  # 千字节
        else:
            return '{:.2f} MB'.format(byte_num / 1024 / 1024)  # 兆字节
