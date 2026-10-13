"""
Step 1:LINE 官方帳號對話匯出檔清洗
輸入:raw_csv/ 資料夾內所有 .csv(每位家長一個檔)
輸出:
  out/messages.csv        訊息層級(已去識別化、去雜訊)
  out/sessions.jsonl      對話 session(連續同角色訊息已合併成 turn)
  out/parent_map.csv      parent_id <-> 原始名稱對照表(含個資!請單獨加密保管,不要進向量庫/雲端)
  out/report.txt          統計與失敗檔案清單

用法:python step1_clean.py
"""
import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

# ========== 設定 ==========
RAW_DIR = Path("./raw_csv")
OUT_DIR = Path("out")
SALT = "請改成你自己的長隨機字串"      # 雜湊用,請保密,改完後不要再換(否則 parent_id 會變)
SESSION_GAP_AFTER_PARENT_HOURS = 48    # 家長問了但還沒被回:等多久都算同一段(補習班常隔天才回)
SESSION_GAP_AFTER_STAFF_HOURS = 12     # 老師回完後,家長/老師隔這麼久再說話 → 視為新話題
AUTO_SENDERS = {"自動回應訊息"}         # 這些傳送者名稱視為系統自動訊息,直接丟棄
# =========================

OUT_DIR.mkdir(exist_ok=True)

# ---------- 讀檔 ----------
def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def parse_file(path: Path):
    text = decode(path.read_bytes())
    pos = text.find("傳送者類型")
    if pos < 0:
        raise ValueError("找不到表頭『傳送者類型』")
    head, body = text[:pos], text[pos:]
    first_line = body.split("\n", 1)[0]
    delim = "\t" if "\t" in first_line else ","

    m = re.search(r"帳號名稱[\t,]+([^\t,\r\n]*)", head)
    account = m.group(1).strip() if m else ""

    rows = list(csv.reader(io.StringIO(body, newline=""), delimiter=delim))[1:]
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        out.append({
            "sender_type": r[0].strip(),
            "sender_name": r[1].strip(),
            "date": r[2].strip(),
            "time": r[3].strip(),
            "text": delim.join(r[4:]).replace("\r\n", "\n").replace("\r", "\n").strip(),
        })
    return account, out


# ---------- 去識別化 ----------
ROLE_SUFFIX = r"(媽媽|爸爸|媽咪|爸比|奶奶|爺爺|阿嬤|阿公|外婆|外公|阿姨|叔叔|家長|太太|先生|小姐|媽|爸)$"
CJK = re.compile(r"^[\u4e00-\u9fff]+$")

RE_PHONE = re.compile(r"(?<!\d)(?:\+?886[-\s]?)?0?9\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)")
RE_TEL = re.compile(r"(?<!\d)0\d{1,2}[-\s]?\d{6,8}(?!\d)")
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
RE_URL = re.compile(r"https?://\S+")
RE_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?!\d)")
RE_LINE_EMOJI = re.compile(r"\((?=[^)]*[A-Za-z])[A-Za-z0-9_ \-]{2,30}\)")  # (moon laugh) 之類


NAME_STOPWORDS = {"unknown", "舊帳號", "新帳號", "問營隊", "媽媽", "爸爸", "家長", "優勝家", "老師", "營隊", "好友", "官方帳號"}


def build_name_patterns(user_names, file_stem=""):
    """從傳送者名稱 + 檔名建立姓名字典(檔名常含孩子名字,如『君慧（雅川、雅山）』),長的先替換。"""
    tokens, surnames = set(), set()
    sources = list(user_names) + [re.sub(r"^\d+_", "", file_stem)]
    for src in sources:
        for part in re.split(r"[\W_]+", src):          # 空白、括號、頓號、&、-、emoji 全部當分隔
            part = part.strip()
            if len(part) < 2 or part.isdigit() or part.lower() in NAME_STOPWORDS:
                continue
            tokens.add(part)
            core = re.sub(ROLE_SUFFIX, "", part).strip()
            if len(core) >= 2 and core.lower() not in NAME_STOPWORDS:
                tokens.add(core)
            if CJK.match(core) and 3 <= len(core) <= 4:
                tokens.add(core[1:])      # 名字(去姓)
                surnames.add(core[0])
    pats = [(re.compile(re.escape(t), re.I), "[姓名]") for t in sorted(tokens, key=len, reverse=True)]
    for sn in surnames:                   # 「賴媽媽」→「[姓]媽媽」
        pats.append((re.compile(re.escape(sn) + r"(?=媽媽|爸爸|媽咪|爸比|先生|太太|小姐)"), "[姓]"))
    return pats


