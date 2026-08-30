import argparse
import csv
import logging
import multiprocessing as mp
import os
import queue
import re
import time

import rdflib

logging.getLogger("rdflib.term").setLevel(logging.ERROR)

from optimizer import SubsumptionOptimizer

PREFIX_HEADER = (
    "PREFIX : <http://valuenet/ontop/>\n"
    "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
)

LOAD_TIMEOUT_S = 180.0

# rdflib does not unescape PN_LOCAL_ESC in prefixed names, so :singer\#age
# becomes a URI with a literal backslash and matches nothing. Expand any
# escape-containing prefixed name to a full IRI up front (no-op for engines
# that handle the escape correctly, e.g. oxigraph).
ESCAPED_PNAME_RE = re.compile(r':([\w.-]*(?:\\.[\w.-]*)+)')


def expand_escaped_pnames(query, base="http://valuenet/ontop/"):
    def repl(match):
        local = re.sub(r'\\(.)', r'\1', match.group(1))
        return f"<{base}{local}>"
    return ESCAPED_PNAME_RE.sub(repl, query)


def rdf_format(db_path):
    return "turtle" if db_path.endswith(".ttl") else "nt"


def load_oxigraph_store(db_path):
    import pyoxigraph
    store = pyoxigraph.Store()
    try:
        fmt = (pyoxigraph.RdfFormat.TURTLE if db_path.endswith(".ttl")
               else pyoxigraph.RdfFormat.N_TRIPLES)
        store.load(path=db_path, format=fmt)
    except (AttributeError, TypeError):  # pyoxigraph < 0.4
        mime = "text/turtle" if db_path.endswith(".ttl") else "application/n-triples"
        with open(db_path, "rb") as f:
            store.load(f, mime)
    return store


def count_results(results):
    try:
        return sum(1 for _ in results)
    except TypeError:  # ASK
        return int(bool(results))


def query_worker(engine, db_path, task_q, result_q):
    """Child process: owns the graph, executes queries until poisoned."""
    try:
        if engine == "oxigraph":
            target = load_oxigraph_store(db_path)
        else:
            target = rdflib.Graph()
            target.parse(db_path, format=rdf_format(db_path))
    except Exception as e:
        result_q.put(("load_error", repr(e)[:300]))
        return
    result_q.put(("ready", None))

    while True:
        q = task_q.get()
        if q is None:
            return
        try:
            start = time.perf_counter()
            n = count_results(target.query(q))
            result_q.put(("result", ((time.perf_counter() - start) * 1000.0, 200, n, "")))
        except Exception as e:
            result_q.put(("result", (0.0, 500, 0, repr(e)[:300])))


class QueryRunner:
    """Runs queries in a child process so a hung query can be killed.
    rdflib gives no way to interrupt Graph.query() in-process, hence the
    process boundary. On timeout the worker is terminated and respawned
    (which reloads the graph) before the next query."""

    def __init__(self, engine, timeout_s):
        self.engine = engine
        self.timeout_s = timeout_s
        self.ctx = mp.get_context("spawn")
        self.proc = None
        self.task_q = None
        self.result_q = None
        self.db_path = None
        self.bad_dbs = {}

    def _start(self, db_path):
        self.task_q = self.ctx.Queue()
        self.result_q = self.ctx.Queue()
        self.proc = self.ctx.Process(
            target=query_worker,
            args=(self.engine, db_path, self.task_q, self.result_q),
            daemon=True,
        )
        self.proc.start()
        try:
            tag, payload = self.result_q.get(timeout=LOAD_TIMEOUT_S)
        except queue.Empty:
            self.shutdown(kill=True)
            raise RuntimeError("graph load timed out in worker")
        if tag != "ready":
            self.shutdown(kill=True)
            raise RuntimeError(f"graph load failed in worker: {payload}")
        self.db_path = db_path

    def execute(self, db_path, query):
        """(qet_ms, status, result_count, note) with status 200/408/500."""
        if db_path in self.bad_dbs:
            return 0.0, 500, 0, self.bad_dbs[db_path]
        if self.proc is None or not self.proc.is_alive() or db_path != self.db_path:
            self.shutdown(kill=True)
            try:
                self._start(db_path)
            except RuntimeError as e:
                self.bad_dbs[db_path] = str(e)
                return 0.0, 500, 0, str(e)

        self.task_q.put(query)
        try:
            tag, payload = self.result_q.get(timeout=self.timeout_s)
            return payload
        except queue.Empty:
            was_alive = self.proc.is_alive()
            self.shutdown(kill=True)
            if was_alive:
                return self.timeout_s * 1000.0, 408, 0, "timeout"
            return 0.0, 500, 0, "worker crashed"

    def shutdown(self, kill=False):
        if self.proc is not None and self.proc.is_alive():
            if kill:
                self.proc.terminate()
            else:
                try:
                    self.task_q.put(None)
                except Exception:
                    pass
            self.proc.join(timeout=2)
            if self.proc.is_alive():
                self.proc.kill()
                self.proc.join(timeout=2)
        self.proc = None
        self.db_path = None


