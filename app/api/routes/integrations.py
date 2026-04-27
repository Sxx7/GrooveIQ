"""GrooveIQ – Integration health / connectivity status."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from app.core.config import settings
from app.core.security import require_admin, require_api_key

router = APIRouter()

_TIMEOUT = 5.0  # seconds per probe


def _sanitize_error(error: str) -> str:
    """Strip internal hostnames, ports, and file paths from error messages."""
    import re

    # Remove URLs (http://hostname:port/path)
    error = re.sub(r"https?://[^\s'\"]+", "<service-url>", error)
    # Remove file paths
    error = re.sub(r"/[\w/.+-]+(?:\.py|\.conf|\.cfg|\.ini|\.json)", "<path>", error)
    return error[:256]  # cap length


async def _probe(url: str, headers: dict | None = None) -> dict[str, Any]:
    """HTTP GET with timeout; return parsed JSON or error dict."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers or {})
            resp.raise_for_status()
            return {"ok": True, "status_code": resp.status_code, "data": resp.json()}
    except httpx.TimeoutException:
        return {"ok": False, "error": "Connection timed out"}
    except httpx.ConnectError as exc:
        return {"ok": False, "error": f"Connection refused or DNS failure: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _check_spotdl() -> dict[str, Any]:
    url = settings.SPOTDL_API_URL
    if not url:
        return {"configured": False}
    result = await _probe(f"{url.rstrip('/')}/health")
    entry: dict[str, Any] = {
        "configured": True,
        "connected": result["ok"],
    }
    if result["ok"]:
        data = result["data"]
        entry["version"] = data.get("version")
        entry["details"] = {k: data[k] for k in ("output_format", "active_tasks") if k in data}
    else:
        entry["error"] = _sanitize_error(result["error"])
    return entry


async def _check_streamrip() -> dict[str, Any]:
    url = settings.STREAMRIP_API_URL
    if not url:
        return {"configured": False}
    result = await _probe(f"{url.rstrip('/')}/health")
    entry: dict[str, Any] = {
        "configured": True,
        "connected": result["ok"],
    }
    if result["ok"]:
        data = result["data"]
        entry["details"] = {
            k: data[k]
            for k in ("available_services", "default_service", "download_quality", "active_tasks")
            if k in data
        }
    else:
        entry["error"] = _sanitize_error(result["error"])
    return entry


async def _check_lidarr() -> dict[str, Any]:
    url = settings.LIDARR_URL
    api_key = settings.LIDARR_API_KEY
    if not url or not api_key:
        return {"configured": False}
    result = await _probe(
        f"{url.rstrip('/')}/api/v1/system/status",
        headers={"X-Api-Key": api_key},
    )
    entry: dict[str, Any] = {
        "configured": True,
        "connected": result["ok"],
    }
    if result["ok"]:
        data = result["data"]
        entry["version"] = data.get("version")
        entry["details"] = {k: data[k] for k in ("osName", "runtimeName") if k in data}
    else:
        entry["error"] = _sanitize_error(result["error"])
    return entry


async def _check_slskd() -> dict[str, Any]:
    url = settings.SLSKD_URL
    if not settings.slskd_enabled:
        return {"configured": False}
    result = await _probe(
        f"{url.rstrip('/')}/api/v0/server",
        headers={"X-API-Key": settings.SLSKD_API_KEY},
    )
    entry: dict[str, Any] = {
        "configured": True,
        "connected": result["ok"],
    }
    if result["ok"]:
        data = result["data"]
        entry["state"] = data.get("state")
        entry["details"] = {k: data[k] for k in ("version", "isConnected") if k in data}
    else:
        entry["error"] = _sanitize_error(result["error"])
    return entry


async def _check_acousticbrainz() -> dict[str, Any]:
    url = settings.AB_LOOKUP_URL
    enabled = settings.AB_LOOKUP_ENABLED
    if not url or not enabled:
        return {"configured": False}
    result = await _probe(f"{url.rstrip('/')}/health")
    entry: dict[str, Any] = {
        "configured": True,
        "connected": result["ok"],
    }
    if result["ok"]:
        data = result["data"]
        entry["status"] = data.get("status")  # "ready" or "ingesting"
        entry["details"] = {k: data[k] for k in ("track_count", "ingestion_progress") if k in data}
    else:
        entry["error"] = _sanitize_error(result["error"])
    return entry


async def _check_lastfm() -> dict[str, Any]:
    api_key = settings.LASTFM_API_KEY
    if not api_key:
        return {"configured": False}
    # Light probe: fetch a known artist to verify the API key works.
    # Use httpx params= to keep the API key out of the URL string
    # (avoids accidental logging of the key in error messages).
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={"method": "artist.getinfo", "artist": "Radiohead", "api_key": api_key, "format": "json"},
            )
            resp.raise_for_status()
            data = resp.json()
            result = {"ok": True, "data": data}
    except httpx.TimeoutException:
        result = {"ok": False, "error": "Connection timed out"}
    except Exception as exc:
        result = {"ok": False, "error": _sanitize_error(str(exc))}

    entry: dict[str, Any] = {
        "configured": True,
        "scrobbling": bool(settings.LASTFM_SCROBBLE_ENABLED),
        "connected": result["ok"] and "error" not in result.get("data", {}),
    }
    if result["ok"] and "error" in result.get("data", {}):
        entry["connected"] = False
        entry["error"] = result["data"]["message"]
    elif not result["ok"]:
        entry["error"] = result.get("error", "Unknown error")
    return entry


