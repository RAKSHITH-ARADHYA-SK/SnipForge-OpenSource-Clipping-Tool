"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import datetime
import json
import re
import secrets
import threading
import time
from pathlib import Path

from . import accounts, alerts, auth, brain, config, creators, ctx, jobs, live

# IST timezone: UTC + 5:30
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Weekly upload slots (hour, minute) in IST:
# Full schedule (8 shorts/day):
# Monday–Thursday: 7:30 AM, 8:30 AM, 10:00 AM, 12:00 PM, 2:00 PM, 4:30 PM, 6:30 PM, 8:30 PM
MON_THU_SLOTS = [(7, 30), (8, 30), (10, 0), (12, 0), (14, 0), (16, 30), (18, 30), (20, 30)]
# Friday: Same as Monday–Thursday
FRI_SLOTS = [(7, 30), (8, 30), (10, 0), (12, 0), (14, 0), (16, 30), (18, 30), (20, 30)]
# Saturday–Sunday: 7:30 AM, 8:30 AM, 9:30 AM, 11:30 AM, 1:30 PM, 3:30 PM, 5:30 PM, 7:30 PM (9:30 PM & 11:00 PM removed)
WEEKEND_SLOTS = [(7, 30), (8, 30), (9, 30), (11, 30), (13, 30), (15, 30), (17, 30), (19, 30)]

# First 3 weeks schedule (4 shorts/day, prime peak times) in IST:
# Monday–Thursday: 7:30 AM, 8:30 AM, 2:00 PM, 6:30 PM
MON_THU_4_SLOTS = [(7, 30), (8, 30), (14, 0), (18, 30)]
# Friday: 7:30 AM, 8:30 AM, 2:00 PM, 6:30 PM
FRI_4_SLOTS = [(7, 30), (8, 30), (14, 0), (18, 30)]
# Saturday–Sunday: 7:30 AM, 8:30 AM, 3:30 PM, 7:30 PM (11:00 PM removed)
WEEKEND_4_SLOTS = [(7, 30), (8, 30), (15, 30), (19, 30)]

# Channel 2 schedule (historical 3 shorts/day before 20 Nov 2026: 6:30 AM, 2:30 PM, 7:30 PM IST):
CH2_SLOTS_UNTIL_DEC31 = [(6, 30), (14, 30), (19, 30)]
END_DATE_CH2 = datetime.date(2026, 11, 19)

# Fixed schedule boundaries in IST:
# First 3 Weeks (historical 4 shorts/day before 20 Nov 2026)
START_DATE_3W = datetime.date(2026, 11, 15)
END_DATE_3W = datetime.date(2026, 11, 19)

# 8 videos/day schedule active from Nov 20, 2026 onwards for both channels
START_DATE_8_PER_DAY = datetime.date(2026, 11, 20)


def _schedule_start_date() -> datetime.date:
    return START_DATE_8_PER_DAY


def _is_channel_2(account_id: str = None) -> bool:
    """True if this account is the second YouTube channel."""
    if not account_id:
        return False
    try:
        acc = accounts.get(account_id)
        if not acc:
            return False
        if acc.get("channel_index") == 2 or acc.get("is_channel_2"):
            return True
        name_lower = (acc.get("name") or "").lower()
        if "channel 2" in name_lower or "ch2" in name_lower or "second" in name_lower or "virox" in name_lower:
            return True
        yt_accs = sorted([a for a in accounts._all() if a.get("platform") == "youtube"], key=lambda x: x.get("added", 0))
        if len(yt_accs) >= 2 and yt_accs[1]["id"] == account_id:
            return True
    except Exception:
        pass
    return False


def _is_first_3_weeks(target_date: datetime.date) -> bool:
    """True if target_date falls within the initial phase before Nov 20, 2026."""
    return START_DATE_3W <= target_date <= END_DATE_3W


def _max_shorts_for_date(target_date: datetime.date, account_id: str = None) -> int:
    """Both channels: 8 shorts/day from Nov 20, 2026 onwards.
    Historical: Ch2 was 3 shorts/day, Ch1 was 4 shorts/day before Nov 20, 2026."""
    if target_date >= START_DATE_8_PER_DAY:
        return 8
    if _is_channel_2(account_id):
        return 3 if target_date <= END_DATE_CH2 else 8
    return 4 if _is_first_3_weeks(target_date) else 8


