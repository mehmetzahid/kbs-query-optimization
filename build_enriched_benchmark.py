"""Build the TBox-enriched benchmark (redundancy-injection protocol).

The MPBoot ontologies are flat, so there is nothing for subsumption pruning
to do out of the box. This script introduces one root class per KG
(:spider_entity), writes subClassOf axioms for every observed class into
ontologies_enriched/, materializes the root-class memberships into
materialized_triples_enriched/ (keeps baseline and optimized queries
result-equivalent), and injects a redundant `?v a :spider_entity .`
constraint per variable per group into the query CSV.

Typed subjects are harvested textually from the line-based Turtle files so
blank-node labels stay verbatim and co-refer within each document.
"""

import csv
import os
import re

import rdflib
from rdflib import RDF, RDFS, OWL, URIRef

from optimizer import SubsumptionOptimizer

ROOT_CLASS = "http://valuenet/ontop/spider_entity"
ROOT_PNAME = ":spider_entity"

TYPE_LINE_RE = re.compile(r'^(<[^>]+>|_:\S+) a <([^>]+)> \.\s*$', re.MULTILINE)

BASE = os.path.dirname(os.path.abspath(__file__))
ABOX_DIR = os.path.join(BASE, "materialized_triples")
OWL_DIR = os.path.join(BASE, "ontologies")
TBOX_OUT = os.path.join(BASE, "ontologies_enriched")
ABOX_OUT = os.path.join(BASE, "materialized_triples_enriched")
QUERIES_IN = os.path.join(BASE, "queries", "dev_nl_sparql.csv")
QUERIES_OUT = os.path.join(BASE, "queries", "dev_nl_sparql_enriched.csv")


def enrich_kg(kg):
    with open(os.path.join(ABOX_DIR, f"{kg}.ttl"), encoding="utf-8") as f:
        text = f.read()

    matches = TYPE_LINE_RE.findall(text)
    if not matches:
        print(f"  {kg}: no type triples found, skipped")
        return False
    subjects = sorted({s for s, _ in matches})
    classes = sorted({c for _, c in matches})

    extra = "".join(f"{s} a <{ROOT_CLASS}> .\n" for s in subjects)
    with open(os.path.join(ABOX_OUT, f"{kg}.ttl"), "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
        f.write(extra)

    tbox = rdflib.Graph()
    owl_path = os.path.join(OWL_DIR, f"{kg}.owl")
    if os.path.exists(owl_path):
        tbox.parse(owl_path, format="xml")
    else:
        print(f"  {kg}: ontology missing, writing hierarchy axioms only")
    declared = {str(c) for c in tbox.subjects(RDF.type, OWL.Class)}
    root = URIRef(ROOT_CLASS)
    tbox.add((root, RDF.type, OWL.Class))
    for cls in sorted(set(classes) | declared):
        if cls != ROOT_CLASS:
            tbox.add((URIRef(cls), RDFS.subClassOf, root))
    tbox.serialize(os.path.join(TBOX_OUT, f"{kg}.ttl"), format="turtle")

    print(f"  {kg}: {len(subjects)} subjects, {len(classes)} classes")
    return True


def type_statement_var(text):
    tokens = text.split()
    if len(tokens) != 3 or not tokens[0].startswith('?'):
        return None
    if tokens[1] not in ('a', 'rdf:type'):
        return None
    if tokens[2].startswith(':') or tokens[2].startswith('<'):
        return tokens[0]
    return None


def inject_redundancy(query):
    """Add `?v a :spider_entity .` after the first type statement of each
    (group, variable)."""
    statements = SubsumptionOptimizer._scan_statements(query)
    insertions = []
    seen = set()
    for stmt in statements:
        var = type_statement_var(stmt["text"])
        if not var or (stmt["group"], var) in seen:
            continue
        seen.add((stmt["group"], var))
        end = stmt["end"]
        if end <= len(query) and query[end - 1] == '.':
            insertions.append((end, f" {var} a {ROOT_PNAME} ."))
        else:  # group ended without a trailing dot
            insertions.append((end, f" . {var} a {ROOT_PNAME}"))
    for pos, text in sorted(insertions, reverse=True):
        query = query[:pos] + text + query[pos:]
    return query, len(insertions)


def main():
    os.makedirs(TBOX_OUT, exist_ok=True)
    os.makedirs(ABOX_OUT, exist_ok=True)

    print("enriching knowledge graphs...")
    kgs = sorted(f[:-4] for f in os.listdir(ABOX_DIR) if f.endswith(".ttl"))
    built = [kg for kg in kgs if enrich_kg(kg)]

    print("\ninjecting redundant type constraints...")
    with open(QUERIES_IN, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = reader.fieldnames

    total = 0
    for row in rows:
        row["query"], n = inject_redundancy(row["query"])
        total += n

    with open(QUERIES_OUT, "w", newline='', encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{len(built)}/{len(kgs)} KGs enriched")
    print(f"{total} triples injected into {len(rows)} queries -> {QUERIES_OUT}")


if __name__ == "__main__":
    main()
