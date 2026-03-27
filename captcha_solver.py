"""AI-assisted slider captcha solver for TikTok Seller Center."""

from __future__ import annotations

import json
import os
import random
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, cast

import cv2
import numpy as np
from playwright.async_api import Frame, Locator, Page

Scope = Page | Frame
SearchScope = Page | Frame | Locator
CvImage = Any


Box = dict[str, float]

CAPTCHA_CONTAINER_SELECTORS = [
    '[class*="captcha" i]',
    '[id*="captcha" i]',
    '[class*="verify" i]',
    '[id*="verify" i]',
    '[class*="secsdk" i]',
    '[id*="secsdk" i]',
    '[class*="slider" i]',
    '[id*="slider" i]',
    '[class*="geetest" i]',
    '[id*="geetest" i]',
]

IMAGE_SELECTORS = [
    '[class*="captcha" i] img',
    '[class*="verify" i] img',
    '[class*="secsdk" i] img',
    '[class*="slider" i] img',
    '[class*="captcha" i] canvas',
    '[class*="verify" i] canvas',
    '[class*="secsdk" i] canvas',
    '[class*="slider" i] canvas',
    'img[class*="captcha" i]',
    'img[class*="verify" i]',
    'canvas',
    'img',
]

SLIDER_SELECTORS = [
    '[class*="secsdk" i] [class*="btn" i]',
    '[class*="secsdk" i] [class*="handle" i]',
    '[class*="secsdk" i] [class*="handler" i]',
    '[class*="captcha" i] [class*="btn" i]',
    '[class*="captcha" i] [class*="handle" i]',
    '[class*="captcha" i] [class*="handler" i]',
    '[class*="verify" i] [class*="btn" i]',
    '[class*="verify" i] [class*="handle" i]',
    '[class*="verify" i] [class*="handler" i]',
    '[class*="slider" i] [class*="btn" i]',
    '[class*="slider" i] [class*="handle" i]',
    '[class*="slider" i] [class*="handler" i]',
    '[role="slider"]',
    '[aria-label*="slider" i]',
    '[aria-label*="drag" i]',
]

TRACK_SELECTORS = [
    '[class*="secsdk" i] [class*="track" i]',
    '[class*="secsdk" i] [class*="rail" i]',
    '[class*="secsdk" i] [class*="bar" i]',
    '[class*="captcha" i] [class*="track" i]',
    '[class*="captcha" i] [class*="rail" i]',
    '[class*="captcha" i] [class*="bar" i]',
    '[class*="verify" i] [class*="track" i]',
    '[class*="verify" i] [class*="rail" i]',
    '[class*="verify" i] [class*="bar" i]',
    '[class*="slider" i] [class*="track" i]',
    '[class*="slider" i] [class*="rail" i]',
    '[class*="slider" i] [class*="bar" i]',
]

SUCCESS_TEXT_RE = re.compile(r"verified|success|passed|通过|成功|验证通过", re.I)
RETRY_TEXT_RE = re.compile(r"try again|retry|failed|incorrect|再试|重试|失败|错误", re.I)
REFRESH_TEXT_RE = re.compile(r"refresh|reload|再次验证|刷新", re.I)


@dataclass(slots=True)
class SolverConfig:
    ai_provider: str = "openai"
    ai_api_key: str = ""
    ai_model: str = ""
    ai_base_url: str = ""


@dataclass(slots=True)
class ElementSnapshot:
    locator: Locator
    box: Box
    tag_name: str
    class_name: str


@dataclass(slots=True)
class CaptchaScene:
    scope: Scope
    background: ElementSnapshot
    piece: ElementSnapshot | None
    slider: ElementSnapshot
    track: ElementSnapshot | None


@dataclass(slots=True)
class GapCandidate:
    strategy: str
    gap_left_px: float
    confidence: float
    piece_aware: bool = False
    notes: str = ""


PIECE_AWARE_STRATEGIES = {"template", "sobel"}
DETECTOR_STRATEGIES = {"yolo"}
HEURISTIC_STRATEGIES = {"variance", "contour"}
AI_STRATEGIES = {"ai"}


@dataclass(slots=True)
class PostDragState:
    captcha_visible: bool
    success_visible: bool
    retry_visible: bool


@dataclass(slots=True)
class DragAttemptResult:
    success: bool
    moved_px: float
    state: PostDragState


@dataclass(slots=True)
class SolverAttemptReport:
    strategy: str
    confidence: float
    gap_left_px: float
    distances: list[float]
    success: bool = False
    final_reason: str = ""
    last_drag_result: DragAttemptResult | None = None


