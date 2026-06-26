# CF Email Manager

**CF Email Manager** 是一个开源、自托管的 **Cloudflare Email 管理平台**。它基于 **FastAPI** 构建，面向需要集中管理 **Cloudflare Email Routing**、邮箱转发规则、收件 Webhook、API Key 和 **Cloudflare Email Sending** 的个人开发者、团队和 SaaS 项目。

项目支持绑定 Cloudflare 账号、同步域名、创建邮箱地址、管理转发规则、通过 Worker Webhook 接收邮件、通过 Cloudflare Email Sending API 发件，并提供浏览器 Web UI 与 JSON API。

**Keywords:** Cloudflare Email Manager, Cloudflare Email Routing, Cloudflare Email Sending, FastAPI email platform, self-hosted email platform, email forwarding, inbound email webhook, API keys, SQLite, SQLAlchemy, Alembic.

> Cloudflare Email Sending 当前在项目中按 Beta 能力接入，README 中的部署说明默认你已经具备对应账号权限。

## 项目亮点

- **Self-hosted email platform**：完全自部署，数据和 Cloudflare API Token 掌握在自己手里。
- **Cloudflare Email Routing 管理**：同步域名、创建邮箱地址、管理转发规则和目标地址。
- **Inbound webhook 收件**：通过 Cloudflare Email Worker 将收到的邮件推送到本平台并存储。
- **Cloudflare Email Sending 发件**：从已授权域名发送邮件，适合轻量通知、工具邮件和测试邮件。
- **Web UI + API**：既可以在浏览器中管理，也可以通过 API Key 程序化收发邮件。
- **安全默认值**：CF API Token 加密存储，API Key 只保存哈希，生产环境强制校验关键安全配置。

## 功能特性

- 用户注册、登录、刷新 Token、HttpOnly Cookie 会话
- 绑定和管理多个 Cloudflare 账号
- 同步 Cloudflare 域名并按用户分配平台域名
- 管理邮箱地址、转发目标地址和转发规则
- 一键部署收件 Worker，并为域名配置 catch-all 到 Worker
- Webhook 接收邮件，支持 HTML 正文沙箱隔离查看
- 通过 Cloudflare Email Sending API 发送邮件
- 创建和管理 API Key，原始 Key 仅创建时展示一次
- 管理员后台：用户列表、用户详情和资源查看
- SEO 入口：`robots.txt`、`sitemap.xml`、公开邮件页面

## 技术栈

| 模块 | 技术 |
|------|------|
| Web 框架 | FastAPI, uvicorn |
| 数据库 | SQLite, SQLAlchemy 2.x Async ORM, aiosqlite |
| 迁移 | Alembic |
| 模板与前端 | Jinja2, Tailwind CSS Play CDN, Alpine.js |
| 配置 | pydantic-settings, `.env` |
| 认证与安全 | JWT, bcrypt, cryptography Fernet |
| HTTP 客户端 | httpx |
| 测试 | pytest, pytest-asyncio, Playwright e2e |
| 部署 | Docker, Cloudflare Worker 示例 |

## 快速开始

### 环境要求

