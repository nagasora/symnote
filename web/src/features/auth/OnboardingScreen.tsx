import { useState } from "react";
import { apiFetch } from "../../api/client";
import type { AuthState } from "../../api/types";
import { BuddyBubble } from "../../components/buddy/BuddyBubble";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";

export function OnboardingScreen({
  auth,
  onLogin,
}: {
  auth: AuthState;
  onLogin: (auth: AuthState) => void;
}) {
  const [name, setName] = useState(auth.user?.name ?? "");
  return (
    <main className="auth-game">
      <Card className="auth-game-card">
        <BuddyBubble line="はじめまして。小さな一歩を一緒に見つけよう。" />
        <h1>あなたの冒険を準備しました</h1>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            const result = await apiFetch<{ user: AuthState["user"] }>(
              "/api/auth/onboarding",
              { body: { name: name || "ユーザー", seed_sample_tasks: true } },
            );
            onLogin({ ...auth, user: result.user });
          }}
        >
          <label>
            呼び名
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <Button variant="sun">冒険をはじめる</Button>
        </form>
      </Card>
    </main>
  );
}
