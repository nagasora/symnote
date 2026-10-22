import { useEffect, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  ClipboardList,
  Map,
  Plus,
  ScrollText,
  UserRound,
  Flame,
  Coins,
} from "lucide-react";
import { useGame } from "../../features/game/GameContext";
import { CaptureSheet } from "../capture/CaptureSheet";
import { ClearCelebration } from "../game/ClearCelebration";
import { ProgressBar } from "../ui/ProgressBar";
import { Toast } from "../ui/Toast";
import { QuestDetail } from "./QuestDetail";
import { TaskBreakdownSheet } from "../quest/TaskBreakdownSheet";
import { Buddy } from "../buddy/Buddy";
import { FocusTimer } from "../game/FocusTimer";

const nav = [
  { to: "/today", label: "きょう", icon: ClipboardList },
  { to: "/map", label: "マップ", icon: Map },
  { to: "/board", label: "依頼", icon: ScrollText },
  { to: "/me", label: "じぶん", icon: UserRound },
];

export function AppShell({ children }: { children: ReactNode }) {
  const { player, today, setCaptureOpen, toast } = useGame();
  const location = useLocation();
  const requestCount = today?.inbox_badge_count ?? 0;
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [location.pathname]);
  const title =
    nav.find((item) => location.pathname.startsWith(item.to))?.label ??
    "SymNote";
  return (
    <div className="game-app">
      <aside className="game-sidebar">
        <NavLink
          className="game-brand"
          to="/today"
          aria-label="SymNote きょうへ"
        >
          <Buddy size={40} />
          <span>
            SymNote<small>ひだまりの冒険</small>
          </span>
        </NavLink>
        <button
          type="button"
          className="sidebar-capture"
          aria-label="ミナに渡す"
          onClick={() => setCaptureOpen(true)}
        >
          <Plus size={21} />
          <span>ミナに渡す</span>
          <kbd>⌘K</kbd>
        </button>
        <nav aria-label="メインナビゲーション">
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} aria-label={label}>
              <Icon />
              <span>{label}</span>
              {to === "/board" && requestCount > 0 && (
                <span
                  className="nav-badge num"
                  aria-label={`${requestCount}件の依頼`}
                >
                  {requestCount}
                </span>
              )}
            </NavLink>
          ))}
        </nav>
        {player && (
          <div className="sidebar-player">
            <div>
              <strong className="num">Lv {player.level}</strong>
              <span className="num">
                {player.xp_into_level}/{player.xp_for_next} XP
              </span>
            </div>
            <ProgressBar
              label="次のレベルまでの経験値"
              value={player.xp_into_level}
              max={player.xp_for_next}
            />
            <p>
              <span>
                <Flame size={14} />
                {player.streak_days}日
              </span>
              <span>
                <Coins size={14} />
                {player.gold} G
              </span>
            </p>
          </div>
        )}
      </aside>
      <header className="mobile-game-header">
        <strong>{title}</strong>
        <div>
          {player && (
            <>
              <span>
                <Flame size={16} />
                {player.streak_days}
              </span>
              <span>
                <Coins size={16} />
                {player.gold}
              </span>
            </>
          )}
        </div>
      </header>
      <main className="game-main">{children}</main>
      <nav className="game-tabbar" aria-label="メインナビゲーション">
        <NavLink to="/today">
          <ClipboardList />
          <span>きょう</span>
        </NavLink>
        <NavLink to="/map">
          <Map />
          <span>マップ</span>
        </NavLink>
        <button
          type="button"
          className="tab-capture"
          aria-label="ミナに渡す"
          onClick={() => setCaptureOpen(true)}
        >
          <Plus />
        </button>
        <NavLink to="/board">
          <ScrollText />
          <span>依頼</span>
          {requestCount > 0 && (
            <span
              className="nav-badge num"
              aria-label={`${requestCount}件の依頼`}
            >
              {requestCount}
            </span>
          )}
        </NavLink>
        <NavLink to="/me">
          <UserRound />
          <span>じぶん</span>
        </NavLink>
      </nav>
      <CaptureSheet />
      <QuestDetail />
      <TaskBreakdownSheet />
      <FocusTimer />
      <ClearCelebration />
      <Toast
        message={toast?.message ?? ""}
        actionLabel={toast?.actionLabel}
        onAction={toast?.action}
      />
    </div>
  );
}
