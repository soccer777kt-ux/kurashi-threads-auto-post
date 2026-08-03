#!/usr/bin/env python3
"""Post the next prepared message to Threads using the official API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
POSTS_PATH = ROOT / "posts.json"
STATE_PATH = ROOT / "state.json"
API_BASE = "https://graph.threads.net/v1.0"
JST = ZoneInfo("Asia/Tokyo")

# Monday=0 ... Sunday=6. Times vary slightly so the account does not look mechanical.
DAILY_TARGETS = {
    0: {"morning": (7, 43), "lunch": (12, 18), "evening": (20, 37)},
    1: {"morning": (8, 12), "lunch": (11, 51), "evening": (21, 8)},
    2: {"morning": (7, 28), "lunch": (12, 42), "evening": (19, 53)},
    3: {"morning": (8, 36), "lunch": (13, 7), "evening": (20, 24)},
    4: {"morning": (7, 51), "lunch": (12, 29), "evening": (21, 16)},
    5: {"morning": (8, 47), "lunch": (11, 43), "evening": (19, 34)},
    6: {"morning": (8, 8), "lunch": (13, 21), "evening": (20, 48)},
}
MIN_POST_INTERVAL = timedelta(minutes=90)
SLOT_GRACE = timedelta(hours=2)


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def api_post(path: str, values: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(values).encode("utf-8")
    request = urllib.request.Request(f"{API_BASE}{path}", data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Threads API returned HTTP {error.code}: {detail}") from error


def current_due_slot(now: datetime) -> str | None:
    """Return only the most recent slot while it is still reasonably fresh."""
    targets = DAILY_TARGETS[now.weekday()]
    for slot_name in ("evening", "lunch", "morning"):
        hour, minute = targets[slot_name]
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now <= target + SLOT_GRACE:
            return slot_name
    return None


def scheduled_slot(state: dict, now: datetime) -> str | None:
    posted_slots = set(state.get("posted_slots", []))
    last_posted_at = state.get("last_posted_at")
    if last_posted_at:
        last_time = datetime.fromisoformat(last_posted_at)
        if now - last_time < MIN_POST_INTERVAL:
            return None

    slot_name = current_due_slot(now)
    if slot_name is None:
        return None
    slot_key = f"{now.date().isoformat()}-{slot_name}"
    return None if slot_key in posted_slots else slot_name


def publish_with_retry(creation_id: str, token: str) -> dict:
    last_error: Exception | None = None
    for attempt in range(6):
        if attempt:
            time.sleep(5)
        try:
            return api_post(
                "/me/threads_publish",
                {"creation_id": creation_id, "access_token": token},
            )
        except RuntimeError as error:
            last_error = error
            message = str(error)
            if "Media Not Found" not in message and "4279009" not in message:
                raise
    raise RuntimeError(f"Threadsの投稿準備が時間内に完了しませんでした: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--index", type=int)
    args = parser.parse_args()

    posts = load_json(POSTS_PATH, [])
    state = load_json(STATE_PATH, {"next_index": 0, "posted_slots": []})
    if not posts:
        print("投稿データがありません。")
        return 0

    now = datetime.now(JST)
    slot_name = None
    if args.scheduled:
        slot_name = scheduled_slot(state, now)
        if slot_name is None:
            print("現在は投稿時刻ではないか、この時間帯は投稿済みです。")
            return 0

    next_index = state.get("next_index", 0) if args.index is None else args.index
    if next_index >= len(posts):
        print("準備済みの投稿をすべて使い終わりました。")
        return 0

    text = posts[next_index]["text"] if isinstance(posts[next_index], dict) else posts[next_index]
    print(f"投稿番号: {next_index + 1}/{len(posts)}")
    print(text)

    if args.dry_run:
        print("\nドライランのためThreadsには投稿していません。")
        return 0

    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        print("THREADS_ACCESS_TOKENが設定されていません。", file=sys.stderr)
        return 1

    created = api_post(
        "/me/threads",
        {"media_type": "TEXT", "text": text, "access_token": token},
    )
    creation_id = created.get("id")
    if not creation_id:
        raise RuntimeError(f"投稿準備IDを取得できませんでした: {created}")

    published = publish_with_retry(creation_id, token)
    post_id = published.get("id")
    if not post_id:
        raise RuntimeError(f"投稿IDを取得できませんでした: {published}")

    if args.index is None:
        state["next_index"] = next_index + 1
    state["last_post_id"] = post_id
    state["last_posted_at"] = now.isoformat()
    completed_slot = slot_name or current_due_slot(now)
    if completed_slot:
        cutoff = (now.date() - timedelta(days=14)).isoformat()
        slots = [item for item in state.get("posted_slots", []) if item[:10] >= cutoff]
        slots.append(f"{now.date().isoformat()}-{completed_slot}")
        state["posted_slots"] = slots

    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"投稿しました。Threads post ID: {post_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
