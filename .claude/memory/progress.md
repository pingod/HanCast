# HanCast 开发进度

## 当前阶段：v2.3.19 工作区前端优化（尚未发布）

**最后更新**: 2026-10-24

本轮已安全拉取 `origin/feat/js-rule-plugins`，远端无新增，本地比 `origin/main` 多 5 笔既有提交。用户确认“克制精致的原生工具”风格：统一紫色亮暗主题、媒体/设备卡片、设置分组、弹窗边界及键盘操作；保持 Linux 不透明策略。修复解析结果竞态、App 路由切换丢失投屏监听、迟到监听清理、主题缓存、播控错误吞掉、进度锁定/旧轮询回写、视频被当图片加载；去掉无功能的多房同步装饰。

验证：媒体/主题 73 项、播控原 20 项及封面新增 1 项、真实 App 生命周期 2 项；类型检查及 Vite 构建通过。浏览器验 346×472 / 434×634 / 800×900、中英设置、日志/屏幕/控制器弹窗、解析/暂停/停止/清空（项目内置 mock，不是真机投屏）。未跑全量测试、未打三平台包、未提交或发布。界面截图在 `docs/images/ui-native-{light,dark}.png`。

## 2026-10-24 续：后端四项复查问题已修

同一工作区（未提交、未 bump、未打 tag）。四项全部落地并有离线并发用例：

1. 规则包三条慢操作任务化（`media/rules/tasks.py` 新增；`commands.py` 注册
   `plugin_task_start/result/cancel`，Sidecar 注册表 59 → **62**，`commands.py:131-222`）；
   壳层 `run_plugin_task` 轮询（`lib.rs:324-390`），前端封装与 Tauri 命令名不变。
   安装改为「暂存目录 → rename」，失败退回旧包；取消与提交互斥。
2. 轮询每代独立取消令牌 + `callback(session, event_type, generation)`，
   处理器在 `_cast_lock` 下校验实例身份与代次，旧回调不再误删新会话。
3. 会话带 `owner`，屏幕通道只停自己那份；媒体投屏开播前 `stop_for_device` 让屏幕让位。
4. 启动期间采集错误记 `_pending_error` 并统一回滚，不再「返回成功却没画面」。

验证：`test_plugin_tasks` 10、`test_session_poll_generation` 与 `test_stop_cast_delivery`
合计 42、`test_screencast` 全量、`test_jsplugins` 43 全绿；`cargo check` 通过。
契约同步：AGENTS.md、hancast-backend/README.md、docs/{架构总览,HanCast-Frontend-API,
HanCast-Frontend-Spec,README,开发指南}.md 的计数与 §11 状态。
**未做**：真机复现这些失败、三平台打包、发布。

---

## 历史记录：2026-07-05 项目初始化

以下保留当时进度，不代表当前实现状态。

---

## 总体进度

| 阶段 | 状态 | 进度 |
|------|------|------|
| Phase 0: 项目初始化 | 🔄 进行中 | 30% |
| Phase 1: 基础架构 | ⏳ 待开始 | 0% |
| Phase 2: 前端 UI | ⏳ 待开始 | 0% |
| Phase 3: 后端功能 | ⏳ 待开始 | 0% |
| Phase 4: 集成优化 | ⏳ 待开始 | 0% |

---

## Phase 0: 项目初始化

### 已完成
- [x] 分析原始 Macast 1.x 代码结构
- [x] 阅读并理解功能规格文档
- [x] 创建 CLAUDE.md 项目指南
- [x] 创建 .claude/memory 记忆系统
- [x] 识别可复用代码模块
- [x] 拆解前后端设计文档
  - [x] `docs/Macast-Frontend-Spec.md` — 前端 UI/UX 方案
  - [x] `docs/Macast-Backend-Spec.md` — 后端 Rust+Python 方案

### 进行中
- [ ] 初始化 Tauri 2.0 项目骨架
- [ ] 配置 Python 后端虚拟环境
- [ ] 设计 Rust ↔ Python 通信协议

### 待开始
- [ ] 配置开发环境文档
- [ ] 创建 CI/CD 配置

---

## 关键里程碑

| 里程碑 | 目标日期 | 状态 |
|--------|----------|------|
| M1: 项目骨架完成 | TBD | ⏳ |
| M2: 基础 UI 可运行 | TBD | ⏳ |
| M3: SSDP 发现可用 | TBD | ⏳ |
| M4: DLNA 投屏可用 | TBD | ⏳ |
| M5: 首个 Beta 版本 | TBD | ⏳ |

---

## 会话记录

### 2026-07-05 — 首次会话
- 完成项目结构分析
- 确定技术栈：Tauri 2.0 + Python
- 创建项目文档体系
- 原始代码最后更新于 2022-01，需要大量现代化改造

### 2026-07-05 — 前后端拆解
- 将原始功能规格文档拆解为两个独立方案
- **前端方案** (Macast-Frontend-Spec.md):
  - Vue 3 + Vite + TypeScript + Pinia
  - Tauri invoke() 命令层定义
  - 组件规格、样式系统、国际化
  - WebView 兼容性约束
- **后端方案** (Macast-Backend-Spec.md):
  - Rust SidecarManager (stdin/stdout IPC)
  - Python Sidecar 入口 + 命令路由器
  - 原始代码复用映射表 (ssdp/protocol/renderer/mpv)
  - 新增 MediaParser + MediaServer
  - 完整通信协议定义
  - PyInstaller 打包流程
