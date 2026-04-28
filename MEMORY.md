## TinyPNG API 限制与应对策略
- **API 请求间隔**：10秒。代码中已实现 `time.sleep(10)` 强制等待，避免触发限流。
- **邮件主题限流**：24小时内同一主题最多50封。自动申请时使用随机生成的 Subject 规避此限制。
- **邮件保存时长**：48小时。不影响即时获取验证码的流程。
- **域名验证**：SnapMail 等临时邮箱域名通常已被 TinyPNG 列入白名单或无需额外验证，但若失败需切换邮箱服务商。
- **应对方案**：当自动申请因限流失败时，支持通过 `config.env` 手动配置多个 API Key 轮询使用。

## SnapMail 服务特性与适配
- **无需 API Key**：SnapMail (snapmail.cc) 为免费公开服务，其 `POST /emailList/filter` 接口无需认证即可使用。
- **API 频率限制**：免费版要求 API 请求间隔至少 10 秒。已在 `snapmail.py` 中实现 `_ensure_rate_limit()` 机制，确保所有请求遵守此间隔。
- **批量申请策略**：为避免触发 24 小时同主题 50 封邮件的限制，批量申请密钥时增加了 30 秒的额外间隔。
- **注册后等待**：将注册后的首次邮件获取等待时间从 5 秒调整为 12 秒，确保符合频率要求并留出邮件到达缓冲期。

## TinyPNG-Unlimited 项目修复与配置
- **SnapMail API 适配**：SnapMail 免费版 API 变更为 `POST /emailList/filter`（无需 API Key）。代码已更新以支持新接口，并增加频率限制控制（10秒间隔）。
- **环境变量配置**：引入 `python-dotenv`，通过 `config.env` 管理敏感信息（TinyPNG API Keys、代理）和运行参数（超时、线程数）。提供 `config.env.template` 模板。
- **密钥管理优化**：支持从环境变量批量加载 API Keys，新增 `add_key` 命令用于手动添加密钥，增强在自动申请失败时的可用性。
- **限流规避策略**：注册后等待 12s，批量申请间隔 30s，并在 `snapmail.py` 中实现全局 `_ensure_rate_limit()` 机制。
