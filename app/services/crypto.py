"""对称加密：使用 SECRET_KEY 派生的 Fernet 密钥加解密 CF API Token。

CF API Token 入库前必须加密，禁止明文存储。
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.exceptions import AppException


@lru_cache
def _fernet() -> Fernet:
    """由 SECRET_KEY 派生 32 字节 url-safe base64 密钥并构造 Fernet。"""
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_token(plaintext: str) -> str:
    """加密明文 Token，返回密文字符串。"""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    """解密密文 Token，失败抛出 AppException。"""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise AppException("API Token 解密失败", code=1500, http_status=500) from exc
