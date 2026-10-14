"""
補習班 LINE 客服草稿系統(FastAPI + line-bot-sdk v3)
- /callback : LINE Webhook(SDK 驗證簽章 → 存訊息 → 檢索 FAQ → Ollama 產生草稿)
- /         : 後台。總覽(導師×時段)、訊息篩選、學生/家長管理、PUSH 傳送(由 Super User 開關)、額度顯示
預設不會自動回覆家長;只有在 Super User 開啟推播、且人工按下「傳送」並確認後,才會用 PUSH 送出。
"""
import asyncio, csv, hashlib, json, os, re, secrets, sqlite3, time, traceback
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from linebot.v3 import SignatureValidator
from linebot.v3.messaging import (AsyncApiClient, AsyncMessagingApi, Configuration,
                                  PushMessageRequest, ReplyMessageRequest, TextMessage)
try:                                                   # 標記已讀 API:需 line-bot-sdk >= 3.21
    from linebot.v3.messaging import MarkMessagesAsReadByTokenRequest
except ImportError:
    MarkMessagesAsReadByTokenRequest = None

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
REPLY_WINDOW = int(E("REPLY_WINDOW_SECONDS", "55"))    # reply token 可用秒數;超過就改用 PUSH
AUTO_THRESHOLD = float(E("AUTO_THRESHOLD", "0.80"))     # 自動回覆相似度門檻預設值(後台可調)
SESSION_HOURS = int(E("SESSION_HOURS", "12"))         # 登入有效時數,到期自動登出
FAILS = {}                                              # 登入失敗紀錄:ip -> [次數, 第一次失敗時間]
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
    CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, role TEXT, expires INTEGER, created INTEGER);
    CREATE TABLE IF NOT EXISTS sent_log(id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER, user_id TEXT,
      name TEXT, ts INTEGER, text TEXT, ok INTEGER, error TEXT, by_role TEXT);""")
    try: c.execute("ALTER TABLE drafts ADD COLUMN sent_via TEXT")      # 舊版資料庫升級
    except sqlite3.OperationalError: pass
    for sql in ("ALTER TABLE messages ADD COLUMN reply_token TEXT", "ALTER TABLE messages ADD COLUMN recv REAL",
                "ALTER TABLE messages ADD COLUMN read_token TEXT", "ALTER TABLE sent_log ADD COLUMN channel TEXT"):
        try: c.execute(sql)
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
        n_auto = sum(str(r.get("auto") or "").strip().upper() in ("Y", "1", "是") for r in rows)
        print(f"FAQ 載入完成:{len(rows)} 題 / {len(keys)} 種問法;可自動回覆(auto=Y)的有 {n_auto} 題")
        if rows and "auto" not in rows[0]:
            print("⚠️ faq.csv 沒有 auto 欄,所有列都不會自動回覆。請新增 auto 欄,並把可自動回覆的列填 Y")
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
    out["local_sent"] = Q("SELECT count(*) n FROM sent_log WHERE ok=1 AND ts>=? AND coalesce(channel,'push')='push'", (int(m0),))[0]["n"]
    S["quota"] = (time.time(), out); return out

def token_of(msg_id):
    r = Q("SELECT read_token FROM messages WHERE id=?", (msg_id,)); return r[0]["read_token"] if r else None

async def mark_read(token):
    """標記 LINE 已讀(該則與之前的訊息都會變已讀)。需 OA Manager 回應設定「聊天」為開啟。失敗不拋例外,回傳 (成功, 錯誤)。"""
    if not token: return False, "沒有已讀 token(舊訊息或事件未帶 token)"
    try:
        if MarkMessagesAsReadByTokenRequest is not None:
            await S["line"].mark_messages_as_read_by_token(MarkMessagesAsReadByTokenRequest(mark_as_read_token=token))
        else:                                          # SDK 太舊:直接呼叫 HTTP
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post("https://api.line.me/v2/bot/chat/markAsRead", json={"markAsReadToken": token},
                                 headers={"Authorization": f"Bearer {TOKEN}"})
            if r.status_code != 200: return False, f"HTTP {r.status_code}: {r.text[:100]}"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"

async def mark_read_quick(token):
    """標已讀但最多等 5 秒,避免拖慢回覆而錯過 reply token 時限。"""
    try: return await asyncio.wait_for(mark_read(token), 5)
    except Exception: return False, "標記已讀逾時"

def auto_decision(hits, text, check_switch=True):
    """回傳 (可自動回覆的 FAQ 列或 None, 原因說明)。原因會顯示在草稿卡片與「自動回覆測試」,方便調整。"""
    if check_switch and get_setting("auto_reply", "0") != "1": return None, "自動回覆功能未開啟(Super User 設定頁)"
    if not hits: return None, f"沒有相似度 ≥ {FAQ_MIN} 的 FAQ"
    if len(text) > 100: return None, "訊息超過 100 字,不自動回覆"
    sc, it = hits[0]
    thr, q = float(get_setting("auto_threshold", str(AUTO_THRESHOLD))), it["question"].split("|")[0]
    if sc < thr: return None, f"最高相似度 {sc:.2f} < 門檻 {thr:.2f}(最像:{q})"
    if "auto" not in it: return None, "faq.csv 沒有 auto 欄,請新增並把可自動回覆的列填 Y"
    if str(it.get("auto") or "").strip().upper() not in ("Y", "1", "是"): return None, f"FAQ「{q}」的 auto 欄不是 Y"
    ans = (it.get("answer") or "").strip()
    if not ans or "【" in ans: return None, f"FAQ「{q}」的答案為空或含【】占位符"
    if str(it.get("updated") or "").strip() == "待確認": return None, f"FAQ「{q}」的更新日期是「待確認」"
    return it, "符合自動回覆條件"

async def auto_send(uid, name, text, it, msg_id):
    """先標已讀,再用免費的 reply 送出 FAQ 標準答案;逾時或失敗才改用 PUSH(且須 Super User 已開啟推播)。回傳 (管道, 錯誤)。"""
    ans = it["answer"].strip()
    row = Q("SELECT reply_token, recv, read_token FROM messages WHERE id=?", (msg_id,))[0]
    await mark_read_quick(row["read_token"])
    channel, err = None, ""
    if row["reply_token"] and time.time() - (row["recv"] or 0) < REPLY_WINDOW:
        try:
            await S["line"].reply_message(ReplyMessageRequest(reply_token=row["reply_token"], messages=[TextMessage(text=ans)]))
            channel = "reply"
        except Exception as e:
            err = f"reply 失敗:{type(e).__name__}: {str(e)[:100]}"
    else:
        err = f"已超過 {REPLY_WINDOW} 秒,reply token 不可用"
    if channel is None:
        if get_setting("push_enabled", "0") != "1":
            err += ";推播功能關閉,無法改用 PUSH"
        else:
            qt = await get_quota()
            if qt.get("ok") and qt.get("remaining") is not None and qt["remaining"] <= 0:
                err += ";PUSH 額度已用完"
            else:
                try: await push(uid, ans); channel = "push"; await get_quota(force=True)
                except Exception as e: err += f";push 失敗:{type(e).__name__}: {str(e)[:100]}"
    X("INSERT INTO sent_log(draft_id,user_id,name,ts,text,ok,error,by_role,channel) VALUES(?,?,?,?,?,?,?,?,?)",
      (0, uid, name, int(time.time()), ans, 1 if channel else 0, err, "auto", channel or ""))
    return channel, err

# ---------- 訊息處理 ----------
async def process(uid, msg_id):
    try:
        await asyncio.sleep(DEBOUNCE)
        if Q("SELECT max(id) m FROM messages WHERE user_id=?", (uid,))[0]["m"] != msg_id: return
        last = Q("SELECT coalesce(max(last_msg_id),0) m FROM drafts WHERE user_id=?", (uid,))[0]["m"]
        burst = [r["text"] for r in Q("SELECT text FROM messages WHERE user_id=? AND id>? ORDER BY id", (uid, last))]
        hist = [r["text"] for r in Q("SELECT text FROM messages WHERE user_id=? AND id<=? ORDER BY id DESC LIMIT 6", (uid, last))][::-1]
        text, name = "\n".join(burst), await get_name(uid)
        label, score, draft, note, srcs, status, sent_via = "personal", 0.0, "", "", [], "new", None
        if any(k in text for k in SENSITIVE):
            label, note = "sensitive", "涉及退費/投訴/孩子狀況等敏感事項,系統不產生草稿,請親自回覆。"
        elif all(TRIVIAL.match(t.strip()) for t in burst):
            label, status = "ack", "ack"               # 無須回覆,但需標記已讀(後台「無須回覆」頁)
        else:
            try:
                hits = [h for h in await faq.search(text) if h[0] >= FAQ_MIN]
                score = hits[0][0] if hits else 0.0
                label = "faq" if score >= FAQ_STRONG else "personal"
                srcs = [{"q": h[1]["question"].split("|")[0], "score": round(h[0], 2)} for h in hits]
                item, why = auto_decision(hits, text)
                if not item and hits and (score >= FAQ_STRONG or get_setting("auto_reply", "0") == "1"):
                    note = f"未自動回覆:{why}"; print(f"[auto] {name}:{why}")
                if item:
                    ch, err = await auto_send(uid, name, text, item, msg_id)
                    if ch:
                        label, status, draft, sent_via = "auto", "auto", item["answer"].strip(), f"auto_{ch}"
                        note = "自動回覆(免費 reply)" if ch == "reply" else "自動回覆(逾時,改用 PUSH 補送)"
                    else:
                        note = f"自動回覆未成功:{err}。已改為人工處理。"
                if status != "auto":
                    out = await make_draft(text, "\n".join(hist), hits)
                    draft = out.get("draft", "")
                    note = (note + " " + out.get("note", "")).strip()
                    if out.get("needs_info"): note = ("資料庫資訊不完整,請補充【】處。 " + note).strip()
            except Exception as e:
                note = f"草稿產生失敗:{type(e).__name__}: {e}"
        X("INSERT INTO drafts(user_id,name,ts,last_msg_id,parent_text,label,score,draft,note,sources,status,final_text,sent_via) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (uid, name, int(time.time()), msg_id, text, label, score, draft, note, json.dumps(srcs, ensure_ascii=False),
           status, draft if status == "auto" else None, sent_via))
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
_h = lambda t: hashlib.sha256(t.encode()).hexdigest()

def current_role(request: Request):
    tok = request.cookies.get("sid")
    if not tok: return None
    r = Q("SELECT role, expires FROM sessions WHERE token=?", (_h(tok),))
    return r[0]["role"] if r and r[0]["expires"] > time.time() else None

def auth(request: Request) -> str:
    role = current_role(request)
    if not role: raise HTTPException(401, "請先登入")
    return role

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
        mid = X("INSERT INTO messages(user_id,ts,text,reply_token,recv,read_token) VALUES(?,?,?,?,?,?)",
                (uid, ev["timestamp"] // 1000, text, ev.get("replyToken"), time.time(), m.get("markAsReadToken")))
        t = asyncio.create_task(process(uid, mid)); tasks.add(t); t.add_done_callback(tasks.discard)
    return "OK"

@app.get("/health")
async def health(): return {"ok": True}

NOSTORE = {"Cache-Control": "no-store"}

@app.get("/")
async def index(request: Request):
    if not current_role(request): return RedirectResponse("/login", status_code=302)
    return FileResponse(Path(__file__).parent / "static" / "app.html", headers=NOSTORE)

@app.get("/login")
async def login_page(request: Request):
    if current_role(request): return RedirectResponse("/", status_code=302)
    return FileResponse(Path(__file__).parent / "static" / "login.html", headers=NOSTORE)

@app.post("/api/login")
async def login(request: Request):
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    now, f = time.time(), FAILS.get(ip)
    if f and f[0] >= 5 and now - f[1] < 300: raise HTTPException(429, "嘗試次數過多,請 5 分鐘後再試")
    j = await request.json()
    u, p = str(j.get("username", "")), str(j.get("password", ""))
    eq = lambda a, b: secrets.compare_digest(a.encode(), b.encode())
    role = "super" if (eq(u, SUPER_USER) and eq(p, SUPER_PASS)) else "admin" if (eq(u, ADMIN_USER) and eq(p, ADMIN_PASS)) else None
    if not role:
        if not f or now - f[1] >= 300: FAILS[ip] = [1, now]
        else: f[0] += 1
        raise HTTPException(401, "帳號或密碼錯誤")
    FAILS.pop(ip, None)
    tok = secrets.token_urlsafe(32)
    X("DELETE FROM sessions WHERE expires<?", (int(now),))
    X("INSERT INTO sessions(token,role,expires,created) VALUES(?,?,?,?)", (_h(tok), role, int(now + SESSION_HOURS * 3600), int(now)))
    resp = JSONResponse({"ok": True, "role": role})
    resp.set_cookie("sid", tok, max_age=SESSION_HOURS * 3600, httponly=True, samesite="lax",
                    secure=request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https")
    return resp

@app.post("/api/logout")
async def logout(request: Request):
    tok = request.cookies.get("sid")
    if tok: X("DELETE FROM sessions WHERE token=?", (_h(tok),))      # 伺服器端也作廢,不只是清瀏覽器
    resp = JSONResponse({"ok": True}); resp.delete_cookie("sid"); return resp

@app.post("/api/logout_all")
async def logout_all(role: str = Depends(need_super)):
    X("DELETE FROM sessions"); return {"ok": True}

# ---------- 後台 API ----------
def _flags():
    return {"push_enabled": get_setting("push_enabled", "0") == "1",
            "auto_enabled": get_setting("auto_reply", "0") == "1",
            "auto_threshold": float(get_setting("auto_threshold", str(AUTO_THRESHOLD))),
            "ack_count": Q("SELECT count(*) n FROM drafts WHERE status='ack'")[0]["n"]}

def _attach(rows):
    for r in rows:
        r["sources"] = json.loads(r["sources"] or "[]")
        r["students"] = [{"name": s["name"], "teacher": s["tname"], "slot": slot_label(s) if s["weekday"] is not None else None}
                         for s in Q("""SELECT s.name, t.name tname, sl.weekday, sl.t_start, sl.t_end FROM students s
                                       LEFT JOIN teachers t ON t.id=s.teacher_id LEFT JOIN slots sl ON sl.id=s.slot_id
                                       WHERE s.parent_user_id=?""", (r["user_id"],))]
    return rows

@app.get("/api/me")
async def me(role: str = Depends(auth)): return {"role": role, **_flags()}

@app.get("/api/meta")
async def meta(role: str = Depends(auth)):
    slots = sorted(Q("SELECT * FROM slots"), key=lambda r: (r["weekday"], *map(int, r["t_start"].split(":"))))
    return {"role": role, **_flags(),
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
        await mark_read_quick(token_of(d[0]["last_msg_id"]))   # 先標已讀,家長才看得到「已讀」
        try: await push(d[0]["user_id"], text); ok, err = 1, ""
        except Exception as e: ok, err = 0, f"{type(e).__name__}: {str(e)[:200]}"
        X("INSERT INTO sent_log(draft_id,user_id,name,ts,text,ok,error,by_role,channel) VALUES(?,?,?,?,?,?,?,?,?)",
          (did, d[0]["user_id"], d[0]["name"], int(time.time()), text, ok, err, role, "push"))
        if not ok: raise HTTPException(502, err)
        X("UPDATE drafts SET status='done', final_text=?, sent_via='push' WHERE id=?", (text, did))
        await get_quota(force=True); return {"ok": True}
    if action == "read":                                    # 無須回覆:只標記 LINE 已讀
        d = Q("SELECT * FROM drafts WHERE id=?", (did,))
        if not d: raise HTTPException(404)
        ok, err = await mark_read(token_of(d[0]["last_msg_id"]))
        X("UPDATE drafts SET status='done', sent_via='read_only' WHERE id=?", (did,))
        return {"ok": True, "line_read": ok, "error": err}
    if action == "flag":                                    # 標記自動回覆有誤(供調整門檻與 FAQ)
        X("UPDATE drafts SET note='⚠️ 人工標記:此自動回覆有誤。 ' || coalesce(note,'') WHERE id=?", (did,)); return {"ok": True}
    if action not in ("done", "skip"): raise HTTPException(404)
    final = (await request.json()).get("final_text", "") if action == "done" else ""
    X("UPDATE drafts SET status=?, final_text=? WHERE id=?", ("done" if action == "done" else "skipped", final, did))
    return {"ok": True}

@app.get("/api/ack", dependencies=[Depends(auth)])
async def ack_list():
    return _attach([dict(r) for r in Q("SELECT * FROM drafts WHERE status='ack' ORDER BY id DESC LIMIT 200")])

@app.post("/api/ack/read_all", dependencies=[Depends(auth)])
async def ack_read_all():
    rows = Q("SELECT id, user_id, last_msg_id FROM drafts WHERE status='ack'")
    latest, failed, err = {}, 0, ""
    for r in rows: latest[r["user_id"]] = max(latest.get(r["user_id"], 0), r["last_msg_id"])
    for mid in latest.values():                             # 每位家長標記最新一則即可(之前的一併已讀)
        ok, e = await mark_read(token_of(mid))
        if not ok: failed, err = failed + 1, e
    if rows:
        X(f"UPDATE drafts SET status='done', sent_via='read_only' WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])
    return {"done": len(rows), "failed": failed, "error": err}

@app.get("/api/auto_log", dependencies=[Depends(auth)])
async def auto_log():
    return _attach([dict(r) for r in Q("SELECT * FROM drafts WHERE status='auto' ORDER BY id DESC LIMIT 100")])

@app.post("/api/auto_test", dependencies=[Depends(auth)])
async def auto_test(request: Request):
    text = ((await request.json()).get("text") or "").strip()
    if not text: raise HTTPException(400, "請輸入測試訊息")
    try: hits = [h for h in await faq.search(text) if h[0] >= FAQ_MIN]
    except Exception as e: raise HTTPException(502, f"檢索失敗:{str(e)[:200]}")
    item, why = auto_decision(hits, text, check_switch=False)
    return {"threshold": float(get_setting("auto_threshold", str(AUTO_THRESHOLD))), "enabled": get_setting("auto_reply", "0") == "1",
            "eligible": bool(item), "reason": why,
            "hits": [{"q": h[1]["question"].split("|")[0], "score": round(h[0], 3), "auto": str(h[1].get("auto") or "").strip(),
                      "has_placeholder": "【" in (h[1].get("answer") or "")} for h in hits]}

@app.post("/api/settings/auto")
async def set_auto(request: Request, role: str = Depends(need_super)):
    j = await request.json()
    if "enabled" in j: set_setting("auto_reply", "1" if j["enabled"] else "0")
    if "threshold" in j:
        try: t = float(j["threshold"])
        except (TypeError, ValueError): raise HTTPException(400, "門檻必須是數字")
        if not 0.5 <= t <= 0.99: raise HTTPException(400, "門檻請介於 0.5~0.99")
        set_setting("auto_threshold", str(t))
    return _flags()

@app.post("/api/settings/push")
async def set_push(request: Request, role: str = Depends(need_super)):
    en = bool((await request.json()).get("enabled"))
    set_setting("push_enabled", "1" if en else "0"); return {"push_enabled": en}

@app.get("/api/sent_log")
async def sent_log(role: str = Depends(need_super)):
    return [dict(r) for r in Q("SELECT ts,name,text,ok,error,by_role,channel FROM sent_log ORDER BY id DESC LIMIT 50")]

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