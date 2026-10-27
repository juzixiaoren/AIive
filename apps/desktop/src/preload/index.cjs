const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("aiiveDesktop", Object.freeze({
  getRuntimeInfo: () => ipcRenderer.invoke("aiive-desktop:runtime-info"),
  // 登录态同步：只把 Token 交给主进程内存，渲染进程不直接接触 Node Host。
  setAuthToken: (token) => ipcRenderer.invoke("aiive-desktop:set-auth-token", token),
}));
