"""テスト環境用に本番 DB の複製とパスコードを用意し、接続先を表示する。

本番 DB は読み取りだけで、オンライン backup API で一貫した複製を作る（本番を止めずに済む）。
"""

from __future__ import annotations

import argparse
import re
import secrets
import socket
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional


def copy_database(source: Path, target: Path) -> None:
    """source を読み取り専用で開き、target に一貫した複製を書き出す。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    partial.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(partial)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    partial.chmod(0o600)
    partial.replace(target)


def ensure_passcode(path: Path) -> str:
    """保存済みのパスコードを返す。無ければ 6 桁の数字を作って保存する。"""
    if path.is_file() and path.read_text().strip():
        return path.read_text().strip()
    code = f"{secrets.randbelow(10**6):06d}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code + "\n")
    path.chmod(0o600)
    return code


def lan_address() -> Optional[str]:
    """同じ Wi-Fi のスマホから届くこの Mac の IP アドレス。見つからなければ None。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP の connect は送信しないため、経路の決定だけに使える。
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


TUNNEL_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def parse_tunnel_url(log_text: str) -> Optional[str]:
    """cloudflared のログから公開された HTTPS の URL を取り出す。無ければ None。"""
    found = TUNNEL_URL_PATTERN.findall(log_text)
    return found[-1] if found else None


def tunnel_is_registered(log_text: str) -> bool:
    """cloudflared のログから、トンネルが今も Cloudflare に登録されているかを判定する。

    最後の「登録成功」より後に「Tunnel not found」（Cloudflare 側で消された）が無ければ True。
    """
    registered = log_text.rfind("Registered tunnel connection")
    return registered >= 0 and log_text.find("Tunnel not found", registered) < 0


def print_qr(url: str) -> None:
    """segno があれば URL の QR コードを端末に表示する。"""
    try:
        import segno
    except ImportError:
        return
    segno.make(url, error="m").terminal(compact=True)


def main(argv: Optional[List[str]] = None) -> int:
    """引数を解釈して DB 複製・パスコード用意・接続先表示を行う。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--passcode-file", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--reset", action="store_true", help="本番から複製し直す")
    parser.add_argument("--show-only", action="store_true", help="接続先の表示だけ行う")
    parser.add_argument("--remote-url", help="外出先（モバイル回線）から開く公開 URL")
    args = parser.parse_args(argv)

    if not args.show_only:
        if args.source.resolve() == args.target.resolve():
            print("テスト用 DB の場所が本番 DB と同じです。中止します。", file=sys.stderr)
            return 1
        if args.reset or not args.target.exists():
            if not args.source.is_file():
                print(f"本番 DB が見つかりません: {args.source}", file=sys.stderr)
                return 1
            copy_database(args.source, args.target)
            print(f"本番 DB を複製しました → {args.target}")
        else:
            print(f"前回のテスト用 DB を使います（複製し直すには --reset）→ {args.target}")
    code = ensure_passcode(args.passcode_file)
    print(f"\nパソコン: http://localhost:{args.port}")
    address = lan_address()
    if args.remote_url:
        print(f"スマホ:   {args.remote_url}  （モバイル回線・外出先からも開ける）")
        print_qr(args.remote_url)
    elif address:
        phone_url = f"http://{address}:{args.port}"
        print(f"スマホ:   {phone_url}  （同じ Wi-Fi に接続して開く）")
        print_qr(phone_url)
    else:
        print("スマホ:   Wi-Fi の IP アドレスが見つかりません。Mac を Wi-Fi に接続してください。")
    print(f"パスコード: {code}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
