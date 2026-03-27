import asyncio
import ctypes
import fnmatch
from collections.abc import Awaitable, Callable
from typing import TypedDict

solve_slider_captcha: Callable[..., Awaitable[bool]] | None = None
solve_slider_captcha_with_result: Callable[..., Awaitable[object]] | None = None
try:
    from captcha_solver import solve_slider_captcha as imported_solve_slider_captcha
    from captcha_solver import solve_slider_captcha_with_result as imported_solve_slider_captcha_with_result

    solve_slider_captcha = imported_solve_slider_captcha
    solve_slider_captcha_with_result = imported_solve_slider_captcha_with_result
    has_captcha_solver = True
except ImportError:
    has_captcha_solver = False
import os
import secrets
import re
import string
import subprocess
import urllib.request
from datetime import datetime, timedelta

from playwright.async_api import async_playwright


UPCOMING_PATTERNS = [
    "Upcoming",
    "即将",
    "Coming soon",
    "Akan datang",
    "Sắp diễn ra",
    "Prochain",
]
DUPLICATE_PATTERNS = ["Duplicate", "复制"]
EMPTY_PRODUCT_PATTERNS = [
    r"\bno\s+(?:products?|items?|skus?)\b",
    r"(?:products?|items?|skus?)\s+(?:not\s+found|empty)\b",
    r"暂无(?:商品|产品)",
    r"没有(?:商品|产品)",
    r"无(?:商品|产品)",
    r"空(?:商品|产品)",
]
ZERO_PRODUCT_PATTERNS = [
    r"\b0(?:\.0+)?\s*(?:products?|items?|skus?)\b",
    r"(?:products?|items?|skus?)\s*[:：]?\s*0(?:\.0+)?\b",
    r"\b0\s*(?:个)?(?:商品|产品)\b",
    r"(?:商品|产品)\s*[:：]?\s*0(?:\.0+)?\b",
]
EMPTY_PRODUCT_RE = re.compile("|".join(f"(?:{p})" for p in EMPTY_PRODUCT_PATTERNS), re.I)
ZERO_PRODUCT_RE = re.compile("|".join(f"(?:{p})" for p in ZERO_PRODUCT_PATTERNS), re.I)
POSITIVE_PRODUCT_COUNT_RE = re.compile(
    r"\b([1-9]\d*)(?:\.0+)?\s*(?:products?|items?|skus?)\b|(?:products?|items?|skus?)\s*[:：]?\s*([1-9]\d*)(?:\.0+)?\b|\b([1-9]\d*)\s*(?:个)?(?:商品|产品)\b|(?:商品|产品)\s*[:：]?\s*([1-9]\d*)(?:\.0+)?\b",
    re.I,
)
TIMESTAMP_RE = re.compile(r"(\d{4})-(\d{1,2})\.(\d{1,2})-(\d{2}:\d{2})")

runtime_base_dir = os.path.dirname(os.path.abspath(__file__))
MANAGEMENT_URL = "https://seller-th.tiktok.com/promotion/marketing-tools/management?tab=3&promotion_type=4&shop_region=TH"
NAME_SUFFIX_LEN = 4
batch_max_rounds = int(os.getenv("BATCH_MAX_ROUNDS", "60"))
CAPTCHA_WAIT_SECONDS = 600
auto_solve_captcha_enabled = False
seed_names_update_callback: Callable[[list[str]], None] | None = None
checkpoint_file_path = os.path.join(runtime_base_dir, ".schedule_anchor.txt")
manual_resume_flag_path = os.path.join(runtime_base_dir, ".manual_resume.flag")
manual_wait_max_seconds = int(os.getenv("MANUAL_WAIT_MAX_SECONDS", "3600"))
cdp_port: int | None = None  # If set, skip scanning and use this port directly
DEBUG_SCREENSHOT_ENV = "FLASHSALE_DEBUG_SCREENSHOTS"
RUNTIME_SCREENSHOT_PATTERNS = (
    "current_page_no_upcoming_detected.png",
    "current_page_no_usable_upcoming.png",
    "duplicate_click_failed.png",
    "duplicate_empty_products_blocked.png",
    "create_page_not_found.png",
    "captcha_detected_round_*.png",
    "set_one_flashsale_result_*.png",
)


class CdpAttempt(TypedDict):
    host: str
    url: str
    ok: bool
    error: str | None


class CdpDiagnostic(TypedDict):
    port: int
    attempts: list[CdpAttempt]
    ok: bool
    endpoint: str | None


class CdpEndpointInfo(TypedDict):
    port: int
    url: str
    browser: str
    shop_name: str
    label: str


class CdpProbe(TypedDict):
    port: int
    url: str
    browser: str


class PromoNameParts(TypedDict):
    raw: str
    dt: datetime
    prefix_full: str
    prefix_norm: str
    delim: str
    timestamp: str


class RawUpcomingRow(TypedDict):
    name: str
    text: str
    hasDuplicate: bool


class UpcomingRow(TypedDict):
    name: str
    text: str
    hasDuplicate: bool
    dt: datetime
    prefix_norm: str


class SeedScheduleEntry(TypedDict):
    name: str
    dt: datetime
    prefix_norm: str


class ParsedUpcomingEntry(TypedDict):
    name: str
    dt: datetime
    prefix: str


class ParsedPlanSource(TypedDict):
    name: str
    dt: datetime
    prefix_norm: str
    text: str
    hasDuplicate: bool
    raw: str
    prefix_full: str
    delim: str
    timestamp: str


class PromoPlan(TypedDict):
    source_name: str
    source_prefix: str
    new_name: str
    next_start: datetime
    next_end: datetime


class PlanDecision(TypedDict, total=False):
    status: str
    reason: str
    plan: PromoPlan


class ProductState(TypedDict):
    ok: bool
    reason: str
    visible_product_count: int
    empty_hits: list[str]
    zero_hits: list[str]
    row_samples: list[str]
    block_samples: list[str]


class ProductSnapshot(TypedDict):
    bodyText: str
    rowTexts: list[str]
    blockTexts: list[str]
    emptyTexts: list[str]
    productCountTexts: list[str]
    loadingTexts: list[str]
    rowCount: int
    blockCount: int


def normalize_promo_name(name: str) -> str:
    return strip_random_suffix((name or "").strip())


def configure_paths(base_dir: str):
    """Allow GUI/EXE wrapper to override runtime file locations."""
    global runtime_base_dir, checkpoint_file_path, manual_resume_flag_path
    runtime_base_dir = base_dir
    checkpoint_file_path = os.path.join(runtime_base_dir, ".schedule_anchor.txt")
    manual_resume_flag_path = os.path.join(runtime_base_dir, ".manual_resume.flag")


def configure_runtime(
    batch_rounds: int | None = None,
    manual_wait_max_seconds_override: int | None = None,
    cdp_port_override: int | None = None,
    auto_solve_captcha_override: bool | None = None,
    seed_names_update_callback_override: Callable[[list[str]], None] | None = None,
):
    global batch_max_rounds, manual_wait_max_seconds, cdp_port, auto_solve_captcha_enabled, seed_names_update_callback
    if batch_rounds is not None:
        batch_max_rounds = int(batch_rounds)
    if manual_wait_max_seconds_override is not None:
        manual_wait_max_seconds = int(manual_wait_max_seconds_override)
    if cdp_port_override is not None:
        cdp_port = int(cdp_port_override)
    if auto_solve_captcha_override is not None:
        auto_solve_captcha_enabled = bool(auto_solve_captcha_override)
    if seed_names_update_callback_override is not None:
        seed_names_update_callback = seed_names_update_callback_override


def _debug_screenshots_enabled() -> bool:
    value = os.getenv(DEBUG_SCREENSHOT_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _gui_config_path() -> str:
    return os.path.join(runtime_base_dir, "gui_config.json")


def _runtime_screenshot_dir() -> str:
    return os.path.join(runtime_base_dir, "_runtime_screenshots")


def _ensure_runtime_screenshot_dir():
    os.makedirs(_runtime_screenshot_dir(), exist_ok=True)


def _build_runtime_screenshot_path(file_name: str) -> str:
    _ensure_runtime_screenshot_dir()
    return os.path.join(_runtime_screenshot_dir(), file_name)


async def save_runtime_screenshot(page, file_name: str, full_page: bool = True) -> str | None:
    path = _build_runtime_screenshot_path(file_name)
    try:
        await page.screenshot(path=path, full_page=full_page)
        return path
    except Exception as exc:
        print(f"WARNING: failed to save screenshot {file_name}: {exc}")
        return None


async def save_debug_screenshot(page, file_name: str, full_page: bool = True) -> str | None:
    if not _debug_screenshots_enabled():
        return None
    return await save_runtime_screenshot(page, file_name, full_page=full_page)


def _iter_runtime_artifact_paths() -> list[str]:
    matched_paths: list[str] = []
    seen: set[str] = set()
    for directory in (runtime_base_dir, _runtime_screenshot_dir()):
        if not os.path.isdir(directory):
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                if not any(fnmatch.fnmatch(entry.name, pattern) for pattern in RUNTIME_SCREENSHOT_PATTERNS):
                    continue
                if entry.path in seen:
                    continue
                seen.add(entry.path)
                matched_paths.append(entry.path)
    return matched_paths


def cleanup_runtime_artifacts() -> int:
    removed = 0
    for path in _iter_runtime_artifact_paths():
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass

    runtime_screenshot_dir = _runtime_screenshot_dir()
    try:
        if os.path.isdir(runtime_screenshot_dir) and not os.listdir(runtime_screenshot_dir):
            os.rmdir(runtime_screenshot_dir)
    except OSError:
        pass

    clear_manual_resume_flag()
    return removed


def windows_notify(title: str, message: str):
    """Best-effort Windows foreground + tray notification fallback."""
    try:
        ctypes.windll.user32.MessageBeep(0x00000030)
    except Exception:
        pass
    try:
        # Fallback lightweight popup
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x00001040)
    except Exception:
        pass


