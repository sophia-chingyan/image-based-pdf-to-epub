"""
Worker — supports three output formats: epub, textlayer, clean.
"""
from __future__ import annotations
import os, sys, json, time, shutil, logging, traceback, gc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml
from store import get_sync_redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("worker")

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config.yaml"))
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

UPLOAD_DIR  = Path("/app/uploads")
OUTPUT_DIR  = Path("/app/outputs")
TMPWORK_DIR = Path("/app/tmp-work")
for d in (UPLOAD_DIR, OUTPUT_DIR, TMPWORK_DIR):
    d.mkdir(parents=True, exist_ok=True)

DPI          = CFG["ocr"]["dpi"]
BATCH_SIZE   = CFG["pipeline"]["page_batch_size"]
WRITING_MODE = CFG["epub"]["default_writing_mode"]
CLEANUP      = CFG["pipeline"].get("tmp_cleanup_on_complete", True)


def update_job(r, job_id: str, **kw):
    raw = r.get(f"job:{job_id}")
    if not raw:
        return
    job = json.loads(raw)
    job.update(kw)
    r.set(f"job:{job_id}", json.dumps(job))


def run_pipeline(r, job: dict, engine) -> None:
    from pdf_ingestion import ingest_pdf, rasterize_page
    from structure_analysis import analyse_page, build_toc, DocumentStructure
    from epub_assembly import assemble_epub
    from pdf_assembly import assemble_textlayer_pdf, assemble_clean_pdf

    job_id   = job["job_id"]
    pdf_path = Path(job["pdf_path"])
    tmp_dir  = TMPWORK_DIR / job_id
    tmp_dir.mkdir(parents=True, exist_ok=True)

    output_formats = job.get("output_formats", ["epub"]) or ["epub"]

    def check_stop() -> bool:
        raw = r.get(f"job:{job_id}")
        if raw:
            cur = json.loads(raw)
            if cur.get("stop_requested") or cur.get("status") == "stopped":
                update_job(r, job_id, status="stopped", message="Stopped by user.", stop_requested=False)
                return True
        return False

    try:
        update_job(r, job_id, status="processing", message="Ingesting PDF…", progress=2)
        ingested = ingest_pdf(pdf_path)
        total_pages = ingested.meta.total_pages
        structured_pages = []
        image_id_counter = [0]

        for page_num in range(total_pages):
            if check_stop():
                ingested.doc.close()
                return

            progress = int(5 + (page_num / total_pages) * 80)
            update_job(r, job_id, message=f"OCR page {page_num+1} / {total_pages}…", progress=progress)

            page_img = rasterize_page(ingested.doc, page_num, dpi=DPI)
            direction     = engine.detect_direction(page_img)
            text_blocks   = engine.recognize(page_img, direction)
            layout_blocks = engine.get_layout(page_img)

            page_info = ingested.pages[page_num]
            sp = analyse_page(page_number=page_num, text_blocks=text_blocks,
                              layout_blocks=layout_blocks, page_info=page_info,
                              direction=direction, image_id_counter=image_id_counter)
            structured_pages.append(sp)
            engine.reset_page_cache()
            del page_img
            if (page_num + 1) % BATCH_SIZE == 0:
                gc.collect()

        ingested.doc.close()

        toc = build_toc(structured_pages)
        structure = DocumentStructure(
            title=ingested.meta.title, author=ingested.meta.author,
            pages=structured_pages, toc=toc)

        # ── Assemble outputs (per-format error isolation) ────────────────────
        output_paths = {}
        format_errors = []
        n = len(output_formats)

        if "epub" in output_formats:
            i = output_formats.index("epub")
            update_job(r, job_id, message="Assembling EPUB…", progress=87 + int(i/n*10))
            try:
                p = OUTPUT_DIR / f"{job_id}.epub"
                assemble_epub(structure, p, writing_mode_override=WRITING_MODE)
                output_paths["epub_path"] = str(p)
            except Exception as e:
                logger.error(f"EPUB assembly failed for {job_id}: {e}\n{traceback.format_exc()}")
                format_errors.append(f"EPUB: {e}")

        if "textlayer" in output_formats:
            i = output_formats.index("textlayer")
            update_job(r, job_id, message="Building searchable PDF…", progress=87 + int(i/n*10))
            try:
                p = OUTPUT_DIR / f"{job_id}_searchable.pdf"
                assemble_textlayer_pdf(structure, pdf_path, p)
                output_paths["textlayer_path"] = str(p)
            except Exception as e:
                logger.error(f"Text-layer PDF assembly failed for {job_id}: {e}\n{traceback.format_exc()}")
                format_errors.append(f"Searchable PDF: {e}")

        if "clean" in output_formats:
            i = output_formats.index("clean")
            update_job(r, job_id, message="Building clean PDF…", progress=87 + int(i/n*10))
            try:
                p = OUTPUT_DIR / f"{job_id}_clean.pdf"
                assemble_clean_pdf(structure, p)
                output_paths["clean_pdf_path"] = str(p)
            except Exception as e:
                logger.error(f"Clean PDF assembly failed for {job_id}: {e}\n{traceback.format_exc()}")
                format_errors.append(f"Clean PDF: {e}")

        # ── Determine final status ───────────────────────────────────────────
        if output_paths:
            # At least one format succeeded
            if format_errors:
                error_summary = "; ".join(format_errors)
                update_job(r, job_id, status="done",
                           message=f"Partial success ({len(format_errors)} format(s) failed)",
                           progress=100, error=error_summary, **output_paths)
                logger.warning(f"Job {job_id} partial: {error_summary}")
            else:
                update_job(r, job_id, status="done", message="Complete",
                           progress=100, **output_paths)
                logger.info(f"Job {job_id} done: {list(output_paths.keys())}")
        else:
            # All formats failed
            error_summary = "; ".join(format_errors) if format_errors else "No output produced"
            update_job(r, job_id, status="failed",
                       message="Conversion failed.", error=error_summary)
            logger.error(f"Job {job_id} failed: all formats errored: {error_summary}")

    except Exception as exc:
        logger.error(f"Job {job_id} failed: {exc}\n{traceback.format_exc()}")
        update_job(r, job_id, status="failed", message="Conversion failed.", error=str(exc))
    finally:
        if CLEANUP and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def cleanup_expired_files(r):
    ur = CFG["pipeline"]["upload_retention_hours"] * 3600
    orr = CFG["pipeline"]["output_retention_days"] * 86400
    now = time.time()
    for f in UPLOAD_DIR.glob("*.pdf"):
        if now - f.stat().st_mtime > ur:
            f.unlink(missing_ok=True)
    for ext in ("*.epub", "*.pdf"):
        for f in OUTPUT_DIR.glob(ext):
            if now - f.stat().st_mtime > orr:
                f.unlink(missing_ok=True)


def main():
    logger.info("Worker starting…")
    from engine_factory import get_engine
    engine = get_engine(CFG["ocr"])
    engine.load()
    logger.info("OCR engine ready.")
    r = get_sync_redis()
    last_cleanup = time.time()

    while True:
        if time.time() - last_cleanup > 3600:
            cleanup_expired_files(r)
            last_cleanup = time.time()
        result = r.brpop("job_queue", timeout=30)
        if result is None:
            continue
        _, job_id = result
        raw = r.get(f"job:{job_id}")
        if not raw:
            continue
        job = json.loads(raw)
        if job.get("status") != "queued":
            continue
        logger.info(f"Processing {job_id}: {job.get('filename')}")
        update_job(r, job_id, status="processing", message="Starting…", progress=1)
        run_pipeline(r, job, engine)


if __name__ == "__main__":
    main()
