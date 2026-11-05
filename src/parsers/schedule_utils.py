"""D項スケジュール判定ユーティリティ

NOTAM の D項 (schedule) 文字列を解析し、指定 UTC 時刻範囲と重なるかを判定する。

対応パターン（実データから確認）:
  1. "1430/2100"                          — 時刻範囲のみ (毎日)
  2. "2300/1200"                          — 日またぎ時刻範囲 (毎日)
  3. "01-03 05 08-10 1430/2100"           — 日付リスト(範囲/個別) + 時刻範囲
  4. "28 2350/0205 29 1045/1300"          — (日 時刻範囲)+ の繰り返し
  5. "AUG 19 26 SEP 02 09 1430/2030"     — 月名 + 日付リスト + 時刻範囲
  6. "22 SEP 12 1500/2000"               — "DD MON DD 時刻" (開始日〜終了日 月名入り)

aixm:Timesheet ベースのスケジュール判定:
  - AIXM の <aixm:Timesheet> 要素から直接解析する is_schedule_active_from_timesheets() を提供する。
  - day には曜日コード (MON/TUE/.../SUN) または "ANY" を指定できる。
  - startTime / endTime は "HH:MM" 形式 (UTC)。
  - excluded="YES" の Timesheet は除外扱い。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

UTC = timezone.utc

# AIXM 曜日コード → weekday() 値 (月曜=0)
_AIXM_DAY_MAP: dict[str, int] = {
    'MON': 0, 'TUE': 1, 'WED': 2, 'THU': 3, 'FRI': 4, 'SAT': 5, 'SUN': 6,
}


@dataclass
class Timesheet:
    """aixm:Timesheet 要素を表すデータクラス。

    Attributes:
        day:        曜日コード ('MON'〜'SUN') または 'ANY' (毎日) または 'WORK_DAY' など。
                    None の場合も毎日扱い。
        start_hh:   開始時 (UTC, 0〜23)
        start_mm:   開始分 (0〜59)
        end_hh:     終了時 (UTC, 0〜23)
        end_mm:     終了分 (0〜59)
        excluded:   True なら除外 (NOT ACTIVE) な時間帯を表す
    """
    day: Optional[str]
    start_hh: int
    start_mm: int
    end_hh: int
    end_mm: int
    excluded: bool = False


def is_schedule_active_from_timesheets(
    timesheets: list[Timesheet],
    sel_start_utc: datetime,
    sel_end_utc: datetime,
) -> bool:
    """aixm:Timesheet リストを使って、選択時間枠 [sel_start_utc, sel_end_utc] と
    重なるアクティブ期間があれば True を返す。

    - excluded=False の Timesheet が 1 件でも重なれば True。
    - timesheets が空の場合は True (= 常にアクティブとみなす)。
    - 選択枠は最大 24h 想定。日をまたぐ場合は各日の曜日で判定する。
    """
    if not timesheets:
        return True

    # 選択枠内の全日付について曜日と分単位時刻を対照する
    # 選択枠が 24h 以上の場合は全曜日が含まれるので簡易的に全曜日 True にする
    sel_sm = sel_start_utc.hour * 60 + sel_start_utc.minute
    sel_em = sel_end_utc.hour * 60 + sel_end_utc.minute

    # 選択枠内の日付セットと、各日付の weekday を収集
    sel_weekdays: set[int] = set()
    cur = sel_start_utc
    while cur.date() <= sel_end_utc.date():
        sel_weekdays.add(cur.weekday())
        cur += timedelta(days=1)

    for ts in timesheets:
        if ts.excluded:
            continue  # 除外時間帯はスキップ

        # 曜日フィルター
        day_upper = (ts.day or 'ANY').upper()
        if day_upper not in ('ANY', 'DAILY'):
            weekday = _AIXM_DAY_MAP.get(day_upper)
            if weekday is None:
                # 未知の曜日コード → 安全側で通過
                pass
            elif weekday not in sel_weekdays:
                continue  # この曜日は選択枠に含まれない

        # 時刻範囲チェック
        if _time_range_overlaps(ts.start_hh, ts.start_mm, ts.end_hh, ts.end_mm, sel_sm, sel_em):
            return True

        # 日またぎ時刻範囲 (例: 2300/0200) の場合、翌日基準でも確認
        if sel_end_utc.date() > sel_start_utc.date():
            if _time_range_overlaps(ts.start_hh, ts.start_mm, ts.end_hh, ts.end_mm, 0, sel_em):
                return True

    return False

MONTH_MAP = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
}

# 時刻範囲 HHMM/HHMM
_TIME_RE = re.compile(r'\b(\d{4})/(\d{4})\b')

# 月名トークン
_MON_RE = re.compile(r'\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b')

# 日付範囲/個別 (1〜31)
_DAY_RANGE_RE = re.compile(r'\b(\d{1,2})-(\d{1,2})\b')
_DAY_RE = re.compile(r'\b(\d{1,2})\b')


def _parse_hhmm(s: str) -> tuple[int, int]:
    """'HHMM' → (hour, minute)"""
    return int(s[:2]), int(s[2:])


def _time_range_overlaps(
    t_start_h: int, t_start_m: int,
    t_end_h: int, t_end_m: int,
    sel_start_min: int,
    sel_end_min: int,
) -> bool:
    """1日の時刻範囲（分単位、0〜1440）と選択範囲が重なるか判定。
    日またぎ (t_end < t_start) も対応。
    sel_* は UTC 0:00 起点の分数。
    """
    s = t_start_h * 60 + t_start_m
    e = t_end_h * 60 + t_end_m

    # 時刻範囲を正規化（日またぎの場合は2つの範囲に分割）
    if s <= e:
        # 通常時刻範囲: [s, e)
        time_ranges = [(s, e)]
    else:
        # 日またぎ時刻範囲: [s, 1440) と [0, e)
        time_ranges = [(s, 1440), (0, e)]

    # 選択時間範囲を正規化（日またぎの場合は2つの範囲に分割）
    if sel_start_min <= sel_end_min:
        # 通常選択範囲: [sel_start_min, sel_end_min)
        sel_ranges = [(sel_start_min, sel_end_min)]
    else:
        # 日またぎ選択範囲: [sel_start_min, 1440) と [0, sel_end_min)
        sel_ranges = [(sel_start_min, 1440), (0, sel_end_min)]

    # すべての組み合わせで重なりをチェック
    for ts, te in time_ranges:
        for ss, se in sel_ranges:
            # 2つの範囲 [ts, te) と [ss, se) が重なるか
            if ts < se and ss < te:
                return True

    return False


def _extract_time_ranges(tokens: list[str]) -> list[tuple[int, int, int, int]]:
    """トークン列から 時刻範囲リスト [(sh,sm,eh,em), ...] を取り出す。"""
    ranges = []
    for t in tokens:
        m = _TIME_RE.fullmatch(t)
        if m:
            sh, sm = _parse_hhmm(m.group(1))
            eh, em = _parse_hhmm(m.group(2))
            ranges.append((sh, sm, eh, em))
    return ranges


# ─────────────────────────────────────────────
# パターン判定メイン
# ─────────────────────────────────────────────

def is_schedule_active(
    schedule: str,
    effective_start: datetime,
    effective_end: datetime,
    sel_start_utc: datetime,
    sel_end_utc: datetime,
) -> bool:
    """D項スケジュールを解析し、選択時間枠 [sel_start, sel_end] (UTC aware) と
    重なるアクティブ期間があれば True を返す。

    schedule が空 / 解析不能の場合は True (= 常にアクティブとみなす)。
    """
    if not schedule or not schedule.strip():
        return True  # D項なし → 全期間有効

    raw = schedule.strip().upper()
    tokens = raw.split()

    # ── 時刻範囲トークンを探す ──────────────────────────────
    time_tokens = [t for t in tokens if _TIME_RE.fullmatch(t)]
    other_tokens = [t for t in tokens if not _TIME_RE.fullmatch(t)]

    if not time_tokens:
        # 時刻範囲が見当たらない → 解析不能、安全側で True
        return True

    # ── パターン4: "28 2350/0205 29 1045/1300" ─────────────
    # other_tokens に数字のみ、time_tokens と数が等しい or 交互に並ぶ
    # → (日, 時刻範囲) のペアとして解析
    if len(other_tokens) > 0 and _looks_like_day_time_pairs(tokens):
        return _check_day_time_pairs(tokens, effective_start, sel_start_utc, sel_end_utc)

    # ── 時刻範囲（共通） ──────────────────────────────────
    # 最後の時刻範囲トークンを使う（複数ある場合は全 OR）
    time_ranges = _extract_time_ranges(time_tokens)

    # ── パターン1/2: 時刻範囲のみ (other_tokens が空) ─────
    if not other_tokens:
        return _check_daily_time_ranges(time_ranges, sel_start_utc, sel_end_utc)

    # ── 月名なしパターン3: 日付(リスト/範囲) + 時刻範囲 ──
    has_month = bool(_MON_RE.search(raw))

    if not has_month:
        days = _parse_day_list(other_tokens)
        if days:
            return _check_day_list(days, time_ranges, effective_start, sel_start_utc, sel_end_utc)
        # 解析不能
        return True

    # ── 月名ありパターン5/6 ───────────────────────────────
    return _check_month_day_pattern(raw, time_ranges, effective_start, sel_start_utc, sel_end_utc)


# ─────────────────────────────────────────────
# ヘルパー
# ─────────────────────────────────────────────

def _check_daily_time_ranges(
    time_ranges: list[tuple[int, int, int, int]],
    sel_start: datetime,
    sel_end: datetime,
) -> bool:
    """毎日同じ時刻範囲が有効なパターン (パターン1/2)。
    UTC 1日を最大2日分チェック。
    """
    sel_sm = sel_start.hour * 60 + sel_start.minute
    sel_em = sel_end.hour * 60 + sel_end.minute
    # 日またぎ考慮: sel が深夜をまたぐ場合も片側で判定
    for sh, sm, eh, em in time_ranges:
        if _time_range_overlaps(sh, sm, eh, em, sel_sm, sel_em):
            return True
        # 翌日基準にずらして再チェック (日またぎsel対応)
        if sel_end.date() > sel_start.date():
            if _time_range_overlaps(sh, sm, eh, em, 0, sel_em):
                return True
    return False


def _check_day_list(
    days: list[int],
    time_ranges: list[tuple[int, int, int, int]],
    effective_start: datetime,
    sel_start: datetime,
    sel_end: datetime,
) -> bool:
    """特定日付リスト + 時刻範囲が有効なパターン (パターン3)。"""
    # effective_start の月/年を基準に日付を解釈する
    base_year = effective_start.year
    base_month = effective_start.month

    sel_sm = sel_start.hour * 60 + sel_start.minute
    sel_em = sel_end.hour * 60 + sel_end.minute
    # 選択範囲の日付セット（日またぎ対応で前後1日も含める）
    sel_date_set: set = set()
    cur = sel_start.date()
    end_d = sel_end.date()
    while cur <= end_d:
        sel_date_set.add(cur)
        cur += timedelta(days=1)

    for day in days:
        for sh, sm, eh, em in time_ranges:
            try:
                cand_date = datetime(base_year, base_month, day, tzinfo=UTC).date()
                # 日またぎ時刻範囲 (sh*60+sm > eh*60+em) は翌日も候補
                is_overnight = (sh * 60 + sm) > (eh * 60 + em)
                check_dates = [cand_date]
                if is_overnight:
                    check_dates.append(cand_date + timedelta(days=1))
                for cd in check_dates:
                    if cd in sel_date_set:
                        if _time_range_overlaps(sh, sm, eh, em, sel_sm, sel_em):
                            return True
            except ValueError:
                continue
    return False


def _parse_day_list(tokens: list[str]) -> list[int]:
    """'01-03 05 08' のようなトークン列から日付リストを返す。"""
    days = []
    for t in tokens:
        m = _DAY_RANGE_RE.fullmatch(t)
        if m:
            start_d, end_d = int(m.group(1)), int(m.group(2))
            days.extend(range(start_d, end_d + 1))
            continue
        m2 = _DAY_RE.fullmatch(t)
        if m2:
            days.append(int(m2.group(1)))
    return days


def _looks_like_day_time_pairs(tokens: list[str]) -> bool:
    """'28 2350/0205 29 1045/1300' のような (日,時刻範囲)ペアパターンか判定。"""
    # 単数字とTIME_REが交互に並ぶパターン
    for i in range(0, len(tokens) - 1, 2):
        if not _DAY_RE.fullmatch(tokens[i]):
            return False
        if not _TIME_RE.fullmatch(tokens[i + 1]):
            return False
    return len(tokens) >= 2 and len(tokens) % 2 == 0


def _check_day_time_pairs(
    tokens: list[str],
    effective_start: datetime,
    sel_start: datetime,
    sel_end: datetime,
) -> bool:
    """パターン4: (日 時刻範囲)ペアの繰り返し。"""
    base_year = effective_start.year
    base_month = effective_start.month
    sel_date_set = _build_sel_date_set(sel_start, sel_end)
    sel_sm = sel_start.hour * 60 + sel_start.minute
    sel_em = sel_end.hour * 60 + sel_end.minute

    for i in range(0, len(tokens) - 1, 2):
        day = int(tokens[i])
        tm = _TIME_RE.fullmatch(tokens[i + 1])
        if not tm:
            continue
        sh, sm = _parse_hhmm(tm.group(1))
        eh, em = _parse_hhmm(tm.group(2))
        try:
            cand_date = datetime(base_year, base_month, day, tzinfo=UTC).date()
            is_overnight = (sh * 60 + sm) > (eh * 60 + em)
            check_dates = [cand_date]
            if is_overnight:
                check_dates.append(cand_date + timedelta(days=1))
            for cd in check_dates:
                if cd in sel_date_set:
                    if _time_range_overlaps(sh, sm, eh, em, sel_sm, sel_em):
                        return True
        except ValueError:
            continue
    return False


def _check_month_day_pattern(
    raw: str,
    time_ranges: list[tuple[int, int, int, int]],
    effective_start: datetime,
    sel_start: datetime,
    sel_end: datetime,
) -> bool:
    """月名入りパターン5/6: 月名と日付から候補日を列挙して判定。"""
    sel_date_set = _build_sel_date_set(sel_start, sel_end)
    sel_sm = sel_start.hour * 60 + sel_start.minute
    sel_em = sel_end.hour * 60 + sel_end.minute

    # "AUG 19 26 SEP 02 09 1430/2030" → {AUG: [19,26], SEP: [02,09]}
    # まず月名トークンの位置を特定してグループ化
    tokens = [t for t in raw.split() if not _TIME_RE.fullmatch(t)]
    month_groups: list[tuple[int, list[int]]] = []  # [(month_num, [days]), ...]
    cur_month: Optional[int] = None
    cur_days: list[int] = []

    for t in tokens:
        mm = _MON_RE.fullmatch(t)
        if mm:
            if cur_month is not None:
                month_groups.append((cur_month, cur_days))
            cur_month = MONTH_MAP[mm.group(1)]
            cur_days = []
        else:
            dr = _DAY_RANGE_RE.fullmatch(t)
            if dr:
                cur_days.extend(range(int(dr.group(1)), int(dr.group(2)) + 1))
            else:
                dr2 = _DAY_RE.fullmatch(t)
                if dr2:
                    cur_days.append(int(dr2.group(1)))
    if cur_month is not None:
        month_groups.append((cur_month, cur_days))

    if not month_groups:
        return True  # 解析不能 → 安全側

    base_year = effective_start.year
    for month_num, days in month_groups:
        for day in days:
            for sh, sm, eh, em in time_ranges:
                try:
                    # 年またぎ考慮: effective_start の年 or 翌年
                    for yr in (base_year, base_year + 1):
                        cand_date = datetime(yr, month_num, day, tzinfo=UTC).date()
                        is_overnight = (sh * 60 + sm) > (eh * 60 + em)
                        check_dates = [cand_date]
                        if is_overnight:
                            check_dates.append(cand_date + timedelta(days=1))
                        for cd in check_dates:
                            if cd in sel_date_set:
                                if _time_range_overlaps(sh, sm, eh, em, sel_sm, sel_em):
                                    return True
                except ValueError:
                    continue
    return False


def _build_sel_date_set(sel_start: datetime, sel_end: datetime) -> set:
    """選択範囲 [sel_start, sel_end] に含まれる全日付のセットを返す。"""
    result = set()
    cur = sel_start.date()
    end_d = sel_end.date()
    while cur <= end_d:
        result.add(cur)
        cur += timedelta(days=1)
    return result
