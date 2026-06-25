# CF Email Manager — Email Worker 示例

本目录包含一个可直接部署的 Cloudflare Email Worker，用于将 CF Email Routing
收到的邮件转发到 CF Email Manager 平台的 Webhook 端点。

## 架构

```
外部发件人
  → Cloudflare Email Routing（收到邮件）
  → 触发本 Worker 的 email() handler
  → postal-mime 解析 MIME（提取 subject / text / html）
  → 构造 JSON {to, from, subject, text, html}
  → HMAC-SHA256 签名
  → POST 平台 /api/v1/inbound/webhook
  → 平台校验签名后入库
  → 用户通过公开链接 /mail/{token} 查看邮件
```

## 前提条件

- 已安装 Node.js 18+
- 已安装 wrangler（`npm install -g wrangler` 或用 npx）
- 已 `wrangler login` 登录 Cloudflare 账号
- 域名已接入 Cloudflare 且已启用 Email Routing
- CF Email Manager 平台已部署并可访问

## 部署步骤

### 1. 安装依赖

```bash
cd examples/worker
npm install
```

### 2. 修改配置

编辑 `wrangler.toml`，将 `WEBHOOK_URL` 改为你的平台地址：

```toml
[vars]
WEBHOOK_URL = "https://your-platform-domain.com/api/v1/inbound/webhook"
```

> 本地开发可用 `http://localhost:8000/api/v1/inbound/webhook`

### 3. 设置 Webhook 密钥

将平台 `.env` 中的 `CF_WEBHOOK_SECRET` 值设为 Worker 的 secret：

```bash
npx wrangler secret put CF_WEBHOOK_SECRET
# 粘贴平台 .env 中 CF_WEBHOOK_SECRET 的值（如 29135d04-7cf8-4427-9788-630732ac4ef8）
```

> ⚠️ Worker 端的 `CF_WEBHOOK_SECRET` 必须与平台 `.env` 中的**完全一致**，
> 否则签名校验失败（401），邮件不入库。

### 4. 部署 Worker

```bash
npx wrangler deploy
```

部署完成后会输出 Worker URL，如：
`https://cf-email-manager-webhook.<your-subdomain>.workers.dev`

### 5. 配置 Email Routing 路由规则

进入 Cloudflare Dashboard：

1. 选择你的域名
2. 左侧菜单 **Email** → **Email Routing**
3. 确保已启用 Email Routing
4. 进入 **Routing Rules** 标签页
5. 添加规则：
   - **匹配条件**：自定义地址（如 `hello@example.com`）或 Catch-all
   - **操作**：发送到 Worker → 选择刚部署的 `cf-email-manager-webhook`
6. 保存

> Catch-all 规则会将域名下所有未匹配的地址都投递给 Worker，
> 适合平台用户创建任意邮箱地址的场景。

### 6. 验证

1. 在 CF Email Manager 平台创建一个邮箱地址（如 `hello@example.com`）
2. 用外部邮箱（如 Gmail）向该地址发一封测试邮件
3. 等待几秒，访问公开查询链接 `/mail/{token}` 或登录平台查看收件箱
4. 如果看到邮件 → 配置成功
5. 如果看不到邮件 → 查看下面的排查指南

## 排查指南

### 邮件查不到

| 现象 | 可能原因 | 排查方法 |
|------|---------|---------|
| 平台显示「暂无邮件」 | Worker 未部署或路由规则未配置 | 确认步骤 4-5 已完成 |
| 平台显示「暂无邮件」 | 签名不匹配（401） | 确认 `CF_WEBHOOK_SECRET` 两端一致 |
| 平台显示「暂无邮件」 | `WEBHOOK_URL` 错误或平台不可达 | 从 Worker 所在网络 curl 测试 |
| Worker 日志报错 | 邮件解析失败 | 查 `wrangler tail` 实时日志 |
| 发件人收到退信 | Email Routing 未启用或域名 DNS 有误 | 检查 MX 讀记录 |

### 查看实时日志

```bash
npx wrangler tail
```

这会实时显示 Worker 的 `console.log` / `console.error` 输出，发一封测试邮件
即可看到处理流程和错误信息。

### 手动测试 Webhook

不通过 Worker，直接用 curl 模拟 Webhook 请求（验证平台端是否正常）：

```bash
# 计算签名（需要与平台 .env 中的 CF_WEBHOOK_SECRET 一致）
SECRET="29135d04-7cf8-4427-9788-630732ac4ef8"
BODY='{"to":"hello@example.com","from":"test@gmail.com","subject":"测试","text":"正文","html":"<p>正文</p>"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/.* //')

curl -X POST http://localhost:8000/api/v1/inbound/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: $SIG" \
  -d "$BODY"
```

> Windows PowerShell 可用：
> ```powershell
> $secret = "29135d04-7cf8-4427-9788-630732ac4ef8"
> $body = '{"to":"hello@example.com","from":"test@gmail.com","subject":"测试","text":"正文"}'
> $hmac = New-Object System.Security.Cryptography.HMACSHA256
> $hmac.Key = [Text.Encoding]::UTF8.GetBytes($secret)
> $sig = ([BitConverter]::ToString($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($body))) -replace '-','').ToLower()
> Invoke-RestMethod -Uri "http://localhost:8000/api/v1/inbound/webhook" -Method Post -Headers @{"X-Webhook-Signature"=$sig; "Content-Type"="application/json"} -Body $body
> ```

返回 `{"code":0,"data":{...}}` 说明平台端正常，问题在 Worker 端。

## 文件说明

| 文件 | 用途 |
|------|------|
| `src/index.js` | Worker 代码：email handler + MIME 解析 + 签名 + POST |
| `wrangler.toml` | wrangler 配置：Worker 名称、环境变量 |
| `package.json` | 依赖声明（postal-mime + wrangler） |
