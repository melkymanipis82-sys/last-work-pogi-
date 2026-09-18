import os, re, json, time, threading, html
from urllib.parse import urlparse, parse_qs
import requests


from config import BOT_TOKEN, ALLOWED_CHAT_ID

DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "saved_uids.json")
LOCK = threading.Lock()
SCAN_INTERVAL = 300
# These are the messages shown by Facebook's desktop web page when a profile
# cannot be viewed.  Keep matching case-insensitive because Facebook may vary
# capitalization/whitespace between desktop responses.
DEAD_MARKERS = [
    "this content isn't available right now",
    "this content is not available right now",
    "this page isn't available",
    "this page is not available",
    "the link you followed may be broken",
    "the page you're trying to view isn't available",
    "this profile is currently unavailable",
]
GENERIC_ERRORS = ["sorry, something went wrong", "we're working on getting this fixed"]


def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {"recovery": d.get("recovery", []), "deletion": d.get("deletion", [])}
    except Exception:
        return {"recovery": [], "deletion": []}


def save_data(d):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def norm(s):
    s = html.unescape(s or "").replace("\u2019", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def normalize_uid(value):
    value = value.strip()
    if value.isdigit() and len(value) >= 5:
        return value
    try:
        u = urlparse(value)
        q = parse_qs(u.query)
        if q.get("id") and q["id"][0].isdigit() and len(q["id"][0]) >= 5:
            return q["id"][0]
        m = re.search(r"(?:/profile\.php/|/profile\.php\?id=|/)(\d{5,})(?:[/?#]|$)", value)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def desktop_check(uid):
    url = f"https://web.facebook.com/profile.php?id={uid}"
    out = {
        "url": url, "http": None, "title": "", "text": "", "status": "UNKNOWN",
        "name": "", "picture_url": "", "matched_marker": None, "error": None,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }

    try:
        # Desktop-web check: no browser/Playwright is required.
        response = requests.get(
            url,
            headers=headers,
            timeout=30,
            allow_redirects=True,
        )
        out["http"] = response.status
        page_html = response.text or ""

        # Extract the desktop page title and OpenGraph identity directly from HTML.
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            page_html,
            flags=re.I | re.S,
        )
        if title_match:
            out["title"] = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()

        def meta_content(prop):
            # Facebook changes attribute order frequently. Parse each meta tag
            # and inspect its attributes without relying on a fixed order.
            for tag in re.findall(r"<meta\b[^>]*>", page_html, flags=re.I | re.S):
                prop_m = re.search(r'\bproperty\s*=\s*["\']([^"\']+)["\']', tag, flags=re.I)
                name_m = re.search(r'\bname\s*=\s*["\']([^"\']+)["\']', tag, flags=re.I)
                content_m = re.search(r'\bcontent\s*=\s*["\'](.*?)["\']', tag, flags=re.I | re.S)
                key = prop_m.group(1) if prop_m else (name_m.group(1) if name_m else "")
                if key.lower() == prop.lower() and content_m:
                    return html.unescape(content_m.group(1)).strip()
            return ""

        og_title = meta_content("og:title")
        og_image = meta_content("og:image")

        if og_title:
            out["name"] = og_title
        if og_image:
            out["picture_url"] = og_image

        # Facebook may show the profile name in the HTML while a login
        # overlay is displayed. In that case og:title can be absent.
        name_candidates = []

        def add_name(value):
            value = html.unescape(value or "")
            value = re.sub(r"\s+", " ", value).strip(" \t\r\n-–—|")
            generic = {
                "facebook", "log in", "login", "log into facebook",
                "log in or sign up", "facebook - log in or sign up",
            }
            if value and len(value) <= 120 and value.lower() not in generic:
                name_candidates.append(value)

        identity_patterns = [
            r"See more from\s+([^<|\n]+)",
            r"\b(?:profile|page)\s+of\s+([^<|\n]+)",
            r'"(?:full_name|name)"\s*:\s*"([^"]{2,120})"',
            r'\b(?:aria-label|alt)\s*=\s*["\']([^"\']{2,120})["\']',
            r'<h1[^>]*>\s*([^<]{2,120})\s*</h1>',
        ]
        for pattern in identity_patterns:
            for match in re.finditer(pattern, page_html, flags=re.I | re.S):
                add_name(match.group(1))
                if len(name_candidates) >= 20:
                    break

        if not out["name"] and name_candidates:
            for candidate in name_candidates:
                if not any(x in candidate.lower() for x in (
                    "facebook", "log in", "login", "password", "email",
                    "create new account", "forgot",
                )):
                    out["name"] = candidate
                    break

        # Convert HTML to readable text for marker-based checks.
        text = re.sub(r"(?is)<(script|style|noscript|svg).*?>.*?</\1>", " ", page_html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        out["text"] = text[:12000]
        ntext = norm(text)

        generic_error = any(x in ntext for x in GENERIC_ERRORS)
        has_facebook_title = "facebook" in norm(out["title"])
        normalized_name = norm(out["name"])

        # A non-generic profile identity is a positive LIVE signal.
        generic_names = {
            "facebook",
            "log into facebook",
            "log in to facebook",
            "facebook - log in or sign up",
            "facebook login",
            "facebook - log in",
        }
        has_real_name = bool(normalized_name) and normalized_name not in generic_names

        # Explicit unavailable markers take priority. A login prompt alone
        # does not mean the UID is dead.
        explicit_dead = None
        for marker in DEAD_MARKERS:
            if norm(marker) in ntext:
                explicit_dead = marker
                break

        # The desktop Facebook unavailable page is a definitive DEAD signal.
        # It takes priority over any stale profile metadata that may still be
        # embedded in the HTML.
        if explicit_dead:
            out["matched_marker"] = explicit_dead
            out["status"] = "DEAD"
        elif has_real_name and not generic_error:
            out["status"] = "LIVE"
            out["matched_marker"] = None
        elif has_real_name:
            # Keep the positive identity signal even if Facebook also emits
            # a generic temporary/login error string.
            out["status"] = "LIVE"
            out["matched_marker"] = None
        elif has_facebook_title and len(ntext) > 150 and not generic_error:
            out["status"] = "LIVE"

    except Exception as e:
        out["error"] = str(e)

    return out


def check_uid(uid):
    return desktop_check(uid)


def update_item(item, result):
    item["status"] = result.get("status", "UNKNOWN")
    item["name"] = result.get("name", "")
    item["picture_url"] = result.get("picture_url", "")
    item["last_check"] = int(time.time())
    item["error"] = result.get("error")
    item["matched_marker"] = result.get("matched_marker")
    item["last_http"] = result.get("http")
    return item


def scan_group(group):
    with LOCK:
        items = list(load_data()[group])
    results = []
    for item in items:
        old_status = item.get("status", "UNKNOWN")
        result = check_uid(item["uid"])
        with LOCK:
            d = load_data()
            for live_item in d[group]:
                if live_item["uid"] == item["uid"]:
                    update_item(live_item, result)
                    copy = live_item.copy()
                    copy["previous_status"] = old_status
                    copy["status_changed"] = old_status != copy["status"]
                    results.append(copy)
                    break
            save_data(d)
    return results


def scan_all():
    return {"recovery": scan_group("recovery"), "deletion": scan_group("deletion")}


def api(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=params, timeout=40)
    return r.json()


def send(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    return api("sendMessage", **params)


def esc(value):
    return html.escape(str(value or ""), quote=False)


def status_label(status, group):
    if group == "recovery" and status == "LIVE":
        return "🟢 ACCOUNT ALIVE"
    if group == "deletion" and status == "DEAD":
        return "🔴 ACCOUNT DEAD / UNAVAILABLE"
    if status == "DEAD":
        return "🔴 DEAD / UNAVAILABLE"
    if status == "LIVE":
        return "🟢 LIVE"
    return "🟡 UNKNOWN"


def fmt_time(ts):
    if not ts:
        return "—"
    return time.strftime("%m/%d/%Y - %I:%M%p (PHT)", time.localtime(ts))


def status_text(item, group, changed=False):
    label = status_label(item.get("status", "UNKNOWN"), group)
    profile_url = f"https://web.facebook.com/profile.php?id={item['uid']}"
    lines = [f"<b>{label}</b>", f"📋 Issue: {esc(item.get('issue')) or '—'}", f'🔗 ID: <a href="{profile_url}"><code>{esc(item["uid"])}</code> - Link URL</a>']
    if item.get("name"):
        lines.append(f"👤 Profile: {esc(item['name'])}")
    lines += [
        f"💰 Amount: {esc(item.get('amount')) or '—'}",
        f"🧑‍💼 Owner: {esc(item.get('owner')) or '—'}",
        f"📝 Details: {esc(item.get('details')) or '—'}",
        f"📅 Checked At: {fmt_time(item.get('last_check'))}",
        f"🔎 Check: {esc(item.get('matched_marker')) or ('Profile looks available' if item.get('status') == 'LIVE' else 'No definitive marker')}",
    ]
    if changed:
        lines.insert(1, f"🔄 <b>Status changed: {esc(item.get('previous_status'))} → {esc(item.get('status'))}</b>")
    return "\n".join(lines)


def buttons_for(item, group):
    profile_url = f"https://web.facebook.com/profile.php?id={item['uid']}"
    return [[
        {"text": "📝 Update Info", "callback_data": f"edit|{group}|{item['uid']}"},
        {"text": "📊 List of UIDs", "callback_data": f"list|{group}"},
    ], [
        {"text": "❌ Delete UID", "callback_data": f"remove|{group}|{item['uid']}"},
        {"text": "🔗 Open Profile", "url": profile_url},
    ], [
        {"text": "👤 View Facebook Profile", "url": profile_url},
    ]]


def format_group(group, items):
    title = "🟥 RECOVERY MONITOR" if group == "recovery" else "🟩 DELETION MONITOR"
    if not items:
        return f"<b>{title}</b>\nNo UIDs monitored."
    return "<b>" + title + "</b>\n\n" + "\n\n".join(status_text(x, group) for x in items)


def default_item(uid):
    return {
        "uid": uid, "status": "UNKNOWN", "name": "", "picture_url": "",
        "last_check": None, "error": None, "matched_marker": None, "last_http": None,
        "issue": "", "amount": "", "owner": "", "details": "",
    }


def add_uid(group, raw):
    uid = normalize_uid(raw)
    if not uid:
        return False, "UID must be numeric (5+ digits) or a Facebook profile URL containing ?id=UID."
    with LOCK:
        d = load_data()
        if any(x["uid"] == uid for x in d[group]):
            return False, "UID already exists in this monitor."
        d[group].append(default_item(uid))
        save_data(d)
    return True, uid


def remove_uid(group, uid):
    uid = normalize_uid(uid) or uid.strip()
    with LOCK:
        d = load_data()
        before = len(d[group])
        d[group] = [x for x in d[group] if x["uid"] != uid]
        save_data(d)
    return before != len(d[group])


def find_item(group, uid):
    uid = normalize_uid(uid) or uid.strip()
    d = load_data()
    return next((x for x in d[group] if x["uid"] == uid), None)


def set_info(group, uid, raw):
    parts = [p.strip() for p in raw.split("|", 4)]
    while len(parts) < 5:
        parts.append("")
    issue, amount, owner, details, extra = parts
    # Fifth field is kept as optional notes/details continuation for easy mobile entry.
    if extra:
        details = (details + " | " + extra).strip(" |")
    with LOCK:
        d = load_data()
        item = next((x for x in d[group] if x["uid"] == uid), None)
        if not item:
            return False
        item["issue"] = issue
        item["amount"] = amount
        item["owner"] = owner
        item["details"] = details
        save_data(d)
    return True


def help_text():
    return """<b>FACEBOOK UID STATUS MONITOR</b>

<b>Monitoring</b>
/addrecovery UID
/adddeletion UID
/scan
/list
/remove recovery UID
/remove deletion UID

<b>UID Information</b>
/setinfo recovery UID | Issue | Amount | Owner | Details
/setinfo deletion UID | Issue | Amount | Owner | Details

Example:
<code>/setinfo recovery 100070780590181 | Codilla Suspended Acc | 2500 | Karl | Recovery case; check appeal status</code>

<b>Automatic checks</b>
The bot checks every 5 minutes but <b>does NOT send a message every 5 minutes</b>.
It sends a notification only when a UID's detected status changes (LIVE ↔ DEAD/UNKNOWN).

Detection uses the rendered Facebook page text as a signal; it is not a guaranteed enforcement-state determination."""


def authorized(msg):
    return str(msg.get("chat", {}).get("id")) == str(ALLOWED_CHAT_ID)


def handle(msg):
    if not authorized(msg):
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        send(chat_id, help_text())
        return
    if cmd in ("/addrecovery", "/adddeletion"):
        group = "recovery" if cmd == "/addrecovery" else "deletion"
        if not arg:
            send(chat_id, f"Usage: {cmd} 123456789")
            return
        ok, val = add_uid(group, arg)
        if not ok:
            send(chat_id, "❌ " + val)
            return
        send(chat_id, f"Added {group} UID <code>{val}</code>. First check…")
        results = scan_group(group)
        item = next((x for x in results if x["uid"] == val), None)
        if item:
            send(chat_id, status_text(item, group), buttons_for(item, group))
        return
    if cmd == "/setinfo":
        # Format: /setinfo recovery UID | issue | amount | owner | details
        first = arg.split(maxsplit=2)
        if len(first) < 3 or first[0] not in ("recovery", "deletion"):
            send(chat_id, "Usage: /setinfo recovery UID | Issue | Amount | Owner | Details")
            return
        group, uid, info = first
        uid = normalize_uid(uid) or uid
        if not set_info(group, uid, info):
            send(chat_id, "❌ UID not found in that monitor.")
            return
        item = find_item(group, uid)
        send(chat_id, "✅ UID information updated.\n\n" + status_text(item, group), buttons_for(item, group))
        return
    if cmd == "/scan":
        send(chat_id, "🔎 Manual scan started…")
        allr = scan_all()
        changed = []
        for group in ("recovery", "deletion"):
            for x in allr[group]:
                if x.get("status_changed"):
                    changed.append((group, x))
        if not changed:
            send(chat_id, "✅ Scan complete. No status changes detected, so no UID notifications were generated.")
        else:
            for group, x in changed:
                send(chat_id, status_text(x, group, changed=True), buttons_for(x, group))
        return
    if cmd == "/list":
        d = load_data()
        send(chat_id, format_group("recovery", d["recovery"]) + "\n\n" + format_group("deletion", d["deletion"]))
        return
    if cmd == "/remove":
        p = arg.split(maxsplit=1)
        if len(p) != 2 or p[0] not in ("recovery", "deletion"):
            send(chat_id, "Usage: /remove recovery 123456789")
            return
        uid = normalize_uid(p[1]) or p[1].strip()
        if remove_uid(p[0], uid):
            send(chat_id, f"Removed <code>{esc(uid)}</code> from {p[0]}.")
        else:
            send(chat_id, "UID not found.")
        return
    send(chat_id, "Unknown command. Use /help.")


def answer_callback(callback):
    if str(callback.get("message", {}).get("chat", {}).get("id")) != str(ALLOWED_CHAT_ID):
        return
    api("answerCallbackQuery", callback_query_id=callback["id"])
    data = callback.get("data", "").split("|")
    chat_id = callback["message"]["chat"]["id"]
    if not data:
        return
    if data[0] == "list" and len(data) > 1:
        group = data[1]
        items = load_data()[group]
        send(chat_id, format_group(group, items))
    elif data[0] == "remove" and len(data) > 2:
        group, uid = data[1], data[2]
        if remove_uid(group, uid):
            send(chat_id, f"❌ Removed <code>{esc(uid)}</code> from {group}.")
        else:
            send(chat_id, "UID not found.")
    elif data[0] == "edit" and len(data) > 2:
        group, uid = data[1], data[2]
        send(chat_id, f"📝 Update this UID with:\n<code>/setinfo {group} {uid} | Issue | Amount | Owner | Details</code>")


def poll():
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset is not None:
                params["offset"] = offset
            data = api("getUpdates", **params)
            if data.get("ok"):
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        handle(upd["message"])
                    elif "callback_query" in upd:
                        answer_callback(upd["callback_query"])
        except Exception as e:
            print("[POLL ERROR]", e)
            time.sleep(5)


def scheduler():
    while True:
        time.sleep(SCAN_INTERVAL)
        try:
            results = scan_all()
            # IMPORTANT: scheduled scans are silent unless a status changed.
            for group in ("recovery", "deletion"):
                for x in results[group]:
                    if x.get("status_changed"):
                        send(ALLOWED_CHAT_ID, status_text(x, group, changed=True), buttons_for(x, group))
        except Exception as e:
            print("[SCHEDULER ERROR]", e)



if __name__ == "__main__":
    if not BOT_TOKEN or not ALLOWED_CHAT_ID:
        raise SystemExit("Set BOT_TOKEN and ALLOWED_CHAT_ID in config.py")
    threading.Thread(target=scheduler, daemon=True).start()
    print("Telegram UID Monitor running. Scheduled notifications are change-only.")
    poll()
