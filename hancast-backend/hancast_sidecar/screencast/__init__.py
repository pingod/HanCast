"""
屏幕投屏子系统（DLNA 大屏 + 浏览器免安装观看，共用同一路 HLS）

模块：
- ffmpeg_manager: FFmpeg 查找/校验 + 捕获源枚举
- capture: FFmpeg 屏幕/音频捕获管线（HLS 输出 + 看门狗）
- httpd: aiohttp 服务（观看页 / HLS 静态）
- manager: 统一生命周期与命令接口
"""

from .manager import ScreenCastManager

__all__ = ["ScreenCastManager"]