@dataclass(slots=True)
class SolverOutcome:
    solved: bool
    reason: str
    reports: list[SolverAttemptReport]


def _runtime_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_solver_config() -> SolverConfig:
    config_path = os.path.join(_runtime_base_dir(), "gui_config.json")
    if not os.path.exists(config_path):
        return SolverConfig()
    try:
        with open(config_path, "r", encoding="utf-8") as file_obj:
            raw = json.load(file_obj)
    except Exception:
        return SolverConfig()
    if not isinstance(raw, dict):
        return SolverConfig()
    return SolverConfig(
        ai_provider=str(raw.get("ai_provider", "openai") or "openai").strip() or "openai",
        ai_api_key=str(raw.get("ai_api_key", "") or "").strip(),
        ai_model=str(raw.get("ai_model", "") or "").strip(),
        ai_base_url=str(raw.get("ai_base_url", "") or "").strip(),
    )


def _bytes_to_cv(img_bytes: bytes) -> CvImage | None:
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return None
    return image


def _remove_whitespace(image: CvImage) -> CvImage:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)
    coords = cv2.findNonZero(thresh)
    if coords is None:
        return image
    x, y, width, height = cv2.boundingRect(coords)
    return image[y:y + height, x:x + width]


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def gap_x_to_drag_distance(gap_x: float) -> float:
    if gap_x <= 0:
        return 0.0
    return 14.7585 * (gap_x ** 0.5190) - 3.9874


def build_drag_distance_candidates(
    gap_left_px: float,
    image_width_px: float,
    background_width_css: float,
    slider_width_css: float,
    track_width_css: float | None,
) -> list[float]:
    ratio = gap_left_px / max(image_width_px, 1.0)
    travel_width = background_width_css
    if track_width_css is not None and track_width_css > 0:
        travel_width = max(track_width_css - slider_width_css * 0.55, background_width_css * 0.65)

    raw_candidates = [
        travel_width * ratio,
        gap_x_to_drag_distance(gap_left_px),
        background_width_css * ratio,
    ]

    minimum_travel = min(8.0, max(travel_width, 8.0))
    maximum_travel = max(travel_width, 24.0)
    normalized: list[float] = []
    seen: set[int] = set()
    for candidate in raw_candidates:
        rounded = int(round(_clamp(candidate, minimum_travel, maximum_travel)))
        if rounded in seen:
            continue
        seen.add(rounded)
        normalized.append(float(rounded))
    return normalized


def _filter_valid_gap_candidates(candidates: Iterable[GapCandidate], image_width_px: float) -> list[GapCandidate]:
    return [item for item in candidates if image_width_px * 0.08 < item.gap_left_px < image_width_px * 0.96]


def _candidate_tier(candidate: GapCandidate) -> int:
    if candidate.strategy in PIECE_AWARE_STRATEGIES or candidate.piece_aware:
        return 0
    if candidate.strategy in DETECTOR_STRATEGIES:
        return 1
    if candidate.strategy in HEURISTIC_STRATEGIES:
        return 2
    if candidate.strategy in AI_STRATEGIES:
        return 3
    return 4


def _consensus_gap_candidate(piece_candidates: list[GapCandidate], image_width_px: float) -> GapCandidate | None:
    if len(piece_candidates) < 2:
        return None
    tolerance = max(12.0, image_width_px * 0.035)
    ranked = sorted(piece_candidates, key=lambda item: item.confidence, reverse=True)
    for index, left in enumerate(ranked):
        for right in ranked[index + 1 :]:
            if abs(left.gap_left_px - right.gap_left_px) <= tolerance:
                merged_gap = (left.gap_left_px + right.gap_left_px) / 2.0
                merged_confidence = min(0.99, max(left.confidence, right.confidence) + 0.12)
                return GapCandidate(
                    strategy=f"consensus:{left.strategy}+{right.strategy}",
                    gap_left_px=merged_gap,
                    confidence=merged_confidence,
                    piece_aware=True,
                    notes="piece-aware-consensus",
                )
    return None


