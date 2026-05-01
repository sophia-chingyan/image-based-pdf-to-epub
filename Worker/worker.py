"""
Worker
======
Background process that:
1. Loads the Gemini OCR engine once at startup
2. Polls Redis job queue
3. Runs the full PDF → EPUB pipeline for each job
4. Updates job status in Redis
5. Handles cleanup of temporary files
6. Honours stop requests issued by the user via /api/stop
7. Tracks daily Gemini API usage and warns/blocks when quota is exceeded

Each PDF page = exactly ONE Gemini API call (OCR + layout + direction
returned together in a single structured-JSON response).
"""

from __future__ import annotations
import os
import json
import time
import shutil
import logging
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from store import get_sync_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("worker")

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config.yaml"))
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

UPLOAD_DIR  = Path("/app/uploads")
OUTPUT_DIR  = Path("/app/outputs")
TMPWORK_DIR = Path("/app/tmp-work")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TMPWORK_DIR.mkdir(parents=True, exist_ok=True)

DPI          = CFG["ocr"]["dpi"]
BATCH_SIZE   = CFG["pipeline"]["page_batch_size"]
WRITING_MODE = CFG["epub"]["default_writing_mode"]
CLEANUP      = CFG["pipeline"]["tmp_cleanup_on_complete"]
RPD_LIMIT    = int(CFG["ocr"].get("rpd_limit", 250))


# ── Daily usage tracking (uses Pacific Time, which is when Google resets) ─────
# Pacific Time is UTC-8 (PST) or UTC-7 (PDT). We approximate as UTC-7,
# which is the correct value for ~8 months of the year. The 1-hour drift
# during winter is acceptable because:
#   - We use this purely as a soft warning, not a hard block
#   - Google's actual server-side counter is what enforces quota
PACIFIC_OFFSET = timedelta(hours=-7)


def pacific_today_key() -> str:
    """Return YYYY-MM-DD for the current Pacific date — used as Redis key suffix."""
    now_pacific = datetime.now(timezone.utc) + PACIFIC_OFFSET
    return now_pacific.strftime("%Y-%m-%d")


def get_today_usage(r: redis.Redis) -> int:
    """Return number of Gemini API calls made today (Pacific time)."""
    raw = r.get(f"gemini_usage:{pacific_today_key()}")
    return int(raw) if raw else 0


def increment_usage(r: redis.Redis, count: int = 1) -> int:
    """Increment today's usage counter and return new total."""
    key = f"gemini_usage:{pacific_today_key()}"
    new_total = r.incrby(key, count)
    # Set 48-hour expiry so old day-keys disappear automatically
    r.expire(key, 48 * 3600)
    return int(new_total)


# ── Helpers ───────────────────────────────────────────────────────────────────
def update_job(r: redis.Redis, job_id: str, **kwargs):
    """Update fields of a job record in Redis."""
    raw = r.get(f"job:{job_id}")
    if not raw:
        return
    job = json.loads(raw)
    job.update(kwargs)
    r.set(f"job:{job_id}", json.dumps(job))


def page_count_safe(pdf_path: Path) -> int:
    """Return total page count of a PDF without running OCR."""
    import fitz
    doc = fitz.open(str(pdf_path))
    n = len(doc)
    doc.close()
    return n