def find_cdp_endpoint(port: int | None = None) -> str | None:
    """Find a single CDP endpoint. If port is given, only check that port."""
    if port:
        for host in ("127.0.0.1", "localhost"):
            try:
                with urllib.request.urlopen(
                    f"http://{host}:{port}/json/version", timeout=0.5
                ) as r:
                    body = r.read().decode("utf-8", "ignore")
                if "webSocketDebuggerUrl" in body:
                    return f"http://{host}:{port}"
            except Exception:
                pass
        return None

    # If no port provided, scan all listening ports
    out = subprocess.check_output(
        "netstat -ano", shell=True, text=True, encoding="utf-8", errors="ignore"
    )
    ports: set[int] = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 2 or ":" not in parts[1]:
            continue
        p = parts[1].rsplit(":", 1)[-1]
        if p.isdigit():
            pi = int(p)
            if 1000 <= pi <= 65535:
                ports.add(pi)

    for port in sorted(ports):
        for host in ("127.0.0.1", "localhost"):
            try:
                with urllib.request.urlopen(
                    f"http://{host}:{port}/json/version", timeout=0.25
                ) as r:
                    body = r.read().decode("utf-8", "ignore")
                if "webSocketDebuggerUrl" in body:
                    return f"http://{host}:{port}"
            except Exception:
                pass
    return None


def diagnose_cdp(port: int) -> CdpDiagnostic:
    """Perform a detailed diagnostic on a specific CDP port."""
    results: CdpDiagnostic = {
        "port": port,
        "attempts": [],
        "ok": False,
        "endpoint": None,
    }
    for host in ("127.0.0.1", "localhost"):
        url = f"http://{host}:{port}/json/version"
        attempt: CdpAttempt = {"host": host, "url": url, "ok": False, "error": None}
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                body = r.read().decode("utf-8", "ignore")
                if "webSocketDebuggerUrl" in body:
                    attempt["ok"] = True
                    results["ok"] = True
                    results["endpoint"] = f"http://{host}:{port}"
                    results["attempts"].append(attempt)
                    break
                else:
                    attempt["error"] = "Invalid response (missing webSocketDebuggerUrl)"
        except Exception as e:
            attempt["error"] = str(e)
        results["attempts"].append(attempt)
    return results


def find_all_cdp_endpoints() -> list[CdpEndpointInfo]:
    """
    Scan all listening ports and return a list of available CDP endpoints.
    Uses Playwright to connect briefly and read the shop name from
    the Purple Bird extension page DOM.
    """
    import json as _json

    out = subprocess.check_output(
        "netstat -ano", shell=True, text=True, encoding="utf-8", errors="ignore"
    )
    ports: set[int] = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 2 or ":" not in parts[1]:
            continue
        p = parts[1].rsplit(":", 1)[-1]
        if p.isdigit():
            pi = int(p)
            if 1000 <= pi <= 65535:
                ports.add(pi)

    # Phase 1: quick HTTP scan to find CDP-capable ports
    cdp_ports: list[CdpProbe] = []
    for port in sorted(ports):
        try:
            url = f"http://127.0.0.1:{port}"
            with urllib.request.urlopen(
                f"{url}/json/version", timeout=0.3
            ) as r:
                body = r.read().decode("utf-8", "ignore")
            if "webSocketDebuggerUrl" in body:
                info = _json.loads(body)
                cdp_ports.append({
                    "port": port,
                    "url": url,
                    "browser": info.get("Browser", ""),
                })
        except Exception:
            pass

    if not cdp_ports:
        return []

    # Phase 2: use Playwright to get shop names from extension pages
    import asyncio
    from playwright.async_api import async_playwright

    async def _read_shop_names(ports_info: list[CdpProbe]) -> list[CdpEndpointInfo]:
        results: list[CdpEndpointInfo] = []
        async with async_playwright() as pw:
            for pi in ports_info:
                shop_name = ""
                try:
                    browser = await pw.chromium.connect_over_cdp(pi["url"])
                    for ctx in browser.contexts:
                        for pg in ctx.pages:
                            u = pg.url or ""
                            if "chrome-extension" in u and "index.html" in u:
                                body_text = await pg.evaluate(
                                    "() => document.body ? document.body.innerText : ''"
                                )
                                # Shop name is typically on the 2nd line
                                lines = [l.strip() for l in body_text.split("\n") if l.strip()]
                                for line in lines:
                                    if line.startswith(("TH-", "VN-", "MY-", "PH-", "SG-", "ID-")):
                                        shop_name = line
                                        break
                                if not shop_name and len(lines) >= 2:
                                    shop_name = lines[1]
                                break
                        if shop_name:
                            break
                    await browser.close()
                except Exception:
                    pass

                label = f":{pi['port']}  {shop_name}" if shop_name else f":{pi['port']}  {pi['browser']}"
                results.append({
                    "port": pi["port"],
                    "url": pi["url"],
                    "browser": pi["browser"],
                    "shop_name": shop_name,
                    "label": label,
                })
        return results

    try:
        loop = asyncio.new_event_loop()
        results = loop.run_until_complete(_read_shop_names(cdp_ports))
        loop.close()
        return results
    except Exception:
        # Fallback: return without shop names
        return [{
            "port": pi["port"],
            "url": pi["url"],
            "browser": pi["browser"],
            "shop_name": "",
            "label": f":{pi['port']}  {pi['browser']}",
        } for pi in cdp_ports]


def parse_promo_name(name: str) -> PromoNameParts | None:
    text = (name or "").strip()
    m = TIMESTAMP_RE.search(text)
    if not m:
        return None

    y, mo, d, hm = m.groups()
    hh, mm = hm.split(":")
    hhi = int(hh)
    mmi = int(mm)
    # support 24:00 as next-day 00:00
    base_date = datetime(int(y), int(mo), int(d), 0, 0)
    if hhi == 24 and mmi == 0:
        dtv = base_date + timedelta(days=1)
    elif 0 <= hhi <= 23:
        dtv = datetime(int(y), int(mo), int(d), hhi, mmi)
    else:
        return None

    prefix_full = text[: m.start()]
    prefix_norm = prefix_full.rstrip(" -_")
    delim = prefix_full[len(prefix_norm) :] or "-"

    return {
        "raw": text,
        "dt": dtv,
        "prefix_full": prefix_full,
        "prefix_norm": prefix_norm,
        "delim": delim,
        "timestamp": m.group(0),
    }


def calc_window_for_name(name: str) -> tuple[datetime, datetime] | None:
    """
    Interpret promo name timestamp as END time.
    Minute rule (from current shop pattern):
    - 引流 / 盈利: start = end - 30 minutes
    - 微利 / 平本: start = end - 29 minutes (xx:31 -> xx+1:00)
    """
    p = parse_promo_name(name)
    if not p:
        return None

    end_dt = p["dt"]
    prefix = (p.get("prefix_norm") or "").strip()
    # overlap-avoidance rule (updated): every slot start +1 minute
    # i.e. 30-minute end-anchor window becomes [end-29min, end]
    # Example:
    # - 微利-2026-3.3-01:00 => 00:31 ~ 01:00
    # - 下个轮回 引流-2026-3.3-02:30 => 02:01 ~ 02:30
    _ = prefix  # keep parsed prefix for future category-specific rules
    start_dt = end_dt - timedelta(minutes=29)
    return start_dt, end_dt


def append_random_suffix(name: str, size: int = NAME_SUFFIX_LEN) -> str:
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(size))
    return f"{name}-{suffix}"


def strip_random_suffix(name: str) -> str:
    """
    Normalize generated names for matching templates.
    Example: 引流-2026-3.3-02:30-0ko0 -> 引流-2026-3.3-02:30
    """
    s = (name or "").strip()
    m = re.match(r"^(.*-\d{4}-\d{1,2}\.\d{1,2}-\d{2}:\d{2})(?:-[A-Za-z0-9]{2,10})$", s)
    if m:
        return m.group(1)
    return s


