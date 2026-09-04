import os, tempfile, json, sys, glob
from datetime import datetime, timezone, timedelta

tmpdir = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "123:FAKE"
os.environ["OWNER_ID"] = "999"
os.environ["DATA_DIR"] = tmpdir

import bot as b

sent = []
photos = []
docs = []
b.send_message = lambda cid, text, *a, **k: sent.append(text)
b.send_photo   = lambda cid, path, cap="", *a, **k: (photos.append(path), sent.append(cap))
b.send_document= lambda cid, path, cap="", *a, **k: docs.append((path, cap))
b.edit_message = lambda cid, mid, text, *a, **k: sent.append(text)

CID = "999"
results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))

def start(cb): b.handle_callback({"id":"x","data":cb}, CID, 1)
def reply(t):  b.handle_text(t, CID)
def reset():
    sent.clear(); photos.clear(); docs.clear(); b.pending.clear()

TZ = b.TZ

# ── Weight entry ──
reset()
start("weight")
reply("68.5")
d = b.load_data()
vals = [r["value"] for r in d[list(d)[0]]["records"] if r["type"]=="weight"]
check("weight 68.5 recorded", vals==["68.5"], str(vals))
reset()
start("weight"); reply("9999")
check("weight 9999 rejected (range)", any("範圍" in m for m in sent))

# ── Safety alerts ──
reset(); start("bp"); reply("185/122")
check("BP 185/122 triggers emergency alert", any("急症" in m or "救護車" in m for m in sent), str([m[:20] for m in sent]))
reset(); start("sugar_0"); 
reply("3.2")  # hypoglycemia
check("glucose 3.2 triggers low-sugar alert", any("低血糖" in m for m in sent))
b.pending.pop(CID,None)  # clear paired uric prompt
reset(); start("sugar_1")
reply("22")   # hyperglycemia
check("glucose 22 triggers high-sugar alert", any("高血糖" in m for m in sent))
b.pending.pop(CID,None)
reset()
start("uric_acid"); b.handle_callback({"id":"x","data":"uricpick_0"},CID,1); reply("600")
check("uric 600 triggers high-uric alert", any("540" in m for m in sent))
# normal reading no alert
reset(); start("bp"); reply("128/79")
check("normal BP no emergency alert", not any("急症" in m or "救護車" in m for m in sent))

# ── Medication check-in ──
# clear any existing
b.clear_meds_today()
txt = b.get_meds_text()
check("meds list has Gliclazide x2", txt.count("Gliclazide")==2, str(txt.count("Gliclazide")))
check("meds progress 0/7 initially", "0/7" in txt, txt)
b.mark_med_taken("amlodipine")
txt2 = b.get_meds_text()
check("after 1 check-in progress 1/7", "1/7" in txt2)
check("amlodipine marked done", "✅" in txt2 and "Amlodipine" in txt2)
b.mark_med_taken("gliclazide_1")
b.mark_med_taken("gliclazide_2")
check("gliclazide both tracked", "3/7" in b.get_meds_text())
# persistence file exists
check("meds_log.json created", os.path.exists(b.get_meds_path()))
b.clear_meds_today()
check("reset meds -> 0/7", "0/7" in b.get_meds_text())

# ── Trend chart generation ──
# Seed ~10 days of data
base = b.hk_now().date()
seed = {}
for i in range(10):
    day = (base - timedelta(days=9-i)).strftime("%Y-%m-%d")
    seed[day] = {"records":[
        {"time":"08:00","type":"sugar_0","value":f"{5.0+i*0.1:.1f}"},
        {"time":"08:01","type":"uric_0","value":str(400+i*10)},
        {"time":"08:05","type":"bp","value":f"{120+i}/{78+i%5}"},
        {"time":"08:10","type":"weight","value":f"{70-i*0.2:.1f}"},
    ]}
with b.data_lock:
    b.save_data(seed)
reset()
b.send_trend_report(CID, 7)
check("trend report sends a photo", len(photos)==1, str(len(photos)))
check("trend png file exists", photos and os.path.exists(photos[0]) and os.path.getsize(photos[0])>5000)
check("trend caption has 血糖/血壓/尿酸/體重", all(k in sent[-1] for k in ["血糖","血壓","尿酸","體重"]))
# 30-day range works too
reset()
b.send_trend_report(CID, 30)
check("30-day trend sends photo", len(photos)==1)

# ── CSV round-trip incl weight ──
# export
b.export_and_send(CID, 1)
csv_path = os.path.join(tmpdir, "health_export.csv")
check("export csv created", os.path.exists(csv_path))
raw = open(csv_path,"rb").read()
txtcsv = raw.decode("utf-8")
check("exported csv contains 體重", "體重" in txtcsv)
# wipe then re-import
with b.data_lock: b.save_data({})
b.restore_from_csv_bytes(raw, CID)
d2 = b.load_data()
n_weight = sum(1 for day in d2.values() for r in day["records"] if r["type"]=="weight")
check("weight survives CSV round-trip", n_weight==10, str(n_weight))
check("import made a .preimport.bak", os.path.exists(b.get_data_path()+".preimport.bak"))

# ── Weekly backup (simulate Monday 09:30) ──
b._last_backup_week = ""
mon = datetime(2026,9,7,9,30,tzinfo=TZ)  # 2026-09-07 is a Monday
b.hk_now = lambda: mon
docs.clear()
b.check_weekly_backup()
check("Monday backup sends a document", len(docs)==1, str(len(docs)))
# same day again -> no duplicate
b.check_weekly_backup()
check("weekly backup only once per week", len(docs)==1)
# Tuesday -> nothing
tue = datetime(2026,9,8,9,30,tzinfo=TZ)
b.hk_now = lambda: tue
b.check_weekly_backup()
check("no backup on Tuesday", len(docs)==1)

print("\n==== RESULTS ====")
ok=0
for name,passed,detail in results:
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  [{detail}]" if (detail and not passed) else ""))
    ok+=passed
print(f"\n{ok}/{len(results)} passed")
sys.exit(0 if ok==len(results) else 1)
