"""
Step 1b:檢查 step1 輸出是否合理。
用法:python step1b_check.py
輸出到 out/check/:
  stats.txt            統計與異常警告(先看這個)
  sample_sessions.txt  隨機抽樣的對話,人工閱讀(最重要的檢查)
  menu_texts.csv       被判定為「選單點擊」的所有文字,確認沒有誤殺真實提問
  leak_candidates.csv  疑似漏網的人名,請人工標記 is_name=Y 後重跑 step1_clean.py
"""
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

OUT = Path("./out")
CHK = OUT / "check"
CHK.mkdir(exist_ok=True)
random.seed(42)

df = pd.read_csv(OUT / "messages.csv", parse_dates=["ts"], dtype={"session_no": "Int64"})
df["text"] = df["text"].fillna("")
df["urls"] = df["urls"].fillna("")
conv = df[df.msg_type != "menu"].copy()

lines = []


def p(s=""):
    lines.append(str(s))
    print(s)


def warn(cond, msg):
    if cond:
        p(f"  ⚠️ {msg}")


# ============ 1. 統計 ============
p("== 基本統計 ==")
p(f"訊息 {len(df)} 則 / 家長 {df.parent_id.nunique()} 位 / session {conv.session_id.nunique()} 段")
p(f"時間範圍:{df.ts.min()} ~ {df.ts.max()}")
p(f"每年訊息數:{df.groupby(df.ts.dt.year).size().to_dict()}")

per_parent = df.groupby("parent_id").size()
p(f"每位家長訊息數:中位數 {per_parent.median():.0f},P90 {per_parent.quantile(.9):.0f},最大 {per_parent.max()}")
top = per_parent.sort_values(ascending=False).head(5)
p(f"訊息最多的 5 位家長:{top.to_dict()}")
warn(per_parent.max() > per_parent.median() * 50,
     "有家長的訊息量遠高於中位數,可能是多個人被合併,或是官方帳號自己的聊天室,請開 messages.csv 查看該 parent_id")

p("\n== Session 結構 ==")
sess = conv.groupby("session_id").agg(n=("text", "size"), roles=("role", "nunique"),
                                      start=("ts", "min"), end=("ts", "max"))
sess["hours"] = (sess.end - sess.start).dt.total_seconds() / 3600
only_parent = int(((sess.roles == 1) & conv.groupby("session_id")["role"].first().eq("parent")).sum())
only_staff = int(((sess.roles == 1) & conv.groupby("session_id")["role"].first().eq("staff")).sum())
p(f"每段訊息數:中位數 {sess.n.median():.0f},P90 {sess.n.quantile(.9):.0f},最大 {sess.n.max()}")
p(f"每段持續時間(小時):中位數 {sess.hours.median():.1f},P90 {sess.hours.quantile(.9):.1f},最大 {sess.hours.max():.0f}")
p(f"只有家長發言的 session:{only_parent}({only_parent / len(sess):.0%})  只有老師發言:{only_staff}({only_staff / len(sess):.0%})")
warn(only_parent / len(sess) > 0.25, "沒被回覆的家長 session 比例偏高:可能是回覆發生在別的 session(切段太細),或確實沒回")
warn((sess.hours > 24 * 14).sum() > 0, f"有 {(sess.hours > 24 * 14).sum()} 段超過 14 天,可能是切段太粗,請看 sample_sessions 最後的『最長 session』")

p("\n== 時間合理性(檢查 +09:00 時區是否造成偏移)==")
staff = conv[conv.role == "staff"]
hour_dist = staff.groupby(staff.ts.dt.hour).size()
p("老師訊息各小時分布:" + str(hour_dist.to_dict()))
p("星期分布(0=週一):" + str(staff.groupby(staff.ts.dt.dayofweek).size().to_dict()))
p("→ 你的營業時間是 週二~五 13:00~21:00、週六 09:00~16:30。若大量老師訊息落在 22~23 點或清晨,代表時間可能差 1 小時。")

c = conv.sort_values(["parent_id", "ts"]).copy()
c["next_role"] = c.groupby("parent_id")["role"].shift(-1)
c["next_ts"] = c.groupby("parent_id")["ts"].shift(-1)
d = c[(c.role == "parent") & (c.next_role == "staff")]
delay = (d.next_ts - d.ts).dt.total_seconds() / 60
p(f"家長→老師 回覆間隔(分鐘):中位數 {delay.median():.0f},P90 {delay.quantile(.9):.0f}")

p("\n== 品質檢查 ==")
dup = int(df.duplicated(["parent_id", "ts", "text"]).sum())
tiny = int((df.text.str.len() <= 1).sum())
p(f"完全重複的訊息:{dup};長度 ≤1 的訊息:{tiny}")
warn(dup > len(df) * 0.01, "重複訊息偏多,可能是 CSV 重複匯出")
p(f"老師訊息含網址:{int((df.urls != '').sum())} 則")
for tag in ["[姓名]", "[PHONE]", "[URL]", "[EMAIL]", "[ID]", "[姓]"]:
    p(f"  被替換成 {tag}:{int(df.text.str.contains(re.escape(tag)).sum())} 則")