async def _check_media_server() -> dict[str, Any]:
    ms_type = settings.MEDIA_SERVER_TYPE
    ms_url = settings.MEDIA_SERVER_URL
    if not ms_type or not ms_url:
        return {"configured": False}

    entry: dict[str, Any] = {
        "configured": True,
        "type": ms_type,
    }

    if ms_type == "navidrome":
        # Subsonic ping endpoint
        import hashlib
        import secrets

        password = settings.MEDIA_SERVER_PASSWORD
        # Decrypt if encrypted
        if password and settings.CREDENTIAL_ENCRYPTION_KEY:
            try:
                from cryptography.fernet import Fernet

                f = Fernet(settings.CREDENTIAL_ENCRYPTION_KEY.encode())
                password = f.decrypt(password.encode()).decode()
            except Exception:
                pass
        salt = secrets.token_hex(8)
        # nosemgrep: python.lang.security.audit.md5-used-as-password.md5-used-as-password
        # MD5(password + salt) is mandated by the Subsonic API spec for auth tokens.
        token = hashlib.md5((password + salt).encode()).hexdigest()
        result = await _probe(
            f"{ms_url.rstrip('/')}/rest/ping.view"
            f"?u={settings.MEDIA_SERVER_USER}&t={token}&s={salt}"
            f"&v=1.16.1&c=grooveiq&f=json"
        )
        entry["connected"] = result["ok"] and result.get("data", {}).get("subsonic-response", {}).get("status") == "ok"
        if not result["ok"]:
            entry["error"] = _sanitize_error(result["error"])
        elif not entry["connected"]:
            sr = result.get("data", {}).get("subsonic-response", {})
            entry["error"] = sr.get("error", {}).get("message", "Auth failed")

    elif ms_type == "plex":
        token = settings.MEDIA_SERVER_TOKEN
        if token and settings.CREDENTIAL_ENCRYPTION_KEY:
            try:
                from cryptography.fernet import Fernet

                f = Fernet(settings.CREDENTIAL_ENCRYPTION_KEY.encode())
                token = f.decrypt(token.encode()).decode()
            except Exception:
                pass
        result = await _probe(
            f"{ms_url.rstrip('/')}/identity",
            headers={"X-Plex-Token": token or "", "Accept": "application/json"},
        )
        entry["connected"] = result["ok"]
        if result["ok"]:
            data = result["data"]
            mc = data.get("MediaContainer", data)
            entry["version"] = mc.get("version")
        else:
            entry["error"] = _sanitize_error(result["error"])
    else:
        entry["connected"] = False
        entry["error"] = f"Unknown media server type: {ms_type}"

    return entry


@router.get("/integrations/status", summary="Integration connectivity status")
async def integrations_status(
    _key: str = Depends(require_api_key),
):
    require_admin(_key)

    results = await asyncio.gather(
        _check_spotdl(),
        _check_streamrip(),
        _check_lidarr(),
        _check_slskd(),
        _check_acousticbrainz(),
        _check_lastfm(),
        _check_media_server(),
    )

    return {
        "checked_at": int(time.time()),
        "integrations": {
            "spotdl_api": results[0],
            "streamrip_api": results[1],
            "lidarr": results[2],
            "slskd": results[3],
            "acousticbrainz_lookup": results[4],
            "lastfm": results[5],
            "media_server": results[6],
        },
    }
