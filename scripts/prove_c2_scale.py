#!/usr/bin/env python3
"""
prove_c2_scale.py — temp-DB proof that C2/C3 can clear a large daily queue.

This never writes the production DB. It copies the source DB to a temp DB,
duplicates real website leads until `--target` rows are staged as `pulled`,
then runs C2 and C3 on that temp database and prints metrics.
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path
from queue import Queue

_WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_WORKSPACE / "pipeline"))
sys.path.insert(0, str(_WORKSPACE / "pipeline" / "nodes"))
sys.path.insert(0, str(_WORKSPACE / "pipeline" / "sources"))
sys.path.insert(0, str(_WORKSPACE / "scripts"))


def _copy_rows(conn, source_rows, target):
    ids = []
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(leads)")]
    insert_cols = [c for c in cols if c != "id"]
    placeholders = ", ".join("?" for _ in insert_cols)
    col_sql = ", ".join(insert_cols)

    for i in range(target):
        src = source_rows[i % len(source_rows)]
        values = []
        for col in insert_cols:
            if col == "company":
                values.append(f"{src['company']} ScaleProof {i + 1:04d}")
            elif col == "phone":
                values.append(f"+1999{i + 1:07d}")
            elif col == "stage":
                values.append("pulled")
            elif col in {
                "owner_name", "processor", "tech_signals", "propensity",
                "pain_tier", "pain_theme", "trigger", "trigger_evidence",
                "trigger_source", "template_key", "template_route",
                "sequence_key", "email_angle", "template_cta",
            }:
                values.append(None)
            elif col == "created_at":
                values.append(src[col] if col in src.keys() else None)
            else:
                values.append(src[col] if col in src.keys() else None)
        cur = conn.execute(
            f"INSERT INTO leads ({col_sql}) VALUES ({placeholders})",
            values,
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def main():
    ap = argparse.ArgumentParser(description="C2/C3 1,000-row temp DB proof")
    ap.add_argument("--source-db", default=str(Path.home() / ".outreach/state/leads.db"))
    ap.add_argument("--temp-db", default="/tmp/c2_scale_proof.db")
    ap.add_argument("--target", type=int, default=1000)
    ap.add_argument("--source-limit", type=int, default=50)
    ap.add_argument("--cache-dir", default="/tmp/c2_scale_html_cache")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    source_db = Path(args.source_db).expanduser()
    temp_db = Path(args.temp_db)
    if not source_db.exists():
        raise SystemExit(f"source DB not found: {source_db}")
    if temp_db.exists():
        temp_db.unlink()
    shutil.copy2(source_db, temp_db)

    os.environ["LEAD_DB"] = str(temp_db)
    os.environ["C2_HTML_CACHE_DIR"] = args.cache_dir
    os.environ.setdefault("C2_HTML_CACHE_TTL_SECONDS", "604800")

    import ledger as L
    import c2_scraper
    import c3_analyzer

    if args.quiet:
        logging.getLogger("c2_scraper").setLevel(logging.WARNING)

    conn = L.connect()
    source_rows = conn.execute(
        """
        SELECT * FROM leads
        WHERE website IS NOT NULL AND website != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (args.source_limit,),
    ).fetchall()
    if not source_rows:
        raise SystemExit("no source leads with websites found")

    proof_ids = _copy_rows(conn, source_rows, args.target)
    qmarks = ",".join("?" for _ in proof_ids)
    conn.execute(f"DELETE FROM email_candidates WHERE business_id IN ({qmarks})", proof_ids)
    conn.execute(f"DELETE FROM reviews WHERE business_id IN ({qmarks}) AND source='scrape'", proof_ids)
    conn.commit()

    started = time.monotonic()
    per = []
    lock = threading.Lock()
    progress = {"done": 0}

    def worker():
        worker_conn = L.connect()
        while True:
            lead_id = work.get()
            if lead_id is None:
                work.task_done()
                break
            row = worker_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            t0 = time.monotonic()
            c2_scraper.process(worker_conn, row)
            dt = time.monotonic() - t0
            with lock:
                per.append(dt)
                progress["done"] += 1
                done = progress["done"]
            if done % 100 == 0 or dt >= 20:
                print(f"C2 progress {done}/{len(proof_ids)} lead={lead_id} seconds={dt:.2f}", flush=True)
            work.task_done()
        worker_conn.close()

    work = Queue()
    workers = max(1, args.workers)
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for lead_id in proof_ids:
        work.put(lead_id)
    for _ in threads:
        work.put(None)
    work.join()
    for thread in threads:
        thread.join()

    for lead_id in proof_ids:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row["stage"] == "scraped":
            c3_analyzer.process(conn, row)

    elapsed = time.monotonic() - started
    summary = {
        "target": args.target,
        "source_unique_websites": len({r["website"] for r in source_rows}),
        "temp_db": str(temp_db),
        "cache_dir": args.cache_dir,
        "workers": workers,
        "elapsed_seconds": round(elapsed, 2),
        "avg_seconds_per_lead": round(sum(per) / len(per), 3) if per else 0,
        "p50_seconds": round(statistics.median(per), 3) if per else 0,
        "p90_seconds": round(sorted(per)[int(len(per) * 0.9) - 1], 3) if per else 0,
        "max_seconds": round(max(per), 3) if per else 0,
        "over_20_seconds": sum(1 for v in per if v >= 20),
        "analyzed": conn.execute(
            f"SELECT COUNT(*) FROM leads WHERE id IN ({qmarks}) AND stage='analyzed'",
            proof_ids,
        ).fetchone()[0],
        "email_candidates": conn.execute(
            f"SELECT COUNT(*) FROM email_candidates WHERE business_id IN ({qmarks})",
            proof_ids,
        ).fetchone()[0],
        "with_processor": conn.execute(
            f"SELECT COUNT(*) FROM leads WHERE id IN ({qmarks}) AND processor IS NOT NULL AND processor != ''",
            proof_ids,
        ).fetchone()[0],
        "with_social_urls": conn.execute(
            f"SELECT COUNT(*) FROM leads WHERE id IN ({qmarks}) AND tech_signals LIKE '%social_url:%'",
            proof_ids,
        ).fetchone()[0],
        "with_pain_theme": conn.execute(
            f"SELECT COUNT(*) FROM leads WHERE id IN ({qmarks}) AND pain_theme IS NOT NULL AND pain_theme != ''",
            proof_ids,
        ).fetchone()[0],
        "bad_x_false_positive": conn.execute(
            f"SELECT COUNT(*) FROM leads WHERE id IN ({qmarks}) AND tech_signals LIKE '%pharmacytownrx.com%social_url:x%'",
            proof_ids,
        ).fetchone()[0],
    }
    print("SUMMARY", json.dumps(summary, sort_keys=True))
    conn.close()
    return 0 if summary["analyzed"] == args.target and summary["bad_x_false_positive"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
