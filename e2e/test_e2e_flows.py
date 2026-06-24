"""端到端（Playwright）关键路径测试。

前置：``.venv/Scripts/python.exe -m playwright install chromium``。
运行：``.venv/Scripts/python.exe -m pytest e2e/``（默认 headless）。

CF 调用由 CF_FAKE_MODE 返回内置假数据（见 conftest 环境变量），不发真实请求。
"""

import uuid

import pytest
from playwright.sync_api import Page, expect

# e2e 涉及真实浏览器与后台线程，放宽全局 filterwarnings=error
pytestmark = pytest.mark.filterwarnings("ignore")


def _signup_login(page: Page, base_url: str) -> str:
    """注册并登录一个唯一用户，返回用户名。"""
    uid = uuid.uuid4().hex[:8]
    username = f"u{uid}"
    page.goto(f"{base_url}/register")
    page.fill('input[name="username"]', username)
    page.fill('input[name="email"]', f"{uid}@example.com")
    page.fill('input[name="password"]', "password123")
    page.get_by_role("button", name="注册", exact=True).click()
    page.wait_for_url(f"{base_url}/login")
    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', "password123")
    page.get_by_role("button", name="登录", exact=True).click()
    page.wait_for_url(f"{base_url}/dashboard")
    return username


def test_register_login_logout(page: Page, live_server: str) -> None:
    """注册 → 登录 → 登出闭环。"""
    username = _signup_login(page, live_server)
    expect(page.locator("body")).to_contain_text(username)
    # 直接提交登出表单（避免依赖 Alpine 下拉的可见性）
    page.evaluate("document.querySelector('form[action=\"/logout\"]').submit()")
    page.wait_for_url(f"{live_server}/login")


def test_full_cf_flow(page: Page, live_server: str) -> None:
    """绑定 CF → 同步域名 → 建邮箱 → 建转发规则 → 发件（均在假 CF 下）。"""
    _signup_login(page, live_server)

    # 绑定 CF 账号
    page.goto(f"{live_server}/cf-accounts/new")
    page.fill('input[name="name"]', "E2E账号")
    page.fill('input[name="account_id"]', "acc-e2e")
    page.fill('input[name="api_token"]', "tok-e2e")
    page.get_by_role("button", name="校验并绑定").click()
    page.wait_for_url(f"{live_server}/cf-accounts")
    expect(page.locator("body")).to_contain_text("E2E账号")

    # 进入详情并同步域名
    page.get_by_role("link", name="E2E账号").click()
    page.get_by_role("button", name="同步域名").click()
    expect(page.locator("body")).to_contain_text("已同步 1 个域名")

    # 域名列表出现同步的域名
    page.goto(f"{live_server}/domains")
    expect(page.locator("body")).to_contain_text("e2e.example.com")

    # 创建邮箱地址
    page.goto(f"{live_server}/email-addresses")
    page.fill('input[name="local_part"]', "hello")
    page.select_option('select[name="domain_id"]', index=0)
    page.get_by_role("button", name="创建", exact=True).click()
    expect(page.locator("body")).to_contain_text("hello@e2e.example.com")

    # 创建转发规则
    page.goto(f"{live_server}/forwarding-rules")
    page.select_option('select[name="email_address_id"]', index=0)
    page.fill('input[name="destination_email"]', "dest@example.com")
    page.get_by_role("button", name="创建", exact=True).click()
    expect(page.locator("body")).to_contain_text("dest@example.com")

    # 发件（假 CF 下成功）
    page.goto(f"{live_server}/outbound")
    page.select_option('select[name="from_address"]', index=0)
    page.fill('textarea[name="to"]', "dest@example.com")
    page.fill('input[name="subject"]', "E2E主题")
    page.fill('textarea[name="text"]', "正文内容")
    page.get_by_role("button", name="发送邮件").click()
    expect(page.locator("body")).to_contain_text("邮件已发送")


def test_api_key_created_once(page: Page, live_server: str) -> None:
    """创建 API Key 时一次性展示原文，刷新后不再显示。"""
    _signup_login(page, live_server)
    page.goto(f"{live_server}/api-keys")
    page.fill('input[name="name"]', "e2e-key")
    page.get_by_role("button", name="创建 API Key").click()
    expect(page.locator("body")).to_contain_text("cfem_")
    expect(page.locator("body")).to_contain_text("仅显示一次")

    page.goto(f"{live_server}/api-keys")
    expect(page.locator("body")).not_to_contain_text("cfem_")
