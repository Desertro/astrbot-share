import asyncio
import hashlib
import json
from collections import OrderedDict
from contextlib import suppress
from http import cookiejar
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from astrbot.api import logger

K = TypeVar("K")
V = TypeVar("V")


class LimitedSizeDict(OrderedDict[K, V]):
    """
    定长字典
    """

    def __init__(self, *args, max_size=20, **kwargs):
        self.max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: K, value: V):
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)  # 移除最早添加的项


async def safe_unlink(path: Path):
    """
    安全删除文件
    """
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except Exception:
        logger.warning(f"删除 {path} 失败")


async def exec_ffmpeg_cmd(cmd: list[str]) -> None:
    """执行命令

    Args:
        cmd (list[str]): 命令序列
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
        return_code = process.returncode
    except FileNotFoundError:
        raise RuntimeError("ffmpeg 未安装或无法找到可执行文件")

    if return_code != 0:
        error_msg = stderr.decode().strip()
        raise RuntimeError(f"ffmpeg 执行失败: {error_msg}")


async def merge_av(
    *,
    v_path: Path,
    a_path: Path,
    output_path: Path,
    ffmpeg_threads: int | None = None,
) -> None:
    """合并视频和音频

    Args:
        v_path (Path): 视频文件路径
        a_path (Path): 音频文件路径
        output_path (Path): 输出文件路径
    """
    logger.info(f"Merging {v_path.name} and {a_path.name} to {output_path.name}")

    cmd = [
        "ffmpeg",
        "-y",
    ]
    if ffmpeg_threads is not None and ffmpeg_threads > 0:
        cmd += ["-threads", str(ffmpeg_threads)]
    cmd += [
        "-i",
        str(v_path),
        "-i",
        str(a_path),
        "-c",
        "copy",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(output_path),
    ]

    await exec_ffmpeg_cmd(cmd)
    await asyncio.gather(safe_unlink(v_path), safe_unlink(a_path))
    logger.info(f"Merged {output_path.name}, {fmt_size(output_path)}")


async def merge_av_h264(
    *,
    v_path: Path,
    a_path: Path,
    output_path: Path,
) -> None:
    """合并视频和音频，并使用 H.264 编码

    Args:
        v_path (Path): 视频文件路径
        a_path (Path): 音频文件路径
        output_path (Path): 输出文件路径
    """
    logger.info(
        f"Merging {v_path.name} and {a_path.name} to {output_path.name} with H.264"
    )

    # 修改命令以确保视频使用 H.264 编码
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(v_path),
        "-i",
        str(a_path),
        "-c:v",
        "libx264",  # 明确指定使用 H.264 编码
        "-preset",
        "medium",  # 编码速度和质量的平衡
        "-crf",
        "23",  # 质量因子，值越低质量越高
        "-c:a",
        "aac",  # 音频使用 AAC 编码
        "-b:a",
        "128k",  # 音频比特率
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(output_path),
    ]

    await exec_ffmpeg_cmd(cmd)
    await asyncio.gather(safe_unlink(v_path), safe_unlink(a_path))
    logger.info(f"Merged {output_path.name} with H.264, {fmt_size(output_path)}")


async def encode_video_to_h264(video_path: Path) -> Path:
    """将视频重新编码到 h264

    Args:
        video_path (Path): 视频路径

    Returns:
        Path: 编码后的视频路径
    """
    output_path = video_path.with_name(f"{video_path.stem}_h264{video_path.suffix}")
    if output_path.exists():
        return output_path
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        str(output_path),
    ]
    await exec_ffmpeg_cmd(cmd)
    logger.info(f"视频重新编码为 H.264 成功: {output_path}, {fmt_size(output_path)}")
    await safe_unlink(video_path)
    return output_path


async def encode_video_to_gif(video_path: Path) -> Path:
    """将短视频转成 gif，供动态表情类内容直接发送。"""
    output_path = video_path.with_suffix(".gif")
    if output_path.exists():
        with suppress(Exception):
            from PIL import Image

            with Image.open(output_path) as img:
                if getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1:
                    return output_path
            await safe_unlink(output_path)
        if output_path.exists():
            return output_path

    video_fps = await _probe_video_fps(video_path)
    fps_filter = f"fps={video_fps}:round=up," if video_fps else ""

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"{fps_filter}scale=iw:-1:flags=lanczos",
        "-loop",
        "0",
        str(output_path),
    ]
    await exec_ffmpeg_cmd(cmd)
    logger.info(f"视频转 GIF 成功: {output_path}, {fmt_size(output_path)}")
    return output_path


async def _probe_video_fps(video_path: Path) -> int | None:
    """读取源视频帧率，避免固定 fps 把极短动图抽成单帧。"""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None

    text = stdout.decode(errors="ignore").strip()
    if not text:
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            fps = round(float(num) / float(den))
        else:
            fps = round(float(text))
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    return max(1, min(fps, 24))


async def encode_gif_to_mp4(
    gif_path: Path,
    *,
    ffmpeg_threads: int | None = None,
) -> Path:
    """将 GIF 转为 MP4，供过大的动态图片按视频发送。"""
    output_path = gif_path.with_name(f"{gif_path.stem}_gif.mp4")
    if output_path.exists():
        return output_path

    cmd = [
        "ffmpeg",
        "-y",
    ]
    if ffmpeg_threads is not None and ffmpeg_threads > 0:
        cmd += ["-threads", str(ffmpeg_threads)]
    cmd += [
        "-i",
        str(gif_path),
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        str(output_path),
    ]
    await exec_ffmpeg_cmd(cmd)
    logger.info(f"GIF 转 MP4 成功: {output_path}, {fmt_size(output_path)}")
    return output_path


def fmt_size(file_path: Path) -> str:
    """格式化文件大小

    Args:
        video_path (Path): 视频路径
    """
    return f"大小: {file_path.stat().st_size / 1024 / 1024:.2f} MB"


def generate_file_name(url: str, default_suffix: str = "") -> str:
    """根据 url 生成文件名

    Args:
        url (str): url
        default_suffix (str): 默认后缀. Defaults to "".

    Returns:
        str: 文件名
    """
    # 根据 url 获取文件后缀
    path = Path(urlparse(url).path)
    suffix = path.suffix if path.suffix else default_suffix
    # 获取 url 的 md5 值
    url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
    file_name = f"{url_hash}{suffix}"
    return file_name


def save_cookies_with_netscape(cookies_str: str, file_path: Path, domain: str):
    """以 netscape 格式保存 cookies

    Args:
        cookies_str: cookies 字符串
        file_path: 保存的文件路径
        domain: 域名
    """
    # 创建 MozillaCookieJar 对象
    cj = cookiejar.MozillaCookieJar(file_path)

    # 从字符串创建 cookies 并添加到 MozillaCookieJar 对象
    for cookie in cookies_str.split(";"):
        name, value = cookie.strip().split("=", 1)
        cj.set_cookie(
            cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain="." + domain,
                domain_specified=True,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=True,
                expires=0,
                discard=True,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""},
                rfc2109=False,
            )
        )

    # 保存 cookies 到文件
    cj.save(ignore_discard=True, ignore_expires=True)


def ck2dict(cookies_str: str) -> dict[str, str]:
    """将 cookies 字符串转换为字典

    Args:
        cookies_str: cookies 字符串

    Returns:
        dict[str, str]: 字典
    """
    res = {}
    for cookie in cookies_str.split(";"):
        name, value = cookie.strip().split("=", 1)
        res[name] = value
    return res


def extract_json_url(data: dict | str) -> str | None:
    """处理 JSON 类型的消息段，提取 URL

    Args:
        data: JSON 类型的消息字典

    Returns:
        Optional[str]: 提取的 URL, 如果提取失败则返回 None
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None

    if not isinstance(data, dict):
        return None

    meta: dict[str, Any] | None = data.get("meta")
    if not meta:
        return None

    for key1, key2 in (
        ("music", "musicUrl"),
        ("detail_1", "qqdocurl"),
        ("news", "jumpUrl"),
        ("music", "jumpUrl"),
    ):
        if url := meta.get(key1, {}).get(key2):
            return url
    return None
