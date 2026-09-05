#!/usr/bin/env python3
"""
Health Bot — 血糖血壓追蹤 + 服药时间提醒
Zeabur deployment with long polling.
"""

import json
import os
import time
import csv
import io
import math
import threading
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from flask import Flask, request

# Matplotlib is used for trend charts. Use the non-interactive Agg backend so it
# works headless on Zeabur (no display server).
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── Shared type metadata (single source of truth for export / import / charts) ─
TYPE_LABELS = {
    "sugar_0": "空腹血糖", "sugar_1": "午後血糖", "sugar_2": "晚後血糖",
    "uric_0": "空腹尿酸", "uric_1": "午後尿酸", "uric_2": "晚後尿酸",
    "uric_acid": "尿酸",
    "bp": "血壓", "weight": "體重",
}
TYPE_UNITS = {
    "sugar_0": "mmol/L", "sugar_1": "mmol/L", "sugar_2": "mmol/L",
    "uric_0": "μmol/L", "uric_1": "μmol/L", "uric_2": "μmol/L",
    "uric_acid": "μmol/L",
    "bp": "mmHg", "weight": "kg",
}
# Reverse map: Chinese label -> internal type code (used by CSV import).
LABEL_TO_TYPE = {v: k for k, v in TYPE_LABELS.items()}

# ── Config ──────────────────────────────────────────────────────────
# BOT_TOKEN must come from the environment. Never hardcode it — this is a
# public repo and a leaked token lets anyone hijack the Telegram bot.
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")
# Owner chat id must come from the environment — never hardcode it in a public
# repo. Accept OWNER_ID (current) or legacy OWNER_CHAT_ID (already set on Zeabur).
OWNER_ID = os.environ.get("OWNER_ID") or os.environ.get("OWNER_CHAT_ID") or ""
if not OWNER_ID:
    raise RuntimeError("OWNER_ID (or OWNER_CHAT_ID) environment variable is required")
OWNER_ID = str(OWNER_ID)
# Persistent storage. /tmp is WIPED on every Zeabur redeploy/restart, so for
# real durability point DATA_DIR at a mounted persistent volume (e.g. /data),
# configured in the Zeabur dashboard. Fallback keeps local/dev working.
DATA_DIR    = os.environ.get("DATA_DIR", "/tmp/hermes_data")
os.makedirs(DATA_DIR, exist_ok=True)

# ── Timezone ────────────────────────────────────────────────────────
TZ = timezone(timedelta(hours=8))   # Hong Kong Time (HKT/UTC+8)

def hk_now():
    return datetime.now(TZ)

# ── Flask ──────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return "Health Bot running"

@app.route("/health")
def health():
    return "OK"

# ── Telegram helpers ────────────────────────────────────────────────

def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[ERROR] send_message: {e}")

