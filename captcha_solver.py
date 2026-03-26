"""
OpenCV-based puzzle slider captcha solver for TikTok Seller Center.

Uses multi-strategy approach:
  Strategy C (Priority): AI Vision (OpenAI / Gemini) gap detection.
  Strategy A: Template matching (Canny edge + matchTemplate) when both
              piece image and background image are available.
  Strategy B: Contour-based gap detection on background-only screenshot.

Searches both main page and iframes for captcha elements.
Falls back gracefully on any failure so caller can use manual resume.
"""

import random

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# 1. OpenCV core
# ---------------------------------------------------------------------------

def _bytes_to_cv(img_bytes: bytes) -> np.ndarray:
    """Convert raw PNG/JPEG bytes to OpenCV BGR image."""
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def gap_x_to_drag_distance(gap_x: float) -> float:
    """
    TikTok slider uses a NON-LINEAR mapping between gap position and drag distance.
    Formula from reverse engineering:  y = 14.7585 * x^0.5190 - 3.9874
    where x = gap coordinate (in image pixels), y = drag distance (in CSS pixels).
    Error < 1px in most cases.
    """
    if gap_x <= 0:
        return 0.0
    return 14.7585 * (gap_x ** 0.5190) - 3.9874


def find_gap_by_variance(bg_bytes: bytes) -> float | None:
    """
    Strategy F: Column variance analysis for gap detection.
    
    The gap area has very different pixel distribution compared to the
    surrounding natural image - it's typically a solid color fill or
    semi-transparent overlay, which creates high variance spikes.
    
    Uses a sliding window to find the region with highest column variance.
    Returns gap LEFT EDGE X as absolute pixel position.
    """
    bg_img = _bytes_to_cv(bg_bytes)
    if bg_img is None:
        return None
    h, w = bg_img.shape[:2]
    gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
    
    # Compute per-column variance across all rows
    col_var = np.var(gray.astype(np.float32), axis=0)
    
    # Skip first 25% (puzzle piece zone) and last 5%
    start_col = int(w * 0.25)
    end_col = int(w * 0.95)
    
    # Use sliding window (~45px, approximate gap width) to find highest variance region
    window = 45
    best_score = 0
    best_col = -1
    
    for col in range(start_col, end_col - window):
        score = float(np.mean(col_var[col:col + window]))
        if score > best_score:
            best_score = score
            best_col = col
    
    if best_col < 0:
        print("  [Variance] No gap found.")
        return None
    
    # Verify: the gap area should have significantly higher variance than surroundings
    overall_var = float(np.mean(col_var[start_col:end_col]))
    ratio = best_score / max(overall_var, 1.0)
    
    print(f"  [Variance] gap_left={best_col}, score={best_score:.0f}, "
          f"avg={overall_var:.0f}, ratio={ratio:.2f}")
    
    if ratio < 1.2:
        # Gap variance not significantly different from surroundings
        print("  [Variance] Variance ratio too low, unreliable.")
        return None
    
    gap_center = best_col + window // 2
    print(f"  [Variance] gap_left_x={best_col}, gap_center={gap_center}")
    return float(best_col)


