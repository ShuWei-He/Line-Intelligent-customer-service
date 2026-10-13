"""
Step 3 (v2):用本機 Ollama 從每段 session「指認」問答配對。

v2 的核心改變:LLM 只負責「指認」(回傳哪幾個編號是家長的問題、哪幾個編號是老師的回答),
不再負責「改寫老師的回答」。回答原文由程式直接從老師的訊息取出,
並由程式驗證角色(問題必須來自家長、回答必須來自老師、且在問題之後)。
這樣可以根絕:編造回答、問答角色互換、把老師公告當成提問。

用法:
  python step3_extract.py --limit 50     # 隨機抽 50 段試跑 → out/qa_raw_v2_test.jsonl
  python step3_extract.py                # 全量(由新到舊,可 Ctrl+C,下次自動接續)→ out/qa_raw_v2.jsonl

排除名單(可選):out/exclude_parents.txt,每行一個 parent_id(例如員工的內部聊天室),會被略過。
"""
import argparse
import json
import random
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

# ========== 設定 ==========
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3.5:9b"
NUM_CTX = 4096
MAX_CHARS = 1800
NUM_PREDICT = 600               # v2 輸出只有編號和短句,比 v1 短很多
TIMEOUT = 600
IN_FILE = Path("out/sessions.jsonl")
EXCLUDE_FILE = Path("out/exclude_parents.txt")
# =========================

CATS = ["price", "course_info", "trial_class", "enrollment_process", "schedule_makeup", "leave",
        "location_hours", "camp_event", "payment_invoice", "child_situation", "complaint_refund", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "q_turns": {"type": "array", "items": {"type": "integer"}},
                    "a_turns": {"type": "array", "items": {"type": "integer"}},
                    "question": {"type": "string"},
                    "category": {"type": "string", "enum": CATS},
                    "kind": {"type": "string", "enum": ["faq", "personal", "event"]},
                    "sensitive": {"type": "boolean"},
                },
                "required": ["q_turns", "a_turns", "question", "category", "kind", "sensitive"],
            },
        },
        "names": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["pairs", "names"],
}

SYSTEM = f"""你是補習班(機器人教育)LINE 客服對話的標註員。對話每個段落開頭有編號,例如「#3 [家長] ...」。
你的任務是找出「家長提出問題或請求,老師給出回應」的配對,只輸出編號,不要複製或改寫老師的回答。

規則:
1. q_turns 只能填 [家長] 的編號;a_turns 只能填 [老師] 的編號,而且老師的回應必須出現在家長提問之後。
2. 老師主動發送的公告、作業說明、比賽或營隊通知、提醒,如果沒有家長對應的提問,不要輸出。家長只回「好」「謝謝」不算提問。
3. question:把家長的問題或請求改寫成獨立、完整、不依賴上下文的一句話。只能依據家長說的內容,不可加入老師回答裡的資訊,也不可自己發明問題。
4. names:對話中出現的所有人名與暱稱(孩子、家長、親戚、老師、同學),照原文逐一列出;沒有就給空陣列。
5. kind:
   - "faq":老師的回答是通用規則、價格、流程、課程介紹,不依賴這個孩子的個別情況,且不限特定日期。
   - "personal":依賴個別孩子、特定時段名額、補課或請假安排、個別協調。
   - "event":限時活動(比賽、營隊、集訓、特定日期的活動)的資訊與費用。
6. sensitive:涉及退費、投訴、孩子的行為或健康狀況、折扣例外時為 true,否則 false。
7. category 只能從這些選一個:{", ".join(CATS)}。
沒有符合的配對就回傳 {{"pairs": [], "names": [...]}}。只輸出 JSON。"""

TRIVIAL = re.compile(r"^[\s\W]*(好的?|好喔|好哦|ok|OK|okay|收到|了解|謝謝|感謝|多謝|謝謝您|謝謝老師|好的謝謝|沒問題|嗯嗯?|哈哈|👍|🙏|\[貼圖\]|\[圖片\])+[\s\W]*$", re.I)


def worth_processing(sess):
    roles = {t["role"] for t in sess["turns"]}
    if roles != {"parent", "staff"}:
        return False, "no_reply_or_no_question"
    parent_texts = [t["text"] for t in sess["turns"] if t["role"] == "parent"]
    meaningful = [x for x in parent_texts if len(x.strip()) >= 4 and not TRIVIAL.match(x.strip())]
    if not meaningful:
        return False, "trivial_parent"
    return True, ""


def render(sess):
    lines, total = [], 0
    for i, t in enumerate(sess["turns"], 1):
        who = "家長" if t["role"] == "parent" else "老師"
        line = f"#{i} [{who}] {t['text']}"
        total += len(line)
        if total > MAX_CHARS:
            lines.append("(後略)")
            break
        lines.append(line)
    return "\n".join(lines)


