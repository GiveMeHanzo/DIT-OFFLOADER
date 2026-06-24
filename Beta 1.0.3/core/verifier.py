"""xxhash64 校验。

使用第三方库 ``xxhash``（C 扩展，远快于 hashlib.md5）。
提供流式计算接口：以固定缓冲区分块读取，内存占用恒定，
可处理任意大小的 R3D/BRAW/MXF 文件。

校验语义（双校验，由 PipelineConfig.verify_source_hash 控制）：
- 源 hash：拷贝前对源文件计算（可在预扫描阶段并行预计算）。
- 目标 hash：拷贝完成后对目标文件计算。
- 二者一致 → verified；不一致 → failed（说明拷贝/磁盘有问题）。
"""
from __future__ import annotations

import os
from typing import Callable, Optional

try:
    import xxhash  # type: ignore
except ImportError as e:  # pragma: no cover - 启动时由 main 检查
    raise ImportError(
        "缺少依赖 'xxhash'，请运行: pip install xxhash"
    ) from e


# 默认 1MiB 读取缓冲，平衡吞吐与内存
DEFAULT_BUFFER = 1024 * 1024


def hash_file(
    path: str | os.PathLike,
    buffer_size: int = DEFAULT_BUFFER,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> str:
    """计算文件的 xxhash64，返回十六进制字符串。

    Args:
        path: 文件路径。
        buffer_size: 读取缓冲字节数。
        progress_cb: 可选回调，参数为本次读取的字节数（用于进度显示）。

    Returns:
        16 位十六进制字符串，如 ``"0x1f2e3d4c5b6a7f8e"``。
    """
    h = xxhash.xxh64()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(buffer_size)
            if not chunk:
                break
            h.update(chunk)
            if progress_cb is not None:
                progress_cb(len(chunk))
    # 返回带 0x 前缀的完整十六进制，便于在 XML 中辨识
    return h.hexdigest()


def verify_pair(
    src_hash: Optional[str],
    dest_hash: str,
) -> bool:
    """比较源/目标 hash 是否一致。

    当 ``src_hash`` 为 None（仅目标校验模式）时，无法判断损坏，
    视为「已计算但未比对」，返回 True（仍写入日志）。
    """
    if src_hash is None:
        return True
    return _norm(src_hash) == _norm(dest_hash)


def _norm(h: str) -> str:
    """规范化 hex 表示（去前缀、转小写），便于比较。"""
    h = h.lower()
    if h.startswith("0x"):
        h = h[2:]
    return h


__all__ = ["hash_file", "verify_pair", "DEFAULT_BUFFER"]
