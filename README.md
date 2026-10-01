# Claude Usage for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/stevengoossensB/ha-claude-usage/actions/workflows/validate.yml/badge.svg)](https://github.com/stevengoossensB/ha-claude-usage/actions/workflows/validate.yml)

Track your **Claude** usage in Home Assistant:

- **Subscription limits** (Claude Pro / Max, incl. Claude Code): 5-hour session and weekly usage %, model-specific weekly limits, reset times and extra-usage spend. The same numbers you see at claude.ai → Settings → Usage.
- **API usage** (pay-as-you-go, via an Anthropic Admin API key): input / output / cache tokens and cost (USD), today and month to date.

Add the integration more than once to track several accounts or both kinds at the same time.

Companion integrations: [ha-codex-usage](https://github.com/stevengoossensB/ha-codex-usage) · [ha-copilot-usage](https://github.com/stevengoossensB/ha-copilot-usage)

## Installation

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/stevengoossensB/ha-claude-usage` as type **Integration**.
2. Install **Claude Usage** and restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Claude Usage*.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=stevengoossensB&repository=ha-claude-usage&category=integration)

## Setup

### Subscription limits
1. Choose *Claude Pro/Max subscription limits*.
2. Click the sign-in link, approve access on claude.ai.
3. Copy the code Claude shows you (`xxxx#yyyy`) and paste it into the form.

Home Assistant gets its **own** OAuth session (the same public client Claude Code uses). It refreshes its token automatically and does **not** touch the login of Claude Code on your computer. If the session ever expires, Home Assistant asks you to re-authenticate.

### API usage
1. Create an Admin API key (`sk-ant-admin…`) in the Claude Console → Settings → Admin keys (organisation admins only).
2. Choose *Anthropic API token usage and cost* and paste the key.

## Entities

| Mode | Entity | Notes |
|---|---|---|
| Subscription | `Session (5h) usage` | % of the rolling 5-hour limit used |
| Subscription | `Session (5h) reset` | timestamp |
| Subscription | `Weekly usage` / `Weekly reset` | all-model weekly limit |
| Subscription | `Weekly <model> usage` / `reset` | created automatically when Anthropic reports a model-specific limit (e.g. Opus) |
| Subscription | `Extra usage` | % of your monthly extra-usage cap, raw details in attributes |
| API | `Total / Input / Output / Cache read / Cache write tokens this month` | `state_class: total`, resets on the 1st (UTC) |
| API | `… tokens today` | UTC day |
| API | `Cost this month`, `Cost today` | USD |

Default polling: 5 min (subscription), 30 min (API). Change it under *Configure*.

## Example automation

Entity IDs start with the entry title (e.g. `sensor.claude_you_example_com_weekly_usage`); adjust to yours.

```yaml
automation:
  - alias: Claude weekly limit almost used
    triggers:
      - trigger: numeric_state
        entity_id: sensor.claude_weekly_usage
        above: 90
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "Claude weekly usage at {{ states('sensor.claude_weekly_usage') }}%"
```

## Notes & caveats

- The subscription endpoint (`api.anthropic.com/api/oauth/usage`) is the one Claude Code itself uses. It is not a documented public API and may change; the parser is deliberately tolerant and new model-specific limits appear as new sensors.
- The endpoint is rate limited. Keep the polling interval at 2 minutes or more.
- Admin API data lags a few minutes behind real time. Days and months are in UTC.

## License

MIT
