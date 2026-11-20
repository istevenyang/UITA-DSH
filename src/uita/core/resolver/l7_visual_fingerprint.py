"""
VisualFingerprintResolver — L3.8 视觉指纹匹配（P4 新增，2026-07-14 升级为特征匹配）。

计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §3.3（P4 原始实现）
          docs/plans/archive/03-resolver-rosetta-实施纪要.md（本次升级）
参考设计：docs/uita-framework.md §2.7（视觉指纹系统）

本机指纹缓存目录结构：resources/visual_fingerprints/<app>_<platform>/
  fingerprint_index.json   — 元数据（命中次数/捕获时间/DPI/过期天数）
  <description>.png        — 指纹图片本体（本机缓存，不入库，见 R17/.gitignore）

匹配算法（2026-07-14 升级为三级降级，2026-08-03 新增④级深色模式兜底，
见计划文档 docs/plans/20260803190000-l3-8-visual-fingerprint-theme-invariant-
gradient-matching.md）：
  统一转灰度（消除深色/浅色主题切换的颜色差异）
    ① ORB 特征匹配（默认，速度快）
    ② SIFT 特征匹配（① 特征点不足时降级，尺度/旋转不变性更强）
    ③ matchTemplate 灰度模板匹配（①②均失败时的兜底，适合纯色/低纹理图标）
    ④ 主题不变边缘/梯度结构匹配（③置信度不足时的最终兜底，见下方专门说明）
  ①② 均基于 knnMatch + Lowe's ratio test 筛选 good matches，再用 RANSAC 求
  Homography，把模板四角变换到搜索图坐标算出 bbox；inlier 比例作为置信度。
  这套流程天然具备一定尺度不变性（ORB 金字塔层）/完全尺度旋转不变性（SIFT），
  覆盖常见 DPI 差异（1x/2x/3x）场景，不再需要 §2.7 原方案里"记录 dpi_scale +
  resize 对齐"的额外步骤。

深色模式兜底（④级，2026-08-03新增）：①②③统一转灰度只能消除"真反转"这一种
深色模式变体，遇到"独立调色板"（深色背景+暗前景，仅靠明度微差区分层次，即
用户反馈"深色模式也有可能是暗背景+暗前景/文字"）时灰度绝对值整体偏移，③的
`matchTemplate`（灰度相关系数）会明显跌破阈值。④在③失败后，把模板/搜索图
都转换成"CLAHE 局部对比度增强 + Sobel 梯度幅值"的主题不变表示
（`_theme_invariant_map()`），再用同一套`matchTemplate`逻辑重试一次：
  - CLAHE 放大局部弱对比度，应对"独立调色板"场景前景仅比背景亮一点点的低
    对比度问题；
  - Sobel 梯度幅值 `sqrt(gx²+gy²)` 对"真反转"（`255-I`）天然不敏感（梯度
    平方后符号被抵消），只反映边缘位置/强度、不反映哪边亮哪边暗，因此同时
    覆盖"局部不变"（背景色变、前景色不变）场景（形状/边缘位置不变即可命中）。
  ④与③同阈值（`self._match_threshold`），`match_algo="gradient"`，归入
  `_match_algo_tier()` 的 tier=0（与③同级）；仅在③置信度不足时才尝试，不
  影响①②③已能命中的现有场景（零回归）；④内部任何异常（如运行环境缺失
  `cv2.createCLAHE`/`cv2.Sobel` 等 API）均静默捕获退化为未命中，不向上抛出。

依赖说明（惰性 import）：cv2（opencv-python）/numpy 均为可选重依赖，模块导入
本身不需要它们已安装，只有在真正调用 match()/capture() 时才会 import；未安装
时抛出清晰的 RuntimeError，不会让整个 core.resolver 包在 import 阶段失败。
单测通过 monkeypatch `_import_cv`/`_import_cv_np` 注入假的 cv2 模块（numpy 为
真实安装的轻量依赖，不需要 mock），不依赖真实 opencv-python 安装。

坐标语义（2026-07-31 修正，见 docs/plans/
archive/03-resolver-rosetta-实施纪要.md）：`match()`
返回的坐标是**截图自身的像素坐标**，不做任何裁剪/窗口偏移/DPI 换算——那一步
（旧版曾用屏幕绝对坐标当像素偏移裁剪截图，属单位错配 bug，已删除）统一交由
调用方通过 `core/resolver/screen_coords.py::ScreenCoordConverter` 处理。

框选点击子区域（click_rect，2026-07-15 新增，见计划文档
docs/plans/archive/03-resolver-rosetta-实施纪要.md）：
  模板图整体命中后，可选按调用方传入的 click_rect（rosetta_map.json 静态声明
  字段，FlatEntry.click_rect，与 pattern 同级）计算出框选子区域在搜索图中的
  映射位置（MatchResult.sub_bbox），供 click 时优先取子区域中心
  （MatchResult.click_center）而非整体中心。本模块**不持久化、不感知
  rosetta_map**，click_rect 完全由调用方（Resolver）每次 match() 传入；
  本模块自身状态（fingerprint_index.json/capture()）不因此发生任何变化。

红框标注去除 + 点击区域自动推导（2026-08-03 新增，见计划文档
docs/plans/archive/03-resolver-rosetta-实施纪要.md）：
  部分指纹模板图是人工截图后用画图工具画一个红色矩形边框标注"应该点击的
  位置"再存成 pattern；红框像素会干扰 ORB/SIFT 特征提取与 matchTemplate
  灰度相关性计算。`_match_one_template()` 在 `imread` 后、灰度转换前，
  对模板图动态检测"中空矩形"红色描边（区别于实心红色 UI 元素）：检测到则
  `inpaint` 抹除后再送入匹配，并把框内区域作为点击子区域，**优先于**调用方
  传入的 `click_rect` 字段（视为最新标注意图）；未检测到则保持原图与
  `click_rect` 不变（零回归）。每次 `match()` 动态检测，不写回
  `pattern`/`rosetta_map.json`/本机缓存。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
_MATCH_THRESHOLD_OVERRIDE: ContextVar[Optional[float]] = ContextVar(
    "visual_fingerprint_match_threshold", default=None,
)

DEFAULT_FINGERPRINT_DIR = "resources/visual_fingerprints"
DEFAULT_STALE_AFTER_DAYS = 30.0
DEFAULT_MATCH_THRESHOLD = 0.7  # matchTemplate 灰度兜底的归一化置信度阈值（2026-07-31 由 0.85 调低）

# ── 特征匹配参数（ORB/SIFT 共用） ────────────────────────────────────────
MIN_FEATURE_POINTS = 2      # 模板/搜索图至少各检出这么多关键点才尝试 knnMatch
# Homography（4 点透视变换）数学自由度恰好是 4 组对应点——刚好 4 组匹配点时
# cv2.findHomography 几乎总能零残差拟合出一个解，此时 inlier_ratio 天然趋近
# 100%，即便这 4 组匹配本身就是噪声误配，RANSAC 的离群点剔除也就失去了统计
# 意义。2026-08-03 由 4 提升到 8（见 docs/plans/
# docs/plans/archive/03-resolver-rosetta-实施纪要.md
# 问题 2），让 min_inlier_ratio 阈值真正有过滤力。
MIN_GOOD_MATCHES = 8
LOWE_RATIO_THRESH = 0.75    # Lowe's ratio test 阈值（越小越严格）
DEFAULT_MIN_INLIER_RATIO = 0.5   # RANSAC 内点比例阈值，低于此值视为不可靠匹配

# Homography 几何合理性校验（2026-08-03 新增，见上方计划文档问题 4）：低纹理/
# 重复图案背景下，稀疏匹配点容易解出一个几何上离谱（bbox 宽高与模板原始尺寸
# 相差悬殊）但数值上"内点比例达标"的伪匹配。变换后 bbox 宽/高相对模板原始
# 宽/高的倍数必须落在 [MIN_SCALE, MAX_SCALE] 区间内，否则拒绝该结果（降级到
# 下一级算法），而不是直接采信。
HOMOGRAPHY_SANITY_MIN_SCALE = 0.3
HOMOGRAPHY_SANITY_MAX_SCALE = 3.0

# 低纹理小图无法稳定产生 ORB/SIFT 特征点，matchTemplate 又不具备尺度不变性。
# 覆盖 Windows 常见 DPI/截图倍率；1.0 已由原始模板匹配处理，不重复计算。
_TEMPLATE_MATCH_SCALES = (
    0.5, 0.67, 0.75, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0,
)

# 搜索图单边像素上限：超过时把**搜索图整体缩到该上限**（模板按同一倍率跟着缩，
# 命中坐标/尺寸按同一倍率还原），几何等比、不改变任何判定阈值。
#
# 动机（真机实测）：matchTemplate / 多尺度兜底 / 梯度匹配三级的开销都是
# "搜索图像素数 × 模板像素数"量级。2240x1480 的原生窗口截图中，单张模板最坏要
# ~2.5s（多尺度兜底 11 档占 ~2.0s），一个 description 挂 8 张指纹时一轮轮询就是
# ~20s——perf 的"页面渲染耗时"测量精度被直接打穿（点完第一帧就命中，却报 19.7s）。
# 缩到单边 <=1200 后单模板降到 ~0.6s，轮询周期回到截图耗时主导的量级。
MAX_SEARCH_SIDE = 1200

# 参与 matchTemplate 的**模板**单边下限（像素）。低于它的模板/缩放档位一律跳过：
# `TM_CCOEFF_NORMED` 在 10px 量级的近均匀小块上极易给出 0.7+ 的高分（相关系数在
# 小样本上方差小、随机波动就能冲高），表现为"页面上根本没这个元素却命中"。
# 真机实测：20x22 的 `列表页_渲染完成__4.png` 在搜索图缩到单边 1200 后再乘
# 0.5 档，得到 5x6 的模板，在**未渲染完成**的截图上命中 conf=0.73、11x11 的死块
# ——正好越过默认阈值 0.7，直接把"渲染完成"判成假绿（perf 的渲染耗时因此失真）。
MIN_MATCH_SIDE = 16

# 兜底匹配（多尺度 DPI ladder + ORB/SIFT + 梯度）的最小间隔：这三级都是"猜"的路径，
# 成本比精确尺度的模板匹配高一个量级（真机全分辨率下 8 张模板走全链 ~18s）。
# 轮询场景（perf 的"等渲染完成"）每轮都跑全链会把取样周期拖到十几秒，
# 故同一 resolver 实例内至多每 FULL_MATCH_MIN_INTERVAL_MS 走一次兜底；
# 中间那些轮只做精确尺度匹配——素材与当前窗口同尺度时它本来就能命中（真机实测
# 干净页面 conf 1.000），猜不出来也不会因为少猜几次就永远猜不出来。
FULL_MATCH_MIN_INTERVAL_MS = 1500

# ── 红框标注检测参数（2026-08-03 新增，见计划文档 docs/plans/
# docs/plans/archive/03-resolver-rosetta-实施纪要.md） ──
# HSV 红色色域分两段（0附近 + 180 附近，因Hue 环形，红色横跨两端），宽容
# 色域覆盖不同画图工具/线宽/轻微色差，不假设精确色值。
RED_HUE_MAX1 = 10
RED_HUE_MIN2 = 170
RED_SAT_MIN = 80    # 饱和度下限，排除偏灰/偏粉的非鲜红像素
RED_VAL_MIN = 60    # 明度下限，排除过暗像素
RED_FRAME_MIN_AREA_RATIO = 0.01   # 红框外框面积占模板面积比例下限，排除噪点
RED_FRAME_MAX_AREA_RATIO = 0.95   # 上限，排除"整图偏红"误判
RED_FRAME_MAX_THICKNESS_RATIO = 0.35   # 边框厚度占外框边长比例上限，排除实心色块
RED_FRAME_INPAINT_RADIUS = 3           # inpaint 修复半径（像素），标注框线宽通常较细


def _search_downscale(shape) -> float:
    """兜底匹配用的搜索图缩放倍率（单边超过 :data:`MAX_SEARCH_SIDE` 时 <1.0）。

    只按长边算一次，保证宽高同倍率（等比缩放，不改变模板/搜索图的相对几何）。
    注意：精确匹配那一遍**不用**这个缩放（走原分辨率），这里只服务"猜"的兜底链路。
    """
    if MAX_SEARCH_SIDE <= 0:      # <=0 视为"关闭该优化"，便于对照/回滚
        return 1.0
    height, width = int(shape[0]), int(shape[1])
    longest = max(height, width)
    if longest <= MAX_SEARCH_SIDE:
        return 1.0
    return MAX_SEARCH_SIDE / float(longest)


def _match_algo_tier(algo: str) -> int:
    """特征匹配（ORB/SIFT）的 confidence 是 RANSAC 内点比例，matchTemplate
    兜底的 confidence 是归一化相关系数——两者不是同一量纲（见模块 docstring），
    跨模板择优时不能直接比较数值大小。本函数把算法分成两级：特征匹配
    tier=1 优先于 matchTemplate 兜底 tier=0；只有同一 tier 内才比较
    confidence 数值（见 match() 与计划文档问题 1）。"""
    return 1 if algo in ("orb", "sift") else 0


@dataclass
class MatchResult:
    x: int
    y: int
    width: int
    height: int
    confidence: float
    # 2026-07-14 新增：记录本次命中所用算法（"orb"/"sift"/"template"），供排查
    # 问题时区分——特征匹配的 confidence（RANSAC 内点比例）与 matchTemplate 的
    # confidence（归一化相关系数）不是同一量纲，不应混同解读。
    match_algo: str = "template"
    # 2026-07-15 新增：框选点击子区域（x, y, w, h），已含 window_rect 偏移，与
    # 搜索图坐标系一致。仅当调用方传入 click_rect（来自 rosetta_map.json 静态
    # 声明字段）且映射成功时非 None，见 docs/plans/20260715170000-...。
    sub_bbox: Optional[tuple[int, int, int, int]] = None
    # 命中的模板文件名（如 ``列表页_渲染完成__2.png``）。多模板"或"匹配时，
    # 报告需要说明"到底哪张指纹命中"，否则 5–10 张里命中哪张无从复核。
    template_path: str = ""

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def click_center(self) -> tuple[int, int]:
        """最终点击点：有框选子区域（sub_bbox）则用其中心，否则退回整体中心。"""
        if self.sub_bbox is not None:
            sx, sy, sw, sh = self.sub_bbox
            return (sx + sw // 2, sy + sh // 2)
        return self.center


class VisualFingerprintResolver:
    def __init__(
        self,
        app_name: str,
        platform: str,
        fingerprint_dir: str = DEFAULT_FINGERPRINT_DIR,
        stale_after_days: float = DEFAULT_STALE_AFTER_DAYS,
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
        min_inlier_ratio: float = DEFAULT_MIN_INLIER_RATIO,
    ):
        self._dir = Path(fingerprint_dir) / f"{app_name}_{platform}"
        self._stale_after_days = stale_after_days
        self._match_threshold = match_threshold
        self._min_inlier_ratio = min_inlier_ratio
        # 上一次走"兜底匹配"的单调时钟毫秒（见 FULL_MATCH_MIN_INTERVAL_MS）
        self._last_full_match_ms: float = 0.0
        self._index: dict = self._load_index()

    # ── 索引文件（命中次数/捕获时间/DPI，供未来"指纹入库辅助工具"筛选依据，
    #    见 backlog #5）────────────────────────────────────────────────

    @property
    def _index_path(self) -> Path:
        return self._dir / "fingerprint_index.json"

    def _load_index(self) -> dict:
        if not self._index_path.exists():
            return {}
        try:
            with open(self._index_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.debug("[VisualFP] 指纹索引读取失败（忽略，视为空）: %s", exc)
            return {}

    def _save_index(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with open(self._index_path, "w", encoding="utf-8") as f:
            json.dump(self._index, f, ensure_ascii=False, indent=2)

    def has_fingerprint(self, description: str) -> bool:
        return (self._dir / f"{description}.png").exists()

    def has_any_fingerprint(self, description: str) -> bool:
        """是否存在至少一张已落盘的指纹模板（含多指纹场景的 `__N.png`）。

        与 `has_fingerprint()` 的区别：后者只检查单模板场景的
        `"{description}.png"`；本方法复用 `_template_paths()`，同时覆盖
        `ensure_from_patterns()` 落盘的多模板场景（`__1.png`/`__2.png` 等）。
        供 L3.8b assert_fingerprint 等调用方判断"该元素是否保有视觉指纹"，
        避免跨模块访问 `_template_paths()` 私有方法（见
        docs/plans/archive/03-resolver-rosetta-实施纪要.md）。
        """
        return len(self._template_paths(description)) > 0

    def fingerprint_age_days(self, description: str) -> float:
        entry = self._index.get(description)
        if not entry or not entry.get("captured_at"):
            return 999.0
        try:
            captured = datetime.fromisoformat(entry["captured_at"])
            return (datetime.utcnow() - captured).total_seconds() / 86400
        except Exception:
            return 999.0

    def is_stale(self, description: str) -> bool:
        return self.fingerprint_age_days(description) > self._stale_after_days

    # ── OpenCV/numpy 惰性依赖 ────────────────────────────────────────────

    @staticmethod
    def _import_cv():
        try:
            import cv2  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "VisualFingerprintResolver 需要 opencv-python，"
                "请先 pip install opencv-python（见 requirements.txt）"
            ) from exc
        return cv2

    @staticmethod
    def _import_cv_np():
        """特征匹配需要同时用到 cv2（ORB/SIFT/BFMatcher/findHomography）与
        numpy（构造关键点坐标数组），惰性成对导入。"""
        try:
            import cv2  # noqa: F401
            import numpy as np  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "VisualFingerprintResolver 需要 opencv-python，"
                "请先 pip install opencv-python（见 requirements.txt）"
            ) from exc
        return cv2, np

    # ── 特征匹配（ORB/SIFT 共用流程） ────────────────────────────────────

    def _match_by_features(
        self, cv2, np, template_gray, search_gray, algo: str,
    ) -> Optional[tuple[int, int, int, int, float, object]]:
        """用指定特征检测器（ORB/SIFT）在 search_gray 中定位 template_gray。

        Returns:
            (x, y, w, h, inlier_ratio, homography) 或 None（特征点不足/匹配数
            不足/Homography 求解失败/内点比例低于阈值）。2026-07-15 新增返回值
            第 6 元素 homography，供上层复用同一变换矩阵映射 click_rect 框选
            区域（见 _map_click_rect），避免重复求解。
        """
        if algo == "orb":
            detector = cv2.ORB_create()
            norm_type = cv2.NORM_HAMMING
        else:
            detector = cv2.SIFT_create()
            norm_type = cv2.NORM_L2

        kp1, des1 = detector.detectAndCompute(template_gray, None)
        kp2, des2 = detector.detectAndCompute(search_gray, None)

        if des1 is None or des2 is None:
            logger.debug("[VisualFP][%s] 未检出描述子，跳过", algo)
            return None
        if len(kp1) < MIN_FEATURE_POINTS or len(kp2) < MIN_FEATURE_POINTS:
            logger.debug(
                "[VisualFP][%s] 关键点不足 (template=%d, search=%d)",
                algo, len(kp1), len(kp2),
            )
            return None

        matcher = cv2.BFMatcher(norm_type)
        raw_matches = matcher.knnMatch(des1, des2, k=2)

        good = []
        for pair in raw_matches:
            if len(pair) != 2:
                continue
            m, n = pair
            if m.distance < LOWE_RATIO_THRESH * n.distance:
                good.append(m)

        if len(good) < MIN_GOOD_MATCHES:
            logger.debug(
                "[VisualFP][%s] good matches 不足 (%d < %d)",
                algo, len(good), MIN_GOOD_MATCHES,
            )
            return None

        src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

        homography, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
        if homography is None:
            logger.debug("[VisualFP][%s] Homography 求解失败", algo)
            return None

        inlier_count = int(mask.sum()) if mask is not None else 0
        inlier_ratio = inlier_count / len(good) if good else 0.0
        if inlier_ratio < self._min_inlier_ratio:
            logger.debug(
                "[VisualFP][%s] 内点比例不足 (%.3f < %.3f)",
                algo, inlier_ratio, self._min_inlier_ratio,
            )
            return None

        th, tw = template_gray.shape[0], template_gray.shape[1]
        corners = np.float32(
            [[0, 0], [0, th - 1], [tw - 1, th - 1], [tw - 1, 0]]
        ).reshape(-1, 1, 2)
        transformed = cv2.perspectiveTransform(corners, homography)
        x, y, w, h = cv2.boundingRect(transformed)

        # 几何合理性校验：低纹理/重复图案背景下，稀疏匹配点容易解出一个几何上
        # 离谱（bbox 宽高与模板原始尺寸相差悬殊）但内点比例数值上达标的伪匹配。
        # 变换后 bbox 宽/高相对模板原始宽/高的倍数必须落在合理区间内，否则拒绝
        # （降级到下一级算法），不直接采信（见模块常量 HOMOGRAPHY_SANITY_*）。
        if tw <= 0 or th <= 0 or w <= 0 or h <= 0:
            logger.debug("[VisualFP][%s] 几何校验：bbox/模板尺寸非正，拒绝", algo)
            return None
        scale_w = w / tw
        scale_h = h / th
        if not (HOMOGRAPHY_SANITY_MIN_SCALE <= scale_w <= HOMOGRAPHY_SANITY_MAX_SCALE) or (
            not (HOMOGRAPHY_SANITY_MIN_SCALE <= scale_h <= HOMOGRAPHY_SANITY_MAX_SCALE)
        ):
            logger.debug(
                "[VisualFP][%s] 几何校验不通过 (w=%d/tw=%d=%.2fx, h=%d/th=%d=%.2fx，"
                "允许区间 [%.1f, %.1f])，拒绝",
                algo, w, tw, scale_w, h, th, scale_h,
                HOMOGRAPHY_SANITY_MIN_SCALE, HOMOGRAPHY_SANITY_MAX_SCALE,
            )
            return None

        return (x, y, w, h, float(inlier_ratio), homography)

    def _map_click_rect(
        self, cv2, np, rect_local: tuple, homography, offset_x: int, offset_y: int,
    ) -> Optional[tuple[int, int, int, int]]:
        """复用 ORB/SIFT 求解的 Homography，把模板局部坐标系下的框选区域
        （click_rect，rosetta_map 静态声明字段）映射到搜索图坐标系，再加
        window_rect 偏移。与整体 bbox 的计算方式完全一致（同一套透视变换），
        保证框选区域与整体匹配结果的几何一致性。

        任何异常（Homography 退化/病态矩阵）均静默捕获返回 None，调用方应
        退回整体中心点击，不影响主流程。
        """
        try:
            rx, ry, rw, rh = rect_local
            corners = np.float32(
                [[rx, ry], [rx, ry + rh - 1], [rx + rw - 1, ry + rh - 1], [rx + rw - 1, ry]]
            ).reshape(-1, 1, 2)
            transformed = cv2.perspectiveTransform(corners, homography)
            x, y, w, h = cv2.boundingRect(transformed)
            return (offset_x + x, offset_y + y, w, h)
        except Exception as exc:
            logger.debug("[VisualFP] click_rect Homography 映射失败（忽略）: %s", exc)
            return None

    def _find_hollow_red_rect(
        self, cv2, red_mask, tw: int, th: int,
    ) -> Optional[tuple[int, int, int, int]]:
        """在红色掩码中找"中空矩形"轮廓（有内部空洞的外轮廓），返回其内部
        区域 bbox（局部坐标 (x, y, w, h)）；未找到符合条件的候选返回 None。

        判据："中空矩形"——红框是一条描边（外轮廓 + 内部空洞轮廓），与"实心
        红色图标/按钮背景"（只有外轮廓、无内部空洞）区分开，避免把UI 本身
        合法的红色元素误判为标注框。额外用面积比例区间 + 边框厚度比例上限
        做双重过滤，排除噪点/整图偏红/边框过粗（更像挖洞的实心块）等情况。

        多个候选时取外框面积最大者。
        """
        contours, hierarchy = cv2.findContours(red_mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if not contours or hierarchy is None:
            return None

        template_area = tw * th
        best_outer_area = -1
        best_inner_rect = None
        for i, contour in enumerate(contours):
            parent = hierarchy[0][i][3]
            if parent != -1:
                continue  # 只处理外轮廓，内轮廓通过 first_child 索引反查
            child_idx = hierarchy[0][i][2]
            if child_idx == -1:
                continue  # 无内部空洞 → 实心色块，非中空标注框，跳过
            ox, oy, ow, oh = cv2.boundingRect(contour)
            area_ratio = (ow * oh) / template_area if template_area else 0
            if not (RED_FRAME_MIN_AREA_RATIO <= area_ratio <= RED_FRAME_MAX_AREA_RATIO):
                continue
            ix, iy, iw, ih = cv2.boundingRect(contours[child_idx])
            if iw <= 0 or ih <= 0:
                continue
            thickness_w = (ow - iw) / 2.0
            thickness_h = (oh - ih) / 2.0
            if thickness_w > ow * RED_FRAME_MAX_THICKNESS_RATIO or thickness_h > oh * RED_FRAME_MAX_THICKNESS_RATIO:
                continue  # 边框太粗，更像实心块上挖了个洞，非细描边标注框
            if ow * oh > best_outer_area:
                best_outer_area = ow * oh
                best_inner_rect = (ix, iy, iw, ih)
        return best_inner_rect

    def _process_red_frame(
        self, cv2, np, template_bgr,
    ) -> tuple:
        """检测模板图中人工标注的红色矩形框并去除（inpaint 抹除），推导出框内
        区域作为点击子区域局部坐标。

        Returns:
            (cleaned_bgr, click_rect_local)：
              - 未检测到符合特征的红框 → (template_bgr 原图不变, None)，零回归
              - 检测到 → (红框像素被 inpaint 抹除后的净图, 框内区域局部坐标
                (x, y, w, h))
            任何异常（缺失 cv2 API/mock 未实现/尺寸异常等）均静默捕获，退回
            (template_bgr, None)，不影响主流程、不误判、不抛出。
        """
        try:
            hsv = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2HSV)
            mask1 = cv2.inRange(
                hsv, np.array([0, RED_SAT_MIN, RED_VAL_MIN]),
                np.array([RED_HUE_MAX1, 255, 255]),
            )
            mask2 = cv2.inRange(
                hsv, np.array([RED_HUE_MIN2, RED_SAT_MIN, RED_VAL_MIN]),
                np.array([179, 255, 255]),
            )
            red_mask = cv2.bitwise_or(mask1, mask2)
            kernel = np.ones((3, 3), np.uint8)
            red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_CLOSE, kernel)  # 桥接抗锯齿断裂

            inner_rect = self._find_hollow_red_rect(
                cv2, red_mask, template_bgr.shape[1], template_bgr.shape[0],
            )
            if inner_rect is None:
                return template_bgr, None

            cleaned = cv2.inpaint(template_bgr, red_mask, RED_FRAME_INPAINT_RADIUS, cv2.INPAINT_TELEA)
            return cleaned, inner_rect
        except Exception as exc:
            logger.debug("[VisualFP] 红框检测/去除失败（忽略，使用原图）: %s", exc)
            return template_bgr, None

    @staticmethod
    def _theme_invariant_map(cv2, np, gray):
        """把灰度图转换成"主题不变"表示：CLAHE 局部对比度增强 + Sobel 梯度
        幅值，供④级兜底匹配使用（见模块 docstring"深色模式兜底"一节，计划
        文档 docs/plans/20260803190000-l3-8-visual-fingerprint-theme-invariant-
        gradient-matching.md）。

        - CLAHE：把局部弱对比度（如"独立调色板"深色模式下前景仅比背景亮
          一点点）放大到可检测范围，不依赖全局固定阈值。
        - Sobel 梯度幅值 sqrt(gx²+gy²)：∇(255-I) = -∇I，平方后符号被抵消，
          天然对"真反转"不敏感；同时只反映边缘位置/强度，不反映哪边亮哪边
          暗，覆盖"局部不变"场景（形状/边缘不变即可命中）。
        - 最终normalize 到 0-255 uint8，与灰度图同量纲，可直接复用现有
          `cv2.matchTemplate(..., cv2.TM_CCOEFF_NORMED)` 逻辑。

        不捕获异常——调用方 `_match_gradient()` 统一 try/except，任何环境
        缺失 CLAHE/Sobel 等 API 均在那一层静默退化。
        """
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        gx = cv2.Sobel(enhanced, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(enhanced, cv2.CV_32F, 0, 1, ksize=3)
        mag = cv2.magnitude(gx, gy)
        return cv2.normalize(mag, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    @staticmethod
    def _match_template_multiscale(
        cv2, template_gray, search_gray, threshold: float,
    ) -> Optional[tuple[float, tuple[int, int], float, int, int]]:
        """按常见 DPI 倍率缩放低纹理模板，返回最佳可靠命中。"""
        best = None
        search_h, search_w = search_gray.shape[:2]
        template_h, template_w = template_gray.shape[:2]
        for scale in _TEMPLATE_MATCH_SCALES:
            width = max(1, round(template_w * scale))
            height = max(1, round(template_h * scale))
            if width > search_w or height > search_h:
                continue
            # 缩到 MIN_MATCH_SIDE 以下的档位不可信（小样本相关系数虚高，见该常量注释）：
            # 宁可漏判也不要把"没出现"判成"出现了"。
            if width < MIN_MATCH_SIDE or height < MIN_MATCH_SIDE:
                continue
            try:
                interpolation = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
                scaled = cv2.resize(
                    template_gray, (width, height), interpolation=interpolation,
                )
                result = cv2.matchTemplate(
                    search_gray, scaled, cv2.TM_CCOEFF_NORMED,
                )
                _, confidence, _, location = cv2.minMaxLoc(result)
            except Exception:
                continue
            if best is None or confidence > best[0]:
                best = (float(confidence), location, scale, width, height)
        if best is None or best[0] < threshold:
            return None
        return best

    def _match_gradient(
        self, cv2, np, template_gray, search_gray,
    ) -> Optional[tuple[float, tuple[int, int]]]:
        """④级：主题不变边缘/梯度结构匹配——③灰度 matchTemplate 置信度不足
        时的深色模式兜底（见模块 docstring）。复用与③同款的 matchTemplate +
        minMaxLoc 逻辑，只是把输入换成 `_theme_invariant_map()` 的输出。

        任何异常（如运行环境缺失 `cv2.createCLAHE`/`cv2.Sobel` 等 API、mock
        未实现等）均静默捕获返回 None，退化为"无④级"，不影响①②③已有行为、
        不向上抛出（与 `_process_red_frame()` 的"尽力而为"降级语义一致）。

        Returns:
            (max_val, max_loc) 且 max_val >= self._match_threshold时返回；
            否则（含异常/低于阈值）返回 None。
        """
        try:
            template_grad = self._theme_invariant_map(cv2, np, template_gray)
            search_grad = self._theme_invariant_map(cv2, np, search_gray)
            result = cv2.matchTemplate(search_grad, template_grad, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
        except Exception as exc:
            logger.debug("[VisualFP] ④梯度匹配异常（忽略，视为未命中）: %s", exc)
            return None
        threshold = _MATCH_THRESHOLD_OVERRIDE.get() or self._match_threshold
        if max_val < threshold:
            logger.debug(
                "[VisualFP] ④梯度匹配置信度不足: %.3f < %.3f", max_val, threshold,
            )
            return None
        return float(max_val), max_loc

    def _template_path_for_index(self, description: str, index: int) -> Path:
        """指纹文件名规则：index=0 → "{description}.png"（零回归，与改造前
        单指纹场景逐字节一致）；index>0 → "{description}__{index}.png"。"""
        suffix = "" if index == 0 else f"__{index}"
        return self._dir / f"{description}{suffix}.png"

    def _template_paths(self, description: str) -> list[Path]:
        """返回 description 对应的全部已落盘模板文件路径，按 index 升序
        （0 号 "{description}.png" 在前，其余 "{description}__1.png" 等按数字序）。
        只有 1 张模板时（现状 100% 场景）返回单元素列表，行为等价改造前。"""
        paths = []
        base = self._dir / f"{description}.png"
        if base.exists():
            paths.append(base)
        extra = sorted(
            self._dir.glob(f"{description}__*.png"),
            key=lambda p: int(p.stem.rsplit("__", 1)[-1]),
        )
        paths.extend(p for p in extra if p.exists())
        return paths

    def _match_one_template(
        self,
        cv2,
        np,
        template_path: Path,
        search_img,
        offset_x: int,
        offset_y: int,
        click_rect: Optional[tuple[int, int, int, int]],
        color_sensitive: bool = False,
        search_scale: float = 1.0,
        template_only: bool = False,
    ) -> Optional[MatchResult]:
        """对单张模板文件执行 ORB → SIFT → matchTemplate 三级降级匹配。

        纯代码搬移自改造前 match() 主体（cv2 惰性导入调用方已完成、灰度转换、
        三级降级、click_rect 映射逻辑逐字节不变），仅将"读取单张模板 + 匹配"
        收窄为独立方法，供外层 match() 对同一 description 下的多张模板逐一
        调用并取置信度最高者。search_img（裁剪后、尚未转灰度的 BGR 搜索图）
        由外层传入；灰度转换放在模板读取成功之后（与改造前的检查顺序保持
        逐字节一致：模板读取失败时不触碰 cv2.cvtColor，见
        test_match_template_read_failure_returns_none）。

        Args:
            search_scale: 外层 match() 对搜索图施加的等比缩小倍率（见
                :data:`MAX_SEARCH_SIDE`）。<1.0 时本方法把模板按同一倍率缩小
                （保持几何等比），并把命中坐标/尺寸/``sub_bbox`` 按
                ``1 / search_scale`` 还原到**原搜索图坐标系**后再返回；
                对调用方而言返回值与不缩放时完全同义。默认 1.0 = 不缩放。
            template_only: 只做**精确尺度**的 matchTemplate，不到阈值立即返回
                ``None``（不跑多尺度/特征/梯度任何兜底）。供 :meth:`match` 的
                第一遍"精确扫描"用——这一遍在**原分辨率**上跑，命中即是真命中。
        """
        template = cv2.imread(str(template_path))
        if template is None and sys.platform == "win32" and hasattr(cv2, "imdecode"):
            # OpenCV imread 在 Windows 上不能稳定处理中文路径；从字节解码可避免乱码。
            try:
                template = cv2.imdecode(np.fromfile(template_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            except (OSError, ValueError):
                template = None
        if template is None:
            logger.debug("[VisualFP] 指纹文件读取失败: %s", template_path)
            return None

        # ── 红框标注去除 + 点击区域自动推导（2026-08-03 新增，见模块 docstring）：
        # 检测到"中空矩形"红色描边则inpaint 抹除后再匹配，并用框内区域覆盖
        # （优先于）调用方传入的 click_rect（视为最新标注意图）；未检测到则
        # template 不变，effective_click_rect 回退到调用方传入的 click_rect
        # （可能仍为 None），与改造前行为完全一致（零回归）。──────────────
        if color_sensitive:
            effective_click_rect = click_rect
            template_match = template
            search_match = search_img
        else:
            template, detected_click_rect = self._process_red_frame(cv2, np, template)
            effective_click_rect = (
                detected_click_rect if detected_click_rect is not None else click_rect
            )
            # 视觉点击默认转灰度，消除深色/浅色主题切换的颜色差异。
            template_match = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            search_match = cv2.cvtColor(search_img, cv2.COLOR_BGR2GRAY)

        # 搜索图已在外层等比缩小（见 MAX_SEARCH_SIDE）→ 模板按同一倍率缩小，
        # 使模板/搜索图相对几何保持不变；click_rect 是**原模板像素坐标系**下的
        # 声明，同步缩放后才能与缩放后的模板对齐；输出前统一按 1/scale 还原。
        if search_scale < 1.0:
            template_match = cv2.resize(
                template_match, None, fx=search_scale, fy=search_scale,
                interpolation=cv2.INTER_AREA,
            )
            if effective_click_rect is not None:
                crx, cry, crw, crh = effective_click_rect
                effective_click_rect = (
                    crx * search_scale, cry * search_scale,
                    crw * search_scale, crh * search_scale,
                )
        description = template_path.stem

        # 缩放坐标系 → 原图坐标系的还原器（search_scale=1.0 时恒等，零回归）。
        # 尺寸类（w/h）直接除；坐标类先减掉原点偏移再除、再加回偏移。
        def _restore(value) -> int:
            return int(round(value / search_scale))

        def _restore_point(value, offset) -> int:
            return offset + int(round((value - offset) / search_scale))

        # ── 尺寸守卫（2026-08-19 新增）：搜索图必须 ≥ 模板，否则 cv2.matchTemplate
        # 直接抛断言异常（(-215) _img <= _templ），导致用例被误判为执行异常硬重启。
        # 窗口隐藏时（矩形在屏幕外 -32000,-32000）截图会缩成 160x28 之类的小废图，
        # 此时模板必然比截图大 → 直接视为不匹配返回 None，而不是崩溃。──────────
        t_h, t_w = template_match.shape[:2]
        s_h, s_w = search_match.shape[:2]
        if s_h < t_h or s_w < t_w:
            logger.debug(
                "[VisualFP] '%s' 搜索图小于模板（%dx%d < %dx%d），跳过匹配",
                description, s_w, s_h, t_w, t_h,
            )
            return None

        # ── 小模板守卫：搜索图被整体缩小后，本来就小的模板会缩到 MIN_MATCH_SIDE 以下，
        # 此时相关系数在小样本上虚高、极易假绿（真机踩到 20x22 模板缩成 5x6 后
        # 在空白页命中 conf=0.73）。这类模板在缩小后的坐标系里没有可靠判据，
        # 直接跳过——同一 description 的其余模板照常参与，判定不会因此变严。
        if t_w < MIN_MATCH_SIDE or t_h < MIN_MATCH_SIDE:
            logger.debug(
                "[VisualFP] '%s' 模板缩到 %dx%d < %d，跳过（小样本匹配不可信）",
                description, t_w, t_h, MIN_MATCH_SIDE,
            )
            return None

        # ── ① ORB → ② SIFT 特征匹配（尺度/旋转鲁棒，解决 DPI/缩放差异） ────
        for algo in (() if (color_sensitive or template_only) else ("orb", "sift")):
            try:
                found = self._match_by_features(cv2, np, template_match, search_match, algo)
            except Exception as exc:
                logger.debug("[VisualFP][%s] 特征匹配异常（忽略，尝试下一级）: %s", algo, exc)
                found = None
            if found is not None:
                x, y, w, h, confidence, homography = found
                logger.debug(
                    "[VisualFP] '%s' 命中 via %s (confidence=%.3f)", description, algo, confidence,
                )
                sub_bbox = None
                if effective_click_rect is not None:
                    sub_bbox = self._map_click_rect(
                        cv2, np, effective_click_rect, homography, offset_x, offset_y,
                    )
                    sub_bbox = tuple(
                        _restore_point(v, off) if i in (0, 1) else _restore(v)
                        for i, (v, off) in enumerate(
                            zip(sub_bbox, (offset_x, offset_y, offset_x, offset_y))
                        )
                    )
                return MatchResult(
                    x=offset_x + _restore(x), y=offset_y + _restore(y),
                    width=max(1, _restore(w)), height=max(1, _restore(h)),
                    confidence=confidence, match_algo=algo, sub_bbox=sub_bbox,
                    template_path=template_path.name,
                )

        # image assertion 保留 legacy_runner 的 BGR 彩色匹配语义；视觉点击继续用灰度。
        result = cv2.matchTemplate(search_match, template_match, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        match_algo = "template_color" if color_sensitive else "template"
        threshold = _MATCH_THRESHOLD_OVERRIDE.get() or self._match_threshold
        th, tw = template_match.shape[0], template_match.shape[1]
        matched_scale = 1.0
        if max_val < threshold:
            # 精确尺度没到阈值就如实说"没命中"：多尺度/特征/梯度三级都是"猜"的路径，
            # 由调用方按 FULL_MATCH_MIN_INTERVAL_MS 限频决定何时再猜。
            if template_only:
                return None
            scaled_match = self._match_template_multiscale(
                cv2, template_match, search_match, threshold,
            )
            if scaled_match is not None:
                max_val, max_loc, matched_scale, tw, th = scaled_match
                match_algo = f"template_scale_{matched_scale:g}"
                logger.debug(
                    "[VisualFP] '%s' 多尺度模板命中: scale=%.2f confidence=%.3f",
                    description, matched_scale, max_val,
                )
            else:
                if color_sensitive:
                    return None
                logger.debug(
                    "[VisualFP] '%s' matchTemplate 及多尺度兜底置信度不足: %.3f < %.3f，"
                    "尝试主题不变梯度匹配",
                    description, max_val, threshold,
                )
                grad_match = self._match_gradient(cv2, np, template_match, search_match)
                if grad_match is None:
                    return None
                max_val, max_loc = grad_match
                match_algo = "gradient"

        sub_bbox = None
        if effective_click_rect is not None:
            # 多尺度模板命中时，框选点击区域按同一倍率缩放后再加命中偏移。
            crx, cry, crw, crh = effective_click_rect
            sub_bbox = (
                _restore_point(offset_x + max_loc[0] + round(crx * matched_scale), offset_x),
                _restore_point(offset_y + max_loc[1] + round(cry * matched_scale), offset_y),
                max(1, _restore(crw * matched_scale)),
                max(1, _restore(crh * matched_scale)),
            )
        return MatchResult(
            x=offset_x + _restore(max_loc[0]), y=offset_y + _restore(max_loc[1]),
            width=max(1, _restore(tw)), height=max(1, _restore(th)),
            confidence=float(max_val), match_algo=match_algo,
            sub_bbox=sub_bbox, template_path=template_path.name,
        )

    def _scan_templates(
        self,
        cv2,
        np,
        template_paths,
        search_img,
        click_rect,
        threshold: float,
        *,
        explicit_threshold: bool,
        color_sensitive: bool,
        search_scale: float,
        template_only: bool,
        stop_on_first: bool = False,
    ) -> Optional[MatchResult]:
        """对一组模板逐一匹配并按 tier 择优（tier 高者优先，同 tier 才比 confidence）。

        ``stop_on_first`` 为真时，第一个过阈值的模板立即返回（不做跨模板择优）：
        第一遍精确扫描只关心"有没有命中"，多扫模板纯属浪费；兜底遍仍走择优。
        """
        best: Optional[MatchResult] = None
        match_args = {"color_sensitive": True} if color_sensitive else {}
        for template_path in template_paths:
            result = self._match_one_template(
                cv2, np, template_path, search_img, 0, 0, click_rect,
                search_scale=search_scale, template_only=template_only, **match_args,
            )
            if result is None or (explicit_threshold and result.confidence < threshold):
                continue
            if stop_on_first:
                return result
            if best is None:
                best = result
                continue
            result_tier = _match_algo_tier(result.match_algo)
            best_tier = _match_algo_tier(best.match_algo)
            if result_tier != best_tier:
                if result_tier > best_tier:
                    best = result
                continue
            if result.confidence > best.confidence:
                best = result
        return best

    def match(
        self,
        description: str,
        screenshot_bgr,
        click_rect: Optional[tuple[int, int, int, int]] = None,
        match_threshold: Optional[float] = None,
        color_sensitive: bool = False,
    ) -> Optional[MatchResult]:
        """在 screenshot_bgr（numpy ndarray，BGR）中定位 description 对应指纹。

        2026-07-24 起支持同一 description 下的多张指纹（见 ensure_from_patterns）。

        匹配分两遍（详见实现内注释）：**第一遍在原分辨率上只做精确尺度的模板匹配**，