def select_gap_candidates(candidates: Iterable[GapCandidate], image_width_px: float, limit: int = 2) -> list[GapCandidate]:
    valid = _filter_valid_gap_candidates(candidates, image_width_px)
    if not valid:
        return []

    piece_candidates = [item for item in valid if _candidate_tier(item) == 0]
    consensus = _consensus_gap_candidate(piece_candidates, image_width_px)

    selected: list[GapCandidate] = []
    seen: set[tuple[str, int]] = set()

    def _add(candidate: GapCandidate):
        key = (candidate.strategy, int(round(candidate.gap_left_px)))
        if key in seen:
            return
        seen.add(key)
        selected.append(candidate)

    if consensus is not None:
        _add(consensus)

    for tier in range(5):
        tier_candidates = [item for item in valid if _candidate_tier(item) == tier]
        tier_candidates.sort(key=lambda item: item.confidence, reverse=True)
        for candidate in tier_candidates:
            _add(candidate)
            if len(selected) >= limit:
                return selected

    return selected[:limit]


def select_best_gap_candidate(candidates: Iterable[GapCandidate], image_width_px: float) -> GapCandidate | None:
    selected = select_gap_candidates(candidates, image_width_px, limit=1)
    return selected[0] if selected else None


def _build_drag_path(start_x: float, end_x: float, y: float, steps: int) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for index in range(1, steps + 1):
        fraction = index / steps
        if fraction < 0.5:
            eased = 2 * fraction * fraction
        else:
            eased = 1 - ((-2 * fraction + 2) ** 2) / 2
        current_x = start_x + (end_x - start_x) * eased
        current_y = y + random.choice([-1.0, 0.0, 0.0, 1.0])
        points.append((current_x, current_y))
    return points


async def _capture_snapshot(locator: Locator) -> ElementSnapshot | None:
    try:
        box = await locator.bounding_box()
        if not box:
            return None
        tag_name = str(await locator.evaluate("el => el.tagName || ''"))
        class_name = str(await locator.evaluate("el => el.className || ''"))
        normalized_box: Box = {
            "x": float(box["x"]),
            "y": float(box["y"]),
            "width": float(box["width"]),
            "height": float(box["height"]),
        }
        return ElementSnapshot(locator=locator, box=normalized_box, tag_name=tag_name, class_name=class_name)
    except Exception:
        return None


async def _iter_container_locators(scope: SearchScope) -> list[Locator]:
    locators: list[Locator] = []
    seen: set[tuple[int, int, int, int]] = set()
    for selector in CAPTCHA_CONTAINER_SELECTORS:
        try:
            items = scope.locator(selector)
            count = min(await items.count(), 8)
        except Exception:
            continue
        for index in range(count):
            item = items.nth(index)
            try:
                if not await item.is_visible(timeout=300):
                    continue
                box = await item.bounding_box()
                if not box or box["width"] < 60 or box["height"] < 40:
                    continue
                key = (round(box["x"]), round(box["y"]), round(box["width"]), round(box["height"]))
                if key in seen:
                    continue
                seen.add(key)
                locators.append(item)
            except Exception:
                continue
    if locators:
        return locators
    return [scope.locator("body").first]


def find_gap_by_template(bg_bytes: bytes, piece_bytes: bytes) -> GapCandidate | None:
    bg = _bytes_to_cv(bg_bytes)
    piece = _bytes_to_cv(piece_bytes)
    if bg is None or piece is None:
        return None

    height_bg, width_bg = bg.shape[:2]
    piece = _remove_whitespace(piece)
    height_piece, width_piece = piece.shape[:2]
    if width_piece >= width_bg or height_piece >= height_bg or width_piece < 10 or height_piece < 10:
        return None

    bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
    piece_gray = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY)
    bg_edges = cv2.Canny(bg_gray, 100, 200)
    piece_edges = cv2.Canny(piece_gray, 100, 200)
    result = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < 0.18:
        return None

    return GapCandidate(
        strategy="template",
        gap_left_px=float(max_loc[0]),
        confidence=float(max_val),
        piece_aware=True,
        notes="edge-template",
    )


