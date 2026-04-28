"""
配置管理模块
支持从环境变量和 .env 文件加载配置
"""
import os
from dotenv import load_dotenv
from typing import Optional, List


def load_config(env_file: str = None):
    """
    加载配置文件
    :param env_file: 环境变量文件路径，默认为项目根目录下的 config.env
    """
    if env_file is None:
        # 默认加载项目根目录下的 config.env
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_file = os.path.join(project_root, 'config.env')
    
    # 加载 .env 文件（如果存在）
    if os.path.exists(env_file):
        load_dotenv(env_file)


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
    
    @classmethod
    def load(cls, env_file: str = None):
        """
        加载所有配置
        :param env_file: 环境变量文件路径
        """
        load_config(env_file)
        
        # TinyPNG API Keys
        cls.TINYPNG_API_KEYS = get_env_list('TINYPNG_API_KEYS', [])
        
        # 代理设置
        cls.HTTP_PROXY = get_env_str('HTTP_PROXY')
        cls.HTTPS_PROXY = get_env_str('HTTPS_PROXY')
        
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
    
    @classmethod
    def get_proxy(cls) -> Optional[str]:
        """
        获取代理设置（优先使用 HTTPS_PROXY，其次 HTTP_PROXY）
        :return: 代理地址
        """
        return cls.HTTPS_PROXY or cls.HTTP_PROXY


# 自动加载配置（在模块导入时执行）
Config.load()
