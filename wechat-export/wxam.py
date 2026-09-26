# -*- coding: utf-8 -*-
"""WXAM（WXGF）微信图片格式解码器（纯离线，依赖 ffmpeg）
格式逆向依据（sarv.blog《WXAM（WXGF）图片格式解析》，2025-08）：
- 微信 4.x 原图 .dat 经 V2 解密后是 WXGF 容器，内部是 HEVC Annex-B 裸流
- 结构：'wxgf'(4B) | hdr_len(1B) | ver(2B) | w(2B BE) | h(2B BE) | args(变长按bit)
        | extra headers(仅 v2，循环至 0x00) | partitions(HEVC 裸流)
- 解码思路：跳过私有 Header（参数按 bit 存储太复杂），直接扫描 NALU 起始码
  （00 00 01 / 00 00 00 01）提取裸流，交给 ffmpeg 转码
- 静态图 = 最大数据段单帧；多帧动画 = 多路流（动画+遮罩），第一版仅支持静态图
用法:
    import wxam
    img_bytes = wxam.decode(wxgf_bytes)   # → JPEG 字节（静态图）
"""
import os
import subprocess
import tempfile

FFMPEG = "ffmpeg"  # 需在 PATH 中；或改为绝对路径

NAL_START = b"\x00\x00\x01"
MAX_SEGMENTS = 64


def _parse_header(data):
    """解析 WXGF 主头，返回 (header_end, version, width, height)"""
    if len(data) < 11 or data[:4] != b"wxgf":
        raise ValueError("不是 WXGF 数据（magic 应为 'wxgf'）")
    hdr_len = data[4]
    version = int.from_bytes(data[5:7], "big")
    width = int.from_bytes(data[7:9], "big")
    height = int.from_bytes(data[9:11], "big")
    # 主头长度 = 4 + hdr_len（hdr_len 覆盖 version~args；防御：至少 11）
    header_end = max(11, 4 + hdr_len)
    if header_end > len(data):
        raise ValueError("头长度超出数据范围")
    return header_end, version, width, height


def _skip_extra_headers(data, pos, version):
    """跳过 Extra Header 区（仅 version==2），返回分区起始位置
    注意：v2 extra headers 含 AIGC JSON 元数据，长度字段格式实测多变、
    循环易错位（2026-09-07 两个失败文件实证：误把流 4B 起始码中间的 0x00
    当结束标记，VPS/SPS 被丢出扫描区）。decode() 已弃用本函数，
    改为从主头结束处直接全量扫描 + 锚定真参数集。保留仅供调试参考。"""
    if version != 2:
        return pos
    while pos < len(data):
        flag = data[pos]
        if flag == 0x00:  # 结束标记，其后即 Partition Header
            return pos + 1
        if pos + 3 > len(data):
            break
        case_len = data[pos + 2]
        pos += 3 + case_len
    return pos


def _extract_nalu_stream(data, pos):
    """扫描 NALU 起始码（4B 优先、3B 兜底），返回拼接后的 Annex-B 字节流
    2026-09-07 修复（14 个 v2 失败文件实证）：
    1. 旧版用 3B 起始码 00 00 01 扫描，会把 4B 起始码 00 00 00 01 的内部
       尾 3 字节误切成新段边界，导致前一段被截断 1 字节（真 VPS 被切成
       7 字节垃圾 → ffmpeg "vps_base_layer_intern"）。→ 4B 优先扫描。
    2. v2 文件流前可能带 PTL 短段（6B 假 VPS）或 PNG 分区垃圾数据
       （假起始码串）→ 从第一个"段长≥10 的真参数集（VPS/SPS）"锚定流
       起点，遇连续 4 个非法/过短段截断（防尾随 PNG 分区混入）。
    3. 返回流必须以起始码开头，否则 ffmpeg -f hevc 会丢弃第一个 NAL
       → 流头统一补 4B 起始码，段间也统一 4B 拼接（Annex-B 兼容）。"""
    def next_start(i):
        """返回 (起始码位置, 长度) 或 None"""
        j4 = data.find(b"\x00\x00\x00\x01", i)
        j3 = data.find(b"\x00\x00\x01", i)
        cand = []
        if j4 >= 0:
            cand.append((j4, 4))
        if j3 >= 0 and (j4 < 0 or j3 != j4 + 1):  # 排除 4B 起始码的尾 3B
            cand.append((j3, 3))
        return min(cand) if cand else None

    # 先收集所有候选段
    segs = []
    i = pos
    while i < len(data) - 4 and len(segs) < MAX_SEGMENTS * 4:
        s = next_start(i)
        if s is None:
            break
        j, length = s
        start = j + length
        if start >= len(data):
            break
        nxt = next_start(start)
        end = nxt[0] if nxt else len(data)
        if end - start > 4:
            segs.append(data[start:end])
        i = end if nxt else len(data)

    # 锚定流起点：第一个段长 ≥ 10 的真参数集（VPS=32 / SPS=33）
    anchor = -1
    for k, seg in enumerate(segs):
        t = (seg[0] >> 1) & 0x3F
        if len(seg) >= 10 and t in (32, 33):
            anchor = k
            break
    if anchor < 0:
        raise ValueError("未找到 HEVC 参数集（VPS/SPS）")

    # 从 anchor 起收集，遇连续 4 个非法段截断（PNG 分区尾随垃圾防护）
    VALID_TYPES = {0, 1, 19, 20, 21, 32, 33, 34, 39, 40}
    out = []
    bad_run = 0
    for seg in segs[anchor:]:
        t = (seg[0] >> 1) & 0x3F
        if len(seg) < 5 or t not in VALID_TYPES:
            bad_run += 1
            if bad_run >= 4:
                break
            continue
        bad_run = 0
        out.append(seg)
        if len(out) >= MAX_SEGMENTS:
            break
    if not out:
        raise ValueError("未找到 HEVC NALU 数据")
    return b"\x00\x00\x00\x01" + b"\x00\x00\x00\x01".join(out)


def decode(wxgf_bytes, ffmpeg=FFMPEG, timeout=120):
    """WXGF 字节流 → JPEG 字节（静态图）。多帧图返回第一帧 JPEG。"""
    header_end, version, width, height = _parse_header(wxgf_bytes)
    # 不再解析 extra headers（AIGC JSON 长度格式多变易错位），
    # 从主头结束处全量扫描，靠 _extract_nalu_stream 的锚定逻辑
    # （第一个段长≥10 的真 VPS/SPS）定位真正的 HEVC 流
    stream = _extract_nalu_stream(wxgf_bytes, header_end)

    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "img.265")
        out = os.path.join(td, "out.jpg")
        with open(raw, "wb") as f:
            f.write(stream)
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "hevc",
               "-i", raw, "-frames:v", "1", "-q:v", "2", out]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(f"ffmpeg 解码失败: {r.stderr.decode('utf-8', 'replace')[:200]}")
        with open(out, "rb") as f:
            return f.read()


if __name__ == "__main__":
    import sys
    data = open(sys.argv[1], "rb").read()
    img = decode(data)
    out = sys.argv[2] if len(sys.argv) > 2 else "out.jpg"
    open(out, "wb").write(img)
    print(f"解码成功 → {out}（{len(img)} 字节）")