- Python 3.12+
- 一个 Cloudflare 账号
- Cloudflare API Token，权限见 [Cloudflare API Token 权限](#cloudflare-api-token-权限)

### 本地安装

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，至少修改：

- `SECRET_KEY`
- `CF_WEBHOOK_SECRET`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`

### 数据库迁移

```bash
alembic upgrade head
```

修改模型后生成迁移：

```bash
alembic revision --autogenerate -m "描述"
```

### 启动开发服务

```bash
uvicorn app.main:app --reload
```

启动后访问：

- Web UI: <http://localhost:8000/>
- Swagger API Docs: <http://localhost:8000/api/v1/docs>
- ReDoc: <http://localhost:8000/api/v1/redoc>
- Health Check: <http://localhost:8000/api/v1/health>

## Docker 运行

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

如果使用 SQLite 并希望数据持久化，请在部署时为数据库文件所在目录配置 volume，并同步调整 `DATABASE_URL`。

## 环境变量

`.env.example` 包含项目所需配置。生产部署时必须设置安全值：

```env
ENVIRONMENT=production
APP_BASE_URL=https://your-domain.com
COOKIE_SECURE=true
CSRF_PROTECTION=true
DEBUG=false
CF_FAKE_MODE=false
```

生产环境启动时会拒绝默认密钥、默认管理员密码、非 HTTPS 回调地址、不安全 Cookie 配置和测试模式。

常用变量：

| 变量 | 说明 |
|------|------|
| `SECRET_KEY` | JWT、会话签名和 Fernet 加密密钥，生产环境必须替换 |
| `DATABASE_URL` | SQLAlchemy 异步数据库连接，默认 `sqlite+aiosqlite:///./cf_email.db` |
| `CF_API_BASE_URL` | Cloudflare API 基础地址 |
| `CF_WEBHOOK_SECRET` | Worker Webhook 签名密钥，旧部署回退使用 |
| `APP_BASE_URL` | 平台公网地址，Worker 回调和生产校验会使用 |
| `ADMIN_EMAIL` | 首次启动自动创建的管理员邮箱 |
| `ADMIN_PASSWORD` | 首次启动自动创建的管理员密码 |

## Web UI 与 API

项目内置服务端渲染 Web UI，普通用户可在浏览器中完成主要工作流：

- 注册、登录、登出
- 仪表盘资源统计与最近收件查看
- CF 账号绑定、同步域名和 Worker 部署
- 邮箱地址、目标地址、转发规则管理
- 收件箱查看、公开邮件链接、撰写并发送邮件
- API Key 管理和个人资料更新
- 管理员用户管理

JSON API 统一前缀为 `/api/v1`，响应格式：

```json
{ "code": 0, "data": {}, "message": "ok" }
```

认证方式：

- 用户认证：`Authorization: Bearer {jwt_token}`
- 程序化调用：`X-API-Key: {api_key}`
- Web 表单：生产环境启用 CSRF token 校验

## Cloudflare API Token 权限

创建 Cloudflare API Token 时按需授予：

| 权限 | 用途 |
|------|------|
| `Zone:Email Routing:Edit` | 管理域名 Email Routing 和转发规则 |
| `Account:Email Routing Addresses:Edit` | 管理转发目标地址 |
| `Account:Email Send:Edit` | 使用 Cloudflare Email Sending 发件 |
| `Zone:Zone:Read` | 读取域名和 Zone 信息 |
| `Account:Workers Scripts:Edit` | 一键部署收件 Worker，可选 |

平台域名分配给用户后，用户只能使用对应域名能力，不能看到底层 Cloudflare API Token。

## Webhook / Worker 收件配置

收件依赖 Cloudflare Email Worker。Worker 由 Cloudflare Email Routing 触发后，将邮件内容 `POST` 到本平台：

```text
POST /api/v1/inbound/webhook
```

Worker 示例位于 [`examples/worker/`](examples/worker/)，包含 `wrangler.toml`、Worker 源码和部署说明。

### 一键部署 Worker

绑定 CF 账号后，如果 Token 包含 `Account:Workers Scripts:Edit` 权限，可以在 CF 账号详情页点击「一键部署 Worker」。平台会自动完成：

1. 启用各域名 Email Routing
2. 上传 Worker 脚本并绑定 `WEBHOOK_URL`
3. 注入 `WEBHOOK_SECRETS`
4. 为账号下域名配置 catch-all 到 Worker

新增域名后再次部署即可更新密钥映射与路由。

### 手动部署 Worker

```bash
cd examples/worker
npm install
npx wrangler secret put CF_WEBHOOK_SECRET
npx wrangler deploy
```

部署后进入 Cloudflare Dashboard，将域名 Email Routing 规则配置为发送到该 Worker。详细步骤见 [`examples/worker/README.md`](examples/worker/README.md)。

### Webhook 签名

平台对原始请求体使用 `CF_WEBHOOK_SECRET` 或域名级 `Domain.webhook_secret` 计算 `HMAC-SHA256`，Worker 需把十六进制签名放入：

```text
X-Webhook-Signature
```

签名校验失败时平台返回 `401`。收到的邮件按 `to` 地址归属到对应用户，可通过 API、Web UI 或公开链接查看。

## 测试

运行单元与接口测试：

```bash
pytest tests/
```

测试使用独立 SQLite 内存数据库，Cloudflare API 调用全部 Mock，不会发真实网络请求。

运行 e2e 测试：

```bash
pip install pytest-playwright
playwright install chromium
pytest e2e/
```

e2e 通过 `CF_FAKE_MODE=1` 使用内置 Cloudflare 假数据，适合本地离线验证关键前端流程。

## 安全设计

- CF API Token 入库前使用 `SECRET_KEY` 经 Fernet 对称加密
- 用户密码使用 bcrypt 哈希
- API Key 只保存哈希，原始值只在创建时返回一次
- Webhook 使用 HMAC-SHA256 签名校验
- 生产环境强制 HTTPS `APP_BASE_URL`、安全 Cookie 和非默认密钥
- HTML 邮件正文在 Web UI 中隔离展示，降低内容注入风险

## 适用场景

- 想自托管 Cloudflare Email Routing 管理后台
- 需要为多个用户分配和管理域名邮箱能力
- 需要轻量收件 Webhook 和邮件归档
- 需要通过 API Key 给内部系统提供收发邮件能力
- 需要一个可二次开发的 FastAPI email management starter

## License

MIT
