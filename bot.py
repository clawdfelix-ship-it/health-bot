#!/usr/bin/env python3
"""
Health Bot — 血糖血壓追蹤 + 服药时间提醒
Zeabur deployment with long polling.
"""

import json
import os
import time
import threading
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from flask import Flask, request

# ── Config ──────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "8602095206:AAGpozHncwHvKAwV1MEeH_vc7j1gdhzgCcE")
OWNER_ID    = "582328026"
DATA_DIR    = "/tmp/hermes_data"
os.makedirs(DATA_DIR, exist_ok=True)

# ── Timezone ────────────────────────────────────────────────────────
TZ = timezone(timedelta(hours=8))   # Hong Kong Time (HKT/UTC+8)

def now():
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

def send_document(chat_id, file_path, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    with open(file_path, "rb") as f:
        photo_data = f.read()
    boundary = "----HealthBotBoundary7MA4YWxkTrZu0gW"
    body = f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
    if caption:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"health_export.csv\"\r\nContent-Type: text/csv\r\n\r\n"
    body = body.encode() + photo_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        print(f"[ERROR] send_document: {e}")

# ── Inline keyboards ─────────────────────────────────────────────────

def main_menu():
    return {
        "inline_keyboard": [
            [{"text": "📋 服藥時間表", "callback_data": "schedule"}],
            [{"text": "📊 記錄血糖/血壓", "callback_data": "record"}],
            [{"text": "📖 今日記錄", "callback_data": "today"}],
            [{"text": "📤 匯出 CSV", "callback_data": "export_csv"}],
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
        [{"text": "❤️ 收縮壓", "callback_data": "bp_sys"}, {"text": "💓 舒張壓", "callback_data": "bp_dia"}],
        [{"text": "🟤 尿酸", "callback_data": "uric_acid"}],
        [{"text": "🔙 返回主菜單", "callback_data": "back"}],
    ]}

# ── Pending state (multi-step entry) ─────────────────────────────────

pending = {}   # chat_id -> {"type": "sugar"|"bp", "idx": int, "time": str}

def get_pending_text(pending_type, idx):
    labels = ["空腹血糖", "午後血糖", "晚後血糖"]
    if pending_type == "sugar":
        return f"🩸 請回覆血糖值（如：5.2）\n\n時間：{now().strftime('%H:%M')}"
    elif pending_type == "bp_sys":
        return f"❤️ 請回覆收縮壓（如：125）\n\n時間：{now().strftime('%H:%M')}"
    elif pending_type == "bp_dia":
        return f"💓 請回覆舒張壓（如：80）\n\n時間：{now().strftime('%H:%M')}"

# ── Data storage ─────────────────────────────────────────────────────

def get_data_path():
    return os.path.join(DATA_DIR, "health_data.json")

def load_data():
    path = get_data_path()
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_data(data):
    path = get_data_path()
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def record_entry(chat_id, entry_type, value):
    """Record a health entry for today."""
    now = now()
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")

    data = load_data()
    if today not in data:
        data[today] = {"records": []}

    record = {
        "time": current_time,
        "type": entry_type,
        "value": value,
    }
    data[today]["records"].append(record)
    save_data(data)
    return record

def get_today_summary():
    """Build today's summary string."""
    now = now()
    today = now.strftime("%Y-%m-%d")
    data = load_data()

    if today not in data or not data[today].get("records"):
        return "今日尚無記錄"

    records = data[today]["records"]
    lines = ["📖 今日記錄 — " + now.strftime("%Y年%m月%d日"), ""]

    sugars = [None, None, None]   # 0=空腹, 1=午後, 2=晚後
    sys_bp = None
    dia_bp = None
    uric = None

    for r in records:
        t = r["type"]
        v = r["value"]
        if t == "sugar_0": sugars[0] = v
        elif t == "sugar_1": sugars[1] = v
        elif t == "sugar_2": sugars[2] = v
        elif t == "bp_sys": sys_bp = v
        elif t == "bp_dia": dia_bp = v
        elif t == "uric_acid": uric = v

    labels = ["空腹血糖", "午後血糖", "晚後血糖"]
    for i, v in enumerate(sugars):
        if v is not None:
            lines.append(f"🩸 {labels[i]}：{v}")
        else:
            lines.append(f"🩸 {labels[i]}：—")

    if sys_bp is not None and dia_bp is not None:
        lines.append(f"❤️ 血壓：{sys_bp}/{dia_bp}")
    elif sys_bp is not None:
        lines.append(f"❤️ 收縮壓：{sys_bp}")
    elif dia_bp is not None:
        lines.append(f"💓 舒張壓：{dia_bp}")

    if uric is not None:
        lines.append(f"🟤 尿酸：{uric}")

    lines.append("")
    lines.append("🔙 返回主菜單")
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
  💊 Amlodipine 2.5mg
  ✅ 可空腹

<b>12:00</b> — 食四君子丸
  🍶 空腹，午餐前30分鐘
  ⏰ 食完等半個鐘先食午飯

<b>12:30</b> — 午餐後
  💊 Metformin 500mg（第一粒）
  ⚠️ 一定要飯後，絕對唔可以空腹食

<b>14:00</b> — 小口飲溫水
  💧 隨意，慢慢飲

<b>16:00</b> — 小口飲溫水
  💧 隨意，17點後停止大量飲水

<b>18:30–19:00</b> — 晚餐
  🍽 清淡少食、唔好食飽
  ⚠️ 少油、唔好炸嘢

<b>晚餐後即時</b> — Metformin 500mg（第二粒）
  💊 一定要飯後
  ⚠️ 食完唔好即刻躺平，坐直休息15分鐘

━━━━━━━━━━━━━━━
⚠️ <b>全日禁忌</b>
❌ 夜晚絕對唔食四君子丸
❌ 避免燥熱、失眠、夜尿多"""

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

    # Sugar entry — initiate pending, ask for uric acid after sugar
    if data in ("sugar_0", "sugar_1", "sugar_2"):
        pending[chat_id] = {"type": "sugar", "sugar_idx": int(data.split("_")[1])}
        edit_message(chat_id, message_id,
            "🩸 請回覆血糖值（如：5.2）\n\n時間：" + now().strftime("%H:%M"),
            back_btn())
        return

    # BP entry — ask for both readings together
    if data in ("bp_sys", "bp_dia"):
        pending[chat_id] = {"type": "bp", "bp_type": data}
        edit_message(chat_id, message_id,
            "❤️ 請回覆血壓（如：125/80）\n\n時間：" + now().strftime("%H:%M"),
            back_btn())
        return

    if data == "uric_acid":
        pending[chat_id] = {"type": "uric_acid"}
        edit_message(chat_id, message_id,
            "🟤 請回覆尿酸值（如：360）\n\n時間：" + now().strftime("%H:%M"),
            back_btn())
        return

def handle_text(text, chat_id):
    if chat_id in pending:
        p = pending.pop(chat_id)
        ptype = p["type"]
        value = text.strip()

        # Validate
        try:
            float(value)
        except ValueError:
            send_message(chat_id, "❌ 數值格式錯誤，請重新輸入（如：5.2）")
            return

        if ptype == "sugar":
            # Record sugar
            sugar_types = ["sugar_0", "sugar_1", "sugar_2"]
            entry_type = sugar_types[p["sugar_idx"]]
            record_entry(chat_id, entry_type, value)
            now = now().strftime("%H:%M")
            labels = ["空腹血糖", "午後血糖", "晚後血糖"]
            send_message(chat_id,
                f"✅ 血糖已記錄\n\n"
                f"🩸 {labels[p['sugar_idx']]}：{value}\n"
                f"時間：{now}")
            # Now ask for uric acid
            pending[chat_id] = {"type": "uric_acid_pending"}
            send_message(chat_id,
                "🟤 請回覆尿酸值（如：360）\n\n"
                f"時間：{now}")
            return

        elif ptype == "uric_acid_pending":
            record_entry(chat_id, "uric_acid", value)
            send_message(chat_id,
                f"✅ 尿酸已記錄\n\n"
                f"🟤 尿酸：{value} μmol/L\n"
                f"時間：{now().strftime('%H:%M')}")
            return

        elif ptype == "bp":
            # Blood pressure — device gives both readings like "125/80"
            if "/" in value:
                parts = value.split("/")
                if len(parts) == 2:
                    sys_val = parts[0].strip()
                    dia_val = parts[1].strip()
                    try:
                        float(sys_val); float(dia_val)
                        record_entry(chat_id, "bp_sys", sys_val)
                        record_entry(chat_id, "bp_dia", dia_val)
                        now = now().strftime("%H:%M")
                        send_message(chat_id,
                            f"✅ 血壓已記錄\n\n"
                            f"❤️ 收縮壓：{sys_val} mmHg\n"
                            f"💓 舒張壓：{dia_val} mmHg\n"
                            f"時間：{now}")
                        return
                    except ValueError:
                        pass
            send_message(chat_id, "❌ 請輸入格式如：125/80（收縮壓/舒張壓）")
            return

        elif ptype == "uric_acid":
            record_entry(chat_id, "uric_acid", value)
            send_message(chat_id,
                f"✅ 尿酸已記錄\n\n"
                f"🟤 尿酸：{value} μmol/L\n"
                f"時間：{now().strftime('%H:%M')}")
            return

    # Default: show main menu
    send_message(chat_id, "🏠 主菜單", main_menu())

def export_and_send(chat_id, message_id):
    data = load_data()
    if not data:
        edit_message(chat_id, message_id, "📤 沒有數據可匯出", back_btn())
        return

    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["日期", "時間", "類型", "數值", "單位"])

    for date, d in sorted(data.items(), reverse=True):
        for r in d.get("records", []):
            t = r["type"]
            labels = {"sugar_0": "空腹血糖", "sugar_1": "午後血糖", "sugar_2": "晚後血糖",
                      "bp_sys": "收縮壓", "bp_dia": "舒張壓", "uric_acid": "尿酸"}
            units = {"sugar_0": "mmol/L", "sugar_1": "mmol/L", "sugar_2": "mmol/L",
                     "bp_sys": "mmHg", "bp_dia": "mmHg", "uric_acid": "μmol/L"}
            writer.writerow([date, r["time"], labels.get(t, t), r["value"], units.get(t, "")])

    csv_path = os.path.join(DATA_DIR, "health_export.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(output.getvalue())

    edit_message(chat_id, message_id, "📤 正在生成 CSV...", back_btn())
    send_document(chat_id, csv_path, "📊 健康數據匯出")

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
                    if cid == OWNER_ID and txt:
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

last_remind = set()

REMINDERS = {
    "09:30": {
        "msg": "⏰ 09:30 — 起床時間！\n\n💧 飲幾小口溫水\n→ 🍶 食四君子丸（空腹）\n⚠️ 和降血壓藥隔30分鐘",
        "key": "sijunzi_am",
    },
    "10:00": {
        "msg": "⏰ 10:00 — 食降血壓藥\n\n💊 Amlodipine 2.5mg（可空腹）",
        "key": "amlodipine",
    },
    "12:00": {
        "msg": "⏰ 12:00 — 食四君子丸\n\n🍶 空腹，午餐前30分鐘\n⏰ 食完等半個鐘先食午飯",
        "key": "sijunzi_noon",
    },
    "12:30": {
        "msg": "⏰ 12:30 — 午餐後\n\n💊 Metformin 500mg（第一粒）\n⚠️ 一定要飯後，絕對唔可以空腹食！",
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
        "msg": "⏰ 19:00 — 晚餐後 Metformin\n\n💊 Metformin 500mg（第二粒）\n⚠️ 一定要飯後，食完唔好即刻躺平！",
        "key": "metformin_2",
    },
}

def check_reminders():
    now = now()
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")
    current_key = f"{today}_{current_time}"

    for t, info in REMINDERS.items():
        if t == current_time:
            key = f"{today}_{t}_{info['key']}"
            if key not in last_remind:
                last_remind.add(key)
                send_message(OWNER_ID, info["msg"])

def reminder_loop():
    last_check = 0
    while True:
        now = time.time()
        if now - last_check >= 60:
            check_reminders()
            last_check = now
        time.sleep(30)

# ── Start ──────────────────────────────────────────────────────────

if BOT_TOKEN:
    t1 = threading.Thread(target=poll_updates, daemon=True)
    t1.start()
    t2 = threading.Thread(target=reminder_loop, daemon=True)
    t2.start()
    print("[Health Bot] Started")
