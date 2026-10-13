"""
Step 3b:對 step3 的結果做「程式端」遮蔽與整理。

為什麼不讓 LLM 自己遮蔽人名:測試中約 1/5 的問答仍殘留孩子真名。
改成「LLM 只負責列出它看到的人名(names),由程式做字串取代」,
並且同一位家長的人名會套用到該家長所有 session(手足的名字也一起遮)。

用法:
  python step3b_finalize.py --test    # 處理 qa_raw_v2_test.jsonl
  python step3b_finalize.py           # 處理 qa_raw_v2.jsonl
輸出(out/):
  qa_clean.jsonl(或 qa_clean_test.jsonl)   最終問答,已遮蔽
  check/names_review.csv                      各家長被遮蔽的名字清單,供抽查
  check/qa_review.txt                         隨機 40 組問答,供人工閱讀
"""
import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from step1_clean import NAME_STOPWORDS, deidentify

OUT = Path("out")
CHK = OUT / "check"
CHK.mkdir(exist_ok=True)

RE_BANK = re.compile(r"((?:帳號|帳戶|戶頭|末[五5]碼|後[五5]碼)\s*[:：]?\s*)\d{3,}")
RE_LONGNUM = re.compile(r"(?<!\d)\d{8,}(?!\d)")
RE_PRICE = re.compile(r"\d[\d,]*\s*元|\$\s*\d|NT\$")
RE_DATE = re.compile(r"\d{1,2}\s*/\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*[日號号]")
EXTRA_STOP = {"老師", "媽媽", "爸爸", "家長", "小朋友", "孩子", "同學", "哥哥", "姐姐", "姊姊", "弟弟", "妹妹",
              "奶奶", "爺爺", "阿嬤", "阿公", "外婆", "外公", "alex"} | NAME_STOPWORDS


def clean_names(names):
    out = set()
    for n in names:
        n = str(n).strip()
        if len(n) < 2 or n.startswith("[") or n.isdigit() or n.lower() in EXTRA_STOP:
            continue
        out.add(n)
    return out


def split_tokens(s):
    return [t for t in re.split(r"[\W_]+", s) if len(t) >= 2 and not t.isdigit() and t.lower() not in EXTRA_STOP]


def mask(text, names):
    for n in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(n), "[姓名]", text, flags=re.I)
    text = RE_BANK.sub(r"\1[已遮蔽]", text)
    text = RE_LONGNUM.sub("[已遮蔽]", text)
    return deidentify(text, [])             # 再套一次電話/Email/網址/身分證


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    src = OUT / ("qa_raw_v2_test.jsonl" if args.test else "qa_raw_v2.jsonl")
    dst = OUT / ("qa_clean_test.jsonl" if args.test else "qa_clean.jsonl")

    recs = [json.loads(l) for l in open(src, encoding="utf-8")]

    # 1) 每位家長的人名集合(LLM 列出的 + 全域老師名字)
    per_parent = defaultdict(Counter)
    for r in recs:
        for n in clean_names(r.get("names", [])):
            per_parent[r["parent_id"]][n] += 1
    staff_tokens = set()
    msg_path = OUT / "messages.csv"
    if msg_path.exists():
        staff_names = pd.read_csv(msg_path, usecols=["role", "staff_name"], dtype=str).query("role == 'staff'")["staff_name"].dropna().unique()
        for sn in staff_names:
            staff_tokens.update(split_tokens(sn))

    rows = [{"parent_id": pid, "name": n, "sessions": c} for pid, cn in per_parent.items() for n, c in cn.items()]
    pd.DataFrame(rows).sort_values(["parent_id", "sessions"], ascending=[True, False]).to_csv(
        CHK / "names_review.csv", index=False, encoding="utf-8-sig")

    # 2) 套用遮蔽、補欄位
    out_rows, n_pairs = [], 0
    for r in recs:
        if not r.get("pairs"):
            continue
        names = set(per_parent.get(r["parent_id"], {}).keys()) | staff_tokens
        for k, p in enumerate(r["pairs"]):
            q, qraw, a = (mask(p[x], names) for x in ("question", "question_raw", "answer"))
            out_rows.append({
                "pair_id": f"{r['session_id']}#{k}", "session_id": r["session_id"], "parent_id": r["parent_id"],
                "date": r["start"][:10],
                "question": q, "question_raw": qraw, "answer": a,
                "category": p["category"], "kind": p["kind"], "sensitive": p["sensitive"],
                "has_price": bool(RE_PRICE.search(a)), "has_date": bool(RE_DATE.search(a)),
                "short_answer": len(a.strip()) <= 6,
            })
            n_pairs += 1
    with open(dst, "w", encoding="utf-8") as f:
        for row in out_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 3) 統計與抽樣
    df = pd.DataFrame(out_rows)
    print(f"輸入 {len(recs)} 段 → 最終 {n_pairs} 組問答 → {dst}")
    if len(df):
        print("kind 分布:", df.kind.value_counts().to_dict())
        print("category 分布:", df.category.value_counts().to_dict())
        print(f"含金額 {int(df.has_price.sum())};含日期 {int(df.has_date.sum())};過短回答 {int(df.short_answer.sum())};敏感 {int(df.sensitive.sum())}")
        random.seed(3)
        pick = random.sample(out_rows, min(40, len(out_rows)))
        with open(CHK / "qa_review.txt", "w", encoding="utf-8") as f:
            for r in pick:
                f.write(f"--- {r['pair_id']} | {r['date']} | {r['kind']} / {r['category']}\n問:{r['question']}\n(家長原話:{r['question_raw'][:120]})\n答:{r['answer']}\n\n")
        print(f"請閱讀 {CHK / 'qa_review.txt'} 與 {CHK / 'names_review.csv'}")


if __name__ == "__main__":
    main()