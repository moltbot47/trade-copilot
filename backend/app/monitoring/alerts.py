"""Health-alerting layer.

Tracks consecutive failure events and fires a Discord alert when a
streak exceeds a threshold. Currently watches:

  - skip_synthetic_data:  bot is running on fabricated bars (broker
                          unreachable) — bot will NOT trade. Alert
                          after 5 in a row.
  - token_refresh_failed: proactive_refresh_all returned 0 successes
                          across all users (likely encryption-key
                          rotation or TL outage). Alert once per
                          consecutive failure window.

Each alert is rate-limited via a per-stream cooldown so a sustained
outage produces ONE alert + a recovery message, not a flood.

DISABLED BY DEFAULT (2026-05-13). The synthetic-streak alerts were
firing constantly because the BarFetcher token expires every ~30 min
and falls back to synthetic on every tick until it refreshes, which
in turn produces a sustained streak that re-alerts every 10 min per
symbol. Operator asked to remove this noise from the signals channel.

Streak tracking still happens in-process so we can re-enable if needed.
Set ``HEALTH_ALERTS_ENABLED=1`` to turn Discord posts back on.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


SYNTHETIC_STREAK_THRESHOLD = 5
ALERT_COOLDOWN_SECONDS = 600.0  # 10 minutes between alerts on the same stream
_ALERTS_ENABLED = os.getenv("HEALTH_ALERTS_ENABLED", "0") == "1"


@dataclass
class _StreakState:
    count: int = 0
    last_alert_ts: float = 0.0
    last_value_ts: float = 0.0


@dataclass
class HealthAlerter:
    """In-process streak tracker + Discord alert publisher.

    Singleton via get_alerter(). Streak counters live in-memory only;
    on process restart we start clean — that's deliberate, so an alert
    isn't sent for a streak that began on a prior process.
    """
    _streaks: dict[str, _StreakState] = field(default_factory=dict)

    def record(self, stream: str, failure: bool) -> Optional[str]:
        """Record an event. Returns the alert message if this triggers one,
        else None. ``failure=True`` increments the streak; False resets it
        and emits a recovery message if we previously alerted.
        """
        state = self._streaks.setdefault(stream, _StreakState())
        state.last_value_ts = time.monotonic()

        if not failure:
            recovered = state.count >= SYNTHETIC_STREAK_THRESHOLD
            state.count = 0
            if recovered and state.last_alert_ts > 0:
                state.last_alert_ts = 0.0
                return f"✅ RECOVERED: `{stream}` streak ended"
            return None

        state.count += 1
        if state.count < SYNTHETIC_STREAK_THRESHOLD:
            return None

        now = time.monotonic()
        # Cooldown only applies once we've alerted at least once on this stream.
        # last_alert_ts == 0.0 means "never alerted" — don't gate the first alert
        # on monotonic time (which can be < ALERT_COOLDOWN_SECONDS at process start).
        if state.last_alert_ts > 0 and (now - state.last_alert_ts) < ALERT_COOLDOWN_SECONDS:
            return None  # already alerted recently
        state.last_alert_ts = now
        return f"🚨 ALERT: `{stream}` streak at {state.count} consecutive failures"

    def reset(self) -> None:
        """Test hook: clear all state."""
        self._streaks.clear()


_alerter_singleton: Optional[HealthAlerter] = None


def get_alerter() -> HealthAlerter:
    global _alerter_singleton
    if _alerter_singleton is None:
        _alerter_singleton = HealthAlerter()
    return _alerter_singleton


async def _post_alert(message: str) -> bool:
    """Send the alert message to the configured Discord webhook.
    Returns True on success. Best-effort; logs warnings on failure.

    Returns early without posting if HEALTH_ALERTS_ENABLED is not set —
    streak tracking still happens in-process; only the Discord side
    is muted.
    """
    if not _ALERTS_ENABLED:
        return False
    from app.integrations.discord_signals import _webhook_url

    url = _webhook_url()
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(url, json={"content": message})
            if r.status_code >= 400:
                logger.warning("alert webhook returned %s: %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as exc:
        logger.warning("alert post failed: %s", exc)
        return False


async def maybe_alert(stream: str, failure: bool) -> None:
    """Convenience wrapper: record the event and post if it triggered one."""
    msg = get_alerter().record(stream, failure)
    if msg:
        await _post_alert(msg)
