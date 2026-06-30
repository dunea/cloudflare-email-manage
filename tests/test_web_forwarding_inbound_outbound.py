"""前端转发规则 / 收件箱 / 发件页面测试。

CF 调用（create/delete routing rule、send_email）通过 monkeypatch 替换，不发真实请求。
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CFAccount,
    DestinationAddress,
    Domain,
    EmailAddress,
    ForwardingRule,
    InboundEmail,
    OutboundEmail,
    User,
)
from app.services.cloudflare import CloudflareClient
from app.services.crypto import encrypt_token


async def _web_login(
    client: AsyncClient,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "password123",
) -> None:
    await client.post(
        "/register",
        data={"username": username, "email": email, "password": password},
    )
    await client.post("/login", data={"username": username, "password": password})


async def _get_user(db_session: AsyncSession, username: str = "alice") -> User:
    return (
        await db_session.execute(select(User).where(User.username == username))
    ).scalar_one()


async def _seed_email(
    db_session: AsyncSession,
    user_id: int,
    *,
    local: str = "hello",
    domain_name: str = "mine.com",
    is_active: bool = True,
) -> EmailAddress:
    """写库构造 CF 账号 + 域名 + 邮箱地址。"""
    cf = CFAccount(
        user_id=user_id,
        name="acc",
        encrypted_api_token=encrypt_token("t"),
        account_id="acc-1",
    )
    db_session.add(cf)
    await db_session.commit()
    await db_session.refresh(cf)
    domain = Domain(
        cf_account_id=cf.id,
        zone_id="z1",
        domain_name=domain_name,
        status="active",
    )
    db_session.add(domain)
    await db_session.commit()
    await db_session.refresh(domain)
    addr = EmailAddress(
        domain_id=domain.id,
        user_id=user_id,
        local_part=local,
        full_address=f"{local}@{domain_name}",
        public_token=uuid.uuid4().hex,
        is_active=is_active,
    )
    db_session.add(addr)
    await db_session.commit()
    await db_session.refresh(addr)
    return addr


def _patch_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _create(
        self: CloudflareClient, zone_id: str, payload: dict[str, object]
    ) -> dict[str, str]:
        return {"id": "rule-1"}

    async def _delete(
        self: CloudflareClient, zone_id: str, rule_id: str
    ) -> dict[str, str]:
        return {"id": rule_id}

    async def _list_dests(
        self: CloudflareClient, account_id: str
    ) -> list[dict[str, object]]:
        # 返回一个已验证目标地址，满足转发规则创建前的 ensure_verified 校验
        return [
            {
                "id": "cf-dest-dest@example.com",
                "email": "dest@example.com",
                "verified": "2026-06-26T08:00:00Z",
            }
        ]

    monkeypatch.setattr(CloudflareClient, "create_routing_rule", _create)
    monkeypatch.setattr(CloudflareClient, "delete_routing_rule", _delete)
    monkeypatch.setattr(CloudflareClient, "list_destination_addresses", _list_dests)


def _patch_send(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _send(
        self: CloudflareClient, account_id: str, payload: dict[str, object]
    ) -> dict[str, str]:
        return {"id": "msg-1"}

    monkeypatch.setattr(CloudflareClient, "send_email", _send)


# ---- 转发规则 ----


async def test_forwarding_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/forwarding-rules", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


async def test_create_forwarding_rule(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_routing(monkeypatch)
    await _web_login(client)
    user = await _get_user(db_session)
    addr = await _seed_email(db_session, user.id)

    resp = await client.post(
        "/forwarding-rules",
        data={"email_address_id": str(addr.id), "destination_email": "dest@example.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rule = (await db_session.execute(select(ForwardingRule))).scalar_one()
    assert rule.cf_rule_id == "rule-1"

    listing = await client.get("/forwarding-rules")
    assert "hello@mine.com" in listing.text
    assert "dest@example.com" in listing.text


async def test_create_forwarding_invalid_destination(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_routing(monkeypatch)
    await _web_login(client)
    user = await _get_user(db_session)
    addr = await _seed_email(db_session, user.id)

    resp = await client.post(
        "/forwarding-rules",
        data={"email_address_id": str(addr.id), "destination_email": "not-an-email"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    rows = (await db_session.execute(select(ForwardingRule))).scalars().all()
    assert rows == []
    listing = await client.get("/forwarding-rules")
    assert "输入有误" in listing.text


async def test_forwarding_form_filters_destinations_by_selected_account(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """转发规则表单使用当前源邮箱所属账号过滤目标地址。"""
    await _web_login(client)
    user = await _get_user(db_session)
    addr = await _seed_email(db_session, user.id)
    source_domain = (
        await db_session.execute(select(Domain).where(Domain.id == addr.domain_id))
    ).scalar_one()

    other_cf = CFAccount(
        user_id=user.id,
        name="other",
        encrypted_api_token=encrypt_token("t2"),
        account_id="acc-2",
    )
    db_session.add(other_cf)
    await db_session.commit()
    await db_session.refresh(other_cf)

    db_session.add_all(
        [
            DestinationAddress(
                cf_account_id=source_domain.cf_account_id,
                user_id=user.id,
                email="good@example.com",
                cf_address_id="cf-dest-good",
                verified=True,
                verified_at=datetime.now(UTC),
            ),
            DestinationAddress(
                cf_account_id=other_cf.id,
                user_id=user.id,
                email="wrong@example.com",
                cf_address_id="cf-dest-wrong",
                verified=True,
                verified_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.commit()

    listing = await client.get("/forwarding-rules")

    assert 'name="source_email"' in listing.text
    assert 'list="source-email-suggestions"' in listing.text
    assert 'name="destination_email"' in listing.text
    assert 'list="destination-email-suggestions"' in listing.text
    assert "建议仅显示最近 25 个" in listing.text
    assert "建议仅显示前 50 个" in listing.text
    assert "min-h-4" in listing.text
    assert "good@example.com" in listing.text
    assert "wrong@example.com" in listing.text


async def test_forwarding_form_shows_all_sources_bound_hint(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """所有启用源邮箱都已绑定规则时，不显示不可用创建表单。"""
    await _web_login(client)
    user = await _get_user(db_session)
    addr = await _seed_email(db_session, user.id)
    domain = (
        await db_session.execute(select(Domain).where(Domain.id == addr.domain_id))
    ).scalar_one()
    db_session.add(
        DestinationAddress(
            cf_account_id=domain.cf_account_id,
            user_id=user.id,
            email="dest@example.com",
            cf_address_id="cf-dest",
            verified=True,
            verified_at=datetime.now(UTC),
        )
    )
    db_session.add(
        ForwardingRule(
            email_address_id=addr.id,
            destination_email="dest@example.com",
            cf_rule_id="rule-1",
            is_active=True,
        )
    )
    await db_session.commit()

    listing = await client.get("/forwarding-rules")

    assert "所有源邮箱均已绑定转发规则" in listing.text
    assert 'name="source_email"' not in listing.text


async def test_toggle_and_delete_forwarding_rule(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_routing(monkeypatch)
    await _web_login(client)
    user = await _get_user(db_session)
    addr = await _seed_email(db_session, user.id)
    await client.post(
        "/forwarding-rules",
        data={"email_address_id": str(addr.id), "destination_email": "dest@example.com"},
    )
    rule = (await db_session.execute(select(ForwardingRule))).scalar_one()

    await client.post(f"/forwarding-rules/{rule.id}/toggle", follow_redirects=False)
    await db_session.refresh(rule)
    assert rule.is_active is False

    await client.post(f"/forwarding-rules/{rule.id}/delete", follow_redirects=False)
    await db_session.refresh(rule)
    assert rule.is_deleted is True


# ---- 收件箱 ----


async def test_inbound_empty(client: AsyncClient) -> None:
    await _web_login(client)
    resp = await client.get("/inbound")
    assert resp.status_code == 200
    assert "收件箱为空" in resp.text


async def test_inbound_list_and_detail(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _web_login(client)
    user = await _get_user(db_session)
    await _seed_email(db_session, user.id)
    msg = InboundEmail(
        to_address="hello@mine.com",
        from_address="sender@example.com",
        subject="问候",
        body_text="正文内容",
        body_html="<b>hi</b>",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)

    listing = await client.get("/inbound")
    assert listing.status_code == 200
    assert "问候" in listing.text
    assert "查看" in listing.text

    detail = await client.get(f"/inbound/{msg.id}")
    assert detail.status_code == 200
    assert "sender@example.com" in detail.text
    assert "正文内容" in detail.text
    assert "HTML 预览" in detail.text


async def test_inbound_detail_not_found(client: AsyncClient) -> None:
    await _web_login(client)
    resp = await client.get("/inbound/99999")
    assert resp.status_code == 404


# ---- 发件 ----


async def test_outbound_no_senders(client: AsyncClient) -> None:
    await _web_login(client)
    resp = await client.get("/outbound/compose")
    assert resp.status_code == 200
    assert "没有可用的发件地址建议" in resp.text


async def test_outbound_send_success(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_send(monkeypatch)
    await _web_login(client)
    user = await _get_user(db_session)
    await _seed_email(db_session, user.id)

    resp = await client.post(
        "/outbound",
        data={
            "from_address": "hello@mine.com",
            "to": "dest@example.com",
            "subject": "测试主题",
            "text": "你好",
            "html": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/outbound"
    page = await client.get("/outbound")
    assert "邮件已发送" in page.text
    assert "发件箱" in page.text
    assert "测试主题" in page.text
    assert "查看" in page.text

    record = (await db_session.execute(select(OutboundEmail))).scalar_one()
    detail = await client.get(f"/outbound/{record.id}")
    assert detail.status_code == 200
    assert "dest@example.com" in detail.text
    assert "你好" in detail.text


async def test_outbound_send_requires_body(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _web_login(client)
    user = await _get_user(db_session)
    await _seed_email(db_session, user.id)

    resp = await client.post(
        "/outbound",
        data={
            "from_address": "hello@mine.com",
            "to": "dest@example.com",
            "subject": "无正文",
            "text": "",
            "html": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "输入有误" in resp.text


async def test_outbound_send_unmanaged_from(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _web_login(client)
    user = await _get_user(db_session)
    await _seed_email(db_session, user.id)

    resp = await client.post(
        "/outbound",
        data={
            "from_address": "ghost@other.com",
            "to": "dest@example.com",
            "subject": "越权发件",
            "text": "x",
            "html": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "发件地址不存在或不可用" in resp.text
