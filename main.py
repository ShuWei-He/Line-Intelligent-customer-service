"""
補習班 LINE 客服草稿系統(FastAPI + line-bot-sdk v3)
- /callback : LINE Webhook(SDK 驗證簽章 → 存訊息 → 檢索 FAQ → Ollama 產生草稿)
- /         : 後台。總覽(導師×時段)、訊息篩選、學生/家長管理、PUSH 傳送(由 Super User 開關)、額度顯示
預設不會自動回覆家長;只有在 Super User 開啟推播、且人工按下「傳送」並確認後,才會用 PUSH 送出。
"""
import asyncio, csv, json, os, re, secrets, sqlite3, time, traceback
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from linebot.v3 import SignatureValidator
from linebot.v3.messaging import (AsyncApiClient, AsyncMessagingApi, Configuration,
                                  PushMessageRequest, ReplyMessageRequest, TextMessage)

load_dotenv()
E = os.environ.get
SECRET, TOKEN = E("LINE_CHANNEL_SECRET", ""), E("LINE_CHANNEL_ACCESS_TOKEN", "")
ADMIN_USER, ADMIN_PASS = E("ADMIN_USER", "admin"), E("ADMIN_PASS", "")
SUPER_USER, SUPER_PASS = E("SUPER_USER", "super"), E("SUPER_PASS", "")
OLLAMA, LLM, EMB = E("OLLAMA_URL", "http://localhost:11434"), E("LLM_MODEL", "qwen3.5:9b"), E("EMBED_MODEL", "bge-m3")
DEBOUNCE = int(E("DEBOUNCE_SECONDS", "20"))
FAQ_STRONG, FAQ_MIN = float(E("FAQ_STRONG", "0.62")), float(E("FAQ_MIN", "0.45"))
PUSH_NOTIFY = E("PUSH_TO_OWNER", "0") == "1"      # 推播「新草稿通知」給自己(會耗額度,預設關)
OWNER = E("OWNER_USER_ID", "")
DATA = Path(E("DATA_DIR", "data"))
DB_PATH = DATA / "app.db"
WD = "一二三四五六日"

SENSITIVE = ["退費", "退款", "退錢", "投訴", "客訴", "申訴", "不滿", "生氣", "霸凌", "打人", "被打", "受傷", "過敏",
             "就醫", "太失望", "消保", "律師", "報警", "換老師", "歧視", "受不了"]
TRIVIAL = re.compile(r"^[\s\W]*(好的?|好喔|ok|OK|收到|了解|謝謝|感謝|多謝|謝謝您|謝謝老師|沒問題|嗯嗯?|👍|🙏)+[\s\W]*$", re.I)
IGNORE, tasks = set(), set()
llm_lock = asyncio.Semaphore(1)
S = {"line": None, "quota": (0, None)}
validator = SignatureValidator(SECRET)

# ---------- SQLite ----------
def Q(sql, a=()):
    c = sqlite3.connect(DB_PATH, timeout=10); c.row_factory = sqlite3.Row
    try: return c.execute(sql, a).fetchall()
    finally: c.close()

def X(sql, a=()):
    c = sqlite3.connect(DB_PATH, timeout=10)
    try:
        cur = c.execute(sql, a); c.commit(); return cur.lastrowid
    finally: c.close()

def get_setting(k, d=""):
    r = Q("SELECT value FROM settings WHERE key=?", (k,)); return r[0]["value"] if r else d