def find_gap_by_sobel_multi(bg_bytes: bytes, piece_bytes: bytes) -> GapCandidate | None:
    bg = _bytes_to_cv(bg_bytes)
    piece = _bytes_to_cv(piece_bytes)
    if bg is None or piece is None:
        return None

    height_bg, width_bg = bg.shape[:2]
    piece = _remove_whitespace(piece)
    height_piece, width_piece = piece.shape[:2]
    if width_piece >= width_bg or height_piece >= height_bg or width_piece < 10 or height_piece < 10:
        return None

    def _sobel(image: CvImage) -> CvImage:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        grad_x = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)
        abs_x = cv2.convertScaleAbs(grad_x)
        abs_y = cv2.convertScaleAbs(grad_y)
        grad = cv2.addWeighted(abs_x, 0.5, abs_y, 0.5, 0)
        return cast(CvImage, cv2.normalize(src=grad, dst=grad.copy(), alpha=0, beta=255, norm_type=cv2.NORM_MINMAX))

    def _enhance(image: CvImage) -> CvImage:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return clahe.apply(gray)

    def _edges(image: CvImage) -> CvImage:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.Canny(blurred, 50, 150)

    def _match_all(piece_image: CvImage, bg_image: CvImage) -> list[tuple[float, float]]:
        matches: list[tuple[float, float]] = []
        for method in (cv2.TM_CCOEFF_NORMED, cv2.TM_CCORR_NORMED):
            matched = cv2.matchTemplate(bg_image, piece_image, method)
            _, max_val, _, max_loc = cv2.minMaxLoc(matched)
            matches.append((float(max_loc[0]), float(max_val)))
        return matches

    try:
        candidates = _match_all(_sobel(piece), _sobel(bg))
        candidates.extend(_match_all(_enhance(piece), _enhance(bg)))
        edge_match = cv2.matchTemplate(_edges(bg), _edges(piece), cv2.TM_CCOEFF_NORMED)
        _, edge_val, _, edge_loc = cv2.minMaxLoc(edge_match)
        candidates.append((float(edge_loc[0]), float(edge_val)))
        candidates.sort(key=lambda item: item[1], reverse=True)
        best_x, best_confidence = candidates[0]
        if best_confidence < 0.16:
            return None
        return GapCandidate(
            strategy="sobel",
            gap_left_px=best_x,
            confidence=best_confidence,
            piece_aware=True,
            notes="sobel-clahe-multi",
        )
    except Exception:
        return None


def find_gap_by_variance(bg_bytes: bytes) -> GapCandidate | None:
    bg_img = _bytes_to_cv(bg_bytes)
    if bg_img is None:
        return None
    _, width = bg_img.shape[:2]
    gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
    col_var = np.var(gray.astype(np.float32), axis=0)
    start_col = int(width * 0.25)
    end_col = int(width * 0.95)
    window = 45
    best_score = 0.0
    best_col = -1
    for col in range(start_col, end_col - window):
        score = float(np.mean(col_var[col:col + window]))
        if score > best_score:
            best_score = score
            best_col = col
    if best_col < 0:
        return None

    overall_var = float(np.mean(col_var[start_col:end_col]))
    ratio = best_score / max(overall_var, 1.0)
    if ratio < 1.2:
        return None
    return GapCandidate(
        strategy="variance",
        gap_left_px=float(best_col),
        confidence=min(0.95, ratio / 2.0),
        notes=f"variance-ratio={ratio:.2f}",
    )


def find_gap_by_contour(img_bytes: bytes) -> GapCandidate | None:
    image = _bytes_to_cv(img_bytes)
    if image is None:
        return None
    height, width = image.shape[:2]
    if height < 20 or width < 20:
        return None

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 100, 200)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dilated = cv2.dilate(edges, kernel, iterations=2)
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    min_x = int(width * 0.3)
    min_side = int(min(height, width) * 0.08)
    max_side = int(max(height, width) * 0.45)
    candidates: list[tuple[float, float, int]] = []
    for contour in contours:
        x, y, contour_width, contour_height = cv2.boundingRect(contour)
        if x < min_x:
            continue
        if contour_width < min_side or contour_height < min_side or contour_width > max_side or contour_height > max_side:
            continue
        aspect = contour_width / max(contour_height, 1)
        if not 0.5 <= aspect <= 2.0:
            continue
        roi = edges[y:y + contour_height, x:x + contour_width]
        edge_density = float(np.count_nonzero(roi) / max(contour_width * contour_height, 1))
        candidates.append((float(x), edge_density, contour_width))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1], reverse=True)
    best_x, density, contour_width = candidates[0]
    return GapCandidate(
        strategy="contour",
        gap_left_px=best_x,
        confidence=min(0.7, density * 2.5),
        notes=f"width={contour_width}",
    )


