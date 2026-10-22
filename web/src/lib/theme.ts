export type Theme = "light" | "dark";

export function initialTheme(): Theme {
  const saved = localStorage.getItem("symnote_theme");
  if (saved === "light" || saved === "dark") return saved;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("symnote_theme", theme);
}