def _slots_for_date(target_date: datetime.date, account_id: str = None):
    """Return the daily slot times in IST based on account channel rules and date."""
    if target_date >= START_DATE_8_PER_DAY:
        wd = target_date.weekday()
        if wd < 4:
            return MON_THU_SLOTS
        if wd == 4:
            return FRI_SLOTS
        return WEEKEND_SLOTS

    if _is_channel_2(account_id):
        if target_date <= END_DATE_CH2:
            return CH2_SLOTS_UNTIL_DEC31
        wd = target_date.weekday()
        if wd < 4:
            return MON_THU_SLOTS
        if wd == 4:
            return FRI_SLOTS
        return WEEKEND_SLOTS

    wd = target_date.weekday()
    if _is_first_3_weeks(target_date):
        if wd < 4:
            return MON_THU_4_SLOTS
        if wd == 4:
            return FRI_4_SLOTS
        return WEEKEND_4_SLOTS
    else:
        if wd < 4:
            return MON_THU_SLOTS
        if wd == 4:
            return FRI_SLOTS
        return WEEKEND_SLOTS


def _slots_for_weekday(wd: int):
    """Legacy helper for weekday slots (8 slots)."""
    if wd < 4:
        return MON_THU_SLOTS
    if wd == 4:
        return FRI_SLOTS
    return WEEKEND_SLOTS


_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    try:
        u = ctx.uid()
    except Exception:
        u = "u_local"
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def _day(t):
    try:
        return datetime.datetime.fromtimestamp(t, tz=IST).strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error", "views", "likes", "comments")}
        jobs.save(job)


def _next_slot(account_id, gap_min=None, cap=None):
    """Earliest slot from the weekly schedule (IST) that is not already occupied for this account.
    From Nov 20, 2026 onwards: 8 shorts/day on both channels.
    Any extra automatically rolls over to the next day."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    taken = {round(p["due_at"]) for p in mine if p.get("due_at")}
    now_ts = time.time()
    now_ist = datetime.datetime.now(IST)

    # Search up to 90 days for the earliest free scheduled slot
    for day_offset in range(90):
        target_date = (now_ist + datetime.timedelta(days=day_offset)).date()
        slots = _slots_for_date(target_date, account_id)
        max_daily = _max_shorts_for_date(target_date, account_id)

        # Strictly enforce daily cap (8 shorts/day from Nov 20 onwards)
        day_str = target_date.strftime("%Y-%m-%d")
        posts_on_day = sum(1 for p in mine if _day(p.get("due_at") or p.get("posted_at", 0)) == day_str)
        if posts_on_day >= max_daily:
            continue

        for hour, minute in slots:
            slot_dt = datetime.datetime(
                target_date.year, target_date.month, target_date.day,
                hour, minute, 0, 0, tzinfo=IST
            )
            slot_ts = slot_dt.timestamp()

            # Skip slots in the past or within 30 seconds
            if slot_ts <= now_ts + 30:
                continue

            # Skip slots already occupied by another post for this account
            if any(abs(t - slot_ts) < 300 for t in taken):
                continue

            return round(slot_ts, 2)

    return round(now_ts + 3600, 2)


def upcoming_slots(account_id: str = None, count: int = 16) -> list:
    """Return the next available schedule slots in IST for selection, with the first free slot marked as default."""
    now_ts = time.time()
    now_ist = datetime.datetime.now(IST)
    mine = _mine()
    if account_id:
        mine = [p for p in mine if p.get("account") == account_id]
    taken = [p["due_at"] for p in mine if p.get("due_at") and p.get("status") in OPEN]

    results = []
    is_ch2 = _is_channel_2(account_id)
    for day_offset in range(60):
        if len(results) >= count:
            break
        target_date = (now_ist + datetime.timedelta(days=day_offset)).date()
        slots = _slots_for_date(target_date, account_id)
        max_daily = _max_shorts_for_date(target_date, account_id)
        day_str = target_date.strftime("%Y-%m-%d")
        posts_on_day = sum(1 for p in mine if _day(p.get("due_at") or p.get("posted_at", 0)) == day_str)
        if posts_on_day >= max_daily:
            continue

        for hour, minute in slots:
            slot_dt = datetime.datetime(
                target_date.year, target_date.month, target_date.day,
                hour, minute, 0, 0, tzinfo=IST
            )
            slot_ts = slot_dt.timestamp()
            if slot_ts <= now_ts + 30:
                continue
            if any(abs(t - slot_ts) < 300 for t in taken):
                continue

            day_diff = (target_date - now_ist.date()).days
            if day_diff == 0:
                prefix = "Today"
            elif day_diff == 1:
                prefix = "Tomorrow"
            else:
                prefix = slot_dt.strftime("%a, %d %b")

            time_str = slot_dt.strftime("%I:%M %p")
            if is_ch2:
                is_phase1 = target_date < START_DATE_8_PER_DAY
                phase_label = "Ch2: 3/day" if is_phase1 else "Ch2: 8/day"
            else:
                is_phase1 = target_date < START_DATE_8_PER_DAY
                phase_label = "Ch1: 4/day" if is_phase1 else "Ch1: 8/day"

            results.append({
                "timestamp": round(slot_ts, 2),
                "iso": slot_dt.isoformat(),
                "time_str": time_str,
                "date_str": day_str,
                "label": f"{prefix} · {time_str} ({phase_label})",
                "short_label": f"{prefix} · {time_str}",
                "phase": 1 if is_phase1 else 2,
                "is_default": len(results) == 0,
            })
            if len(results) >= count:
                break

    return results


def realign_scheduled_queue(account_id: str = None, force_all: bool = False):
    """Assigns slot times to queued posts.
    If force_all is True, recalculates slots for all waiting scheduled posts at 8/day.
    Otherwise only assigns slot times to newly queued posts that don't have a due_at yet."""
    with _lock:
        if force_all:
            to_align = [
                p for p in _posts
                if p.get("status") == "scheduled"
            ]
        else:
            to_align = [
                p for p in _posts
                if p.get("status") == "scheduled"
                and p.get("due_at") is None
            ]
        if account_id:
            to_align = [p for p in to_align if p.get("account") == account_id]
        if not to_align:
            return
        to_align.sort(key=lambda p: (p.get("created") or 0))
        if force_all:
            for p in to_align:
                p["due_at"] = None
        for p in to_align:
            user_id = p.get("user") or (auth.local_user().id if hasattr(auth, "local_user") else None)
            if user_id:
                with ctx.as_user(user_id):
                    p["due_at"] = _next_slot(p["account"])
                    _mirror(p)
            else:
                p["due_at"] = _next_slot(p["account"])
                _mirror(p)
        _save()


