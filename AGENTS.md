# CF Email Manager — 项目说明

## 项目概述

基于 Python + FastAPI + SQLite + SQLAlchemy（异步）+ uvicorn 的 Cloudflare Email 管理平台。

**核心目标：**
- 用户可绑定自己的 CF 账号（全部权限或仅指定域名）
- 管理邮箱地址、转发规则
- 使用 CF Email Sending API（Beta）直接从域名发送邮件
- 通过 Webhook 接收邮件并存储
- 提供 API Key 供用户程序化收发邮件
- 平台管理员可将自己 CF 账号下的域名分配给用户使用
- 开源项目，支持用户完全自部署，绕过平台直接使用

---

## 技术栈

- **Python 3.12+**
- **FastAPI** — 异步 Web 框架
- **SQLAlchemy 2.x** — 异步 ORM（AsyncSession + async_sessionmaker）
- **aiosqlite** — SQLite 异步驱动
- **uvicorn** — ASGI 服务器
- **Pydantic v2** — 数据验证和序列化
- **alembic** — 数据库迁移
- **python-jose[cryptography]** — JWT 生成和验证
- **passlib[bcrypt]** — 密码哈希
- **httpx** — 异步 HTTP 客户端（调用 CF API）
- **cryptography** — API Token 对称加密存储
- **pytest + pytest-asyncio + httpx** — 异步测试

---

## Cloudflare API 说明

### Email Routing（收件/转发）
- 基础 URL：`https://api.cloudflare.com/client/v4`
- 获取转发规则：`GET /zones/{zone_id}/email/routing/rules`
- 创建转发规则：`POST /zones/{zone_id}/email/routing/rules`
- 删除转发规则：`DELETE /zones/{zone_id}/email/routing/rules/{rule_id}`
- 获取目标地址：`GET /accounts/{account_id}/email/routing/addresses`
- 创建目标地址：`POST /accounts/{account_id}/email/routing/addresses`

### Email Sending（发件，Beta）
- 发送邮件：`POST /accounts/{account_id}/email/send`
- 请求体：`{ "from": "...", "to": ["..."], "subject": "...", "text": "...", "html": "..." }`
- 认证：`Authorization: Bearer {api_token}`
- 日发送配额：1000 封/天（免费）

### Zone API（域名管理）
- 获取域名列表：`GET /zones?account.id={account_id}`
- 获取单个域名：`GET /zones/{zone_id}`

### API Token 权限要求（告知用户）
- `Zone:Email Routing:Edit`（转发规则管理）
- `Account:Email Routing Addresses:Edit`（目标地址管理）
- `Account:Email Send:Edit`（发件 Beta 权限）
- `Zone:Zone:Read`（读取域名信息）

---

## 数据库模型（SQLAlchemy Mapped 风格）

```
User               — 用户账号（id, username, email, hashed_password, role, is_active）
CFAccount          — 用户绑定的 CF 账号（id, user_id, name, encrypted_api_token, account_id, permission_type, allowed_zone_ids）
Domain             — 域名（id, cf_account_id, zone_id, domain_name, owner_type, status）
DomainAssignment   — 平台域名分配给普通用户（id, domain_id, user_id）
EmailAddress       — 邮箱地址（id, domain_id, user_id, local_part, full_address, is_active）
ForwardingRule     — 转发规则（id, email_address_id, destination_email, cf_rule_id, is_active）
InboundEmail       — 收到的邮件（id, to_address, from_address, subject, body_text, body_html, received_at）
APIKey             — 用户 API Key（id, user_id, key_hash, name, last_used_at, is_active）
```

### 字段规范
- 主键：`id: Mapped[int]`，自增
- 时间：`created_at: Mapped[datetime]`，UTC，`server_default=func.now()`
- 软删除：重要数据加 `is_deleted: Mapped[bool] = mapped_column(default=False)`
- 外键：`user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))`

---

## 项目目录结构

```
cf-email-manager/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI 入口，注册路由
│   ├── config.py                  # Settings（pydantic-settings 读取 .env）
│   ├── database.py                # AsyncEngine + AsyncSession + Base
│   │
│   ├── models/                    # SQLAlchemy ORM 模型
│   │   ├── __init__.py
│   │   ├── user.py
│   │   ├── cf_account.py
│   │   ├── domain.py
│   │   ├── email_address.py
│   │   ├── forwarding_rule.py
│   │   ├── inbound_email.py
│   │   └── api_key.py
│   │
│   ├── schemas/                   # Pydantic v2 请求/响应模型
│   │   ├── __init__.py
│   │   ├── common.py              # 通用响应格式 ApiResponse[T]
│   │   ├── user.py
│   │   ├── cf_account.py
│   │   ├── domain.py
│   │   ├── email_address.py
│   │   ├── forwarding_rule.py
│   │   ├── inbound_email.py
│   │   └── api_key.py
│   │
│   ├── routers/                   # 路由层（薄层，只做参数校验）
│   │   ├── __init__.py
│   │   ├── auth.py                # 注册/登录/刷新 Token
│   │   ├── users.py               # 用户信息管理
│   │   ├── cf_accounts.py         # CF 账号绑定
│   │   ├── domains.py             # 域名管理与分配
│   │   ├── email_addresses.py     # 邮箱地址 CRUD
│   │   ├── forwarding_rules.py    # 转发规则管理
│   │   ├── inbound.py             # Webhook 收件端点
│   │   ├── outbound.py            # 发件 API
│   │   ├── api_keys.py            # API Key 管理
│   │   └── admin.py               # 管理员专属操作
│   │
│   ├── services/                  # 业务逻辑层
│   │   ├── __init__.py
│   │   ├── cloudflare.py          # CF API 全部封装（httpx AsyncClient）
│   │   ├── auth_service.py        # 注册/登录/Token 逻辑
│   │   ├── user_service.py
│   │   ├── domain_service.py      # 域名同步、分配逻辑
│   │   ├── email_service.py       # 邮箱地址管理逻辑
│   │   ├── forwarding_service.py  # 转发规则逻辑
│   │   ├── inbound_service.py     # 收件处理逻辑
│   │   └── outbound_service.py    # 发件逻辑（调用 CF Email Sending Beta）
│   │
│   ├── dependencies.py            # Depends 依赖（current_user、is_admin 等）
│   └── exceptions.py              # 自定义异常 + handler
│
├── tests/
│   ├── conftest.py                # AsyncClient + 测试数据库 fixture
│   ├── test_auth.py
│   ├── test_users.py
│   ├── test_cf_accounts.py
│   ├── test_domains.py
│   ├── test_email_addresses.py
│   ├── test_forwarding_rules.py
│   ├── test_inbound.py
│   ├── test_outbound.py
│   └── test_api_keys.py
│
├── alembic/
│   ├── versions/
│   └── env.py
│
├── AGENTS.md
├── README.md
├── requirements.txt
├── .env.example
├── alembic.ini
└── pyproject.toml
```

