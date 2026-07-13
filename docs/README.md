# AIive 文档索引

## 目录结构

```
docs/
├── README.md                          # 本索引
├── references.md                      # 技术参考链接
├── architecture/                      # 架构设计文档
│   ├── memory_architecture_design.md  # 记忆系统架构设计
│   └── memory_v2_architecture.md      # 记忆系统 V2 实现架构
├── memory/                            # 记忆系统专项文档
│   ├── 记忆重构_V2 方案.md             # 记忆重构 V2 方案
│   └── memory_refactor_final_plan.md  # 记忆重构终版计划
├── plans/                             # 项目计划
│   ├── final_project_plan_v7.md       # V7 终版项目计划
│   ├── current_phase.md               # 当前阶段说明
│   └── versions/                      # 各版本计划 (v0-v33)
├── reports/                           # 各类报告
│   ├── memory_triple_recall_fix_report.md              # 记忆系统三重召回修复报告
│   ├── PROGRESS_REPORT.md                               # 进度报告
│   └── AIive_v1-v20_functional_audit_and_repair_report.md  # V1-V20 功能审计修复报告
├── audit/                             # 审计报告
│   ├── CODE_REVIEW_REPORT.md
│   ├── intermittent_tool_calling_audit.md
│   ├── intermittent_tool_calling_fix_report.md
│   ├── v0_v20_architecture_drift_report.md
│   ├── v0_v20_dependency_architecture_report.md
│   ├── v0_v20_dependency_hardening_completion_report.md
│   ├── v1_v20_functional_truth_report.md
│   ├── v20_action_planner_refactor_completion_report.md
│   ├── v20_action_planner_refactor_report.md
│   └── v20_memory_architecture_drift_report.md
├── adr/                               # 架构决策记录
│   └── 0001-tech-stack.md
└── guides/                            # 指南与规范
    ├── 编码规范.md
    └── FEATURE_VERIFICATION_GUIDE.md
```

## 快速导航

### 新人上手
1. `guides/编码规范.md` — 项目编码规范
2. `guides/FEATURE_VERIFICATION_GUIDE.md` — 功能验证指南
3. `plans/final_project_plan_v7.md` — 终版项目计划
4. `adr/0001-tech-stack.md` — 技术选型决策

### 记忆系统
1. `architecture/memory_architecture_design.md` — 架构设计
2. `architecture/memory_v2_architecture.md` — V2 实现架构
3. `memory/记忆重构_V2 方案.md` — 重构方案
4. `memory/memory_refactor_final_plan.md` — 终版重构计划
5. `reports/memory_triple_recall_fix_report.md` — 三重召回修复报告

### 审计与报告
- `audit/` — 各阶段审计报告
- `reports/` — 进度报告与修复总结

### 版本计划
- `plans/versions/` — V0 到 V33 各版本计划详情
