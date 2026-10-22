import { Navigate, Route, Routes } from "react-router-dom";
import { BoardPage } from "./features/board/BoardPage";
import { ComponentKit } from "./features/dev/ComponentKit";
import { MapPage } from "./features/map/MapPage";
import { MePage } from "./features/me/MePage";
import { SettingsPage } from "./features/settings/SettingsPage";
import { TodayPage } from "./features/today/TodayPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/today" element={<TodayPage />} />
      <Route path="/map" element={<MapPage />} />
      <Route path="/board" element={<BoardPage />} />
      <Route path="/me" element={<MePage />} />
      <Route path="/me/settings" element={<SettingsPage />} />
      {import.meta.env.DEV && (
        <Route path="/dev/kit" element={<ComponentKit />} />
      )}
      <Route
        path="/inbox"
        element={<Navigate replace to="/board?tab=requests" />}
      />
      <Route path="/all" element={<Navigate replace to="/map?view=all" />} />
      <Route
        path="/ideas"
        element={<Navigate replace to="/map?view=ideas" />}
      />
      <Route
        path="/settings"
        element={<Navigate replace to="/me/settings" />}
      />
      <Route path="*" element={<Navigate replace to="/today" />} />
    </Routes>
  );
}
