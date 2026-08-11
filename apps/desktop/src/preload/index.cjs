const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("aiiveDesktop", Object.freeze({
  getRuntimeInfo: () => ipcRenderer.invoke("aiive-desktop:runtime-info"),
}));