def find_gap_by_ai_vision(
    bg_bytes: bytes,
    api_key: str,
    provider: str = "openai",
    model: str = "",
    piece_bytes: bytes | None = None,
    base_url: str = "",
) -> GapCandidate | None:
    if not api_key:
        return None

    bg_img = _bytes_to_cv(bg_bytes)
    if bg_img is None:
        return None
    _, width = bg_img.shape[:2]

    import base64
    import io

    prompt_lines = [
        f"This is a slider CAPTCHA background image that is {width} pixels wide.",
        "Find the LEFT EDGE X coordinate of the missing gap/hole where the puzzle piece should fit.",
        "Return only one integer pixel coordinate, nothing else.",
    ]
    if piece_bytes:
        prompt_lines.append("A second image may contain the draggable puzzle piece for reference.")
    prompt = "\n".join(prompt_lines)
    bg_b64 = base64.b64encode(bg_bytes).decode("utf-8")

    try:
        raw_text = ""
        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url or None)
            content: list[dict[str, object]] = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{bg_b64}"}},
            ]
            if piece_bytes:
                piece_b64 = base64.b64encode(piece_bytes).decode("utf-8")
                content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{piece_b64}"}})
            user_message = cast(Any, {"role": "user", "content": content})
            response = client.chat.completions.create(
                model=model or "gpt-4o",
                messages=cast(Any, [user_message]),
                temperature=0,
                max_tokens=24,
            )
            raw_text = (response.choices[0].message.content or "").strip()
        elif provider == "gemini":
            import google.generativeai as genai
            from PIL import Image

            genai.configure(api_key=api_key)
            gemini_model = genai.GenerativeModel(model or "gemini-2.0-flash")
            payload: list[object] = [prompt, Image.open(io.BytesIO(bg_bytes))]
            if piece_bytes:
                payload.append(Image.open(io.BytesIO(piece_bytes)))
            response = gemini_model.generate_content(payload)
            raw_text = str(getattr(response, "text", "") or "").strip()
        else:
            return None

        match = re.search(r"\d+", raw_text)
        if not match:
            return None
        gap_left = float(int(match.group()))
        if not 0 < gap_left < width:
            return None
        return GapCandidate(
            strategy="ai",
            gap_left_px=gap_left,
            confidence=0.58,
            piece_aware=piece_bytes is not None,
            notes=f"provider={provider}",
        )
    except Exception:
        return None


def find_gap_by_yolo(bg_bytes: bytes) -> GapCandidate | None:
    try:
        from captcha_recognizer.slider import Slider
    except ImportError:
        return None

    bg_img = _bytes_to_cv(bg_bytes)
    if bg_img is None:
        return None
    _, width = bg_img.shape[:2]

    try:
        model = Slider()
        box, confidence = model.identify(source=bg_img, show=False)
        if not box or confidence < 0.3:
            return None
        x1, _, _, _ = box[:4]
        if x1 < width * 0.10:
            return None
        return GapCandidate(
            strategy="yolo",
            gap_left_px=float(x1),
            confidence=float(confidence),
            notes="captcha-recognizer",
        )
    except Exception:
        return None


async def _first_visible_snapshot(scope: SearchScope, selectors: list[str], min_width: float = 24, min_height: float = 20) -> ElementSnapshot | None:
    best: ElementSnapshot | None = None
    best_area = 0.0
    for selector in selectors:
        try:
            items = scope.locator(selector)
            count = min(await items.count(), 8)
        except Exception:
            continue
        for index in range(count):
            locator = items.nth(index)
            try:
                if not await locator.is_visible(timeout=250):
                    continue
            except Exception:
                continue
            snapshot = await _capture_snapshot(locator)
            if snapshot is None:
                continue
            width = snapshot.box["width"]
            height = snapshot.box["height"]
            if width < min_width or height < min_height:
                continue
            if width > 260 or height > 240:
                continue
            area = width * height
            if area > best_area:
                best = snapshot
                best_area = area
    return best


def _boxes_overlap(a: Box, b: Box, padding: float = 0.0) -> bool:
    ax1 = a["x"] - padding
    ay1 = a["y"] - padding
    ax2 = a["x"] + a["width"] + padding
    ay2 = a["y"] + a["height"] + padding
    bx1 = b["x"] - padding
    by1 = b["y"] - padding
    bx2 = b["x"] + b["width"] + padding
    by2 = b["y"] + b["height"] + padding
    return ax1 <= bx2 and ax2 >= bx1 and ay1 <= by2 and ay2 >= by1


def _score_background_candidate(candidate: ElementSnapshot, slider: ElementSnapshot | None, track: ElementSnapshot | None) -> float:
    area = candidate.box["width"] * candidate.box["height"]
    score = area
    reference = track or slider
    if reference is not None:
        center_y = candidate.box["y"] + candidate.box["height"] / 2
        ref_center_y = reference.box["y"] + reference.box["height"] / 2
        vertical_delta = abs(center_y - ref_center_y)
        score -= vertical_delta * 4.0
        if _boxes_overlap(candidate.box, reference.box, padding=24.0):
            score += 15000.0
        if candidate.box["x"] <= reference.box["x"] + reference.box["width"] and candidate.box["width"] >= reference.box["width"] * 1.2:
            score += 8000.0
    return score


