import { useState } from "react";
import { LogIn, RefreshCcw } from "lucide-react";
import { apiFetch } from "../../api/client";
import type { AuthState } from "../../api/types";
import { Buddy } from "../../components/buddy/Buddy";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { isNativeApp, nativeSignIn } from "../../nativeClient";

export function LoadingScreen({
  error,
  retry,
}: {
  error?: string;
  retry: () => void;
}) {
  return (
    <main className="auth-game">
      <Card className="auth-game-card">
        <Buddy size={84} expression={error ? "sleepy" : "normal"} />
        <h1>{error ? "うまく開けませんでした" : "冒険の準備中…"}</h1>
        {error && (
          <>
            <p>{error}</p>
            <Button variant="sun" onClick={retry}>
              <RefreshCcw size={18} />
              もう一度
            </Button>
          </>
        )}
      </Card>
    </main>
  );
}

export function LoginScreen({
  auth,
  onLogin,
}: {
  auth: AuthState;
  onLogin: (auth: AuthState) => void;
}) {
  const [passcode, setPasscode] = useState("");
  const [email, setEmail] = useState("owner@example.com");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  async function login(path: string, body: unknown) {
    setPending(true);
    setError("");
    try {
      const result = await apiFetch<{ user: AuthState["user"] }>(path, {
        body,
      });
      onLogin({ ...auth, authenticated: true, user: result.user });
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "ログインできませんでした",
      );
    } finally {
      setPending(false);
    }
  }
  return (
    <main className="auth-game">
      <Card className="auth-game-card">
        <Buddy size={92} />
        <span className="auth-brand">SymNote</span>
        <h1>おかえり</h1>
        <p>ミナと一緒に、きょうの一歩をはじめよう。</p>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {auth.google_enabled && !isNativeApp() && (
          <a
            className="game-button sun md auth-google"
            href="/api/auth/google/start"
          >
            <LogIn size={18} />
            Google アカウントで開く
          </a>
        )}
        {auth.google_enabled && isNativeApp() && auth.mobile_enabled && (
          <Button
            variant="sun"
            disabled={pending}
            onClick={async () => {
              setPending(true);
              try {
                await nativeSignIn();
              } catch {
                setError("ログインできませんでした");
                setPending(false);
              }
            }}
          >
            Google アカウントで開く
          </Button>
        )}
        {auth.passcode_enabled && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void login("/api/auth/login", { passcode });
            }}
          >
            <label>
              パスコード
              <input
                type="password"
                value={passcode}
                onChange={(event) => setPasscode(event.target.value)}
              />
            </label>
            <Button variant="sun" disabled={pending}>
              開く
            </Button>
          </form>
        )}
        {auth.e2e_enabled && (
          <form
            className="test-login"
            onSubmit={(event) => {
              event.preventDefault();
              void login("/api/auth/e2e-login", {
                email,
                name: "テストユーザー",
              });
            }}
          >
            <label>
              開発用メール
              <input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </label>
            <Button disabled={pending}>テストで開く</Button>
          </form>
        )}
      </Card>
    </main>
  );
}
