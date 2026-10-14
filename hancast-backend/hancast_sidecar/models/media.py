from dataclasses import dataclass, asdict, field
from typing import Optional

from urllib.parse import urlparse, urlunparse


def secure_thumbnail(url: Optional[str]) -> Optional[str]:
    """封面图统一升级为 https

    封面只在界面里当 <img> 显示。Tauri 的页面上下文是安全上下文
    （tauri://localhost），http 图片会被按混合内容直接拦掉、连请求都不发，
    表现为「解析成功但投屏卡片图片挂了」（B 站 data.pic 下发的就是 http://）。
    """
    if not url:
        return url
    parts = urlparse(url)
    if parts.scheme != "http":
        return url
    return urlunparse(parts._replace(scheme="https"))


@dataclass
class MediaInfo:
    media_type: str  # 'file', 'url'
    uri: str
    title: str
    duration: Optional[float] = None
    mime_type: str = ""
    file_size: Optional[int] = None
    thumbnail: Optional[str] = field(default=None)

    def __post_init__(self):
        self.thumbnail = secure_thumbnail(self.thumbnail)

    def to_dict(self) -> dict:
        return asdict(self)
