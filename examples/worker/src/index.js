/**
 * CF Email Manager — 账号级 Email Worker
 *
 * 一个 CF 账号部署一个 Worker，服务该账号下已启用收件路由的邮箱域名。
 * Worker 持有两个环境变量：
 *   - WEBHOOK_URL：平台收件 Webhook 端点（plain_text）
 *   - WEBHOOK_SECRETS：域名→{zone_id, secret} 的 JSON 映射（secret，例如
 *                     {"example.com":{"zone_id":"abc...","secret":"xyz..."},
 *                      "foo.com":    {"zone_id":"def...","secret":"uvw..."}}）
 *
 * 流程：
 *   1. email(message, env, ctx) 被 Email Routing 触发
 *   2. 从 message.to 提取域名，查 WEBHOOK_SECRETS 找到该域名的 {zone_id, secret}
 *   3. 缓冲 message.raw（单次流，必须先 buffer）
 *   4. 用 postal-mime 解析 MIME，提取 subject / text / html
 *   5. 构造 JSON 载荷 {to, from, envelope_from, from_name, reply_to, message_id, zone_id, subject, text, html}
 *   6. 用该域名密钥计算 HMAC-SHA256，转十六进制
 *   7. fetch POST 到 WEBHOOK_URL，带 X-Webhook-Signature 头
 *
 * 平台侧签名校验：
 *   hmac.new(domain.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
 *   平台按 (zone_id, domain_name) 唯一定位 Domain.webhook_secret，
 *   zone_id 由本 Worker 在载荷中提供（与签名 secret 来自同一配置项），
 *   避免多账号同名域名（如管理员/用户都绑了 example.com）时选错密钥。
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
 * 每项值形如 {zone_id, secret}，两者都是必填字符串。
 * @param {string | undefined} raw
 * @returns {Record<string, {zone_id: string, secret: string}>}
 */
function parseSecrets(raw) {
  if (!raw) return {};
  try {
    const obj = JSON.parse(raw);
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      const out = {};
      for (const key of Object.keys(obj)) {
        const value = obj[key];
        if (
          value &&
          typeof value === "object" &&
          typeof value.zone_id === "string" &&
          typeof value.secret === "string"
        ) {
          out[key.toLowerCase()] = {
            zone_id: value.zone_id,
            secret: value.secret,
          };
        }
      }
      return out;
    }
  } catch (err) {
    console.error("WEBHOOK_SECRETS 解析失败:", err);
  }
  return {};
}

/**
 * 清理并截断可选文本字段。
 * @param {unknown} value
 * @param {number} maxLength
 * @returns {string}
 */
function metadataText(value, maxLength) {
  if (typeof value !== "string") return "";
  return value.trim().slice(0, maxLength);
}

/**
 * 从 postal-mime 地址对象中取邮箱地址。
 * @param {unknown} value
 * @returns {string}
 */
function normalizeAddress(value) {
  let address = "";
  if (value && typeof value === "object" && typeof value.address === "string") {
    address = value.address;
  } else if (typeof value === "string") {
    address = value;
  }
  return metadataText(address, 320);
}

/**
 * 从 postal-mime 地址对象中取展示名称。
 * @param {unknown} value
 * @returns {string}
 */
function addressName(value) {
  if (value && typeof value === "object" && typeof value.name === "string") {
    return metadataText(value.name, 255);
  }
  return "";
}

/**
 * 取 postal-mime 地址列表中的第一个邮箱地址。
 * @param {unknown} value
 * @returns {string}
 */
function firstAddress(value) {
  if (!Array.isArray(value) || value.length === 0) return "";
  return normalizeAddress(value[0]);
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

    // 从收件地址提取域名，查找对应的 {zone_id, secret}
    const domain = extractDomain(message.to);
    const entry = domain ? webhookSecrets[domain] : undefined;
    if (!entry) {
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
    // zone_id 来自该域名的配置项，与签名 secret 同源，确保平台侧
    // 能按 (zone_id, domain_name) 唯一定位 Domain.webhook_secret。
    const headerFrom = normalizeAddress(parsed.from) || message.from;
    const payload = {
      to: message.to,
      from: headerFrom,
      from_name: addressName(parsed.from),
      envelope_from: metadataText(message.from, 320),
      reply_to: firstAddress(parsed.replyTo),
      message_id: metadataText(
        parsed.messageId || message.headers.get("message-id") || "",
        255,
      ),
      zone_id: entry.zone_id,
      subject: parsed.subject || "",
      text: parsed.text || "",
      html: parsed.html || "",
    };

    // 序列化为 JSON 字节（平台对原始字节做签名校验，必须与此完全一致）
    const bodyBytes = new TextEncoder().encode(JSON.stringify(payload));

    // 计算 HMAC-SHA256 签名
    const signature = await hmacSha256Hex(entry.secret, bodyBytes);

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
