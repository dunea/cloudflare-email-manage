/**
 * CF Email Manager — Email Worker
 *
 * 接收 Cloudflare Email Routing 投递的邮件，解析后 POST 到平台 Webhook。
 *
 * 流程：
 *   1. email(message, env, ctx) 被 Email Routing 触发
 *   2. 缓冲 message.raw（单次流，必须先 buffer）
 *   3. 用 postal-mime 解析 MIME，提取 subject / text / html
 *   4. 构造 JSON 载荷 {to, from, subject, text, html}
 *   5. 对载荷字节计算 HMAC-SHA256（密钥 = CF_WEBHOOK_SECRET），转十六进制
 *   6. fetch POST 到 WEBHOOK_URL，带 X-Webhook-Signature 头
 *
 * 平台侧签名校验：
 *   hmac.new(CF_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
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

export default {
  /**
   * Email Routing 触发的邮件处理器。
   * @param {ForwardableEmailMessage} message — 收到的邮件
   * @param {Record<string, string>} env — 环境变量（WEBHOOK_URL, CF_WEBHOOK_SECRET）
   * @param {ExecutionContext} ctx — 执行上下文
   */
  async email(message, env, ctx) {
    const webhookUrl = env.WEBHOOK_URL;
    const webhookSecret = env.CF_WEBHOOK_SECRET;

    if (!webhookUrl || !webhookSecret) {
      console.error("缺少 WEBHOOK_URL 或 CF_WEBHOOK_SECRET 环境变量");
      message.setReject("服务器配置不完整");
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
    const signature = await hmacSha256Hex(webhookSecret, bodyBytes);

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
        console.error(
          `Webhook 返回非 2xx: ${resp.status} ${resp.statusText}`,
        );
        const text = await resp.text().catch(() => "");
        console.error("响应体:", text);
      }
    } catch (err) {
      console.error("Webhook 请求失败:", err);
    }
  },
};
