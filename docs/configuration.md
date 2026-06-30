# 配置与部署指南

本文档介绍 CF Email Manager 的配置文件、本地启动、数据库迁移、Docker 部署、Cloudflare API Token 和 Worker 收件配置。

## 1. 准备环境

要求：

- Python 3.12+
- 一个 Cloudflare 账号
- 一个具备邮件路由、发件和 Worker 权限的 Cloudflare API Token

本地安装：

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

## 2. 配置 `.env`

`.env.example` 已包含全部配置项。首次运行至少修改：

```env
SECRET_KEY=your-real-secret-key-at-least-32-chars
CF_WEBHOOK_SECRET=your-real-webhook-secret
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=change-this-password
```

生产部署推荐配置：

```env
ENVIRONMENT=production
APP_BASE_URL=https://your-domain.com
COOKIE_SECURE=true
CSRF_PROTECTION=true
DEBUG=false
CF_FAKE_MODE=false
AUTO_MIGRATE_SQLITE=false
```

生产环境启动时会拒绝默认密钥、默认管理员密码、非 HTTPS 回调地址、不安全 Cookie 配置和测试模式。

## 3. 常用环境变量

| 变量 | 说明 |
|------|------|
| `SECRET_KEY` | JWT、会话签名和 Fernet 加密密钥，生产环境必须替换 |
| `DATABASE_URL` | SQLAlchemy 异步数据库连接，默认 `sqlite+aiosqlite:///./cf_email.db` |
| `AUTO_MIGRATE_SQLITE` | 是否在启动时自动迁移 SQLite 数据库，默认 `false` |
| `CF_API_BASE_URL` | Cloudflare API 基础地址 |
| `CF_WEBHOOK_SECRET` | Worker Webhook 签名密钥，旧部署回退使用 |
| `APP_BASE_URL` | 平台公网地址，Worker 回调和生产校验会使用 |
| `ADMIN_EMAIL` | 首次启动自动创建的管理员邮箱 |
| `ADMIN_PASSWORD` | 首次启动自动创建的管理员密码 |
| `COOKIE_SECURE` | 前端会话 Cookie 是否仅限 HTTPS |
| `CSRF_PROTECTION` | Web 表单 CSRF 防护 |
| `TRUST_PROXY_HEADERS` | 是否信任反向代理传入的客户端 IP 头，默认 `false` |
| `TRUSTED_PROXY_IPS` | 可信反向代理 IP/CIDR 列表，仅在 `TRUST_PROXY_HEADERS=true` 时使用 |
| `LOGIN_RATE_LIMIT_ATTEMPTS` / `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | 登录失败限流阈值与窗口 |
| `API_KEY_RATE_LIMIT_ATTEMPTS` / `API_KEY_RATE_LIMIT_WINDOW_SECONDS` | API Key 调用限流阈值与窗口 |
| `PUBLIC_MAIL_RATE_LIMIT_ATTEMPTS` / `PUBLIC_MAIL_RATE_LIMIT_WINDOW_SECONDS` | 公开邮件链接访问限流阈值与窗口 |
| `PUBLIC_MAIL_SEND_RATE_LIMIT_ATTEMPTS` / `PUBLIC_MAIL_SEND_RATE_LIMIT_WINDOW_SECONDS` | 公开邮件链接发件限流阈值与窗口 |
| `WEBHOOK_MAX_BYTES` | Webhook 请求体最大字节数，默认 1 MiB |

如果部署在可信反向代理后并需要真实客户端 IP，请同时设置：

```env
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_IPS=127.0.0.1,10.0.0.0/8
```

## 4. 数据库迁移

首次启动前执行：

```bash
alembic upgrade head
```

已有部署拉取新代码后，建议先备份当前数据库，再执行迁移：

```bash
alembic upgrade head
```

如果使用仓库默认 SQLite 数据库，Windows 本地可执行：

```powershell
Copy-Item .\cf_email.db .\cf_email.db.bak
.\.venv\Scripts\python.exe -m alembic upgrade head
```

`AUTO_MIGRATE_SQLITE` 默认保持 `false`。启动时如果检测到已有 SQLite 数据库的 Alembic 版本落后于当前代码，应用会拒绝启动并提示执行迁移，避免页面访问时才出现 500。

自部署单实例 SQLite 可在确认备份策略后设置：

```env
AUTO_MIGRATE_SQLITE=true
```

修改模型后生成迁移：

```bash
alembic revision --autogenerate -m "描述"
```

## 5. 启动服务

开发模式：

```bash
uvicorn app.main:app --reload
```

启动后访问：

- Web UI: <http://localhost:8000/>
- Swagger API Docs: <http://localhost:8000/api/v1/docs>
- ReDoc: <http://localhost:8000/api/v1/redoc>
- Health Check: <http://localhost:8000/api/v1/health>

JSON API 统一前缀为 `/api/v1`，成功响应格式：

```json
{ "code": 0, "data": {}, "message": "ok" }
```

认证方式：

- 用户认证：`Authorization: Bearer {jwt_token}`
- 程序化调用：`X-API-Key: {api_key}`
- Web 表单：生产环境启用 CSRF token 校验

API Key scope 支持 `send` 与 `read_inbound`。`send` 可调用 `/api/v1/outbound/send`，`read_inbound` 可读取 `/api/v1/inbound` 与 `/api/v1/inbound/{id}`，资源管理接口不开放给 API Key。

## 6. Docker 运行

构建镜像：

```bash
docker build -t cf-email-manager .
```

首次启动前执行迁移：

```bash
docker run --rm --env-file .env cf-email-manager alembic upgrade head
```

启动容器：

```bash
docker run --rm -p 8000:8000 --env-file .env cf-email-manager
```

如果使用 SQLite 并希望数据持久化，请为数据库文件所在目录配置 volume，并同步调整 `DATABASE_URL`。

例如 Docker/Portainer 中可将 volume 挂载到 `/app/data`，并设置：

```env
DATABASE_URL=sqlite+aiosqlite:////app/data/cf_email.db
AUTO_MIGRATE_SQLITE=true
```

`AUTO_MIGRATE_SQLITE=true` 仅对 SQLite 生效；启动时会自动创建 SQLite 文件父目录并执行 `alembic upgrade head`。如果不启用该开关，仍需手动执行迁移。

## 7. Cloudflare API Token 权限

绑定账号时系统会执行权限预检。创建 Cloudflare API Token 时必须授予：

| 权限 | 用途 |
|------|------|
| `Zone:Email Routing:Edit` | 管理域名 Email Routing 和转发规则 |
| `Account:Email Routing Addresses:Edit` | 管理转发目标地址 |
| `Account:Email Send:Edit` | 使用 Cloudflare Email Sending 发件 |
| `Zone:Zone:Read` | 读取域名和 Zone 信息 |
| `Account:Workers Scripts:Edit` / `Workers Scripts Write` | 一键部署收件 Worker |

Token 设置注意事项：

- 支持 User API Token 和 Account API Token。
- User API Token 在 Cloudflare 控制台 `My Profile` -> `API Tokens` 创建。
- Account API Token 在 `Manage Account` -> `API Tokens` 创建，绑定时必须填写所属 Account ID。
- 资源范围必须覆盖要接入的 Account 和至少一个 Zone。
- API Token 输入框只填写原始 Token，不要包含 `Bearer` 前缀。
- 如果 Token 配置了来源 IP 限制，请放行本服务的公网出口 IP。
- Workers Scripts 编辑权限是硬性门槛；没有该权限无法部署收件 Worker，也无法完整收发邮件。

绑定账号时：

- User API Token 使用 `GET /user/tokens/verify` 校验。
- Account API Token 使用 `GET /accounts/{account_id}/tokens/verify` 校验。
- 缺少任一核心权限都会拒绝绑定，并返回具体失败项和修复建议。

平台域名分配给用户后，用户只能使用对应域名能力，不能看到底层 Cloudflare API Token。

## 8. Webhook / Worker 收件配置

收件依赖 Cloudflare Email Worker。Worker 由 Cloudflare Email Routing 触发后，将邮件内容 `POST` 到本平台：

```text
POST /api/v1/inbound/webhook
```

Worker 示例位于 [`examples/worker/`](../examples/worker/)，包含 `wrangler.toml`、Worker 源码和部署说明。

### 一键部署 Worker

绑定 CF 账号后，如果 Token 权限预检通过，可以在 CF 账号详情页点击「一键部署 Worker」。平台会自动完成：

1. 启用各邮箱域名 Email Routing
2. 上传 Worker 脚本并绑定 `WEBHOOK_URL`
3. 注入 `WEBHOOK_SECRETS`
4. 为已启用收件路由的邮箱域名配置 catch-all 到 Worker

创建某域名下第一个邮箱地址后，该域名会启用收件路由；启用新的邮箱域名后再次部署即可更新密钥映射与路由。

### 手动部署 Worker

```bash
cd examples/worker
npm install
npx wrangler secret put CF_WEBHOOK_SECRET
npx wrangler deploy
```

部署后进入 Cloudflare Dashboard，将域名 Email Routing 规则配置为发送到该 Worker。详细步骤见 [`examples/worker/README.md`](../examples/worker/README.md)。

### Webhook 签名

平台对原始请求体使用 `CF_WEBHOOK_SECRET` 或域名级 `Domain.webhook_secret` 计算 `HMAC-SHA256`，Worker 需把十六进制签名放入：

```text
X-Webhook-Signature
```

签名校验失败时平台返回 `401`。收到的邮件按 `to` 地址归属到对应用户，可通过 API、Web UI 或公开链接查看。新 Worker 会把 MIME Header `From` 作为展示发件人，同时把 Cloudflare `message.from` 保存为信封发件人，便于排查退信和转发链路。
