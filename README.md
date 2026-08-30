# Query Optimization in Knowledge-Based Systems Using Graph-Based Subsumption Reasoning

A static SPARQL query optimizer that prunes hierarchically redundant triple
patterns from Basic Graph Patterns (BGPs) before execution, evaluated on the
[Spider4SPARQL](https://github.com/ckosten/Spider4SPARQL) benchmark.

Instead of relying on full graph homomorphism (NP-hard), the optimizer parses
a query with rdflib's SPARQL parser, traverses the algebraic tree to collect
per-BGP `rdf:type` constraints, and cross-references them against an RDFS
TBox (precomputed `rdfs:subClassOf` transitive closure). It then removes,
per group graph pattern:

1. **Exact duplicate triple patterns** (self-subsumption — idempotent under
   SPARQL's BGP set semantics; 367 such duplicates occur naturally in the
   Spider4SPARQL dev queries), and
2. **Subsumption-redundant type constraints** — `?x a :C` where another
   constraint `?x a :D` with `D ⊑ C` already holds in the same group.

Every rewrite is re-parsed before use; on any failure the original query is
returned unchanged, so the optimizer can never emit invalid SPARQL.

Key empirical results (932 dev queries, 18 KGs, pyoxigraph engine):
result equivalence is preserved on 100% of queries, execution-time savings
scale with knowledge-graph size (up to ~46% on `world_1`), and pruning
rescues some queries from timeout entirely.

## Components

| File | Role |
|---|---|
| `optimizer.py` | `SubsumptionOptimizer`: algebra-based BGP analysis + group-scoped statement rewriting |
| `benchmark.py` | Benchmark runner: hard per-query timeouts (worker process), rdflib/pyoxigraph engines, subset mode |
| `prepare_dataset.py` | Fetches Spider4SPARQL queries + ValueNet ontologies; verifies the materialized KGs |
| `build_enriched_benchmark.py` | Builds the TBox-enriched benchmark (redundancy-injection protocol) |

## Setup

Requires Python ≥ 3.9.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Step 1 — Fetch the Spider4SPARQL data (not redistributed here)

First download the **Materialized Knowledge Graphs Dev set** archive from the
Spider4SPARQL project ([Google Drive link](https://drive.google.com/file/d/1S6xaI0VfnFMrsuyjxv2uQPok_CikhLLx/view?usp=sharing),
also referenced in the [Spider4SPARQL README](https://github.com/ckosten/Spider4SPARQL))
and extract the `.ttl` files into `materialized_triples/`. For example, with
[gdown](https://github.com/wkentaro/gdown):

```bash
.venv/bin/pip install gdown
.venv/bin/gdown 1S6xaI0VfnFMrsuyjxv2uQPok_CikhLLx -O dev_kgs.zip
unzip dev_kgs.zip -d materialized_triples/   # adjust if the archive nests folders
```

Then fetch the query pairs and ValueNet/MPBoot ontologies and verify the graphs:

```bash
.venv/bin/python prepare_dataset.py
```

This downloads `queries/dev_nl_sparql.csv` and `ontologies/<kg>.owl` from the
official repository, and excludes two KGs whose materialized files are broken
in the official distribution (`wta_1.ttl` is truncated mid-triple,
`orchestra.ttl` is empty), leaving 932 queries over 18 knowledge graphs.

## Step 2 — Build the enriched benchmark (intermediary artifacts)

The MPBoot ontologies are flat direct mappings with no class hierarchy, so
subsumption pruning has no natural targets. The enrichment script applies a
controlled redundancy-injection protocol:

```bash
.venv/bin/python build_enriched_benchmark.py
```

This generates (all git-ignored, fully reproducible):

- `ontologies_enriched/<kg>.ttl` — official ontology + root class
  `:spider_entity` and `C rdfs:subClassOf :spider_entity` axioms,
- `materialized_triples_enriched/<kg>.ttl` — ABox + materialized root-class
  memberships (so baseline and optimized queries stay result-equivalent),
- `queries/dev_nl_sparql_enriched.csv` — queries with a redundant
  `?v a :spider_entity .` constraint injected per variable per group.

## Step 3 — Run the benchmarks

Natural-redundancy baseline (duplicate pruning only):

```bash
.venv/bin/python benchmark.py \
    --input queries/dev_nl_sparql.csv \
    --db-path materialized_triples \
    --engine oxigraph --timeout 10 \
    --output results_plain.csv
```

Subsumption treatment (enriched TBox + injected redundancy):

```bash
.venv/bin/python benchmark.py \
    --input queries/dev_nl_sparql_enriched.csv \
    --db-path materialized_triples_enriched \
    --tbox-path ontologies_enriched \
    --engine oxigraph --timeout 10 \
    --output results_enriched.csv
```

Useful flags:

- `--engine {rdflib,oxigraph}` — execution engine. `oxigraph` (Rust) runs the
  full suite in minutes; `rdflib` executes BGPs in written order and hangs on
  several queries (which is what the timeout machinery is for). The
  optimizer's TBox traversal always uses rdflib.
- `--timeout SECONDS` — hard per-query wall-clock timeout (default 10).
  Queries run in a child process that is killed on timeout (status 408).
- `--limit N --offset K` — run a subset, e.g. `--offset 99 --limit 1` to
  reproduce a single row.
- `--tbox-path DIR` — per-KG TBox files (`<kg>.ttl`/`<kg>.owl`); without it
  the domain graph itself is used as the schema.

The output CSV contains, per query: baseline/optimized SPARQL, pruning counts
(duplicates vs. subsumption), optimizer overhead, per-side status
(200 ok / 408 timeout / 500 error), execution times, result counts, and a
result-equivalence flag.

## Implementation notes for reproducers

- **ValueNet prefix quirk**: entities/properties use the
  `http://valuenet/ontop/` prefix, injected into every query.
- **rdflib escaping pitfall**: Spider4SPARQL queries escape `#` in prefixed
  local names (`:singer\#age`). rdflib's SPARQL parser does not unescape
  `PN_LOCAL_ESC`, producing URIs with a literal backslash that match nothing
  — such queries silently return 0 rows. `benchmark.py` therefore expands all
  escape-containing prefixed names to full IRIs before execution (a semantic
  no-op for spec-compliant engines such as pyoxigraph).
- Timings are hardware-dependent and single-thread-bound; compare statuses,
  counts, and ratios across machines, not absolute milliseconds.

## References

- C. Kosten, P. Cudre-Mauroux, K. Stockinger. *Spider4SPARQL: A Complex
  Benchmark for Evaluating Knowledge Graph Question Answering Systems.*
  IEEE BigData 2023. doi:10.1109/BigData59044.2023.10386182
  ([repository](https://github.com/ckosten/Spider4SPARQL))
- Spider4SPARQL knowledge graphs were materialized with the
  [Ontop](https://ontop-vkg.org/) VKG framework from the ValueNet/Spider
  databases; the per-database ontologies and mappings were generated with
  MPBoot (see `knowledge_graph_construction/` in the Spider4SPARQL repo).
