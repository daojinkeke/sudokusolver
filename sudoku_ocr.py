#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sudoku_ocr.py —— 拍照 / 截图解数独

流程: 图片 -> 预处理 -> 检测 9x9 网格 -> 逐格 Tesseract 数字识别
      -> 81 字符题目(. 表示空格) -> 调用 tdoku 的 solve.exe 求解

依赖:
  - Pillow   (pip install pillow)
  - Tesseract OCR (Windows 推荐 UB-Mannheim 构建, 需含 eng.traineddata)

用法示例:
  python sudoku_ocr.py                  # 自动截屏(倒计时3秒)->识别->求解->写结果txt
  python sudoku_ocr.py --delay 5        # 截屏前倒计时 5 秒
  python sudoku_ocr.py puzzle.jpg       # 直接处理已有图片
  python sudoku_ocr.py puzzle.png --no-solve
  python sudoku_ocr.py puzzle.png --tesseract "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
  python sudoku_ocr.py --fill           # 求解后拟人点击: 点空格->点下方对应数字按钮, 自动回填
  python sudoku_ocr.py --fill --delay 5 # 截屏倒计时5秒, 回填前再给3秒切窗(可 --fill-delay 调)
  python sudoku_ocr.py --loop           # 循环刷题: 回填后自动点"下一题"->延迟->再做一轮,
                                        # 直到截屏/求解出错或按住 ESC 退出
  python sudoku_ocr.py --loop --loop-delay 4 --next-wait 10

截屏模式的文件流转:
  新截图 -> puzzle/puzzle.png; 上一张 -> puzzle_backup/puzzle.png(仅保留1张)
  结果   -> puzzle/result_YYYYmmdd_HHMMSS.txt (1-81 格编号 + 1-9 按钮坐标)

坐标复用 (数独窗口位置固定):
  首次检测的网格线与下方 9 个粉色数字按钮坐标存入 puzzle/grid_cache.json;
  之后直接复用, 仅做抽样校验。读取异常(无解/已知数过少)时自动重新检测一轮,
  也可用 --recache 强制重检。