def resolve_db_path(db_base_path, db_id):
    candidates = [
        os.path.join(db_base_path, f"{db_id}.ttl"),
        os.path.join(db_base_path, f"{db_id}.nt"),
        os.path.join(db_base_path, db_id, f"{db_id}.ttl"),
        os.path.join(db_base_path, db_id, f"{db_id}.nt"),
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


class OptimizerCache:
    """One optimizer per database. The schema comes from the TBox directory
    (<kg>.ttl or <kg>.owl) when given, otherwise from the domain graph.
    Schema parsing always goes through rdflib, independent of the engine."""

    def __init__(self, tbox_dir=None):
        self.tbox_dir = tbox_dir
        self.cache = {}

    def _tbox_file(self, db_id):
        if not self.tbox_dir:
            return None
        for name, fmt in ((f"{db_id}.ttl", "turtle"), (f"{db_id}.owl", "xml")):
            path = os.path.join(self.tbox_dir, name)
            if os.path.exists(path):
                return path, fmt
        return None

    def get(self, db_path, db_id):
        if db_path not in self.cache:
            schema = rdflib.Graph()
            tbox = self._tbox_file(db_id)
            if tbox:
                schema.parse(tbox[0], format=tbox[1])
            else:
                schema.parse(db_path, format=rdf_format(db_path))
            self.cache[db_path] = SubsumptionOptimizer(schema)
        return self.cache[db_path]


def run_benchmark(input_file, output_file, db_base_path, limit=None, offset=0,
                  timeout_s=10.0, engine="rdflib", tbox_path=None):
    input_file = os.path.expanduser(input_file)
    db_base_path = os.path.expanduser(db_base_path)

    print(f"Benchmark on {input_file}")
    print(f"engine={engine} timeout={timeout_s}s "
          f"rows={offset}..{offset + limit - 1 if limit else 'end'}\n")

    with open(input_file, mode='r', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("input CSV has no data rows, aborting")
        return

    print(f"{len(rows)} queries loaded")

    end = offset + limit if limit else len(rows)
    selected = list(enumerate(rows))[offset:end]

    runner = QueryRunner(engine, timeout_s)
    optimizers = OptimizerCache(tbox_dir=os.path.expanduser(tbox_path) if tbox_path else None)

    results_log = []
    missing_dbs = corrupted_dbs = 0
    timeouts_base = timeouts_opt = 0

    try:
        for idx, row in selected:
            db_id = row.get("kg_name", "").strip()
            raw_query = row.get("query", "").strip()
            if not db_id or not raw_query:
                continue

            gold_query = expand_escaped_pnames(PREFIX_HEADER + raw_query)

            db_path = resolve_db_path(db_base_path, db_id)
            if not db_path:
                missing_dbs += 1
                continue

            try:
                optimizer = optimizers.get(db_path, db_id)
            except Exception:
                corrupted_dbs += 1
                continue

            opt_start = time.perf_counter()
            optimized_query, triples_removed, opt_stats = optimizer.optimize_sparql(gold_query)
            opt_overhead_ms = (time.perf_counter() - opt_start) * 1000.0

            base_qet, base_status, base_count, base_note = runner.execute(db_path, gold_query)

            if triples_removed == 0:
                # unchanged query, re-running would only measure noise
                opt_qet, opt_status, opt_count, opt_note = base_qet, base_status, base_count, base_note
            else:
                opt_qet, opt_status, opt_count, opt_note = runner.execute(db_path, optimized_query)

            timeouts_base += base_status == 408
            timeouts_opt += opt_status == 408

            both_ok = base_status == 200 and opt_status == 200
            results_log.append({
                "Row_Idx": idx,
                "Domain_DB": db_id,
                "Baseline_SPARQL": gold_query.replace('\n', ' '),
                "Optimized_SPARQL": optimized_query.replace('\n', ' '),
                "Triples_Pruned": triples_removed,
                "Duplicates_Pruned": opt_stats["duplicates_removed"],
                "Subsumption_Pruned": opt_stats["subsumption_removed"],
                "Optimizer_Overhead_ms": round(opt_overhead_ms, 3),
                "Baseline_Status": base_status,
                "Optimized_Status": opt_status,
                "Baseline_QET_ms": round(base_qet, 2),
                "Optimized_QET_ms": round(opt_qet, 2),
                "Execution_Time_Saved_ms": round(base_qet - opt_qet, 2) if both_ok or base_status == 408 else "",
                "Baseline_Result_Count": base_count,
                "Optimized_Result_Count": opt_count,
                "Result_Equivalence_Preserved": base_count == opt_count if both_ok else "",
                "Note": base_note or opt_note or (opt_stats["fallback"] or ""),
            })

            flag = ""
            if base_status == 408:
                flag = " [BASELINE TIMEOUT]"
            elif base_status == 500:
                flag = " [ERROR]"
            print(f"[{idx}] {db_id}: base={base_status} ({base_qet:.0f}ms) "
                  f"opt={opt_status} ({opt_qet:.0f}ms) pruned={triples_removed}{flag}",
                  flush=True)
    finally:
        runner.shutdown(kill=True)

    print("\n--- Summary ---")
    print(f"processed: {len(results_log)}")
    print(f"baseline timeouts: {timeouts_base}")
    print(f"optimized timeouts: {timeouts_opt}")
    if missing_dbs:
        print(f"skipped, missing db files: {missing_dbs}")
    if corrupted_dbs:
        print(f"skipped, unparseable db files: {corrupted_dbs}")

    if results_log:
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(results_log[0].keys()))
            writer.writeheader()
            writer.writerows(results_log)
        print(f"\nresults written to {output_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Subsumption optimizer benchmark")
    parser.add_argument("--input", type=str, required=True, help="path to the NL-SPARQL csv")
    parser.add_argument("--output", type=str, required=True, help="path to output csv")
    parser.add_argument("--db-path", type=str, required=True, help="directory with the materialized graphs")
    parser.add_argument("--limit", type=int, default=None, help="only run the first N selected rows")
    parser.add_argument("--offset", type=int, default=0, help="skip the first N rows")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-query timeout in seconds")
    parser.add_argument("--engine", choices=["rdflib", "oxigraph"], default="rdflib",
                        help="execution engine (schema traversal always uses rdflib)")
    parser.add_argument("--tbox-path", type=str, default=None,
                        help="directory with per-KG TBox files (<kg>.ttl or <kg>.owl); "
                             "defaults to using the domain graph as schema")

    args = parser.parse_args()
    run_benchmark(args.input, args.output, args.db_path,
                  limit=args.limit, offset=args.offset,
                  timeout_s=args.timeout, engine=args.engine,
                  tbox_path=args.tbox_path)
