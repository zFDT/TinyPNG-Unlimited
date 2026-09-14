"""
版本号唯一来源。

发版流程：改这里的 __version__ -> 提交 -> 打同名 tag（如 v1.1.0）并推送，
GitHub Actions 会自动为该 tag 构建 Windows / macOS / Linux 三平台产物并创建 Release。

不要在别处再写版本号字符串，否则迟早会对不上。
"""

__version__ = '1.2.0'