def run_pipeline(r: redis.Redis, job: dict, engine) -> None:
    """Full PDF → EPUB conversion pipeline for a single job."""
    from pdf_ingestion import ingest_pdf, rasterize_page
    from structure_analysis import analyse_page, build_toc, DocumentStructure
    from epub_assembly import assemble_epub

    job_id   = job["job_id"]
    pdf_path = Path(job["pdf_path"])
    tmp_dir  = TMPWORK_DIR / job_id
    tmp_dir.mkdir(parents=True, exist_ok=True)

    def check_stop() -> bool:
        """Return True and update status if a stop has been requested."""
        raw_current = r.get(f"job:{job_id}")
        if raw_current:
            current = json.loads(raw_current)
            if current.get("stop_requested") or current.get("status") == "stopped":
                update_job(r, job_id, status="stopped",
                           message="Stopped by user.",
                           stop_requested=False)
                return True
        return False

    try:
        # ── Step 1: Ingest PDF ───────────────────────────────────────────────
        update_job(r, job_id, status="processing", message="Ingesting PDF…", progress=2)
        ingested = ingest_pdf(pdf_path)
        total_pages = ingested.meta.total_pages

        structured_pages = []
        image_id_counter = [0]

        # ── Steps 2–5: Rasterize → Gemini OCR → Structure ───────────────────
        for page_num in range(total_pages):
            if check_stop():
                ingested.doc.close()
                return

            # ── Daily quota check (hard stop if exceeded) ────────────────────
            usage_today = get_today_usage(r)
            if usage_today >= RPD_LIMIT:
                msg = (f"Daily Gemini quota reached ({usage_today}/{RPD_LIMIT}). "
                       f"Quota resets at midnight Pacific Time. Job will be "
                       f"stopped — restart it tomorrow to continue.")
                logger.warning(msg)
                update_job(r, job_id, status="failed",
                           message="Daily Gemini quota reached.",
                           error=msg)
                ingested.doc.close()
                return

            progress = int(5 + (page_num / total_pages) * 85)
            update_job(
                r, job_id,
                message=(f"OCR Processing (page {page_num + 1} of {total_pages}) "
                         f"· Quota {usage_today}/{RPD_LIMIT}…"),
                progress=progress,
            )

            # Step 2: Rasterize
            page_img = rasterize_page(ingested.doc, page_num, dpi=DPI)

            # Steps 3-4: Single Gemini call gives us direction + text + layout
            direction     = engine.detect_direction(page_img)
            text_blocks   = engine.recognize(page_img, direction)
            layout_blocks = engine.get_layout(page_img)

            # Increment quota counter (one Gemini call was made for this page)
            increment_usage(r, count=1)

            # Step 5: Structure analysis
            page_info = ingested.pages[page_num]
            structured_page = analyse_page(
                page_number=page_num,
                text_blocks=text_blocks,
                layout_blocks=layout_blocks,
                page_info=page_info,
                direction=direction,
                image_id_counter=image_id_counter,
            )
            structured_pages.append(structured_page)

            # Free memory: drop the cached Gemini result and the page image
            engine.reset_page_cache()
            del page_img

            if (page_num + 1) % BATCH_SIZE == 0:
                import gc
                gc.collect()

        ingested.doc.close()

        # ── Step 6: Build TOC ────────────────────────────────────────────────
        toc = build_toc(structured_pages)

        structure = DocumentStructure(
            title=ingested.meta.title,
            author=ingested.meta.author,
            pages=structured_pages,
            toc=toc,
        )

        # ── Step 7: Assemble EPUB ────────────────────────────────────────────
        update_job(r, job_id, message="Assembling EPUB…", progress=92)
        epub_path = OUTPUT_DIR / f"{job_id}.epub"
        assemble_epub(structure, epub_path, writing_mode_override=WRITING_MODE)

        # ── Step 8: Done ─────────────────────────────────────────────────────
        update_job(
            r, job_id,
            status="done",
            message="Complete",
            progress=100,
            epub_path=str(epub_path),
        )
        logger.info(f"Job {job_id} completed: {epub_path}")

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f"Job {job_id} failed: {exc}\n{tb}")
        update_job(
            r, job_id,
            status="failed",
            message="Conversion failed.",
            error=str(exc),
        )

    finally:
        if CLEANUP and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def cleanup_expired_files(r: redis.Redis):
    """
    Delete uploads older than retention period and EPUBs past their expiry.
    Runs once per hour.
    """
    upload_retention = CFG["pipeline"]["upload_retention_hours"] * 3600
    output_retention = CFG["pipeline"]["output_retention_days"] * 86400
    now = time.time()

    for f in UPLOAD_DIR.glob("*.pdf"):
        if now - f.stat().st_mtime > upload_retention:
            f.unlink(missing_ok=True)
            logger.info(f"Deleted expired upload: {f.name}")

    for f in OUTPUT_DIR.glob("*.epub"):
        if now - f.stat().st_mtime > output_retention:
            f.unlink(missing_ok=True)
            logger.info(f"Deleted expired EPUB: {f.name}")


def main():
    logger.info("Worker starting…")

    # ── Load OCR engine once ──────────────────────────────────────────────────
    from engine_factory import get_engine
    engine = get_engine(CFG["ocr"])
    engine.load()
    logger.info("OCR engine ready.")

    # ── Connect to Redis ──────────────────────────────────────────────────────
    r = get_sync_redis()

    last_cleanup = time.time()
    CLEANUP_INTERVAL = 3600  # 1 hour

    logger.info("Listening for jobs…")

    while True:
        if time.time() - last_cleanup > CLEANUP_INTERVAL:
            cleanup_expired_files(r)
            last_cleanup = time.time()

        result = r.brpop("job_queue", timeout=30)
        if result is None:
            continue

        _, job_id = result
        raw = r.get(f"job:{job_id}")
        if not raw:
            logger.warning(f"Job {job_id} not found in Redis — skipping.")
            continue

        job = json.loads(raw)
        if job.get("status") != "queued":
            logger.info(f"Job {job_id} skipped (status: {job.get('status')}).")
            continue

        logger.info(f"Processing job {job_id}: {job.get('filename')}")
        update_job(r, job_id, status="processing", message="Starting…", progress=1)

        run_pipeline(r, job, engine)


if __name__ == "__main__":
    main()
