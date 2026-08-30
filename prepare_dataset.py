"""Fetch the Spider4SPARQL artifacts (queries + ValueNet ontologies) and
verify the materialized graphs. Nothing from Spider4SPARQL is committed to
this repo; the materialized KG archive has to be downloaded manually first
(link in README.md).

wta_1 and orchestra are excluded: their materialized files are broken in the
official distribution (truncated / empty).
"""

import csv
import io
import os
import sys
import urllib.request

REPO_RAW = "https://raw.githubusercontent.com/ckosten/Spider4SPARQL/main"
QUERIES_URL = f"{REPO_RAW}/nl_sparql_pairs/dev/dev_nl_sparql.csv"

BROKEN_KGS = {"wta_1", "orchestra"}

DEV_KGS = [
    "battle_death", "car_1", "concert_singer", "course_teach",
    "cre_Doc_Template_Mgt", "dog_kennels", "employee_hire_evaluation",
    "flight_2", "museum_visit", "network_1", "pets_1", "poker_player",
    "real_estate_properties", "singer", "student_transcripts_tracking",
    "tvshow", "voter_1", "world_1",
]

BASE = os.path.dirname(os.path.abspath(__file__))


def fetch(url):
    with urllib.request.urlopen(url) as resp:
        return resp.read()


def fetch_queries():
    os.makedirs(os.path.join(BASE, "queries"), exist_ok=True)
    print("downloading dev_nl_sparql.csv...")
    raw = fetch(QUERIES_URL).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    rows = [r for r in reader if r["kg_name"].strip() not in BROKEN_KGS]
    out = os.path.join(BASE, "queries", "dev_nl_sparql.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {len(rows)} queries kept ({', '.join(sorted(BROKEN_KGS))} excluded)")


def fetch_ontologies():
    out_dir = os.path.join(BASE, "ontologies")
    os.makedirs(out_dir, exist_ok=True)
    print("downloading MPBoot ontologies...")
    for kg in DEV_KGS:
        low = kg.lower()
        url = (f"{REPO_RAW}/knowledge_graph_construction/dev_files/"
               f"bootstrapper.spider_{low}/mpboot_spider_{low}.owl")
        try:
            data = fetch(url)
        except Exception as e:
            print(f"  FAILED {kg}: {e}")
            continue
        with open(os.path.join(out_dir, f"{kg}.owl"), "wb") as f:
            f.write(data)
    print(f"  {len(DEV_KGS)} ontologies -> {out_dir}")


def verify_materialized():
    kg_dir = os.path.join(BASE, "materialized_triples")
    print("verifying materialized graphs...")
    if not os.path.isdir(kg_dir):
        print(f"  {kg_dir} missing.\n"
              "  Download the dev KG archive (link in README.md), extract the"
              " .ttl files there and re-run this script.")
        return False
    for broken in BROKEN_KGS:
        path = os.path.join(kg_dir, f"{broken}.ttl")
        if os.path.exists(path):
            os.remove(path)
            print(f"  removed broken file {broken}.ttl")
    ok = True
    for kg in DEV_KGS:
        path = next((p for p in (os.path.join(kg_dir, f"{kg}.ttl"),
                                 os.path.join(kg_dir, f"{kg}.nt"),
                                 os.path.join(kg_dir, kg, f"{kg}.ttl"),
                                 os.path.join(kg_dir, kg, f"{kg}.nt"))
                     if os.path.exists(p)), None)
        if not path or os.path.getsize(path) == 0:
            print(f"  missing or empty: {kg}")
            ok = False
            continue
        # cheap truncation check: last line has to close with a dot
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 64))
            tail = f.read().decode("utf-8", errors="replace").rstrip()
        if not tail.endswith("."):
            print(f"  truncated: {path}")
            ok = False
    if ok:
        print(f"  all {len(DEV_KGS)} graphs present and intact")
    return ok


if __name__ == "__main__":
    fetch_queries()
    fetch_ontologies()
    complete = verify_materialized()
    if complete:
        print("\ndone. next: python build_enriched_benchmark.py")
    else:
        print("\nincomplete, see messages above")
    sys.exit(0 if complete else 1)
