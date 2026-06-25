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


def test_nav_submenu(page: Page, live_server: str) -> None:
    """顶部二级菜单：点击「邮件」分组展开下拉，子项可正常导航。"""
    _signup_login(page, live_server)
    # 默认视口 1280×720 ≥ lg，桌面横向导航可见；点击分组按钮展开下拉
    page.get_by_role("button", name="邮件").click()
    page.get_by_role("link", name="收件箱").click()
    page.wait_for_url(f"{live_server}/inbound")
    expect(page.locator("body")).to_contain_text("收件箱")


def test_delete_uses_confirm_dialog(page: Page, live_server: str) -> None:
    """删除走自定义确认 Dialog（替代原生 confirm）：撤销 API Key。"""
    _signup_login(page, live_server)
    page.goto(f"{live_server}/api-keys")
    page.fill('input[name="name"]', "to-revoke")
    page.get_by_role("button", name="创建 API Key").click()
    # 新 Key 以模态弹出，先关闭
    expect(page.locator("body")).to_contain_text("仅显示一次")
    page.get_by_role("button", name="我已保存").click()
    # 点「撤销」应弹出自定义确认弹窗（依赖 $store.confirm 注册成功）
    page.get_by_role("button", name="撤销").click()
    expect(page.get_by_text("确认操作")).to_be_visible()
    # 点「确认」提交表单，Key 被撤销并提示
    page.get_by_role("button", name="确认", exact=True).click()
    expect(page.locator("body")).to_contain_text("已撤销 API Key")


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


def test_email_addresses_dropdown_contains_all_options(
    page: Page, live_server: str
) -> None:
    """邮箱地址列表：复制链接 / 下载链接下拉应展示 8 项。"""
    local_part = f"ddl{uuid.uuid4().hex[:6]}"
    expected_address = f"{local_part}@e2e.example.com"

    _signup_login(page, live_server)

    page.goto(f"{live_server}/cf-accounts/new")
    page.fill('input[name="name"]', "E2E账号")
    page.fill('input[name="account_id"]', "acc-e2e")
    page.fill('input[name="api_token"]', "tok-e2e")
    page.get_by_role("button", name="校验并绑定").click()
    page.wait_for_url(f"{live_server}/cf-accounts")
    expect(page.locator("body")).to_contain_text("E2E账号")
    page.get_by_role("link", name="E2E账号").click()
    page.get_by_role("button", name="同步域名").click()
    expect(page.locator("body")).to_contain_text("已同步 1 个域名")

    page.goto(f"{live_server}/email-addresses")
    page.wait_for_selector('input[name="local_part"]')
    page.fill('input[name="local_part"]', local_part)
    page.select_option('form[action="/email-addresses"] select[name="domain_id"]', index=0)
    page.get_by_role("button", name="创建", exact=True).click()
    expect(page.locator("body")).to_contain_text(expected_address)

    page.get_by_role("button", name="复制链接").click()
    expect(page.get_by_text("复制当前页（HTML 链接）")).to_be_visible()
    expect(page.get_by_text("复制当前与之前页（HTML 链接）")).to_be_visible()
    expect(page.get_by_text("复制近 100 条（HTML 链接）")).to_be_visible()
    expect(page.get_by_text("复制近 500 条（HTML 链接）")).to_be_visible()
    expect(page.get_by_text("复制当前页（纯文本链接）")).to_be_visible()
    expect(page.get_by_text("复制近 500 条（纯文本链接）")).to_be_visible()
    page.locator("body").click(position={"x": 5, "y": 5})

    page.get_by_role("button", name="下载链接").click()
    expect(page.get_by_text("下载当前页（HTML 链接）")).to_be_visible()
    expect(page.get_by_text("下载当前与之前页（纯文本链接）")).to_be_visible()
    expect(page.get_by_text("下载近 100 条（HTML 链接）")).to_be_visible()
    expect(page.get_by_text("下载近 500 条（纯文本链接）")).to_be_visible()
    page.locator("body").click(position={"x": 5, "y": 5})

    page.get_by_role("button", name="复制链接").click()
    with page.expect_response(
        lambda r: "/email-addresses/links" in r.url
    ) as resp_info:
        page.get_by_text("复制近 500 条（纯文本链接）").click()
    response = resp_info.value
    assert response.status == 200
    assert "size=500" in response.url
    assert "order=desc" in response.url