warn(df.text.str.contains(re.escape("[姓名]")).mean() > 0.2,
     "[姓名] 比例偏高,字典裡可能有太常見的詞(例如暱稱剛好是『謝謝』),請看 sample_sessions 確認語句有沒有被誤替換")

# ============ 2. 選單文字 ============
menu = df[df.msg_type == "menu"]["text"].value_counts().rename_axis("text").reset_index(name="count")
menu["len"] = menu.text.str.len()
menu.to_csv(CHK / "menu_texts.csv", index=False, encoding="utf-8-sig")
long_menu = menu[menu.len > 12]
p(f"\n== 選單點擊 == 共 {len(menu)} 種文字;其中長度>12 的 {len(long_menu)} 種(這些最可能是誤判的真實提問,請看 menu_texts.csv)")

# ============ 3. 疑似漏網人名 ============
# 概念:孩子名字通常只出現在「同一位家長」的對話裡;一般詞彙則出現在很多家長。
FOLLOW = r"(?:媽媽|爸爸|媽咪|爸比|今天|明天|昨天|下週|這週|請假|試上|上課|同學|小朋友|的課|要|有|在|很|好|沒|不|也|會|已經|可以|想|先|再|的)"
RE_CAND = re.compile(r"(?=([\u4e00-\u9fff]{2,3})" + FOLLOW + ")")
doc_parents = defaultdict(set)
count = Counter()
context = {}
for pid, text in zip(df.parent_id, df.text):
    for m in RE_CAND.finditer(text):
        tok = m.group(1)
        doc_parents[tok].add(pid)
        count[(pid, tok)] += 1
        if (pid, tok) not in context:
            s = max(0, m.start() - 8)
            context[(pid, tok)] = text[s:m.start() + len(tok) + 10].replace("\n", " ")
rows = []
for (pid, tok), n in count.items():
    if len(doc_parents[tok]) == 1 and n >= 2:          # 只出現在單一家長且至少出現 2 次
        rows.append({"parent_id": pid, "token": tok, "count": n, "context": context[(pid, tok)], "is_name": ""})
leak = pd.DataFrame(rows).sort_values("count", ascending=False).head(500) if rows else pd.DataFrame(
    columns=["parent_id", "token", "count", "context", "is_name"])
leak_path = CHK / "leak_candidates.csv"
if leak_path.exists():                                   # 不覆蓋已經人工標記過的檔案
    p(f"\n(已存在 {leak_path},為保護你的標記,不覆蓋;若要重新產生請先刪除)")
else:
    leak.to_csv(leak_path, index=False, encoding="utf-8-sig")
    p(f"\n== 疑似漏網人名 == 候選 {len(leak)} 筆 → {leak_path}")

# 殘留的明顯個資格式
RE_DIGITS = re.compile(r"\d{8,}")
RE_AT = re.compile(r"@[A-Za-z0-9_.]{3,}")
RE_TITLE = re.compile(r"(?<!\[姓\])(?<![\u4e00-\u9fff\]])[\u4e00-\u9fff]{2,3}(?:媽媽|爸爸)")
for name, rx in [("8 碼以上數字", RE_DIGITS), ("@帳號", RE_AT), ("『某某媽媽/爸爸』", RE_TITLE)]:
    hits = df[df.text.str.contains(rx)]
    p(f"殘留檢查 {name}:{len(hits)} 則" + (f",例:{hits.text.iloc[0][:40]!r}" if len(hits) else ""))

# ============ 4. 抽樣對話 ============
def render(sid, g):
    out = [f"----- {sid}({len(g)} 則,{g.ts.min():%Y-%m-%d}) -----"]
    for _, r in g.iterrows():
        who = "家長" if r.role == "parent" else f"老師({r.staff_name})"
        out.append(f"[{r.ts:%m-%d %H:%M}] {who}:{r.text}")
    return "\n".join(out)


groups = dict(tuple(conv.groupby("session_id")))
ids = list(groups)
pick = random.sample(ids, min(25, len(ids)))
parent_only = [i for i in ids if groups[i].role.nunique() == 1 and groups[i].role.iloc[0] == "parent"]
pick_po = random.sample(parent_only, min(5, len(parent_only)))
longest = sess.sort_values("n", ascending=False).head(3).index.tolist()
with open(CHK / "sample_sessions.txt", "w", encoding="utf-8") as f:
    f.write("######## A. 隨機 25 段 ########\n\n")
    f.write("\n\n".join(render(i, groups[i]) for i in pick))
    f.write("\n\n######## B. 只有家長發言的 5 段(確認是真的沒回,還是切段切壞)########\n\n")
    f.write("\n\n".join(render(i, groups[i]) for i in pick_po))
    f.write("\n\n######## C. 最長的 3 段(確認沒有不相干的對話被黏在一起)########\n\n")
    f.write("\n\n".join(render(i, groups[i]) for i in longest))

(CHK / "stats.txt").write_text("\n".join(lines), encoding="utf-8")
p(f"\n完成。請依序查看 {CHK}\\ 下的 stats.txt → sample_sessions.txt → menu_texts.csv → leak_candidates.csv")