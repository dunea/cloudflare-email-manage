"""收件处理逻辑：Webhook 签名校验、入库与查询。

Webhook 端点需校验签名：请求头 X-Webhook-Signature 为对原始请求体的
HMAC-SHA256 十六进制摘要，使用常量时间比较。
签名密钥按收件地址的域名 + 载荷中的 zone_id 唯一定位 Domain.webhook_secret
（per-domain，且天然避开多账号同名域名歧义），
未匹配时回退到全局 CF_WEBHOOK_SECRET（兼容旧部署）。
收到的邮件按 to_address 是否归属当前用户的邮箱地址进行隔离查询。
"""

import hashlib
import hmac
import json

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.config import settings
from app.exceptions import AppException, AuthError, NotFoundError
from app.models import Domain, EmailAddress, InboundEmail, User
from app.schemas.inbound_email import InboundEmailPayload

# Webhook 签名请求头名称
WEBHOOK_SIGNATURE_HEADER = "X-Webhook-Signature"


def _expected_signature(raw_body: bytes, secret: str) -> str:
    """根据指定密钥计算请求体的 HMAC-SHA256 十六进制摘要。"""
    return hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """常量时间比较 Webhook 签名是否匹配。"""
    if not signature:
        return False
    return hmac.compare_digest(_expected_signature(raw_body, secret), signature)


