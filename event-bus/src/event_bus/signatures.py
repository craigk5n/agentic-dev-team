import hashlib
import hmac


def verify_forgejo(payload: bytes, header: str, secret: str) -> bool:
    """
    Verify Forgejo webhook signature from X-Gitea-Signature.
    Forgejo sends the raw HMAC-SHA256 hex digest with no prefix.
    """
    if not secret:
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())
