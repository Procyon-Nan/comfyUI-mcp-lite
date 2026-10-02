"""图片签名 URL（v0.2 规格）。

ChatLuna 类发图组件只吃可公开抓取的 URL，不会带 Authorization 头，
因此 /images 端点改走查询串签名鉴权：

    key     = MCP_AUTH_TOKEN
    message = f"{prompt_id}:{index}:{e}"        # e = unix 过期时间
    s       = HMAC-SHA256(key, message) 的 hex

生成（工具拼 urls）与校验（端点收请求）共用 compute_signature，
保证两侧算法一致；比对用 hmac.compare_digest 防时序侧信道。
"""

from __future__ import annotations

import hashlib
import hmac
import time


def compute_signature(token: str, prompt_id: str, index: int, expires: int) -> str:
    """按规格计算签名（hex）；生成与校验共用。"""
    message = f"{prompt_id}:{index}:{expires}"
    return hmac.new(token.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def build_image_url(
    base_url: str, token: str, prompt_id: str, index: int, ttl_seconds: int
) -> str:
    """拼一条带过期与签名的图片 URL（base_url 即 PUBLIC_BASE_URL，调用方保证非空）。"""
    expires = int(time.time()) + ttl_seconds
    signature = compute_signature(token, prompt_id, index, expires)
    return f"{base_url.rstrip('/')}/images/{prompt_id}/{index}?e={expires}&s={signature}"


def verify_signature(
    token: str, prompt_id: str, index: int, expires: int, supplied: str
) -> bool:
    """校验签名与有效期：重算比对 + 未过期；任一不符返回 False（端点回 403）。"""
    expected = compute_signature(token, prompt_id, index, expires)
    # 两侧都转 bytes 再比：str 版 compare_digest 只接受 ASCII，用户可传任意字符
    return hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ) and time.time() <= expires