def _is_piece_candidate(candidate: ElementSnapshot, background: ElementSnapshot) -> bool:
    candidate_area = candidate.box["width"] * candidate.box["height"]
    background_area = background.box["width"] * background.box["height"]
    if candidate_area >= background_area * 0.55:
        return False
    if candidate.box["width"] >= background.box["width"] * 0.65:
        return False
    if candidate.box["height"] >= background.box["height"] * 0.85:
        return False
    return _boxes_overlap(candidate.box, background.box, padding=36.0)


def _scene_geometry_is_plausible(background: ElementSnapshot, slider: ElementSnapshot, track: ElementSnapshot | None) -> bool:
    bg = background.box
    slider_box = slider.box
    reference = track.box if track is not None else slider_box

    if bg["width"] < slider_box["width"] * 1.4:
        return False
    if bg["height"] < slider_box["height"] * 0.8:
        return False
    if bg["x"] > slider_box["x"] + slider_box["width"]:
        return False
    if bg["x"] + bg["width"] < reference["x"] + reference["width"] * 0.6:
        return False

    bg_center_y = bg["y"] + bg["height"] / 2
    ref_center_y = reference["y"] + reference["height"] / 2
    allowed_vertical_delta = max(bg["height"], reference["height"]) * 1.25
    return abs(bg_center_y - ref_center_y) <= allowed_vertical_delta


async def _find_images_in(
    scope: SearchScope,
    slider: ElementSnapshot | None = None,
    track: ElementSnapshot | None = None,
) -> tuple[ElementSnapshot, ElementSnapshot | None] | None:
    image_snapshots: list[ElementSnapshot] = []
    seen: set[tuple[int, int, int, int]] = set()
    for selector in IMAGE_SELECTORS:
        try:
            items = scope.locator(selector)
            count = min(await items.count(), 12)
        except Exception:
            continue
        for index in range(count):
            locator = items.nth(index)
            try:
                if not await locator.is_visible(timeout=250):
                    continue
            except Exception:
                continue
            snapshot = await _capture_snapshot(locator)
            if snapshot is None:
                continue
            width = snapshot.box["width"]
            height = snapshot.box["height"]
            if width < 30 or height < 30:
                continue
            key = (round(snapshot.box["x"]), round(snapshot.box["y"]), round(width), round(height))
            if key in seen:
                continue
            seen.add(key)
            image_snapshots.append(snapshot)

    if not image_snapshots:
        return None

    image_snapshots.sort(key=lambda item: _score_background_candidate(item, slider, track), reverse=True)
    background = image_snapshots[0]
    piece: ElementSnapshot | None = None
    for candidate in image_snapshots[1:]:
        if _is_piece_candidate(candidate, background):
            piece = candidate
            break
    return background, piece


async def _extract_scene_from_scope(scope: Scope) -> CaptchaScene | None:
    slider = await _first_visible_snapshot(scope, SLIDER_SELECTORS)
    if slider is None:
        return None
    track = await _first_visible_snapshot(scope, TRACK_SELECTORS, min_width=60, min_height=10)
    images = await _find_images_in(scope, slider=slider, track=track)
    if images is None:
        return None
    background, piece = images
    if not _scene_geometry_is_plausible(background, slider, track):
        return None
    return CaptchaScene(scope=scope, background=background, piece=piece, slider=slider, track=track)


async def _find_captcha_scene(page: Page) -> CaptchaScene | None:
    scope_candidates: list[Scope] = [page]
    scope_candidates.extend(frame for frame in page.frames if frame != page.main_frame)

    for scope in scope_candidates:
        for container in await _iter_container_locators(scope):
            try:
                slider = await _first_visible_snapshot(container, SLIDER_SELECTORS)
                if slider is None:
                    continue
                track = await _first_visible_snapshot(container, TRACK_SELECTORS, min_width=60, min_height=10)
                images = await _find_images_in(container, slider=slider, track=track)
                if images is None:
                    continue
                background, piece = images
                if not _scene_geometry_is_plausible(background, slider, track):
                    continue
                return CaptchaScene(scope=scope, background=background, piece=piece, slider=slider, track=track)
            except Exception:
                continue

        fallback_scene = await _extract_scene_from_scope(scope)
        if fallback_scene is not None:
            return fallback_scene
    return None


async def _read_image_bytes(snapshot: ElementSnapshot | None) -> bytes | None:
    if snapshot is None:
        return None
    try:
        return await snapshot.locator.screenshot(type="png")
    except Exception:
        return None


