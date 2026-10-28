"""文件夹投屏（播放队列）后端：`MediaParser.list_folder` 的行为测试。

背景：用户要「投整个文件夹 + 可调文件夹内顺序」。队列的初始顺序靠后端给，
所以这里钉死三件事：
- 自然序（`第2集` 排在 `第10集` 前），这正是本功能要解决的痛点；
- 只列当前层的媒体文件，跳过子目录、隐藏文件、macOS `._` 资源叉与不支持的扩展名；
- 不探时长（`duration` 为 None），否则一个 200 集的目录会串行跑 200 次 ffprobe 卡死界面。
"""
import os

import pytest

from hancast_sidecar.media.parser import MediaParser


@pytest.fixture
def parser():
    return MediaParser()


def _touch(folder, name, data=b"x"):
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


class TestListFolder:
    def test_natural_order_two_before_ten(self, parser, tmp_path):
        for name in ["第10集.mp4", "第2集.mp4", "第1集.mp4"]:
            _touch(str(tmp_path), name)
        res = parser.list_folder(str(tmp_path))
        titles = [f["title"] for f in res["files"]]
        assert titles == ["第1集.mp4", "第2集.mp4", "第10集.mp4"]
        assert res["folder"] == str(tmp_path)

    def test_skips_dirs_hidden_and_unsupported(self, parser, tmp_path):
        _touch(str(tmp_path), "花絮.mkv")
        _touch(str(tmp_path), "readme.txt")          # 不支持的扩展名
        _touch(str(tmp_path), ".hidden.mp4")          # 隐藏文件
        _touch(str(tmp_path), "._资源叉.mp4")          # macOS 资源叉
        os.makedirs(os.path.join(str(tmp_path), "子目录"))
        _touch(os.path.join(str(tmp_path), "子目录"), "嵌套.mp4")  # 目录内的文件不该被递归列出
        res = parser.list_folder(str(tmp_path))
        titles = [f["title"] for f in res["files"]]
        assert titles == ["花絮.mkv"]

    def test_no_duration_probing(self, parser, tmp_path, monkeypatch):
        """list_folder 不许探时长——把 ffprobe 调用换成一碰就炸的桩来证明。"""
        _touch(str(tmp_path), "a.mp4")

        def boom(*_a, **_k):
            raise AssertionError("list_folder 不应调用 ffprobe（会卡死大目录）")

        monkeypatch.setattr(parser, "_probe_duration", boom, raising=False)
        res = parser.list_folder(str(tmp_path))
        assert res["files"][0]["duration"] is None

    def test_media_info_shape(self, parser, tmp_path):
        _touch(str(tmp_path), "movie.mp4")
        f = parser.list_folder(str(tmp_path))["files"][0]
        assert f["media_type"] == "file"
        assert f["uri"].endswith("movie.mp4")
        assert f["mime_type"] == "video/mp4"

    def test_not_a_folder_raises(self, parser, tmp_path):
        with pytest.raises(NotADirectoryError):
            parser.list_folder(str(tmp_path / "does-not-exist"))

    def test_empty_folder(self, parser, tmp_path):
        res = parser.list_folder(str(tmp_path))
        assert res["files"] == []