def _peek_field(raw_body: bytes, field: str) -> str:
    """从原始请求体中安全地读取指定字段（仅用于定位签名密钥，不信任）。"""
    try:
        data = json.loads(raw_body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get(field)
    return str(value) if value is not None else ""


def _peek_to_address(raw_body: bytes) -> str:
    """从原始请求体中安全地读取 to 字段（仅用于定位签名密钥，不信任）。"""
    return _peek_field(raw_body, "to")


def _peek_zone_id(raw_body: bytes) -> str:
    """从原始请求体中安全地读取 zone_id 字段（仅用于定位签名密钥，不信任）。"""
    return _peek_field(raw_body, "zone_id")


def _extract_domain_part(to_address: str) -> str:
    """从收件地址中提取域名（小写），失败返回空串。"""
    if not to_address or "@" not in to_address:
        return ""
    return to_address.rsplit("@", 1)[1].strip().lower()


async def _resolve_secret(
    session: AsyncSession, to_address: str, zone_id: str | None = None
) -> str:
    """根据 zone_id + 收件域名查找签名密钥，未匹配则回退到全局密钥。

    Worker 在 webhook 载荷中附带 zone_id（与签名 secret 同源），
    平台按 ``(zone_id, domain_name)`` 唯一定位 Domain.webhook_secret，
    避免多账号同名域名（如管理员与用户都绑了 example.com）时选错密钥。

    - 新部署 Worker 必传 zone_id：``(zone_id, domain_name)`` 命中 → 用该域 secret；
      该域名在 DB 中有非空 secret，但 Worker 用的 zone_id 与之不匹配 → fail-close
      （返回空串，调用方 401），配置错误时不应静默用全局密钥兜底。
    - 该域名存在但 secret=NULL / zone_id 不匹配但该域名无任何非空 secret
      → 回退到全局 CF_WEBHOOK_SECRET。
    - 旧部署 Worker 不传 zone_id 时按域名降级匹配首条非空 secret
      （保持向后兼容；新部署须经 worker_deploy_service 写入 zone_id）。
    - 域名完全不在 DB（陌生发件人）→ 回退到全局 CF_WEBHOOK_SECRET。
    """
    domain_part = _extract_domain_part(to_address)
    if not domain_part:
        return settings.CF_WEBHOOK_SECRET

    if zone_id:
        # (zone_id, domain_name) 精确命中 → 直接返回该域 secret
        stmt = select(Domain.webhook_secret).where(
            Domain.zone_id == zone_id,
            func.lower(Domain.domain_name) == domain_part,
            Domain.webhook_secret.is_not(None),
        )
        result = (await session.execute(stmt)).scalars().first()
        if result:
            return result
        # 未命中：检查该域名是否在 DB 中有非空 secret（即 zone_id 不匹配）
        other_secret = (
            await session.execute(
                select(Domain.webhook_secret)
                .where(
                    func.lower(Domain.domain_name) == domain_part,
                    Domain.webhook_secret.is_not(None),
                )
                .limit(1)
            )
        ).scalars().first()
        if other_secret is not None:
            # 域名存在但 zone_id 不匹配 → 配置错误，fail-close
            return ""
        return settings.CF_WEBHOOK_SECRET

    # 旧 Worker 兼容：按域名降级匹配首条非空 secret
    stmt = (
        select(Domain.webhook_secret)
        .where(
            func.lower(Domain.domain_name) == domain_part,
            Domain.webhook_secret.is_not(None),
        )
        .order_by(Domain.id)
        .limit(1)
    )
    result = (await session.execute(stmt)).scalars().first()
    if result:
        return result
    return settings.CF_WEBHOOK_SECRET


async def process_webhook(
    session: AsyncSession, raw_body: bytes, signature: str | None
) -> InboundEmail:
    """校验签名、解析载荷并存储收到的邮件。

    先从请求体中读取 to/zone_id 字段以定位签名密钥，再校验签名，
    最后用 Pydantic 严格解析载荷入库。
    """
    to_address = _peek_to_address(raw_body)
    zone_id = _peek_zone_id(raw_body)
    secret = await _resolve_secret(session, to_address, zone_id)

    if not verify_signature(raw_body, signature, secret):
        raise AuthError("Webhook 签名校验失败")

    try:
        payload = InboundEmailPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        raise AppException(
            f"Webhook 载荷无效: {exc.errors()}", code=1422, http_status=422
        ) from exc

    email = InboundEmail(
        to_address=str(payload.to_address).lower(),
        from_address=str(payload.from_address).lower(),
        subject=payload.subject,
        body_text=payload.body_text,
        body_html=payload.body_html,
    )
    session.add(email)
    await session.commit()
    await session.refresh(email)
    return email


def _accessible_stmt(user: User) -> Select[tuple[InboundEmail]]:
    """构造按 to_address 归属过滤的收件查询（管理员可见全部）。"""
    stmt = select(InboundEmail)
    if user.role != "admin":
        owned = (
            select(func.lower(EmailAddress.full_address))
            .where(EmailAddress.user_id == user.id)
            .scalar_subquery()
        )
        stmt = stmt.where(func.lower(InboundEmail.to_address).in_(owned))
    return stmt


async def get_inbound_email_or_404(
    session: AsyncSession, email_id: int, user: User
) -> InboundEmail:
    """按 id 查询收件邮件并校验归属。"""
    stmt = _accessible_stmt(user).where(InboundEmail.id == email_id)
    email = (await session.execute(stmt)).scalar_one_or_none()
    if email is None:
        raise NotFoundError("邮件不存在")
    return email


async def list_inbound_emails(
    session: AsyncSession,
    user: User,
    page: int,
    size: int,
    to_address: str | None = None,
) -> tuple[list[InboundEmail], int]:
    """分页查询收到的邮件；按归属隔离，可按 to_address 过滤。"""
    base = _accessible_stmt(user)
    if to_address is not None:
        base = base.where(func.lower(InboundEmail.to_address) == to_address.lower())

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    result = await session.execute(
        base.order_by(InboundEmail.id.desc()).offset((page - 1) * size).limit(size)
    )
    return list(result.scalars().all()), total


async def get_latest_inbound_by_address(
    session: AsyncSession, full_address: str
) -> InboundEmail | None:
    """按收件地址取最新一封邮件（按 received_at / id 倒序）。

    地址比较大小写不敏感（邮件协议中域名部分不区分大小写，
    多数实现 local-part 也不区分）。
    """
    stmt = (
        select(InboundEmail)
        .where(func.lower(InboundEmail.to_address) == full_address.lower())
        .order_by(InboundEmail.received_at.desc(), InboundEmail.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