def validate(pairs, turns):
    """程式端驗證:角色、順序、編號範圍。回傳 (通過的配對, 被拒原因計數)。回答原文由程式取自老師訊息。"""
    ok, rej = [], Counter()
    for p in pairs:
        q, a = p.get("q_turns") or [], p.get("a_turns") or []
        if not q or not a:
            rej["empty_index"] += 1
            continue
        if any((not isinstance(i, int)) or i < 1 or i > len(turns) for i in q + a):
            rej["bad_index"] += 1
            continue
        if any(turns[i - 1]["role"] != "parent" for i in q):
            rej["q_not_parent"] += 1
            continue
        if any(turns[i - 1]["role"] != "staff" for i in a):
            rej["a_not_staff"] += 1
            continue
        if min(a) < min(q):
            rej["answer_before_question"] += 1
            continue
        question = re.sub(r"\[(家長|老師)\]", "", p.get("question", "")).strip()
        if len(question) < 4:
            rej["bad_question"] += 1
            continue
        qs, as_ = sorted(set(q)), sorted(set(a))
        ok.append({
            "question": question,
            "question_raw": "\n".join(turns[i - 1]["text"] for i in qs),
            "answer": "\n".join(turns[i - 1]["text"] for i in as_),
            "q_turns": qs, "a_turns": as_,
            "category": p.get("category", "other"),
            "kind": p.get("kind", "personal"),
            "sensitive": bool(p.get("sensitive", False)),
        })
    return ok, rej


def call_ollama(user_prompt):
    body = {
        "model": MODEL, "stream": False, "think": False, "format": SCHEMA,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt}],
        "options": {"temperature": 0.1, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
    }
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"]


def extract(sess):
    prompt = f"對話日期:{sess['start'][:10]}\n\n{render(sess)}"
    last_err = ""
    for _ in range(2):
        try:
            data = json.loads(call_ollama(prompt))
            pairs, rej = validate(data.get("pairs", []), sess["turns"])
            return pairs, data.get("names", []), rej, ""
        except Exception as e:                # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
    return None, [], Counter(), last_err


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    exclude = set()
    if EXCLUDE_FILE.exists():
        exclude = {l.split()[0] for l in EXCLUDE_FILE.read_text(encoding="utf-8").splitlines()
                   if l.strip() and not l.startswith("#")}
    sessions = [json.loads(l) for l in open(IN_FILE, encoding="utf-8")]
    todo, skipped = [], Counter()
    for s in sessions:
        if s["parent_id"] in exclude:
            skipped["excluded_parent"] += 1
            continue
        ok, why = worth_processing(s)
        if ok:
            todo.append(s)
        else:
            skipped[why] += 1
    total_eligible = len(todo)
    print(f"總 session {len(sessions)};需交給 LLM:{total_eligible};規則略過:{dict(skipped)}")

    if args.limit:
        random.seed(7)
        todo = random.sample(todo, min(args.limit, len(todo)))
        out_path = Path("out/qa_raw_v2_test.jsonl")
    else:
        todo.sort(key=lambda s: s["start"], reverse=True)
        out_path = Path("out/qa_raw_v2.jsonl")

    if out_path.exists():
        done = {json.loads(l)["session_id"] for l in open(out_path, encoding="utf-8")}
        todo = [s for s in todo if s["session_id"] not in done]
        print(f"已完成 {len(done)} 段,接續處理剩餘 {len(todo)} 段")

    n_ok = n_err = n_pairs = 0
    rej_total = Counter()
    t0 = time.time()
    with open(out_path, "a", encoding="utf-8") as fout:
        for i, s in enumerate(todo, 1):
            t1 = time.time()
            pairs, names, rej, err = extract(s)
            fout.write(json.dumps({
                "session_id": s["session_id"], "parent_id": s["parent_id"], "start": s["start"],
                "pairs": pairs, "names": names, "rejected": dict(rej), "error": err,
                "sec": round(time.time() - t1, 1)}, ensure_ascii=False) + "\n")
            fout.flush()
            rej_total.update(rej)
            if pairs is None:
                n_err += 1
            else:
                n_ok += 1
                n_pairs += len(pairs)
            if i % 5 == 0 or i == len(todo):
                avg = (time.time() - t0) / i
                print(f"[{i}/{len(todo)}] 平均 {avg:.1f}s/段;成功 {n_ok} 失敗 {n_err};通過驗證 {n_pairs} 組;"
                      f"被程式拒絕 {sum(rej_total.values())} 組;預估剩餘 {(len(todo) - i) * avg / 3600:.1f} 小時")

    print(f"\n完成。輸出:{out_path}")
    print(f"被拒絕的原因統計:{dict(rej_total)}")
    if args.limit and todo:
        avg = (time.time() - t0) / len(todo)
        print(f"→ 試跑平均 {avg:.1f}s/段;全量 {total_eligible} 段預估 {total_eligible * avg / 3600:.1f} 小時")
    print("下一步:python step3b_finalize.py(補遮蔽人名、產生可檢視的清單)")


if __name__ == "__main__":
    main()