def enqueue(job, clip, account_ids, now=False, due_at=None, upload=True) -> list:
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted",) for p in _mine()):
                continue  # never post the same clip to the same account twice
            if now:
                due = time.time()
                pinned = False
            elif due_at is not None:
                due = float(due_at)
                pinned = True
            else:
                due = _next_slot(aid)
                pinned = False
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "pinned": pinned, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    if not now and made and due_at is None:
        realign_scheduled_queue()
    if upload:
        for p in made:
            try:
                user_id = p.get("user")
                if user_id:
                    with ctx.as_user(user_id):
                        _post(p)
                else:
                    _post(p)
            except Exception:
                pass
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    desc = (clip.get("description") or "").strip()
    creator_name = (creator.get("name") or "").strip() if creator and creator.get("id") else ""
    if creator_name and not re.search(r'(?i)\bcredits?\s*:', desc):
        desc = brain.add_credits(desc, creator_name)
    desc = brain.ensure_hashtags(desc)
    tags = [h.lstrip("#") for h in clip.get("hashtags", [])]
    for req in brain.REQUIRED_TAGS:
        if not any(t.lower() == req.lower() for t in tags):
            tags.append(req)
    new_tags = [f"#{h}" for h in tags if f"#{h}".lower() not in desc.lower()]
    for req in brain.REQUIRED_HASHTAGS:
        if req.lower() not in desc.lower() and req.lower() not in [x.lower() for x in new_tags]:
            new_tags.append(req)
    if new_tags:
        lines = desc.split("\n")
        if lines and all(part.startswith("#") for part in lines[-1].split() if part):
            lines[-1] = f"{lines[-1]} {' '.join(new_tags)}".strip()
            desc = "\n".join(lines).strip()
        else:
            desc = f"{desc}\n\n{' '.join(new_tags)}".strip()
    footer = (creator.get("post_footer") or "").strip()
    return "\n\n".join(x for x in (desc, footer) if x)


