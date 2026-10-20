"""日本語ルールベース クイック入力パーサーの単体テスト。"""

from __future__ import annotations

import datetime as dt
import pytest

from symnote.core.quick_parse import parse_quick_input

BASE_DATE = dt.date(2026, 4, 20)  # 月曜日


@pytest.mark.parametrize(
    ("raw", "expected_title", "expected_date", "expected_time"),
    [
        ("明日 15時 歯医者予約", "歯医者予約", "2026-04-21", "15:00"),
        ("今日 9:30 朝会資料を送る", "朝会資料を送る", "2026-04-20", "09:30"),
        ("明後日 午後3時30分 税理士へ電話", "税理士へ電話", "2026-04-22", "15:30"),
        ("来週 企画書ドラフト提出", "企画書ドラフト提出", "2026-04-27", None),
        ("金曜 17時 請求書を発行する", "請求書を発行する", "2026-04-24", "17:00"),
        ("来週水曜 10時半 定例ミーティング準備", "定例ミーティング準備", "2026-04-29", "10:30"),
        ("5/10 母の日ギフト手配", "母の日ギフト手配", "2026-05-10", None),
        ("5月15日 14:00 契約書更新", "契約書更新", "2026-05-15", "14:00"),
        ("2026-06-01 ドメイン更新確認", "ドメイン更新確認", "2026-06-01", None),
        ("3日後 フォローアップ連絡", "フォローアップ連絡", "2026-04-23", None),
        ("2週間後 進捗レビュー", "進捗レビュー", "2026-05-04", None),
        ("夜9時 ストレッチする", "ストレッチする", "2026-04-20", "21:00"),
        ("正午 ランチ予約確認", "ランチ予約確認", "2026-04-20", "12:00"),
        ("朝8時 ゴミ出し", "ゴミ出し", "2026-04-20", "08:00"),
        ("夕方6時半 牛乳を買う", "牛乳を買う", "2026-04-20", "18:30"),
    ],
)
def test_parse_quick_input_dates_and_times(
    raw: str,
    expected_title: str,
    expected_date: str | None,
    expected_time: str | None,
) -> None:
    res = parse_quick_input(raw, today=BASE_DATE)
    assert res.kind == "task"
    assert res.clean_title == expected_title
    assert res.due_date == expected_date
    assert res.due_time == expected_time


def test_parse_quick_input_recurrence_rules() -> None:
    r1 = parse_quick_input("毎週月・木 9:00 ゴミ出し", today=BASE_DATE)
    assert r1.recurrence is not None
    assert r1.recurrence.frequency == "weekly"
    assert r1.recurrence.weekdays == (0, 3)
    assert r1.due_time == "09:00"
    assert r1.clean_title == "ゴミ出し"

    r2 = parse_quick_input("毎平日 8:30 メールチェック", today=BASE_DATE)
    assert r2.recurrence is not None
    assert r2.recurrence.frequency == "weekdays"
    assert r2.due_time == "08:30"
    assert r2.clean_title == "メールチェック"

    r3 = parse_quick_input("毎月25日 家賃振込", today=BASE_DATE)
    assert r3.recurrence is not None
    assert r3.recurrence.frequency == "monthly"
    assert r3.recurrence.month_day == 25
    assert r3.due_date == "2026-04-25"
    assert r3.clean_title == "家賃振込"

    r4 = parse_quick_input("隔週金曜 16時 1on1準備", today=BASE_DATE)
    assert r4.recurrence is not None
    assert r4.recurrence.frequency == "weekly"
    assert r4.recurrence.interval == 2
    assert r4.recurrence.weekdays == (4,)
    assert r4.due_time == "16:00"
    assert r4.clean_title == "1on1準備"


def test_parse_quick_input_detects_memo_for_multiline_or_note_prefix() -> None:
    m1 = parse_quick_input("メモ: 次回リリースのアイデア整理", today=BASE_DATE)
    assert m1.kind == "memo"
    assert m1.clean_title == "次回リリースのアイデア整理"

    m2 = parse_quick_input("会議メモ\n・A案で進める\n・来週までに試作", today=BASE_DATE)
    assert m2.kind == "memo"
