"""
Step 2:找出「重複出現的老師訊息」= 群發廣告 / 制式模板。
這類訊息不是真正的個人回覆,會污染案例庫,但其中的制式回答可能很適合放進 FAQ。
用法:python step2_audit.py  →  out/template_candidates.csv(人工檢視)
"""
import pandas as pd

MIN_PARENTS = 5   # 同一段文字寄給幾位以上不同家長,就列為候選

df = pd.read_csv("out/messages.csv")
staff = df[df.role == "staff"]

rep = (staff.groupby("text")
       .agg(n_parents=("parent_id", "nunique"), n_total=("text", "size"))
       .query("n_parents >= @MIN_PARENTS")
       .sort_values("n_parents", ascending=False))
rep["preview"] = rep.index.str.slice(0, 80).str.replace("\n", " ")
rep.to_csv("out/template_candidates.csv", encoding="utf-8-sig")
print(f"候選制式訊息 {len(rep)} 則,涵蓋 {rep.n_total.sum()} 筆訊息 / 老師訊息共 {len(staff)} 筆")
print(rep[["n_parents", "preview"]].head(20).to_string())