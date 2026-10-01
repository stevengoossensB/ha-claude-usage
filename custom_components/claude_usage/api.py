"""Small async client for Claude subscription limits and the Anthropic Admin API."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    ADMIN_BASE_URL,
    ANTHROPIC_VERSION,
    OAUTH_AUTHORIZE_URL,
    OAUTH_BETA,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
    PROFILE_URL,
    USAGE_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=90)


class ClaudeUsageError(Exception):
    """Generic error talking to Anthropic."""


class ClaudeAuthError(ClaudeUsageError):
    """Credentials are invalid or expired and cannot be refreshed."""


class ClaudeRateLimitError(ClaudeUsageError):
    """The endpoint asked us to slow down."""


# --------------------------------------------------------------------------- #
# OAuth helpers
# --------------------------------------------------------------------------- #


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_authorize_url(challenge: str, state: str) -> str:
    """Build the claude.ai authorize URL (manual code-paste flow)."""
    params = {
        "code": "true",
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def parse_pasted_code(pasted: str) -> tuple[str, str | None]:
    """The callback page shows `code#state`; accept that or a full URL."""
    pasted = pasted.strip()
    if "code=" in pasted:
        query = parse_qs(urlparse(pasted).query)
        return query.get("code", [""])[0], query.get("state", [None])[0]
    if "#" in pasted:
        code, state = pasted.split("#", 1)
        return code, state
    return pasted, None


@dataclass
class OAuthTokens:
    """Token set returned by the OAuth server."""

    access_token: str
    refresh_token: str
    expires_at: float
    account_email: str | None = None
    account_id: str | None = None
    organization_name: str | None = None


def _tokens_from_response(data: dict[str, Any], old_refresh: str | None = None) -> OAuthTokens:
    try:
        access = data["access_token"]
    except KeyError as err:
        raise ClaudeAuthError(f"No access_token in token response: {list(data)}") from err
    account = data.get("account") or {}
    org = data.get("organization") or {}
    return OAuthTokens(
        access_token=access,
        refresh_token=data.get("refresh_token") or old_refresh or "",
        expires_at=time.time() + float(data.get("expires_in", 3600)),
        account_email=account.get("email_address") or account.get("email"),
        account_id=account.get("uuid"),
        organization_name=org.get("name"),
    )


async def _post_token(session: aiohttp.ClientSession, payload: dict[str, str]) -> dict[str, Any]:
    """POST to the token endpoint (form encoded, JSON as fallback)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    last_error = "unknown"
    last_status = 0
    for as_json in (False, True):
        kwargs: dict[str, Any] = {"json": payload} if as_json else {"data": payload}
        try:
            async with session.post(
                OAUTH_TOKEN_URL, headers=headers, timeout=TIMEOUT, **kwargs
            ) as resp:
                text = await resp.text()
                if resp.status == 200:
                    return await resp.json(content_type=None)
                if resp.status == 429:
                    raise ClaudeRateLimitError("Token endpoint rate limited")
                last_error = f"HTTP {resp.status}: {text[:300]}"
                last_status = resp.status
                if resp.status in (400, 401, 403) and "invalid_grant" in text:
                    raise ClaudeAuthError(last_error)
                if resp.status == 415 or (resp.status == 400 and not as_json):
                    continue  # retry once as JSON
                break
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ClaudeUsageError(f"Token request failed: {err}") from err
    if last_status >= 500 or last_status == 0:
        raise ClaudeUsageError(last_error)
    raise ClaudeAuthError(last_error)