def build_next_from_rows(rows: list[UpcomingRow]) -> PromoPlan:
    parsed: list[ParsedPlanSource] = []
    for row in rows:
        p = parse_promo_name(row.get("name", ""))
        if p:
            obj: ParsedPlanSource = {
                "name": row["name"],
                "text": row["text"],
                "hasDuplicate": row["hasDuplicate"],
                "dt": p["dt"],
                "raw": p["raw"],
                "prefix_full": p["prefix_full"],
                "prefix_norm": p["prefix_norm"],
                "delim": p["delim"],
                "timestamp": p["timestamp"],
            }
            parsed.append(obj)

    if parsed:
        parsed.sort(key=lambda x: x["dt"])
        source = parsed[-1]

        prefix_sequence: list[str] = []
        for item in parsed:
            pr = (item.get("prefix_norm") or "").strip()
            if pr and pr not in prefix_sequence:
                prefix_sequence.append(pr)

        current_prefix = (source.get("prefix_norm") or "").strip()
        if current_prefix and current_prefix in prefix_sequence and len(prefix_sequence) >= 2:
            next_prefix = prefix_sequence[
                (prefix_sequence.index(current_prefix) + 1) % len(prefix_sequence)
            ]
        else:
            next_prefix = current_prefix or "Seller Flash Sale"

        next_end_base = source["dt"] + timedelta(minutes=30)
        # name timestamp is end-time anchor
        stamp = f"{next_end_base.year}-{next_end_base.month}.{next_end_base.day}-{next_end_base.strftime('%H:%M')}"
        new_name = f"{next_prefix}{source.get('delim', '-')}{stamp}"

        w = calc_window_for_name(new_name)
        if w:
            next_start, next_end = w
        else:
            next_start = next_end_base - timedelta(minutes=30)
            next_end = next_end_base

        return {
            "source_name": source.get("name", ""),
            "source_prefix": current_prefix or next_prefix,
            "new_name": new_name,
            "next_start": next_start,
            "next_end": next_end,
        }

    # fallback: no parseable timestamp, keep first name prefix and push from current time
    fallback_row = rows[0] if rows else {"name": "Seller Flash Sale"}
    src_name = fallback_row.get("name", "Seller Flash Sale")
    now = datetime.now().replace(second=0, microsecond=0)
    next_end_base = now + timedelta(minutes=30)
    stamp = f"{next_end_base.year}-{next_end_base.month}.{next_end_base.day}-{next_end_base.strftime('%H:%M')}"

    p = parse_promo_name(src_name)
    if p:
        pre = p.get("prefix_norm") or "Seller Flash Sale"
        delim = p.get("delim") or "-"
        new_name = f"{pre}{delim}{stamp}"
    else:
        new_name = f"Seller Flash Sale-{stamp}"

    w = calc_window_for_name(new_name)
    if w:
        next_start, next_end = w
    else:
        next_start = next_end_base - timedelta(minutes=30)
        next_end = next_end_base

    return {
        "source_name": src_name,
        "source_prefix": (p.get("prefix_norm") or "Seller Flash Sale") if p else "Seller Flash Sale",
        "new_name": new_name,
        "next_start": next_start,
        "next_end": next_end,
    }


def filter_usable_upcoming_rows(rows: list[RawUpcomingRow]) -> list[UpcomingRow]:
    usable: list[UpcomingRow] = []
    seen_names: set[str] = set()
    for row in rows:
        if not row.get("hasDuplicate"):
            continue

        raw_name = (row.get("name") or "").strip()
        normalized_name = normalize_promo_name(raw_name)
        parsed = parse_promo_name(normalized_name)
        if not parsed:
            continue

        dedupe_key = normalized_name.casefold()
        if dedupe_key in seen_names:
            continue
        seen_names.add(dedupe_key)

        item: UpcomingRow = {
            "name": normalized_name,
            "text": row.get("text", ""),
            "hasDuplicate": bool(row.get("hasDuplicate")),
            "dt": parsed["dt"],
            "prefix_norm": (parsed.get("prefix_norm") or "").strip(),
        }
        usable.append(item)

    usable.sort(key=lambda x: x["dt"])
    return usable


def _normalize_inline_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _looks_like_header_only_text(text: str) -> bool:
    normalized = _normalize_inline_text(text).lower()
    if not normalized:
        return True

    canonical = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", normalized)
    tokens = [token for token in canonical.split() if token]
    if not tokens:
        return True

    header_tokens = {
        "product", "products", "item", "items", "sku", "skus", "name", "image",
        "price", "stock", "status", "action", "actions", "discount", "discounts",
        "商品", "产品", "名称", "图片", "价格", "库存", "状态", "操作", "折扣",
    }
    return all(token in header_tokens for token in tokens)


