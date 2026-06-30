"""发件逻辑（调用 CF Email Sending Beta）与发件箱查询。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.exceptions import AppException, NotFoundError
from app.models import CFAccount, Domain, EmailAddress, OutboundEmail, User
from app.schemas.outbound import SendEmailRequest, SendEmailResult
from app.services.cf_account_service import build_client


async def _resolve_sender(
    session: AsyncSession, user: User, from_address: str
) -> tuple[CFAccount, EmailAddress]:
    """校验发件地址归属当前用户，并解析其所属 CF 账号。"""
    stmt = select(EmailAddress).where(
        func.lower(EmailAddress.full_address) == from_address.lower(),
        EmailAddress.is_deleted.is_(False),
        EmailAddress.is_active.is_(True),
    )
    if user.role != "admin":
        stmt = stmt.where(EmailAddress.user_id == user.id)
    email_address = (await session.execute(stmt)).scalar_one_or_none()
    if email_address is None:
        raise NotFoundError("发件地址不存在或不可用")

    cf_account = await _cf_account_for_email_address(session, email_address)
    return cf_account, email_address


async def _cf_account_for_email_address(
    session: AsyncSession, email_address: EmailAddress
) -> CFAccount:
    """根据邮箱地址解析所属 CF 账号。"""
    domain = (
        await session.execute(
            select(Domain).where(Domain.id == email_address.domain_id)
        )
    ).scalar_one_or_none()
    if domain is None:
        raise NotFoundError("域名不存在")

    cf_account = (
        await session.execute(
            select(CFAccount).where(
                CFAccount.id == domain.cf_account_id,
                CFAccount.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if cf_account is None:
        raise NotFoundError("CF 账号不存在")
    return cf_account


def _build_send_payload(data: SendEmailRequest) -> dict[str, object]:
    """构造 CF Email Sending 请求体。"""
    payload: dict[str, object] = {
        "from": str(data.from_address),
        "to": [str(addr) for addr in data.to],
        "subject": data.subject,
    }
    if data.text is not None:
        payload["text"] = data.text
    if data.html is not None:
        payload["html"] = data.html
    return payload


def _json_dumps(value: object) -> str:
    """稳定序列化 JSON 字段。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _new_outbound_record(
    email_address: EmailAddress, data: SendEmailRequest
) -> OutboundEmail:
    """创建初始发件记录，调用方负责加入 session。"""
    return OutboundEmail(
        user_id=email_address.user_id,
        from_address=str(data.from_address).lower(),
        to_addresses_json=_json_dumps([str(addr).lower() for addr in data.to]),
        subject=data.subject,
        body_text=data.text,
        body_html=data.html,
        status="sending",
    )


async def _send_with_resolved_sender(
    session: AsyncSession,
    cf_account: CFAccount,
    email_address: EmailAddress,
    data: SendEmailRequest,
) -> SendEmailResult:
    """在已校验发件地址的前提下发送邮件并记录发件箱。"""
    client = build_client(cf_account)
    outbound = _new_outbound_record(email_address, data)
    session.add(outbound)

    payload = _build_send_payload(data)
    try:
        response = await client.send_email(cf_account.account_id, payload)
    except AppException as exc:
        outbound.status = "failed"
        outbound.error_message = exc.message
        await session.commit()
        await session.refresh(outbound)
        raise

    outbound.status = "sent"
    outbound.provider_response_json = _json_dumps(response)
    outbound.sent_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(outbound)
    return SendEmailResult(
        from_address=str(data.from_address),
        to=[str(addr) for addr in data.to],
        subject=data.subject,
        status="sent",
        outbound_email_id=outbound.id,
        provider_response=response,
    )


async def send_email(
    session: AsyncSession, user: User, data: SendEmailRequest
) -> SendEmailResult:
    """发送邮件：校验发件地址归属后调用 CF Email Sending（Beta）。"""
    cf_account, email_address = await _resolve_sender(
        session, user, str(data.from_address)
    )
    return await _send_with_resolved_sender(session, cf_account, email_address, data)


async def send_email_from_address(
    session: AsyncSession, email_address: EmailAddress, data: SendEmailRequest
) -> SendEmailResult:
    """从已解析的邮箱地址发件，用于公开 token 页面。"""
    if not email_address.is_active or email_address.is_deleted:
        raise NotFoundError("发件地址不存在或不可用")
    if str(data.from_address).lower() != email_address.full_address.lower():
        raise NotFoundError("发件地址不存在或不可用")
    cf_account = await _cf_account_for_email_address(session, email_address)
    return await _send_with_resolved_sender(session, cf_account, email_address, data)


def _accessible_stmt(user: User) -> Select[tuple[OutboundEmail]]:
    """构造按用户归属过滤的发件箱查询（管理员可见全部）。"""
    stmt = select(OutboundEmail)
    if user.role != "admin":
        stmt = stmt.where(OutboundEmail.user_id == user.id)
    return stmt


async def list_outbound_emails(
    session: AsyncSession,
    user: User,
    page: int,
    size: int,
    from_address: str | None = None,
) -> tuple[list[OutboundEmail], int]:
    """分页查询当前用户发件箱，可按发件地址过滤。"""
    base = _accessible_stmt(user)
    if from_address is not None:
        base = base.where(func.lower(OutboundEmail.from_address) == from_address.lower())
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    result = await session.execute(
        base.order_by(OutboundEmail.id.desc()).offset((page - 1) * size).limit(size)
    )
    return list(result.scalars().all()), total


async def get_outbound_email_or_404(
    session: AsyncSession, email_id: int, user: User
) -> OutboundEmail:
    """按 id 查询发件邮件并校验归属。"""
    stmt = _accessible_stmt(user).where(OutboundEmail.id == email_id)
    email = (await session.execute(stmt)).scalar_one_or_none()
    if email is None:
        raise NotFoundError("邮件不存在")
    return email


async def list_outbound_emails_by_address(
    session: AsyncSession,
    full_address: str,
    page: int,
    size: int,
) -> tuple[list[OutboundEmail], int]:
    """按单个发件地址分页查询发件箱，用于公开邮箱查询页。"""
    base = select(OutboundEmail).where(
        func.lower(OutboundEmail.from_address) == full_address.lower()
    )
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    result = await session.execute(
        base.order_by(OutboundEmail.id.desc()).offset((page - 1) * size).limit(size)
    )
    return list(result.scalars().all()), total
