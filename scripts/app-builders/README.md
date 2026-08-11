# 应用构建器

`../build-app.sh` 是统一入口，每个可分发客户端对应本目录中的一个同名可执行脚本。

- 当前目标：`android.sh`、`desktop.sh`
- `desktop.sh` 使用 Electron Forge，按当前宿主系统生成 Windows、macOS 或 Linux 产物。

构建器只负责客户端产物；服务器部署仍仅包含 `backend/` 与 `frontend/`。