def _extract_regex_hits(text: str, regex: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for match in regex.finditer(text or ""):
        hit = _normalize_inline_text(match.group(0))
        if hit and hit not in hits:
            hits.append(hit)
    return hits


def assess_create_page_product_state(snapshot: ProductSnapshot) -> ProductState:
    body_text = _normalize_inline_text(str(snapshot.get("bodyText") or ""))
    raw_row_texts = snapshot.get("rowTexts") or []
    raw_block_texts = snapshot.get("blockTexts") or []
    raw_empty_texts = snapshot.get("emptyTexts") or []
    raw_product_count_texts = snapshot.get("productCountTexts") or []
    raw_loading_texts = snapshot.get("loadingTexts") or []

    row_texts = []
    for text in raw_row_texts:
        normalized = _normalize_inline_text(str(text or ""))
        if not normalized:
            continue
        if EMPTY_PRODUCT_RE.search(normalized) or ZERO_PRODUCT_RE.search(normalized):
            continue
        if _looks_like_header_only_text(normalized):
            continue
        if normalized not in row_texts:
            row_texts.append(normalized)

    block_texts = []
    for text in raw_block_texts:
        normalized = _normalize_inline_text(str(text or ""))
        if not normalized:
            continue
        if EMPTY_PRODUCT_RE.search(normalized) or ZERO_PRODUCT_RE.search(normalized):
            continue
        if _looks_like_header_only_text(normalized):
            continue
        if normalized not in block_texts:
            block_texts.append(normalized)

    empty_texts = []
    for text in raw_empty_texts:
        normalized = _normalize_inline_text(str(text or ""))
        if not normalized:
            continue
        if normalized not in empty_texts:
            empty_texts.append(normalized)

    product_count_hits = []
    for text in raw_product_count_texts:
        normalized = _normalize_inline_text(str(text or ""))
        if not normalized:
            continue
        if POSITIVE_PRODUCT_COUNT_RE.search(normalized) and normalized not in product_count_hits:
            product_count_hits.append(normalized)

    if not product_count_hits:
        for hit in _extract_regex_hits(body_text, POSITIVE_PRODUCT_COUNT_RE):
            if hit not in product_count_hits:
                product_count_hits.append(hit)

    loading_texts = []
    for text in raw_loading_texts:
        normalized = _normalize_inline_text(str(text or ""))
        if not normalized:
            continue
        if normalized not in loading_texts:
            loading_texts.append(normalized)

    empty_hits = []
    for source_text in [body_text, *empty_texts]:
        for hit in _extract_regex_hits(source_text, EMPTY_PRODUCT_RE):
            if hit not in empty_hits:
                empty_hits.append(hit)

    zero_hits = []
    for source_text in [body_text, *empty_texts]:
        for hit in _extract_regex_hits(source_text, ZERO_PRODUCT_RE):
            if hit not in zero_hits:
                zero_hits.append(hit)

    row_count = max(0, len(row_texts))
    block_count = max(0, len(block_texts))
    visible_product_count = max(row_count, block_count)
    has_confirmed_products = visible_product_count > 0 or bool(product_count_hits)

    if not has_confirmed_products and empty_hits:
        return {
            "ok": False,
            "reason": "empty_products",
            "visible_product_count": visible_product_count,
            "empty_hits": empty_hits,
            "zero_hits": zero_hits,
            "row_samples": row_texts[:5],
            "block_samples": block_texts[:5],
        }

    if not has_confirmed_products and zero_hits:
        return {
            "ok": False,
            "reason": "zero_products",
            "visible_product_count": visible_product_count,
            "empty_hits": empty_hits,
            "zero_hits": zero_hits,
            "row_samples": row_texts[:5],
            "block_samples": block_texts[:5],
        }

    if not has_confirmed_products and loading_texts:
        return {
            "ok": False,
            "reason": "products_loading",
            "visible_product_count": visible_product_count,
            "empty_hits": empty_hits,
            "zero_hits": zero_hits,
            "row_samples": row_texts[:5],
            "block_samples": block_texts[:5],
        }

    if not has_confirmed_products:
        return {
            "ok": False,
            "reason": "products_unconfirmed",
            "visible_product_count": visible_product_count,
            "empty_hits": empty_hits,
            "zero_hits": zero_hits,
            "row_samples": row_texts[:5],
            "block_samples": block_texts[:5],
        }

    return {
        "ok": True,
        "reason": "products_confirmed",
        "visible_product_count": max(visible_product_count, len(product_count_hits)),
        "empty_hits": empty_hits,
        "zero_hits": zero_hits,
        "row_samples": row_texts[:5],
        "block_samples": block_texts[:5],
    }


async def inspect_create_page_product_state(page) -> ProductState:
    snapshot: ProductSnapshot = await page.evaluate(
        r"""() => {
            const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (!style || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
                    return false;
                }
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };
            const dedupe = (items) => {
                const seen = new Set();
                const result = [];
                for (const item of items) {
                    const normalized = normalize(item);
                    if (!normalized || seen.has(normalized)) continue;
                    seen.add(normalized);
                    result.push(normalized);
                }
                return result;
            };

            const rowTexts = [];
            const rowSelectors = ['tr', '[role="row"]', '.arco-table-row', '.theme-m4b-table-row', '[class*="table-row" i]'];
            const scannedRows = new Set();
            for (const selector of rowSelectors) {
                for (const row of document.querySelectorAll(selector)) {
                    if (scannedRows.has(row) || !isVisible(row)) continue;
                    scannedRows.add(row);
                    const cells = [...row.querySelectorAll('td,[role="cell"],.arco-table-td')].filter(isVisible);
                    const hasVisualProduct = !!row.querySelector('img, [style*="background-image"]');
                    const text = normalize(row.innerText || '');
                    if (cells.length < 2 && !hasVisualProduct) continue;
                    if (!text && !hasVisualProduct) continue;
                    rowTexts.push(text);
                }
            }

            const blockTexts = [];
            const blockSelectors = [
                '[class*="product" i]',
                '[class*="goods" i]',
                '[class*="sku" i]',
                '[data-testid*="product"]',
                '[data-testid*="sku"]',
                '[data-e2e*="product"]',
                '[data-e2e*="sku"]'
            ];
            const scannedBlocks = new Set();
            for (const selector of blockSelectors) {
                for (const block of document.querySelectorAll(selector)) {
                    if (scannedBlocks.has(block) || !isVisible(block)) continue;
                    scannedBlocks.add(block);
                    const text = normalize(block.innerText || '');
                    const hasVisualProduct = !!block.querySelector('img, [style*="background-image"]');
                    if (!text && !hasVisualProduct) continue;
                    blockTexts.push(text);
                }
            }

            const emptyTexts = [];
            const productCountTexts = [];
            const loadingTexts = [];
            const emptySelectors = [
                '.arco-empty',
                '.ant-empty',
                '[class*="empty" i]',
                '[class*="no-data" i]',
                '[class*="no-result" i]',
                '[data-testid*="empty"]'
            ];
            const scannedEmpties = new Set();
            for (const selector of emptySelectors) {
                for (const node of document.querySelectorAll(selector)) {
                    if (scannedEmpties.has(node) || !isVisible(node)) continue;
                    scannedEmpties.add(node);
                    emptyTexts.push(node.innerText || '');
                }
            }

            const loadingSelectors = [
                '[class*="loading" i]',
                '[class*="skeleton" i]',
                '[class*="spin" i]',
                '[aria-busy="true"]',
                '[data-testid*="loading"]'
            ];
            const scannedLoadingNodes = new Set();
            for (const selector of loadingSelectors) {
                for (const node of document.querySelectorAll(selector)) {
                    if (scannedLoadingNodes.has(node) || !isVisible(node)) continue;
                    scannedLoadingNodes.add(node);
                    const text = normalize(node.innerText || '');
                    loadingTexts.push(text || selector);
                }
            }

            const countSelectors = [
                '[class*="product" i]',
                '[class*="goods" i]',
                '[class*="sku" i]',
                '[data-testid*="product"]',
                '[data-e2e*="product"]',
                'span',
                'div'
            ];
            const countRegex = /\b([1-9]\d*)(?:\.0+)?\s*(?:products?|items?|skus?)\b|(?:products?|items?|skus?)\s*[:：]?\s*([1-9]\d*)(?:\.0+)?\b|\b([1-9]\d*)\s*(?:个)?(?:商品|产品)\b|(?:商品|产品)\s*[:：]?\s*([1-9]\d*)(?:\.0+)?\b/i;
            const scannedCountNodes = new Set();
            for (const selector of countSelectors) {
                for (const node of document.querySelectorAll(selector)) {
                    if (scannedCountNodes.has(node) || !isVisible(node)) continue;
                    scannedCountNodes.add(node);
                    const text = normalize(node.innerText || '');
                    if (!text || !countRegex.test(text)) continue;
                    productCountTexts.push(text);
                }
            }

            return {
                bodyText: document.body ? document.body.innerText || '' : '',
                rowTexts: dedupe(rowTexts).slice(0, 40),
                blockTexts: dedupe(blockTexts).slice(0, 40),
                emptyTexts: dedupe(emptyTexts).slice(0, 20),
                productCountTexts: dedupe(productCountTexts).slice(0, 20),
                loadingTexts: dedupe(loadingTexts).slice(0, 20),
                rowCount: dedupe(rowTexts).length,
                blockCount: dedupe(blockTexts).length,
            };
        }"""
    )
    return assess_create_page_product_state(snapshot)


async def wait_for_copied_products(page, attempts: int = 12, delay_ms: int = 1000) -> ProductState:
    last_state: ProductState = {
        "ok": False,
        "reason": "products_unconfirmed",
        "visible_product_count": 0,
        "empty_hits": [],
        "zero_hits": [],
        "row_samples": [],
        "block_samples": [],
    }
    consecutive_empty_reads = 0
    for idx in range(max(1, attempts)):
        last_state = await inspect_create_page_product_state(page)
        if last_state.get("ok"):
            return last_state
        if last_state.get("reason") == "products_loading":
            if idx < attempts - 1:
                await page.wait_for_timeout(delay_ms + 400)
            continue
        if last_state.get("reason") in {"empty_products", "zero_products"}:
            consecutive_empty_reads += 1
            if consecutive_empty_reads >= 2:
                return last_state
        else:
            consecutive_empty_reads = 0
        if idx < attempts - 1:
            extra_wait = 500 if last_state.get("reason") == "products_unconfirmed" else 0
            await page.wait_for_timeout(delay_ms + extra_wait)
    return last_state


def load_seed_schedule() -> list[SeedScheduleEntry]:
    gui_config_path = _gui_config_path()
    if not os.path.exists(gui_config_path):
        return []
    try:
        import json

        with open(gui_config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return []

    vals = cfg.get("seed_names", []) if isinstance(cfg, dict) else []
    result: list[SeedScheduleEntry] = []
    seen: set[str] = set()
    for raw in vals:
        normalized = normalize_promo_name(str(raw or ""))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        parsed = parse_promo_name(normalized)
        if not parsed:
            continue
        result.append({
            "name": normalized,
            "dt": parsed["dt"],
            "prefix_norm": (parsed.get("prefix_norm") or "").strip(),
        })

    result.sort(key=lambda x: x["dt"])
    return result


def compute_rolling_seed_names(anchor_dt: datetime, seed_count: int = 4) -> list[str]:
    base_schedule = load_seed_schedule()
    if not base_schedule:
        return []

    prefix_cycle = [item["prefix_norm"] for item in base_schedule if item["prefix_norm"]]
    deduped_cycle: list[str] = []
    for prefix in prefix_cycle:
        if prefix not in deduped_cycle:
            deduped_cycle.append(prefix)
    if not deduped_cycle:
        deduped_cycle = ["Seller Flash Sale"]

    start_index = 0
    base_anchor_dt = base_schedule[0]["dt"]
    delta_minutes = int((anchor_dt - base_anchor_dt).total_seconds() // 60)
    slot_offset = max(0, delta_minutes // 30)
    start_index = slot_offset % len(deduped_cycle)

    cycle_length = len(deduped_cycle)
    results: list[str] = []
    for index in range(seed_count):
        slot_dt = anchor_dt + timedelta(minutes=30 * index)
        prefix = deduped_cycle[(start_index + index) % cycle_length]
        stamp = f"{slot_dt.year}-{slot_dt.month}.{slot_dt.day}-{slot_dt.strftime('%H:%M')}"
        results.append(f"{prefix}-{stamp}")
    return results


def persist_seed_names(seed_names: list[str]):
    gui_config_path = _gui_config_path()
    config: dict[str, object] = {}
    if os.path.exists(gui_config_path):
        try:
            import json

            with open(gui_config_path, "r", encoding="utf-8") as file_obj:
                raw = json.load(file_obj)
                if isinstance(raw, dict):
                    config = raw
        except Exception:
            config = {}

    config["seed_names"] = seed_names
    try:
        import json

        with open(gui_config_path, "w", encoding="utf-8") as file_obj:
            json.dump(config, file_obj, ensure_ascii=False, indent=2)
    except Exception:
        return

    if seed_names_update_callback is not None:
        try:
            seed_names_update_callback(seed_names)
        except Exception:
            pass


def choose_from_seed_schedule(upcoming_rows: list[UpcomingRow]) -> PlanDecision:
    schedule = load_seed_schedule()

    upcoming_dts = []
    for r in upcoming_rows:
        parsed = parse_promo_name(normalize_promo_name(r.get("name") or ""))
        if parsed:
            upcoming_dts.append(parsed["dt"])

    if not upcoming_dts:
        return {"status": "blocked", "reason": "no_usable_upcoming"}

    current_max_dt = max(upcoming_dts)
    anchor_dt = current_max_dt

    last_dt = load_checkpoint_dt()
    if last_dt and last_dt > anchor_dt:
        anchor_dt = last_dt

    source_row = next((row for row in schedule if row["dt"] == anchor_dt), None) if schedule else None

    def _prefix_of(name: str) -> str | None:
        p = parse_promo_name(normalize_promo_name(name))
        if not p:
            return None
        return (p.get("prefix_norm") or "").strip() or None

    def _pick_source_for_target(target_name: str, fallback_name: str | None) -> str:
        target_prefix = _prefix_of(target_name)
        if not target_prefix:
            return fallback_name or (source_row["name"] if source_row else f"ANCHOR@{anchor_dt}")

        # Parse currently visible upcoming rows
        up_items: list[ParsedUpcomingEntry] = []
        for r in upcoming_rows:
            nm = normalize_promo_name(r.get("name") or "")
            p = parse_promo_name(nm)
            if p:
                up_items.append({"name": nm, "dt": p["dt"], "prefix": (p.get("prefix_norm") or "").strip()})

        # 1) prefer user-provided seed template with same prefix (and visible in upcoming)
        up_names = {x["name"] for x in up_items}
        for seed in schedule:
            sn = normalize_promo_name(seed["name"])
            sp = _prefix_of(sn)
            if sp == target_prefix and sn in up_names:
                return sn

        # 2) otherwise choose latest visible upcoming row with same prefix
        same_prefix = [x for x in up_items if x["prefix"] == target_prefix]
        if same_prefix:
            same_prefix.sort(key=lambda x: x["dt"])
            return same_prefix[-1]["name"]

        # 3) fallback to anchor-based source
        return fallback_name or (source_row["name"] if source_row else f"ANCHOR@{anchor_dt}")

    # If all visible upcoming rows are the same prefix (e.g. only 引流),
    # treat as collapsed/filtered view and stop to avoid wrong template chaining.
    visible_prefixes = set()
    for r in upcoming_rows:
        p = parse_promo_name(normalize_promo_name(r.get("name") or ""))
        if p:
            pr = (p.get("prefix_norm") or "").strip()
            if pr:
                visible_prefixes.add(pr)
    if len(visible_prefixes) <= 1:
        print(f"WARNING: upcoming rows collapsed to single prefix: {list(visible_prefixes)}. Stop to prevent wrong template duplication.")
        return {"status": "blocked", "reason": "collapsed_upcoming"}

    # Prefer GUI-provided reference schedule when available
    if schedule:
        for row in schedule:
            if row["dt"] > anchor_dt:
                w = calc_window_for_name(row["name"])
                if w:
                    start_dt, end_dt = w
                else:
                    start_dt = row["dt"] - timedelta(minutes=30)
                    end_dt = row["dt"]

                fallback_source = source_row["name"] if source_row else f"ANCHOR@{anchor_dt}"
                picked_source = _pick_source_for_target(row["name"], fallback_source)
                picked_prefix = _prefix_of(row["name"]) or ""

                return {
                    "status": "planned",
                    "plan": {
                        "source_name": picked_source,
                        "source_prefix": picked_prefix,
                        "new_name": row["name"],
                        "next_start": start_dt,
                        "next_end": end_dt,
                    },
                }
        # GUI reference schedule exists but exhausted at anchor
        return {"status": "exhausted", "reason": "seed_schedule_exhausted"}

    # No usable GUI reference schedule: derive next by current upcoming and still keep prefix/template mapping
    parsed_up: list[ParsedUpcomingEntry] = []
    for r in upcoming_rows:
        nm = normalize_promo_name(r.get("name") or "")
        p = parse_promo_name(nm)
        if p:
            parsed_up.append({
                "name": nm,
                "dt": p["dt"],
                "prefix": (p.get("prefix_norm") or "").strip(),
            })

    if not parsed_up:
        return {"status": "blocked", "reason": "no_parseable_upcoming"}

    parsed_up.sort(key=lambda x: x["dt"])
    latest = parsed_up[-1]

    # infer prefix cycle from visible upcoming, then rotate
    cycle = []
    for item in parsed_up:
        pr = item["prefix"]
        if pr and pr not in cycle:
            cycle.append(pr)
    current_prefix = latest["prefix"]
    if current_prefix and current_prefix in cycle and len(cycle) >= 2:
        next_prefix = cycle[(cycle.index(current_prefix) + 1) % len(cycle)]
    else:
        next_prefix = current_prefix or "Seller Flash Sale"

    next_end = latest["dt"] + timedelta(minutes=30)
    stamp = f"{next_end.year}-{next_end.month}.{next_end.day}-{next_end.strftime('%H:%M')}"
    new_name = f"{next_prefix}-{stamp}"
    w = calc_window_for_name(new_name)
    if w:
        next_start, next_end2 = w
    else:
        next_start, next_end2 = next_end - timedelta(minutes=29), next_end

    picked_source = _pick_source_for_target(new_name, latest["name"])
    picked_prefix = _prefix_of(new_name) or next_prefix

    return {
        "status": "auto_computed",
        "plan": {
            "source_name": picked_source,
            "source_prefix": picked_prefix,
            "new_name": new_name,
            "next_start": next_start,
            "next_end": next_end2,
        },
    }


def load_checkpoint_dt() -> datetime | None:
    if not os.path.exists(checkpoint_file_path):
        return None
    try:
        raw = open(checkpoint_file_path, "r", encoding="utf-8").read().strip()
    except Exception:
        return None
    if not raw:
        return None

    # 1) direct datetime format
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except Exception:
        pass

    # 2) promo-name-like format containing timestamp
    p = parse_promo_name(raw)
    if p:
        return p["dt"]
    return None


def save_checkpoint_dt(dtv: datetime):
    try:
        with open(checkpoint_file_path, "w", encoding="utf-8") as f:
            f.write(dtv.strftime("%Y-%m-%d %H:%M"))
    except Exception:
        pass


def clear_manual_resume_flag():
    try:
        if os.path.exists(manual_resume_flag_path):
            os.remove(manual_resume_flag_path)
    except Exception:
        pass


async def wait_manual_continue(reason: str, timeout_seconds: int = manual_wait_max_seconds) -> bool:
    """
    Pause automation and wait for manual resume.
    Resume conditions:
    1) Operator creates file .manual_resume.flag, OR
    2) Press Ctrl+C to abort this run.
    """
    clear_manual_resume_flag()
    print(f"MANUAL PAUSE: {reason}")
    print(f"Create file to continue: {manual_resume_flag_path}")
    print("Waiting for manual continue...")

    waited = 0
    step = 2
    while waited < timeout_seconds:
        if os.path.exists(manual_resume_flag_path):
            print("Manual continue detected.")
            clear_manual_resume_flag()
            return True
        await asyncio.sleep(step)
        waited += step

    print("Manual continue timeout reached.")
    return False


async def force_fill(locator, value: str) -> bool:
    try:
        await locator.click(timeout=6000)
        await locator.fill(value, timeout=6000)
        return True
    except Exception:
        try:
            handle = await locator.element_handle()
            if handle:
                await handle.evaluate(
                    """(el, v) => {
                        el.focus();
                        el.value = v;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    value,
                )
                return True
        except Exception:
            pass
    return False


async def fill_with_confirm(locator, value: str) -> bool:
    """Fill input, then press Enter to confirm picker input."""
    ok = await force_fill(locator, value)
    if not ok:
        return False
    try:
        await locator.press("Enter", timeout=2000)
    except Exception:
        try:
            await locator.page.keyboard.press("Enter")
        except Exception:
            pass
    return True
async def fill_end_time_fast(locator, value: str) -> bool:
    """Optimized helper for the 4th field (End Time) to avoid long hangs."""
    print("Filling end time (4th field): fast path...")
    try:
        # Quick attempt with shorter timeout to avoid long blind waits
        await locator.click(timeout=1500)
        await locator.fill(value, timeout=1500)
        await locator.press("Enter", timeout=1000)
        return True
    except Exception as e:
        print(f"Filling end time fast path failed: {e}. Triggering fallback...")
    # Fallback to existing robust fill_with_confirm logic if fast path fails
    return await fill_with_confirm(locator, value)


async def fill_name_verified(locator, value: str) -> bool:
    """Fill name input and verify the value was correctly set with retries."""
    def _normalize(s: str) -> str:
        return " ".join((s or "").split()).strip()

    target = _normalize(value)
    for attempt in range(1, 4):
        print(f"Filling name (attempt {attempt}/3): {value}")
        ok = await fill_with_confirm(locator, value)
        if not ok:
            print(f"  Attempt {attempt} fill_with_confirm failed.")
            continue

        # Read back and verify
        try:
            actual = await locator.input_value()
            actual_norm = _normalize(actual)
            if actual_norm == target:
                if attempt > 1:
                    print(f"  Success on attempt {attempt}.")
                return True
            else:
                print(f"  Mismatch on attempt {attempt}: expected '{target}', got '{actual_norm}'")
        except Exception as e:
            print(f"  Attempt {attempt} read-back failed: {e}")

        await asyncio.sleep(0.5)  # Short wait before retry

    print(f"ERROR: Failed to verify name input after 3 attempts. Target: '{target}'")
    return False





async def extract_upcoming_rows(page) -> list[RawUpcomingRow]:
    payload = {
        "upcomingPatterns": UPCOMING_PATTERNS,
        "duplicatePatterns": DUPLICATE_PATTERNS,
    }
    return await page.evaluate(
        r"""(cfg) => {
            const upcomingPatterns = cfg.upcomingPatterns || [];
            const duplicatePatterns = cfg.duplicatePatterns || [];
            const statusRe = new RegExp(upcomingPatterns.join('|'), 'i');
            const dupRe = new RegExp(duplicatePatterns.join('|'), 'i');
            const selectors = ['tr', '[role="row"]', '.arco-table-row', '.theme-m4b-table-row'];

            const unique = new Set();
            const rows = [];

            for (const sel of selectors) {
                for (const r of document.querySelectorAll(sel)) {
                    if (unique.has(r)) continue;
                    unique.add(r);

                    const txt = (r.innerText || '').replace(/\s+/g, ' ').trim();
                    if (!txt) continue;
                    if (!statusRe.test(txt)) continue;

                    const cells = [...r.querySelectorAll('td,[role="cell"],.arco-table-td')]
                        .map(c => (c.innerText || '').replace(/\s+/g, ' ').trim())
                        .filter(Boolean);
                    const name = cells[0] || txt.split(' ')[0] || '';

                    const hasDuplicate = [...r.querySelectorAll('button,[role="button"],a')]
                        .some(el => dupRe.test((el.innerText || '').trim()));

                    rows.push({ name, text: txt, hasDuplicate });
                }
            }
            return rows;
        }""",
        payload,
    )


async def click_upcoming_filter_if_exists(page):
    await page.evaluate(
        r"""(patterns) => {
            const re = new RegExp('^(' + patterns.join('|') + ')$', 'i');
            const nodes = [...document.querySelectorAll('button,a,span,div,[role="tab"],[role="button"]')];
            for (const n of nodes) {
                const t = (n.innerText || '').trim();
                if (re.test(t)) {
                    n.click();
                    return true;
                }
            }
            return false;
        }""",
        UPCOMING_PATTERNS,
    )


async def click_duplicate_for_source(page, source_name: str) -> dict[str, object]:
    payload = {
        "upcomingPatterns": UPCOMING_PATTERNS,
        "duplicatePatterns": DUPLICATE_PATTERNS,
        "sourceName": source_name,
    }
    return await page.evaluate(
        r"""(cfg) => {
            const upcomingPatterns = cfg.upcomingPatterns || [];
            const duplicatePatterns = cfg.duplicatePatterns || [];
            const sourceName = cfg.sourceName || '';
            const statusRe = new RegExp(upcomingPatterns.join('|'), 'i');
            const dupRe = new RegExp(duplicatePatterns.join('|'), 'i');
            const rows = [...document.querySelectorAll('tr,[role="row"],.arco-table-row,.theme-m4b-table-row')]
                .filter(r => statusRe.test((r.innerText || '').trim()));

            const extractName = (row) => {
                const cells = [...row.querySelectorAll('td,[role="cell"],.arco-table-td')]
                    .map(c => (c.innerText || '').replace(/\s+/g, ' ').trim())
                    .filter(Boolean);
                if (cells.length > 0) return cells[0];
                const txt = (row.innerText || '').replace(/\s+/g, ' ').trim();
                return txt.split(' ')[0] || '';
            };

            const parseMeta = (name) => {
                const m = name.match(/^(.*?)-(\d{4}-\d{1,2}\.\d{1,2}-\d{2}:\d{2})(?:-.+)?$/);
                if (!m) return null;
                return {
                    prefix: (m[1] || '').trim(),
                    stamp: (m[2] || '').trim(),
                };
            };

            const srcMeta = parseMeta(sourceName || '');

            let target = null;
            if (sourceName) {
                target = rows.find(r => ((r.innerText || '').replace(/\s+/g, ' ').trim()).includes(sourceName));
            }

            // fallback match by prefix + timestamp (ignores random suffix)
            if (!target && srcMeta) {
                target = rows.find(r => {
                    const n = extractName(r);
                    const meta = parseMeta(n);
                    return !!meta && meta.prefix === srcMeta.prefix && meta.stamp === srcMeta.stamp;
                });
            }

            if (!target) return { ok: false, reason: 'row_not_found' };

            const btn = [...target.querySelectorAll('button,[role="button"],a')]
                .find(el => dupRe.test((el.innerText || '').trim()));
            if (!btn) return { ok: false, reason: 'duplicate_not_found' };

            btn.scrollIntoView({ block: 'center', inline: 'center' });
            btn.click();
            return {
                ok: true,
                buttonText: (btn.innerText || '').trim(),
                pickedName: extractName(target),
            };
        }""",
        payload,
    )


async def click_duplicate_for_prefix(page, target_prefix: str) -> dict[str, object]:
    """
    Prefer duplicating row whose promotion name prefix matches target_prefix.
    This avoids always falling back to the latest row (e.g., 平本-...02:00).
    """
    payload = {
        "upcomingPatterns": UPCOMING_PATTERNS,
        "duplicatePatterns": DUPLICATE_PATTERNS,
        "targetPrefix": target_prefix,
    }
    return await page.evaluate(
        r"""(cfg) => {
            const upcomingPatterns = cfg.upcomingPatterns || [];
            const duplicatePatterns = cfg.duplicatePatterns || [];
            const targetPrefix = (cfg.targetPrefix || '').trim();
            const statusRe = new RegExp(upcomingPatterns.join('|'), 'i');
            const dupRe = new RegExp(duplicatePatterns.join('|'), 'i');

            const rows = [...document.querySelectorAll('tr,[role="row"],.arco-table-row,.theme-m4b-table-row')]
                .filter(r => statusRe.test((r.innerText || '').trim()));

            const extractName = (row) => {
                const cells = [...row.querySelectorAll('td,[role="cell"],.arco-table-td')]
                    .map(c => (c.innerText || '').replace(/\s+/g, ' ').trim())
                    .filter(Boolean);
                if (cells.length > 0) return cells[0];
                const txt = (row.innerText || '').replace(/\s+/g, ' ').trim();
                return txt.split(' ')[0] || '';
            };

            const getTimeValue = (name) => {
                const m = name.match(/(\d{4})-(\d{1,2})\.(\d{1,2})-(\d{2}):(\d{2})/);
                if (!m) return -1;
                const y = Number(m[1]);
                const mo = Number(m[2]);
                const d = Number(m[3]);
                const hh = Number(m[4]);
                const mm = Number(m[5]);
                // coarse sortable scalar
                return ((((y * 100 + mo) * 100 + d) * 100 + hh) * 100 + mm);
            };

            const candidates = [];
            for (const r of rows) {
                const name = extractName(r);
                const prefix = (name.split('-')[0] || '').trim();
                const btn = [...r.querySelectorAll('button,[role="button"],a')]
                    .find(el => dupRe.test((el.innerText || '').trim()));
                if (!btn) continue;
                candidates.push({ row: r, btn, name, prefix, timeValue: getTimeValue(name) });
            }

            let target = null;
            if (targetPrefix) {
                const same = candidates.filter(x => x.prefix === targetPrefix);
                if (same.length > 0) {
                    same.sort((a, b) => b.timeValue - a.timeValue);
                    target = same[0];
                }
            }
            // Strict mode: when prefix requested, do not fallback to arbitrary row.
            if (!target && targetPrefix) {
                return {
                    ok: false,
                    reason: 'prefix_not_found',
                    targetPrefix,
                    candidatePrefixes: [...new Set(candidates.map(c => c.prefix))],
                };
            }
            if (!target) target = candidates[0] || null;

            if (!target) return { ok: false, reason: 'duplicate_not_found' };

            target.btn.scrollIntoView({ block: 'center', inline: 'center' });
            target.btn.click();
            return {
                ok: true,
                buttonText: (target.btn.innerText || '').trim(),
                pickedName: target.name,
                pickedPrefix: target.prefix,
            };
        }""",
        payload,
    )


async def detect_slider_captcha(page) -> bool:
    return await page.evaluate(
        r"""() => {
            const selectors = [
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
                'iframe[src*="captcha" i]',
                'iframe[src*="verify" i]',
                'iframe[src*="geetest" i]'
            ];

            const isVisible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };

            let visibleNodeHit = false;
            for (const sel of selectors) {
                const nodes = document.querySelectorAll(sel);
                for (const n of nodes) {
                    if (isVisible(n)) {
                        visibleNodeHit = true;
                        break;
                    }
                }
                if (visibleNodeHit) break;
            }

            // avoid false positives from static page text
            return visibleNodeHit;
        }"""
    )


async def wait_captcha_resolved(page, timeout_seconds: int = CAPTCHA_WAIT_SECONDS) -> bool:
    checks = max(1, timeout_seconds // 2)
    for _ in range(checks):
        has_captcha = await detect_slider_captcha(page)
        if not has_captcha:
            return True
        await page.wait_for_timeout(2000)
    return False


async def detect_slider_captcha_anywhere(context) -> bool:
    for pg in context.pages:
        try:
            if await detect_slider_captcha(pg):
                return True
        except Exception:
            continue
    return False


async def wait_captcha_resolved_anywhere(context, timeout_seconds: int = CAPTCHA_WAIT_SECONDS) -> bool:
    checks = max(1, timeout_seconds // 2)
    for _ in range(checks):
        found = False
        for pg in context.pages:
            try:
                if await detect_slider_captcha(pg):
                    found = True
                    break
            except Exception:
                continue
        if not found:
            return True
        await asyncio.sleep(2)
    return False


async def ensure_management_page(page):
    low = (page.url or "").lower()
    if "/promotion/marketing-tools/management" not in low:
        await page.goto(MANAGEMENT_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
    else:
        await page.wait_for_timeout(1500)


async def detect_got_it_modal(page) -> bool:
    return await page.evaluate(
        r"""() => {
            const isVisible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };

            const roots = [
                ...document.querySelectorAll('[role="dialog"], .arco-modal, .ant-modal, [class*="modal" i], [class*="dialog" i]')
            ].filter(isVisible);

            const textRe = /promotion created|promotion editing guidelines|manage your promotions|创建成功|活动编辑指南|管理促销/i;
            const btnRe = /got\s*it|ok|知道了|确定|关闭/i;

            for (const root of roots) {
                const txt = (root.innerText || '').trim();
                if (!textRe.test(txt) && !btnRe.test(txt)) continue;
                const btn = [...root.querySelectorAll('button,[role="button"],a')]
                    .find(el => isVisible(el) && btnRe.test((el.innerText || '').trim()));
                if (btn) return true;
            }

            return false;
        }"""
    )


async def dismiss_post_submit_guidelines(page) -> bool:
    """
    Dismiss post-submit success/guideline modal if present.
    Handles variants like:
    - Promotion created
    - Promotion editing guidelines
    - Manage your promotions
    with buttons: Got it / OK / 知道了 / 确定 / 关闭
    """
    async def _click_first_visible(loc) -> bool:
        try:
            cnt = await loc.count()
        except Exception:
            cnt = 0
        for i in range(min(cnt, 8)):
            item = loc.nth(i)
            try:
                if await item.is_visible():
                    await item.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    text_re = re.compile(
        r"promotion created|promotion editing guidelines|manage your promotions|创建成功|活动编辑指南|管理促销",
        re.I,
    )
    btn_re = re.compile(r"got\s*it|ok|知道了|确定|关闭", re.I)

    # poll for delayed render, but click ONLY inside dialog roots (no page-wide random clicking)
    for _ in range(12):
        if not await detect_got_it_modal(page):
            await page.wait_for_timeout(700)
            continue

        dialog_root = page.locator("[role='dialog'], .arco-modal, .ant-modal, [class*='modal'], [class*='dialog']")
        root_count = await dialog_root.count()

        # 1) within visible dialog roots with expected text
        for i in range(min(root_count, 6)):
            root = dialog_root.nth(i)
            try:
                if not await root.is_visible():
                    continue
                root_text = await root.inner_text()
                if not text_re.search(root_text) and not btn_re.search(root_text):
                    continue

                btn_in_root = root.get_by_role("button", name=btn_re)
                if await _click_first_visible(btn_in_root):
                    await page.wait_for_timeout(1000)
                    return True

                btn_in_root_css = root.locator(
                    "button:has-text('Got it'), button:has-text('got it'), button:has-text('Got It'), "
                    "button:has-text('OK'), button:has-text('Ok'), button:has-text('知道了'), "
                    "button:has-text('确定'), button:has-text('关闭'), "
                    "[role='button']:has-text('Got it'), [role='button']:has-text('got it'), [role='button']:has-text('Got It'), "
                    "[role='button']:has-text('OK'), [role='button']:has-text('Ok'), [role='button']:has-text('知道了'), "
                    "[role='button']:has-text('确定'), [role='button']:has-text('关闭')"
                )
                if await _click_first_visible(btn_in_root_css):
                    await page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue

        # 2) strict global fallback: role button only (no div/span clicks)
        role_btn = page.get_by_role("button", name=btn_re)
        if await _click_first_visible(role_btn):
            await page.wait_for_timeout(900)
            return True

        # 3) last resort keyboard if modal exists but no clickable captured
        try:
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(700)
            return True
        except Exception:
            pass

        await page.wait_for_timeout(700)

    return False


async def dismiss_post_submit_guidelines_anywhere(context) -> bool:
    clicked = False
    # create pages often on top; scan all pages for safety
    for pg in list(context.pages):
        try:
            done = await dismiss_post_submit_guidelines(pg)
            if done:
                clicked = True
        except Exception:
            continue
    return clicked


async def pick_management_page(context):
    pages = context.pages
    print("Page count:", len(pages))

    best = None
    best_score = -1
    for i, pg in enumerate(pages):
        url = pg.url or ""
        try:
            title = await pg.title()
        except Exception:
            title = ""
        print(f"[{i}] {title} | {url}")

        score = 0
        low = url.lower()
        if "/promotion/marketing-tools/management" in low:
            score += 10
        if "promotion_type=4" in low:
            score += 5
        if "tab=3" in low:
            score += 3
        if "seller-" in low:
            score += 2

        if score > best_score:
            best_score = score
            best = pg

    if best is None:
        best = pages[0] if pages else await context.new_page()

    await best.bring_to_front()
    return best


async def try_auto_solve_captcha(page, context=None) -> bool:
    """
    Attempt automatic captcha solving via OpenCV.
    Tries on the given page first, then scans all context pages.
    Returns True if captcha was solved.
    """
    if not auto_solve_captcha_enabled:
        print("Automatic captcha solving is disabled by runtime setting.")
        return False

    if not has_captcha_solver or solve_slider_captcha_with_result is None:
        print("captcha_solver module not available, skipping auto-solve.")
        return False

    # Try on primary page first
    try:
        outcome = await solve_slider_captcha_with_result(page)
        print(f"Auto-solve provisional outcome on primary page: {getattr(outcome, 'reason', 'unknown')}")
        if getattr(outcome, 'solved', False):
            return True
    except Exception as e:
        print(f"Auto-solve error on primary page: {e}")

    # Try on other pages in context
    if context:
        for pg in context.pages:
            if pg == page:
                continue
            try:
                from flashsale_runner import detect_slider_captcha
                if await detect_slider_captcha(pg):
                    outcome = await solve_slider_captcha_with_result(pg)
                    print(f"Auto-solve provisional outcome on context page: {getattr(outcome, 'reason', 'unknown')}")
                    if getattr(outcome, 'solved', False):
                        return True
            except Exception:
                continue

    return False


async def main():
    endpoint = find_cdp_endpoint(cdp_port)
    print("CDP endpoint:", endpoint)
    if not endpoint:
        print("ERROR: 未找到可连接CDP端口")
        return

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        page = await pick_management_page(context)
        await page.wait_for_load_state("domcontentloaded")

        created_count = 0
        for round_idx in range(batch_max_rounds):
            print(f"\n=== ROUND {round_idx + 1} ===")
            await ensure_management_page(page)

            upcoming_rows = []
            upcoming_ready = False
            for _ in range(8):
                raw_upcoming_rows = await extract_upcoming_rows(page)
                upcoming_rows = filter_usable_upcoming_rows(raw_upcoming_rows)
                if upcoming_rows:
                    parsed_prefixes = []
                    for rr in upcoming_rows:
                        pp = parse_promo_name((rr.get("name") or "").strip())
                        if pp:
                            pfx = (pp.get("prefix_norm") or "").strip()
                            if pfx:
                                parsed_prefixes.append(pfx)
                    # Require at least 4 parsed rows and at least 2 distinct prefixes
                    # to avoid acting on transient/collapsed table states.
                    if len(parsed_prefixes) >= 4 and len(set(parsed_prefixes)) >= 2:
                        upcoming_ready = True
                        break
                await click_upcoming_filter_if_exists(page)
                await page.wait_for_timeout(1200)

            print("Detected upcoming rows:", len(upcoming_rows))
            print("Upcoming names:", [r.get("name") for r in upcoming_rows])

            if not upcoming_rows:
                shot_path = await save_runtime_screenshot(page, "current_page_no_usable_upcoming.png", full_page=True)
                print(f"ERROR: no usable upcoming rows detected. screenshot={shot_path or 'save-failed'}")
                break

            if not upcoming_ready:
                shot_path = await save_runtime_screenshot(page, "current_page_no_usable_upcoming.png", full_page=True)
                print(
                    f"ERROR: upcoming rows incomplete after retries; refusing to plan from partial table. "
                    f"screenshot={shot_path or 'save-failed'}"
                )
                break

            plan_decision = choose_from_seed_schedule(upcoming_rows)
            plan = plan_decision.get("plan")
            plan_status = plan_decision.get("status")
            if plan_status == "blocked":
                print(f"ERROR: planning blocked because {plan_decision.get('reason')}")
                break
            if plan is None and plan_status in {"exhausted", "auto_computed"}:
                # GUI 参考不存在或参考轮次已用完，自动从 Upcoming 行推算下一个
                print("Auto-computing next slot from current Upcoming rows...")
                plan = build_next_from_rows(upcoming_rows)
            if plan is None:
                print("No further schedule row to create. Batch complete.")
                break

            submit_name = append_random_suffix(plan["new_name"])
            print("Source name:", plan["source_name"])
            print("Base name:", plan["new_name"])
            print("Submit name:", submit_name)
            print("Window:", plan["next_start"], "->", plan["next_end"])

            target_prefix = (plan.get("source_prefix") or "").strip()
            if not target_prefix:
                _p_new = parse_promo_name(strip_random_suffix(plan["new_name"]))
                target_prefix = (_p_new.get("prefix_norm", "") if _p_new else "")
            click_result = await click_duplicate_for_prefix(page, target_prefix)

            # fallback to exact source name (strict matching) when prefix-pick fails
            if not click_result.get("ok"):
                click_result = await click_duplicate_for_source(page, plan["source_name"])

            print("Click duplicate result:", click_result)
            if not click_result.get("ok"):
                shot_path = await save_runtime_screenshot(page, "duplicate_click_failed.png", full_page=True)
                print(f"ERROR: duplicate click failed. screenshot={shot_path or 'save-failed'}")
                break

            # Wait for create page/tab
            create_page = None
            for _ in range(12):
                for pg in context.pages:
                    u = (pg.url or "").lower()
                    if "/promotion/marketing-tools/flash-sale/create" in u:
                        create_page = pg
                if create_page is not None:
                    break
                await page.wait_for_timeout(1000)

            if create_page is None and "/promotion/marketing-tools/flash-sale/create" in (page.url or "").lower():
                create_page = page

            if create_page is None:
                shot_path = await save_runtime_screenshot(page, "create_page_not_found.png", full_page=True)
                print(f"ERROR: create page not found. screenshot={shot_path or 'save-failed'}")
                break

            await create_page.bring_to_front()
            await create_page.wait_for_load_state("domcontentloaded")
            await create_page.wait_for_timeout(2200)

            product_state = await wait_for_copied_products(create_page)
            print("Copied product state:", product_state)
            if not product_state.get("ok"):
                shot_path = await save_runtime_screenshot(
                    create_page,
                    "duplicate_empty_products_blocked.png",
                    full_page=True,
                )
                print(
                    f"ERROR: duplicate create page has no confirmed copied products ({product_state.get('reason')}). "
                    f"screenshot={shot_path or 'save-failed'}"
                )
                break

            ok_name = await fill_name_verified(
                create_page.locator(
                    "#name_input, input[placeholder*='Promotion name'], input[placeholder*='名称']"
                ).first,
                submit_name,
            )

            start_date = plan["next_start"].strftime("%m/%d/%Y")
            start_time = plan["next_start"].strftime("%I:%M %p").lstrip("0")
            end_date = plan["next_end"].strftime("%m/%d/%Y")
            end_time = plan["next_end"].strftime("%I:%M %p").lstrip("0")

            ok_start_date = await fill_with_confirm(
                create_page.locator("input[placeholder='Start time'], input[placeholder*='Start']").first,
                start_date,
            )

            time_inputs = create_page.locator("input[placeholder='Select time'], input[placeholder*='Select time']")
            ok_start_time = await fill_with_confirm(time_inputs.nth(0), start_time)

            ok_end_date = await fill_with_confirm(
                create_page.locator("input[placeholder='End time'], input[placeholder*='End']").first,
                end_date,
            )
            ok_end_time = await fill_end_time_fast(time_inputs.nth(1), end_time)

            # verify actual values after confirmation
            actual_start_date = await create_page.locator(
                "input[placeholder='Start time'], input[placeholder*='Start']"
            ).first.input_value()
            actual_end_date = await create_page.locator(
                "input[placeholder='End time'], input[placeholder*='End']"
            ).first.input_value()
            actual_start_time = await time_inputs.nth(0).input_value()
            actual_end_time = await time_inputs.nth(1).input_value()

            await create_page.keyboard.press("Tab")
            await create_page.wait_for_timeout(800)

            # Pre-save hard guard for activity name
            name_presave_guard = True
            name_input_locator = create_page.locator(
                "#name_input, input[placeholder*='Promotion name'], input[placeholder*='名称']"
            ).first
            
            def _normalize_local(s: str) -> str:
                return " ".join((s or "").split()).strip()

            current_name_raw = await name_input_locator.input_value()
            if _normalize_local(current_name_raw) != _normalize_local(submit_name):
                print(f"PRE-SAVE GUARD: Name mismatch detected! Expected '{submit_name}', got '{current_name_raw}'. Attempting rewrite...")
                await fill_name_verified(name_input_locator, submit_name)
                # Second check
                current_name_after = await name_input_locator.input_value()
                if _normalize_local(current_name_after) != _normalize_local(submit_name):
                    print(f"PRE-SAVE GUARD CRITICAL: Name still mismatches after rewrite! Got '{current_name_after}'. BLOCKING SAVE.")
                    name_presave_guard = False
                else:
                    print("PRE-SAVE GUARD: Name rewrite successful.")
            else:
                print("PRE-SAVE GUARD: Name verified OK.")

            if not name_presave_guard:
                print("ABORTING SAVE: Name validation failed right before submission. Breaking loop.")
                break

            save_btn = create_page.locator(
                "button:has-text('Save'), button:has-text('Publish'), button:has-text('保存'), button:has-text('发布')"
            ).first
            try:
                await save_btn.click(timeout=9000)
                ok_save = True
            except Exception:
                ok_save = False

            await create_page.wait_for_timeout(3500)

            dismissed = await dismiss_post_submit_guidelines_anywhere(context)
            if dismissed:
                print("Post-submit guideline modal dismissed.")

            has_captcha = await detect_slider_captcha_anywhere(context)
            if has_captcha:
                shot_path = await save_debug_screenshot(
                    create_page,
                    f"captcha_detected_round_{round_idx + 1}.png",
                    full_page=True,
                )
                if shot_path:
                    print(f"DEBUG: captcha screenshot saved: {shot_path}")

                auto_solved = False
                if auto_solve_captcha_enabled:
                    try:
                        print("CAPTCHA detected. Attempting automatic solve before manual fallback...")
                        auto_solved = await try_auto_solve_captcha(create_page, context)
                        print("CAPTCHA auto-solved:", auto_solved)
                    except Exception as e:
                        print(f"ERROR: automatic captcha solve failed with exception: {e}")
                else:
                    print("CAPTCHA detected. Automatic solve is disabled; using manual fallback.")

                if auto_solved:
                    try:
                        await save_btn.click(timeout=9000)
                        ok_save = True
                    except Exception:
                        pass
                    await create_page.wait_for_timeout(2200)
                    dismissed2 = await dismiss_post_submit_guidelines_anywhere(context)
                    if dismissed2:
                        print("Post-auto-solve guideline modal dismissed.")
                    has_captcha = await detect_slider_captcha_anywhere(context)
                    if not has_captcha:
                        print("CAPTCHA fully cleared by auto-solve path.")
                    else:
                        print("CAPTCHA still present after auto-solve. Falling back to manual flow.")

                if has_captcha:
                    print("CAPTCHA detected. Please solve it manually in browser. Waiting auto-resume...")
                    windows_notify(
                        "FlashSale 验证码提醒",
                        f"第{round_idx + 1}轮检测到验证码。请手动完成滑动验证，脚本将自动检测并继续。",
                    )
                    solved = await wait_captcha_resolved_anywhere(context, 30)
                    print("CAPTCHA solved:", solved)
                    if not solved:
                        solved = await wait_captcha_resolved_anywhere(context, CAPTCHA_WAIT_SECONDS)
                        print("CAPTCHA solved after extended wait:", solved)
                        if not solved:
                            print("ERROR: CAPTCHA not solved in time. Stop batch.")
                            break
                    try:
                        await save_btn.click(timeout=9000)
                        ok_save = True
                    except Exception:
                        pass
                    await create_page.wait_for_timeout(2000)

                    dismissed2 = await dismiss_post_submit_guidelines_anywhere(context)
                    if dismissed2:
                        print("Post-captcha guideline modal dismissed.")

            # if no captcha but got-it modal still visible, keep dismissing briefly
            late_modal = False
            for _pg in context.pages:
                try:
                    if await detect_got_it_modal(_pg):
                        late_modal = True
                        break
                except Exception:
                    continue
            if late_modal:
                dismissed3 = await dismiss_post_submit_guidelines_anywhere(context)
                if dismissed3:
                    print("Late guideline modal dismissed.")
                else:
                    windows_notify(
                        "FlashSale 弹窗提醒",
                        f"第{round_idx + 1}轮出现 Got it/指南弹窗，请手动关闭。",
                    )
                    pause_ok2 = await wait_manual_continue(
                        f"got-it dialog on round {round_idx + 1}; close dialog then create resume flag",
                        600,
                    )
                    if not pause_ok2:
                        print("ERROR: manual continue timeout while waiting got-it close. Stop batch.")
                        break

            created_count += 1 if ok_save else 0

            if ok_save:
                shot_path = await save_debug_screenshot(
                    create_page,
                    f"set_one_flashsale_result_{created_count:02d}.png",
                    full_page=True,
                )
                if shot_path:
                    print(f"DEBUG: success screenshot saved: {shot_path}")

            print(
                "Fill status:",
                {
                    "name": ok_name,
                    "name_presave_guard": name_presave_guard,
                    "start_date": ok_start_date,
                    "start_time": ok_start_time,
                    "end_date": ok_end_date,
                    "end_time": ok_end_time,
                    "save_click": ok_save,
                },
            )
            print(
                "Actual inputs:",
                {
                    "start_date": actual_start_date,
                    "start_time": actual_start_time,
                    "end_date": actual_end_date,
                    "end_time": actual_end_time,
                },
            )
            print(
                "Final:",
                submit_name,
                start_date,
                start_time,
                "->",
                end_date,
                end_time,
            )

            if ok_save:
                # persist schedule anchor and rolling seed names only after confirmed save progression
                save_checkpoint_dt(plan["next_end"])
                new_seed_names = compute_rolling_seed_names(plan["next_end"])
                if new_seed_names:
                    persist_seed_names(new_seed_names)
                    print("Rolling seed names updated:", new_seed_names)

            # Continue from current page; next round will force back to management.
            page = create_page

        print(f"\nBATCH DONE. Created count = {created_count}")


if __name__ == "__main__":
    asyncio.run(main())
