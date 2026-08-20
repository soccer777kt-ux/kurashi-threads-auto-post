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
AFFILIATE_POSTS_PATH = ROOT / "affiliate_posts.json"
STATE_PATH = ROOT / "state.json"
API_BASE = "https://graph.threads.net/v1.0"
JST = ZoneInfo("Asia/Tokyo")
TOKYO_WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=35.6762&longitude=139.6503"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
    "precipitation_probability_max"
    "&timezone=Asia%2FTokyo&forecast_days=1"
)

# Three daily posting windows in Japan time. The workflow starts shortly before
# each window, and this script picks one stable pseudo-random minute per day.
# Fallback workflow runs calculate the same target, so they cannot move the post
# or create a duplicate after a successful primary run.
DAILY_WINDOWS = {
    "morning": ((7, 30), (8, 0)),
    "lunch": ((12, 0), (13, 0)),
    "night": ((20, 0), (21, 0)),
}
DAILY_TARGETS = {weekday: DAILY_WINDOWS for weekday in range(7)}
MIN_POST_INTERVAL = timedelta(minutes=60)
# GitHub's scheduled runs are best-effort and may arrive late.  Keep each slot
# recoverable for over three hours, while still stopping before the next slot.
SLOT_GRACE = timedelta(hours=3, minutes=15)
SLOT_TARGET_OFFSET_MINUTES = 7
SLOT_TARGET_STEP_MINUTES = 10
# Primary GitHub Actions runs start shortly before each window and wait for the
# stable random target. This avoids depending on many best-effort cron events.
SLOT_EARLY_START = timedelta(minutes=15)
MAX_TARGET_WAIT = timedelta(minutes=75)
WEATHER_WEEKDAYS = {0, 2, 5}  # Monday, Wednesday, Saturday
AFFILIATE_SCHEDULE = {
    1: {"night"},  # Tuesday night
    4: {"lunch"},  # Friday lunch
}
NON_WEATHER_MORNING_POSTS = (
    (
        "おはようございます🌿\n\n"
        "朝からもう疲れた、って思う日もある。\n"
        "まだ一日が始まったばかりなのにね😂\n\n"
        "今日は全部できなくていい。\n"
        "大事なことがひとつ進めば十分◎"
    ),
    (
        "おはようございます🌿\n\n"
        "5歳と2歳に同時に話しかけられる朝。\n"
        "返事をしただけで、もう次の用事😂\n\n"
        "ちゃんと朝を回してる。\n"
        "それだけでもう花丸です◎"
    ),
    (
        "おはようございます🌿\n\n"
        "「早くして」と言いたくないのに、\n"
        "時計を見ると言ってしまう朝もある。\n\n"
        "そんな日も大丈夫。\n"
        "今日また一緒に笑えたら、それで十分◎"
    ),
    (
        "おはようございます🌿\n\n"
        "朝ごはんが簡単でも、\n"
        "洗濯物が昨日のままでも大丈夫。\n\n"
        "完璧な朝より、無事な朝。\n"
        "今日も自分にやさしくいこう。"
    ),
    (
        "おはようございます🌿\n\n"
        "自分のことは最後になりがちな朝。\n"
        "家族の支度をしている間に、もうこんな時間😂\n\n"
        "温かい飲み物をひと口だけでも。\n"
        "自分の分も忘れずに◎"
    ),
    (
        "おはようございます🌿\n\n"
        "家を出る直前に限って、\n"
        "探し物が始まるのはなぜだろう😂\n\n"
        "今朝いちばん探した物、何でした？"
    ),
    (
        "おはようございます🌿\n\n"
        "今日も頑張る、より\n"
        "今日は何を頑張らないか決めたい朝。\n\n"
        "ひとつ手放すなら、何にしますか？"
    ),
)


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


