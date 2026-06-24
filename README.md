# CF Email Manager

基于 **Python + FastAPI + SQLite + SQLAlchemy（异步）+ uvicorn** 的 Cloudflare Email 管理平台。

支持绑定 Cloudflare 账号、管理邮箱地址与转发规则、通过 CF Email Sending API（Beta）发件、
通过 Webhook 收件，并提供 API Key 供程序化收发邮件。开源、可完全自部署。

> ⚠️ **CF Email Sending 目前为 Beta**，免费额度为 **1000 封/天**。

---

## 环境要求

- Python 3.12+
- 一个 Cloudflare 账号与 API Token（权限见下文）

## 安装

```bash
# 1. 创建虚拟环境
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 准备环境变量
cp .env.example .env
# 编辑 .env，至少修改 SECRET_KEY、ADMIN_EMAIL、ADMIN_PASSWORD
```

## 数据库迁移

```bash
# 应用迁移（创建所有表）
alembic upgrade head

# 修改模型后生成新迁移
alembic revision --autogenerate -m "描述"
```

## 启动

```bash
uvicorn app.main:app --reload
```

启动后访问：

- Swagger 文档：<http://localhost:8000/api/v1/docs>
- ReDoc 文档：<http://localhost:8000/api/v1/redoc>
- 健康检查：<http://localhost:8000/api/v1/health>

## 测试

```bash
pytest tests/
```

每个测试用例使用独立的 SQLite 内存数据库，测试中所有 Cloudflare API 调用均为 Mock。

---

## Cloudflare API Token 权限要求

创建 API Token 时需勾选以下权限：

| 权限 | 用途 |
|------|------|
| `Zone:Email Routing:Edit` | 转发规则管理 |
| `Account:Email Routing Addresses:Edit` | 目标地址管理 |
| `Account:Email Send:Edit` | 发件（Beta） |
| `Zone:Zone:Read` | 读取域名信息 |

## Webhook 收件配置

收件依赖在 Cloudflare Worker（Email Routing 触发）中将邮件 `POST` 到本平台的
Webhook 端点 `POST /api/v1/inbound/webhook`。请求体为 JSON：

```json
{ "to": "hello@example.com", "from": "sender@x.com", "subject": "...", "text": "...", "html": "..." }
```

平台对**原始请求体字节**使用 `CF_WEBHOOK_SECRET` 计算 `HMAC-SHA256` 十六进制摘要，
Worker 需将该摘要放入请求头 `X-Webhook-Signature`，平台以常量时间比较校验，校验失败返回 401。
Worker 侧签名示例：

```js
const sig = await crypto.subtle.sign(
  "HMAC",
  await crypto.subtle.importKey("raw", enc.encode(CF_WEBHOOK_SECRET),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]),
  body, // 与请求体完全一致的字节
);
// 将 sig 转为 hex 后写入 X-Webhook-Signature 头
```

收到的邮件按收件地址（`to`）归属对应用户，可通过
`GET /api/v1/inbound`、`GET /api/v1/inbound/{id}`（需 JWT）查询。

## API 约定

- 统一前缀：`/api/v1`
- 成功响应：`{"code": 0, "data": ..., "message": "ok"}`
- 错误响应：`{"code": 非0, "data": null, "message": "错误描述"}`
- 用户认证：`Authorization: Bearer {jwt_token}`
- 程序化调用：`X-API-Key: {api_key}`

## 安全说明

- CF API Token 入库前使用 `SECRET_KEY` 经 Fernet 对称加密存储
- 用户密码使用 bcrypt 哈希
- API Key 仅存储哈希值，原始值只在创建时返回一次

## 许可证

MIT