async def _detect_gap_candidates(scene: CaptchaScene, config: SolverConfig) -> tuple[list[GapCandidate], float]:
    bg_bytes = await _read_image_bytes(scene.background)
    piece_bytes = await _read_image_bytes(scene.piece)
    if not bg_bytes:
        return [], scene.background.box["width"]

    bg_image = _bytes_to_cv(bg_bytes)
    image_width = float(bg_image.shape[1]) if bg_image is not None else float(scene.background.box["width"])
    candidates: list[GapCandidate] = []

    if piece_bytes:
        for detector in (find_gap_by_template, find_gap_by_sobel_multi):
            candidate = detector(bg_bytes, piece_bytes)
            if candidate is not None:
                candidates.append(candidate)

    for detector in (find_gap_by_variance, find_gap_by_yolo, find_gap_by_contour):
        candidate = detector(bg_bytes)
        if candidate is not None:
            candidates.append(candidate)

    if config.ai_api_key:
        candidate = find_gap_by_ai_vision(
            bg_bytes,
            api_key=config.ai_api_key,
            provider=config.ai_provider,
            model=config.ai_model,
            piece_bytes=piece_bytes,
            base_url=config.ai_base_url,
        )
        if candidate is not None:
            candidates.append(candidate)

    return candidates, image_width


async def _read_post_drag_state(scene: CaptchaScene) -> PostDragState:
    try:
        body_text = await scene.scope.evaluate("() => document.body ? document.body.innerText || '' : ''")
    except Exception:
        body_text = ""

    try:
        slider_visible = await scene.slider.locator.is_visible(timeout=250)
    except Exception:
        slider_visible = False

    try:
        background_visible = await scene.background.locator.is_visible(timeout=250)
    except Exception:
        background_visible = False

    normalized_text = " ".join(str(body_text or "").split())
    success_visible = bool(SUCCESS_TEXT_RE.search(normalized_text))
    retry_visible = bool(RETRY_TEXT_RE.search(normalized_text))
    captcha_visible = slider_visible or background_visible
    return PostDragState(
        captcha_visible=captcha_visible,
        success_visible=success_visible,
        retry_visible=retry_visible,
    )


async def _refresh_captcha(scene: CaptchaScene) -> bool:
    for selector in [
        'button:has-text("Refresh")',
        'button:has-text("刷新")',
        '[role="button"]:has-text("Refresh")',
        '[role="button"]:has-text("刷新")',
        '[class*="refresh" i]',
        '[aria-label*="refresh" i]',
    ]:
        try:
            locator = scene.scope.locator(selector).first
            if await locator.is_visible(timeout=250):
                await locator.click(timeout=1000)
                await scene.scope.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


async def _perform_drag(scene: CaptchaScene, distance_px: float) -> DragAttemptResult:
    slider_box_before = await scene.slider.locator.bounding_box()
    if not slider_box_before:
        return DragAttemptResult(success=False, moved_px=0.0, state=PostDragState(True, False, False))

    start_x = slider_box_before["x"] + slider_box_before["width"] / 2
    start_y = slider_box_before["y"] + slider_box_before["height"] / 2
    end_x = start_x + distance_px
    steps = max(12, int(abs(distance_px) // 7))
    drag_path = _build_drag_path(start_x, end_x, start_y, steps)

    owning_page = scene.scope if isinstance(scene.scope, Page) else scene.scope.page

    cdp_session = None
    try:
        cdp_session = await owning_page.context.new_cdp_session(owning_page)
    except Exception:
        cdp_session = None

    if cdp_session is not None:
        await cdp_session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(start_x), "y": int(start_y), "button": "none", "pointerType": "mouse"})
        await scene.scope.wait_for_timeout(random.randint(80, 160))
        await cdp_session.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": int(start_x), "y": int(start_y), "button": "left", "buttons": 1, "clickCount": 1, "pointerType": "mouse"})
        await scene.scope.wait_for_timeout(random.randint(90, 180))
        for current_x, current_y in drag_path:
            await cdp_session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(current_x), "y": int(current_y), "button": "left", "buttons": 1, "pointerType": "mouse"})
            await scene.scope.wait_for_timeout(random.randint(10, 24))
        overshoot = random.randint(3, 7)
        await cdp_session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(end_x + overshoot), "y": int(start_y), "button": "left", "buttons": 1, "pointerType": "mouse"})
        await scene.scope.wait_for_timeout(random.randint(70, 130))
        await cdp_session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(end_x), "y": int(start_y), "button": "left", "buttons": 1, "pointerType": "mouse"})
        await scene.scope.wait_for_timeout(random.randint(50, 120))
        await cdp_session.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": int(end_x), "y": int(start_y), "button": "left", "buttons": 0, "clickCount": 1, "pointerType": "mouse"})
        try:
            await cdp_session.detach()
        except Exception:
            pass
    else:
        await owning_page.mouse.move(start_x, start_y)
        await scene.scope.wait_for_timeout(random.randint(80, 160))
        await owning_page.mouse.down()
        await scene.scope.wait_for_timeout(random.randint(90, 180))
        for current_x, current_y in drag_path:
            await owning_page.mouse.move(current_x, current_y)
            await scene.scope.wait_for_timeout(random.randint(10, 24))
        await scene.scope.wait_for_timeout(random.randint(80, 140))
        await owning_page.mouse.up()

    await scene.scope.wait_for_timeout(1100)
    slider_box_after = await scene.slider.locator.bounding_box()
    moved_px = 0.0
    if slider_box_after:
        moved_px = slider_box_after["x"] - slider_box_before["x"]
    state = await _read_post_drag_state(scene)
    success = (not state.captcha_visible and not state.retry_visible) or state.success_visible
    return DragAttemptResult(success=success, moved_px=moved_px, state=state)


