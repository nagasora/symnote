"""Local project, next-action, and progress review screens."""

from __future__ import annotations

import datetime as dt
from typing import Any

import streamlit as st

from symnote.core import progress
from symnote.core.db import fetch_tasks

GOAL_STATUSES = ("planned", "active", "blocked", "done")
STATUS_LABELS = {
    "planned": "これから",
    "active": "進行中",
    "blocked": "停止中",
    "done": "完了",
    "not_started": "未着手の申告",
    "in_progress": "進行中の申告",
    "completed": "完了の申告",
    "needs_approval": "承認待ちの申告",
}


def _local_time(value: str | None) -> str:
    if not value:
        return "更新なし"
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _project_label(row: dict[str, Any]) -> str:
    scope = (
        "個人"
        if row["scope"] == "personal"
        else f"ワークスペース / {row['workspace_name'] or '未設定'}"
    )
    return f"{row['name']} · {scope} · {row['project_key']}"


def _create_project_form() -> None:
    """Offer one short first-project path for a new local workspace."""
    with st.form("progress_create_project", clear_on_submit=True):
        name = st.text_input("プロジェクト名", placeholder="例：研究発表の準備", max_chars=120)
        scope = st.radio(
            "保存先の区分",
            options=("personal", "workspace"),
            format_func=lambda value: (
                "個人" if value == "personal" else "ワークスペース（表示用ラベル）"
            ),
            horizontal=True,
        )
        workspace_name = st.text_input("ワークスペース名（任意）", placeholder="例：研究チーム")
        owner = st.text_input("担当者（任意）", placeholder="例：自分")
        description = st.text_area("目的（任意）", max_chars=2_000, height=72)
        submitted = st.form_submit_button(
            "プロジェクトを作る", type="primary", use_container_width=True
        )
    if submitted:
        if not name:
            st.error("プロジェクト名を入力してください。")
            return
        try:
            created = progress.create_project(
                name=name,
                scope=scope,
                workspace_name=workspace_name,
                owner=owner,
                description=description,
            )
        except ValueError as exc:
            st.error(str(exc))
            return
        st.session_state["progress_selected_project"] = created["id"]
        st.success(f"「{created['name']}」を作りました。次は目標を1つ追加しましょう。")
        st.rerun()