def weather_label(code: int) -> tuple[str, str]:
    """Return a short Japanese forecast label and emoji for a WMO weather code."""
    if code == 0:
        return "晴れ", "☀️"
    if code == 1:
        return "晴れ時々くもり", "🌤️"
    if code == 2:
        return "晴れたりくもったり", "⛅"
    if code == 3:
        return "くもり", "☁️"
    if code in (45, 48):
        return "霧", "🌫️"
    if 51 <= code <= 57:
        return "小雨", "🌦️"
    if 61 <= code <= 67:
        return "雨", "☔"
    if 71 <= code <= 77:
        return "雪", "❄️"
    if 80 <= code <= 82:
        return "にわか雨", "🌦️"
    if code in (85, 86):
        return "にわか雪", "🌨️"
    if 95 <= code <= 99:
        return "雷雨", "⛈️"
    return "変わりやすい空", "🌤️"


def fetch_tokyo_weather() -> dict | None:
    """Fetch today's Tokyo forecast; return None so posting can continue on failure."""
    request = urllib.request.Request(
        TOKYO_WEATHER_URL,
        headers={"User-Agent": "kurashi-threads-auto-post/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        daily = payload["daily"]
        code = int(daily["weather_code"][0])
        description, emoji = weather_label(code)
        return {
            "code": code,
            "description": description,
            "emoji": emoji,
            "max_temp": round(float(daily["temperature_2m_max"][0])),
            "min_temp": round(float(daily["temperature_2m_min"][0])),
            "rain_probability": round(
                float(daily["precipitation_probability_max"][0])
            ),
        }
    except (KeyError, IndexError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"東京の天気を取得できなかったため通常の朝投稿にします: {error}")
        return None


def stable_daily_choice(items: tuple[str, ...], now: datetime, salt: str) -> str:
    seed = f"{now.date().isoformat()}:{salt}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return items[int.from_bytes(digest[:8], "big") % len(items)]


def build_morning_post(now: datetime) -> str:
    """Alternate weather mornings with short encouragement and empathy mornings."""
    if now.weekday() not in WEATHER_WEEKDAYS:
        return stable_daily_choice(NON_WEATHER_MORNING_POSTS, now, "morning-empathy")

    weather = fetch_tokyo_weather()
    if weather is None:
        return stable_daily_choice(NON_WEATHER_MORNING_POSTS, now, "weather-fallback")

    code = weather["code"]
    rain_probability = weather["rain_probability"]
    max_temp = weather["max_temp"]
    min_temp = weather["min_temp"]

    if rain_probability >= 50 or 51 <= code <= 67 or 80 <= code <= 82:
        weather_posts = (
            (
                "おはようございます☔\n\n"
                "雨の朝って、傘を持たせるだけでひと仕事。\n"
                "いつもより少し遅くても大丈夫。\n"
                "家を出られたら、もう花丸◎\n\n"
                "雨の日の朝、何がいちばん大変？"
            ),
            (
                "おはようございます☔\n\n"
                "雨音を聞いた瞬間、\n"
                "送迎の大変さが頭に浮かぶ朝😂\n\n"
                "今日も完璧じゃなくていい。\n"
                "無事に送り出せたら100点◎\n\n"
                "雨の日、どうやって乗り切ってる？"
            ),
        )
        weather_salt = "weather-rain-empathy"
    elif max_temp >= 30:
        weather_posts = (
            (
                "おはようございます☀️\n\n"
                "朝から暑いだけで、ちょっと疲れるよね😂\n"
                "子どもの支度をして、自分も動いてる。\n"
                "それだけで十分頑張ってる◎\n\n"
                "今日は無理せずいこう。"
            ),
            (
                "おはようございます🌻\n\n"
                "暑い朝は、ちゃんと起きただけでもえらい。\n"
                "水分とって、できることからひとつずつ◎\n\n"
                "今日の自分への小さなご褒美、何にする？"
            ),
        )
        weather_salt = "weather-hot-empathy"
    elif min_temp <= 10:
        weather_posts = (
            (
                "おはようございます🧣\n\n"
                "寒い朝って、布団から出るだけでもひと仕事。\n"
                "家族を起こして支度してる時点で、もう十分えらい◎\n\n"
                "今日もゆっくり始めよう。"
            ),
            (
                "おはようございます☕\n\n"
                "寒い朝は、子どもも大人も動きたくない😂\n"
                "急がせすぎなくて大丈夫。\n"
                "ひとつ進めば、それで花丸◎"
            ),
        )
        weather_salt = "weather-cold-empathy"
    else:
        weather_posts = (
            (
                "おはようございます🌿\n\n"
                "朝って、始まった瞬間からやることだらけ。\n"
                "全部できなくても大丈夫。\n"
                "家族が動き出したら、それだけで花丸◎\n\n"
                "今日も自分にやさしくいこう。"
            ),
            (
                "おはようございます🌿\n\n"
                "思い通りに進まない朝もある。\n"
                "それでも今日を始めてる私たち、ちゃんと頑張ってる◎\n\n"
                "今日は何をひとつ手放す？"
            ),
        )
        weather_salt = "weather-mild-empathy"

    return stable_daily_choice(weather_posts, now, weather_salt)


def slot_timing(now: datetime, slot_name: str) -> tuple[datetime, datetime, datetime]:
    """Return window start, the day's random target, and window end."""
    start_hm, end_hm = DAILY_TARGETS[now.weekday()][slot_name]
    start = now.replace(hour=start_hm[0], minute=start_hm[1], second=0, microsecond=0)
    end = now.replace(hour=end_hm[0], minute=end_hm[1], second=0, microsecond=0)
    available_minutes = int((end - start).total_seconds() // 60)
    seed = f"kurashi_yutakanii:{now.date().isoformat()}:{slot_name}:v1"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    # Pick from the same off-hour minutes used by GitHub Actions.  This keeps
    # the post inside the requested window without a long-running sleep job.
    candidates = list(
        range(SLOT_TARGET_OFFSET_MINUTES, available_minutes, SLOT_TARGET_STEP_MINUTES)
    )
    if not candidates:
        candidates = [0]
    offset_minutes = candidates[int.from_bytes(digest[:8], "big") % len(candidates)]
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


def scheduled_slot(
    state: dict, now: datetime, allow_future: bool = False
) -> tuple[str, datetime] | None:
    """Return one unposted slot that is due now or safe to wait for.

    Primary scheduled jobs may start shortly before a window and wait for the
    stable random target. Recovery jobs arriving after the target publish
    immediately. Posted-slot state still prevents duplicates.
    """
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
        if target <= now <= end + SLOT_GRACE:
            return slot_name, target
        if (
            allow_future
            and start - SLOT_EARLY_START <= now < target
            and target - now <= MAX_TARGET_WAIT
        ):
            return slot_name, target
    return None


def is_affiliate_slot(now: datetime, slot_name: str | None) -> bool:
    """Return True for the two weekly affiliate posting slots."""
    return bool(slot_name and slot_name in AFFILIATE_SCHEDULE.get(now.weekday(), set()))


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


def save_state(state: dict) -> None:
    """Persist posting state, including any affiliate reply awaiting recovery."""
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def publish_text_reply(parent_post_id: str, reply_text: str, token: str) -> str:
    """Publish a text reply beneath an already-published Threads post."""
    created = api_post(
        "/me/threads",
        {
            "media_type": "TEXT",
            "text": reply_text,
            "reply_to_id": parent_post_id,
            "access_token": token,
        },
    )
    creation_id = created.get("id")
    if not creation_id:
        raise RuntimeError(f"返信準備IDを取得できませんでした: {created}")

    published = publish_with_retry(creation_id, token)
    reply_id = published.get("id")
    if not reply_id:
        raise RuntimeError(f"返信IDを取得できませんでした: {published}")
    return reply_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--wait-until-target", action="store_true")
    parser.add_argument("--index", type=int)
    args = parser.parse_args()

    posts = load_json(POSTS_PATH, [])
    affiliate_posts = load_json(AFFILIATE_POSTS_PATH, [])
    state = load_json(STATE_PATH, {"next_index": 0, "posted_slots": []})
    now = datetime.now(JST)
    print(f"実行時刻（日本時間）: {now.isoformat()}")

    token = os.environ.get("THREADS_ACCESS_TOKEN")
    pending_reply = state.get("pending_affiliate_reply")
    if pending_reply and not args.dry_run:
        if not token:
            print("THREADS_ACCESS_TOKENが設定されていません。", file=sys.stderr)
            return 1
        print("未完了のAmazonリンク返信を復旧します。")
        reply_id = publish_text_reply(
            pending_reply["parent_post_id"], pending_reply["text"], token
        )
        state.pop("pending_affiliate_reply", None)
        state["last_affiliate_reply_id"] = reply_id
        save_state(state)
        print(f"Amazonリンクを返信しました。Threads reply ID: {reply_id}")
        return 0

    slot_name = None
    if args.scheduled:
        due = scheduled_slot(state, now, allow_future=args.wait_until_target)
        if due is None:
            print("現在は投稿時間帯ではないか、この時間帯は投稿済みです。")
            return 0
        slot_name, target_time = due
        print(f"本日のランダム投稿時刻: {target_time.isoformat()}")
        print(f"対象時間帯: {slot_name}")
        if target_time > now:
            wait_seconds = (target_time - now).total_seconds()
            print(f"ランダム投稿時刻まで{round(wait_seconds)}秒待機します。")
            time.sleep(wait_seconds)
            now = datetime.now(JST)
            print(f"待機完了時刻（日本時間）: {now.isoformat()}")

    is_affiliate = False
    affiliate_index = None
    if slot_name == "morning":
        next_index = None
        post = {"text": build_morning_post(now)}
    elif args.scheduled and is_affiliate_slot(now, slot_name) and affiliate_posts:
        is_affiliate = True
        next_index = None
        affiliate_index = state.get("next_affiliate_index", 0)
        post = affiliate_posts[affiliate_index % len(affiliate_posts)]
    else:
        if not posts:
            print("投稿データがありません。")
            return 0
        if args.index is None:
            # Continue rotating prepared posts instead of silently stopping when
            # the end of posts.json is reached.
            next_index = state.get("next_index", 0) % len(posts)
        else:
            next_index = args.index
            if not 0 <= next_index < len(posts):
                print("指定された投稿番号が範囲外です。", file=sys.stderr)
                return 1
        post = posts[next_index]

    text = post["text"] if isinstance(post, dict) else post
    reply_text = post.get("reply_text") if is_affiliate else None
    image_path = post.get("image_path") if isinstance(post, dict) else None
    if is_affiliate:
        if not reply_text:
            raise RuntimeError("Amazon投稿のreply_textが設定されていません。")
        if "http://" in text or "https://" in text:
            raise RuntimeError("Amazonリンクは本文ではなくreply_textに設定してください。")
    if slot_name == "morning":
        print("朝の天気・共感投稿")
    elif is_affiliate:
        print(
            f"Amazonアソシエイト投稿: "
            f"{affiliate_index % len(affiliate_posts) + 1}/{len(affiliate_posts)}"
        )
    else:
        print(f"投稿番号: {next_index + 1}/{len(posts)}")
    print(text)
    if reply_text:
        print("\n投稿後の最初の返信:")
        print(reply_text)
    if image_path:
        print(f"画像: {image_path}")

    if args.dry_run:
        print("\nドライランのためThreadsには投稿していません。")
        return 0

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

    if args.index is None and next_index is not None:
        state["next_index"] = (next_index + 1) % len(posts)
    if is_affiliate and affiliate_index is not None:
        state["next_affiliate_index"] = affiliate_index + 1
    state["last_post_id"] = post_id
    state["last_posted_at"] = now.isoformat()
    completed_slot = slot_name or current_due_slot(now)
    if completed_slot:
        cutoff = (now.date() - timedelta(days=14)).isoformat()
        slots = [item for item in state.get("posted_slots", []) if item[:10] >= cutoff]
        slots.append(f"{now.date().isoformat()}-{completed_slot}")
        state["posted_slots"] = slots
    print(f"投稿しました。Threads post ID: {post_id}")

    if reply_text:
        # Save the parent immediately. If the reply API call fails, the workflow's
        # always-run state step commits this recovery record. The next scheduled
        # run retries only the reply instead of duplicating the parent post.
        state["pending_affiliate_reply"] = {
            "parent_post_id": post_id,
            "text": reply_text,
            "product_id": post.get("product_id"),
        }
        save_state(state)
        reply_id = publish_text_reply(post_id, reply_text, token)
        state.pop("pending_affiliate_reply", None)
        state["last_affiliate_reply_id"] = reply_id
        print(f"Amazonリンクを返信しました。Threads reply ID: {reply_id}")

    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