async def solve_slider_captcha_with_result(page: Page, max_attempts: int = 3) -> SolverOutcome:
    config = load_solver_config()
    reports: list[SolverAttemptReport] = []

    for attempt in range(1, max_attempts + 1):
        print(f"  [Captcha] Auto-solve attempt {attempt}/{max_attempts}...")
        scene = await _find_captcha_scene(page)
        if scene is None:
            print("  [Captcha] No captcha scene found.")
            return SolverOutcome(solved=False, reason="no_scene", reports=reports)

        gap_candidates, image_width = await _detect_gap_candidates(scene, config)
        chosen_candidates = select_gap_candidates(gap_candidates, image_width, limit=2)
        if not chosen_candidates:
            print("  [Captcha] No usable gap candidate detected.")
            await scene.scope.wait_for_timeout(900)
            await _refresh_captcha(scene)
            continue

        slider_width = scene.slider.box["width"]
        track_width = scene.track.box["width"] if scene.track else None
        for chosen in chosen_candidates:
            distances = build_drag_distance_candidates(
                gap_left_px=chosen.gap_left_px,
                image_width_px=image_width,
                background_width_css=scene.background.box["width"],
                slider_width_css=slider_width,
                track_width_css=track_width,
            )
            report = SolverAttemptReport(
                strategy=chosen.strategy,
                confidence=chosen.confidence,
                gap_left_px=chosen.gap_left_px,
                distances=distances,
            )
            reports.append(report)
            print(
                f"  [Captcha] Strategy={chosen.strategy}, gap_left={chosen.gap_left_px:.1f}px, "
                f"confidence={chosen.confidence:.2f}, distances={distances}"
            )

            for distance in distances:
                drag_result = await _perform_drag(scene, distance)
                report.last_drag_result = drag_result
                print(
                    f"  [Captcha] Drag {distance:.0f}px -> moved={drag_result.moved_px:.1f}px, "
                    f"captcha_visible={drag_result.state.captcha_visible}, retry_visible={drag_result.state.retry_visible}, "
                    f"success_visible={drag_result.state.success_visible}"
                )
                if drag_result.success:
                    report.success = True
                    report.final_reason = "provisional_success"
                    print("  [Captcha] Auto-solve provisional SUCCESS.")
                    return SolverOutcome(solved=True, reason="provisional_success", reports=reports)

                moved_px = abs(drag_result.moved_px)
                if moved_px < max(6.0, slider_width * 0.18):
                    report.final_reason = "barely_moved"
                    print("  [Captcha] Slider barely moved; trying next candidate or refresh.")
                    break

                if drag_result.state.retry_visible:
                    report.final_reason = "retry_visible"
                    print("  [Captcha] Retry state detected after drag; trying next candidate or refresh.")
                    break

            if not report.final_reason:
                report.final_reason = "candidate_exhausted"

        await _refresh_captcha(scene)
        await scene.scope.wait_for_timeout(1200)

    print(f"  [Captcha] Auto-solve failed after {max_attempts} attempts.")
    return SolverOutcome(solved=False, reason="attempts_exhausted", reports=reports)


async def solve_slider_captcha(page: Page, max_attempts: int = 3) -> bool:
    outcome = await solve_slider_captcha_with_result(page, max_attempts=max_attempts)
    return outcome.solved
