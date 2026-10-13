import argparse
import csv
import io
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

FIELDS = ["傳送者類型", "傳送者名稱", "傳送日期", "傳送時間", "內容"]


def decode_file(raw):
    """依序嘗試常見 CSV 編碼。"""
    for encoding in ("utf-8-sig", "cp950", "big5", "utf-16"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError("無法辨識檔案編碼")


def parse_date(value):
    value = (value or "").strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def read_messages(raw):
    """略過檔案前面的帳號資訊，找到真正的表頭。"""
    text = decode_file(raw)
    lines = text.splitlines()

    header_index = None
    for i, line in enumerate(lines):
        if all(field in line for field in FIELDS):
            header_index = i
            break

    if header_index is None:
        raise ValueError("找不到訊息欄位表頭")

    header = lines[header_index]
    delimiter = "\t" if header.count("\t") > header.count(",") else ","

    reader = csv.DictReader(
        io.StringIO("\n".join(lines[header_index:]), newline=""),
        delimiter=delimiter,
    )

    for row in reader:
        if not row:
            continue

        # 正規化欄位名稱及文字
        row = {
            str(k).strip().lstrip("\ufeff"): (v or "").strip()
            for k, v in row.items()
            if k is not None
        }

        sender_type = row.get("傳送者類型", "").strip()
        sent_date = parse_date(row.get("傳送日期", ""))

        if sender_type not in ("Account", "User") or not sent_date:
            continue

        yield sender_type, sent_date


def iter_csv_files(source):
    """支援 ZIP 或已解壓縮的資料夾。"""
    source = Path(source)

    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".csv"):
                    try:
                        yield name, zf.read(name)
                    except Exception as e:
                        print(f"[讀取失敗] {name}: {e}")
        return

    if source.is_dir():
        for path in source.rglob("*"):
            if path.is_file() and path.suffix.lower() == ".csv":
                try:
                    yield str(path.relative_to(source)), path.read_bytes()
                except Exception as e:
                    print(f"[讀取失敗] {path}: {e}")
        return

    raise FileNotFoundError(f"找不到 ZIP 或資料夾：{source}")


def main():
    parser = argparse.ArgumentParser(
        description="批次統計 LINE 官方帳號每月訊息量"
    )
    parser.add_argument("source", help="ZIP 壓縮檔或 CSV 資料夾")
    parser.add_argument(
        "--months", type=int, default=12,
        help="統計月份數，預設 12"
    )
    parser.add_argument(
        "--include-current", action="store_true",
        help="將本月也納入統計；預設只統計完整月份"
    )
    parser.add_argument(
        "--out", default="line_report",
        help="報表輸出資料夾，預設 line_report"
    )
    args = parser.parse_args()

    if args.months < 1:
        parser.error("--months 必須大於 0")

    # 前一年
    # today = date.today()
    # end_year, end_month = today.year, today.month

    # if not args.include_current:
    #     end_month -= 1
    #     if end_month == 0:
    #         end_year -= 1
    #         end_month = 12

    # def month_index(y, m):
    #     return y * 12 + m - 1

    # end_idx = month_index(end_year, end_month)
    # start_idx = end_idx - args.months + 1

    # months = []
    # for idx in range(start_idx, end_idx + 1):
    #     y, m = divmod(idx, 12)
    #     months.append(f"{y:04d}-{m + 1:02d}")

    # allowed_months = set(months)

    all_months = set()

    for filename, raw in iter_csv_files(args.source):
        try:
            for sender_type, sent_date in read_messages(raw):
                all_months.add(sent_date.strftime("%Y-%m"))
        except Exception as e:
            print(f"[解析失敗] {filename}: {e}")

    if not all_months:
        raise SystemExit("沒有找到有效的訊息日期，請檢查 CSV 格式。")

    start_year, start_month = map(int, min(all_months).split("-"))
    end_year, end_month = map(int, max(all_months).split("-"))

    def month_index(y, m):
        return y * 12 + m - 1

    start_idx = month_index(start_year, start_month)
    end_idx = month_index(end_year, end_month)

    months = []
    for idx in range(start_idx, end_idx + 1):
        y, m = divmod(idx, 12)
        months.append(f"{y:04d}-{m + 1:02d}")

    allowed_months = set(months)

    # 月統計
    stats = defaultdict(lambda: {
        "account_messages": 0,
        "user_messages": 0,
        "account_files": set(),
        "user_files": set(),
    })

    file_rows = []
    errors = []
    total_files = 0
    parsed_files = 0

    for filename, raw in iter_csv_files(args.source):
        total_files += 1
        per_file = defaultdict(lambda: [0, 0])

        try:
            for sender_type, sent_date in read_messages(raw):
                month = sent_date.strftime("%Y-%m")

                # 僅統計指定的月份範圍
                if month not in allowed_months:
                    continue

                s = stats[month]

                if sender_type == "Account":
                    s["account_messages"] += 1
                    s["account_files"].add(filename)
                    per_file[month][0] += 1
                else:
                    s["user_messages"] += 1
                    s["user_files"].add(filename)
                    per_file[month][1] += 1

            parsed_files += 1

            for month, counts in per_file.items():
                file_rows.append({
                    "月份": month,
                    "CSV檔案": filename,
                    "官方帳號訊息數": counts[0],
                    "家長訊息數": counts[1],
                })

        except Exception as e:
            errors.append({
                "CSV檔案": filename,
                "錯誤": str(e),
            })
            print(f"[解析失敗] {filename}: {e}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 每月總表：即使某月沒有訊息也會保留該月份
    monthly_path = out_dir / "monthly_message_stats.csv"
    with monthly_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "月份",
            "官方帳號送出訊息數",
            "家長送出訊息數",
            "訊息總數",
            "有官方訊息的對話檔數",
            "有家長訊息的對話檔數",
        ])
        writer.writeheader()

        for month in months:
            s = stats[month]
            writer.writerow({
                "月份": month,
                "官方帳號送出訊息數": s["account_messages"],
                "家長送出訊息數": s["user_messages"],
                "訊息總數": s["account_messages"] + s["user_messages"],
                "有官方訊息的對話檔數": len(s["account_files"]),
                "有家長訊息的對話檔數": len(s["user_files"]),
            })

    # 每份 CSV、每月的明細
    detail_path = out_dir / "monthly_by_file.csv"
    with detail_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "月份", "CSV檔案", "官方帳號訊息數", "家長訊息數"
        ])
        writer.writeheader()
        writer.writerows(sorted(
            file_rows, key=lambda r: (r["月份"], r["CSV檔案"])
        ))

    # 解析失敗清單
    error_path = out_dir / "parse_errors.csv"
    with error_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["CSV檔案", "錯誤"])
        writer.writeheader()
        writer.writerows(errors)

    print("\n===== LINE 每月訊息統計完成 =====")
    print(f"讀取檔案：{total_files}")
    print(f"成功解析：{parsed_files}")
    print(f"解析失敗：{len(errors)}")
    print(f"統計期間：{months[0]} ～ {months[-1]}")
    print(f"官方帳號訊息總數：{sum(s['account_messages'] for s in stats.values()):,}")
    print(f"家長訊息總數：{sum(s['user_messages'] for s in stats.values()):,}")
    print(f"\n報表位置：{out_dir.resolve()}")
    print(f"月報：{monthly_path.name}")
    print(f"明細：{detail_path.name}")
    print(f"錯誤清單：{error_path.name}")


if __name__ == "__main__":
    main()