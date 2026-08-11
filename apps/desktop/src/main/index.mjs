import { app, BrowserWindow, ipcMain, nativeImage, Tray, Menu, utilityProcess } from "electron";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const currentDir = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(currentDir, "../..");
const backendUrl = (process.env.AIIVE_DESKTOP_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const webUrl = process.env.AIIVE_DESKTOP_WEB_URL || `${backendUrl}/aiive/`;

let mainWindow;
let tray;
let nodeHost;
let nodeHostRestartTimer;
let nodeHostStableTimer;
let nodeHostRestartMs = 1000;
let nodeId = "";

function statePath() {
  return path.join(app.getPath("userData"), "desktop-node.json");
}

function loadNodeId() {
  try {
    const state = JSON.parse(fs.readFileSync(statePath(), "utf8"));
    if (typeof state.nodeId === "string" && state.nodeId.length >= 8) return state.nodeId;
  } catch {
    // 首次启动或旧状态损坏时生成新节点身份。
  }
  const next = crypto.randomUUID();
  fs.mkdirSync(path.dirname(statePath()), { recursive: true });
  fs.writeFileSync(statePath(), JSON.stringify({ nodeId: next }, null, 2), { mode: 0o600 });
  return next;
}

function resourceIconPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "aiive-app-icon.png");
  }
  return path.resolve(desktopRoot, "../../assets/branding/aiive-app-icon.png");
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 640,
    icon: resourceIconPath(),
    webPreferences: {
      preload: path.resolve(desktopRoot, "src/preload/index.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  void mainWindow.loadURL(webUrl);
  mainWindow.on("close", (event) => {
    if (!app.isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
}

function createTray() {
  const icon = nativeImage.createFromPath(resourceIconPath()).resize({ width: 20, height: 20 });
  tray = new Tray(icon);
  tray.setToolTip("AIive Desktop Node");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "打开 AIive", click: () => { mainWindow.show(); mainWindow.focus(); } },
    { type: "separator" },
    { label: "退出", click: () => { app.isQuitting = true; app.quit(); } },
  ]));
  tray.on("double-click", () => mainWindow.show());
}

function startNodeHost() {
  const script = app.isPackaged
    ? path.join(process.resourcesPath, "app.asar.unpacked/src/node-host/client.mjs")
    : path.resolve(desktopRoot, "src/node-host/client.mjs");
  nodeHost = utilityProcess.fork(script, [], {
    serviceName: "AIive Desktop Node",
    env: {
      ...process.env,
      AIIVE_DESKTOP_NODE_ID: nodeId,
      AIIVE_DESKTOP_NODE_NAME: process.env.AIIVE_DESKTOP_NODE_NAME || app.getName(),
      AIIVE_DESKTOP_BACKEND_URL: backendUrl,
      AIIVE_DESKTOP_APP_VERSION: app.getVersion(),
    },
  });
  nodeHostStableTimer = setTimeout(() => { nodeHostRestartMs = 1000; }, 30000);
  nodeHost.on("exit", (code) => {
    clearTimeout(nodeHostStableTimer);
    nodeHost = undefined;
    if (!app.isQuitting) {
      console.error(`AIive Desktop Node exited: ${code}; restarting in ${nodeHostRestartMs}ms`);
      nodeHostRestartTimer = setTimeout(startNodeHost, nodeHostRestartMs);
      nodeHostRestartMs = Math.min(30000, nodeHostRestartMs * 2);
    }
  });
}

ipcMain.handle("aiive-desktop:runtime-info", () => ({
  desktop: true,
  nodeId,
  backendUrl,
  platform: process.platform,
  arch: process.arch,
  version: app.getVersion(),
}));

app.whenReady().then(() => {
  nodeId = loadNodeId();
  createWindow();
  createTray();
  startNodeHost();
  if (app.isPackaged) app.setLoginItemSettings({ openAtLogin: true });
});

app.on("activate", () => {
  if (mainWindow) mainWindow.show();
  else createWindow();
});

app.on("before-quit", () => {
  app.isQuitting = true;
  clearTimeout(nodeHostRestartTimer);
  clearTimeout(nodeHostStableTimer);
  nodeHost?.kill();
});
