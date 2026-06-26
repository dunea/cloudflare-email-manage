# CF Email Manager — Email Worker 源码

本目录是 CF Email Manager 平台收件 Worker 的**源码参考**与 bundle 构建源。

- **源码**：`src/index.js`（账号级 Worker，根据收件地址域名在
  `WEBHOOK_SECRETS` JSON 映射中查找对应的签名密钥）
- **bundle 产物**：`app/assets/email_worker.bundle.js`（后端一键部署时上传到 CF）

## 推荐：在平台前端一键部署

平台已支持一键部署 Worker。直接登录平台 → CF 账号详情 → 点击「一键部署 Worker」
即可，平台会用你已绑定的 CF API Token 自动：

1. 启用各域名 Email Routing
2. 上传 Worker 脚本（含 `WEBHOOK_URL` binding）
3. 注入 `WEBHOOK_SECRETS`（域名→签名密钥 JSON）
4. 为每个域名配置 catch-all → Worker

无需本机 Node 环境，无需 wrangler，无需手动配路由。

## 手动部署（降级方案）

仅在 Token 权限不足或 API 部署失败时使用。

### 1. 安装依赖并打包

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

### 3. 设置密钥

Worker 需要两个环境变量/secret：
- `WEBHOOK_URL`（plain_text，平台收件地址）— 在 `wrangler.toml` 的 `[vars]` 中配置
- `WEBHOOK_SECRETS`（secret，JSON 映射 `{"example.com":"<密钥1>","foo.com":"<密钥2>"}`）
  — 通过 `npx wrangler secret put WEBHOOK_SECRETS` 设置

> ⚠️ `WEBHOOK_SECRETS` JSON 中的每个域名密钥必须与平台 `Domain.webhook_secret`
> **完全一致**；缺失域名时该收件地址会被 Worker 拒绝投递。

### 4. 部署 Worker

```bash
npx wrangler deploy
```

### 5. 配置 Email Routing 路由规则

进入 Cloudflare Dashboard → 选择域名 → Email → Email Routing → Routing Rules：
添加 catch-all 规则，操作选择「发送到 Worker」并指定刚部署的 Worker。

## 重建 bundle 产物

后端一键部署读取的是 `app/assets/email_worker.bundle.js`（含 postal-mime）。
修改源码后需重新打包：

```bash
cd examples/worker
npx esbuild src/index.js --bundle --format=esm --platform=browser \
  --target=es2022 --outfile=../../app/assets/email_worker.bundle.js
```

> 需要 Node 18+；esbuild 通过 npx 临时下载，无需全局安装。

## 架构

```text
外部发件人
  → Cloudflare Email Routing（catch-all → Worker）
  → 触发本 Worker 的 email() handler
  → 从 message.to 提取域名，查 WEBHOOK_SECRETS 找密钥
  → postal-mime 解析 MIME（提取 subject / text / html）
  → 构造 JSON {to, from, subject, text, html}
  → HMAC-SHA256(域名密钥) 签名
  → POST 平台 /api/v1/inbound/webhook
  → 平台按收件域名查 Domain.webhook_secret 验签 → 入库
  → 用户通过公开链接 /mail/{token} 查看邮件
```

## 排查

如邮件未入库：

| 现象 | 可能原因 | 排查 |
|------|---------|------|
| Worker 报错「未找到域名 X 对应的签名密钥」 | `WEBHOOK_SECRETS` 中缺少该域名 | 同步域名后在平台重新一键部署 |
| 平台返回 401 | 签名不匹配 | 确认 Worker 的 `WEBHOOK_SECRETS` 中该域名密钥与平台 `Domain.webhook_secret` 一致 |
| 平台返回 401 | `WEBHOOK_URL` 不可达 | 从 Worker 所在网络 curl 测试 |
| 平台返回 422 | 载荷结构异常 | 用 `wrangler tail` 查看请求体 |

```bash
npx wrangler tail    # 实时日志
```

## 排查指南

### 邮件查不到

| 现象 | 可能原因 | 排查方法 |
|------|---------|---------|
| 平台显示「暂无邮件」 | Worker 未部署或路由规则未配置 | 确认步骤 4-5 已完成 |
| 平台显示「暂无邮件」 | 签名不匹配（401） | 确认 Worker `WEBHOOK_SECRETS` 中该域名密钥与平台 `Domain.webhook_secret` 一致 |
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
# 计算签名（密钥需与平台 domain 表中 example.com 的 webhook_secret 一致）
SECRET="<domain-webhook-secret>"
BODY='{"to":"hello@example.com","from":"test@gmail.com","subject":"测试","text":"正文","html":"<p>正文</p>"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/.* //')

curl -X POST http://localhost:8000/api/v1/inbound/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: $SIG" \
  -d "$BODY"
```

> Windows PowerShell 可用：
> ```powershell
> $secret = "<domain-webhook-secret>"
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
