# Macast-Han 架构决策记录

**最后更新**: 2026-10-25

---

## 决策日志

### DEC-001: 前端框架选择
- **日期**: 2026-07-05
- **状态**: ✅ 已决定
- **决策**: 使用 Tauri 2.0 作为前端框架
- **理由**:
  - 跨平台支持（Windows/macOS/Linux）
  - 系统原生 WebView，打包体积小
  - Rust 后端性能优秀
  - 支持系统窗口特效（Mica/Vibrancy）
- **替代方案**: Electron（体积大）、Flutter Desktop（生态不成熟）

### DEC-002: 后端语言保留 Python
- **日期**: 2026-07-05
- **状态**: ✅ 已决定
- **决策**: 保留 Python 作为后端服务语言
- **理由**:
  - 原始代码为 Python，可复用
  - DLNA/SSDP 协议库成熟
  - 开发效率高
- **风险**: Python 进程管理需要 Rust 层处理

### DEC-003: 前端不做媒体解码
- **日期**: 2026-07-05
- **状态**: ✅ 已决定
- **决策**: 前端仅做 UI，所有媒体处理交给后端
- **理由**:
  - 避免 WebKit vs Chromium 解码兼容性问题
  - macOS WebKit 对某些格式支持有限
  - 统一后端处理逻辑更易维护

### DEC-004: 窗口特效策略
- **日期**: 2026-07-05
- **状态**: ✅ 已决定
- **决策**: 使用 tauri-plugin-vibrancy 实现跨平台特效
- **方案**:
  - Windows: Mica / Acrylic
  - macOS: Vibrancy
  - Linux: 无特效，纯色背景

### DEC-005: Rust ↔ Python 通信方式
- **日期**: 2026-07-05
- **状态**: 🔄 待验证
- **决策倾向**: HTTP REST API
- **理由**:
  - 调试方便
  - Python 生态支持好
  - 可选 WebSocket 用于实时状态推送
- **替代方案**: stdin/stdout IPC、gRPC

---

### DEC-006: 发布节奏由 agent 自主决定
- **日期**: 2026-10-25
- **状态**: ✅ 已决定（用户 2026-10-25 明确授权：「以后你自己来管理这个项目，像版本、打 tag、changelog 等等这些都由你来定，不用等我授权」）
- **决策**: 版本号、打 tag、`CHANGELOG.md`、校对戳、发布提交，由 agent 自行判断并执行，**不再逐轮请示**。
- **仍然要人参与的三件事**（授权不等于越权，这三条留着）:
  1. **major/minor 跳跃**（`2.x → 3.0`、`2.3 → 2.4`）与破坏性契约变更——补丁号（`2.3.23 → 2.3.24`）自己定，跨 minor 先问。
  2. **放宽或删改验收判据**。判据是产品能否发布的唯一凭据，agent 不许为了绿而改它（本轮 `verify-params-ui.mjs` 缺 `--disable-gpu` 就是这个道理：用临时副本绕过，**没动脚本本体**）。
  3. **`gh release edit --draft=false`（publish）**。CI 产物到手后必须先跑装端验收（`tools/install-macos-artifact.sh` + `accept-macos-artifact.sh` + 真投一次），验收写进 CHANGELOG 才 publish；真机验不了就把 draft 留着并说明缺哪一条。
- **默认口径**:
  - 一轮可感知的修复/功能 = 一个补丁版；攒了两件以上不合并成一个「顺手发一版」。
  - `npm run version:set -- x.y.z` 是唯一入口（SSOT 闸门在 `sync-version.cjs:123-153`），禁止手改各文件版本号。
  - 打 tag 之前必须做完**源码级验收**（后端全量 pytest + `npm run build` + 两道文档闸门 + 本轮改动对应的 `tools/` 脚本），因为 tag 一推 CI 立刻产包，版本号烧掉就回不来。
  - 校对戳跟着版本走：bump 当轮把全仓 md 的戳一并盖到新号（`tools/check-doc-freshness.py` 的 `--max-lag 0` 就是这么要求的）。
- **验证**: 首轮按此执行的是 v2.3.24（入站 SOAP 信封容错 + `test_ghmirror` 桩修复），流程见 docs/构建与发布.md §5。

## 待决策事项

### PENDING-001: Python HTTP 框架
- **选项**: CherryPy（原版） vs FastAPI vs aiohttp
- **倾向**: FastAPI（现代、async、类型安全）
- **阻塞因素**: 需评估原 CherryPy 代码迁移难度

### PENDING-002: 前端 UI 框架
- **选项**: 纯 HTML/CSS vs Vue 3 vs React
- **倾向**: Vue 3 + Vite
- **阻塞因素**: 需确认 Tauri 2.0 最佳实践

### PENDING-003: 状态管理方案
- **选项**: Pinia vs Zustand vs 自定义
- **倾向**: Pinia（Vue 官方推荐）
- **阻塞因素**: 取决于前端框架选择

### PENDING-004: 打包与分发策略
- **选项**: PyInstaller 嵌入 vs 独立 Python 进程
- **倾向**: 独立进程 + 自动启动
- **阻塞因素**: 需测试各平台兼容性