def _post(p):
    # If the post was already pre-uploaded to YouTube with scheduled publishAt:
    if p.get("remote_id") and p.get("platform") == "youtube":
        p.update(status="posted", posted_at=time.time(), error=None)
        creators.log(p["creator"], f"Published scheduled short on {p['account_name']}: {p.get('title')}")
        _save()
        _mirror(p)
        return

    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        if acc["platform"] == "youtube":
            shorts_tag = "" if "#shorts" in cap.lower() else "\n\n#Shorts"
            due = p.get("due_at")
            publish_at = None
            # If scheduled for more than 2 minutes in future, upload natively with YouTube's publishAt
            if due and due > time.time() + 120:
                dt_utc = datetime.datetime.fromtimestamp(due, tz=datetime.timezone.utc)
                publish_at = dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            rid, url = accounts.youtube_upload(acc, path, title, (cap + shorts_tag).strip(), clip.get("hashtags", []), creator["privacy"], publish_at=publish_at)
        elif acc["platform"] == "instagram":
            publish_at = None
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            publish_at = None
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")

        if publish_at:
            p.update(status="scheduled", remote_id=rid, url=url, error=None)
            creators.log(p["creator"], f"Pre-scheduled on {acc['name']} (auto-publishes on YouTube): {title}")
        else:
            p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
            creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc:
            current_due = p.get("due_at") or 0
            if current_due <= time.time() + 600:
                p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
            else:
                p.update(status="scheduled")
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def scheduled_count() -> int:
    return sum(1 for p in _mine() if p.get("status") in OPEN)


def _run():
    while True:
        try:
            time.sleep(5)
            now_ts = time.time()
            due = [
                p for p in list(_posts)
                if p.get("status") == "scheduled"
                and p.get("due_at") is not None
                and p["due_at"] <= now_ts
                and p.get("user")
            ]
            for p in due:
                try:
                    with ctx.as_user(p["user"]):
                        if accounts.settings()["paused"]:
                            continue
                        with _lock:
                            if p["status"] != "scheduled":
                                continue
                            p["status"] = "posting"
                        _post(p)
                except Exception:  # noqa: BLE001
                    pass

            # Pre-upload any scheduled YouTube posts that haven't been uploaded yet
            pending_pre = [
                p for p in list(_posts)
                if p.get("status") == "scheduled"
                and p.get("platform") == "youtube"
                and not p.get("remote_id")
                and p.get("due_at") is not None
                and p["due_at"] > now_ts + 120
                and p.get("tries", 0) < 3
                and p.get("user")
            ]
            if pending_pre:
                p = pending_pre[0]
                try:
                    with ctx.as_user(p["user"]):
                        if not accounts.settings()["paused"]:
                            _post(p)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            time.sleep(5)


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            stats = accounts.youtube_stats(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in stats:
                    st = stats[p["remote_id"]]
                    p["views"] = st.get("views", 0)
                    p["likes"] = st.get("likes", 0)
                    p["comments"] = st.get("comments", 0)
                    _mirror(p)
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v
                _mirror(p)


def refresh_stats():
    with _lock:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p.get("status") == "posted" and p.get("remote_id")])
            except Exception:  # noqa: BLE001
                pass
        _save()


def _stats():
    while True:
        refresh_stats()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        if p.get("remote_id") and p.get("platform") == "youtube":
            acc = accounts.get(p["account"])
            if acc:
                try:
                    accounts.youtube_delete(acc, p["remote_id"])
                except Exception:
                    pass
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)