"""

import argparse
import ctypes
import io
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PIL import Image, ImageOps

DIGITS = "123456789"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUZZLE_DIR = os.path.join(BASE_DIR, "puzzle")
BACKUP_DIR = os.path.join(PUZZLE_DIR, "puzzle_backup")
PUZZLE_PNG = os.path.join(PUZZLE_DIR, "puzzle.png")
CACHE_PATH = os.path.join(PUZZLE_DIR, "grid_cache.json")
MIN_GIVENS = 17  # 标准数独有唯一解至少需要 17 个已知数


# ----------------------------- 环境定位 -----------------------------

def find_tesseract(explicit):
    if explicit and os.path.isfile(explicit):
        return explicit
    env = os.environ.get("TESSERACT_CMD")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("tesseract")
    if found:
        return found
    for p in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
    ):
        if os.path.isfile(p):
            return p
    sys.exit(
        "错误: 未找到 tesseract。\n"
        "请先安装 Tesseract OCR (Windows: winget install UB-Mannheim.TesseractOCR),\n"
        "或用 --tesseract 参数指定 tesseract.exe 的完整路径。"
    )


# ----------------------------- 图像预处理 -----------------------------

def otsu_threshold(gray):
    """对灰度图计算 Otsu 二值化阈值 (直方图统计, 纯标准库)。"""
    hist = gray.histogram()
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    w_b = s_b = 0.0
    var_max = -1.0
    thr = 127
    for i, h in enumerate(hist):
        w_b += h
        s_b += i * h
        if w_b == 0 or w_b == total:
            continue
        w_f = total - w_b
        m_b = s_b / w_b
        m_f = (sum_all - s_b) / w_f
        v = w_b * w_f * (m_b - m_f) ** 2
        if v > var_max:
            var_max, thr = v, i
    return thr


def load_and_preprocess(path):
    """返回 (rgb, gray): 原图 RGB 与同尺寸灰度图; 过小的图先放大。"""
    img = Image.open(path).convert("RGB")
    if img.width < 1000:
        scale = max(2, round(1100 / img.width))
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    gray = ImageOps.grayscale(img)
    return img, gray


# ----------------------------- 网格检测 -----------------------------

def _score_groups(scores, thresh, gap):
    """把超过阈值的连续位置聚成组(相邻小间隙合并), 返回 [(中心, 平均强度)]。"""
    raw_groups, cur = [], []
    for i, s in enumerate(scores):
        if s > thresh:
            cur.append((i, s))
        elif cur:
            raw_groups.append(cur)
            cur = []
    if cur:
        raw_groups.append(cur)
    merged = []
    for g in raw_groups:
        if merged and g[0][0] - merged[-1][-1][0] <= gap:
            merged[-1].extend(g)
        else:
            merged.append(g)
    return [
        (sum(i for i, _ in g) / len(g), sum(s for _, s in g) / len(g))
        for g in merged
    ]


def fit_lattice(groups, pitch_tol=0.35, min_match=7):
    """把候选线拟合成 10 条等距格网线; 允许少数线被遮挡(如选中框盖住)。

    思路: 相邻候选差的小间距聚类估计线距 pitch -> 枚举锚点与线序号,
    生成完整 10 线格网并统计命中数, 取命中最多的方案。
    """
    if len(groups) < 6:
        return None
    pos = sorted(p for p, _ in groups)
    diffs = [pos[i + 1] - pos[i] for i in range(len(pos) - 1)]
    med = sorted(diffs)[len(diffs) // 2]
    small = [d for d in diffs if d < med * 1.6]
    pitch = sum(small) / len(small)
    if pitch < 6:
        return None
    tol = pitch_tol * pitch
    best = None
    for a, pa in enumerate(pos):
        for ia in range(10):
            preds = [pa + (i - ia) * pitch for i in range(10)]
            match, resid = 0, 0.0
            for pp in preds:
                d, q = min((abs(pp - q), q) for q in pos)
                if d <= tol:
                    match += 1
                    resid += d
            # 两端最多各缺 1 条边, 预测范围不能超出检测范围太远
            if preds[0] < pos[0] - pitch - tol or \
               preds[-1] > pos[-1] + pitch + tol:
                continue
            score = (match, -resid)
            if best is None or score > best[0]:
                best = (score, preds)
    return best[1] if best and best[0][0] >= min_match else None


def _colored_line_mask(rgb_small):
    """彩色线掩膜: 抓高饱和且不太暗的像素(青蓝网格线、粉色选中框)。

    青蓝线灰度值接近底色, 靠灰度分不开; 改用饱和度+明度,
    同时排除深藏青文字/侧边栏(明度低)和近白单元格底色(饱和度低)。
    """
    sw, sh = rgb_small.size
    data = rgb_small.tobytes()
    mask = bytearray(sw * sh)
    k = 0
    for i in range(0, len(data), 3):
        r, g, b = data[i], data[i + 1], data[i + 2]
        mx = r if r > g else g
        if b > mx:
            mx = b
        mn = r if r < g else g
        if b < mn:
            mn = b
        if mx > 150 and (mx - mn) * 100 > 22 * mx:
            mask[k] = 255
        k += 1
    return bytes(mask), sw, sh


def detect_grid_colored(rgb):
    """针对彩色网格线(如青蓝色)的检测, 返回全分辨率坐标 (xs, ys) 或 None。"""
    w0, h0 = rgb.size
    s = min(1.0, 1000.0 / w0)
    small = rgb.resize((max(1, round(w0 * s)), max(1, round(h0 * s))),
                        Image.BILINEAR)
    raw, w, h = _colored_line_mask(small)
    row_scores = [raw[y * w:(y + 1) * w].count(255) / w for y in range(h)]
    col_scores = [raw[x::w].count(255) / h for x in range(w)]
    gap = max(4, round((w / 9) * 0.12))

    def pick(scores):
        if not scores or max(scores) <= 0:
            return None
        thresh = max(0.12, max(scores) * 0.45)
        groups = _score_groups(scores, thresh, gap)
        return fit_lattice(groups)

    yp, xp = pick(row_scores), pick(col_scores)
    if not yp or not xp:
        return None
    # 横纵线距应近似相等(正方形格子)
    pitch_y = (yp[-1] - yp[0]) / 9
    pitch_x = (xp[-1] - xp[0]) / 9
    if not (0.72 < pitch_x / pitch_y < 1.38):
        return None
    xs = [p / s for p in xp]
    ys = [p / s for p in yp]
    return xs, ys


def detect_grid_dark(gray):
    """传统黑白照片: 全局 Otsu 后找深色网格线, 返回 (xs, ys) 或 None。"""
    w, h = gray.size
    thr = otsu_threshold(gray)
    raw = gray.point(lambda x: 0 if x < thr else 255).tobytes()
    row_scores = [raw[y * w:(y + 1) * w].count(0) / w for y in range(h)]
    col_scores = [raw[x::w].count(0) / h for x in range(w)]
    gap = max(5, w // 150)

    def pick(scores):
        groups = _score_groups(scores, 0.32, gap)
        if len(groups) < 6:
            return None
        p = fit_lattice(groups, pitch_tol=0.4, min_match=7)
        if not p:
            return None
        d = [p[i + 1] - p[i] for i in range(9)]
        return p if min(d) > 10 and min(d) / max(d) > 0.4 else None

    ys, xs = pick(row_scores), pick(col_scores)
    return (xs, ys) if xs and ys else None


def detect_grid(rgb, gray):
    """先试彩色线检测, 再试深色线检测。"""
    return detect_grid_colored(rgb) or detect_grid_dark(gray)


# ----------------------------- 数字按钮(红色表格)检测 -----------------------------

def _is_pink(r, g, b):
    """粉色按钮填充: 高 R, 中低 G/B, 明显偏红。"""
    return r > 235 and 195 <= g <= 240 and 195 <= b <= 240 \
        and r - g > 12 and r - b > 12


def detect_keypad(rgb, xs, ys):
    """检测网格下方 9 个粉色数字按钮, 返回 1-9 顺序的中心点 [(x,y)*9] 或 None。"""
    w, h = rgb.size
    pitch = (ys[-1] - ys[0]) / 9
    # 按钮带位于网格底边线下方约 0.5~2.0 格距
    ry0, ry1 = int(ys[-1] + pitch * 0.35), int(ys[-1] + pitch * 2.3)
    ry1 = min(ry1, h)
    if ry1 <= ry0:
        return None
    data = rgb.crop((0, ry0, w, ry1)).tobytes()
    rw = w
    rh = ry1 - ry0
    row_scores = []
    for y in range(rh):
        base = y * rw * 3
        cnt = 0
        for x in range(0, rw, 2):
            i = base + x * 3
            if _is_pink(data[i], data[i + 1], data[i + 2]):
                cnt += 1
        row_scores.append(cnt)
    peak = max(row_scores) if row_scores else 0
    if peak < 30:
        return None
    # 找最强连续行带(按钮纵向范围)
    band = [y for y, c in enumerate(row_scores) if c > peak * 0.35]
    groups, cur = [], []
    for y in band:
        if cur and y - cur[-1] <= 8:
            cur.append(y)
        else:
            if cur:
                groups.append(cur)
            cur = [y]
    if cur:
        groups.append(cur)
    if not groups:
        return None
    band_rows = max(groups, key=len)
    by0, by1 = band_rows[0], band_rows[-1]
    cy = ry0 + (by0 + by1) / 2
    inset = max(4, int((by1 - by0) * 0.14))
    # 列投影(避开圆角上下沿), 分割 9 个按钮
    col_scores = []
    for x in range(w):
        cnt = 0
        # 注意 data 是裁剪缓冲, 行号要用局部坐标 (减去 ry0)
        for y in range(by0 + inset, by1 - inset, 2):
            i = (y * rw + x) * 3
            if _is_pink(data[i], data[i + 1], data[i + 2]):
                cnt += 1
        col_scores.append(cnt)
    cmax = max(col_scores)
    high = bytearray(1 if c > cmax * 0.25 else 0 for c in col_scores)
    # 一维闭运算: 桥接按钮内部由数字笔画造成的短低谷(<=6px),
    # 按钮间的真间隙(~9px)保持断开
    close_w = max(3, int(pitch * 0.06))
    x = 0
    while x < w:
        if high[x]:
            x += 1
            continue
        j = x
        while j < w and not high[j]:
            j += 1
        if 0 < x and j < w and (j - x) <= close_w:
            for k in range(x, j):
                high[k] = 1
        x = j
    segs, cur = [], None
    for x, v in enumerate(high):
        if v:
            if cur is None:
                cur = [x, x]
            else:
                cur[1] = x
        elif cur is not None:
            segs.append(tuple(cur))
            cur = None
    if cur is not None:
        segs.append(tuple(cur))
    centers = [(a + b) / 2 for a, b in segs]
    if len(centers) != 9:
        return None
    pitches = [centers[i + 1] - centers[i] for i in range(8)]
    mean = sum(pitches) / 8
    cv = (sum((p - mean) ** 2 for p in pitches) / 8) ** 0.5 / mean
    span = centers[-1] - centers[0]
    grid_span = xs[-1] - xs[0]
    if cv > 0.08 or not (0.7 * grid_span < span < 1.15 * grid_span):
        return None
    return [(round(x, 1), round(cy, 1)) for x in centers]


def detect_next_button(rgb, xs, ys):
    """检测“恭喜过关”弹窗左下的“下一题”按钮, 返回中心 (x, y) 或 None。

    弹窗出现时网格中部被遮挡, 但网格坐标已由缓存/检测得到。
    实测两个纯白按钮位于网格顶边下方 0.68~0.92 格高带内:
    左为“下一题”(宽约 0.18 格宽), 右为“邀请好友答题”(更宽),
    所有尺寸阈值均按格宽/格高比例, 与分辨率无关。
    """
    w, h = rgb.size
    gw, gh = xs[-1] - xs[0], ys[-1] - ys[0]
    x0, x1 = int(xs[0]), int(xs[9])
    y0 = int(ys[0] + gh * 0.68)
    y1 = min(int(ys[0] + gh * 0.92), h)
    if y1 <= y0 or x1 <= x0:
        return None
    px = rgb.load()

    def white(x, y):
        r, g, b = px[x, y]
        return r >= 248 and g >= 248 and b >= 248

    # 行投影: 定位按钮纵向行带(标题白框在 0.5 格高以上, 被本区间排除)
    rows = [sum(1 for x in range(x0, x1) if white(x, y))
            for y in range(y0, y1)]
    peak = max(rows) if rows else 0
    if peak < gw * 0.08:
        return None
    band = [y0 + i for i, c in enumerate(rows) if c > peak * 0.3]
    groups, cur = [], []
    for y in band:
        if cur and y - cur[-1] <= 8:
            cur.append(y)
        else:
            if cur:
                groups.append(cur)
            cur = [y]
    if cur:
        groups.append(cur)
    if not groups:
        return None
    grp = max(groups, key=len)  # 按钮行
    by0, by1 = grp[0], grp[-1]
    bh = by1 - by0
    if not 0.03 * gh < bh < 0.16 * gh:
        return None
    inset = max(2, int(bh * 0.15))

    # 列投影 + 一维闭运算(桥接按钮内部文字笔画造成的短低谷)
    cols = [sum(1 for y in range(by0 + inset, by1 - inset + 1) if white(x, y))
            for x in range(x0, x1)]
    cmax = max(cols)
    high = bytearray(1 if c > cmax * 0.25 else 0 for c in cols)
    close_w = max(3, int(gw * 0.06))
    i = 0
    while i < len(high):
        if high[i]:
            i += 1
            continue
        j = i
        while j < len(high) and not high[j]:
            j += 1
        if 0 < i and j < len(high) and (j - i) <= close_w:
            for k in range(i, j):
                high[k] = 1
        i = j
    segs, seg_cur = [], None
    for i, v in enumerate(high):
        x = x0 + i
        if v:
            if seg_cur is None:
                seg_cur = [x, x]
            else:
                seg_cur[1] = x
        elif seg_cur is not None:
            segs.append(tuple(seg_cur))
            seg_cur = None
    if seg_cur is not None:
        segs.append(tuple(seg_cur))

    valid = [(a, b) for a, b in segs if 0.08 * gw < (b - a) < 0.45 * gw]
    if len(valid) != 2:
        return None
    valid.sort()
    if valid[1][0] - valid[0][1] < 0.02 * gw:  # 两按钮必须明显分离
        return None
    ax, bx = valid[0]  # 左侧即“下一题”
    return round((ax + bx) / 2.0, 1), round((by0 + by1) / 2.0, 1)


def validate_cached_grid(rgb, xs, ys):
    """抽样校验缓存的网格线上是否仍有彩色(青蓝/粉)线条, 防止界面错位。"""
    px = rgb.load()

    def colored(x, y):
        r, g, b = px[int(x), int(y)]
        mx, mn = max(r, g, b), min(r, g, b)
        return mx > 150 and (mx - mn) * 100 > 22 * mx

    def coverage(points):
        hits = sum(1 for p in points if colored(*p))
        return hits / len(points) if points else 0

    row_pts, col_pts = [], []
    for k in (0, 3, 6, 9):
        y = ys[k]
        row_pts += [(x, y + d) for x in range(int(xs[0]), int(xs[-1]), 3)
                    for d in (-1, 1)]
        x = xs[k]
        col_pts += [(x + d, yy) for yy in range(int(ys[0]), int(ys[-1]), 3)
                    for d in (-1, 1)]
    return coverage(row_pts) > 0.4 and coverage(col_pts) > 0.4


# ----------------------------- 坐标缓存 -----------------------------

def load_cache(path=CACHE_PATH):
    """读取网格/按钮坐标缓存; 损坏或字段缺失时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        xs, ys = data["grid"]["xs"], data["grid"]["ys"]
        if len(xs) == 10 and len(ys) == 10:
            return data
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def save_cache(size, xs, ys, keypad, next_button=None, path=CACHE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "image_size": list(size),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "grid": {"xs": [round(v, 2) for v in xs],
                 "ys": [round(v, 2) for v in ys]},
        "keypad": keypad,
        "next_button": list(next_button) if next_button else None,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def update_cache_next_button(btn, image_size, path=CACHE_PATH):
    """把实时检测到的“下一题”坐标合并写入已有缓存(保留网格/按钮坐标)。

    坐标按缓存基准分辨率存储, 与 keypad 一致; 缓存不存在时忽略。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    base = data.get("image_size") or list(image_size)
    sx, sy = base[0] / image_size[0], base[1] / image_size[1]
    data["next_button"] = [round(btn[0] * sx, 1), round(btn[1] * sy, 1)]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def fallback_grid(gray):
    """检测失败时的退化方案: 按深色内容包围盒 9x9 等分。"""
    thr = otsu_threshold(gray)
    dark = gray.point(lambda x: 255 if x < thr else 0)
    box = dark.getbbox()
    if box:
        x0, y0, x1, y1 = box
    else:
        x0, y0, x1, y1 = 0, 0, gray.size[0], gray.size[1]
    xs = [x0 + (x1 - x0) * c / 9 for c in range(10)]
    ys = [y0 + (y1 - y0) * r / 9 for r in range(10)]
    return xs, ys


# ----------------------------- 切格与识别 -----------------------------

def cell_images(gray, xs, ys, debug_dir=None):
    """切出 81 个格子: 局部二值化 -> 抠出深色字形 -> 居中放到白底方图。

    返回 [(idx, PIL.Image 或 None)], None 表示空白格。
    """
    cells = []
    for r in range(9):
        for c in range(9):
            idx = r * 9 + c
            x0, x1 = xs[c], xs[c + 1]
            y0, y1 = ys[r], ys[r + 1]
            cw, ch = x1 - x0, y1 - y0
            mx, my = cw * 0.15, ch * 0.15
            crop = gray.crop((int(x0 + mx), int(y0 + my),
                              int(x1 - mx), int(y1 - my)))
            thr = otsu_threshold(crop)
            bin_cell = crop.point(lambda x: 0 if x < thr else 255)  # 深色字形
            dark_ratio = bin_cell.tobytes().count(0) / (
                bin_cell.width * bin_cell.height
            )
            if dark_ratio < 0.015:
                cells.append((idx, None))  # 空白格
                continue
            # 取字形包围盒, 去掉残余边框/噪点
            ink_box = ImageOps.invert(bin_cell).getbbox()
            glyph = bin_cell.crop(ink_box) if ink_box else bin_cell
            gw, gh = glyph.size
            # 残留的贯穿线(宽或高占满格子)不可信, 当空格处理
            if gw > crop.width * 0.92 or gh > crop.height * 0.92:
                cells.append((idx, None))
                continue
            # 居中放到 1.8 倍边长的白底方图上(留白充足 Tesseract 才稳定)
            side = int(max(gw, gh) * 1.8)
            canvas = Image.new("L", (side, side), 255)
            canvas.paste(glyph, ((side - gw) // 2, (side - gh) // 2))
            scale = max(1, int(160 / side) + 1)
            big = canvas.resize((side * scale, side * scale), Image.LANCZOS)
            if debug_dir:
                big.save(os.path.join(debug_dir, f"cell_{idx:02d}_r{r + 1}c{c + 1}.png"))
            cells.append((idx, big))
    return cells


def ocr_one(idx, img, tess_cmd):
    """对单格调用 tesseract, 依次尝试多种 PSM, 返回识别到的数字或 None。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    for psm in (8, 13, 6, 10):  # 单词 -> 原始行 -> 文本块 -> 单字符(依次兜底)
        cmd = [
            tess_cmd, "stdin", "stdout",
            "--psm", str(psm),
            "-c", "tessedit_char_whitelist=123456789",
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        p = subprocess.run(
            cmd, input=png, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        text = p.stdout.decode("utf-8", "ignore")
        for ch in text:
            if ch in DIGITS:
                return idx, ch
    return idx, None


def recognize(cells, tess_cmd):
    """并发识别非空格, 返回长度 81 的列表(元素为数字或 '.')。"""
    board = ["."] * 81
    todo = [(i, im) for i, im in cells if im is not None]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(ocr_one, i, im, tess_cmd) for i, im in todo]
        for f in futures:
            idx, digit = f.result()
            if digit:
                board[idx] = digit
    return board


# ----------------------------- 求解与展示 -----------------------------

def run_solver(puzzle, solver_path):
    if not os.path.isfile(solver_path):
        sys.exit(f"错误: 找不到求解器 {solver_path}")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    p = subprocess.run(
        [solver_path, "1"], input=(puzzle + "\n").encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags,
    )
    out = p.stdout.decode("utf-8", "ignore").strip()
    if not out:
        return None, p.stderr.decode("utf-8", "ignore").strip()
    line = out.splitlines()[-1].strip()
    parts = line.split(":")
    if len(parts) >= 3:  # tdoku solve 示例格式: puzzle:count:solution
        return parts[-1][:81], parts[-2]
    if len(parts[0]) >= 81:  # 其它直接输出 81 字符的求解器
        return parts[0][:81], None
    return None, line


def format_grid(s):
    out = []
    for r in range(9):
        row = [ch if ch in DIGITS else "." for ch in s[r * 9:(r + 1) * 9]]
        out.append(
            " ".join(row[:3]) + " | " + " ".join(row[3:6]) + " | " + " ".join(row[6:9])
        )
        if r in (2, 5):
            out.append("------+-------+------")
    return "\n".join(out)


# ----------------------------- 截屏与文件流转 -----------------------------

def rotate_to_backup():
    """容量为 1 的队列: 删除备份槽里的旧 puzzle.png, 再把当前图移入。

    只操作固定槽位 puzzle.png; backup 目录里的其他文件(如 puzzle.jpg)保留。
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.isfile(PUZZLE_PNG):
        return False
    slot = os.path.join(BACKUP_DIR, "puzzle.png")
    if os.path.isfile(slot):
        os.remove(slot)
    shutil.move(PUZZLE_PNG, slot)
    return True


def capture_screen(delay, save_path):
    """倒计时后全屏截屏, 直接落盘到 save_path(不经过临时目录)。"""
    from PIL import ImageGrab
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    for i in range(delay, 0, -1):
        print(f"\r请切换到数独窗口, {i} 秒后截屏...", end="", flush=True)
        time.sleep(1)
    if delay:
        print("\r", end=" " * 30 + "\r")
    shot = ImageGrab.grab(all_screens=True)
    shot.save(save_path)
    return save_path


def _numbered_lines(s):
    """把 81 字符串渲染成带 01-81 编号的 9 行文本。"""
    lines = []
    for r in range(9):
        seg = s[r * 9:(r + 1) * 9]
        lines.append("  ".join(f"{r * 9 + c + 1:02d}:{ch}" for c, ch in enumerate(seg)))
    return "\n".join(lines)


def write_result_txt(puzzle, solution=None, count=None, keypad=None,
                     out_dir=PUZZLE_DIR):
    """写出带格子编号的结果 txt, 返回文件路径。"""
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"result_{stamp}.txt")
    parts = [
        "数独识别与求解结果",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "题目 (编号 01-81, . 表示空格):",
        _numbered_lines(puzzle),
        "",
        f"题目字符串: {puzzle}",
    ]
    if solution:
        parts += [
            "",
            f"答案 (解的数量: {count}):" if count else "答案:",
            _numbered_lines(solution),
            "",
            f"答案字符串: {solution}",
        ]
    else:
        parts += ["", "未求解 (--no-solve) 或求解失败。"]
    if keypad:
        parts += [
            "",
            "下方数字按钮中心坐标 (屏幕像素, 左上角为原点, 顺序 1-9):",
            "  ".join(f"{i + 1}:({x:.0f},{y:.0f})"
                      for i, (x, y) in enumerate(keypad)),
        ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")
    return path


# ----------------------------- 拟人点击回填 (仅 Windows, 纯标准库 ctypes) -----------------------------
#
# 坐标约定: xs/ys/keypad 均为截图(ImageGrab all_screens=True)像素坐标,
# 其原点对应 Windows 虚拟屏幕原点 (SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN),
# 单主屏时该原点就是 (0,0), 截图坐标可直接当屏幕坐标用。
# 输入经 Win32 SendInput 注入(系统级真实输入), 与 C# 版 HumanInputSimulator
# 同一套拟人思路: 贝塞尔弧形轨迹 + 高斯抖动 + 偶发过冲回拉 + 随机按压/停顿。

if os.name == "nt":
    from ctypes import wintypes

    class _MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class _InputUnion(ctypes.Union):
        _fields_ = [("mi", _MouseInput),
                    ("ki", ctypes.c_byte * 24),
                    ("hi", ctypes.c_byte * 8)]

    class _Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]

    class _Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    _user32 = ctypes.windll.user32

    _INPUT_MOUSE = 0
    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _MOUSEEVENTF_VIRTUALDESK = 0x4000
    _SM_XVIRTUALSCREEN = 76
    _SM_YVIRTUALSCREEN = 77
    _SM_CXVIRTUALSCREEN = 78
    _SM_CYVIRTUALSCREEN = 79
    _VK_ESCAPE = 0x1B


class HumanClicker:
    """拟人鼠标: 传入截图坐标即可, 内部换算到虚拟屏幕归一化坐标后 SendInput。"""

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("拟人点击回填仅支持 Windows")
        # 关键: 让 GetSystemMetrics 返回物理像素, 与 ImageGrab.grab() 对齐;
        # 否则 DPI 缩放下 _vw/_vh 是逻辑像素, ImageGrab 是物理像素, 归一化坐标偏低.
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass  # 已被系统设置或更高层进程调用时可能抛错, 忽略
        self._rng = random.Random()
        self._vx = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        self._vy = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        self._vw = _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
        self._vh = _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
        print(f"[HumanClicker] 虚拟屏幕 origin=({self._vx},{self._vy}) size={self._vw}x{self._vh} (DPI-aware)")
        # —— 拟人参数(像素/毫秒), 保守取值保证落点准确, 可自行调快 ——
        self.step_min_ms = 4            # 轨迹每步最小停顿
        self.step_max_ms = 12           # 轨迹每步最大停顿
        self.step_len_px = (18.0, 34.0)  # 每步移动像素范围
        self.jitter_px = 3.0            # 轨迹高斯抖动强度(接近目标衰减)
        self.overshoot_chance = 0.15    # 越过目标再回拉的概率
        self.click_before = (140, 55, 50, 420)   # 点击前反应 (均值,标准差,下限,上限)
        self.click_hold = (70, 28, 28, 200)       # 按下保持时长
        self.cell_to_key_gap = (110, 45, 50, 300)  # 点格 -> 点数字按钮之间
        self.round_gap = (150, 60, 70, 520)       # 填完一格 -> 下一格

    # ---------- 底层注入 ----------

    def _send_mouse(self, flags, dx=0, dy=0):
        inp = _Input(type=_INPUT_MOUSE)
        inp.u.mi = _MouseInput(
            dx=dx, dy=dy, mouseData=0, dwFlags=flags, time=0,
            dwExtraInfo=ctypes.pointer(wintypes.ULONG(0)))
        _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def _send_move(self, image_x, image_y):
        """把截图坐标换算成虚拟桌面 0~65535 归一化坐标并移动。"""
        nx = int((image_x + self._vx) * 65535.0 / self._vw)
        ny = int((image_y + self._vy) * 65535.0 / self._vh)
        self._send_mouse(
            _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
            nx, ny)

    def _cursor_pos(self):
        """当前光标位置, 返回截图坐标系下的像素坐标。"""
        pt = _Point()
        _user32.GetCursorPos(ctypes.byref(pt))
        return pt.x - self._vx, pt.y - self._vy

    # ---------- 随机与停顿 ----------

    def _gauss(self):
        """Box-Muller 标准正态分布。"""
        u1 = 1.0 - self._rng.random()
        u2 = 1.0 - self._rng.random()
        return math.sqrt(-2.0 * math.log(u1)) * math.sin(2.0 * math.pi * u2)

    def _gauss_ms(self, spec):
        mean, std, lo, hi = spec
        ms = int(round(mean + self._gauss() * std))
        time.sleep(max(lo, min(hi, ms)) / 1000.0)

    def _step_pause(self):
        ms = self._rng.randint(self.step_min_ms, self.step_max_ms)
        if self._rng.random() < 0.04:  # 4% 概率插入一次“犹豫”
            ms += self._rng.randint(30, 110)
        time.sleep(ms / 1000.0)

    @staticmethod
    def _ease_in_out_quad(t):
        if t < 0.5:
            return 2.0 * t * t
        return 1.0 - (-2.0 * t + 2.0) ** 2 / 2.0

    # ---------- 高层手势 ----------

    def move_to(self, target_x, target_y):
        """贝塞尔弧形轨迹 + 高斯抖动移动到截图坐标 (target_x, target_y)。"""
        start_x, start_y = self._cursor_pos()
        dx, dy = target_x - start_x, target_y - start_y
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < 1.0:
            self._send_move(target_x, target_y)
            return

        aim_x, aim_y = target_x, target_y
        overshoot = self._rng.random() < self.overshoot_chance
        if overshoot:  # 终点越过目标 1.5%~4%, 之后回拉
            ratio = 0.015 + self._rng.random() * 0.025
            aim_x = target_x + dx * ratio
            aim_y = target_y + dy * ratio

        # 控制点在起点-终点中垂线方向随机偏移 ±10%, 形成弧形
        mid_x, mid_y = (start_x + aim_x) / 2.0, (start_y + aim_y) / 2.0
        arc = (self._rng.random() * 2.0 - 1.0) * 0.10 * dist
        ctrl_x = mid_x + (-dy / dist) * arc
        ctrl_y = mid_y + (dx / dist) * arc

        lo, hi = self.step_len_px
        steps = int(round(dist / self._rng.uniform(lo, hi)))
        steps = max(14, min(55, steps))

        for i in range(1, steps + 1):
            t = self._ease_in_out_quad(i / steps)
            mt = 1.0 - t
            bx = mt * mt * start_x + 2.0 * mt * t * ctrl_x + t * t * aim_x
            by = mt * mt * start_y + 2.0 * mt * t * ctrl_y + t * t * aim_y
            scale = self.jitter_px * (1.0 - 0.75 * t)  # 越接近目标抖动越小
            self._send_move(bx + self._gauss() * scale,
                            by + self._gauss() * scale)
            self._step_pause()

        if overshoot:
            corr_steps = 3 + self._rng.randint(0, 3)
            for i in range(1, corr_steps + 1):
                t = self._ease_in_out_quad(i / corr_steps)
                cx2 = aim_x + (target_x - aim_x) * t + self._gauss() * self.jitter_px * 0.25
                cy2 = aim_y + (target_y - aim_y) * t + self._gauss() * self.jitter_px * 0.25
                self._send_move(cx2, cy2)
                time.sleep(self._rng.randint(8, 20) / 1000.0)

        self._send_move(target_x, target_y)  # 精确落点, 保证点击准确

    def click(self, image_x, image_y):
        """移动到截图坐标后, 随机反应停顿 + 随机按压时长地左键点击。"""
        self.move_to(image_x, image_y)
        self._gauss_ms(self.click_before)
        self._send_mouse(_MOUSEEVENTF_LEFTDOWN)
        self._gauss_ms(self.click_hold)
        self._send_mouse(_MOUSEEVENTF_LEFTUP)
        time.sleep(self._rng.randint(10, 60) / 1000.0)

    def fill_sudoku(self, puzzle, solution, xs, ys, keypad):
        """按业务顺序回填: 每个原题空格 -> 点该格 -> 点下方对应数字按钮。

        puzzle/solution 均为 81 字符串; xs/ys 为 10 条网格线坐标;
        keypad 为 1-9 顺序(从左到右)的按钮中心 [(x,y)*9]。
        返回实际填入的格数; 中途按 ESC 中止时返回已填数。
        """
        empties = [i for i, ch in enumerate(puzzle) if ch not in DIGITS]
        total = len(empties)
        if not empties:
            print("没有需要填写的空格。")
            return 0
        # —— 归位: 鼠标直线(不走贝塞尔)移到第一个空格中心 ——
        # 避免起始位置在屏幕底部时, 贝塞尔弧形轨迹的中间点下探到任务栏区域;
        # 也让后续 trajectory 的起点接近目标, 自然轨迹长度短、更稳定。
        first_row, first_col = divmod(empties[0], 9)
        first_cell_x = (xs[first_col] + xs[first_col + 1]) / 2.0
        first_cell_y = (ys[first_row] + ys[first_row + 1]) / 2.0
        self._send_move(first_cell_x, first_cell_y)  # 直接 SendInput 瞬移, 无轨迹
        time.sleep(0.4)

        for n, idx in enumerate(empties, 1):
            if _user32.GetAsyncKeyState(_VK_ESCAPE) & 0x8000:
                print(f"\n已按 ESC 中止回填, 完成 {n - 1}/{total} 格。")
                return n - 1
            row, col = divmod(idx, 9)
            cell_x = (xs[col] + xs[col + 1]) / 2.0
            cell_y = (ys[row] + ys[row + 1]) / 2.0
            self.click(cell_x, cell_y)              # 1) 点空格
            self._gauss_ms(self.cell_to_key_gap)
            digit = int(solution[idx])              # 2) 答案数字
            key_x, key_y = keypad[digit - 1]        #    从左到右 1-9
            self.click(key_x, key_y)                # 3) 点对应数字按钮
            print(f"\r回填进度: {n}/{total} (第{row + 1}行第{col + 1}列填 {digit})",
                  end="", flush=True)
            if n < total:
                self._gauss_ms(self.round_gap)
        print()
        return total


# ----------------------------- 主流程 -----------------------------

def acquire_coords(rgb, gray, force=False, persist=True):
    """获取网格与按钮坐标。

    force=False: 优先用 JSON 缓存(分辨率一致 + 抽样校验通过), 界面位置固定
                 时可跳过检测, 显著加快速度;
    force=True : 强制重新检测; persist=True 时成功后更新缓存。
    返回 (xs, ys, keypad, next_button, used_cache); 检测失败返回 None。
    """
    if not force:
        cache = load_cache()
        if cache and cache.get("image_size"):
            cw, ch = cache["image_size"]
            nw, nh = rgb.size
            sx, sy = nw / cw, nh / ch
            # 允许截图边缘 1px 级别的轻微尺寸差异, 坐标等比缩放
            if 0.97 < sx < 1.03 and 0.97 < sy < 1.03:
                xs = [v * sx for v in cache["grid"]["xs"]]
                ys = [v * sy for v in cache["grid"]["ys"]]
                if validate_cached_grid(rgb, xs, ys):
                    kp = cache.get("keypad")
                    if kp:
                        kp = [[x * sx, y * sy] for x, y in kp]
                    nb = cache.get("next_button")
                    if nb:
                        nb = [nb[0] * sx, nb[1] * sy]
                    return xs, ys, kp, nb, True
    grid = detect_grid(rgb, gray)
    if not grid:
        return None
    xs, ys = grid
    keypad = detect_keypad(rgb, xs, ys)
    if persist:
        save_cache(rgb.size, xs, ys, keypad)
    return xs, ys, keypad, None, False


def esc_down():
    """ESC 当前是否按住(任意时刻的紧急中止开关)。"""
    if os.name != "nt":
        return False
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000)


def interruptible_sleep(seconds):
    """可被 ESC 打断的等待; 返回 False 表示用户按了 ESC。"""
    end = time.time() + seconds
    while time.time() < end:
        if esc_down():
            return False
        time.sleep(min(0.1, end - time.time()))
    return True


def recognize_round(image_path, tess_cmd, args, persist_cache):
    """单轮识别: 读图 -> (缓存/重检)坐标 -> OCR -> 求解。

    内置“读取错误再重新读一次”的两轮重试, 但只做识别, 不做点击;
    返回 (puzzle, solution, info, xs, ys, keypad, next_button,
          used_cache, rgb, gray)。
    """
    if args.debug_dir:
        os.makedirs(args.debug_dir, exist_ok=True)

    rgb, gray = load_and_preprocess(image_path)

    xs = ys = keypad = next_button = None
    used_cache = False
    puzzle = solution = info = None
    for attempt in (1, 2):
        force_redetect = (attempt == 2) or (args.recache and attempt == 1)
        coords = acquire_coords(rgb, gray, force=force_redetect,
                                persist=persist_cache)
        if coords:
            xs, ys, keypad, next_button, used_cache = coords
        elif attempt == 1:
            print("提示: 未检测到完整网格线, 改用等分方式 (建议把图片裁到只含网格)",
                  file=sys.stderr)
            xs, ys = fallback_grid(gray)
            keypad, next_button, used_cache = None, None, False
        else:
            break  # 重检仍失败, 沿用第一轮结果

        cells = cell_images(gray, xs, ys,
                            args.debug_dir if attempt == 1 else None)
        puzzle = "".join(recognize(cells, tess_cmd))
        givens = sum(ch in DIGITS for ch in puzzle)

        solution, info = (None, None)
        if not args.no_solve:
            solution, info = run_solver(puzzle, args.solver)

        ok = givens >= MIN_GIVENS and (args.no_solve or bool(solution))
        if ok or attempt == 2:
            break
        # 第一轮坐标不是本轮新检测出来的, 才值得重检
        if used_cache or not coords:
            reason = "题目无解" if givens >= MIN_GIVENS \
                else f"已知数仅 {givens} 个 (<{MIN_GIVENS})"
            print(f"提示: 复用坐标读取异常({reason}), 重新检测网格并再读一次...",
                  file=sys.stderr)
            continue
        break

    if args.debug_dir:
        from PIL import ImageDraw
        overlay = rgb.copy()
        d = ImageDraw.Draw(overlay)
        for x in xs:
            d.line([(x, ys[0]), (x, ys[-1])], fill=(255, 0, 0), width=3)
        for y in ys:
            d.line([(xs[0], y), (xs[-1], y)], fill=(255, 0, 0), width=3)
        if keypad:
            for i, (kx, ky) in enumerate(keypad, 1):
                d.ellipse((kx - 18, ky - 18, kx + 18, ky + 18),
                          outline=(255, 0, 0), width=3)
                d.text((kx + 22, ky - 12), str(i), fill=(255, 0, 0))
        overlay.save(os.path.join(args.debug_dir, "grid_overlay.png"))

    return (puzzle, solution, info, xs, ys, keypad, next_button,
            used_cache, rgb, gray)


def wait_next_button(xs, ys, base_size, timeout=8.0):
    """回填后轮询截屏等待“恭喜过关”弹窗, 返回 (x, y, 来源) 或 None。

    优先实时检测“下一题”并把坐标写入缓存; 超时未检出时回退到缓存坐标;
    用户按 ESC 或既没检出也无缓存时返回 None。
    """
    from PIL import ImageGrab
    deadline = time.time() + timeout
    shot = None
    while time.time() < deadline:
        if esc_down():
            return None
        shot = ImageGrab.grab(all_screens=True)
        sxs = [x * shot.width / base_size[0] for x in xs]
        sys_ys = [y * shot.height / base_size[1] for y in ys]
        btn = detect_next_button(shot, sxs, sys_ys)
        if btn:
            update_cache_next_button(btn, shot.size)
            return btn[0], btn[1], "实时检测"
        time.sleep(0.8)
    cache = load_cache()
    if shot is not None and cache and cache.get("next_button"):
        cw, ch = cache["image_size"]
        cx, cy = cache["next_button"]
        return (cx * shot.width / cw, cy * shot.height / ch, "缓存复用")
    return None


def main_loop(args, tess_cmd):
    """循环刷题: 截屏->识别->求解->回填->点“下一题”->等待新题, 直到出错或 ESC。"""
    print("循环刷题模式: 每轮自动 截屏→识别→求解→回填→点“下一题”;")
    print("任意时刻按住 ESC 可安全退出; 截屏/求解失败时自动结束循环。\n")
    clicker = HumanClicker()
    round_no = 1
    while True:
        if esc_down():
            print("检测到 ESC, 退出循环。")
            break
        print(f"================ 第 {round_no} 轮 ================")
        try:
            if round_no == 1:
                backed_up = rotate_to_backup()
                image_path = capture_screen(args.delay, PUZZLE_PNG)
                if backed_up:
                    print(f"上一张截图已移入 {BACKUP_DIR}")
            else:
                image_path = capture_screen(0, PUZZLE_PNG)  # 后续轮次不倒计时
            print(f"截图已保存: {image_path}")
            result = recognize_round(image_path, tess_cmd, args,
                                     persist_cache=True)
        except Exception as e:
            print(f"截屏或识别出错, 结束循环: {e}")
            break

        (puzzle, solution, info, xs, ys, keypad, next_button,
         used_cache, rgb, gray) = result

        print(f"题目 (坐标来源: {'缓存坐标' if used_cache else '实时检测'}):")
        print(format_grid(puzzle))

        if not solution:
            print(f"\n本轮求解失败(求解器: {info}), 可能已不在答题界面, "
                  f"结束循环。")
            break
        if not keypad or len(keypad) != 9:
            print("\n未获取到下方 9 个数字按钮的坐标, 结束循环。"
                  "可加 --recache 下轮强制重检。")
            break

        print("\n求解结果:")
        print(format_grid(solution))
        write_result_txt(puzzle, solution, info, keypad, PUZZLE_DIR)

        empties = sum(ch not in DIGITS for ch in puzzle)
        if round_no == 1:
            print(f"\n即将拟人回填 {empties} 个空格, 请把数独窗口保持在前台。")
            if not interruptible_sleep(args.fill_delay):
                print("ESC 退出。")
                break
        done = clicker.fill_sudoku(puzzle, solution, xs, ys, keypad)
        if done != empties:
            print("\n回填未完成 (ESC 中止), 结束循环。")
            break

        print(f"\n第 {round_no} 轮答案已填完, 等待“恭喜过关”弹窗 "
              f"(最多 {args.next_wait:.0f} 秒)...")
        btn = wait_next_button(xs, ys, rgb.size, args.next_wait)
        if btn is None:
            print("未找到“下一题”按钮且无缓存坐标(可能未过关或界面变化), "
                  "结束循环。")
            break
        bx, by, source = btn
        print(f"点击“下一题” ({bx:.0f}, {by:.0f}, {source})")
        time.sleep(0.6)
        clicker.click(bx, by)

        print(f"等待 {args.loop_delay:.1f} 秒加载下一题... (ESC 退出)")
        if not interruptible_sleep(args.loop_delay):
            print("ESC 退出。")
            break
        round_no += 1

    print(f"\n循环结束, 共完成 {round_no - 1} 轮。")


def main():
    ap = argparse.ArgumentParser(description="数独图片 OCR 识别并用 tdoku 求解")
    ap.add_argument("image", nargs="?",
                    help="数独题目图片; 不提供则自动全屏截屏存入 puzzle/puzzle.png")
    ap.add_argument("--delay", type=int, default=3,
                    help="截屏模式倒计时秒数 (默认 3)")
    ap.add_argument("--tesseract", help="tesseract 可执行文件完整路径")
    ap.add_argument(
        "--solver",
        default=os.path.join(BASE_DIR, "tdoku-master", "solve.exe"),
        help="solve.exe 路径 (默认 ./tdoku-master/solve.exe)",
    )
    ap.add_argument("--no-solve", action="store_true", help="只识别, 不调用求解器")
    ap.add_argument("--raw", action="store_true", help="只输出 81 字符串, 方便管道使用")
    ap.add_argument("--txt", action="store_true",
                    help="文件模式下也输出带编号的结果 txt (截屏模式默认输出)")
    ap.add_argument("--debug-dir", help="把切出的格子图保存到该目录, 便于排查误识别")
    ap.add_argument("--recache", action="store_true",
                    help="忽略坐标缓存, 强制重新检测网格与按钮")
    ap.add_argument("--fill", action="store_true",
                    help="求解后拟人点击回填: 依次点空格再点下方对应数字按钮 (仅 Windows)")
    ap.add_argument("--fill-delay", type=int, default=3,
                    help="--fill 开始前切换窗口的倒计时秒数 (默认 3)")
    ap.add_argument("--loop", action="store_true",
                    help="循环刷题: 回填后自动点“下一题”并进入下一轮, "
                         "直到截屏/求解出错或按 ESC (仅截屏模式)")
    ap.add_argument("--loop-delay", type=float, default=3.0,
                    help="--loop 点击下一题后等待新题加载的秒数 (默认 3)")
    ap.add_argument("--next-wait", type=float, default=8.0,
                    help="--loop 等待“恭喜过关”弹窗出现的最长秒数 (默认 8)")
    args = ap.parse_args()

    if args.loop:
        if os.name != "nt":
            sys.exit("错误: --loop 仅支持 Windows。")
        if args.image is not None:
            sys.exit("错误: --loop 只能在截屏模式使用 (不要提供图片参数)。")
        if args.no_solve:
            sys.exit("错误: --loop 需要求解, 不能与 --no-solve 同时使用。")
        main_loop(args, find_tesseract(args.tesseract))
        return

    capture_mode = args.image is None
    if capture_mode:
        # 队列轮换: 上一张 puzzle.png -> puzzle_backup(仅保留1张)
        backed_up = rotate_to_backup()
        image_path = capture_screen(args.delay, PUZZLE_PNG)
        if backed_up:
            print(f"上一张截图已移入 {BACKUP_DIR}")
        print(f"截图已保存: {image_path}")
    else:
        if not os.path.isfile(args.image):
            sys.exit(f"错误: 找不到图片 {args.image}")
        image_path = args.image

    tess_cmd = find_tesseract(args.tesseract)

    (puzzle, solution, info, xs, ys, keypad, next_button, used_cache,
     rgb, gray) = recognize_round(image_path, tess_cmd, args,
                                  persist_cache=capture_mode)

    if args.raw:
        print(puzzle)
    else:
        src = "缓存坐标" if used_cache else "实时检测"
        print(f"识别到的题目 (坐标来源: {src}):")
        print(format_grid(puzzle))
        print(f"\n81 字符串: {puzzle}")
        if keypad:
            print("数字按钮坐标: " + "  ".join(
                f"{i + 1}=({x:.0f},{y:.0f})"
                for i, (x, y) in enumerate(keypad)))

    # 截屏模式默认写结果 txt; 文件模式需 --txt
    if capture_mode or args.txt:
        txt_dir = PUZZLE_DIR if capture_mode \
            else os.path.dirname(os.path.abspath(image_path))
        txt_path = write_result_txt(puzzle, solution, info, keypad, txt_dir)
        print(f"结果已写入: {txt_path}")

    if not args.no_solve and not solution:
        print(f"\n求解失败 (可能识别有误导致题目无解)。求解器输出: {info}",
              file=sys.stderr)
        sys.exit(1)

    if solution and not args.raw:
        tail = f" (解的数量: {info})" if info else ""
        print(f"\n求解结果{tail}:")
        print(format_grid(solution))
    elif solution:
        print(solution)

    # ---- 拟人点击回填: 点原题空格 -> 点下方红色按钮行的对应数字 ----
    if args.fill:
        if os.name != "nt":
            sys.exit("错误: --fill 仅支持 Windows。")
        if args.no_solve or not solution:
            sys.exit("错误: --fill 需要先求解出答案 (不能与 --no-solve 同时使用)。")
        if not keypad or len(keypad) != 9:
            sys.exit(
                "错误: 未获取到下方 9 个数字按钮的坐标, 无法回填。\n"
                "请确认截图包含网格下方的红色(粉色)数字按钮行, "
                "或加 --recache 强制重新检测后重试。")

        empties = sum(ch not in DIGITS for ch in puzzle)
        print(f"\n即将拟人回填 {empties} 个空格。请把数独窗口切到前台, "
              f"回填过程中按 ESC 可中止。")
        for i in range(args.fill_delay, 0, -1):
            print(f"\r{i} 秒后开始点击...", end="", flush=True)
            time.sleep(1)
        print("\r" + " " * 24 + "\r", end="")

        done = HumanClicker().fill_sudoku(puzzle, solution, xs, ys, keypad)
        if done == empties:
            print(f"回填完成, 共填入 {done} 个数字。")
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
