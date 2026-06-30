"""公开邮件查询路由：无需登录，通过公开令牌查看邮箱邮件并发件。

- GET /mail/{token}      → HTML 页面（供人工快速查看）
- GET /mail/{token}.txt  → 纯文本（便于程序化读取）
- POST /mail/{token}/send → 通过该邮箱公开令牌发件

令牌为 EmailAddress.public_token（无符号 uuid）。停用或已删除的邮箱不可访问。
"""

import re
from html import unescape
from html.parser import HTMLParser
from typing import Annotated, Literal, TypedDict

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import ValidationError

from app.config import settings
from app.dependencies import SessionDep
from app.exceptions import AppException, NotFoundError
from app.models import EmailAddress, InboundEmail
from app.schemas.inbound_email import InboundEmailRead
from app.schemas.outbound import OutboundEmailRead, SendEmailRequest
from app.services import email_service, inbound_service, outbound_service
from app.services.rate_limit import client_ip, hit
from app.web.templating import error_message, flash, render, render_error

router = APIRouter(tags=["前端-公开邮件"])

_PUBLIC_PREVIEW_LENGTH = 180
_PUBLIC_PREVIEW_SCAN_LENGTH = 64 * 1024
_PUBLIC_PREVIEW_TEXT_LIMIT = _PUBLIC_PREVIEW_LENGTH + 64
_HTML_TAG_RE = re.compile(
    r"<\s*/?\s*[a-z][a-z0-9:-]*(?:\s+[^<>]*)?>",
    re.IGNORECASE,
)
_HTML_BREAK_TAGS = {"br", "p", "div", "li", "tr", "td", "th", "section", "article"}
_HTML_IGNORED_TAGS = {"script", "style", "noscript"}

BodyViewMode = Literal["html", "text", "empty"]


class PublicMailBodyView(TypedDict):
    """公开邮件详情页正文展示策略。"""

    default_mode: BodyViewMode
    html_content: str | None
    text_content: str | None
    html_label: str
    text_label: str


class _PreviewLimitReached(Exception):
    """HTML 预览文本已足够，提前停止解析。"""


