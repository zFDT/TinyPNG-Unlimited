"""
调试脚本：模拟申请密钥流程的邮件读取步骤，输出完整响应。

用法：
  1. 填入 config.env 里配置的 APIHZ_ID / APIHZ_KEY
  2. 在程序日志中找到最新一次创建的邮箱地址和密码（或直接运行来创建新邮箱）
  3. python debug_email_fetch.py

输出文件：
  debug_email.html  —— 邮件完整 HTML 内容，可用浏览器打开
  debug_email.json  —— 接口原始 JSON 响应
"""

import json
import os
import sys
import time

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 把项目根目录加入路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinypng_unlimited.config import Config
from tinypng_unlimited.apihz_mail import ApihzMail
import requests

Config.load()

if not Config.APIHZ_ID or not Config.APIHZ_KEY:
    print('错误：请在 config.env 中配置 APIHZ_ID 和 APIHZ_KEY')
    sys.exit(1)

with requests.Session() as session:
    print('正在创建临时邮箱...')
    mail = ApihzMail.create_new_mail(session)
    print(f'邮箱: {mail}  密码: {ApihzMail.pwd}')

    print('向 TinyPNG 注册...')
    res = session.post('https://tinypng.com/web/api', json={
        'fullName': mail.split('@')[0],
        'mail': mail,
    })
    print(f'注册响应: {res.status_code} {res.text}')
    if res.status_code == 429:
        print('\n⚠ TinyPNG 注册频率限制，当前 IP 已被临时封禁。')
        print('请等待 15～30 分钟后再试，或在 config.env 中配置 HTTP_PROXY。')
        sys.exit(1)
    if res.status_code != 200 or res.text != '{}':
        print(f'注册失败: {res.status_code} {res.text}')
        sys.exit(1)

    print(f'\n等待 {ApihzMail._min_interval}s 让邮件到达...')
    time.sleep(ApihzMail._min_interval)

    print('读取邮件...')
    import urllib3
    raw_res = session.get(
        ApihzMail.READ_URL,
        params={
            'id': Config.APIHZ_ID,
            'key': Config.APIHZ_KEY,
            'mail': ApihzMail.mail,
            'pwd': ApihzMail.pwd,
            'page': 1,
        },
        verify=False,
        timeout=(10, 30),
    )

    print(f'HTTP 状态: {raw_res.status_code}')
    data = raw_res.json()

    # 保存原始 JSON
    json_path = os.path.join(os.path.dirname(__file__), 'debug_email.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'原始 JSON 已保存: {json_path}')

    if data.get('code') == 200 and data.get('data'):
        content = data['data'][0].get('content', '')
        subject = data['data'][0].get('subject', '')
        print(f'\n主题: {subject}')
        print(f'内容长度: {len(content)} 字符')

        # 保存完整 HTML
        html_path = os.path.join(os.path.dirname(__file__), 'debug_email.html')
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f'完整邮件 HTML 已保存: {html_path}')
        print('(可用浏览器打开查看，或搜索 tinify.com/login 找激活链接)')

        # 尝试搜索激活链接
        import re
        patterns = [
            (r'href=["\']?(https://tinify\.com/login\?token=[^"\'>\s&]+(?:&amp;|&)[^"\'>\s]+)', '主要 href 匹配'),
            (r'(https://tinify\.com/login\?token=[^\s<"\']+)', '纯文本匹配'),
            (r'(https://[^\s<"\']*token=[^\s<"\']+)', '宽松 token 匹配'),
        ]
        print('\n---- 正则匹配尝试 ----')
        for pattern, name in patterns:
            m = re.search(pattern, content)
            if m:
                url = m.group(1).replace('&amp;', '&')
                print(f'[{name}] 匹配成功: {url}')
            else:
                print(f'[{name}] 未匹配')

        # 打印包含 tinify 的所有行（便于肉眼检查）
        print('\n---- 包含 tinify 的行 ----')
        for i, line in enumerate(content.splitlines()):
            if 'tinify' in line.lower():
                print(f'  行 {i}: {line[:200]}')
    else:
        print(f'获取邮件失败: {data}')
