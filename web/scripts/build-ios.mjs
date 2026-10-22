import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const env = { ...process.env };
const xcode = "/Applications/Xcode.app/Contents/Developer";
if (!env.DEVELOPER_DIR && existsSync(xcode)) env.DEVELOPER_DIR = xcode;
const result = spawnSync("xcodebuild", [
  "-project", "ios/App/App.xcodeproj", "-scheme", "App",
  "-configuration", "Debug", "-sdk", "iphonesimulator",
  "-destination", "generic/platform=iOS Simulator",
  "-derivedDataPath", join(tmpdir(), "symnote-ios-derived"),
  "CODE_SIGNING_ALLOWED=NO", "build",
], { stdio: "inherit", env });
if (result.error) console.error("Xcodeを起動できませんでした。Xcodeのインストールを確認してください。");
process.exit(result.status ?? 1);
