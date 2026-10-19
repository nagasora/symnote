"""実行モード（本番・テスト環境）と画面のパスコードに関する設定を読む。"""

from __future__ import annotations

import hmac
import ipaddress
import os
import threading
import time
from typing import Any, Dict, List, Mapping, Optional

MAX_PASSCODE_FAILURES = 5
PASSCODE_WINDOW_SECONDS = 15 * 60
GLOBAL_MAX_PASSCODE_FAILURES = 30
MAX_TRACKED_PASSCODE_SOURCES = 1000


def is_test_mode() -> bool:
    """SYMNOTE_TEST_MODE が有効なら True（外部サービスへの書き込みを止める環境）。"""
    return os.getenv("SYMNOTE_TEST_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def app_passcode() -> str:
    """画面を開くのに必要なパスコード。未設定なら空文字（ロックなし）。"""
    return os.getenv("SYMNOTE_APP_PASSCODE", "").strip()


def passcode_matches(entered: str, expected: str) -> bool:
    """入力されたパスコードが一致するかを、時間差の出ない比較で判定する。"""
    if not expected:
        return True
    return hmac.compare_digest(entered.strip().encode(), expected.encode())


def is_public_bind(address: Optional[str]) -> bool:
    """待ち受けアドレスを明示して端末の外に公開しているなら True。

    未指定（開発時の既定）と、SYMNOTE_ALLOW_NO_PASSCODE=1 で外側の公開範囲を別途絞っている
    場合（例: Docker で 127.0.0.1 にだけ公開）は False。
    """
    if os.getenv("SYMNOTE_ALLOW_NO_PASSCODE", "").strip() == "1":
        return False
    host = (address or "").strip().strip("[]")
    if host in {"", "localhost"}:
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


class PasscodeLimiter:
    """パスコードの失敗回数を送信元ごとに数え、総当たりを一定時間止める（プロセス全体で共有）。

    送信元ごとの上限で本人以外の連続失敗が本人を締め出さないようにし、送信元を偽っても
    総当たりできないよう全体の上限も併せて持つ。
    """

    def __init__(
        self,
        max_failures: int = MAX_PASSCODE_FAILURES,
        window: float = PASSCODE_WINDOW_SECONDS,
        global_max_failures: int = GLOBAL_MAX_PASSCODE_FAILURES,
    ) -> None:
        self.max_failures = max_failures
        self.window = window
        self.global_max_failures = global_max_failures
        self._failures: Dict[str, List[float]] = {}
        self._global: List[float] = []
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        self._global = [t for t in self._global if t > cutoff]
        self._failures = {
            key: kept for key, times in self._failures.items() if (kept := [t for t in times if t > cutoff])
        }

    def _wait(self, times: List[float], limit: int, now: float) -> float:
        if len(times) < limit:
            return 0.0
        return max(0.0, times[-limit] + self.window - now)

    def _locked_for_unlocked(self, source: str, now: float) -> float:
        self._prune(now)
        return max(
            self._wait(self._failures.get(source, []), self.max_failures, now),
            self._wait(self._global, self.global_max_failures, now),
        )

    def _record_unlocked(self, source: str, now: float) -> None:
        if source not in self._failures and len(self._failures) >= MAX_TRACKED_PASSCODE_SOURCES:
            oldest = min(self._failures, key=lambda key: self._failures[key][-1])
            del self._failures[oldest]
        self._failures.setdefault(source, []).append(now)
        self._global.append(now)

    def locked_for(self, now: Optional[float] = None, source: str = "") -> float:
        """送信元か全体がロック中なら残り秒数、そうでなければ 0。"""
        now = time.time() if now is None else now
        with self._lock:
            return self._locked_for_unlocked(source, now)

    def record_failure(self, now: Optional[float] = None, source: str = "") -> None:
        """失敗を 1 回記録する。"""
        now = time.time() if now is None else now
        with self._lock:
            self._prune(now)
            self._record_unlocked(source, now)

    def reset(self) -> None:
        """失敗の記録をすべて消す。"""
        with self._lock:
            self._failures.clear()
            self._global.clear()

    def attempt(self, entered: str, expected: str, now: Optional[float] = None, source: str = "") -> bool:
        """ロック中でなければ照合し、結果を記録する。ロック中は照合せず False。

        確認・照合・記録を 1 つのロックの中で行う。別々に呼ぶと、複数のセッションから同時に
        送られた入力がどれもロック前の確認を通り、上限を超えて試せてしまう。
        成功時に消すのはその送信元の記録だけで、全体の記録は他の送信元の失敗なので残す。
        """
        now = time.time() if now is None else now
        with self._lock:
            if self._locked_for_unlocked(source, now):
                return False
            if passcode_matches(entered, expected):
                self._failures.pop(source, None)
                return True
            self._record_unlocked(source, now)
            return False


def passcode_source(headers: Mapping[str, str]) -> str:
    """パスコード試行の送信元を表すキー。Cloudflare 経由なら利用者の IP、それ以外は "direct"。

    CF-Connecting-IP は Cloudflare が利用者の送った値を上書きするので、トンネル経由では偽れない。
    LAN から直接つないだ場合は偽れるが、その時は全体の上限が総当たりを止める。
    """
    for name, value in headers.items():
        if name.lower() == "cf-connecting-ip" and value.strip():
            return value.strip()[:64]
    return "direct"


PASSCODE_LIMITER = PasscodeLimiter()


class CalendarWriteBlocked(RuntimeError):
    """テスト環境で Google カレンダーへ書き込もうとしたことを表す。"""


class _ReadOnlyEvents:
    """events() のうち読み取りだけを通し、書き込みを拒否する。"""

    _BLOCKED = {"insert", "update", "patch", "delete", "move", "quickAdd", "import_"}

    def __init__(self, events: Any) -> None:
        self._events = events

    def __getattr__(self, name: str) -> Any:
        if name in self._BLOCKED:
            raise CalendarWriteBlocked("テスト環境のため、Google カレンダーへは書き込みません。")
        return getattr(self._events, name)


class ReadOnlyCalendarService:
    """本番カレンダーを読み取り専用で見せるサービスのラッパー。"""

    def __init__(self, service: Any) -> None:
        self._service = service

    def events(self) -> _ReadOnlyEvents:
        """読み取り専用の events リソースを返す。"""
        return _ReadOnlyEvents(self._service.events())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)
