"""SEO 辅助路由：robots.txt 与 sitemap.xml（供搜索引擎抓取公开页面）。"""

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

router = APIRouter(tags=["前端-SEO"])

# 登录后才可见的应用路径，禁止搜索引擎抓取
_DISALLOW = [
    "/dashboard",
    "/cf-accounts",
    "/domains",
    "/email-addresses",
    "/forwarding-rules",
    "/inbound",
    "/outbound",
    "/api-keys",
    "/profile",
    "/admin",
]

# 可被索引的公开页面
_PUBLIC_PATHS = ["/", "/login", "/register"]


@router.get("/robots.txt")
async def robots(request: Request) -> PlainTextResponse:
    """robots.txt：允许抓取公开页，屏蔽登录后应用路径，并指向 sitemap。"""
    lines = ["User-agent: *", "Allow: /"]
    lines += [f"Disallow: {path}" for path in _DISALLOW]
    lines.append("")
    lines.append(f"Sitemap: {request.base_url}sitemap.xml")
    return PlainTextResponse("\n".join(lines) + "\n")


@router.get("/sitemap.xml")
async def sitemap(request: Request) -> Response:
    """sitemap.xml：列出公开可索引页面。"""
    base = str(request.base_url).rstrip("/")
    urls = "".join(f"<url><loc>{base}{path}</loc></url>" for path in _PUBLIC_PATHS)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return Response(content=xml, media_type="application/xml")