def set_setting(k, v): X("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))

def slot_label(r): return f"週{WD[r['weekday']]} {r['t_start']}-{r['t_end']}"

def init_db():
    DATA.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, ts INTEGER, text TEXT);
    CREATE TABLE IF NOT EXISTS profiles(user_id TEXT PRIMARY KEY, name TEXT);
    CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, name TEXT, ts INTEGER,
      last_msg_id INTEGER, parent_text TEXT, label TEXT, score REAL, draft TEXT, note TEXT, sources TEXT,
      status TEXT DEFAULT 'new', final_text TEXT, sent_via TEXT);
    CREATE TABLE IF NOT EXISTS teachers(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
    CREATE TABLE IF NOT EXISTS slots(id INTEGER PRIMARY KEY AUTOINCREMENT, weekday INTEGER NOT NULL,
      t_start TEXT NOT NULL, t_end TEXT NOT NULL, UNIQUE(weekday, t_start, t_end));
    CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      parent_user_id TEXT, teacher_id INTEGER, slot_id INTEGER, note TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS sent_log(id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER, user_id TEXT,
      name TEXT, ts INTEGER, text TEXT, ok INTEGER, error TEXT, by_role TEXT);""")
    try: c.execute("ALTER TABLE drafts ADD COLUMN sent_via TEXT")      # 舊版資料庫升級
    except sqlite3.OperationalError: pass
    if not c.execute("SELECT 1 FROM slots LIMIT 1").fetchone():        # 預設週六四個時段
        c.executemany("INSERT INTO slots(weekday,t_start,t_end) VALUES(5,?,?)",
                      [("9:00", "10:30"), ("10:30", "12:00"), ("13:40", "15:10"), ("15:10", "16:40")])
    c.commit(); c.close()

# ---------- FAQ 檢索(Ollama embedding 跑 CPU)----------
async def embed(texts):
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{OLLAMA}/api/embed", json={"model": EMB, "input": texts, "keep_alive": "30m",
                                                       "options": {"num_gpu": 0}})
    if r.status_code != 200:
        raise RuntimeError(f"Ollama /api/embed 回應 {r.status_code}:{r.text[:200]}(model not found → ollama pull {EMB};404 page not found → 更新 Ollama)")
    v = np.array(r.json()["embeddings"], dtype="float32")
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)

class FAQ:
    items, vecs = [], None
    async def load(self):
        p = DATA / "faq.csv"
        rows = [r for r in csv.DictReader(open(p, encoding="utf-8-sig")) if r.get("question") and r.get("answer")] if p.exists() else []
        keys, items = [], []
        for r in rows:
            for qv in r["question"].split("|"):
                if qv.strip(): keys.append(qv.strip()); items.append(r)
        self.items, self.vecs = items, (await embed(keys) if keys else None)
        print(f"FAQ 載入完成:{len(rows)} 題 / {len(keys)} 種問法")
    async def search(self, text, k=3):
        if self.vecs is None: await self.load()
        if self.vecs is None: return []
        sims = self.vecs @ (await embed([text]))[0]
        out, seen = [], set()
        for i in np.argsort(-sims):
            it = self.items[i]
            if it["question"] in seen: continue
            seen.add(it["question"]); out.append((float(sims[i]), it))
            if len(out) >= k: break
        return out

faq = FAQ()

SYSTEM = """你是補習班(機器人教育)櫃台人員的助理,負責替老師撰寫「回覆家長的草稿」,草稿會由老師審核修改後才送出。
規則:
1. 只能使用【參考資料】中的事實。不可自行編造或推測價格、日期、名額、規則、課程內容。
2. 資料不足以回答時,needs_info 設為 true,並在草稿中用【請補充:…】標出老師需要填的地方。
3. 語氣親切自然,像補習班櫃台:開頭可用「媽媽您好」(不確定身分用「您好」),句尾可用「喔」「唷」,簡短分段,不要過度客套。
4. 不要提到你是 AI,不要提到「參考資料」。若家長問的是個別安排(補課、請假、時段),給出禮貌的確認或詢問草稿,時間與名額用【請補充:…】。
5. 參考資料有「更新」年月。若更新日期距今超過一年,或標示「待確認」,其中的價格、日期、名額不可直接採用,改用【請確認:…】標示。
6. note:用一句話告訴老師回覆前要確認什麼(例如價格是否最新);沒有就留空字串。
只輸出 JSON。"""
SCHEMA = {"type": "object", "properties": {"draft": {"type": "string"}, "needs_info": {"type": "boolean"},
          "note": {"type": "string"}}, "required": ["draft", "needs_info", "note"]}

async def make_draft(text, history, hits):
    ref = "\n\n".join(f"[參考{i+1}](相似度 {s:.2f},更新:{it.get('updated', '')})\n問:{it['question'].split('|')[0]}\n答:{it['answer']}"
                      for i, (s, it) in enumerate(hits))
    user = f"今天日期:{time.strftime('%Y-%m-%d')}\n\n【參考資料】\n{ref or '(無相關資料)'}\n\n【家長先前的訊息】\n{history or '(無)'}\n\n【家長這次的訊息】\n{text}\n\n請產生給老師審核的回覆草稿。"
    body = {"model": LLM, "stream": False, "think": False, "format": SCHEMA,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            "options": {"temperature": 0.3, "num_ctx": 4096, "num_predict": 600}}
    async with llm_lock:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{OLLAMA}/api/chat", json=body); r.raise_for_status()
    return json.loads(r.json()["message"]["content"])

# ---------- LINE(SDK)----------
async def push(to, text):
    return await S["line"].push_message(PushMessageRequest(to=to, messages=[TextMessage(text=text)]))

async def get_name(uid):
    r = Q("SELECT name FROM profiles WHERE user_id=?", (uid,))
    if r: return r[0]["name"]
    try:
        name = (await S["line"].get_profile(uid)).display_name
        X("INSERT OR REPLACE INTO profiles VALUES(?,?)", (uid, name)); return name
    except Exception: return "(未知)"

async def get_quota(force=False):
    t, v = S["quota"]
    if not force and v and time.time() - t < 60: return v
    out = {"ok": False}
    try:
        q, c = await S["line"].get_message_quota(), await S["line"].get_message_quota_consumption()
        qtype = str(getattr(q.type, "value", q.type)).lower()
        limit = q.value if qtype == "limited" else None
        used = c.total_usage
        out = {"ok": True, "type": qtype, "limit": limit, "used": used, "remaining": None if limit is None else limit - used}
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    m0 = time.mktime(time.strptime(time.strftime("%Y-%m-01"), "%Y-%m-%d"))
    out["local_sent"] = Q("SELECT count(*) n FROM sent_log WHERE ok=1 AND ts>=?", (int(m0),))[0]["n"]
    S["quota"] = (time.time(), out); return out

# ---------- 訊息處理 ----------
async def process(uid, msg_id):
    try:
        await asyncio.sleep(DEBOUNCE)
        if Q("SELECT max(id) m FROM messages WHERE user_id=?", (uid,))[0]["m"] != msg_id: return
        last = Q("SELECT coalesce(max(last_msg_id),0) m FROM drafts WHERE user_id=?", (uid,))[0]["m"]
        burst = [r["text"] for r in Q("SELECT text FROM messages WHERE user_id=? AND id>? ORDER BY id", (uid, last))]
        hist = [r["text"] for r in Q("SELECT text FROM messages WHERE user_id=? AND id<=? ORDER BY id DESC LIMIT 6", (uid, last))][::-1]
        text, name = "\n".join(burst), await get_name(uid)
        label, score, draft, note, srcs, status = "personal", 0.0, "", "", [], "new"
        if any(k in text for k in SENSITIVE):
            label, note = "sensitive", "涉及退費/投訴/孩子狀況等敏感事項,系統不產生草稿,請親自回覆。"
        elif all(TRIVIAL.match(t.strip()) for t in burst):
            label, status = "ack", "skipped"
        else:
            try:
                hits = [h for h in await faq.search(text) if h[0] >= FAQ_MIN]
                score = hits[0][0] if hits else 0.0
                label = "faq" if score >= FAQ_STRONG else "personal"
                srcs = [{"q": h[1]["question"].split("|")[0], "score": round(h[0], 2)} for h in hits]
                out = await make_draft(text, "\n".join(hist), hits)
                draft, note = out.get("draft", ""), out.get("note", "")
                if out.get("needs_info"): note = ("資料庫資訊不完整,請補充【】處。 " + note).strip()
            except Exception as e:
                note = f"草稿產生失敗:{type(e).__name__}: {e}"
        X("INSERT INTO drafts(user_id,name,ts,last_msg_id,parent_text,label,score,draft,note,sources,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (uid, name, int(time.time()), msg_id, text, label, score, draft, note, json.dumps(srcs, ensure_ascii=False), status))
        if PUSH_NOTIFY and OWNER and status == "new":
            await push(OWNER, f"📩 {name}:{text[:40]}\n草稿已備妥,請開後台查看")
    except Exception:
        traceback.print_exc()

@asynccontextmanager
async def lifespan(app):
    if not ADMIN_PASS or not SUPER_PASS: raise RuntimeError("請在 .env 設定 ADMIN_PASS 與 SUPER_PASS")
    init_db()
    p = DATA / "ignore_texts.txt"
    if p.exists(): IGNORE.update(l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip())
    try: await faq.load()
    except Exception as e: print(f"⚠️ FAQ 載入失敗(請確認 Ollama 已啟動且已 pull {EMB}):{e}")
    async with AsyncApiClient(Configuration(access_token=TOKEN)) as client:
        S["line"] = AsyncMessagingApi(client)
        yield

app = FastAPI(lifespan=lifespan)
sec = HTTPBasic()

def auth(c: HTTPBasicCredentials = Depends(sec)) -> str:
    eq = lambda a, b: secrets.compare_digest(a.encode(), b.encode())
    if eq(c.username, SUPER_USER) and eq(c.password, SUPER_PASS): return "super"
    if eq(c.username, ADMIN_USER) and eq(c.password, ADMIN_PASS): return "admin"
    raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

def need_super(role: str = Depends(auth)):
    if role != "super": raise HTTPException(403, "需要 Super User 權限")
    return role

@app.post("/callback")
async def callback(request: Request):
    body = await request.body()
    if not validator.validate(body.decode("utf-8"), request.headers.get("x-line-signature", "")):
        raise HTTPException(400, "bad signature")
    for ev in json.loads(body).get("events", []):          # 以 dict 解析:遇到新事件類型也不會出錯
        m, src = ev.get("message", {}), ev.get("source", {})
        if ev.get("type") != "message" or m.get("type") != "text" or src.get("type") != "user": continue
        uid, text = src["userId"], m["text"].strip()[:2000]
        if text == "#myid":
            await S["line"].reply_message(ReplyMessageRequest(reply_token=ev["replyToken"], messages=[TextMessage(text=uid)])); continue
        if text in IGNORE: continue
        mid = X("INSERT INTO messages(user_id,ts,text) VALUES(?,?,?)", (uid, ev["timestamp"] // 1000, text))
        t = asyncio.create_task(process(uid, mid)); tasks.add(t); t.add_done_callback(tasks.discard)
    return "OK"

@app.get("/health")
async def health(): return {"ok": True}

@app.get("/", dependencies=[Depends(auth)])
async def index(): return FileResponse(Path(__file__).parent / "static" / "app.html")

# ---------- 後台 API ----------
@app.get("/api/me")
async def me(role: str = Depends(auth)): return {"role": role, "push_enabled": get_setting("push_enabled", "0") == "1"}

@app.get("/api/meta")
async def meta(role: str = Depends(auth)):
    slots = sorted(Q("SELECT * FROM slots"), key=lambda r: (r["weekday"], *map(int, r["t_start"].split(":"))))
    return {"role": role, "push_enabled": get_setting("push_enabled", "0") == "1",
            "teachers": [dict(r) for r in Q("SELECT * FROM teachers ORDER BY name")],
            "slots": [{"id": r["id"], "label": slot_label(r), "weekday": r["weekday"]} for r in slots],
            "contacts": [dict(r) for r in Q("SELECT user_id, name FROM profiles ORDER BY name")]}

@app.get("/api/quota", dependencies=[Depends(auth)])
async def quota(): return await get_quota()

@app.get("/api/drafts", dependencies=[Depends(auth)])
async def list_drafts(teacher: str = "", slot: str = ""):
    where, args = ["d.status='new'"], []
    if teacher == "none":
        where.append("NOT EXISTS (SELECT 1 FROM students s WHERE s.parent_user_id=d.user_id AND s.teacher_id IS NOT NULL)")
    else:
        cond = []
        if teacher.isdigit(): cond.append("s.teacher_id=?"); args.append(int(teacher))
        if slot.isdigit(): cond.append("s.slot_id=?"); args.append(int(slot))
        if cond: where.append("EXISTS (SELECT 1 FROM students s WHERE s.parent_user_id=d.user_id AND " + " AND ".join(cond) + ")")
    rows = [dict(r) for r in Q(f"SELECT d.* FROM drafts d WHERE {' AND '.join(where)} ORDER BY d.id DESC LIMIT 100", args)]
    for r in rows:
        r["sources"] = json.loads(r["sources"] or "[]")
        r["students"] = [{"name": s["name"], "teacher": s["tname"], "slot": slot_label(s) if s["weekday"] is not None else None}
                         for s in Q("""SELECT s.name, t.name tname, sl.weekday, sl.t_start, sl.t_end FROM students s
                                       LEFT JOIN teachers t ON t.id=s.teacher_id LEFT JOIN slots sl ON sl.id=s.slot_id
                                       WHERE s.parent_user_id=?""", (r["user_id"],))]
    return rows

@app.get("/api/overview", dependencies=[Depends(auth)])
async def overview():
    cells = {}
    for r in Q("SELECT teacher_id t, slot_id s, count(*) n FROM students GROUP BY teacher_id, slot_id"):
        cells[(r["t"], r["s"])] = {"t": r["t"], "s": r["s"], "students": r["n"], "pending": 0}
    for r in Q("""SELECT s.teacher_id t, s.slot_id sl, count(DISTINCT d.id) n FROM drafts d JOIN students s ON s.parent_user_id=d.user_id
                  WHERE d.status='new' GROUP BY s.teacher_id, s.slot_id"""):
        cells.setdefault((r["t"], r["sl"]), {"t": r["t"], "s": r["sl"], "students": 0, "pending": 0})["pending"] = r["n"]
    bt = {r["t"]: r["n"] for r in Q("SELECT s.teacher_id t, count(DISTINCT d.id) n FROM drafts d JOIN students s ON s.parent_user_id=d.user_id WHERE d.status='new' GROUP BY s.teacher_id")}
    bs = {r["t"]: r["n"] for r in Q("SELECT s.slot_id t, count(DISTINCT d.id) n FROM drafts d JOIN students s ON s.parent_user_id=d.user_id WHERE d.status='new' GROUP BY s.slot_id")}
    un = Q("SELECT count(*) n FROM drafts d WHERE d.status='new' AND NOT EXISTS (SELECT 1 FROM students s WHERE s.parent_user_id=d.user_id AND s.teacher_id IS NOT NULL)")[0]["n"]
    return {"cells": list(cells.values()), "by_teacher": bt, "by_slot": bs, "unassigned": un}

@app.post("/api/drafts/{did}/{action}")
async def act(did: int, action: str, request: Request, role: str = Depends(auth)):
    if action == "send":
        if get_setting("push_enabled", "0") != "1": raise HTTPException(403, "推播功能已由 Super User 關閉")
        d = Q("SELECT * FROM drafts WHERE id=?", (did,))
        if not d: raise HTTPException(404)
        text = ((await request.json()).get("text") or "").strip()
        if not text or len(text) > 5000: raise HTTPException(400, "訊息不可為空,且不可超過 5000 字")
        qt = await get_quota()
        if qt.get("ok") and qt.get("remaining") is not None and qt["remaining"] <= 0:
            raise HTTPException(403, "本月 PUSH 額度已用完")
        try: await push(d[0]["user_id"], text); ok, err = 1, ""
        except Exception as e: ok, err = 0, f"{type(e).__name__}: {str(e)[:200]}"
        X("INSERT INTO sent_log(draft_id,user_id,name,ts,text,ok,error,by_role) VALUES(?,?,?,?,?,?,?,?)",
          (did, d[0]["user_id"], d[0]["name"], int(time.time()), text, ok, err, role))
        if not ok: raise HTTPException(502, err)
        X("UPDATE drafts SET status='done', final_text=?, sent_via='push' WHERE id=?", (text, did))
        await get_quota(force=True); return {"ok": True}
    if action not in ("done", "skip"): raise HTTPException(404)
    final = (await request.json()).get("final_text", "") if action == "done" else ""
    X("UPDATE drafts SET status=?, final_text=? WHERE id=?", ("done" if action == "done" else "skipped", final, did))
    return {"ok": True}

@app.post("/api/settings/push")
async def set_push(request: Request, role: str = Depends(need_super)):
    en = bool((await request.json()).get("enabled"))
    set_setting("push_enabled", "1" if en else "0"); return {"push_enabled": en}

@app.get("/api/sent_log")
async def sent_log(role: str = Depends(need_super)):
    return [dict(r) for r in Q("SELECT ts,name,text,ok,error,by_role FROM sent_log ORDER BY id DESC LIMIT 50")]

@app.post("/api/reload_faq", dependencies=[Depends(auth)])
async def reload_faq():
    await faq.load(); return {"ok": True, "items": len(faq.items)}

# ---- 導師 / 時段 / 學生 ----
async def _name(request, field="name"):
    v = ((await request.json()).get(field) or "").strip()
    if not v: raise HTTPException(400, "請輸入名稱")
    return v

@app.post("/api/teachers", dependencies=[Depends(auth)])
async def add_teacher(request: Request):
    try: X("INSERT INTO teachers(name) VALUES(?)", (await _name(request),))
    except sqlite3.IntegrityError: raise HTTPException(400, "已有同名導師")
    return {"ok": True}

@app.delete("/api/teachers/{tid}", dependencies=[Depends(auth)])
async def del_teacher(tid: int):
    X("UPDATE students SET teacher_id=NULL WHERE teacher_id=?", (tid,)); X("DELETE FROM teachers WHERE id=?", (tid,)); return {"ok": True}

@app.post("/api/slots", dependencies=[Depends(auth)])
async def add_slot(request: Request):
    j = await request.json()
    ok = lambda s: re.fullmatch(r"\d{1,2}:\d{2}", str(s or ""))
    if not (ok(j.get("start")) and ok(j.get("end")) and str(j.get("weekday")) in list("0123456")):
        raise HTTPException(400, "時段格式錯誤(例:9:00 與 10:30)")
    try: X("INSERT INTO slots(weekday,t_start,t_end) VALUES(?,?,?)", (int(j["weekday"]), j["start"], j["end"]))
    except sqlite3.IntegrityError: raise HTTPException(400, "已有相同時段")
    return {"ok": True}

@app.delete("/api/slots/{sid}", dependencies=[Depends(auth)])
async def del_slot(sid: int):
    X("UPDATE students SET slot_id=NULL WHERE slot_id=?", (sid,)); X("DELETE FROM slots WHERE id=?", (sid,)); return {"ok": True}

@app.get("/api/students", dependencies=[Depends(auth)])
async def students():
    return [dict(r) for r in Q("SELECT * FROM students ORDER BY teacher_id, slot_id, name")]

def _stu(j):
    name = (j.get("name") or "").strip()
    if not name: raise HTTPException(400, "請輸入學生姓名")
    i = lambda v: int(v) if str(v or "").isdigit() and int(v) > 0 else None
    return (name, j.get("parent_user_id") or None, i(j.get("teacher_id")), i(j.get("slot_id")), (j.get("note") or "").strip())

@app.post("/api/students", dependencies=[Depends(auth)])
async def add_student(request: Request):
    X("INSERT INTO students(name,parent_user_id,teacher_id,slot_id,note) VALUES(?,?,?,?,?)", _stu(await request.json())); return {"ok": True}

@app.put("/api/students/{sid}", dependencies=[Depends(auth)])
async def upd_student(sid: int, request: Request):
    X("UPDATE students SET name=?,parent_user_id=?,teacher_id=?,slot_id=?,note=? WHERE id=?", (*_stu(await request.json()), sid)); return {"ok": True}

@app.delete("/api/students/{sid}", dependencies=[Depends(auth)])
async def del_student(sid: int):
    X("DELETE FROM students WHERE id=?", (sid,)); return {"ok": True}