def answer_callback(callback_id, text=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id, "text": text}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def edit_message(chat_id, message_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[ERROR] edit_message: {e}")

def download_file(file_id):
    """Download a Telegram file (by file_id) and return its raw bytes."""
    get_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
    q = get_url + "?" + urllib.parse.urlencode({"file_id": file_id})
    with urllib.request.urlopen(q, timeout=20) as r:
        meta = json.loads(r.read().decode())
    file_path = meta.get("result", {}).get("file_path")
    if not file_path:
        raise RuntimeError("getFile 冇回傳 file_path")
    dl = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    with urllib.request.urlopen(dl, timeout=30) as r:
        return r.read()

def restore_from_csv_bytes(raw_bytes, chat_id):
    """Rebuild health_data.json from an exported CSV (日期,時間,類型,數值,單位).
    Accepts both the Chinese labels and the raw type codes (old buggy export)."""
    label_to_type = dict(LABEL_TO_TYPE)
    label_to_type["尿酸"] = "uric_acid"  # legacy generic uric label
    known = set(TYPE_LABELS.keys())

    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw_bytes.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        send_message(chat_id, "❌ 無法解讀檔案編碼")
        return

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        send_message(chat_id, "❌ CSV 係空嘅")
        return

    header = [h.strip() for h in rows[0]]
    try:
        i_date = header.index("日期")
        i_time = header.index("時間")
        i_type = header.index("類型")
        i_val  = header.index("數值")
    except ValueError:
        send_message(chat_id, "❌ CSV 欄位不符（要有 日期/時間/類型/數值）")
        return

    rebuilt = {}
    count = 0
    for row in rows[1:]:
        if len(row) <= max(i_date, i_time, i_type, i_val):
            continue
        date = row[i_date].strip()
        rtime = row[i_time].strip()
        label = row[i_type].strip()
        value = row[i_val].strip()
        if not date or not value:
            continue
        rtype = label_to_type.get(label, label)
        if rtype not in known:
            continue
        rebuilt.setdefault(date, {"records": []})
        # upsert: replace same type for that day (keeps last occurrence)
        rebuilt[date]["records"] = [
            r for r in rebuilt[date]["records"] if r["type"] != rtype
        ]
        rebuilt[date]["records"].append({"time": rtime, "type": rtype, "value": value})
        count += 1

    if count == 0:
        send_message(chat_id, "❌ CSV 入面搵唔到有效記錄")
        return

    # Back up the current data file BEFORE the destructive full-replace, so an
    # old CSV can't silently wipe newer records — user can restore the .bak.
    bak_path = backup_data_file("preimport.bak")

    with data_lock:
        save_data(rebuilt)
    days = len(rebuilt)
    bak_note = f"\n💾 匯入前已自動備份：{os.path.basename(bak_path)}" if bak_path else ""
    send_message(chat_id,
        f"✅ 匯入完成\n\n"
        f"📊 還原 {count} 條記錄，覆蓋 {days} 日\n"
        f"📁 儲存位置：{DATA_DIR}{bak_note}")

def handle_document(message, chat_id):
    doc = message.get("document") or {}
    fname = (doc.get("file_name") or "").lower()
    file_id = doc.get("file_id")
    if not file_id:
        return
    if not fname.endswith(".csv"):
        send_message(chat_id, "📎 請傳 CSV 檔（用主菜單「📤 匯出 CSV」產生嘅格式）")
        return
    send_message(chat_id, "📥 收到 CSV，正在匯入…")
    try:
        raw = download_file(file_id)
        restore_from_csv_bytes(raw, chat_id)
    except Exception as e:
        print(f"[ERROR] restore: {e}")
        send_message(chat_id, f"❌ 匯入失敗：{e}")

def send_document(chat_id, file_path, caption="", filename=None, content_type="text/csv"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    with open(file_path, "rb") as f:
        file_data = f.read()
    fname = filename or os.path.basename(file_path)
    boundary = "----HealthBotBoundary7MA4YWxkTrZu0gW"
    body = f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
    if caption:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{fname}\"\r\nContent-Type: {content_type}\r\n\r\n"
    body = body.encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=60)
    except Exception as e:
        print(f"[ERROR] send_document: {e}")

def send_photo(chat_id, file_path, caption=""):
    """Send a chart/photo (png) via multipart form upload."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(file_path, "rb") as f:
        photo_data = f.read()
    boundary = "----HealthBotPhotoBoundary7MA4YWxk"
    body = f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
    if caption:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"trend.png\"\r\nContent-Type: image/png\r\n\r\n"
    body = body.encode() + photo_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=45)
    except Exception as e:
        print(f"[ERROR] send_photo: {e}")
        # Fallback: tell the user and still send the text stats
        send_message(chat_id, "⚠️ 圖表傳送失敗，請稍後再試")

# ── Inline keyboards ─────────────────────────────────────────────────

def main_menu():
    return {
        "inline_keyboard": [
            [{"text": "📋 服藥時間表", "callback_data": "schedule"}],
            [{"text": "📊 記錄血糖/血壓", "callback_data": "record"}],
            [{"text": "📖 今日記錄", "callback_data": "today"},
             {"text": "📈 趨勢報告", "callback_data": "trend"}],
            [{"text": "💊 服藥打卡", "callback_data": "meds_today"}],
            [{"text": "📤 匯出 CSV", "callback_data": "export_csv"},
             {"text": "📥 匯入還原", "callback_data": "import_help"}],
        ]
    }

def back_btn():
    return {"inline_keyboard": [[{"text": "🔙 返回主菜單", "callback_data": "back"}]]}

def schedule_menu():
    return {"inline_keyboard": [
        [{"text": "🔙 返回主菜單", "callback_data": "back"}],
    ]}

def record_menu():
    return {"inline_keyboard": [
        [{"text": "🩸 空腹血糖", "callback_data": "sugar_0"}, {"text": "🩸 午後血糖", "callback_data": "sugar_1"}, {"text": "🩸 晚後血糖", "callback_data": "sugar_2"}],
        [{"text": "❤️ 血壓", "callback_data": "bp"}, {"text": "🟤 尿酸", "callback_data": "uric_acid"}],
        [{"text": "⚖️ 體重", "callback_data": "weight"}],
        [{"text": "🔙 返回主菜單", "callback_data": "back"}],
    ]}

# ── Pending state (multi-step entry) ─────────────────────────────────

pending = {}   # chat_id -> {"type": ..., ...}

# ── Data storage ─────────────────────────────────────────────────────

# One lock guards the whole load→modify→save cycle. The poll thread and the
# reminder thread both touch storage; without this, a record and a CSV import
# racing each other could interleave writes.
data_lock = threading.Lock()

def get_data_path():
    return os.path.join(DATA_DIR, "health_data.json")

def load_data():
    path = get_data_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # A corrupt/half-written file must never crash the bot or nuke history.
        # Park the bad file for inspection and start fresh.
        print(f"[ERROR] data file unreadable ({e}); backing up and starting empty")
        try:
            os.replace(path, f"{path}.corrupt.{int(time.time())}")
        except OSError:
            pass
        return {}

def save_data(data):
    # Atomic write: dump to a temp file in the SAME directory, then os.replace()
    # it over the real file. os.replace is atomic on POSIX, so a crash or a
    # second concurrent writer can never leave a truncated half-JSON behind
    # (which would make load_data() blow up and lose every past record).
    path = get_data_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def backup_data_file(tag="bak"):
    """Copy the current health_data.json aside before a destructive op (CSV import)."""
    path = get_data_path()
    if os.path.exists(path):
        bak = f"{path}.{tag}"
        try:
            with open(path) as src, open(bak, "w") as dst:
                dst.write(src.read())
            return bak
        except OSError as e:
            print(f"[WARN] backup failed: {e}")
    return None

def record_entry(chat_id, entry_type, value):
    """Record a health entry for today (upsert — replaces same type if exists today)."""
    now = hk_now()
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")

    record = {"time": current_time, "type": entry_type, "value": value}
    with data_lock:
        data = load_data()
        if today not in data:
            data[today] = {"records": []}
        # Upsert: remove existing record of same type today, then append new one
        data[today]["records"] = [
            r for r in data[today]["records"] if r["type"] != entry_type
        ]
        data[today]["records"].append(record)
        save_data(data)
    return record

# ── Safety alerts ───────────────────────────────────────────────────
# Record-then-warn: the reading is still saved, but dangerously extreme values
# trigger an immediate prominent alert. Thresholds are conservative clinical
# red flags (hypertensive crisis, hypo/hyperglycaemia) — they prompt seeking
# care, they do NOT diagnose.
def safety_alerts(entry_type, value):
    """Return a list of warning strings for a freshly-recorded reading (may be empty)."""
    alerts = []
    try:
        if entry_type.startswith("sugar"):
            s = float(value)
            if math.isfinite(s):
                if s < 3.9:
                    alerts.append("⚠️ <b>低血糖警報</b>：血糖低過 3.9 mmol/L。\n"
                                  "請即刻食糖/飲甜嘢，15 分鐘後再度；若暈眩、出汗、心慌，盡快求醫。")
                elif s > 16.7:
                    alerts.append("⚠️ <b>高血糖警報</b>：血糖高過 16.7 mmol/L。\n"
                                  "請飲水、注意酮症癥狀（嘔吐、腹痛、呼吸有果味），持續偏高要盡快求醫。")
        elif entry_type.startswith("uric"):
            u = float(value)
            if math.isfinite(u) and u > 540:
                alerts.append("⚠️ 尿酸持續高過 540 μmol/L 屬明顯超標，建議盡快同醫生跟進。")
        elif entry_type == "bp":
            if "/" in str(value):
                sy, di = str(value).split("/")
                sy, di = int(sy), int(di)
                if sy >= 180 or di >= 120:
                    alerts.append("🚨 <b>血壓急症警報</b>：血壓達到或超過 180/120。\n"
                                  "若伴隨劇烈頭痛、胸痛、視力模糊、言語不清、手腳無力，<b>即刻召救護車</b>；"
                                  "否則靜坐休息 5 分鐘後再度一次，仍偏高要立即求醫。")
    except (ValueError, TypeError):
        pass
    return alerts

def get_today_summary():
    """Build today's summary string."""
    now = hk_now()
    today = now.strftime("%Y-%m-%d")
    data = load_data()

    if today not in data or not data[today].get("records"):
        return "今日尚無記錄"

    records = data[today]["records"]
    lines = ["📖 今日記錄 — " + now.strftime("%Y年%m月%d日"), ""]

    # sugar_0/1/2 + uric_0/1/2 come in pairs
    sugars = [None, None, None]   # 0=空腹, 1=午後, 2=晚後
    urics  = [None, None, None]
    sys_bp = None
    dia_bp = None
    weight = None

    for r in records:
        t = r["type"]
        v = r["value"]
        if t == "sugar_0": sugars[0] = v
        elif t == "sugar_1": sugars[1] = v
        elif t == "sugar_2": sugars[2] = v
        elif t == "uric_0":  urics[0]  = v
        elif t == "uric_1":  urics[1]  = v
        elif t == "uric_2":  urics[2]  = v
        elif t == "weight": weight = v
        elif t == "bp":
            if "/" in str(v):
                parts = str(v).split("/")
                sys_bp = parts[0]
                dia_bp = parts[1]

    labels = ["空腹血糖", "午後血糖", "晚後血糖"]

    def uric_tier(v):
        try:
            u = float(v)
        except (TypeError, ValueError):
            return "⚪"
        if not math.isfinite(u):
            return "⚪"
        if u < 420:   return "🟢"
        if u < 540:   return "🟡"
        return "🔴"

    for i, v in enumerate(sugars):
        s_label = labels[i]
        u_val = urics[i]
        if v is not None and u_val is not None:
            tier = uric_tier(u_val)
            lines.append(f"🩸 {s_label}：{v}    {tier}尿酸：{u_val}")
        elif v is not None:
            lines.append(f"🩸 {s_label}：{v}")
        else:
            lines.append(f"🩸 {s_label}：—")

    if sys_bp is not None and dia_bp is not None:
        lines.append(f"❤️ 血壓：{sys_bp}/{dia_bp}")
    elif sys_bp is not None:
        lines.append(f"❤️ 收縮壓：{sys_bp}")
    elif dia_bp is not None:
        lines.append(f"💓 舒張壓：{dia_bp}")

    if weight is not None:
        lines.append(f"⚖️ 體重：{weight} kg")

    lines.append("")
    return "\n".join(lines)

# ── Schedule display ────────────────────────────────────────────────

def get_schedule_text():
    return """📋 <b>今日服藥時間表</b>

<b>09:30</b> — 起床
  💧 飲幾小口溫水
  → 🍶 食四君子丸
  ✅ 空腹（起身即刻食，唔好飯後食）
  ⚠️ 和降血壓藥隔夠30分鐘

<b>10:00</b> — 食降血壓藥
  💊 Amlodipine 5mg 食<b>半粒</b>（=2.5mg）
  ✅ 每日一次，可空腹

<b>12:00</b> — 食四君子丸
  🍶 空腹，午餐前30分鐘
  ⏰ 食完等半個鐘先食午飯

<b>12:30</b> — 午餐時（跟飯食）
  💊 Metformin 500mg（一粒）
  💊 Gliclazide 80mg（一粒）
  ⚠️ 兩隻都要餐時食，唔好空腹
  ⚠️ Gliclazide 有低血糖風險，要準時食飯

<b>14:00</b> — 小口飲溫水
  💧 隨意，慢慢飲

<b>16:00</b> — 小口飲溫水
  💧 隨意，17點後停止大量飲水

<b>18:30–19:00</b> — 晚餐
  🍽 清淡少食、唔好食飽
  ⚠️ 少油、唔好炸嘢

<b>19:00 晚餐時</b> —
  💊 Metformin 500mg（一粒）
  💊 Gliclazide 80mg（一粒）
  ⚠️ 跟飯食，食完唔好即刻躺平，坐直休息15分鐘
  ⚠️ Gliclazide 留意低血糖（手抖、出汗、心慌即刻食糖）

━━━━━━━━━━━━━━━
⚠️ <b>全日禁忌</b>
❌ 夜晚絕對唔食四君子丸
❌ 避免燥熱、失眠、夜尿多

<i>劑量根據聖母醫院 2026-06-02 配藥紀錄</i>"""

# ── Update handler ───────────────────────────────────────────────────

def handle_callback(callback, chat_id, message_id):
    data = callback.get("data", "")
    answer_callback(callback.get("id"))

    if data == "back":
        edit_message(chat_id, message_id, "🏠 主菜單", main_menu())
        return

    if data == "schedule":
        edit_message(chat_id, message_id, get_schedule_text(), schedule_menu())
        return

    if data == "record":
        edit_message(chat_id, message_id,
            "📊 選擇記錄項目：", record_menu())
        return

    if data == "today":
        edit_message(chat_id, message_id, get_today_summary(), back_btn())
        return

    if data == "export_csv":
        export_and_send(chat_id, message_id)
        return

    if data == "import_help":
        edit_message(chat_id, message_id,
            "📥 <b>匯入還原</b>\n\n"
            "將之前用「📤 匯出 CSV」備份嘅 <b>health_export.csv</b> 檔案，\n"
            "直接 send 俾呢個 bot，就會自動匯入還原。\n\n"
            "⚠️ 匯入會以 CSV 內容取代現有記錄（同日同類型以最新為準）。\n\n"
            "適用：重新部署後數據被清空時還原歷史。",
            back_btn())
        return

    # Sugar entry — initiate pending, ask for uric acid after sugar
    if data in ("sugar_0", "sugar_1", "sugar_2"):
        pending[chat_id] = {"type": "sugar", "sugar_idx": int(data.split("_")[1])}
        edit_message(chat_id, message_id,
            "🩸 請回覆血糖值（如：5.2）\n\n時間：" + hk_now().strftime("%H:%M"),
            back_btn())
        return

    # Blood pressure — single combined input
    if data == "bp":
        pending[chat_id] = {"type": "bp"}
        edit_message(chat_id, message_id,
            "❤️ 請回覆血壓（如：128/79）\n\n格式：上壓/下壓\n時間：" + hk_now().strftime("%H:%M"),
            back_btn())
        return

    if data == "uric_acid":
        # Standalone uric acid: ask WHICH slot it belongs to (空腹/午後/晚後),
        # otherwise it would always overwrite the fasting slot (uric_0) and the
        # other two slots could only ever be filled via the paired sugar flow.
        edit_message(chat_id, message_id,
            "🟤 邊個時段嘅尿酸？",
            {"inline_keyboard": [
                [{"text": "🌅 空腹尿酸", "callback_data": "uricpick_0"},
                 {"text": "🌞 午後尿酸", "callback_data": "uricpick_1"},
                 {"text": "🌙 晚後尿酸", "callback_data": "uricpick_2"}],
                [{"text": "🔙 返回主菜單", "callback_data": "back"}],
            ]})
        return

    if data in ("uricpick_0", "uricpick_1", "uricpick_2"):
        idx = int(data.split("_")[1])
        pending[chat_id] = {"type": "uric_acid", "uric_idx": idx}
        slot = ["空腹", "午後", "晚後"][idx]
        edit_message(chat_id, message_id,
            f"🟤 請回覆{slot}尿酸值（如：360）\n\n時間：" + hk_now().strftime("%H:%M"),
            back_btn())
        return

    # Weight entry
    if data == "weight":
        pending[chat_id] = {"type": "weight"}
        edit_message(chat_id, message_id,
            "⚖️ 請回覆體重（kg，如：65.5）\n\n時間：" + hk_now().strftime("%H:%M"),
            back_btn())
        return

    # Trend report (7 / 30 days) with chart
    if data == "trend":
        edit_message(chat_id, message_id, "📈 請選擇報告時段：",
            {"inline_keyboard": [
                [{"text": "📅 最近 7 日", "callback_data": "trend_7"},
                 {"text": "🗓️ 最近 30 日", "callback_data": "trend_30"}],
                [{"text": "📄 就醫報告 PDF（90 日）", "callback_data": "clinic_90"}],
                [{"text": "🔙 返回主菜單", "callback_data": "back"}],
            ]})
        return
    if data in ("trend_7", "trend_30"):
        days = 7 if data == "trend_7" else 30
        edit_message(chat_id, message_id, f"📈 正在生成最近 {days} 日報告…", back_btn())
        send_trend_report(chat_id, days)
        return
    if data.startswith("clinic_"):
        try:
            cdays = int(data.split("_", 1)[1])
        except (ValueError, IndexError):
            cdays = 90
        edit_message(chat_id, message_id, f"📄 正在產生最近 {cdays} 日就醫報告 PDF…", back_btn())
        send_clinic_report(chat_id, cdays)
        return

    # Medication check-in
    if data == "meds_today":
        edit_message(chat_id, message_id, get_meds_text(), meds_keyboard())
        return
    if data.startswith("medtake_"):
        key = data.split("_", 1)[1]
        mark_med_taken(key)
        edit_message(chat_id, message_id, get_meds_text(), meds_keyboard())
        answer_callback(callback.get("id"), "✅ 已記錄")
        return
    if data == "meds_undo":
        clear_meds_today()
        edit_message(chat_id, message_id, get_meds_text(), meds_keyboard())
        return

def handle_text(text, chat_id):
    if chat_id in pending:
        p = pending.pop(chat_id)
        ptype = p["type"]
        value = text.strip()

        def parse_finite(s):
            """float() that also rejects nan/inf and non-numeric junk. Returns None on bad input."""
            try:
                x = float(s)
            except (ValueError, TypeError):
                return None
            return x if math.isfinite(x) else None

        # Validate single-number entries. Blood pressure uses "上壓/下壓"
        # (e.g. 128/79) which isn't a single float — it has its own parser below.
        if ptype != "bp":
            num = parse_finite(value)
            if num is None:
                send_message(chat_id, "❌ 數值格式錯誤，請重新輸入（如：5.2）")
                return
            # Physiologically-plausible ranges guard against fat-finger typos
            # (e.g. typing 52 instead of 5.2) that would poison health trends.
            if ptype == "sugar" and not (2.0 <= num <= 30.0):
                send_message(chat_id,
                    "❌ 血糖數值超出合理範圍（2–30 mmol/L），請重新輸入\n"
                    "（如真實數值確係咁，請聯絡醫生）")
                return
            if ptype == "uric_acid" and not (50.0 <= num <= 1500.0):
                send_message(chat_id,
                    "❌ 尿酸數值超出合理範圍（50–1500 μmol/L），請重新輸入")
                return
            if ptype == "weight" and not (20.0 <= num <= 400.0):
                send_message(chat_id,
                    "❌ 體重數值超出合理範圍（20–400 kg），請重新輸入")
                return

        if ptype == "sugar":
            sugar_types  = ["sugar_0", "sugar_1", "sugar_2"]
            entry_type   = sugar_types[p["sugar_idx"]]
            record_entry(chat_id, entry_type, value)
            labels = ["空腹血糖", "午後血糖", "晚後血糖"]
            send_message(chat_id,
                f"✅ 血糖已記錄\n\n"
                f"🩸 {labels[p['sugar_idx']]}：{value}\n"
                f"時間：{hk_now().strftime('%H:%M')}")
            for a in safety_alerts(entry_type, value):
                send_message(chat_id, a)
            # Now ask for paired uric acid
            pending[chat_id] = {"type": "uric_acid", "uric_idx": p["sugar_idx"]}
            send_message(chat_id,
                "🟤 請回覆尿酸值（如：360）\n\n"
                f"時間：{hk_now().strftime('%H:%M')}")
            return

        elif ptype == "weight":
            record_entry(chat_id, "weight", value)
            send_message(chat_id,
                f"✅ 體重已記錄\n\n"
                f"⚖️ 體重：{value} kg\n"
                f"時間：{hk_now().strftime('%H:%M')}")
            return

        elif ptype == "uric_acid":
            uric_idx = p.get("uric_idx", 0)
            uric_types = ["uric_0", "uric_1", "uric_2"]
            record_entry(chat_id, uric_types[uric_idx], value)
            u = float(value)
            slot_labels = ["空腹", "午後", "晚後"]
            slot = slot_labels[uric_idx]
            if u < 420:
                uric_label = f"🟢 {slot}尿酸：{value} μmol/L（正常）"
            elif u < 540:
                uric_label = f"🟡 {slot}尿酸：{value} μmol/L（偏高）"
            else:
                uric_label = f"🔴 {slot}尿酸：{value} μmol/L（好高）"
            send_message(chat_id,
                f"✅ 尿酸已記錄\n\n"
                f"{uric_label}\n"
                f"時間：{hk_now().strftime('%H:%M')}")
            for a in safety_alerts(uric_types[uric_idx], value):
                send_message(chat_id, a)
            return

        elif ptype == "bp":
            # Blood pressure — device gives both readings like "125/80"
            if "/" not in value:
                send_message(chat_id, "❌ 請輸入格式如：128/79（上壓/下壓）")
                return
            parts = value.split("/")
            if len(parts) != 2:
                send_message(chat_id, "❌ 請輸入格式如：128/79（上壓/下壓）")
                return
            try:
                sys_val = int(parts[0].strip())
                dia_val = int(parts[1].strip())
            except ValueError:
                send_message(chat_id, "❌ 請輸入數字（如：128/79）")
                return
            # Range check (each reading independently)
            if not (60 <= sys_val <= 250 and 40 <= dia_val <= 150):
                send_message(chat_id, "❌ 數值超出正常範圍（上壓60-250，下壓40-150），請重新輸入")
                return
            # Systolic must exceed diastolic — if not, the two were almost
            # certainly typed in reverse (e.g. 80/120).
            if sys_val <= dia_val:
                send_message(chat_id,
                    "❌ 上壓（收縮壓）必須大過下壓（舒張壓）。\n"
                    "你可能打反咗，請按 上壓/下壓 重新輸入（如：128/79）")
                return
            # Store as single "bp" entry
            record_entry(chat_id, "bp", f"{sys_val}/{dia_val}")
            send_message(chat_id,
                f"✅ 血壓已記錄\n\n"
                f"❤️ 血壓：{sys_val}/{dia_val} mmHg\n"
                f"時間：{hk_now().strftime('%H:%M')}")
            for a in safety_alerts("bp", f"{sys_val}/{dia_val}"):
                send_message(chat_id, a)
            return

    # Default: show main menu
    send_message(chat_id, "🏠 主菜單", main_menu())

def export_and_send(chat_id, message_id):
    data = load_data()
    if not data:
        edit_message(chat_id, message_id, "📤 沒有數據可匯出", back_btn())
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["日期", "時間", "類型", "數值", "單位"])

    for date, d in sorted(data.items(), reverse=True):
        for r in d.get("records", []):
            t = r["type"]
            writer.writerow([date, r["time"], TYPE_LABELS.get(t, t), r["value"],
                             TYPE_UNITS.get(t, "")])

    csv_path = os.path.join(DATA_DIR, "health_export.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(output.getvalue())

    edit_message(chat_id, message_id, "📤 正在生成 CSV...", back_btn())
    send_document(chat_id, csv_path, "📊 健康數據匯出")

# ── Medication check-in ─────────────────────────────────────────────
# Tracked separately from health readings (own file) so it doesn't pollute the
# CSV/chart data. key matches the REMINDERS keys so we can show ⬜/✅ per med.
MEDS = [
    ("sijunzi_am",    "09:30", "🍶 四君子丸（起床空腹）"),
    ("amlodipine",    "10:00", "💊 Amlodipine 2.5mg（半粒，每日一次）"),
    ("sijunzi_noon",  "12:00", "🍶 四君子丸（午餐前）"),
    ("metformin_1",   "12:30", "💊 Metformin 500mg（午餐時）"),
    ("gliclazide_1",  "12:30", "💊 Gliclazide 80mg（午餐時）"),
    ("metformin_2",   "19:00", "💊 Metformin 500mg（晚餐時）"),
    ("gliclazide_2",  "19:00", "💊 Gliclazide 80mg（晚餐時）"),
]

def get_meds_path():
    return os.path.join(DATA_DIR, "meds_log.json")

def load_meds():
    path = get_meds_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def save_meds(data):
    path = get_meds_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def mark_med_taken(key):
    today = hk_now().strftime("%Y-%m-%d")
    ts = hk_now().strftime("%H:%M")
    with data_lock:
        data = load_meds()
        data.setdefault(today, {})[key] = ts
        save_meds(data)

def clear_meds_today():
    today = hk_now().strftime("%Y-%m-%d")
    with data_lock:
        data = load_meds()
        data.pop(today, None)
        save_meds(data)

def get_meds_text():
    today = hk_now().strftime("%Y-%m-%d")
    taken = load_meds().get(today, {})
    lines = ["💊 <b>今日服藥打卡</b> — " + hk_now().strftime("%m月%d日"), ""]
    done = 0
    for key, t, label in MEDS:
        if key in taken:
            lines.append(f"✅ {label}　<i>{t}（已食 {taken[key]}）</i>")
            done += 1
        else:
            lines.append(f"⬜ {label}　<i>{t}</i>")
    lines.append("")
    lines.append(f"進度：{done}/{len(MEDS)}")
    if done == len(MEDS):
        lines.append("🎉 今日全部藥物已打卡，做得好！")
    return "\n".join(lines)

def meds_keyboard():
    today = hk_now().strftime("%Y-%m-%d")
    taken = load_meds().get(today, {})
    rows = []
    for key, t, label in MEDS:
        mark = "✅" if key in taken else "⬜"
        # strip emoji from button label (keep it short)
        short = label.split(" ", 1)[-1]
        rows.append([{"text": f"{mark} {t} {short}", "callback_data": f"medtake_{key}"}])
    rows.append([{"text": "♻️ 重置今日打卡", "callback_data": "meds_undo"},
                 {"text": "🔙 返回主菜單", "callback_data": "back"}])
    return {"inline_keyboard": rows}

# ── Trend report + chart ────────────────────────────────────────────

def collect_series(days):
    """Return (dates, {metric: [values]}) for the last `days` days (None = no reading)."""
    data = load_data()
    today = hk_now().date()
    start = today - timedelta(days=days - 1)
    dates, sugar, uric, sysv, diav, wt = [], [], [], [], [], []
    for i in range(days):
        d = start + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        dates.append(d)
        su, ur = [], []
        sy = di = w = None
        for r in data.get(ds, {}).get("records", []):
            t, v = r["type"], r["value"]
            try:
                if t.startswith("sugar_"):
                    su.append(float(v))
                elif t.startswith("uric_"):
                    ur.append(float(v))
                elif t == "weight":
                    w = float(v)
                elif t == "bp" and "/" in str(v):
                    p = str(v).split("/")
                    sy, di = int(p[0]), int(p[1])
            except (ValueError, TypeError):
                pass
        sugar.append(sum(su) / len(su) if su else None)
        uric.append(sum(ur) / len(ur) if ur else None)
        sysv.append(sy); diav.append(di); wt.append(w)
    return dates, {"sugar": sugar, "uric": uric, "sys": sysv, "dia": diav, "weight": wt}

def _stats(vals):
    """(count, avg, min, max) over finite, non-None values."""
    xs = [v for v in vals if v is not None and math.isfinite(v)]
    if not xs:
        return None
    return len(xs), sum(xs) / len(xs), min(xs), max(xs)

def build_trend_chart(dates, series, path):
    """Draw a 4-panel trend chart. English labels to avoid CJK-font tofu on the server."""
    x = list(range(len(dates)))
    xticks = x
    xlabels = [d.strftime("%m/%d") for d in dates]
    # thin out labels if many days
    step = max(1, len(dates) // 8)

    fig, axes = plt.subplots(4, 1, figsize=(9, 12), sharex=True)
    fig.suptitle(f"Health Trend — last {len(dates)} days", fontsize=14, fontweight="bold")

    def plot(ax, pts, color, ylabel, marker="o"):
        px = [i for i, v in zip(x, pts) if v is not None]
        py = [v for v in pts if v is not None]
        if px:
            ax.plot(px, py, marker=marker, color=color, linewidth=1.5, markersize=4)
        ax.set_ylabel(ylabel, fontsize=10)
        ax.grid(True, alpha=0.3)
        ax.set_xticks(x[::step])
        ax.set_xticklabels(xlabels[::step], rotation=45, ha="right", fontsize=8)

    plot(axes[0], series["sugar"], "#e74c3c", "Glucose\n(mmol/L)")
    axes[0].axhspan(3.9, 7.0, color="#2ecc71", alpha=0.12)  # rough normal band
    plot(axes[1], series["sys"], "#c0392b", "BP (mmHg)")
    plot(axes[1], series["dia"], "#3498db", "BP (mmHg)")
    axes[1].legend(["Systolic", "Diastolic"], fontsize=8, loc="upper right")
    plot(axes[2], series["uric"], "#8e44ad", "Uric acid\n(umol/L)")
    axes[2].axhline(420, color="#f39c12", linestyle="--", linewidth=0.8, alpha=0.7)
    plot(axes[3], series["weight"], "#16a085", "Weight\n(kg)", marker="s")

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(path, dpi=110)
    plt.close(fig)

def send_trend_report(chat_id, days):
    dates, series = collect_series(days)
    has_any = any(any(v is not None for v in series[k]) for k in series)
    if not has_any:
        send_message(chat_id, f"📈 最近 {days} 日暫時冇健康記錄，先去「📊 記錄」低啲數據啦。")
        return

    # Text stats
    def fmt(st, unit, nd=1):
        if st is None:
            return "暫無數據"
        n, avg, mn, mx = st
        return f"平均 {avg:.{nd}f}　最低 {mn:.{nd}f}　最高 {mx:.{nd}f} {unit}（{n} 日）"

    # Abnormal counts
    high_bp = sum(1 for s, d_ in zip(series["sys"], series["dia"]) if s is not None and (s >= 140 or (d_ or 0) >= 90))
    low_sugar = sum(1 for v in series["sugar"] if v is not None and v < 3.9)
    high_sugar = sum(1 for v in series["sugar"] if v is not None and v > 16.7)
    high_uric = sum(1 for v in series["uric"] if v is not None and v > 540)

    caption = (
        f"📈 <b>最近 {days} 日健康報告</b>\n\n"
        f"🩸 <b>血糖</b>\n{fmt(_stats(series['sugar']), 'mmol/L')}\n"
        f"   低血糖 {low_sugar} 日、極高血糖 {high_sugar} 日\n\n"
        f"❤️ <b>血壓</b>\n上壓 {fmt(_stats(series['sys']), 'mmHg', 0)}\n"
        f"下壓 {fmt(_stats(series['dia']), 'mmHg', 0)}\n   偏高（≥140/90）{high_bp} 日\n\n"
        f"🟤 <b>尿酸</b>\n{fmt(_stats(series['uric']), 'μmol/L', 0)}\n   明確超標（>540）{high_uric} 日\n\n"
        f"⚖️ <b>體重</b>\n{fmt(_stats(series['weight']), 'kg')}\n\n"
        f"<i>圖表入面綠色帶係血糖大致正常區間，紫線虛線係尿酸 420 參考線。</i>"
    )

    chart_path = os.path.join(DATA_DIR, "trend.png")
    try:
        build_trend_chart(dates, series, chart_path)
        send_photo(chat_id, chart_path, caption)
    except Exception as e:
        print(f"[ERROR] trend chart: {e}")
        send_message(chat_id, caption)

# ── Clinic / medical-visit report (PDF) ─────────────────────────────
# One-tap export for doctor visits: a full-period trend chart + summary
# stats + a chronological list of abnormal events. Text is English so it
# renders on the server (no CJK-font issues) and is readable by clinicians.
def abnormal_events(days):
    """Collect dated abnormal readings (BP crisis, hypo/hyperglycaemia, high uric)."""
    data = load_data()
    today = hk_now().date()
    start = today - timedelta(days=days - 1)
    events = []
    for i in range(days):
        d = start + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        for r in data.get(ds, {}).get("records", []):
            t, v = r["type"], r["value"]
            label = TYPE_LABELS.get(t, t)
            try:
                if t.startswith("sugar_"):
                    s = float(v)
                    if s < 3.9:
                        events.append((ds, r.get("time", ""), label, f"{s} mmol/L LOW glucose (hypo)"))
                    elif s > 16.7:
                        events.append((ds, r.get("time", ""), label, f"{s} mmol/L HIGH glucose"))
                elif t.startswith("uric_") or t == "uric_acid":
                    u = float(v)
                    if u > 540:
                        events.append((ds, r.get("time", ""), label, f"{u} umol/L very high uric acid"))
                elif t == "bp" and "/" in str(v):
                    sy, di = [int(x) for x in str(v).split("/")[:2]]
                    if sy >= 180 or di >= 120:
                        events.append((ds, r.get("time", ""), label, f"{sy}/{di} mmHg BP CRISIS"))
                    elif sy >= 140 or di >= 90:
                        events.append((ds, r.get("time", ""), label, f"{sy}/{di} mmHg elevated BP"))
            except (ValueError, TypeError):
                pass
    return events

def build_clinic_pdf(dates, series, events, path, days):
    """Render a printable clinic report PDF (English to avoid CJK tofu)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    chart_path = os.path.join(DATA_DIR, "clinic_trend.png")
    build_trend_chart(dates, series, chart_path)

    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    y = h - 20 * mm

    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, y, "Health Report — HK Bank League player")
    y -= 7 * mm
    c.setFont("Helvetica", 10)
    rng = f"{dates[0].strftime('%Y-%m-%d')} to {dates[-1].strftime('%Y-%m-%d')} ({days} days)"
    c.drawString(20 * mm, y, f"Generated: {hk_now().strftime('%Y-%m-%d %H:%M')} HKT   |   Period: {rng}")
    y -= 8 * mm

    # Summary stats
    def stat_line(label, st, unit):
        nonlocal y
        if st is None:
            return
        n, avg, mn, mx = st
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20 * mm, y, label)
        c.setFont("Helvetica", 10)
        c.drawString(70 * mm, y, f"avg {avg:.1f}   min {mn:.1f}   max {mx:.1f} {unit}  ({n} days)")
        y -= 6 * mm

    stat_line("Glucose (fasting/etc avg)", _stats(series["sugar"]), "mmol/L")
    stat_line("BP systolic", _stats(series["sys"]), "mmHg")
    stat_line("BP diastolic", _stats(series["dia"]), "mmHg")
    stat_line("Uric acid", _stats(series["uric"]), "umol/L")
    stat_line("Weight", _stats(series["weight"]), "kg")
    y -= 4 * mm

    # Chart (fit width)
    try:
        from reportlab.lib.utils import ImageReader
        img = ImageReader(chart_path)
        iw, ih = img.getSize()
        draw_w = w - 40 * mm
        draw_h = draw_w * ih / iw
        max_h = 130 * mm
        if draw_h > max_h:
            draw_h = max_h
            draw_w = draw_h * iw / ih
        c.drawImage(img, 20 * mm, y - draw_h, width=draw_w, height=draw_h)
        y -= draw_h + 6 * mm
    except Exception as e:
        print(f"[ERROR] pdf chart embed: {e}")

    # Abnormal events list (new page if needed)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, y, f"Abnormal events ({len(events)})")
    y -= 6 * mm
    c.setFont("Helvetica", 8.5)
    if not events:
        c.drawString(20 * mm, y, "No threshold-crossing events recorded in this period.")
    else:
        for ds, tm, label, msg in events:
            if y < 20 * mm:
                c.showPage()
                y = h - 20 * mm
                c.setFont("Helvetica", 8.5)
            c.setFillColor(colors.HexColor("#b91c1c") if "CRISIS" in msg or "LOW" in msg else colors.HexColor("#92400e"))
            c.drawString(20 * mm, y, f"{ds} {tm}  [{label}]  {msg}")
            c.setFillColor(colors.black)
            y -= 4.5 * mm

    c.showPage()
    c.save()

def send_clinic_report(chat_id, days=90):
    dates, series = collect_series(days)
    has_any = any(any(v is not None for v in series[k]) for k in series)
    if not has_any:
        send_message(chat_id, f"📄 最近 {days} 日冇健康記錄，未能產生報告。")
        return
    try:
        events = abnormal_events(days)
        pdf_path = os.path.join(DATA_DIR, "clinic_report.pdf")
        build_clinic_pdf(dates, series, events, pdf_path, days)
        send_message(chat_id,
            f"📄 <b>就醫報告（最近 {days} 日）</b>\n"
            f"包含整段走勢圖、平均/最高/最低、同 {len(events)} 項異常事件，可直接打印帶去見醫護。")
        send_document(chat_id, pdf_path,
            caption=f"Health report — last {days} days",
            filename="health_report.pdf",
            content_type="application/pdf")
    except Exception as e:
        print(f"[ERROR] clinic report: {e}")
        send_message(chat_id, "❌ 報告產生失敗，請稍後再試。")

# ── Sustained-trend alerts (not just one-off spikes) ────────────────
# Runs in the reminder loop; at most once per day per metric. Flags a
# *pattern* (e.g. most of the last week elevated) rather than a single reading.
_last_trend_alert_date = ""

def check_trend_alerts():
    global _last_trend_alert_date
    now = hk_now()
    today = now.strftime("%Y-%m-%d")
    if now.hour < 18:          # run once in the evening
        return
    if _last_trend_alert_date == today:
        return
    try:
        dates, series = collect_series(7)
        notes = []
        # BP: elevated (>=140/90) on at least 5 of the days with readings
        bp_days = [(s, d_) for s, d_ in zip(series["sys"], series["dia"]) if s is not None]
        if len(bp_days) >= 5:
            high = sum(1 for s, d_ in bp_days if s >= 140 or (d_ or 0) >= 90)
            if high >= 5:
                notes.append("❤️ <b>血壓連續偏高</b>：過去 7 日有 5 日或以上上壓 ≥140 或下壓 ≥90。\n建議記低同醫生講，並留意定時服降血壓藥。")
        # Fasting glucose: 5+ days above 7
        fast_high = sum(1 for v in series["sugar"] if v is not None and v > 7.0)
        if fast_high >= 5:
            notes.append("🩸 <b>血糖持續偏高</b>：過去 7 日有 5 日或以上平均血糖高於 7.0 mmol/L。\n建議檢查餐時降糖藥有冇跟飯食，並同醫生跟進。")
        # Uric: 2+ days above 540 in the week
        uric_high = sum(1 for v in series["uric"] if v is not None and v > 540)
        if uric_high >= 2:
            notes.append("🟤 <b>尿酸反覆超標</b>：過去 7 日有 2 日或以上高於 540 μmol/L。\n建議戒口（少酒、少內臟/海鮮）並同醫生跟進。")
        if notes:
            _last_trend_alert_date = today
            head = "📈 <b>健康趨勢提示</b>（唔係單次超標，而係呢排持續）\n\n"
            send_message(OWNER_ID, head + "\n\n".join(notes))
    except Exception as e:
        print(f"[ERROR] trend alerts: {e}")

# ── Weekly auto-backup ──────────────────────────────────────────────

_last_backup_week = ""

def check_weekly_backup():
    """Every Monday morning, auto-send a fresh CSV export to the owner (safety net)."""
    global _last_backup_week
    now = hk_now()
    if now.weekday() != 0:   # 0 = Monday
        return
    if now.hour < 9:
        return
    week_tag = now.strftime("%Y-W%W")
    if _last_backup_week == week_tag:
        return
    data = load_data()
    if not data:
        _last_backup_week = week_tag
        return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["日期", "時間", "類型", "數值", "單位"])
    for date, d in sorted(data.items(), reverse=True):
        for r in d.get("records", []):
            t = r["type"]
            writer.writerow([date, r["time"], TYPE_LABELS.get(t, t), r["value"],
                             TYPE_UNITS.get(t, "")])
    csv_path = os.path.join(DATA_DIR, "health_export.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(output.getvalue())
    send_document(OWNER_ID, csv_path, f"📦 每週自動備份（{now.strftime('%Y-%m-%d')}）")
    _last_backup_week = week_tag

# ── Telegram polling ───────────────────────────────────────────────

offset = None

def poll_updates():
    global offset
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            full_url = f"{url}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(full_url)
            response = urllib.request.urlopen(req, timeout=35)
            updates = json.loads(response.read().decode())

            for update in updates.get("result", []):
                offset = update["update_id"] + 1

                msg = update.get("message", {})
                cb  = update.get("callback_query", {})

                if msg:
                    cid = str(msg.get("chat", {}).get("id", ""))
                    txt = msg.get("text", "")
                    if cid != OWNER_ID:
                        continue
                    if msg.get("document"):
                        handle_document(msg, cid)
                    elif txt:
                        handle_text(txt, cid)

                if cb:
                    cid  = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    mid  = cb.get("message", {}).get("message_id")
                    if cid == OWNER_ID:
                        handle_callback(cb, cid, mid)

        except Exception as e:
            print(f"[WARN] poll error: {e}")
            time.sleep(3)

# ── Reminder scheduler ─────────────────────────────────────────────

REMINDERS = {
    "09:30": {
        "msg": "⏰ 09:30 — 起床時間！\n\n💧 飲幾小口溫水\n→ 🍶 食四君子丸（空腹）\n⚠️ 和降血壓藥隔30分鐘",
        "key": "sijunzi_am",
    },
    "10:00": {
        "msg": "⏰ 10:00 — 食降血壓藥\n\n💊 Amlodipine 5mg 食<b>半粒</b>（=2.5mg），每日一次（可空腹）",
        "key": "amlodipine",
    },
    "12:00": {
        "msg": "⏰ 12:00 — 食四君子丸\n\n🍶 空腹，午餐前30分鐘\n⏰ 食完等半個鐘先食午飯",
        "key": "sijunzi_noon",
    },
    "12:30": {
        "msg": "⏰ 12:30 — 午餐時（跟飯食）\n\n💊 Metformin 500mg（一粒）\n💊 Gliclazide 80mg（一粒）\n⚠️ 兩隻都要<b>餐時</b>食，唔好空腹；Gliclazide 有低血糖風險，記得準時食飯",
        "key": "metformin_1",
    },
    "14:00": {
        "msg": "💧 14:00 — 小口飲溫水\n\n慢慢飲，唔好灌大口",
        "key": "water_afternoon",
    },
    "16:00": {
        "msg": "💧 16:00 — 小口飲溫水\n\n⚠️ 17點後停止大量飲水",
        "key": "water_late",
    },
    "19:00": {
        "msg": "⏰ 19:00 — 晚餐時（跟飯食）\n\n💊 Metformin 500mg（一粒）\n💊 Gliclazide 80mg（一粒）\n⚠️ 餐時食，食完唔好即刻躺平；Gliclazide 留意低血糖",
        "key": "metformin_2",
    },
}

last_remind = set()
_last_cleared_date = ""

def check_reminders():
    global last_remind, _last_cleared_date
    now = hk_now()
    today = now.strftime("%Y-%m-%d")
    # Current minute as an int (HH*60+MM) so we can compare with <= instead of
    # exact string equality.
    cur_min = now.hour * 60 + now.minute

    # Clear old reminders at midnight to prevent unbounded growth
    if _last_cleared_date != today:
        last_remind.clear()
        _last_cleared_date = today

    for t, info in REMINDERS.items():
        hh, mm = t.split(":")
        remind_min = int(hh) * 60 + int(mm)
        key = f"{today}_{t}_{info['key']}"
        # Fire once the clock reaches (or drifts past) the reminder minute.
        # Exact-match (t == current_time) could skip a whole reminder if the
        # 30s poll drifted past that minute — dangerous for medication alerts.
        if cur_min >= remind_min and key not in last_remind:
            last_remind.add(key)
            send_message(OWNER_ID, info["msg"])

def reminder_loop():
    last_check = 0
    while True:
        now = time.time()
        if now - last_check >= 60:
            check_reminders()
            try:
                check_weekly_backup()
            except Exception as e:
                print(f"[WARN] weekly backup: {e}")
            try:
                check_trend_alerts()
            except Exception as e:
                print(f"[WARN] trend alerts: {e}")
            last_check = now
        time.sleep(30)

# ── Start ──────────────────────────────────────────────────────────

if BOT_TOKEN:
    t1 = threading.Thread(target=poll_updates, daemon=True)
    t1.start()
    t2 = threading.Thread(target=reminder_loop, daemon=True)
    t2.start()
    print("[Health Bot] Started")