def reschedule(pid: str, target_time: float):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be rescheduled.")
        p["due_at"] = round(float(target_time), 2)
        p["pinned"] = True
        if p.get("remote_id") and p.get("platform") == "youtube":
            acc = accounts.get(p["account"])
            if acc:
                try:
                    dt_utc = datetime.datetime.fromtimestamp(p["due_at"], tz=datetime.timezone.utc)
                    accounts.youtube_update_schedule(acc, p["remote_id"], publish_at=dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
                except Exception:
                    pass
        _save()
    _mirror(p)


def swap_posts(pid1: str, pid2: str):
    with _lock:
        p1 = next((p for p in _mine() if p["id"] == pid1), None)
        p2 = next((p for p in _mine() if p["id"] == pid2), None)
        if not p1 or not p2 or p1["status"] != "scheduled" or p2["status"] != "scheduled":
            raise ValueError("Both posts must be waiting scheduled posts.")
        p1["due_at"], p2["due_at"] = p2["due_at"], p1["due_at"]
        p1["pinned"] = True
        p2["pinned"] = True
        for p in (p1, p2):
            if p.get("remote_id") and p.get("platform") == "youtube":
                acc = accounts.get(p["account"])
                if acc:
                    try:
                        dt_utc = datetime.datetime.fromtimestamp(p["due_at"], tz=datetime.timezone.utc)
                        accounts.youtube_update_schedule(acc, p["remote_id"], publish_at=dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
                    except Exception:
                        pass
        _save()
    _mirror(p1)
    _mirror(p2)
    _mirror(p1)
    _mirror(p2)


def schedule_calendar(days_ahead=21, account_id: str = None) -> dict:
    now_ist = datetime.datetime.now(IST)
    start_date = _schedule_start_date()
    all_accounts = [a for a in accounts.list_accounts() if a.get("platform") == "youtube"]
    all_accounts.sort(key=lambda x: x.get("added", 0))

    if not account_id and all_accounts:
        account_id = all_accounts[0]["id"]

    active_acc = next((a for a in all_accounts if a["id"] == account_id), None)
    if not active_acc and all_accounts:
        active_acc = all_accounts[0]
        account_id = active_acc["id"]

    is_ch2 = _is_channel_2(account_id)

    mine = _mine()
    if account_id:
        mine = [p for p in mine if p.get("account") == account_id]

    post_map = {}
    for p in mine:
        if p.get("due_at") and p.get("status") in OPEN + ("posted",):
            post_map[round(p["due_at"])] = {
                "id": p["id"],
                "title": p.get("title") or "Clip",
                "status": p.get("status"),
                "platform": p.get("platform"),
                "account": p.get("account"),
                "account_name": p.get("account_name"),
                "score": p.get("score"),
                "job": p.get("job"),
                "clip": p.get("clip"),
                "views": p.get("views"),
                "likes": p.get("likes"),
                "comments": p.get("comments"),
                "url": p.get("url"),
                "due_at": p.get("due_at"),
            }

    days = []
    for i in range(days_ahead):
        target_date = (now_ist + datetime.timedelta(days=i)).date()
        max_daily = _max_shorts_for_date(target_date, account_id)
        slots_times = _slots_for_date(target_date, account_id)
        day_slots = []
        for hour, minute in slots_times:
            dt = datetime.datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=IST)
            ts = dt.timestamp()
            matched = None
            for taken_ts, p_data in post_map.items():
                if abs(taken_ts - ts) < 600:
                    matched = p_data
                    break
            day_slots.append({
                "time": dt.strftime("%I:%M %p"),
                "time_str": dt.strftime("%I:%M %p"),
                "timestamp": round(ts, 2),
                "past": ts < time.time(),
                "post": matched,
            })

        # After all the timings of the video are over (all slots in the past), remove that day from the schedule
        if all(s["past"] for s in day_slots):
            continue

        booked_count = sum(1 for s in day_slots if s["post"])
        if is_ch2:
            is_phase1 = target_date < START_DATE_8_PER_DAY
            phase_desc = "Channel 2: 3/day (6:30 AM, 2:30 PM, 7:30 PM)" if is_phase1 else "Channel 2: Standard (8/day)"
        else:
            is_phase1 = target_date < START_DATE_8_PER_DAY
            phase_desc = "Channel 1: 4/day" if is_phase1 else "Channel 1: Standard (8/day)"

        days.append({
            "date": target_date.strftime("%Y-%m-%d"),
            "weekday": target_date.strftime("%A"),
            "day_name": target_date.strftime("%A"),
            "short_date": target_date.strftime("%d %b"),
            "is_first_3_weeks": is_phase1,
            "phase": phase_desc,
            "max_slots": max_daily,
            "slots_count": max_daily,
            "booked_slots": booked_count,
            "filled_count": booked_count,
            "free_slots": max(0, max_daily - booked_count),
            "slots": day_slots,
        })

    return {
        "start_date": start_date.strftime("%Y-%m-%d"),
        "account_id": account_id,
        "account_name": active_acc["name"] if active_acc else "",
        "is_channel_2": is_ch2,
        "accounts": [
            {
                "id": a["id"],
                "name": a["name"],
                "label": a.get("label", "YouTube"),
                "is_channel_2": _is_channel_2(a["id"]),
                "channel_num": 2 if _is_channel_2(a["id"]) else 1
            }
            for a in all_accounts
        ],
        "total_scheduled": len(post_map),
        "days": days,
        "paused": accounts.settings()["paused"],
    }


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:25]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]

    posted_posts = [p for p in _posts if p.get("status") == "posted"]
    total_views = sum(p.get("views") or 0 for p in posted_posts)
    total_likes = sum(p.get("likes") or 0 for p in posted_posts)
    total_comments = sum(p.get("comments") or 0 for p in posted_posts)
    top_clips = sorted(posted_posts, key=lambda p: (p.get("views") or 0, p.get("likes") or 0), reverse=True)[:6]
    engagement = round((total_likes / total_views * 100), 1) if total_views > 0 else 0.0

    yt_accounts = [a for a in accounts.list_accounts() if a.get("platform") == "youtube"]
    yt_accounts.sort(key=lambda x: x.get("added", 0))

    by_account = {}
    for idx, acc in enumerate(yt_accounts):
        aid = acc["id"]
        ch_num = 2 if _is_channel_2(aid) else (idx + 1)
        acc_posts = [p for p in posted_posts if p.get("account") == aid]
        v = sum(p.get("views") or 0 for p in acc_posts)
        l = sum(p.get("likes") or 0 for p in acc_posts)
        c = sum(p.get("comments") or 0 for p in acc_posts)
        eng = round((l / v * 100), 1) if v > 0 else 0.0
        acc_top = sorted(acc_posts, key=lambda p: (p.get("views") or 0, p.get("likes") or 0), reverse=True)[:6]
        by_account[aid] = {
            "account_id": aid,
            "account_name": acc["name"],
            "channel_num": ch_num,
            "views": v,
            "likes": l,
            "comments": c,
            "engagement": eng,
            "top": [{k: p.get(k) for k in ("id", "title", "views", "likes", "comments", "url", "posted_at", "platform", "account", "account_name")} for p in acc_top],
        }

    analytics = {
        "views": total_views,
        "likes": total_likes,
        "comments": total_comments,
        "engagement": engagement,
        "top": [{k: p.get(k) for k in ("id", "title", "views", "likes", "comments", "url", "posted_at", "platform", "account", "account_name")} for p in top_clips],
        "by_account": by_account,
        "accounts": [
            {
                "id": acc["id"],
                "name": acc["name"],
                "channel_num": 2 if _is_channel_2(acc["id"]) else (idx + 1),
            }
            for idx, acc in enumerate(yt_accounts)
        ],
    }

    # Upcoming next video per YouTube channel
    next_posts = []
    now_dt = datetime.datetime.now(IST)
    for idx, acc in enumerate(yt_accounts):
        ch_num = 2 if _is_channel_2(acc["id"]) else (idx + 1)
        ch_posts = [p for p in _posts if p.get("account") == acc["id"] and p.get("status") in OPEN and p.get("due_at")]
        ch_posts.sort(key=lambda p: p["due_at"])
        if ch_posts:
            nxt = ch_posts[0]
            dt = datetime.datetime.fromtimestamp(nxt["due_at"], tz=IST)
            diff_days = (dt.date() - now_dt.date()).days
            if diff_days == 0:
                day_prefix = "Today"
            elif diff_days == 1:
                day_prefix = "Tomorrow"
            else:
                day_prefix = dt.strftime("%a, %d %b")
            time_str = dt.strftime("%I:%M %p")
            next_posts.append({
                "account_id": acc["id"],
                "account_name": acc["name"],
                "channel_num": ch_num,
                "post_id": nxt["id"],
                "title": nxt.get("title") or "Clip",
                "due_at": nxt["due_at"],
                "time_str": time_str,
                "day_prefix": day_prefix,
                "when_label": f"{day_prefix} at {time_str} (IST)",
                "status": nxt.get("status"),
                "remote_id": nxt.get("remote_id", ""),
                "url": nxt.get("url", ""),
                "is_prescheduled": bool(nxt.get("remote_id")),
                "is_empty": False,
            })
        else:
            next_posts.append({
                "account_id": acc["id"],
                "account_name": acc["name"],
                "channel_num": ch_num,
                "post_id": None,
                "title": None,
                "when_label": "No upcoming videos scheduled",
                "is_empty": True,
            })

    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "analytics": analytics, "alerts": alerts.configured(), "recording": len(live.active_mine()),
            "next_posts": next_posts}


def digest_text() -> str:
    o = overview()
    lines = ["SnipForge daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    realign_scheduled_queue()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
