"""
Gemini OCR Engine Implementation
=================================
Uses Google's Gemini Vision-Language Model to perform OCR + layout analysis
in a single API call per page.

Why Gemini:
- No local OCR models — zero RAM cost on the Zeabur server
- Native support for Traditional Chinese, Simplified Chinese, Japanese,
  Korean, English, and 100+ other languages
- Excellent vertical-text recognition
- Single API call returns BOTH text AND layout classification

Default model: gemini-2.5-flash (free tier: 10 RPM, 250 RPD)
Configurable in config.yaml under ocr.model_name.

Rate-limiting:
The engine self-throttles to stay within the configured RPM. Free-tier users
should leave rpm_limit at 10 for gemini-2.5-flash.
"""

from __future__ import annotations
import os
import io
import json
import time
import logging
import threading
from typing import List, Dict, Any
from collections import deque

from ocr_engine import (
    OCREngine, TextBlock, LayoutBlock, BBox,
    TextDirection, LayoutType
)

logger = logging.getLogger(__name__)


# ── Language detection helpers (used to tag TextBlock.language) ───────────────
CJK_RANGES = [
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x20000, 0x2A6DF),
    (0x3000, 0x303F),
]
HIRAGANA_RANGE = (0x3040, 0x309F)
KATAKANA_RANGE = (0x30A0, 0x30FF)
HANGUL_RANGE   = (0xAC00, 0xD7AF)
TRAD_CHARS = set("繁體傳統國語臺灣")


def _in_range(char: str, lo: int, hi: int) -> bool:
    return lo <= ord(char) <= hi


def _detect_lang_from_text(text: str) -> str:
    has_hiragana = any(_in_range(c, *HIRAGANA_RANGE) for c in text)
    has_katakana = any(_in_range(c, *KATAKANA_RANGE) for c in text)
    has_hangul   = any(_in_range(c, *HANGUL_RANGE)   for c in text)
    has_cjk      = any(
        any(_in_range(c, lo, hi) for lo, hi in CJK_RANGES)
        for c in text
    )
    has_trad = any(c in TRAD_CHARS for c in text)
    if has_hangul:
        return "korean"
    if has_hiragana or has_katakana:
        return "japan"
    if has_cjk:
        return "ch_tra" if has_trad else "ch_sim"
    return "en"


# ── Rate limiter ──────────────────────────────────────────────────────────────
class RateLimiter:
    """
    Sliding-window rate limiter. Blocks the calling thread until a request
    can be made without exceeding `max_rpm` requests in any 60-second window.
    """
    def __init__(self, max_rpm: int):
        self.max_rpm = max_rpm
        self.calls: deque = deque()
        self.lock = threading.Lock()

    def wait(self):
        """Block until a request can be issued without exceeding the limit."""
        with self.lock:
            now = time.monotonic()
            # Remove timestamps older than 60 seconds
            while self.calls and now - self.calls[0] > 60.0:
                self.calls.popleft()
            if len(self.calls) >= self.max_rpm:
                sleep_for = 60.0 - (now - self.calls[0]) + 0.1
                if sleep_for > 0:
                    logger.info(f"Rate limit reached — sleeping {sleep_for:.1f}s")
                    time.sleep(sleep_for)
                    now = time.monotonic()
                    while self.calls and now - self.calls[0] > 60.0:
                        self.calls.popleft()
            self.calls.append(time.monotonic())


# ── Prompt for OCR + layout (single call per page) ────────────────────────────
OCR_PROMPT = """You are an OCR engine. Read this PDF page image and return ONLY a JSON object describing every text region you can see.

Return this exact JSON structure:
{
  "direction": "horizontal" or "vertical",
  "blocks": [
    {
      "text": "the recognised text content",
      "type": "heading" | "paragraph" | "list-item" | "footnote" | "page-number" | "caption",
      "bbox": [x0, y0, x1, y1]
    }
  ]
}

Rules:
- "direction" indicates the dominant text flow on this page. Vertical text (typical in CJK literature) flows top-to-bottom, right-to-left.
- "bbox" is in pixel coordinates of THIS image, where (0,0) is top-left and values are integers.
- Classify each region:
  * "heading": large title text, chapter/section headers
  * "paragraph": ordinary body text
  * "list-item": bullet or numbered list entry
  * "footnote": small text typically at the bottom of the page
  * "page-number": isolated page number, usually at top or bottom corner/center
  * "caption": text describing a figure or image
- Preserve the original language and script — do NOT translate.
- For vertical text, output the text in natural reading order (top-to-bottom within each column, columns ordered right-to-left).
- Order "blocks" in natural reading order for the page.
- If the page has no text (e.g. a full-page illustration), return {"direction":"horizontal","blocks":[]}.
- Return ONLY the JSON object, no commentary, no markdown fences.
"""