def render_projects_page() -> None:
    """Create and organize projects → goals → existing or new tasks."""
    st.subheader("プロジェクトから、次の行動まで")
    st.caption(
        "個人 / ワークスペースは端末内の整理ラベルです。"
        "共有認証やマルチテナント境界ではありません。"
    )
    projects = progress.list_projects()
    if not projects:
        st.info("最初のプロジェクトを作り、目標と ToDo を順番に追加します。")
        _create_project_form()
        return

    for row in projects:
        with st.container(border=True):
            st.markdown(f"**{row['name']}**")
            st.caption(
                f"{_project_label(row)} · 目標 {row['goal_count']} · ToDo {row['task_count']}"
            )
            if progress.is_stale(row["last_report_at"]):
                st.warning(
                    "進捗報告なし / 7 日以上更新なし。"
                    "再開するときは現在と次の一手を確認してください。"
                )
            else:
                st.caption(f"最新のエージェント報告: {_local_time(row['last_report_at'])}")
            if row["last_report_outcome"] == "conflict":
                st.error("最新の進捗報告は競合中です。状態は上書きされていません。")

    project_ids = [row["id"] for row in projects]
    selected_id = st.selectbox(
        "開くプロジェクト",
        project_ids,
        index=project_ids.index(st.session_state.get("progress_selected_project"))
        if st.session_state.get("progress_selected_project") in project_ids
        else 0,
        format_func=lambda project_id: _project_label(
            next(row for row in projects if row["id"] == project_id)
        ),
        key="progress_selected_project",
    )
    project = next(row for row in projects if row["id"] == selected_id)
    goals = progress.list_goals(selected_id)

    with st.expander("目標を追加", expanded=not goals):
        with st.form("progress_create_goal", clear_on_submit=True):
            title = st.text_input("目標", placeholder="例：発表資料を提出する", max_chars=200)
            description = st.text_area("補足", max_chars=2_000, height=72)
            status = st.selectbox(
                "状態", GOAL_STATUSES, format_func=lambda value: STATUS_LABELS[value]
            )
            current = st.text_area(
                "現在", placeholder="今どこまで進んでいるか", max_chars=2_000, height=72
            )
            next_action = st.text_input(
                "次の一手", placeholder="例：図を1枚追加する", max_chars=2_000
            )
            owner = st.text_input("担当者", value=project["owner"], max_chars=120)
            blocked = st.text_input("止まっている理由（任意）", max_chars=2_000)
            approval_required = st.checkbox("承認が必要")
            approval_state = st.selectbox(
                "承認状況",
                ("not_required", "pending", "approved", "rejected"),
                format_func=lambda value: {
                    "not_required": "不要",
                    "pending": "確認待ち",
                    "approved": "承認済み",
                    "rejected": "差し戻し",
                }[value],
            )
            submitted = st.form_submit_button(
                "目標を追加", type="primary", use_container_width=True
            )
        if submitted:
            try:
                progress.create_goal(
                    selected_id,
                    title,
                    description,
                    status,
                    current,
                    next_action,
                    owner,
                    blocked,
                    approval_required,
                    approval_state,
                )
            except (ValueError, LookupError) as exc:
                st.error(str(exc))
            else:
                st.success("目標を追加しました。次に ToDo を1つ決めましょう。")
                st.rerun()

    if not goals:
        return

    st.markdown("#### 目標と ToDo")
    for goal in goals:
        with st.container(border=True):
            status_label = STATUS_LABELS.get(goal["status"], goal["status"])
            st.markdown(f"**{goal['title']}** · {status_label}")
            if goal["current_work"]:
                st.text(f"現在：{goal['current_work']}")
            st.text(f"次の一手：{goal['next_action'] or 'まだ決まっていません'}")
            st.caption(
                f"担当: {goal['owner'] or '未設定'} · ToDo {goal['task_count']} · "
                f"版 {goal['revision']}"
            )
            if goal["blocked_reason"]:
                st.warning(f"停止理由: {goal['blocked_reason']}")
            if goal["approval_required"]:
                st.info(f"承認: {goal['approval_state']}")
            if goal["description"]:
                st.text(goal["description"])

            with st.expander("目標・現在・次の一手を更新"):
                with st.form(f"progress_goal_{goal['id']}"):
                    title = st.text_input("目標", value=goal["title"], max_chars=200)
                    description = st.text_area(
                        "補足", value=goal["description"], max_chars=2_000, height=72
                    )
                    status = st.selectbox(
                        "状態",
                        GOAL_STATUSES,
                        index=GOAL_STATUSES.index(goal["status"]),
                        format_func=lambda value: STATUS_LABELS[value],
                    )
                    current = st.text_area(
                        "現在", value=goal["current_work"], max_chars=2_000, height=72
                    )
                    next_action = st.text_input(
                        "次の一手", value=goal["next_action"], max_chars=2_000
                    )
                    owner = st.text_input("担当者", value=goal["owner"], max_chars=120)
                    blocked = st.text_input(
                        "停止理由", value=goal["blocked_reason"], max_chars=2_000
                    )
                    approval_required = st.checkbox(
                        "承認が必要",
                        value=bool(goal["approval_required"]),
                        key=f"goal_approval_required_{goal['id']}",
                    )
                    approval_options = ("not_required", "pending", "approved", "rejected")
                    approval_state = st.selectbox(
                        "承認状況",
                        approval_options,
                        index=approval_options.index(goal["approval_state"]),
                        format_func=lambda value: {
                            "not_required": "不要",
                            "pending": "確認待ち",
                            "approved": "承認済み",
                            "rejected": "差し戻し",
                        }[value],
                    )
                    save = st.form_submit_button(
                        "更新を保存", type="primary", use_container_width=True
                    )
                if save:
                    try:
                        progress.update_goal(
                            goal["id"],
                            goal["revision"],
                            title=title,
                            description=description,
                            status=status,
                            current_work=current,
                            next_action=next_action,
                            owner=owner,
                            blocked_reason=blocked,
                            approval_required=approval_required,
                            approval_state=approval_state,
                        )
                    except (ValueError, LookupError) as exc:
                        st.error(str(exc))
                    else:
                        st.success("目標を更新しました。")
                        st.rerun()

            _render_goal_tasks(selected_id, goal)


