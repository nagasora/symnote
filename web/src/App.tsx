import { useEffect, useState } from "react";
import { BrowserRouter, useNavigate } from "react-router-dom";
import { apiFetch } from "./api/client";
import type { AuthState } from "./api/types";
import { AppShell } from "./components/layout/AppShell";
import { AuthContext } from "./features/auth/AuthContext";
import { LoadingScreen, LoginScreen } from "./features/auth/LoginScreen";
import { OnboardingScreen } from "./features/auth/OnboardingScreen";
import { GameProvider } from "./features/game/GameContext";
import { applyTheme, initialTheme } from "./lib/theme";
import { AppRoutes } from "./routes";

const oldHashRoutes: Record<string, string> = {
  today: "/today",
  inbox: "/board?tab=requests",
  all: "/map?view=all",
  ideas: "/map?view=ideas",
  settings: "/me/settings",
};

function HashRedirect() {
  const navigate = useNavigate();
  useEffect(() => {
    const key = location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
    if (oldHashRoutes[key]) {
      history.replaceState(null, "", oldHashRoutes[key]);
      navigate(oldHashRoutes[key], { replace: true });
    }
  }, [navigate]);
  return null;
}

function AuthenticatedApp() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  useEffect(() => {
    applyTheme(initialTheme());
  }, []);
  useEffect(() => {
    setError("");
    apiFetch<AuthState>("/api/auth/me")
      .then(setAuth)
      .catch(() =>
        setError(
          "サーバーに接続できませんでした。通信状況を確認してください。",
        ),
      );
  }, [reload]);
  if (!auth)
    return (
      <LoadingScreen
        error={error}
        retry={() => setReload((value) => value + 1)}
      />
    );
  if (!auth.authenticated) return <LoginScreen auth={auth} onLogin={setAuth} />;
  if (auth.user && !auth.user.onboarded)
    return <OnboardingScreen auth={auth} onLogin={setAuth} />;
  return (
    <AuthContext.Provider value={{ auth, setAuth }}>
      <GameProvider>
        <HashRedirect />
        <AppShell>
          <AppRoutes />
        </AppShell>
      </GameProvider>
    </AuthContext.Provider>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <AuthenticatedApp />
    </BrowserRouter>
  );
}
