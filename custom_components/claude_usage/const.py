"""Constants for the Claude Usage integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "claude_usage"

CONF_AUTH_TYPE: Final = "auth_type"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_EXPIRES_AT: Final = "expires_at"
CONF_ADMIN_KEY: Final = "admin_key"
CONF_SCAN_INTERVAL: Final = "scan_interval"

AUTH_SUBSCRIPTION: Final = "subscription"
AUTH_ADMIN: Final = "admin_api"

DEFAULT_SCAN_SUBSCRIPTION: Final = timedelta(minutes=5)
DEFAULT_SCAN_ADMIN: Final = timedelta(minutes=30)
MIN_SCAN_INTERVAL: Final = 60  # seconds

# OAuth (same public client the Claude Code CLI uses).
OAUTH_CLIENT_ID: Final = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL: Final = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL: Final = "https://platform.claude.com/v1/oauth/token"
OAUTH_REDIRECT_URI: Final = "https://platform.claude.com/oauth/code/callback"
OAUTH_SCOPES: Final = "user:profile user:inference"

USAGE_URL: Final = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL: Final = "https://api.anthropic.com/api/oauth/profile"
OAUTH_BETA: Final = "oauth-2025-04-20"
# Anthropic rate limits the token endpoint for claude-code/* user agents from
# non-CLI clients, so identify honestly as this integration.
USER_AGENT: Final = "ha-claude-usage/0.1.1"

# Admin API
ADMIN_BASE_URL: Final = "https://api.anthropic.com/v1/organizations"
ANTHROPIC_VERSION: Final = "2023-06-01"