def _render_goal_tasks(project_id: int, goal: dict[str, Any]) -> None:
    tasks = progress.list_project_tasks(project_id, goal["id"])
    if not tasks:
        st.caption("この目標に紐づく ToDo はまだありません。")
    activity = progress.list_project_activity(project_id, limit=200)
    latest_task_report = {}
    for event in activity:
        if event["event_type"] == "report" and event["task_id"] is not None:
            latest_task_report.setdefault(event["task_id"], event)
    for task in tasks:
        with st.container(border=True):
            title = task["title"] or task["details"].splitlines()[0]
            st.markdown(f"**{title}** · {task['status']}")
            st.caption(
                f"担当: {task['owner'] or '未設定'} · 期限: {task['due_date'] or 'なし'} · "
                f"版 {task['progress_revision']}"
            )
            event = latest_task_report.get(task["id"])
            if event:
                if event["outcome"] == "conflict":
                    st.error(f"報告の版が競合しています: {event['conflict_reason']}")
                else:
                    st.caption(
                        "エージェント申告: "
                        f"{STATUS_LABELS.get(event['claimed_status'], event['claimed_status'])}"
                        f" · {_local_time(event['received_at'])} · 未検証"
                    )
                    if event["current_work"]:
                        st.text(f"申告された現在: {event['current_work']}")
                    if event["next_action"]:
                        st.text(f"申告された次: {event['next_action']}")
                    if event["approval_required"]:
                        st.info(f"申告された承認: {event['approval_state']}")
    with st.expander(f"「{goal['title']}」に ToDo を追加"):
        with st.form(f"progress_task_{goal['id']}", clear_on_submit=True):
            title = st.text_input("ToDo", placeholder="例：図のキャプションを書く", max_chars=200)
            details = st.text_area("詳細（任意）", max_chars=2_000, height=72)
            owner = st.text_input("担当者（任意）", value=goal["owner"], max_chars=120)
            has_due_date = st.checkbox("期限を設定")
            due_date = st.date_input("期限", value=dt.date.today(), disabled=not has_due_date)
            create = st.form_submit_button("ToDo を追加", type="primary", use_container_width=True)
        if create:
            try:
                progress.create_project_task(
                    project_id,
                    goal["id"],
                    title,
                    details,
                    owner,
                    due_date.isoformat() if has_due_date else None,
                )
            except (ValueError, LookupError) as exc:
                st.error(str(exc))
            else:
                st.success("ToDo を追加しました。")
                st.rerun()

    unlinked = [row for row in fetch_tasks(limit=300) if row.get("progress_project_id") is None]
    if unlinked:
        with st.expander("既存の ToDo をこの目標にまとめる"):
            by_id = {row["id"]: row for row in unlinked}
            with st.form(f"progress_link_task_{goal['id']}"):
                task_id = st.selectbox(
                    "既存 ToDo",
                    list(by_id),
                    format_func=lambda item_id: (
                        by_id[item_id].get("tags") or by_id[item_id]["raw_text"][:80]
                    ),
                )
                owner = st.text_input("担当者（任意）", max_chars=120)
                link = st.form_submit_button("この目標に追加", use_container_width=True)
            if link:
                task = by_id[task_id]
                try:
                    progress.link_task_to_goal(
                        task_id,
                        project_id,
                        goal["id"],
                        task["progress_revision"],
                        owner,
                    )
                except (ValueError, LookupError) as exc:
                    st.error(str(exc))
                else:
                    st.success("既存 ToDo をまとめました。")
                    st.rerun()


