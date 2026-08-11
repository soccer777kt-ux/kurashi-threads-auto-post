#!/usr/bin/env python3
"""Post the next prepared message to Threads using the official API."""

from __future__ import annotations

import argparse
import hashlib
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

# Two daily posting windows in Japan time. The workflow starts shortly before
# each window, and this script picks one stable pseudo-random minute per day.
# Fallback workflow runs calculate the same target, so they cannot move the post
# or create a duplicate after a successful primary run.
DAILY_WINDOWS = {
    "lunch": ((12, 0), (13, 0)),
    "night": ((20, 0), (21, 0)),
}
DAILY_TARGETS = {weekday: DAILY_WINDOWS for weekday in range(7)}
MIN_POST_INTERVAL = timedelta(minutes=60)
EARLY_START = timedelta(minutes=10)
SLOT_GRACE = timedelta(minutes=15)


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


def slot_timing(now: datetime, slot_name: str) -> tuple[datetime, datetime, datetime]:
    """Return window start, the day's random target, and window end."""
    start_hm, end_hm = DAILY_TARGETS[now.weekday()][slot_name]
    start = now.replace(hour=start_hm[0], minute=start_hm[1], second=0, microsecond=0)
    end = now.replace(hour=end_hm[0], minute=end_hm[1], second=0, microsecond=0)
    available_minutes = int((end - start).total_seconds() // 60)
    seed = f"kurashi_yutakanii:{now.date().isoformat()}:{slot_name}:v1"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    offset_minutes = int.from_bytes(digest[:8], "big") % available_minutes
    target = start + timedelta(minutes=offset_minutes)
    return start, target, end


def current_due_slot(now: datetime) -> str | None:
    """Return the current random slot while it is still reasonably fresh."""
    targets = DAILY_TARGETS[now.weekday()]
    latest_first = sorted(targets, key=lambda name: targets[name][0], reverse=True)
    for slot_name in latest_first:
        _, target, end = slot_timing(now, slot_name)
        if target <= now <= end + SLOT_GRACE:
            return slot_name
    return None


def scheduled_slot(state: dict, now: datetime) -> tuple[str, datetime] | None:
    """Return one unposted slot and its stable random target time."""
    posted_slots = set(state.get("posted_slots", []))
    last_posted_at = state.get("last_posted_at")
    if last_posted_at:
        last_time = datetime.fromisoformat(last_posted_at)
        if now - last_time < MIN_POST_INTERVAL:
            return None

    targets = DAILY_TARGETS[now.weekday()]
    for slot_name in sorted(targets, key=lambda name: targets[name][0]):
        start, target, end = slot_timing(now, slot_name)
        slot_key = f"{now.date().isoformat()}-{slot_name}"
        if slot_key in posted_slots:
            continue
        if start - EARLY_START <= now <= end + SLOT_GRACE:
            return slot_name, target
    return None


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
        due = scheduled_slot(state, now)
        if due is None:
            print("現在は投稿時間帯ではないか、この時間帯は投稿済みです。")
            return 0
        slot_name, target_time = due
        if now < target_time:
            wait_seconds = (target_time - now).total_seconds()
            print(f"本日のランダム投稿時刻: {target_time.isoformat()}")
            print(f"投稿時刻まで約{int(wait_seconds // 60)}分待機します。")
            time.sleep(wait_seconds)
            now = datetime.now(JST)
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
