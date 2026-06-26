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

- **前端网站：<http://localhost:8000/>**（注册 / 登录后使用完整界面）
- Swagger 文档：<http://localhost:8000/api/v1/docs>
- ReDoc 文档：<http://localhost:8000/api/v1/redoc>
- 健康检查：<http://localhost:8000/api/v1/health>

## 前端网站

项目内置服务端渲染（**FastAPI + Jinja2 + TailwindCSS + Alpine.js**）的 Web 界面，
普通用户可在浏览器完成全部操作，无需直接调用 API：

- 注册 / 登录 / 登出（基于 HttpOnly Cookie 会话）
- 仪表盘：资源统计与最近收件
- 绑定 / 管理 CF 账号、同步域名
- 管理域名（管理员可将平台域名分配给用户）
- 创建与管理邮箱地址、转发规则
- 收件箱查看（HTML 正文沙箱隔离）、撰写并发送邮件
- 管理 API Key（原文仅创建时展示一次）、修改个人资料
- 管理员后台：用户列表与详情

> Tailwind 与 Alpine 通过 Play CDN 加载（运行时联网）；核心表单在无 JS / 无网络时仍可提交。

## 测试

```bash
pytest tests/
```

每个测试用例使用独立的 SQLite 内存数据库，测试中所有 Cloudflare API 调用均为 Mock。

### 端到端（e2e）测试

前端关键路径用 **Playwright** 驱动真实浏览器测试，位于 `e2e/` 目录（默认 `pytest` 不收集）：

```bash
pip install pytest-playwright
playwright install chromium   # 首次需下载浏览器
pytest e2e/
```

e2e 通过 `CF_FAKE_MODE=1` 让 Cloudflare 调用返回内置假数据，可离线运行（无需真实 CF 账号）。

---

## Cloudflare API Token 权限要求

创建 API Token 时需勾选以下权限：

| 权限 | 用途 |
|------|------|
| `Zone:Email Routing:Edit` | 转发规则管理 |
| `Account:Email Routing Addresses:Edit` | 目标地址管理 |
| `Account:Email Send:Edit` | 发件（Beta） |
| `Zone:Zone:Read` | 读取域名信息 |
| `Account:Workers Scripts:Edit` | **一键部署收件 Worker（可选）** |

## Webhook 收件配置

收件依赖在 Cloudflare Email Worker（Email Routing 触发）中将邮件 `POST` 到本平台的
Webhook 端点 `POST /api/v1/inbound/webhook`。

> 📌 **开箱即用的 Worker 示例代码**位于 [`examples/worker/`](examples/worker/)，
> 包含完整的 Worker 代码、wrangler 配置和部署步骤，可直接 `wrangler deploy`。

### 一键部署（推荐）

平台已支持**前端一键部署收件 Worker**。绑定 CF 账号（Token 含
`Account:Workers Scripts:Edit` 权限）后，在 CF 账号详情页点击
「一键部署 Worker」即可自动完成：

1. 启用各域名 Email Routing
2. 上传 Worker 脚本（绑定 `WEBHOOK_URL`）
3. 注入 `WEBHOOK_SECRETS`（域名→`{zone_id, secret}` JSON 映射）
4. 为账号下每个域名配置 catch-all → Worker

新增域名后，再次点击「一键部署」即可更新密钥映射与路由。

平台对 Webhook 签名采用**每域名独立密钥**（`Domain.webhook_secret`），
按收件地址域名自动定位；未匹配时回退到全局 `CF_WEBHOOK_SECRET`
（兼容旧部署）。

### 快速部署（手动降级）

```bash
cd examples/worker
npm install
# 编辑 wrangler.toml 中的 WEBHOOK_URL 为你的平台地址
npx wrangler secret put CF_WEBHOOK_SECRET   # 粘贴平台 .env 中的值
npx wrangler deploy
```

部署后在 CF Dashboard → 你的域名 → Email → Email Routing → Routing Rules 中，
将收件地址（或 Catch-all）的操作设为「发送到 Worker」，选择刚部署的 Worker。

详细步骤和排查指南见 [`examples/worker/README.md`](examples/worker/README.md)。

### Webhook 协议

请求体为 JSON：

```json
{ "to": "hello@example.com", "from": "sender@x.com", "subject": "...", "text": "...", "html": "..." }
```

平台对**原始请求体字节**使用 `CF_WEBHOOK_SECRET` 计算 `HMAC-SHA256` 十六进制摘要，
Worker 需将该摘要放入请求头 `X-Webhook-Signature`，平台以常量时间比较校验，校验失败返回 401。
Worker 侧签名示例：

```js
const key = await crypto.subtle.importKey(
  "raw", enc.encode(CF_WEBHOOK_SECRET),
  { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
const sig = await crypto.subtle.sign("HMAC", key, body);
// 将 sig 转为 hex 后写入 X-Webhook-Signature 头
```

收到的邮件按收件地址（`to`）归属对应用户，可通过
`GET /api/v1/inbound`、`GET /api/v1/inbound/{id}`（需 JWT）查询，
或通过公开链接 `GET /mail/{token}`（无需登录）查看最新邮件。

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
