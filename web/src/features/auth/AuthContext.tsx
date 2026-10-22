import {
  createContext,
  useContext,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { AuthState } from "../../api/types";

export interface AuthContextValue {
  auth: AuthState;
  setAuth: Dispatch<SetStateAction<AuthState | null>>;
}
export const AuthContext = createContext<AuthContextValue | null>(null);
export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthContext");
  return value;
}
