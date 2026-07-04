"""
Schedule gate for running the digest under a timezone-naive cron (GitHub Actions
cron is fixed UTC with no DST awareness). Run this hourly; it decides whether
"now" is close enough to config.yaml's schedule.send_time, evaluated in
schedule.timezone, to actually run the pipeline.

To keep the digest landing in your local morning while traveling, just edit
schedule.timezone in config.yaml to your current IANA timezone (e.g.
"Asia/Tokyo") and commit/push — no code or cron change needed.

Also guards against double-sends: if a digest already went out today (in the
target timezone), it will not fire again even if the cron window matches twice.

Usage: python scripts/check_schedule.py
Exits 0 always; writes should_run=true/false to $GITHUB_OUTPUT if set, and to stdout.
"""

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

WINDOW_MINUTES = 25  # keep < 30 so two consecutive hourly cron firings can't both match


def already_sent_today(tz: ZoneInfo, now: datetime) -> bool:
    db_path = Path(__file__).parent.parent / "data" / "digest.db"
    if not db_path.exists():
        return False
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT sent_at FROM digests ORDER BY sent_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return False
    last_sent = datetime.fromisoformat(row[0])
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=tz)
    else:
        last_sent = last_sent.astimezone(tz)
    return last_sent.date() == now.date()


def main():
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    schedule = config.get("schedule", {})
    tz_name = schedule.get("timezone", "UTC")
    send_time = schedule.get("send_time", "07:00")

    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    target_hour, target_minute = (int(x) for x in send_time.split(":"))
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)

    within_window = abs((now - target).total_seconds()) <= WINDOW_MINUTES * 60
    should_run = within_window and not already_sent_today(tz, now)

    print(f"  Now: {now.isoformat()} ({tz_name})")
    print(f"  Target: {target.isoformat()}")
    print(f"  Within window: {within_window}")
    print(f"  Already sent today: {already_sent_today(tz, now)}")
    print(f"  should_run: {should_run}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"should_run={'true' if should_run else 'false'}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