任一模板过阈值即返回（不缩放、不降级，命中框即像素真值）；只有第一遍全落空时才
进第二遍——搜索图等比缩小后走 ORB → SIFT → 多尺度 → 梯度完整降级链，且同一实例
每 FULL_MATCH_MIN_INTERVAL_MS 至多一次。只有 1 张模板时两遍都等价于单模板匹配。

        坐标语义（2026-07-31 修正，见 docs/plans/
        archive/03-resolver-rosetta-实施纪要.md）：返回的
        `MatchResult.x/y`（及 `sub_bbox`）是**截图自身的像素坐标**，不是屏幕
        绝对坐标——本方法不再假设 `screenshot_bgr` 是"全屏图，需要按窗口坐标
        裁剪出窗口区域"；`take_screenshot()` 在原生引擎下返回的通常已经是
        **窗口局部**图（`_last_screenshot_is_window=True`），旧版用屏幕绝对
        逻辑坐标（points）当像素偏移去裁剪该图属于单位错配的 bug（裁出越界/
        错位区域），已删除。像素坐标 → 屏幕绝对坐标的换算统一交由调用方
        （`core/resolver/resolver.py::Resolver._l3_8_visual_fingerprint()`）
        通过 `core/resolver/screen_coords.py::ScreenCoordConverter` 处理。

        Args:
            click_rect:  (x, y, w, h)，模板图自身局部像素坐标系下的框选点击
                         子区域，来自 rosetta_map.json 的静态声明字段
                         `FlatEntry.click_rect`（见 docs/plans/20260715170000-
                         l3-8-visual-fingerprint-click-region-framing.md）。
                         本方法不持久化/不感知 rosetta_map，只是把调用方传入
                         的值映射到搜索图坐标系并写入 MatchResult.sub_bbox。
                         为 None（现状 100% 指纹）时行为与改造前完全一致，
                         MatchResult.sub_bbox 恒为 None，零回归。
            color_sensitive: 使用 legacy_runner 原生 BGR 模板匹配语义；用于迁移后的
                             image assertion，避免颜色不同但轮廓相同的误命中。

        Returns:
            None：指纹文件（全部）不存在 / 全部模板全部算法均未能可靠匹配。
        """
        explicit_threshold = match_threshold is not None
        threshold = self._match_threshold if match_threshold is None else float(match_threshold)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("match_threshold must be between 0 and 1")
        template_paths = self._template_paths(description)
        if not template_paths:
            return None
        cv2, np = self._import_cv_np()

        token = _MATCH_THRESHOLD_OVERRIDE.set(threshold)
        try:
            # ── 第一遍：**原分辨率**精确匹配（不缩放、不降级、不跨模板择优） ────
            # 素材与当前窗口同尺度时这一遍就够（真机干净页面实测 conf 1.000），
            # 且不重采样 → 命中框/坐标就是像素真值。perf 的"页面渲染耗时"取样周期
            # 直接由它决定：命中轮 = 单张模板一次模板匹配（真机 230–330ms）。
            best = self._scan_templates(
                cv2, np, template_paths, screenshot_bgr, click_rect, threshold,
                explicit_threshold=explicit_threshold, color_sensitive=color_sensitive,
                search_scale=1.0, template_only=True, stop_on_first=True,
            )
            if best is not None:
                return best

            # ── 第二遍（兜底，限频）：缩到 MAX_SEARCH_SIDE 后走完整降级链 ──────
            # 多尺度 ladder / ORB / SIFT / 梯度都是"猜"的路径（DPI 差异、主题变换、
            # 局部改版）。全分辨率下 8 张模板走全链 ~18s，故：① 搜索图与模板同倍率
            # 缩小以省成本（命中坐标按 1/scale 还原，语义与不缩放同义）；② 同一
            # resolver 实例至多每 FULL_MATCH_MIN_INTERVAL_MS 走一次——轮询场景下
            # 不会把每次取样都拖成十几秒，而"猜不出来"也不会因为少猜几次就永远猜不出。
            # 择优规则（2026-08-03 修正）：特征匹配（ORB/SIFT）的 confidence 与模板
            # 兜底的 confidence 量纲不同，不能跨算法直接比数值——按 _match_algo_tier()
            # 分级，tier 更高（特征匹配）者优先，同 tier 内才比 confidence。
            now_ms = time.monotonic() * 1000.0
            if now_ms - self._last_full_match_ms < FULL_MATCH_MIN_INTERVAL_MS:
                return None
            self._last_full_match_ms = now_ms

            search_scale = _search_downscale(screenshot_bgr.shape)
            search_img = screenshot_bgr
            if search_scale < 1.0:
                search_img = cv2.resize(
                    screenshot_bgr, None, fx=search_scale, fy=search_scale,
                    interpolation=cv2.INTER_AREA,
                )
            return self._scan_templates(
                cv2, np, template_paths, search_img, click_rect, threshold,
                explicit_threshold=explicit_threshold, color_sensitive=color_sensitive,
                search_scale=search_scale, template_only=False,
            )
        finally:
            _MATCH_THRESHOLD_OVERRIDE.reset(token)

    def ensure_from_pattern(self, description: str, pattern_b64: str, index: int = 0) -> None:
        """rosetta_map.pattern 解压为本机指纹文件（仅当本机指纹不存在时，见 §2.7 设计）。

        Args:
            index: 同一 description 下第几张指纹（默认 0）。index=0 时文件名
                   不变（"{description}.png"，零回归）；index>0 时追加索引
                   后缀（"{description}__{index}.png"），见 ensure_from_patterns。
        """
        template_path = self._template_path_for_index(description, index)
        if template_path.exists():
            return
        from uita.core.rosetta.pattern_codec import decode

        png_bytes = decode(pattern_b64)
        self._dir.mkdir(parents=True, exist_ok=True)
        with open(template_path, "wb") as f:
            f.write(png_bytes)
        logger.debug(
            "[VisualFP] 从 rosetta_map.pattern 还原本机指纹: %s (index=%d)", description, index,
        )

    def ensure_from_patterns(self, description: str, patterns: list) -> None:
        """批量版 ensure_from_pattern：逐张调用，index 按列表顺序递增
        （0, 1, 2, ...）。单张列表时行为等价 ensure_from_pattern(desc, p, 0)。

        单张指纹解压失败不影响其余指纹落盘（每张独立 try/except），与
        capture()/自愈路径的"尽力而为"语义一致。
        """
        for i, p in enumerate(patterns):
            try:
                self.ensure_from_pattern(description, p, index=i)
            except Exception as exc:
                logger.debug("[VisualFP] ensure_from_patterns[%d] 失败（忽略）: %s", i, exc)

    def capture(
        self,
        description: str,
        screenshot_bgr,
        bbox: tuple,
        dpi_scale: float = 1.0,
    ) -> None:
        """L4 定位成功后录制新指纹（自愈写入本机缓存，不直接写 rosetta_map.json，
        见 R22——入库需人工审核，本方法仅写本机临时缓存）。"""
        cv2 = self._import_cv()
        x, y, w, h = bbox
        crop = screenshot_bgr[y:y + h, x:x + w]
        self._dir.mkdir(parents=True, exist_ok=True)
        template_path = self._dir / f"{description}.png"
        cv2.imwrite(str(template_path), crop)

        entry = self._index.setdefault(description, {"hit_count": 0})
        entry["captured_at"] = datetime.utcnow().isoformat()
        entry["dpi_scale"] = dpi_scale
        entry["hit_count"] = entry.get("hit_count", 0) + 1
        self._save_index()
        logger.debug("[VisualFP] 录制新指纹: %s", description)