def render_today_focus() -> None:
    """Show one concise next action and its freshness before the existing Today list."""
    focus = progress.list_progress_focus()
    st.subheader("次の一手")
    if not focus:
        st.info("プロジェクトの目標はまだありません。『プロジェクト』から最初の1つを作れます。")
        return
    first = focus[0]
    has_current_report = bool(first["last_event_id"] and first["last_report_outcome"] != "conflict")
    action = (
        (first["reported_next"] if has_current_report else "")
        or first["next_action"]
        or "次にやることを決めてください。"
    )
    current = (
        (first["reported_current"] if has_current_report else "")
        or first["current_work"]
        or "現在地はまだ記録されていません。"
    )
    with st.container(border=True):
        owner = (
            (first["reported_owner"] if has_current_report else "") or first["owner"] or "未設定"
        )
        st.caption(f"{first['project_name']} / {first['title']} · 担当 {owner}")
        if first["last_report_outcome"] == "conflict":
            st.error("最新の進捗報告は版の競合で保留中です。レビュー画面で確認してください。")
        elif has_current_report and first["reported_status"]:
            st.caption(
                "エージェント申告: "
                f"{STATUS_LABELS.get(first['reported_status'], first['reported_status'])} · 未検証"
            )
        st.markdown(f"**{action}**")
        st.text(f"現在: {current}")
        if has_current_report and first["reported_next"]:
            st.caption("エージェントの申告 · 未検証")
        blocked_reason = (
            first["reported_blocked_reason"] if has_current_report else first["blocked_reason"]
        )
        if blocked_reason:
            st.warning(f"停止理由: {blocked_reason}")
        approval_required = (
            first["reported_approval_required"]
            if has_current_report
            else first["approval_required"]
        )
        if approval_required:
            approval_state = (
                first["reported_approval_state"] if has_current_report else first["approval_state"]
            )
            st.info(f"承認状況: {approval_state}")
        if progress.is_stale(first["last_report_at"]):
            st.caption(
                "最新のエージェント報告なし、または7日以上経過。作業を再開する前に状況を確認してください。"
            )
        else:
            st.caption(f"最新報告: {_local_time(first['last_report_at'])}")


