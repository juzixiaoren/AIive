"""单用户登录认证（V49）。

- security: 密码哈希（Argon2id）、JWT 签发/校验、签名密钥解析
- service: 初始化状态、首次注册、登录（含失败限流）、当前账号
- guard: 全局 HTTP 认证守卫与 WebSocket 首帧认证
"""
