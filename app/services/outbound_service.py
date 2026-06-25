"""发件逻辑（调用 CF Email Sending Beta）。

发件地址必须是平台内当前用户已管理且启用的邮箱地址（full_address）。
据其所属域名解析 CF 账号（可能是平台账号），解密 Token 构造客户端后调用
CF Email Sending（Beta）。日发送配额 1000 封（免费）。
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import NotFoundError
from app.models import CFAccount, Domain, EmailAddress, User
from app.schemas.outbound import SendEmailRequest, SendEmailResult
from app.services.cf_account_service import build_client


async def _resolve_sender(
    session: AsyncSession, user: User, from_address: str
) -> CFAccount:
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

    domain = (
        await session.execute(
            select(Domain).where(Domain.id == email_address.domain_id)
        )
    ).scalar_one_or_none()
    if domain is None:
        raise NotFoundError("域名不存在")

    cf_account = (
        await session.execute(
            select(CFAccount).where(CFAccount.id == domain.cf_account_id)
        )
    ).scalar_one_or_none()
    if cf_account is None:
        raise NotFoundError("CF 账号不存在")
    return cf_account


def _build_send_payload(data: SendEmailRequest) -> dict[str, Any]:
    """构造 CF Email Sending 请求体。"""
    payload: dict[str, Any] = {
        "from": str(data.from_address),
        "to": [str(addr) for addr in data.to],
        "subject": data.subject,
    }
    if data.text is not None:
        payload["text"] = data.text
    if data.html is not None:
        payload["html"] = data.html
    return payload


async def send_email(
    session: AsyncSession, user: User, data: SendEmailRequest
) -> SendEmailResult:
    """发送邮件：校验发件地址归属后调用 CF Email Sending（Beta）。"""
    cf_account = await _resolve_sender(session, user, str(data.from_address))
    client = build_client(cf_account)
    payload = _build_send_payload(data)
    response = await client.send_email(cf_account.account_id, payload)
    return SendEmailResult(
        from_address=str(data.from_address),
        to=[str(addr) for addr in data.to],
        subject=data.subject,
        provider_response=response,
    )