---

## 代码规范

### 类型注解
- 所有函数必须有完整类型注解（参数 + 返回值）
- SQLAlchemy 模型必须使用 `Mapped[...]` + `mapped_column()` 风格
- 禁止使用 `Any`（极少数情况需注释说明原因）
- Pydantic 模型使用 `model_config = ConfigDict(...)`

### 异步规范
- 所有数据库操作使用 `AsyncSession`，禁止同步 `Session`
- 所有 CF API 调用使用 `httpx.AsyncClient`
- 路由函数统一 `async def`

### 分层规范
- **routers/** — 只做参数接收、权限校验、调用 service、返回响应，禁止写业务逻辑
- **services/** — 所有业务逻辑，可调用其他 service
- **models/** — 只放 ORM 模型定义
- **schemas/** — 只放 Pydantic 模型

### 注释规范
- 代码注释使用**中文**
- 函数 docstring 简洁说明功能

### API 规范
- 统一前缀：`/api/v1`
- 成功响应：`{"code": 0, "data": ..., "message": "ok"}`
- 错误响应：`{"code": 非0, "data": null, "message": "错误描述"}`
- 认证：`Authorization: Bearer {jwt_token}`（登录后获取）
- API Key：`X-API-Key: {api_key}`（程序化调用）
- 分页：`?page=1&size=20`

### 安全规范
- CF API Token 存库前必须用 SECRET_KEY 对称加密（Fernet）
- 密码使用 bcrypt 哈希
- API Key 只存哈希值，原始值只在创建时返回一次
- Webhook 端点需验证 CF 签名

### 错误处理
- 自定义异常在 `exceptions.py` 定义
- 在 `main.py` 注册全局 exception handler
- 禁止在路由层直接裸 raise HTTPException

---

## 环境变量（.env.example）

```
# 应用
SECRET_KEY=your-secret-key-at-least-32-chars
ACCESS_TOKEN_EXPIRE_MINUTES=60
REFRESH_TOKEN_EXPIRE_DAYS=30

# 数据库
DATABASE_URL=sqlite+aiosqlite:///./cf_email.db

# Cloudflare
CF_API_BASE_URL=https://api.cloudflare.com/client/v4
CF_WEBHOOK_SECRET=your-webhook-secret-here

# 平台管理员（首次启动自动创建）
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=change-me-on-first-run

# 应用
APP_NAME=CF Email Manager
APP_VERSION=0.1.0
DEBUG=false
```

---

## 用户角色

| 角色 | 权限 |
|------|------|
| `admin` | 管理所有用户、CF 账号、域名分配、查看所有邮件 |
| `user` | 只能管理自己绑定的资源 |

---

## 验收标准（每个阶段完成后检查）

- [ ] `pytest tests/` 全部通过，无跳过、无警告
- [ ] `uvicorn app.main:app --reload` 能正常启动
- [ ] `http://localhost:8000/api/v1/docs` 能访问 Swagger 文档
- [ ] 所有 SQLAlchemy 模型使用 Mapped[] 类型注解
- [ ] `.env.example` 包含所有必要变量
- [ ] `README.md` 有安装步骤和启动命令
- [ ] 代码中没有 `print()` 调试输出
- [ ] 代码中没有 `TODO` / `FIXME`
- [ ] alembic 初始迁移文件存在且 `alembic upgrade head` 可执行
- [ ] 测试中 CF API 调用全部 Mock，不发真实请求

---

## 重要说明

1. **CF Email Sending 目前是 Beta**，日限 1000 封，README 中需注明
2. **CF API Token 必须加密存储**，使用 `cryptography.fernet.Fernet` 对称加密
3. **Webhook 收件**需要用户在 CF Worker 中配置转发到本平台，文档需说明配置步骤
4. **平台域名分配**给用户时，用户不能看到底层 CF API Token，只能使用域名功能
5. **测试隔离**：每个测试用例使用独立的内存数据库，避免互相干扰