async def exchange_code(
    session: aiohttp.ClientSession, code: str, verifier: str, state: str
) -> OAuthTokens:
    """Exchange the pasted authorization code for tokens."""
    data = await _post_token(
        session,
        {
            "grant_type": "authorization_code",
            "code": code,
            "state": state,
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    return _tokens_from_response(data)


async def refresh_tokens(session: aiohttp.ClientSession, refresh_token: str) -> OAuthTokens:
    """Use the refresh token to obtain a new access token (refresh tokens rotate)."""
    data = await _post_token(
        session,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        },
    )
    return _tokens_from_response(data, refresh_token)


# --------------------------------------------------------------------------- #
# Subscription limits
# --------------------------------------------------------------------------- #


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class UsageWindow:
    """One rate-limit window."""

    key: str
    name: str
    utilization: float | None
    resets_at: datetime | None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubscriptionUsage:
    """Parsed /api/oauth/usage response."""

    windows: dict[str, UsageWindow]
    extra_usage: dict[str, Any] | None
    raw: dict[str, Any]


_KNOWN_NAMES = {
    "five_hour": "Session (5h)",
    "seven_day": "Weekly",
    "seven_day_opus": "Weekly Opus",
    "seven_day_sonnet": "Weekly Sonnet",
    "seven_day_oauth_apps": "Weekly OAuth apps",
}


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")


def parse_subscription_usage(data: dict[str, Any]) -> SubscriptionUsage:
    """Turn the raw payload into windows, tolerant to schema drift."""
    windows: dict[str, UsageWindow] = {}

    for key, value in data.items():
        if key == "extra_usage" or not isinstance(value, dict) or "utilization" not in value:
            continue
        name = _KNOWN_NAMES.get(key) or key.replace("_", " ").capitalize()
        util = value.get("utilization")
        windows[key] = UsageWindow(
            key=key,
            name=name,
            utilization=float(util) if util is not None else None,
            resets_at=_parse_dt(value.get("resets_at")),
        )

    # Newer payloads carry model-scoped weekly limits in `limits[]`.
    for item in data.get("limits") or []:
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        model = ((item.get("scope") or {}).get("model") or {})
        label = model.get("display_name") or model.get("id")
        if not label:
            continue
        key = f"weekly_{_slug(label)}"
        if key in windows:
            continue
        pct = item.get("percent")
        windows[key] = UsageWindow(
            key=key,
            name=f"Weekly {label}",
            utilization=float(pct) if pct is not None else None,
            resets_at=_parse_dt(item.get("resets_at")),
            extra={"severity": item.get("severity"), "is_active": item.get("is_active")},
        )

    # Attach severity info to the generic windows too.
    for item in data.get("limits") or []:
        if not isinstance(item, dict):
            continue
        target = {"session": "five_hour", "weekly_all": "seven_day"}.get(item.get("kind"))
        if target and target in windows:
            windows[target].extra.update(
                {"severity": item.get("severity"), "is_active": item.get("is_active")}
            )

    extra = data.get("extra_usage")
    return SubscriptionUsage(
        windows=windows,
        extra_usage=extra if isinstance(extra, dict) else None,
        raw=data,
    )


async def fetch_subscription_usage(
    session: aiohttp.ClientSession, access_token: str
) -> SubscriptionUsage:
    """GET /api/oauth/usage."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    try:
        async with session.get(USAGE_URL, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status in (401, 403):
                raise ClaudeAuthError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
            if resp.status == 429:
                raise ClaudeRateLimitError("Usage endpoint rate limited")
            if resp.status != 200:
                raise ClaudeUsageError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ClaudeUsageError(f"Usage request failed: {err}") from err
    return parse_subscription_usage(data)


async def fetch_profile(session: aiohttp.ClientSession, access_token: str) -> dict[str, Any]:
    """Best effort: account info for naming the entry."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": USER_AGENT,
    }
    try:
        async with session.get(PROFILE_URL, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError):
        pass
    return {}


# --------------------------------------------------------------------------- #
# Admin API (pay-as-you-go API usage)
# --------------------------------------------------------------------------- #


@dataclass
class AdminUsage:
    """Month-to-date and today's API usage."""

    month_start: datetime
    day_start: datetime
    tokens_month: dict[str, int]
    tokens_today: dict[str, int]
    cost_month: float | None
    cost_today: float | None
    by_model_month: dict[str, int]


TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")


def _sum_usage_result(result: dict[str, Any]) -> dict[str, int]:
    cache_creation = result.get("cache_creation") or {}
    cache_write = int(result.get("cache_creation_input_tokens") or 0)
    if isinstance(cache_creation, dict):
        cache_write += sum(int(v or 0) for v in cache_creation.values())
    return {
        "input": int(result.get("uncached_input_tokens") or 0),
        "output": int(result.get("output_tokens") or 0),
        "cache_read": int(result.get("cache_read_input_tokens") or 0),
        "cache_write": cache_write,
    }


