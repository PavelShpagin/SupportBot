"""Cloudflare R2 blob storage helpers.

Uses the S3-compatible API via boto3. Falls back to local disk if R2 is not configured.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_r2_client = None
_r2_bucket: str = ""
_r2_public_url: str = ""
_r2_enabled: bool = False


def init_r2() -> bool:
    """Initialise the R2 client from environment variables. Returns True if configured."""
    global _r2_client, _r2_bucket, _r2_public_url, _r2_enabled

    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    access_key = os.getenv("CLOUDFLARE_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("CLOUDFLARE_SECRET_ACCESS", "").strip()
    # tolerate the typo variant that may exist in .env
    bucket = (
        os.getenv("CLOUDFLARE_BUCKET", "").strip()
        or os.getenv("CLOUADFLARE_BUCKET", "").strip()
    )

    if not all([account_id, access_key, secret_key, bucket]):
        log.info("R2 not configured — attachments will be stored locally")
        _r2_enabled = False
        return False

    try:
        import boto3
        from botocore.config import Config

        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        _r2_client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
        _r2_bucket = bucket

        # Public URL for serving stored objects.
        # Set CLOUDFLARE_R2_PUBLIC_URL to an r2.dev subdomain or custom domain.
        # If not set, falls back to the bot's /r2 proxy endpoint which serves
        # R2 objects authenticated server-side (no public bucket required).
        public_url = os.getenv("CLOUDFLARE_R2_PUBLIC_URL", "").strip().rstrip("/")
        if public_url:
            _r2_public_url = public_url
            log.info("R2 storage enabled: bucket=%s public_url=%s", bucket, _r2_public_url)
        else:
            _r2_public_url = "https://supportbot.info/r2"
            log.info(
                "R2 storage enabled: bucket=%s (CLOUDFLARE_R2_PUBLIC_URL not set, "
                "using internal proxy %s)",
                bucket,
                _r2_public_url,
            )

        _r2_enabled = True
        return True
    except Exception as e:
        log.warning("R2 init failed: %s — falling back to local storage", e)
        _r2_enabled = False
        return False


def is_enabled() -> bool:
    return _r2_enabled


def upload(key: str, data: bytes, content_type: str = "application/octet-stream") -> str | None:
    """Upload bytes to R2 under `key`. Returns the public URL or None on failure."""
    if not _r2_enabled or _r2_client is None:
        return None
    try:
        _r2_client.put_object(
            Bucket=_r2_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        url = f"{_r2_public_url}/{key}"
        log.info("Uploaded to R2: %s → %s", key, url)
        return url
    except Exception as e:
        log.warning("R2 upload failed for key=%s: %s", key, e)
        return None


def download(key: str) -> tuple[bytes, str] | None:
    """Download an object from R2. Returns (bytes, content_type) or None on failure."""
    if not _r2_enabled or _r2_client is None:
        return None
    try:
        resp = _r2_client.get_object(Bucket=_r2_bucket, Key=key)
        data = resp["Body"].read()
        content_type = resp.get("ContentType", "application/octet-stream")
        return data, content_type
    except Exception as e:
        log.warning("R2 download failed for key=%s: %s", key, e)
        return None


def url_for(key: str) -> str:
    """Return the public URL for a given R2 key."""
    return f"{_r2_public_url}/{key}"


def is_r2_url(path: str) -> bool:
    """Return True if the path is already an R2/https URL (not a local path)."""
    return path.startswith("https://") or path.startswith("http://")


def key_from_url(url: str) -> str | None:
    """Extract the R2 object key from a public URL. Returns None if not an R2 URL."""
    if not _r2_public_url or not url.startswith(_r2_public_url):
        return None
    return url[len(_r2_public_url):].lstrip("/")
