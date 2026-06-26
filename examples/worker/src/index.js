/**
 * CF Email Manager — 账号级 Email Worker
 *
 * 一个 CF 账号部署一个 Worker，服务该账号下所有域名。
 * Worker 持有两个环境变量：
 *   - WEBHOOK_URL：平台收件 Webhook 端点（plain_text）
 *   - WEBHOOK_SECRETS：域名→签名密钥 的 JSON 映射（secret，例如
 *                     {"example.com":"abc...","foo.com":"xyz..."}）
 *
 * 流程：
 *   1. email(message, env, ctx) 被 Email Routing 触发
 *   2. 从 message.to 提取域名，查 WEBHOOK_SECRETS 找到该域名的密钥
 *   3. 缓冲 message.raw（单次流，必须先 buffer）
 *   4. 用 postal-mime 解析 MIME，提取 subject / text / html
 *   5. 构造 JSON 载荷 {to, from, subject, text, html}
 *   6. 用该域名密钥计算 HMAC-SHA256，转十六进制
 *   7. fetch POST 到 WEBHOOK_URL，带 X-Webhook-Signature 头
 *
 * 平台侧签名校验：
 *   hmac.new(domain.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
 */

import PostalMime from "postal-mime";

/**
 * 将 ArrayBuffer 转为十六进制字符串。
 * @param {ArrayBuffer} buffer
 * @returns {string}
 */
function bufferToHex(buffer) {
  const bytes = new Uint8Array(buffer);
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * 用 Web Crypto API 计算 HMAC-SHA256 十六进制摘要。
 * @param {string} secret — HMAC 密钥
 * @param {Uint8Array} data — 要签名的字节
 * @returns {Promise<string>} 十六进制摘要
 */
async function hmacSha256Hex(secret, data) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, data);
  return bufferToHex(signature);
}

/**
 * 从收件地址提取域名（小写）。
 * @param {string} address — 例如 "hello@example.com"
 * @returns {string} — 域名部分，未取到返回空串
 */
function extractDomain(address) {
  if (typeof address !== "string") return "";
  const at = address.lastIndexOf("@");
  if (at < 0) return "";
  return address.slice(at + 1).toLowerCase().trim();
}

/**
 * 解析 WEBHOOK_SECRETS（JSON 字符串）为字典对象，键统一小写。
 * @param {string | undefined} raw
 * @returns {Record<string, string>}
 */
function parseSecrets(raw) {
  if (!raw) return {};
  try {
    const obj = JSON.parse(raw);
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      const out = {};
      for (const key of Object.keys(obj)) {
        const value = obj[key];
        if (typeof value === "string") {
          out[key.toLowerCase()] = value;
        }
      }
      return out;
    }
  } catch (err) {
    console.error("WEBHOOK_SECRETS 解析失败:", err);
  }
  return {};
}

export default {
  /**
   * Email Routing 触发的邮件处理器。
   * @param {ForwardableEmailMessage} message — 收到的邮件
   * @param {Record<string, string>} env — 环境变量
   * @param {ExecutionContext} ctx — 执行上下文
   */
  async email(message, env, _ctx) {
    const webhookUrl = env.WEBHOOK_URL;
    const webhookSecrets = parseSecrets(env.WEBHOOK_SECRETS);

    if (!webhookUrl) {
      console.error("缺少 WEBHOOK_URL 环境变量");
      message.setReject("服务器配置不完整");
      return;
    }
    if (Object.keys(webhookSecrets).length === 0) {
      console.error("WEBHOOK_SECRETS 为空或无效");
      message.setReject("签名密钥未配置");
      return;
    }

    // 从收件地址提取域名，查找对应的签名密钥
    const domain = extractDomain(message.to);
    const secret = domain ? webhookSecrets[domain] : undefined;
    if (!secret) {
      console.error(`未找到域名 ${domain} 对应的签名密钥`);
      message.setReject("该域名未配置收件");
      return;
    }

    // message.raw 是单次流，必须先缓冲
    const rawBuffer = await new Response(message.raw).arrayBuffer();

    // 用 postal-mime 解析 MIME
    let parsed;
    try {
      parsed = await PostalMime.parse(rawBuffer);
    } catch (err) {
      console.error("邮件解析失败:", err);
      message.setReject("邮件解析失败");
      return;
    }

    // 构造平台 Webhook 载荷（字段名与 InboundEmailPayload 一致）
    const payload = {
      to: message.to,
      from: message.from,
      subject: parsed.subject || "",
      text: parsed.text || "",
      html: parsed.html || "",
    };

    // 序列化为 JSON 字节（平台对原始字节做签名校验，必须与此完全一致）
    const bodyBytes = new TextEncoder().encode(JSON.stringify(payload));

    // 计算 HMAC-SHA256 签名
    const signature = await hmacSha256Hex(secret, bodyBytes);

    // POST 到平台 Webhook
    try {
      const resp = await fetch(webhookUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Webhook-Signature": signature,
        },
        body: bodyBytes,
      });

      if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        console.error(
          `Webhook 返回非 2xx: ${resp.status} ${resp.statusText}`,
          text,
        );
        message.setReject(`Webhook 投递失败: ${resp.status}`);
        return;
      }
    } catch (err) {
      console.error("Webhook 请求失败:", err);
      message.setReject("Webhook 请求失败");
    }
  },
};