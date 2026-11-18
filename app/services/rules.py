"""空指纹校验水线：前后端、各服务共用同一判定，禁止各处各写一套。"""

from app.services.errors import ServiceError

# 指纹水线：去空白后最短长度。页面与后台同取这一条规则。
MIN_FINGERPRINT_LEN = 6
EMPTY_FINGERPRINT_MSG = "内容指纹为空，禁止登记：请补全指纹后再写入"
TOO_SHORT_FINGERPRINT_MSG = f"内容指纹短于水线（至少 {MIN_FINGERPRINT_LEN} 位），禁止登记"
PLACEHOLDER_PREFIX = "PLACEHOLDER-PENDING-RECHECK:"
WITHDRAWN_MARK = "已撤回"


def normalize_fingerprint(content_hash: str | None) -> str:
    """统一归一化：None 安全、去首尾空白。"""
    return (content_hash or "").strip()


def validate_fingerprint(content_hash: str | None) -> str:
    """登记/复检/扫库共用的水线判定。不合法即抛 ServiceError（回白话）。"""
    digest = normalize_fingerprint(content_hash)
    if not digest:
        raise ServiceError(EMPTY_FINGERPRINT_MSG, 422)
    if len(digest) < MIN_FINGERPRINT_LEN:
        raise ServiceError(TOO_SHORT_FINGERPRINT_MSG, 422)
    if digest.startswith(PLACEHOLDER_PREFIX):
        raise ServiceError("占位指纹不可作为正式指纹提交，请走强制复检补真实指纹", 422)
    return digest


def is_empty_fingerprint(content_hash: str | None) -> bool:
    return normalize_fingerprint(content_hash) == ""


def is_placeholder_fingerprint(content_hash: str | None) -> bool:
    return normalize_fingerprint(content_hash).startswith(PLACEHOLDER_PREFIX)


def waterline_meta() -> dict:
    """暴露给前端的同一份水线，页面拦截与后台校验据此保持一致。"""
    return {
        "min_fingerprint_len": MIN_FINGERPRINT_LEN,
        "empty_message": EMPTY_FINGERPRINT_MSG,
        "too_short_message": TOO_SHORT_FINGERPRINT_MSG,
        "placeholder_prefix": PLACEHOLDER_PREFIX,
    }
