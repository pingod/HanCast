"""
屏幕投屏子系统（参考 Piik：DLNA 大屏 + WebRTC 浏览器免安装观看）

模块：
- ffmpeg_manager: FFmpeg 查找/校验 + 捕获源枚举
- capture: FFmpeg 屏幕/音频捕获管线（raw 帧 / HLS 双模式）
- webrtc_room: aiortc 观看房间（共享 track + 多观众）
- httpd: aiohttp 服务（WebSocket 信令 / HLS 静态 / 观看页）
- manager: 统一生命周期与命令接口
"""

from .manager import ScreenCastManager

__all__ = ["ScreenCastManager"]
