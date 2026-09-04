import os, tempfile, json, importlib, sys

tmpdir = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "123:FAKE"
os.environ["OWNER_ID"] = "999"
os.environ["DATA_DIR"] = tmpdir

import bot as b

# Capture all bot→user messages
sent = []
b.send_message = lambda cid, text, *a, **k: sent.append(text)

def reset():
    sent.clear()
    b.pending.clear()

CID = "999"

# helper: start an entry via callback then reply text
def start(cb_data):
    b.handle_callback({"id":"x","data":cb_data}, CID, 1)
def reply(txt):
    b.handle_text(txt, CID)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))

# ── 🔴1 standalone uric acid slot picker ──
reset()
start("uric_acid")
# bot should have shown slot picker, NOT pending yet
check("uric standalone shows slot picker (no pending)", CID not in b.pending)
# pick 午後 (idx1)
b.handle_callback({"id":"x","data":"uricpick_1"}, CID, 1)
check("uricpick_1 sets uric_idx=1", b.pending.get(CID,{}).get("uric_idx")==1)
reply("480")
d = b.load_data()
recs = d[list(d)[0]]["records"]
uric_types = [r["type"] for r in recs]
check("standalone 午後尿酸 stored as uric_1 (NOT uric_0)", "uric_1" in uric_types and "uric_0" not in uric_types, str(uric_types))

# ── 🔴2 sugar range check ──
reset()
start("sugar_0")
reply("52")   # typo for 5.2
check("sugar 52 rejected (out of range)", any("範圍" in m for m in sent), str(sent[-1:]))
reset()
start("sugar_0")
reply("999")
check("sugar 999 rejected", any("範圍" in m for m in sent))
reset()
start("sugar_1")
reply("5.2")
check("sugar 5.2 accepted", any("血糖已記錄" in m for m in sent))
# after sugar, bot asks uric — cancel pending
b.pending.pop(CID, None)

# ── 🔴3 nan / inf rejected ──
reset()
start("sugar_0")
reply("nan")
check("nan rejected", any("格式錯誤" in m or "範圍" in m for m in sent))
reset()
start("uric_acid"); b.handle_callback({"id":"x","data":"uricpick_0"}, CID, 1)
reply("inf")
check("inf rejected", any("格式錯誤" in m or "範圍" in m for m in sent))

# ── 🔴4 BP systolic>diastolic ──
reset()
start("bp")
reply("80/120")
check("BP 80/120 (reversed) rejected", any("打反" in m or "上壓" in m for m in sent), str(sent[-1:]))
reset()
start("bp")
reply("128/79")
check("BP 128/79 accepted", any("血壓已記錄" in m for m in sent))
reset()
start("bp")
reply("300/200")
check("BP 300/200 out of range rejected", any("範圍" in m for m in sent))

# ── 🔴5 atomic write + load corruption tolerance ──
# write a corrupt file and ensure load_data survives
path = b.get_data_path()
with open(path,"w") as f:
    f.write('{"2026-09-04": {"recor')  # truncated
d2 = b.load_data()
check("corrupt JSON -> load returns {} (no crash)", d2 == {})
import glob
check("corrupt file parked as .corrupt.*", len(glob.glob(path+".corrupt.*"))==1)

# ── 🟡6 import backup ──
reset()
# put some current data
b.record_entry(CID, "sugar_0", "5.5")
csv_bytes = ("日期,時間,類型,數值,單位\n"
             "2026-01-01,08:00,空腹血糖,5.0,mmol/L\n"
             "2026-01-01,08:01,空腹尿酸,400,μmol/L\n").encode("utf-8")
b.restore_from_csv_bytes(csv_bytes, CID)
check("import backup .preimport.bak created", os.path.exists(path+".preimport.bak"))
d3 = b.load_data()
check("import replaced data", "2026-01-01" in d3 and not any(k.startswith("2026-09") for k in d3), str(list(d3)))

# ── 🟡7 reminder never skips a minute ──
fired = []
b.send_message = lambda cid, text, *a, **k: fired.append(text)
b._last_cleared_date = ""
b.last_remind = set()
# simulate current time 10:05 (past the 10:00 reminder) — drift scenario
from datetime import datetime, timezone, timedelta
fake = datetime(2026,9,4,10,5,0, tzinfo=b.TZ)
b.hk_now = lambda: fake
b.check_reminders()
check("10:00 med reminder fires even at 10:05 (no skip)", any("Amlodipine" in m for m in fired), str(len(fired)))
# call again same minute -> no duplicate
b.check_reminders()
aml = [m for m in fired if "Amlodipine" in m]
check("reminder fires only once per day", len(aml)==1, str(len(aml)))

# ── summary with bad value doesn't crash (🟡8) ──
b.send_message = lambda *a, **k: None
with open(b.get_data_path(),"w") as f:
    json.dump({"2026-09-04":{"records":[
        {"time":"08:00","type":"sugar_0","value":"5.2"},
        {"time":"08:01","type":"uric_0","value":"garbage"}]}}, f, ensure_ascii=False)
try:
    b.hk_now = lambda: datetime(2026,9,4,9,0,0,tzinfo=b.TZ)
    s = b.get_today_summary()
    check("summary survives non-numeric uric (shows ⚪)", "⚪" in s, repr(s))
except Exception as e:
    check("summary survives non-numeric uric", False, repr(e))

print("\n==== RESULTS ====")
ok = 0
for name, passed, detail in results:
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  [{detail}]" if (detail and not passed) else ""))
    ok += passed
print(f"\n{ok}/{len(results)} passed")
sys.exit(0 if ok==len(results) else 1)