class _HTMLTextExtractor(HTMLParser):
    """从 HTML 正文中提取适合列表预览的可读文本。"""

    def __init__(self, max_text_length: int) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0
        self._text_length = 0
        self._max_text_length = max_text_length

    def _append_text(self, text: str) -> None:
        """追加可见文本，达到上限后停止解析。"""
        if not text:
            return
        remaining = self._max_text_length - self._text_length
        if remaining <= 0:
            raise _PreviewLimitReached
        chunk = text[:remaining]
        self._parts.append(chunk)
        self._text_length += len(chunk)
        if len(text) > remaining:
            raise _PreviewLimitReached

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag_name = tag.lower()
        if tag_name in _HTML_IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag_name in _HTML_BREAK_TAGS:
            self._append_text(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _HTML_IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = data.strip()
        if text:
            self._append_text(text)

    def text(self) -> str:
        """返回已压缩空白的纯文本。"""
        return " ".join(" ".join(self._parts).split())


async def _resolve_by_token(
    request: Request, session: SessionDep, token: str
) -> EmailAddress:
    """按令牌解析邮箱；无效则抛 404（不暴露存在性差异）。"""
    ip = client_ip(request)
    hit(
        "public_mail_ip",
        ip,
        settings.PUBLIC_MAIL_RATE_LIMIT_ATTEMPTS,
        settings.PUBLIC_MAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    hit(
        "public_mail",
        f"{ip}:{token}",
        settings.PUBLIC_MAIL_RATE_LIMIT_ATTEMPTS,
        settings.PUBLIC_MAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    address = await email_service.get_email_address_by_token(session, token)
    if address is None:
        raise NotFoundError("链接无效或邮箱不可用")
    return address


def _parse_recipients(raw: str) -> list[str]:
    """将逗号/分号/换行分隔的收件人文本解析为列表。"""
    normalized = raw.replace("\n", ",").replace(";", ",")
    return [addr.strip() for addr in normalized.split(",") if addr.strip()]


def _looks_like_html(
    value: str | None, scan_length: int = _PUBLIC_PREVIEW_SCAN_LENGTH
) -> bool:
    """粗略判断文本正文是否实际存放了 HTML。"""
    if not value:
        return False
    sample = value[:scan_length]
    if "<" not in sample or ">" not in sample:
        return False
    leading = sample.lstrip()
    return leading[:9].lower() == "<!doctype" or bool(_HTML_TAG_RE.search(sample))


def _html_to_text(value: str) -> str:
    """把 HTML 片段转换为列表预览文本。"""
    limited = value[:_PUBLIC_PREVIEW_SCAN_LENGTH]
    parser = _HTMLTextExtractor(_PUBLIC_PREVIEW_TEXT_LIMIT)
    try:
        parser.feed(limited)
        parser.close()
    except _PreviewLimitReached:
        return parser.text()
    except Exception:
        stripped = _HTML_TAG_RE.sub(" ", limited)
        return " ".join(unescape(stripped).split())
    return parser.text()


def _truncate_preview(text: str) -> str:
    """压缩并截断公开收件箱预览。"""
    limited = text[:_PUBLIC_PREVIEW_SCAN_LENGTH]
    normalized = " ".join(limited.split())
    if len(normalized) > _PUBLIC_PREVIEW_LENGTH:
        return f"{normalized[:_PUBLIC_PREVIEW_LENGTH].rstrip()}..."
    return normalized


def _hit_public_send_limit(request: Request, token: str) -> None:
    """公开邮件发件独立限流，降低 token 泄露后的滥用风险。"""
    ip = client_ip(request)
    hit(
        "public_mail_send_ip",
        ip,
        settings.PUBLIC_MAIL_SEND_RATE_LIMIT_ATTEMPTS,
        settings.PUBLIC_MAIL_SEND_RATE_LIMIT_WINDOW_SECONDS,
    )
    hit(
        "public_mail_send",
        f"{ip}:{token}",
        settings.PUBLIC_MAIL_SEND_RATE_LIMIT_ATTEMPTS,
        settings.PUBLIC_MAIL_SEND_RATE_LIMIT_WINDOW_SECONDS,
    )


def _inbound_preview(email: InboundEmail) -> str:
    """生成公开收件箱列表预览，避免列表渲染完整正文。"""
    if email.body_text:
        if _looks_like_html(email.body_text):
            text = _html_to_text(email.body_text)
            return _truncate_preview(text) or "HTML 正文，点击查看完整内容"
        return _truncate_preview(email.body_text)
    if email.body_html:
        text = _html_to_text(email.body_html)
        return _truncate_preview(text) or "HTML 正文，点击查看完整内容"
    return ""


def _mail_body_view(email: InboundEmail) -> PublicMailBodyView:
    """决定公开详情页默认展示 HTML 预览还是纯文本。"""
    text = email.body_text
    html = email.body_html
    if html:
        return {
            "default_mode": "html",
            "html_content": html,
            "text_content": text,
            "html_label": "HTML 预览",
            "text_label": "纯文本正文",
        }
    if text and _looks_like_html(text):
        return {
            "default_mode": "html",
            "html_content": text,
            "text_content": text,
            "html_label": "HTML 预览",
            "text_label": "源码文本",
        }
    if text:
        return {
            "default_mode": "text",
            "html_content": None,
            "text_content": text,
            "html_label": "HTML 预览",
            "text_label": "纯文本正文",
        }
    return {
        "default_mode": "empty",
        "html_content": None,
        "text_content": None,
        "html_label": "HTML 预览",
        "text_label": "纯文本正文",
    }


def _inbound_list_item(email: InboundEmail) -> dict[str, object]:
    """公开收件箱列表项：完整正文只在详情页展示。"""
    return {
        "email": InboundEmailRead.model_validate(email),
        "preview": _inbound_preview(email),
    }


@router.get("/mail/{token}.txt", response_class=PlainTextResponse)
async def public_mail_text(
    request: Request, token: str, session: SessionDep
) -> PlainTextResponse:
    """纯文本格式返回邮箱最新一封邮件，便于程序化读取。"""
    try:
        address = await _resolve_by_token(request, session, token)
    except NotFoundError:
        return PlainTextResponse("链接无效或邮箱不可用", status_code=404)

    email = await inbound_service.get_latest_inbound_by_address(
        session, address.full_address
    )
    if email is None:
        return PlainTextResponse("暂无邮件", media_type="text/plain; charset=utf-8")

    lines = [
        f"发件人: {email.from_address}",
        f"收件人: {email.to_address}",
        f"时间: {email.received_at:%Y-%m-%d %H:%M}",
        f"主题: {email.subject or '(无主题)'}",
        "",
        email.body_text or "",
    ]
    return PlainTextResponse(
        "\n".join(lines), media_type="text/plain; charset=utf-8"
    )


@router.get("/mail/{token}")
async def public_mail_html(
    request: Request,
    token: str,
    session: SessionDep,
    tab: str = Query(default="inbound"),
    inbound_page: int = Query(default=1, ge=1),
    outbound_page: int = Query(default=1, ge=1),
) -> Response:
    """渲染公开邮箱工作台，供人工在不登录情况下查看和发件。"""
    try:
        address = await _resolve_by_token(request, session, token)
    except NotFoundError:
        return render_error(request, 404, "链接无效或邮箱不可用")

    inbound, inbound_total = await inbound_service.list_inbound_emails_by_address(
        session, address.full_address, inbound_page, 20
    )
    outbound, outbound_total = await outbound_service.list_outbound_emails_by_address(
        session, address.full_address, outbound_page, 20
    )
    return render(
        request,
        "public/mail.html",
        user=None,
        layout="landing",
        active=None,
        address=address.full_address,
        token=token,
        tab=tab if tab in {"inbound", "outbound", "compose"} else "inbound",
        inbound_emails=[_inbound_list_item(e) for e in inbound],
        inbound_page=inbound_page,
        inbound_total=inbound_total,
        outbound_emails=[OutboundEmailRead.model_validate(e) for e in outbound],
        outbound_page=outbound_page,
        outbound_total=outbound_total,
        form={},
    )


@router.get("/mail/{token}/inbound/{email_id:int}")
async def public_mail_inbound_detail(
    request: Request, token: str, email_id: int, session: SessionDep
) -> Response:
    """公开邮箱链接查看单封收件详情。"""
    try:
        address = await _resolve_by_token(request, session, token)
        email = await inbound_service.get_inbound_email_by_address_or_404(
            session, address.full_address, email_id
        )
    except NotFoundError:
        return render_error(request, 404, "邮件不存在或链接无效")

    return render(
        request,
        "public/mail_detail.html",
        user=None,
        layout="landing",
        active=None,
        address=address.full_address,
        token=token,
        email=InboundEmailRead.model_validate(email),
        body_view=_mail_body_view(email),
    )


@router.post("/mail/{token}/send")
async def public_mail_send(
    request: Request,
    token: str,
    session: SessionDep,
    to: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    text: Annotated[str, Form()] = "",
    html: Annotated[str, Form()] = "",
) -> Response:
    """公开邮箱链接发件；发件人固定为 token 对应邮箱地址。"""
    _hit_public_send_limit(request, token)
    try:
        address = await _resolve_by_token(request, session, token)
        data = SendEmailRequest(
            from_address=address.full_address,
            to=_parse_recipients(to),
            subject=subject,
            text=text or None,
            html=html or None,
        )
        await outbound_service.send_email_from_address(session, address, data)
    except NotFoundError:
        return render_error(request, 404, "链接无效或邮箱不可用")
    except (ValidationError, AppException) as exc:
        flash(request, error_message(exc), "error")
        return RedirectResponse(f"/mail/{token}?tab=compose", status_code=303)

    flash(request, "邮件已发送", "success")
    return RedirectResponse(f"/mail/{token}?tab=outbound", status_code=303)