async def _admin_get_all(
    session: aiohttp.ClientSession, admin_key: str, path: str, params: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    headers = {
        "x-api-key": admin_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "User-Agent": USER_AGENT,
    }
    buckets: list[dict[str, Any]] = []
    page: str | None = None
    for _ in range(20):  # hard stop on pagination
        query = list(params) + ([("page", page)] if page else [])
        try:
            async with session.get(
                f"{ADMIN_BASE_URL}/{path}", headers=headers, params=query, timeout=TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise ClaudeAuthError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
                if resp.status == 429:
                    raise ClaudeRateLimitError("Admin API rate limited")
                if resp.status != 200:
                    raise ClaudeUsageError(
                        f"{path} HTTP {resp.status}: {(await resp.text())[:200]}"
                    )
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ClaudeUsageError(f"Admin API request failed: {err}") from err
        buckets.extend(data.get("data") or [])
        if not data.get("has_more") or not data.get("next_page"):
            break
        page = data["next_page"]
    return buckets


async def validate_admin_key(session: aiohttp.ClientSession, admin_key: str) -> dict[str, Any]:
    """Validate the key; returns organization info when available."""
    headers = {
        "x-api-key": admin_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "User-Agent": USER_AGENT,
    }
    try:
        async with session.get(
            f"{ADMIN_BASE_URL}/me", headers=headers, timeout=TIMEOUT
        ) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            if resp.status in (401, 403):
                raise ClaudeAuthError(f"HTTP {resp.status}")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ClaudeUsageError(str(err)) from err
    # /me not available: fall back to a tiny usage query.
    start = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)
    await _admin_get_all(
        session,
        admin_key,
        "usage_report/messages",
        [("starting_at", start.isoformat().replace("+00:00", "Z")), ("limit", "1")],
    )
    return {}


async def fetch_admin_usage(session: aiohttp.ClientSession, admin_key: str) -> AdminUsage:
    """Fetch month-to-date token usage and cost (UTC calendar month)."""
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_iso = month_start.isoformat().replace("+00:00", "Z")

    usage_buckets = await _admin_get_all(
        session,
        admin_key,
        "usage_report/messages",
        [
            ("starting_at", start_iso),
            ("bucket_width", "1d"),
            ("limit", "31"),
            ("group_by[]", "model"),
        ],
    )
    tokens_month = dict.fromkeys(TOKEN_FIELDS, 0)
    tokens_today = dict.fromkeys(TOKEN_FIELDS, 0)
    by_model: dict[str, int] = {}
    for bucket in usage_buckets:
        bstart = _parse_dt(bucket.get("starting_at"))
        for result in bucket.get("results") or []:
            sums = _sum_usage_result(result)
            for k, v in sums.items():
                tokens_month[k] += v
                if bstart and bstart >= day_start:
                    tokens_today[k] += v
            model = result.get("model") or "unknown"
            by_model[model] = by_model.get(model, 0) + sum(sums.values())

    cost_month: float | None = None
    cost_today: float | None = None
    try:
        cost_buckets = await _admin_get_all(
            session,
            admin_key,
            "cost_report",
            [("starting_at", start_iso), ("bucket_width", "1d"), ("limit", "31")],
        )
    except ClaudeAuthError:
        raise
    except ClaudeUsageError as err:
        _LOGGER.debug("Cost report unavailable: %s", err)
    else:
        cost_month = 0.0
        cost_today = 0.0
        for bucket in cost_buckets:
            bstart = _parse_dt(bucket.get("starting_at"))
            for result in bucket.get("results") or []:
                # Amounts are decimal strings in the lowest currency unit (cents).
                amount = float(result.get("amount") or 0) / 100
                cost_month += amount
                if bstart and bstart >= day_start:
                    cost_today += amount

    return AdminUsage(
        month_start=month_start,
        day_start=day_start,
        tokens_month=tokens_month,
        tokens_today=tokens_today,
        cost_month=round(cost_month, 4) if cost_month is not None else None,
        cost_today=round(cost_today, 4) if cost_today is not None else None,
        by_model_month=by_model,
    )
