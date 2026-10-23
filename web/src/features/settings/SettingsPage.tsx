import { SourceIntegrations } from "./SourceIntegrations";
import { useEffect, useState } from "react";
import {
  ArrowLeft,
  Database,
  Download,
  LogOut,
  Moon,
  Plug,
  Save,
  Sun,
  UserRound,
  Users,
} from "lucide-react";
import { Link } from "react-router-dom";
import { Share } from "@capacitor/share";
import { apiFetch } from "../../api/client";
import type { BuddySettings } from "../../api/types";
import { useAuth } from "../auth/AuthContext";
import { useGame } from "../game/GameContext";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { TrashPanel } from "../../components/TrashPanel";
import { applyTheme, type Theme } from "../../lib/theme";
import { isNativeApp, nativeSignOut } from "../../nativeClient";

interface SettingsData {
  ai_usage: { today_used: number; daily_limit: number; remaining: number };
  calendar_connected: boolean;
  owner_features_enabled: boolean;
  invites?: Array<{
    email: string;
    is_owner: boolean;
    note: string;
    accepted_at: string | null;
  }>;
}

export function SettingsPage() {
  const { auth, setAuth } = useAuth();
  const { player, refresh, showToast } = useGame();
  const [theme, setTheme] = useState<Theme>(() =>
    document.documentElement.dataset.theme === "dark" ? "dark" : "light",
  );
  const [data, setData] = useState<SettingsData | null>(null);
  const [buddy, setBuddy] = useState<BuddySettings>({
    name: "ミナ",
    tone: "やさしい",
    user_name: auth.user?.name ?? "",
    sound_enabled: false,
  });
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");
  const load = async () =>
    setData(await apiFetch<SettingsData>("/api/settings"));
  useEffect(() => {
    void load();
  }, []);
  useEffect(() => {
    if (player?.buddy) setBuddy(player.buddy);
  }, [player]);
  function chooseTheme(value: Theme) {
    setTheme(value);
    applyTheme(value);
  }
  async function saveBuddy() {
    await apiFetch("/api/settings/buddy", { method: "PATCH", body: buddy });
    await refresh();
    showToast("相棒の設定を保存しました");
  }
  return (
    <section className="game-screen settings-screen">
      <header className="game-page-head">
        <div>
          <Link to="/me" className="back-link">
            <ArrowLeft />
            じぶんへ
          </Link>
          <h1>設定</h1>
        </div>
      </header>
      <section className="settings-group">
        <h2>
          <UserRound />
          表示と相棒
        </h2>
        <Card>
          <div className="settings-row">
            <div>
              <strong>外観テーマ</strong>
              <span>ひだまりの昼と、夜のキャンプ</span>
            </div>
            <div className="theme-buttons">
              <Button
                variant={theme === "light" ? "sun" : "ghost"}
                onClick={() => chooseTheme("light")}
              >
                <Sun size={18} />
                ライト
              </Button>
              <Button
                variant={theme === "dark" ? "sun" : "ghost"}
                onClick={() => chooseTheme("dark")}
              >
                <Moon size={18} />
                ダーク
              </Button>
            </div>
          </div>
          <div className="buddy-settings">
            <label>
              相棒の名前
              <input
                value={buddy.name}
                onChange={(event) =>
                  setBuddy({ ...buddy, name: event.target.value })
                }
              />
            </label>
            <label>
              あなたの呼び名
              <input
                value={buddy.user_name ?? ""}
                onChange={(event) =>
                  setBuddy({ ...buddy, user_name: event.target.value })
                }
              />
            </label>
            <label>
              口調
              <select
                value={buddy.tone}
                onChange={(event) =>
                  setBuddy({ ...buddy, tone: event.target.value })
                }
              >
                <option>やさしい</option>
                <option>元気</option>
                <option>クール</option>
                <option>執事</option>
              </select>
            </label>
            <label className="sound-toggle">
              <input
                type="checkbox"
                checked={buddy.sound_enabled}
                onChange={(event) =>
                  setBuddy({ ...buddy, sound_enabled: event.target.checked })
                }
              />
              クリア音を鳴らす
            </label>
            <Button variant="sun" onClick={() => void saveBuddy()}>
              <Save size={17} />
              保存する
            </Button>
          </div>
        </Card>
      </section>
      <section className="settings-group">
        <h2>
          <Plug />
          連携
        </h2>
        <Card>
          <div className="settings-row">
            <div>
              <strong>Google カレンダー</strong>
              <span>
                {data?.calendar_connected
                  ? "接続済み。予定をきょうの冒険に表示します。"
                  : "未接続です。既存の連携画面から設定できます。"}
              </span>
            </div>
            <span
              className={`connection-dot ${data?.calendar_connected ? "connected" : ""}`}
            />
          </div>
          {data && (
            <div className="settings-row">
              <div>
                <strong>提案機能の利用枠</strong>
                <span>
                  本日 {data.ai_usage.today_used} / {data.ai_usage.daily_limit}{" "}
                  回（残り {data.ai_usage.remaining} 回）
                </span>
              </div>
            </div>
          )}
        </Card>
      </section>
      <SourceIntegrations />
      {auth.user?.is_owner && (
        <section className="settings-group">
          <h2>
            <Users />
            招待
          </h2>
          <Card>
            <form
              className="invite-form"
              onSubmit={async (event) => {
                event.preventDefault();
                if (!email.trim()) return;
                await apiFetch("/api/settings/invites", {
                  body: { email, note },
                });
                setEmail("");
                setNote("");
                await load();
                showToast("招待リストに追加しました");
              }}
            >
              <input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="Google メールアドレス"
              />
              <input
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="メモ"
              />
              <Button variant="sun">招待する</Button>
            </form>
            {data?.invites?.map((invite) => (
              <div className="settings-row" key={invite.email}>
                <div>
                  <strong>{invite.email}</strong>
                  <span>
                    {invite.note || "メモなし"}・
                    {invite.accepted_at ? "参加済み" : "未ログイン"}
                  </span>
                </div>
                {!invite.is_owner && (
                  <Button
                    variant="quiet"
                    onClick={async () => {
                      await apiFetch(
                        `/api/settings/invites/${encodeURIComponent(invite.email)}`,
                        { method: "DELETE" },
                      );
                      await load();
                    }}
                  >
                    取り消す
                  </Button>
                )}
              </div>
            ))}
          </Card>
        </section>
      )}
      <section className="settings-group">
        <h2>
          <Database />
          データ
        </h2>
        <Card>
          <div className="settings-row">
            <div>
              <strong>バックアップを書き出す</strong>
              <span>クエスト・メモ・マップをJSON形式で保存します。</span>
            </div>
            {isNativeApp() ? (
              <Button
                onClick={async () => {
                  const backup = await apiFetch<unknown>("/api/export");
                  await Share.share({
                    title: "SymNoteのバックアップ",
                    text: JSON.stringify(backup, null, 2),
                  });
                }}
              >
                <Download size={17} />
                共有する
              </Button>
            ) : (
              <a className="game-button ghost md" href="/api/export" download>
                <Download size={17} />
                書き出す
              </a>
            )}
          </div>
        </Card>
        <TrashPanel onRestored={() => void refresh()} />
      </section>
      <section className="settings-group">
        <h2>アカウント</h2>
        <Card>
          <div className="settings-row">
            <div>
              <strong>{auth.user?.name}</strong>
              <span>{auth.user?.email}</span>
            </div>
            <Button
              onClick={async () => {
                if (isNativeApp()) await nativeSignOut();
                else {
                  await apiFetch("/api/auth/logout", { body: {} });
                  setAuth(await apiFetch("/api/auth/me"));
                }
              }}
            >
              <LogOut size={17} />
              ログアウト
            </Button>
          </div>
        </Card>
      </section>
    </section>
  );
}
