# Macast-Han 已知问题与解决方案

**最后更新**: 2026-10-25

---

## 技术债务

### TD-001: 原始代码依赖过时
- **来源**: Macast-main/
- **问题**: 依赖库版本老旧（CherryPy、pystray 等）
- **影响**: 安全风险、兼容性问题
- **解决**: 全面升级依赖或替换为现代方案

### TD-002: 原始 GUI 代码废弃
- **来源**: macast/gui.py
- **问题**: 使用 pystray 实现系统托盘，与 Tauri 架构冲突
- **影响**: 需要完全重写前端
- **解决**: 由 Tauri 前端替代，gui.py 仅作参考

### TD-003: Python 进程管理
- **来源**: 架构设计
- **问题**: Rust 需要管理 Python 子进程生命周期
- **影响**: 崩溃恢复、资源清理
- **解决**: 实现 PythonBridge 模块，包含健康检查和自动重启

---

## 兼容性问题

### COMP-001: WebKit CSS 兼容性
- **平台**: macOS
- **问题**: 某些 CSS 特性在 WebKit 中不支持或表现不同
- **解决**:
  - 使用 `@supports` 检测
  - 避免最新 CSS 特性
  - 跨平台测试

### COMP-002: 媒体格式支持差异
- **平台**: macOS vs Windows
- **问题**: WebKit 对某些视频格式解码支持有限
- **解决**: 前端不负责解码，全部交给后端处理

### COMP-003: 系统特效 API 差异
- **平台**: 全平台
- **问题**: 每个平台的窗口特效 API 不同
- **解决**: 使用 tauri-plugin-vibrancy 抽象层

### COMP-004: 入站 SOAP 信封带前导空白 → 投屏被回裸 HTTP 500
- **平台**: 全平台（接收投屏侧）
- **发现日期**: 2026-10-25
- **状态**: 🟢 已解决并发布（v2.3.24，2026-10-25）
- **描述**: 某控制点把 `<?xml version="1.0"?>` 前面垫 16 个空格再 POST。libxml 严格按
  XML 规范拒绝，`DLNAProtocol.call()` 当时的 `etree.fromstring` 没有 try，异常冒到
  `server.py` 兜底 catch 回裸 500 —— 动作分发没走到，Device Guard 弹框也就没机会出现，
  用户只看到「投不上」。
- **取证要点**: lxml 该错误的**列号能反推报文形态**（列 = 声明真实起始列 + 5，
  `line 1, column 22` 即前面正好 16 个字符）。当时 SOAP Action/Body 只在 debug 级、
  面板默认 INFO，日志里既无来源 IP 也无 body，只能靠列号猜。
- **解决方案**: 前导空白/BOM 剔除后继续处理（`dlna.py:48-68`、`dlna.py:628-632`）；
  剔完仍不可解析的回 SOAP Fault 402（`dlna.py:633-641`）；兜底 catch 补 IP/Action/path/
  body 前 200 字符的取证（`protocol/server.py:178-192`）。详见 docs/故障排查.md §5.4。
- **验证方法**: `tests/test_dlna_soap_envelope_leniency.py`（**14 项**，tag 后补了 2 项边界：
  空 Body 的 `IndexError` 路径、Fault 自身必须可解析）+ 真起 DLNA server 喂现场报文，断言
  `CurrentURI` 落到动作处理器；**发布后又对装好的产物真验一次**——对 `/Applications/HanCast.app`
  的 2.3.24 打 16 前导空格报文回 200 + `GetTransportInfoResponse`（动作分发正常、日志落剔除告警），
  纯废文回 200 + Fault `402`。
- **发布**: tag `v2.3.24`（提交 `1cac337`），Release 已 publish：
  https://github.com/pingod/HanCast/releases/tag/v2.3.24

---

## 架构风险

### RISK-001: Python 启动延迟
- **描述**: Python 进程启动可能较慢
- **缓解**:
  - 预启动 Python 进程
  - 显示加载动画
  - 异步初始化

### RISK-002: 端口冲突
- **描述**: DLNA 服务需要固定端口，可能与其他应用冲突
- **缓解**:
  - 端口自动检测
  - 配置文件可修改
  - 启动时检查端口可用性

### RISK-003: 防火墙拦截
- **描述**: SSDP 多播可能被防火墙拦截
- **缓解**:
  - 启动时检测网络环境
  - 提供防火墙配置提示
  - 文档说明

---

## 解决方案模板

```markdown
### ISSUE-XXX: [标题]
- **发现日期**: YYYY-MM-DD
- **严重程度**: 高/中/低
- **状态**: 🔴 未解决 / 🟡 进行中 / 🟢 已解决
- **描述**: [问题描述]
- **复现步骤**: [步骤]
- **解决方案**: [方案]
- **验证方法**: [如何验证修复]
```
