"""Run the browser acceptance suite against disposable local state only."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="symnote-game-e2e-") as directory:
        os.environ.update({
            "DB_PATH": str(Path(directory) / "owner.db"),
            "SYMNOTE_DATA_DIR": str(Path(directory) / "data"),
            "SYMNOTE_STORAGE_BACKEND": "sqlite",
            "SYMNOTE_TEST_MODE": "1",
            "SYMNOTE_E2E": "1",
            "SYMNOTE_OWNER_EMAIL": "owner@example.com",
            "SYMNOTE_ALLOWED_HOSTS": "127.0.0.1,localhost",
            "SYMNOTE_PUBLIC_ORIGIN": "",
            "SYMNOTE_APP_PASSCODE": "",
            "SYMNOTE_GOOGLE_CLIENT_ID": "",
            "SYMNOTE_GOOGLE_CLIENT_SECRET": "",
            "LLM_PROVIDER": "gemini", "LLM_API_KEY": "", "OPENAI_API_KEY": "",
        })
        from symnote.api.app import create_app
        from symnote.core import candidates, db
        from symnote.core.accounts import authenticate_email, invite_user, user_scope

        db.init_db()
        invite_user("owner@example.com", is_owner=True)
        for width in (390, 1024, 1440):
            for theme in ("light", "dark"):
                email = f"browser-{width}-{theme}@example.com"
                invite_user(email)
                with user_scope(authenticate_email(email, name="そら")):
                    db.insert_task("田中さんへ見積書を送る", tags="田中さんへ見積書を送る", status="today", estimate_minutes=15)
                    db.insert_task("ゼミ資料のスライドを仕上げる", tags="ゼミ資料のスライドを仕上げる", status="today", estimate_minutes=90)
                    db.insert_item(kind="memo", raw_text="読書の習慣をつくる", ai_category="idea")
                    candidates.propose_candidates([candidates.validate_candidate(
                        source="gmail", source_ref="fixture-mail", title="打ち合わせの候補日を返信する",
                        excerpt="来週の候補日を教えてください。",
                    ), candidates.validate_candidate(source="gmail", source_ref="fixture-retry", title="確認用の依頼に返信する")])
        from starlette.responses import FileResponse
        from starlette.routing import Route

        async def axe_resource(_request):
            return FileResponse(ROOT / "web" / "node_modules" / "axe-core" / "axe.min.js")

        app = create_app(static_dir=ROOT / "web" / "dist")
        app.routes.insert(0, Route("/test-assets/axe.js", axe_resource))
        uvicorn.run(app, host="127.0.0.1", port=8898)


if __name__ == "__main__":
    main()
