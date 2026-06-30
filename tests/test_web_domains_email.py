"""前端域名与邮箱地址页面测试：列表 / 详情 / 分配 / 邮箱 CRUD。

域名由测试直接写库（绕过 CF 同步），不发真实网络请求。
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CFAccount, Domain, DomainAssignment, EmailAddress, User
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


async def _seed_domain(
    db_session: AsyncSession,
    user_id: int,
    *,
    domain_name: str = "example.com",
    zone_id: str = "z1",
) -> Domain:
    """直接写库构造一个 CF 账号 + 域名。"""
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
        zone_id=zone_id,
        domain_name=domain_name,
        status="active",
    )
    db_session.add(domain)
    await db_session.commit()
    await db_session.refresh(domain)
    return domain


# ---- 域名 ----


async def test_domains_list_empty(client: AsyncClient) -> None:
    await _web_login(client)
    resp = await client.get("/domains")
    assert resp.status_code == 200
    assert "还没有域名" in resp.text


async def test_domains_list_shows_domain(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _web_login(client)
    user = await _get_user(db_session)
    await _seed_domain(db_session, user.id, domain_name="mine.com")
    resp = await client.get("/domains")
    assert resp.status_code == 200
    assert "mine.com" in resp.text


async def test_domain_detail(client: AsyncClient, db_session: AsyncSession) -> None:
    await _web_login(client)
    user = await _get_user(db_session)
    domain = await _seed_domain(db_session, user.id, domain_name="mine.com")
    resp = await client.get(f"/domains/{domain.id}")
    assert resp.status_code == 200
    assert "mine.com" in resp.text


async def test_domain_detail_shows_email_preview_for_large_domain(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """域名详情只展示邮箱预览，完整列表交给分页邮箱地址页。"""
    await _web_login(client)
    user = await _get_user(db_session)
    domain = await _seed_domain(db_session, user.id, domain_name="bulk.com")
    for i in range(30):
        db_session.add(
            EmailAddress(
                domain_id=domain.id,
                user_id=user.id,
                local_part=f"user{i}",
                full_address=f"user{i}@bulk.com",
                public_token=f"{i:032x}",
            )
        )
    await db_session.commit()

    resp = await client.get(f"/domains/{domain.id}")

    assert resp.status_code == 200
    assert "共 30 个" in resp.text
    assert "当前仅展示前 20 个预览" in resp.text
    assert "完整列表、筛选和批量链接操作" in resp.text
    assert "user0@bulk.com" in resp.text
    assert "user29@bulk.com" not in resp.text


async def test_domain_detail_not_found(client: AsyncClient) -> None:
    await _web_login(client)
    resp = await client.get("/domains/99999")
    assert resp.status_code == 404


async def test_domain_assignment_flow(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """域名所有者共享域名给用户，再取消共享。"""
    await _web_login(client)
    alice = await _get_user(db_session)

    await client.post(
        "/register",
        data={"username": "bob", "email": "bob@example.com", "password": "password123"},
    )
    bob = await _get_user(db_session, "bob")
    domain = await _seed_domain(
        db_session, alice.id, domain_name="mine.com"
    )

    resp = await client.post(
        f"/domains/{domain.id}/assignments",
        data={"username": "bob"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    detail = await client.get(f"/domains/{domain.id}")
    assert "bob" in detail.text
    assert "已共享域名给用户" in detail.text

    resp = await client.post(
        f"/domains/{domain.id}/assignments/{bob.id}/delete", follow_redirects=False
    )
    assert resp.status_code == 303
    detail = await client.get(f"/domains/{domain.id}")
    assert "已取消共享" in detail.text


async def test_domain_assignment_non_owner_redirected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """非域名所有者共享域名被拒绝（flash 错误后重定向）。"""
    await _web_login(client)

    # 另一个用户绑定域名
    await client.post(
        "/register",
        data={"username": "carol", "email": "carol@example.com", "password": "password123"},
    )
    carol = await _get_user(db_session, "carol")
    domain = await _seed_domain(db_session, carol.id, domain_name="carol.com")

    # alice（非所有者）尝试共享
    resp = await client.post(
        f"/domains/{domain.id}/assignments",
        data={"username": "alice"},
        follow_redirects=False,
    )
    # 非所有者会 flash 错误并重定向到域名详情页
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/domains/{domain.id}"


# ---- 邮箱地址 ----


async def test_email_address_crud(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """创建 → 列表可见 → 停用 → 删除。"""
    await _web_login(client)
    user = await _get_user(db_session)
    domain = await _seed_domain(db_session, user.id, domain_name="mine.com")

    resp = await client.post(
        "/email-addresses",
        data={"domain_id": str(domain.id), "local_part": "hello"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    listing = await client.get("/email-addresses")
    assert "hello@mine.com" in listing.text

    email = (await db_session.execute(select(EmailAddress))).scalar_one()
    assert email.is_active is True

    await client.post(f"/email-addresses/{email.id}/toggle", follow_redirects=False)
    await db_session.refresh(email)
    assert email.is_active is False

    await client.post(f"/email-addresses/{email.id}/delete", follow_redirects=False)
    await db_session.refresh(email)
    assert email.is_deleted is True


async def test_create_first_email_enables_inbound_routing_and_flash(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """创建域名下第一个邮箱地址时，启用收件路由并提示重新部署 Worker。"""
    await _web_login(client)
    user = await _get_user(db_session)
    domain = await _seed_domain(db_session, user.id, domain_name="mail.com")
    assert domain.inbound_routing_enabled is False

    resp = await client.post(
        "/email-addresses",
        data={"domain_id": str(domain.id), "local_part": "hello"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "重新一键部署 Worker" in resp.text
    await db_session.refresh(domain)
    assert domain.inbound_routing_enabled is True


async def test_email_address_invalid_local_part(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """非法本地部分被拒绝并提示，不落库。"""
    await _web_login(client)
    user = await _get_user(db_session)
    domain = await _seed_domain(db_session, user.id)

    resp = await client.post(
        "/email-addresses",
        data={"domain_id": str(domain.id), "local_part": "bad space!"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    rows = (await db_session.execute(select(EmailAddress))).scalars().all()
    assert rows == []
    listing = await client.get("/email-addresses")
    assert "输入有误" in listing.text


async def test_email_addresses_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/email-addresses", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


async def test_email_addresses_filter_empty_domain_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """前端"全部域名"提交空 domain_id 时不应触发 422，正常返回列表。"""
    await _web_login(client)
    user = await _get_user(db_session)
    await _seed_domain(db_session, user.id, domain_name="mine.com")
    resp = await client.get("/email-addresses", params={"domain_id": ""})
    assert resp.status_code == 200


async def test_email_addresses_filter_invalid_domain_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """非正整数的 domain_id 被忽略，不触发 422，返回不过滤列表。"""
    await _web_login(client)
    user = await _get_user(db_session)
    await _seed_domain(db_session, user.id, domain_name="mine.com")
    for bad in ("0", "-1", "abc"):
        resp = await client.get("/email-addresses", params={"domain_id": bad})
        assert resp.status_code == 200


async def test_domain_assignment_unknown_username(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """共享给不存在的用户名时 flash 错误并重定向，不落库。"""
    await _web_login(client)
    user = await _get_user(db_session)
    domain = await _seed_domain(db_session, user.id, domain_name="mine.com")

    resp = await client.post(
        f"/domains/{domain.id}/assignments",
        data={"username": "no-such-user"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    detail = await client.get(f"/domains/{domain.id}")
    assert "不存在" in detail.text
    rows = (
        await db_session.execute(select(DomainAssignment))
    ).scalars().all()
    assert rows == []


async def test_non_owner_share_unknown_username_no_enumeration(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """非所有者共享时不暴露"用户不存在"，统一返回权限错误。"""
    await _web_login(client)

    await client.post(
        "/register",
        data={"username": "carol", "email": "carol@example.com", "password": "password123"},
    )
    carol = await _get_user(db_session, "carol")
    domain = await _seed_domain(db_session, carol.id, domain_name="carol.com")

    resp = await client.post(
        f"/domains/{domain.id}/assignments",
        data={"username": "no-such-user"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    detail = await client.get(f"/domains/{domain.id}")
    assert "目标用户不存在" not in detail.text
    assert "所有者" in detail.text