def find_gap_by_yescaptcha(bg_bytes: bytes, client_key: str, img_width: int) -> float | None:
    """
    Strategy Y: Use YesCaptcha cloud API to detect gap position.
    
    Sends the background image to YesCaptcha's ImageToTextTask.
    The service (AI + human) identifies the gap X coordinate.
    Returns gap X as absolute pixel position.
    """
    import base64, json, urllib.request, time
    
    b64_img = base64.b64encode(bg_bytes).decode('utf-8')
    
    # Step 1: Create task
    create_url = "https://api.yescaptcha.com/createTask"
    payload = {
        "clientKey": client_key,
        "task": {
            "type": "ImageToTextTaskMuggle",
            "body": b64_img,
        }
    }
    
    try:
        req = urllib.request.Request(
            create_url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        print("  [YesCaptcha] Creating task...")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        
        print(f"  [YesCaptcha] createTask response: {result}")
        
        if result.get("errorId", 1) != 0:
            print(f"  [YesCaptcha] Error: {result.get('errorDescription', 'unknown')}")
            return None
        
        # For Muggle type, result is returned immediately
        solution = result.get("solution", {})
        text = solution.get("text", "")
        task_id = result.get("taskId", "")
        
        # If solution available immediately
        if text:
            print(f"  [YesCaptcha] Immediate result: '{text}'")
            # Try to parse as number (gap X coordinate or ratio)
            try:
                val = float(text.strip().replace(',', '.'))
                if val <= 1.0:
                    # It's a ratio
                    gap_x = val * img_width
                else:
                    # It's a pixel value
                    gap_x = val
                print(f"  [YesCaptcha] Parsed gap_x={gap_x:.1f}")
                return gap_x
            except ValueError:
                print(f"  [YesCaptcha] Could not parse '{text}' as number")
        
        # Step 2: Poll for result if async
        if task_id:
            result_url = "https://api.yescaptcha.com/getTaskResult"
            for attempt in range(10):
                time.sleep(2)
                poll_payload = {"clientKey": client_key, "taskId": task_id}
                req2 = urllib.request.Request(
                    result_url,
                    data=json.dumps(poll_payload).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )
                with urllib.request.urlopen(req2, timeout=15) as resp2:
                    poll_result = json.loads(resp2.read().decode('utf-8'))
                
                status = poll_result.get("status", "")
                print(f"  [YesCaptcha] Poll #{attempt+1}: status={status}")
                
                if status == "ready":
                    sol = poll_result.get("solution", {})
                    text = sol.get("text", "")
                    print(f"  [YesCaptcha] Result: '{text}'")
                    try:
                        val = float(text.strip().replace(',', '.'))
                        if val <= 1.0:
                            gap_x = val * img_width
                        else:
                            gap_x = val
                        print(f"  [YesCaptcha] Parsed gap_x={gap_x:.1f}")
                        return gap_x
                    except ValueError:
                        print(f"  [YesCaptcha] Could not parse '{text}' as number")
                        return None
                elif status == "processing":
                    continue
                else:
                    print(f"  [YesCaptcha] Unexpected status: {poll_result}")
                    return None
        
        return None
    except Exception as e:
        print(f"  [YesCaptcha] Error: {e}")
        return None


def _remove_whitespace(image: np.ndarray) -> np.ndarray:
    """Crop image to remove surrounding whitespace/transparent areas."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)
    coords = cv2.findNonZero(thresh)
    if coords is None:
        return image
    x, y, w, h = cv2.boundingRect(coords)
    return image[y:y+h, x:x+w]


def find_gap_by_template(bg_bytes: bytes, piece_bytes: bytes) -> float | None:
    """
    Strategy A: Template matching (PuzzleCaptchaSolver approach).
    Returns gap center X as a RATIO (0.0 ~ 1.0) of background width.
    """
    bg = _bytes_to_cv(bg_bytes)
    piece = _bytes_to_cv(piece_bytes)
    if bg is None or piece is None:
        return None

    h_bg, w_bg = bg.shape[:2]
    piece = _remove_whitespace(piece)
    h_p, w_p = piece.shape[:2]

    if w_p >= w_bg or h_p >= h_bg or w_p < 10 or h_p < 10:
        return None

    bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
    piece_gray = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY)

    bg_edges = cv2.Canny(bg_gray, 100, 200)
    piece_edges = cv2.Canny(piece_gray, 100, 200)

    result = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    if max_val < 0.15:
        print(f"  [Template] Low confidence: {max_val:.3f}")
        return None

    gap_center_x = max_loc[0] + w_p / 2
    ratio = gap_center_x / w_bg
    print(f"  [Template] Match at x={max_loc[0]}, confidence={max_val:.3f}, ratio={ratio:.3f}")
    return ratio


def find_gap_by_sobel_multi(bg_bytes: bytes, piece_bytes: bytes) -> float | None:
    """
    Strategy E: TikTok-specialized Sobel + CLAHE + multi-method template matching.
    Based on github.com/Gisnsl/tiktok-captcha-solver PuzzleSolver.
    Uses Sobel gradient, CLAHE enhancement, and Canny edges with multiple
    template matching methods, picks the highest confidence result.
    Returns gap center X as a RATIO (0.0 ~ 1.0) of background width.
    """
    bg = _bytes_to_cv(bg_bytes)
    piece = _bytes_to_cv(piece_bytes)
    if bg is None or piece is None:
        return None

    h_bg, w_bg = bg.shape[:2]
    piece = _remove_whitespace(piece)
    h_p, w_p = piece.shape[:2]

    if w_p >= w_bg or h_p >= h_bg or w_p < 10 or h_p < 10:
        return None

    def _sobel(img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        gx = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)
        ax = cv2.convertScaleAbs(gx)
        ay = cv2.convertScaleAbs(gy)
        grad = cv2.addWeighted(ax, 0.5, ay, 0.5, 0)
        return cv2.normalize(grad, None, 0, 255, cv2.NORM_MINMAX)

    def _enhance(img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return clahe.apply(gray)

    def _edges(img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.Canny(blurred, 50, 150)

    def _match_all(a, b):
        out = []
        for m in (cv2.TM_CCOEFF_NORMED, cv2.TM_CCORR_NORMED):
            matched = cv2.matchTemplate(b, a, m)
            _, mx, _, mx_loc = cv2.minMaxLoc(matched)
            out.append((mx_loc[0], mx))
        return out

    def _match_single(a, b):
        matched = cv2.matchTemplate(b, a, cv2.TM_CCOEFF_NORMED)
        _, mx, _, mx_loc = cv2.minMaxLoc(matched)
        return (mx_loc[0], mx)

    try:
        p_sobel = _sobel(piece)
        t_sobel = _sobel(bg)

        results = _match_all(p_sobel, t_sobel)
        results += _match_all(_enhance(piece), _enhance(bg))
        results.append(_match_single(_edges(piece), _edges(bg)))

        results.sort(key=lambda x: x[1], reverse=True)
        best_x, best_conf = results[0]

        if best_conf < 0.15:
            print(f"  [SobelMulti] Low confidence: {best_conf:.3f}")
            return None

        gap_center_x = best_x + w_p / 2
        ratio = gap_center_x / w_bg
        print(f"  [SobelMulti] Best match x={best_x}, conf={best_conf:.3f}, ratio={ratio:.3f}")
        return ratio
    except Exception as e:
        print(f"  [SobelMulti] Error: {e}")
        return None


def find_gap_by_contour(img_bytes: bytes) -> float | None:
    """
    Strategy B: Contour-based gap detection from single background image.
    Returns gap center X as a RATIO (0.0 ~ 1.0) of image width.
    """
    img = _bytes_to_cv(img_bytes)
    if img is None:
        return None

    h, w = img.shape[:2]
    if h < 20 or w < 20:
        return None

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 100, 200)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dilated = cv2.dilate(edges, kernel, iterations=2)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    min_x = int(w * 0.3)
    min_side = int(min(h, w) * 0.08)
    max_side = int(max(h, w) * 0.45)

    candidates = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if x < min_x:
            continue
        if cw < min_side or ch < min_side or cw > max_side or ch > max_side:
            continue
        aspect = cw / max(ch, 1)
        if aspect < 0.5 or aspect > 2.0:
            continue
        roi = edges[y:y+ch, x:x+cw]
        edge_density = np.count_nonzero(roi) / max(cw * ch, 1)
        center_x_ratio = (x + cw / 2) / w
        candidates.append((center_x_ratio, edge_density, x, y, cw, ch))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[1], reverse=True)
    best = candidates[0]
    print(f"  [Contour] Gap candidate: x={best[2]}, y={best[3]}, "
          f"w={best[4]}, h={best[5]}, density={best[1]:.3f}, ratio={best[0]:.3f}")
    return best[0]


def find_gap_by_ai_vision(bg_bytes: bytes, api_key: str, provider: str = 'openai',
                          model: str = '', piece_bytes: bytes = None,
                          base_url: str = '') -> float | None:
    """
    Strategy C: AI Vision (OpenAI / Gemini) gap detection.
    Sends background + piece screenshot to vision model.
    Returns gap center X as a RATIO (0.0 ~ 1.0) of background width.
    """
    import base64
    import re

    bg_img = _bytes_to_cv(bg_bytes)
    if bg_img is None:
        return None
    h, w = bg_img.shape[:2]

    # Prompt: ask for the LEFT EDGE X pixel of the gap/hole only
    # We send ONLY the background image (not piece), so AI focuses on the gap
    prompt = (
        f"This image is a CAPTCHA slider puzzle background, {w} pixels wide.\n"
        "There is a visible darker GAP or HOLE (a missing puzzle piece shape) in the image.\n"
        "Return ONLY the integer X pixel coordinate of the LEFT EDGE of the gap/hole, "
        "measured from the left side of the image.\n"
        "Just one integer, nothing else. Example: 185"
    )

    # Send ONLY background image (not combined with piece) for cleaner detection
    send_bytes = bg_bytes

    b64 = base64.b64encode(send_bytes).decode('utf-8')
    print(f"  [AIVision] provider={provider}, model={model or 'default'}, img={w}x{h}")

    try:
        raw = None
        if provider == 'openai':
            from openai import OpenAI
            client_kwargs = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = OpenAI(**client_kwargs)
            use_model = model or 'gpt-4o'
            response = client.chat.completions.create(
                model=use_model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}}
                    ]}
                ],
                temperature=0,
                max_tokens=20
            )
            raw = response.choices[0].message.content.strip()

        elif provider == 'gemini':
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            use_model = model or 'gemini-2.0-flash'
            gm = genai.GenerativeModel(use_model)
            import PIL.Image, io
            img_pil = PIL.Image.open(io.BytesIO(send_bytes))
            response = gm.generate_content([prompt, img_pil])
            raw = response.text.strip()

        else:
            print(f"  [AIVision] Unknown provider: {provider}")
            return None

        print(f"  [AIVision] Raw response: {repr(raw)}")
        match = re.search(r'\d+', raw)
        if match:
            gap_left_x = int(match.group())
            # Return as ratio of image width (this is the LEFT EDGE of gap)
            ratio = gap_left_x / w
            print(f"  [AIVision] gap_left_x={gap_left_x}, ratio={ratio:.3f}")
            return ratio
        print(f"  [AIVision] Could not parse integer from response")

    except Exception as e:
        import traceback
        print(f"  [AIVision] Request failed: {e}")
        print(f"  [AIVision] Traceback: {traceback.format_exc()}")

    return None


def find_gap_by_yolo(bg_bytes: bytes) -> float | None:
    """
    Strategy D: YOLO-based gap detection using captcha-recognizer.
    Uses a pre-trained YOLOv11 ONNX model to detect the gap position.
    Returns gap center X as a RATIO (0.0 ~ 1.0) of background width.
    """
    try:
        from captcha_recognizer.slider import Slider
    except ImportError:
        print("  [YOLO] captcha-recognizer not installed. Skip.")
        return None

    bg_img = _bytes_to_cv(bg_bytes)
    if bg_img is None:
        return None
    h, w = bg_img.shape[:2]

    try:
        model = Slider()
        box, conf = model.identify(source=bg_img, show=False)
        print(f"  [YOLO] box={box}, conf={conf:.3f}")

        if not box or conf < 0.3:
            print(f"  [YOLO] No gap detected or low confidence.")
            return None

        x1, y1, x2, y2 = box[:4]
        
        # Filter: if box is too close to the left edge (< 10% of width),
        # it's likely the puzzle piece, not the gap.
        # Record piece_right_edge for dynamic offset calculation.
        if x1 < w * 0.10:
            piece_right_edge = x2  # right edge of puzzle piece
            print(f"  [YOLO] Box too close to left edge (x1={x1:.1f}), puzzle piece. piece_right_edge={piece_right_edge:.1f}")
            return None, piece_right_edge  # return piece info even though gap not found
        
        # Use LEFT EDGE x1 as gap position
        ratio = x1 / w
        print(f"  [YOLO] gap_left_x={x1:.1f}, ratio={ratio:.3f}")
        return ratio, None  # gap found, no separate piece detection

    except Exception as e:
        import traceback
        print(f"  [YOLO] Error: {e}")
        print(f"  [YOLO] {traceback.format_exc()}")
        return None


# ---------------------------------------------------------------------------
# 2. Playwright element finders — search main page + all iframes
# ---------------------------------------------------------------------------

async def _search_frames(page, finder_fn):
    """
    Run finder_fn on main page first, then on each iframe frame.
    Returns the first non-None result and the frame it was found in.
    """
    # Try main page first
    result = await finder_fn(page)
    if result is not None:
        return result, page

    # Try each iframe/frame
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            result = await finder_fn(frame)
            if result is not None:
                return result, frame
        except Exception:
            continue

    return None, None


async def _find_images_in(scope):
    """Find captcha image(s) within a given page or frame scope."""
    selectors = [
        '[class*="captcha" i] img',
        '[class*="verify" i] img',
        '[class*="secsdk" i] img',
        '[class*="slider" i] img',
        'img[class*="captcha" i]',
        'img[class*="verify" i]',
        'canvas',
        'img',
    ]

    all_imgs = []
    seen_srcs = set()
    for sel in selectors:
        try:
            items = scope.locator(sel)
            count = await items.count()
            for i in range(count):
                loc = items.nth(i)
                try:
                    if not await loc.is_visible(timeout=300):
                        continue
                except Exception:
                    continue
                box = await loc.bounding_box()
                if not box or box["width"] < 30 or box["height"] < 30:
                    continue
                # Deduplicate by position
                key = f"{round(box['x'])}_{round(box['y'])}_{round(box['width'])}"
                if key in seen_srcs:
                    continue
                seen_srcs.add(key)
                all_imgs.append((loc, box))
        except Exception:
            continue

    if not all_imgs:
        return None

    # Sort by area descending — largest is likely the background
    all_imgs.sort(key=lambda x: x[1]["width"] * x[1]["height"], reverse=True)

    bg = all_imgs[0]
    piece = None
    if len(all_imgs) >= 2:
        bg_area = bg[1]["width"] * bg[1]["height"]
        p = all_imgs[1]
        p_area = p[1]["width"] * p[1]["height"]
        if p_area < bg_area * 0.5:
            piece = p

    return {"bg": bg, "piece": piece}


async def _find_slider_in(scope):
    """Find slider button within a given page or frame scope.
    
    IMPORTANT: The slider handle must be large enough to be draggable (>= 28px).
    Elements smaller than 28px are likely icons inside the handle, not the handle itself.
    When a small element is found, we try to use its PARENT as the actual handle.
    """
    selectors = [
        # Specific captcha slider selectors
        '[class*="secsdk" i] [class*="btn" i]',
        '[class*="secsdk" i] [class*="icon" i]',
        '[class*="secsdk" i] [class*="handler" i]',
        '[class*="secsdk" i] [class*="handle" i]',
        '[class*="captcha" i] [class*="btn" i]',
        '[class*="captcha" i] [class*="handler" i]',
        '[class*="captcha" i] [class*="handle" i]',
        '[class*="verify" i] [class*="btn" i]',
        '[class*="verify" i] [class*="handler" i]',
        '[class*="verify" i] [class*="handle" i]',
        '[class*="slider" i] [class*="btn" i]',
        '[class*="slider" i] [class*="icon" i]',
        '[class*="slider" i] [class*="handler" i]',
        '[class*="slider" i] [class*="handle" i]',
        # Generic drag/slide elements
        '[class*="drag" i]',
        '[class*="slide" i]:not([class*="slider" i])',
        # Role/aria selectors
        '[role="slider"]',
        '[aria-label*="slider" i]',
        '[aria-label*="drag" i]',
        '[aria-label*="slide" i]',
    ]
    
    best_candidate = None
    best_area = 0
    
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if not await loc.is_visible(timeout=300):
                continue
            box = await loc.bounding_box()
            if not box:
                continue
                
            # Get element info for diagnostics
            tag = await loc.evaluate("el => el.tagName")
            cls = await loc.evaluate("el => el.className || ''")
            w, h = box["width"], box["height"]
            
            # Skip elements that are way too large (probably containers)
            if w > 200 or h > 200:
                continue
            
            # If element is too SMALL (< 28px), it's likely an icon INSIDE the handle.
            # Try its parent element instead.
            if w < 28 or h < 28:
                print(f"  [SliderFind] Skipping small element: {tag}.{cls[:40]} ({w:.0f}x{h:.0f}), trying parent...")
                try:
                    parent = scope.locator(f"{sel} >> xpath=..")
                    if await parent.first.is_visible(timeout=300):
                        p_box = await parent.first.bounding_box()
                        if p_box and p_box["width"] >= 28 and p_box["height"] >= 20 and p_box["width"] < 200:
                            p_tag = await parent.first.evaluate("el => el.tagName")
                            p_cls = await parent.first.evaluate("el => el.className || ''")
                            print(f"  [SliderFind] Using parent: {p_tag}.{p_cls[:40]} ({p_box['width']:.0f}x{p_box['height']:.0f})")
                            area = p_box["width"] * p_box["height"]
                            if area > best_area:
                                best_candidate = parent.first
                                best_area = area
                            continue
                except Exception:
                    pass
                continue
            
            # Good candidate: not too small, not too large
            area = w * h
            if area > best_area:
                print(f"  [SliderFind] Candidate: {tag}.{cls[:40]} ({w:.0f}x{h:.0f})")
                best_candidate = loc
                best_area = area
                
        except Exception:
            continue
    
    return best_candidate


async def _find_track_in(scope):
    """Find slider track within a given page or frame scope."""
    selectors = [
        '[class*="secsdk" i] [class*="track" i]',
        '[class*="secsdk" i] [class*="bar" i]',
        '[class*="secsdk" i] [class*="rail" i]',
        '[class*="captcha" i] [class*="track" i]',
        '[class*="captcha" i] [class*="bar" i]',
        '[class*="verify" i] [class*="track" i]',
        '[class*="slider" i] [class*="track" i]',
        '[class*="slider" i] [class*="bar" i]',
        '[class*="slider" i] [class*="rail" i]',
    ]
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if await loc.is_visible(timeout=300):
                return loc
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 3. Main solver
# ---------------------------------------------------------------------------

async def solve_slider_captcha(page, max_attempts: int = 3) -> bool:
    """
    Attempt to automatically solve a puzzle slider captcha.
    Searches both main page and iframes for captcha elements.

    Returns True if captcha was solved, False otherwise.
    """
    for attempt in range(max_attempts):
        print(f"  Auto-solve attempt {attempt + 1}/{max_attempts}...")

        try:
            # Step 1: Find captcha images (check main page + iframes)
            img_result, img_frame = await _search_frames(page, _find_images_in)
            if not img_result:
                print("  Could not find captcha image in any frame.")
                return False

            bg_loc, bg_box = img_result["bg"]
            piece_info = img_result.get("piece")
            print(f"  Found captcha image: {bg_box['width']:.0f}x{bg_box['height']:.0f} in {'iframe' if img_frame != page else 'main page'}")

            # Step 2: Find slider button (check all frames)
            slider_btn, slider_frame = await _search_frames(page, _find_slider_in)
            if not slider_btn:
                print("  Could not find slider button in any frame.")
                return False

            slider_box = await slider_btn.bounding_box()
            print(f"  Found slider button: {slider_box['width']:.0f}x{slider_box['height']:.0f} "
                  f"at ({slider_box['x']:.0f},{slider_box['y']:.0f}) "
                  f"in {'iframe' if slider_frame != page else 'main page'}")

            # Step 3: Screenshot images
            bg_screenshot = None
            piece_screenshot = None
            try:
                bg_screenshot = await bg_loc.screenshot(type="png")
            except Exception:
                pass
            if not bg_screenshot:
                print("  Could not screenshot background image.")
                continue

            if piece_info:
                try:
                    piece_screenshot = await piece_info[0].screenshot(type="png")
                    print(f"  Found piece image: {piece_info[1]['width']:.0f}x{piece_info[1]['height']:.0f}")
                except Exception:
                    pass
            else:
                print("  No piece image found (single background only)")

            # Step 4: Detect gap position
            gap_ratio = None
            
            # --- DEBUG INFO: Save background image for manual inspection ---
            try:
                with open(r"c:\tmp\debug_bg.png", "wb") as f:
                    f.write(bg_screenshot)
            except:
                pass

            # Load AI config once (used by Strategy C first)
            import json, os, sys
            if getattr(sys, 'frozen', False):
                _base_dir = os.path.dirname(sys.executable)
            else:
                _base_dir = os.path.dirname(os.path.abspath(__file__))
            _config_path = os.path.join(_base_dir, "gui_config.json")
            _ai_api_key = ""
            _ai_provider = "openai"
            _ai_model = ""
            _ai_base_url = ""
            if os.path.exists(_config_path):
                try:
                    _cfg = json.load(open(_config_path, "r", encoding="utf-8"))
                    _ai_api_key = _cfg.get("ai_api_key", "").strip()
                    _ai_provider = _cfg.get("ai_provider", "openai").strip()
                    _ai_model = _cfg.get("ai_model", "").strip()
                    _ai_base_url = _cfg.get("ai_base_url", "").strip()
                except Exception:
                    pass
            print(f"  [DEBUG] Config: {_config_path}, ai_provider={_ai_provider}, key={'set' if _ai_api_key else 'empty'}")

            # Get screenshot pixel dimensions for coordinate conversion
            import cv2 as _cv2, numpy as _np
            _img = _cv2.imdecode(_np.frombuffer(bg_screenshot, _np.uint8), _cv2.IMREAD_UNCHANGED)
            img_pixel_width = _img.shape[1] if _img is not None else bg_box["width"]
            
            # ------- DETECT GAP POSITION (as absolute pixel X in screenshot) --------
            gap_x_pixel = None  # absolute pixel position in screenshot
            used_strategy = None

            # Strategy Y (TOP PRIORITY): YesCaptcha cloud API
            # AI + human recognition - most accurate for complex puzzles
            YESCAPTCHA_KEY = "bc1ce5b5b67ff2540ea3860060c195f4becd543685295"
            if YESCAPTCHA_KEY:
                try:
                    print("  Trying Strategy Y: YesCaptcha cloud API...")
                    y_result = find_gap_by_yescaptcha(bg_screenshot, YESCAPTCHA_KEY, img_pixel_width)
                    if y_result is not None:
                        gap_x_pixel = y_result
                        used_strategy = "YesCaptcha"
                        print(f"  [DEBUG] Strategy Y result: gap_x={gap_x_pixel:.1f}")
                except Exception as e:
                    print(f"  [DEBUG] Strategy Y error: {e}")

            # Strategy F: Column variance analysis (local fallback)
            # Most reliable local method - finds the gap by variance spike
            if gap_x_pixel is None:
              try:
                print("  Trying Strategy F: Column variance analysis...")
                f_result = find_gap_by_variance(bg_screenshot)
                if f_result is not None:
                    gap_x_pixel = f_result
                    used_strategy = "Variance"
                    print(f"  [DEBUG] Strategy F result: gap_x={gap_x_pixel}")
              except Exception as e:
                print(f"  [DEBUG] Strategy F error: {e}")

            # Strategy D: YOLO (captcha-recognizer)
            # NOTE: YOLO's identify() often detects the PUZZLE PIECE, not the gap!
            # Only trust results where x1 > 25% of image width.
            if gap_x_pixel is None:
                try:
                    print("  Trying Strategy D: YOLO (captcha-recognizer)...")
                    yolo_result = find_gap_by_yolo(bg_screenshot)
                    if yolo_result is not None:
                        d_ratio, piece_x2 = yolo_result
                        if d_ratio is not None:
                            candidate_x = d_ratio * img_pixel_width
                            # Only trust if beyond 25% - closer detections are likely the piece
                            if candidate_x > img_pixel_width * 0.25:
                                gap_x_pixel = candidate_x
                                used_strategy = "YOLO"
                                print(f"  [DEBUG] Strategy D result: gap_x={gap_x_pixel:.1f}")
                            else:
                                print(f"  [DEBUG] YOLO x={candidate_x:.1f} too close to left, likely piece. Skipping.")
                except Exception as e:
                    print(f"  [DEBUG] Strategy D error: {e}")

            # Strategy C: AI Vision (REMOTE, fallback)
            if gap_x_pixel is None and _ai_api_key:
                try:
                    print(f"  Trying Strategy C: AI Vision ({_ai_provider})...")
                    c_ratio = find_gap_by_ai_vision(
                        bg_screenshot, _ai_api_key, _ai_provider, _ai_model,
                        piece_bytes=piece_screenshot, base_url=_ai_base_url
                    )
                    if c_ratio is not None:
                        gap_x_pixel = c_ratio * img_pixel_width
                        used_strategy = "AIVision"
                        print(f"  [DEBUG] Strategy C result: gap_x={gap_x_pixel:.1f}")
                except Exception as e:
                    print(f"  [DEBUG] Strategy C error: {e}")

            # Strategy B: Contour detection (last resort)
            if gap_x_pixel is None:
                try:
                    print("  Trying Strategy B: Contour detection...")
                    b_ratio = find_gap_by_contour(bg_screenshot)
                    if b_ratio is not None:
                        gap_x_pixel = b_ratio * img_pixel_width
                        used_strategy = "Contour"
                        print(f"  [DEBUG] Strategy B result: gap_x={gap_x_pixel:.1f}")
                except Exception as e:
                    print(f"  [DEBUG] Strategy B error: {e}")

            if gap_x_pixel is None:
                print("  Could not detect gap position with any strategy.")
                continue

            # ------- CALCULATE DRAG DISTANCE --------
            # The puzzle piece starts at the LEFT EDGE of the image (x≈0).
            # When we drag by D pixels, the piece moves D pixels rightward.
            # So drag_distance = gap_x (how far the piece needs to move).
            # DO NOT subtract slider_center offset - it only affects where
            # the mouse starts, not how far the piece needs to travel.
            
            gap_ratio = gap_x_pixel / img_pixel_width
            drag_distance = int(round(gap_ratio * bg_box["width"]))
            
            slider_center_x = slider_box["x"] + slider_box["width"] / 2
            target_y = slider_box["y"] + slider_box["height"] / 2
            
            print(f"  [PICK] Using {used_strategy}, gap_x={gap_x_pixel:.1f}px (ratio={gap_ratio:.3f})")
            print(f"  [CALC] bg_box=({bg_box['x']:.0f},{bg_box['y']:.0f}) {bg_box['width']:.0f}x{bg_box['height']:.0f}")
            print(f"  [CALC] drag_distance={drag_distance}px (= gap_ratio * bg_width)")
            print(f"  [CALC] slider_center={slider_center_x:.0f}")
            
            if drag_distance <= 5:
                print(f"  Drag distance too small ({drag_distance}px), skipping.")
                continue

            # Step 6: Simulate drag using CDP pointer events.
            
            # Record position before drag
            pre_drag_box = await slider_btn.bounding_box()
            
            target_x = slider_center_x + drag_distance
            
            print(f"  [Drag] Using CDP pointer events...")
            print(f"  [Drag] From ({slider_center_x:.0f},{target_y:.0f}) -> ({target_x:.0f},{target_y:.0f})")
            
            # Use CDP to dispatch pointer events directly, which is more reliable
            # than page.mouse for captcha sliders
            cdp = None
            try:
                cdp = await page.context.new_cdp_session(page)
            except Exception:
                pass
            
            if cdp:
                # Use CDP Input.dispatchMouseEvent with proper pointer attributes
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": int(slider_center_x),
                    "y": int(target_y),
                    "button": "none",
                    "pointerType": "mouse",
                })
                await page.wait_for_timeout(random.randint(100, 200))
                
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mousePressed",
                    "x": int(slider_center_x),
                    "y": int(target_y),
                    "button": "left",
                    "buttons": 1,
                    "clickCount": 1,
                    "pointerType": "mouse",
                })
                await page.wait_for_timeout(random.randint(100, 200))
                
                # Move in multiple steps to simulate drag
                steps = max(10, drag_distance // 8)
                for i in range(1, steps + 1):
                    frac = i / steps
                    # Ease-in-out curve
                    if frac < 0.5:
                        ease = 2 * frac * frac
                    else:
                        ease = 1 - (-2 * frac + 2) ** 2 / 2
                    
                    cx = int(slider_center_x + drag_distance * ease)
                    cy = int(target_y + random.choice([-1, 0, 0, 1]))
                    
                    await cdp.send("Input.dispatchMouseEvent", {
                        "type": "mouseMoved",
                        "x": cx,
                        "y": cy,
                        "button": "left",
                        "buttons": 1,
                        "pointerType": "mouse",
                    })
                    await page.wait_for_timeout(random.randint(10, 30))
                
                # Small overshoot
                overshoot = random.randint(3, 8)
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": int(target_x + overshoot),
                    "y": int(target_y),
                    "button": "left",
                    "buttons": 1,
                    "pointerType": "mouse",
                })
                await page.wait_for_timeout(random.randint(100, 200))
                
                # Pull back to target
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": int(target_x),
                    "y": int(target_y),
                    "button": "left",
                    "buttons": 1,
                    "pointerType": "mouse",
                })
                await page.wait_for_timeout(random.randint(50, 150))
                
                # Release
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseReleased",
                    "x": int(target_x),
                    "y": int(target_y),
                    "button": "left",
                    "buttons": 0,
                    "clickCount": 1,
                    "pointerType": "mouse",
                })
                
                try:
                    await cdp.detach()
                except Exception:
                    pass
            else:
                # Fallback: use page.mouse (less reliable)
                print("  [Drag] CDP unavailable, using page.mouse fallback...")
                await page.mouse.move(slider_center_x, target_y)
                await page.wait_for_timeout(random.randint(100, 200))
                await page.mouse.down()
                await page.wait_for_timeout(random.randint(100, 200))
                
                steps = max(10, drag_distance // 8)
                cx, cy = slider_center_x, target_y
                for i in range(1, steps + 1):
                    frac = i / steps
                    if frac < 0.5:
                        ease = 2 * frac * frac
                    else:
                        ease = 1 - (-2 * frac + 2) ** 2 / 2
                    cx = slider_center_x + drag_distance * ease
                    cy = target_y + random.choice([-1, 0, 0, 1])
                    await page.mouse.move(cx, cy)
                    await page.wait_for_timeout(random.randint(10, 30))
                
                await page.wait_for_timeout(random.randint(80, 200))
                await page.mouse.up()
            
            # Step 7: Verify drag actually moved the slider
            await page.wait_for_timeout(500)
            post_drag_box = await slider_btn.bounding_box()
            if post_drag_box:
                dx = post_drag_box["x"] - pre_drag_box["x"]
                print(f"  [Drag] Slider moved: {dx:.0f}px (expected ~{drag_distance}px)")
                if abs(dx) < 5:
                    print("  [Drag] WARNING: Slider did NOT move! Events may not be reaching the element.")
            else:
                print("  [Drag] Could not verify slider position after drag")

            # Step 8: Check if solved
            await page.wait_for_timeout(1500)

            from flashsale_runner import detect_slider_captcha
            still_has = await detect_slider_captcha(page)
            if not still_has:
                print("  Auto-solve SUCCESS!")
                return True
            else:
                print("  Captcha still present after drag. Retrying...")
                try:
                    refresh_btn = page.locator(':text("Refresh"), :text("刷新")').first
                    if await refresh_btn.is_visible(timeout=500):
                        await refresh_btn.click()
                        await page.wait_for_timeout(1500)
                except Exception:
                    pass

        except Exception as e:
            import traceback
            print(f"  Auto-solve error: {e}")
            print(f"  {traceback.format_exc()}")
            continue

    print(f"  Auto-solve failed after {max_attempts} attempts.")
    return False