def deidentify(text, name_pats):
    text = RE_EMAIL.sub("[EMAIL]", text)
    text = RE_URL.sub("[URL]", text)
    text = RE_ID.sub("[ID]", text)
    text = RE_PHONE.sub("[PHONE]", text)
    text = RE_TEL.sub("[PHONE]", text)
    for pat, rep in name_pats:
        text = pat.sub(rep, text)
    return text


# ---------- 雜訊 ----------
RE_MEDIA = re.compile(r"^(貼圖|圖片|照片|影片|語音訊息|檔案|位置資訊|聯絡資訊)已傳送$|^\[(貼圖|圖片|影片|檔案)\]$")


RE_SYSTEM = re.compile(r"^(已取消通話|未接來電|已拒絕通話|對方未接聽|對方已拒絕通話|通話時間\s*[\d:]+)$")


def classify(text):
    if RE_SYSTEM.match(text):
        return "system", ""
    m = RE_MEDIA.match(text)
    if m:
        return "media", (m.group(1) or m.group(2))
    return "text", ""


def parse_ts(r):
    try:
        return datetime.strptime(f"{r['date']} {r['time']}", "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def find_menu_triggers(rows):
    """家長訊息後 2 秒內緊接著『自動回應』→ 視為圖文選單/關鍵字觸發(如『📍課程介紹』『行事曆』),不是真的提問。"""
    trig = set()
    for i in range(len(rows) - 1):
        r, nxt = rows[i], rows[i + 1]
        if r["sender_type"] == "User" and nxt["sender_name"] in AUTO_SENDERS:
            t1, t2 = parse_ts(r), parse_ts(nxt)
            if t1 and t2 and 0 <= (t2 - t1).total_seconds() <= 2:
                trig.add(i)
    return trig


EXTRA_REVIEW = OUT_DIR / "check" / "leak_candidates.csv"


def load_extra_names():
    """讀取 step1b 產生、經人工標記 is_name=Y 的漏網人名 → {parent_id: [name,...]}"""
    if not EXTRA_REVIEW.exists():
        return {}
    d = pd.read_csv(EXTRA_REVIEW, encoding="utf-8-sig", dtype=str).fillna("")
    if "is_name" not in d.columns:
        return {}
    d = d[d["is_name"].str.strip().str.upper() == "Y"]
    res = {}
    for pid, tok in zip(d["parent_id"], d["token"]):
        if tok.strip():
            res.setdefault(pid, []).append(tok.strip())
    return res


def make_parent_id(key: str) -> str:
    return "P" + hashlib.sha256((SALT + key).encode("utf-8")).hexdigest()[:10]


# ---------- 主流程 ----------
def main():
    print(f"工作目錄:{Path.cwd()}")
    print(f"讀取資料夾:{RAW_DIR.resolve()}(存在={RAW_DIR.exists()})")
    files = sorted(p for p in RAW_DIR.rglob("*") if p.suffix.lower() == ".csv") if RAW_DIR.exists() else []
    print(f"找到 {len(files)} 個 CSV")
    if not files:
        print("→ 找不到 CSV。請確認 raw_csv 資料夾與本腳本在同一層,且裡面有 .csv 檔(或改 RAW_DIR 為完整路徑)。")
        return
    all_rows, parent_map, failed = [], [], []
    dropped_system = 0
    extra_names = load_extra_names()
    if extra_names:
        print(f"載入人工標記的漏網人名:{sum(len(v) for v in extra_names.values())} 個(涵蓋 {len(extra_names)} 位家長)")

    for i, f in enumerate(files, 1):
        try:
            account, rows = parse_file(f)
        except Exception as e:
            failed.append((f.name, f"{type(e).__name__}: {e}"))
            print(f"[失敗] {f.name}: {type(e).__name__}: {e}")
            continue
        if not rows:
            failed.append((f.name, f"沒有訊息(檔案 {f.stat().st_size} bytes)"))
            continue
        rows.sort(key=lambda r: parse_ts(r) or datetime.min)   # 穩定排序,同秒保持原順序
        triggers = find_menu_triggers(rows)

        user_names = [r["sender_name"] for r in rows if r["sender_type"] == "User" and r["sender_name"]]
        key = Counter(user_names).most_common(1)[0][0] if user_names else f.stem
        pid = make_parent_id(f.stem)          # 用檔名(含唯一編號),避免多個 Unknown 被合併
        parent_map.append({"parent_id": pid, "main_name": key, "file": f.name, "account": account})
        name_pats = build_name_patterns(set(user_names), f.stem)
        for t in sorted(extra_names.get(pid, []), key=len, reverse=True):
            name_pats.insert(0, (re.compile(re.escape(t)), "[姓名]"))

        for idx, r in enumerate(rows):
            if r["sender_name"] in AUTO_SENDERS:
                continue
            role = "parent" if r["sender_type"] == "User" else "staff"
            kind, media = classify(r["text"])
            if kind == "system":
                dropped_system += 1
                continue
            urls = " ".join(RE_URL.findall(r["text"])) if role == "staff" else ""
            if idx in triggers:
                kind, text = "menu", r["text"]            # 選單點擊:保留記錄但不進對話
            elif kind == "media":
                if role == "staff":
                    continue                      # 老師的貼圖/圖片直接丟掉
                text = f"[{media}]"               # 家長傳圖,保留佔位符(可能是問題的一部分)
            else:
                text = RE_LINE_EMOJI.sub("", r["text"])
                text = deidentify(text, name_pats)
                text = re.sub(r"[ \t]+", " ", text).strip()
            if not text:
                continue
            all_rows.append({
                "parent_id": pid,
                "role": role,
                "staff_name": r["sender_name"] if role == "staff" else "",
                "date": r["date"], "time": r["time"],
                "msg_type": kind, "text": text, "urls": urls,
                "account": account,
            })
        if i % 100 == 0:
            print(f"已處理 {i}/{len(files)}")

    if not all_rows:
        print("沒有任何有效訊息。請看上方 [失敗] 訊息,或檢查 CSV 內容。")
        return
    df = pd.DataFrame(all_rows)
    df["ts"] = pd.to_datetime(df["date"] + " " + df["time"], format="%Y/%m/%d %H:%M:%S", errors="coerce")
    bad_ts = int(df["ts"].isna().sum())
    df = df.dropna(subset=["ts"]).sort_values(["parent_id", "ts"]).reset_index(drop=True)

    # 切 session(選單點擊不算對話)
    conv = df[df.msg_type != "menu"].copy()
    gap_h = conv.groupby("parent_id")["ts"].diff().dt.total_seconds() / 3600
    prev_role = conv.groupby("parent_id")["role"].shift()
    limit = prev_role.map({"parent": SESSION_GAP_AFTER_PARENT_HOURS}).fillna(SESSION_GAP_AFTER_STAFF_HOURS)
    gap = gap_h > limit
    conv["session_no"] = gap.groupby(conv["parent_id"]).cumsum().astype(int)
    conv["session_id"] = conv["parent_id"] + "_" + conv["session_no"].astype(str)
    df = df.join(conv[["session_no", "session_id"]])
    df["session_no"] = df["session_no"].astype("Int64")

    df.drop(columns=["date", "time"]).to_csv(OUT_DIR / "messages.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(parent_map).to_csv(OUT_DIR / "parent_map.csv", index=False, encoding="utf-8-sig")

    # sessions.jsonl:連續同角色訊息合併成一個 turn
    n_sessions = 0
    with open(OUT_DIR / "sessions.jsonl", "w", encoding="utf-8") as fout:
        for sid, g in conv.groupby("session_id", sort=False):
            turns = []
            for _, r in g.iterrows():
                if turns and turns[-1]["role"] == r["role"]:
                    turns[-1]["text"] += "\n" + r["text"]
                    turns[-1]["end"] = str(r["ts"])
                else:
                    turns.append({"role": r["role"], "text": r["text"],
                                  "start": str(r["ts"]), "end": str(r["ts"])})
            fout.write(json.dumps({
                "session_id": sid, "parent_id": g["parent_id"].iloc[0],
                "start": str(g["ts"].iloc[0]), "n_turns": len(turns), "turns": turns,
            }, ensure_ascii=False) + "\n")
            n_sessions += 1

    report = [
        f"CSV 檔案數:{len(files)}(失敗/空檔 {len(failed)})",
        f"成功讀取 {len(parent_map)} 檔,不重複 parent_id:{df['parent_id'].nunique()}",
        f"已丟棄系統事件(取消通話/未接來電):{dropped_system}",
        f"清洗後訊息數:{len(df)}(時間解析失敗被丟棄 {bad_ts})",
        f"  家長訊息:{(df.role == 'parent').sum()}  老師訊息:{(df.role == 'staff').sum()}",
        f"選單/關鍵字點擊(已排除出對話):{(df.msg_type == 'menu').sum()}",
        f"  最常見:{df[df.msg_type == 'menu']['text'].value_counts().head(10).to_dict()}",
        f"Session 數:{n_sessions}",
        f"老師帳號分布:{df[df.role == 'staff']['staff_name'].value_counts().head(10).to_dict()}",
        "", "失敗檔案:",
    ] + [f"  {n}: {why}" for n, why in failed]
    (OUT_DIR / "report.txt").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))


if __name__ == "__main__":
    main()