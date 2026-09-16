"""
研究会日程（東大 海洋技術環境学専攻）監視スクリプト
- 毎朝1回実行し、ページのテーブルを構造化して前回スナップショットと比較
- 「今日以降」の日程のみを通知対象とする（過去分の修正は無視）
- 変更を検知したら LINE グループへプッシュ通知
"""

import os
import re
import json
import time
import pathlib
import requests
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ config
PAGE  = "https://mee.k.u-tokyo.ac.jp/wp/seminar/"
STATE = pathlib.Path("state/seminar.json")

UA = {
    "User-Agent": "seminar-watcher/1.0 (personal schedule checker)",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

JST = timezone(timedelta(hours=9))

# 0 = 今日以降のみ通知 / 3 = 直近3日前の訂正も拾う / 30 = 過去1ヶ月まで
KEEP_PAST_DAYS = 0

YEAR_RE   = re.compile(r"(20\d{2})\s*年度")
DATE_RE   = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*([※★]*)\s*$")
COLS      = ["曜日", "時間", "場所", "発表者"]
HEADER_HD = {"日付", "曜日", "時間", "場所", "発表者"}


# ------------------------------------------------------------------ fetch
def fetch_rows() -> dict:
    """研究会日程テーブルを {キー: {列: 値}} に構造化して返す"""
    # ?_=unixtime で W3 Total Cache をバイパス
    r = requests.get(PAGE, params={"_": int(time.time())}, headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    main = soup.select_one(
        ".entry-content, .post-content, article, #main, #content, main"
    ) or soup
    for tag in main.select("script, style, nav, aside, footer, form, .widget"):
        tag.decompose()

    rows, seen = {}, {}
    for table in main.find_all("table"):
        # 直前に出てくる「20XX年度」見出しを年度として採用
        y = table.find_previous(string=YEAR_RE)
        year = YEAR_RE.search(y).group(1) if y else "unknown"

        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True).replace("\xa0", " ")
                     for td in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if cells[0] in HEADER_HD:           # ヘッダ行を除外
                continue

            m = DATE_RE.match(cells[0])
            if not m:                           # 日付でない行は無視
                continue
            mm, dd, marks = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)

            # 同一年度内の日付重複（例: 2025年度の 02/18 と 02/18★）に対応
            base = f"{year}-{mm}/{dd}"
            seen[base] = seen.get(base, 0) + 1
            key = base if seen[base] == 1 else f"{base}#{seen[base]}"

            rec = {"印": marks}
            for i, name in enumerate(COLS, start=1):
                rec[name] = cells[i] if i < len(cells) else ""
            rows[key] = rec

    return rows


# ------------------------------------------------------------------ filter
def key_to_date(key: str):
    """'2026-10/14#2' → date(2026, 10, 14)。判定不能なら None"""
    m = re.match(r"^(\d{4})-(\d{2})/(\d{2})", key)
    if not m:
        return None                                  # year が unknown 等
    nendo, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year = nendo if mm >= 4 else nendo + 1           # 年度 → 西暦（4月始まり）
    try:
        return datetime(year, mm, dd).date()
    except ValueError:
        return None


def prune(rows: dict) -> dict:
    """過去分を除外。日付判定できないものは安全側で残す"""
    cutoff = datetime.now(JST).date() - timedelta(days=KEEP_PAST_DAYS)
    out = {}
    for k, v in rows.items():
        d = key_to_date(k)
        if d is None or d >= cutoff:
            out[k] = v
    return out


# ------------------------------------------------------------------ diff
def compare(old: dict, new: dict):
    added   = {k: v for k, v in new.items() if k not in old}
    removed = {k: v for k, v in old.items() if k not in new}
    changed = {k: (old[k], new[k]) for k in new
               if k in old and old[k] != new[k]}
    return added, removed, changed


def fmt(key: str, rec: dict) -> str:
    date = key.split("#")[0]
    mark = rec.get("印", "")
    body = " / ".join(f"{c}:{rec[c]}" for c in COLS if rec.get(c))
    return f"{date}{mark}  {body}"


def fmt_changed(key: str, o: dict, n: dict) -> str:
    date  = key.split("#")[0]
    lines = [f"{date}{n.get('印', '')}"]
    for c in ["印"] + COLS:
        if o.get(c, "") != n.get(c, ""):
            lines.append(f"  {c}: 「{o.get(c, '') or '(空)'}」→「{n.get(c, '') or '(空)'}」")
    return "\n".join(lines)


# ------------------------------------------------------------------ notify
def push_line(text: str):
    """LINE Messaging API で個人チャットにプッシュ送信"""
    token   = os.environ["LINE_CHANNEL_TOKEN"]
    to_id   = os.environ["LINE_USER_ID"]        # ← LINE_GROUP_ID から変更

    # 1メッセージ5000文字上限 → 4500で分割、最大5通まで
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 4500:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf)
    chunks = chunks[:5]

    res = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"to": to_id,
              "messages": [{"type": "text", "text": c.strip()} for c in chunks]},
        timeout=20,
    )
    if res.status_code != 200:
        raise RuntimeError(f"LINE push failed: {res.status_code} {res.text}")


# ------------------------------------------------------------------ main
def main():
    raw = fetch_rows()

    # 安全ガード：取得失敗を「全件削除」と誤検知させない（フィルタ前の件数で判定）
    if len(raw) < 10:
        raise SystemExit(f"ERROR: 取得件数が異常 ({len(raw)}件)。状態は更新しません")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    prev_raw = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else None

    # 初回：ベースライン保存のみ（通知なし）
    if prev_raw is None:
        STATE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"initialized: 全{len(raw)}件を保存（監視対象 {len(prune(raw))}件 / 通知なし）")
        return

    # ★ 新旧の両方に同じフィルタを適用 → 過去へ流れた分を「削除」と誤検知しない
    cur, prev = prune(raw), prune(prev_raw)

    added, removed, changed = compare(prev, cur)

    if not (added or removed or changed):
        # 過去分の修正にも追従できるよう、スナップショットは常に最新化
        STATE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"no change (監視対象 {len(cur)}件 / 全{len(raw)}件)")
        return

    msg = ["📅 研究会日程が更新されました", PAGE, ""]
    if added:
        msg.append("🆕【新規追加】")
        msg += [fmt(k, v) for k, v in sorted(added.items())][:20]
        msg.append("")
    if changed:
        msg.append("✏️【変更】")
        for k, (o, n) in sorted(changed.items())[:20]:
            msg.append(fmt_changed(k, o, n))
        msg.append("")
    if removed:
        msg.append("🗑【中止・削除】")
        msg += [fmt(k, v) for k, v in sorted(removed.items())][:20]

    push_line("\n".join(msg))
    STATE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"notified: +{len(added)} ~{len(changed)} -{len(removed)}")


if __name__ == "__main__":
    main()