class GeminiOCREngine(OCREngine):
    """
    OCR engine backed by Google Gemini.

    A single API call per page returns OCR text + layout classification +
    direction detection. This minimises API quota use.
    """

    def __init__(self, config: dict):
        self.config        = config
        self.model_name    = config.get("model_name", "gemini-2.5-flash")
        self.rpm_limit     = int(config.get("rpm_limit", 10))
        self.rpd_limit     = int(config.get("rpd_limit", 250))
        self.api_key       = os.environ.get("GEMINI_API_KEY", "").strip()
        self.max_retries   = int(config.get("max_retries", 3))
        self.timeout_s     = int(config.get("request_timeout_s", 120))
        self._client       = None
        self._loaded       = False
        self._rate_limiter = RateLimiter(self.rpm_limit)

        # Cache of last result per page (so detect_direction → recognize →
        # get_layout can share one API call). Keyed by id() of the image
        # object, cleared after each page in the worker pipeline.
        self._page_cache: Dict[int, dict] = {}

    def load(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY environment variable is not set. "
                "Get a key at https://aistudio.google.com and add it in "
                "Zeabur's environment variables."
            )

        logger.info(f"Initialising Gemini client (model={self.model_name}, rpm={self.rpm_limit})…")
        from google import genai
        self._client = genai.Client(api_key=self.api_key)
        self._loaded = True
        logger.info("Gemini client ready.")

    # ── OCREngine interface ──────────────────────────────────────────────────

    def detect_language(self, page_image) -> str:
        """Run the page-level analysis and infer dominant language."""
        result = self._analyse_page(page_image)
        all_text = "".join(b.get("text", "") for b in result.get("blocks", []))
        return _detect_lang_from_text(all_text)

    def detect_direction(self, page_image) -> TextDirection:
        result = self._analyse_page(page_image)
        d = result.get("direction", "horizontal")
        return "vertical" if d == "vertical" else "horizontal"

    def recognize(
        self,
        page_image,
        direction: TextDirection,
    ) -> List[TextBlock]:
        result = self._analyse_page(page_image)
        blocks: List[TextBlock] = []
        for b in result.get("blocks", []):
            text = (b.get("text") or "").strip()
            if not text:
                continue
            bbox_arr = b.get("bbox") or [0, 0, 0, 0]
            try:
                x0, y0, x1, y1 = bbox_arr
            except (ValueError, TypeError):
                x0 = y0 = x1 = y1 = 0
            bbox = BBox(float(x0), float(y0), float(x1), float(y1))
            lang = _detect_lang_from_text(text)
            blocks.append(TextBlock(
                text=text,
                bbox=bbox,
                language=lang,
                font_size_estimate=bbox.height,
                confidence=1.0,
                direction=direction,
            ))
        return blocks

    def get_layout(self, page_image) -> List[LayoutBlock]:
        result = self._analyse_page(page_image)
        layout_blocks: List[LayoutBlock] = []
        for b in result.get("blocks", []):
            type_str = (b.get("type") or "").lower().strip()
            block_type: LayoutType = type_str if type_str in {
                "heading", "paragraph", "list-item", "footnote",
                "page-number", "caption", "image"
            } else "unknown"
            bbox_arr = b.get("bbox") or [0, 0, 0, 0]
            try:
                x0, y0, x1, y1 = bbox_arr
            except (ValueError, TypeError):
                x0 = y0 = x1 = y1 = 0
            layout_blocks.append(LayoutBlock(
                block_type=block_type,
                bbox=BBox(float(x0), float(y0), float(x1), float(y1)),
            ))
        return layout_blocks

    def health_check(self) -> bool:
        return self._loaded

    def reset_page_cache(self):
        """Called by the worker between pages to free memory."""
        self._page_cache.clear()

    # ── Internal: single API call per page, cached ──────────────────────────
    def _analyse_page(self, page_image) -> dict:
        """
        Run one Gemini API call for this page, cache the result, and
        return the parsed dict {"direction": ..., "blocks": [...]}.

        Reusing the cached result across detect_direction / recognize /
        get_layout means each PDF page costs exactly ONE API call.
        """
        self._assert_loaded()
        cache_key = id(page_image)
        if cache_key in self._page_cache:
            return self._page_cache[cache_key]

        # Convert BGR ndarray → JPEG bytes
        jpeg_bytes = self._image_to_jpeg(page_image)

        result = self._call_gemini_with_retry(jpeg_bytes)
        self._page_cache[cache_key] = result
        return result

    def _image_to_jpeg(self, page_image) -> bytes:
        """Convert OpenCV BGR ndarray to JPEG bytes for the API."""
        from PIL import Image
        import numpy as np
        # Downscale very large pages to keep token usage low.
        # Gemini handles up to about 3072×3072 well; we cap at 2048 max-side.
        h, w = page_image.shape[:2]
        max_side = 2048
        if max(h, w) > max_side:
            scale = max_side / max(h, w)
            new_w = int(w * scale)
            new_h = int(h * scale)
            import cv2
            page_image = cv2.resize(page_image, (new_w, new_h), interpolation=cv2.INTER_AREA)

        rgb = page_image[:, :, ::-1]   # BGR → RGB
        pil = Image.fromarray(rgb.astype(np.uint8))
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()

    def _call_gemini_with_retry(self, jpeg_bytes: bytes) -> dict:
        from google.genai import types
        from google.genai import errors as genai_errors

        attempt = 0
        last_exc: Exception = RuntimeError("no attempts made")

        while attempt < self.max_retries:
            attempt += 1
            self._rate_limiter.wait()
            try:
                response = self._client.models.generate_content(
                    model=self.model_name,
                    contents=[
                        types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
                        OCR_PROMPT,
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.0,
                    ),
                )
                text = (response.text or "").strip()
                return self._parse_response(text)

            except genai_errors.APIError as e:
                last_exc = e
                # Quota / rate-limit (HTTP 429) — back off
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code in (429, 503):
                    wait = min(60, 5 * (2 ** (attempt - 1)))
                    logger.warning(
                        f"Gemini API throttled (HTTP {code}) — retry {attempt}/"
                        f"{self.max_retries} in {wait}s"
                    )
                    time.sleep(wait)
                    continue
                # Other API errors — re-raise immediately
                raise

            except Exception as e:
                last_exc = e
                wait = 3 * attempt
                logger.warning(
                    f"Gemini call failed: {e} — retry {attempt}/"
                    f"{self.max_retries} in {wait}s"
                )
                time.sleep(wait)

        raise RuntimeError(
            f"Gemini API failed after {self.max_retries} attempts: {last_exc}"
        )

    def _parse_response(self, text: str) -> dict:
        """Tolerantly parse Gemini's JSON response."""
        if not text:
            return {"direction": "horizontal", "blocks": []}
        # Strip markdown fences if the model added any
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to locate a JSON object inside the response
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    logger.warning("Could not parse Gemini response as JSON.")
                    return {"direction": "horizontal", "blocks": []}
            else:
                logger.warning("Gemini response did not contain JSON.")
                return {"direction": "horizontal", "blocks": []}

        if not isinstance(data, dict):
            return {"direction": "horizontal", "blocks": []}

        blocks = data.get("blocks", [])
        if not isinstance(blocks, list):
            blocks = []

        return {
            "direction": data.get("direction", "horizontal"),
            "blocks":    blocks,
        }

    def _assert_loaded(self):
        if not self._loaded:
            raise RuntimeError("GeminiOCREngine.load() must be called before use.")
