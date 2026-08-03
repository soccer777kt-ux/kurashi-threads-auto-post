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

# Monday=0 ... Sunday=6. Keep these aligned with the primary workflow schedules.
# Each primary run also has a workflow-level retry 30 minutes later. The state file
# prevents that retry from creating a duplicate after a successful primary run.
DAILY_SLOTS = {
    "early": (7, 50),
    "late_morning": (10, 30),
    "lunch": (12, 50),
    "evening": (18, 0),
    "night": (20, 40),
}
DAILY_TARGETS = {weekday: DAILY_SLOTS for weekday in range(7)}
MIN_POST_INTERVAL = timedelta(minutes=90)
SLOT_GRACE = timedelta(minutes=90)


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
    latest_first = sorted(targets.items(), key=lambda item: item[1], reverse=True)
    for slot_name, (hour, minute) in latest_first:
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
    print(f"実行時刻（日本時間）: {now.isoformat()}")
    slot_name = None
    if args.scheduled:
        slot_name = scheduled_slot(state, now)
        if slot_name is None:
            print("現在は投稿時刻ではないか、この時間帯は投稿済みです。")
            return 0
        print(f"対象時間帯: {slot_name}")

    next_index = state.get("next_index", 0) if args.index is None else args.index
    if next_index >= len(posts):
        print("準備済みの投稿をすべて使い終わりました。")
        return 0

    post = posts[next_index]
    text = post["text"] if isinstance(post, dict) else post
    image_path = post.get("image_path") if isinstance(post, dict) else None
    print(f"投稿番号: {next_index + 1}/{len(posts)}")
    print(text)
    if image_path:
        print(f"画像: {image_path}")

    if args.dry_run:
        print("\nドライランのためThreadsには投稿していません。")
        return 0

    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        print("THREADS_ACCESS_TOKENが設定されていません。", file=sys.stderr)
        return 1

    creation_values = {"media_type": "TEXT", "text": text, "access_token": token}
    if image_path:
        image_file = (ROOT / image_path).resolve()
        try:
            image_file.relative_to(ROOT)
        except ValueError as error:
            raise RuntimeError(f"画像パスが不正です: {image_path}") from error
        if not image_file.is_file():
            raise RuntimeError(f"画像ファイルが見つかりません: {image_path}")

        repository = os.environ.get(
            "GITHUB_REPOSITORY", "soccer777kt-ux/kurashi-threads-auto-post"
        )
        revision = os.environ.get("GITHUB_SHA", "main")
        encoded_path = urllib.parse.quote(image_path, safe="/")
        image_url = (
            f"https://raw.githubusercontent.com/{repository}/{revision}/{encoded_path}"
        )
        creation_values.update({"media_type": "IMAGE", "image_url": image_url})

    created = api_post("/me/threads", creation_values)
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