def render_review_page() -> None:
    """Import inert JSONL reports and review completion claims against their own snapshot."""
    st.subheader("進捗報告と証拠の確認")
    st.caption(
        "取り込んだ報告は claim のままです。ここではコマンドを実行せず、参照先も自動で開きません。"
    )
    uploaded = st.file_uploader(
        "JSON / JSONL の進捗ファイル", type=("json", "jsonl"), key="progress_import_file"
    )
    if uploaded and st.button("進捗報告を取り込む", type="primary", use_container_width=True):
        try:
            raw = uploaded.getvalue().decode("utf-8")
            result = progress.import_progress_events(progress.parse_progress_file(raw))
        except (UnicodeDecodeError, ValueError, LookupError) as exc:
            st.error(str(exc))
        else:
            st.success(
                f"新規 {len(result['imported_event_ids'])} 件 · "
                f"重複 {result['duplicates_skipped']} 件 · "
                f"競合 {len(result['conflicts'])} 件"
            )

    projects = progress.list_projects()
    if projects:
        st.markdown("#### 更新状況")
        for project in projects:
            with st.container(border=True):
                label = _project_label(project)
                if progress.is_stale(project["last_report_at"]):
                    st.warning(f"{label} · 更新なし / 7日以上")
                else:
                    st.caption(f"{label} · 最新報告 {_local_time(project['last_report_at'])}")

    reviews = progress.list_pending_completion_reviews()
    st.markdown("#### 完了申告の確認")
    if not reviews:
        st.info("確認待ちの完了申告はありません。")
    for claim in reviews:
        with st.container(border=True):
            target = claim["task_title"] or claim["goal_title"] or claim["target_kind"]
            st.markdown(f"**{target}** · 完了申告 / 未検証")
            st.text(f"プロジェクト: {claim['project_name']} ({claim['project_key']})")
            st.text(f"現在: {claim['current_work'] or '未記入'}")
            st.text(f"次の一手: {claim['next_action'] or '未記入'}")
            st.text(
                f"コード: {claim['source_repo']} · {claim['source_branch']} · "
                f"{claim['source_commit']}"
            )
            st.text(f"worktree: {claim['source_worktree'] or '未記入'}")
            st.text(f"成果物参照: {claim['artifact_ref'] or '未記入'}")
            st.text(
                f"テスト参照: {claim['test_ref'] or '未記入'} · "
                f"申告結果: {claim['claimed_test_result']}"
            )
            can_verify = bool(
                claim["source_commit"]
                and claim["artifact_ref"]
                and claim["test_ref"]
                and claim["claimed_test_result"] == "passed"
            )
            if not can_verify:
                st.warning(
                    "成果物・テスト根拠・passed の申告がそろうまで verified にはできません。"
                )
            with st.form(f"progress_review_{claim['event_id']}"):
                reviewer = st.text_input("確認者", max_chars=120)
                note = st.text_area("確認メモ", max_chars=1_000, height=72)
                checked_artifact = st.checkbox("この申告の成果物参照を確認した")
                checked_test = st.checkbox("この申告と同じ commit のテスト根拠を確認した")
                submit = st.form_submit_button(
                    "この申告を verified として記録",
                    type="primary",
                    disabled=not can_verify,
                    use_container_width=True,
                )
            if submit:
                try:
                    progress.review_completion_claim(
                        claim["event_id"],
                        reviewer,
                        note,
                        artifact_checked=checked_artifact,
                        test_checked=checked_test,
                    )
                except (ValueError, LookupError) as exc:
                    st.error(str(exc))
                else:
                    st.success("同じ報告・commit・根拠を参照するレビュー記録を追加しました。")
                    st.rerun()

    activity = progress.list_project_activity(limit=100)
    conflicts = [row for row in activity if row["outcome"] == "conflict"]
    st.markdown("#### 版の競合")
    if not conflicts:
        st.caption("競合はありません。")
    for row in conflicts:
        with st.container(border=True):
            target = row["task_title"] or row["goal_title"] or row["target_kind"]
            st.error(f"{row['project_name']} · {target}")
            st.text(row["conflict_reason"])
            st.text(f"報告 commit: {row['source_commit']} · {row['received_at']}")

    st.markdown("#### 追記履歴")
    if not activity:
        st.caption("活動履歴はありません。")
    for row in activity[:30]:
        with st.container(border=True):
            target = row["task_title"] or row["goal_title"] or row["project_name"]
            label = (
                "レビュー"
                if row["event_type"] == "review"
                else ("進捗申告" if row["event_type"] == "report" else "手動変更")
            )
            outcome = row["review_outcome"] or row["outcome"]
            st.text(f"{label}: {target} · {outcome} · {_local_time(row['received_at'])}")
            if row["event_type"] == "report":
                st.caption(
                    f"{row['source_repo']} · {row['source_branch']} · {row['source_commit']}"
                )
                if row["review_outcome"] == "verified":
                    st.caption(f"レビュー: {row['reviewer']} · {row['review_note']}")

    st.markdown("#### JSONL エクスポート")
    if projects:
        export_choice = st.selectbox(
            "対象",
            [None, *[row["id"] for row in projects]],
            format_func=lambda value: (
                "すべて"
                if value is None
                else next(row["name"] for row in projects if row["id"] == value)
            ),
        )
        exported = progress.export_progress_jsonl(export_choice)
        st.download_button(
            "活動ログをダウンロード",
            exported,
            file_name="symnote-progress.jsonl",
            mime="application/x-ndjson",
            use_container_width=True,
        )
    st.caption(
        "個人/ワークスペース区分はローカルの整理用です。ユーザー認証や共有アクセス制御はありません。"
    )
