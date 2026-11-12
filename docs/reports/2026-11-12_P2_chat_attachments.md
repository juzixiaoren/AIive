# P2 跨端聊天附件实现报告

日期：2026-11-12。Backend、Web、Android 在一个 Part 内实现，Desktop 复用 Web。

## 实现与取舍

- 复用 Object Store 的 SHA256 内容寻址和 safe_delete，不建立第二套文件服务。
- 新增 chat_attachments 元数据表和 Alembic c4d7e9f1a203；原件不进入普通事件或快照。
- 认证 API：POST /api/attachments（raw-body + URL 编码 X-Filename），GET /{id}、GET /{id}/content、DELETE /{id}。
- 服务端绑定当前唯一 Thread；附件在原用户消息 Turn 事务内关联，ID 集合参与 Turn 指纹。
- 下载、绑定和上下文入口均检查 Thread 和中央 Forget 可见性；附件 ID 不暴露对象路径。
- 文档复用 document_reader，拒绝截断提取、无文本层 PDF、伪 MIME/格式、非法文件名、DOCX 解压超限与宏。
- 图片通过 OpenAI-compatible 多模态 content block 直接发送给主视觉模型或独立视觉模型；未配置则明确拒绝。
- 附件完整文本/图片进入 ContextAssembler 的现有 ContextOverflowPacker，无独立 Token 池。
- 为保证连续引用，当前实现每轮重载所有可见聊天附件。总量超窗明确失败；代价是持续 Token 和 IO 成本。
- 后续读取附件的轮次持久记录 attachment_context 来源关系；Forget 阻断原件及派生轮次读取。
- 附件派生回复不作为自动记忆抽取输入，原用户文本仍可按原规则提取；不自动导入知识库。
- 24h 未发送上传与取消/失败关联由上传入口及每小时 GC 回收，同 hash 活跃引用保护共享原件。
- Web/Android 共享选择、图片预览、上传进度、失败重试、多附件、下载校验和历史卡片组件。
- Android 复用官方 Filesystem/Share；Desktop 使用现有 Web，不另建附件系统。
- 客户端保留文本直至 started；同一草稿重试复用 turn_id，历史卡片以稳定附件 ID 展示。

## 验证

- 原聊天与 ContextAssembler 41 项通过。
- 完整离线首轮：1960 passed / 134 skipped / 2 failed（482.92s）；两个失败已修复并通过聚焦回归：
  历史分页 Forget 过滤；认证静态检查将组件 prop 名误识别为认证 URL/头构造。
- 附件专项覆盖各文档/图片格式、安全拒绝、认证、归属、绑定拒绝、完整性、上下文预算、取消、
  TTL/共享对象、历史、重放和 Forget；测试随原 pytest / single-file Job 自动运行。
- 真实模型烟测（独立临时 SQLite/Object Store）：通过真实 SSE 上传 TXT，模型返回文件随机标记及
  17+25=42；后续真实 SSE 追问再次返回同一标记；原件下载成功。未修改生产会话。
- Web 与 Android TypeScript/Vite 生产构建通过；聊天模型 4 项、跨端工作/附件契约 15 项通过。
- ruff F/E9 与 basedpyright 全 Backend 通过；现有 CI 9 Job 全部保留，最终状态以提交 Checks 为准。

## 验收边界

- 当前未配置视觉服务：多模态输入和明确降级由离线模型边界测试验证，未宣称完成真实视觉模型识别验收。
- 本机未发现 Android SDK/JDK21 完整工具链，完成的是移动前端生产构建；未进行 APK/真机系统选择器、保存分享验收。
- 格式校验不执行脚本/宏，不等同于通用杀毒引擎；无文本层 PDF 不内置 OCR。
- 上传上限可下调（解析器当前最大20MiB），附件数可下调（客户端最高5个）。
- 未删除旧实现；保留任务附件与知识库原链路，聊天附件与其职责不同。
- 初始工作区既有 P1 报告与阶段记录改动保持原样，不纳入本 Part 提交。
