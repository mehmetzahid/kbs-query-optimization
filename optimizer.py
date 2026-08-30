"""Subsumption-based static optimizer for SPARQL BGPs.

Type constraints are collected from the algebra tree (rdflib SPARQL parser),
redundancy is decided against a precomputed rdfs:subClassOf closure, and the
query string is rewritten by deleting exact source spans. If the rewritten
query fails to re-parse we fall back to the original.
"""

import re

import rdflib
from rdflib import URIRef, Variable
from rdflib.namespace import RDF, RDFS
from rdflib.plugins.sparql import algebra as sparql_algebra
from rdflib.plugins.sparql import parser as sparql_parser
from rdflib.plugins.sparql.parserutils import CompValue

RDF_TYPE = str(RDF.type)
PREFIX_DECL_RE = re.compile(r'PREFIX\s+([A-Za-z][\w.-]*|)\s*:\s*<([^>]*)>', re.IGNORECASE)
UNESCAPE_RE = re.compile(r'\\(.)')


class SubsumptionOptimizer:

    def __init__(self, schema_graph):
        self.schema = schema_graph
        self.superclasses = self._build_closure(schema_graph)

    @staticmethod
    def _build_closure(graph):
        direct = {}
        for s, _, o in graph.triples((None, RDFS.subClassOf, None)):
            if isinstance(s, URIRef) and isinstance(o, URIRef) and s != o:
                direct.setdefault(str(s), set()).add(str(o))
        closure = {}
        for cls, parents in direct.items():
            seen, stack = set(), list(parents)
            while stack:
                cur = stack.pop()
                if cur not in seen and cur != cls:
                    seen.add(cur)
                    stack.extend(direct.get(cur, ()))
            closure[cls] = seen
        return closure

    def is_subclass_of(self, child_uri, parent_uri):
        child, parent = str(child_uri), str(parent_uri)
        return child == parent or parent in self.superclasses.get(child, ())

    def _redundant_classes(self, classes):
        """Which classes constraining the same variable can be dropped.
        A strict superclass of another class in the set is redundant; for
        equivalence cycles keep the lexicographically smallest."""
        redundant = set()
        for c in classes:
            for other in classes:
                if c == other:
                    continue
                c_up = c in self.superclasses.get(other, ())
                other_up = other in self.superclasses.get(c, ())
                if c_up and not other_up:
                    redundant.add(c)
                elif c_up and other_up and other < c:
                    redundant.add(c)
        if redundant >= set(classes):
            redundant.discard(min(classes))  # never drop the last constraint
        return redundant

    @staticmethod
    def _bgp_type_constraints(parse_tree):
        """Per-BGP {var: {class_uri}} maps for rdf:type triples with a
        variable subject and constant class, taken from the algebra tree."""
        query = sparql_algebra.translateQuery(parse_tree)
        bgps = []

        def visit(node):
            if isinstance(node, CompValue) and node.name == "BGP":
                per_var = {}
                for s, p, o in node.get("triples") or []:
                    if p == RDF.type and isinstance(s, Variable) and isinstance(o, URIRef):
                        per_var.setdefault(str(s), set()).add(str(o))
                if per_var:
                    bgps.append(per_var)

        sparql_algebra.traverse(query.algebra, visitPre=visit)
        return bgps

    def _redundant_pairs(self, parse_tree):
        pairs = set()
        for per_var in self._bgp_type_constraints(parse_tree):
            for var, classes in per_var.items():
                for cls in self._redundant_classes(classes):
                    pairs.add((var, cls))
        return pairs

    @staticmethod
    def _scan_statements(q):
        """Split a query into statements tagged with the id of the enclosing
        {} group, keeping exact source spans so statements can be deleted
        in place. Dots inside IRIs, literals, parens and escaped local names
        (:singer\\#age) are not treated as separators."""
        statements = []
        group_stack = [0]
        next_group = 1
        paren_depth = 0
        in_iri = in_squote = in_dquote = False
        start = 0
        i, n = 0, len(q)

        def flush(end_text, end_span):
            text = q[start:end_text].strip()
            if text and group_stack[-1] != 0:
                statements.append(
                    {"start": start, "end": end_span,
                     "group": group_stack[-1], "text": text})

        while i < n:
            c = q[i]
            if in_iri:
                if c == '>':
                    in_iri = False
            elif in_squote:
                if c == '\\':
                    i += 1
                elif c == "'":
                    in_squote = False
            elif in_dquote:
                if c == '\\':
                    i += 1
                elif c == '"':
                    in_dquote = False
            elif c == '\\':
                i += 1
            elif c == '<':
                in_iri = True
            elif c == "'":
                in_squote = True
            elif c == '"':
                in_dquote = True
            elif c == '#':
                while i < n and q[i] != '\n':
                    i += 1
            elif c == '(':
                paren_depth += 1
            elif c == ')':
                paren_depth = max(0, paren_depth - 1)
            elif paren_depth == 0:
                if c == '{':
                    # pending text is structural (where/OPTIONAL/sub-select)
                    group_stack.append(next_group)
                    next_group += 1
                    start = i + 1
                elif c == '}':
                    flush(i, i)
                    if len(group_stack) > 1:
                        group_stack.pop()
                    start = i + 1
                elif c == '.' and (i + 1 >= n or q[i + 1] in ' \t\r\n}'):
                    flush(i, i + 1)
                    start = i + 1
            i += 1
        return statements

    @staticmethod
    def _expand_token(token, prefixes, as_predicate=False):
        if as_predicate and token == 'a':
            return RDF_TYPE
        if token.startswith('<') and token.endswith('>'):
            return token[1:-1]
        match = re.match(r'^([A-Za-z][\w.-]*|):(.+)$', token)
        if not match:
            return None
        prefix, local = match.groups()
        if prefix not in prefixes:
            return None
        return prefixes[prefix] + UNESCAPE_RE.sub(r'\1', local)

    def _as_type_statement(self, text, prefixes):
        tokens = text.split()
        if len(tokens) != 3 or not tokens[0].startswith('?'):
            return None
        if self._expand_token(tokens[1], prefixes, as_predicate=True) != RDF_TYPE:
            return None
        cls = self._expand_token(tokens[2], prefixes)
        if cls is None:
            return None
        return tokens[0][1:], cls

    def optimize_sparql(self, sparql_query):
        """Returns (optimized_query, triples_removed, stats)."""
        stats = {"duplicates_removed": 0, "subsumption_removed": 0, "fallback": None}

        try:
            parse_tree = sparql_parser.parseQuery(sparql_query)
        except Exception as e:
            stats["fallback"] = f"input parse failed: {e}"[:150]
            return sparql_query, 0, stats

        try:
            algebra_pairs = self._redundant_pairs(parse_tree)
        except Exception:
            algebra_pairs = None

        prefixes = dict(PREFIX_DECL_RE.findall(sparql_query))
        statements = self._scan_statements(sparql_query)

        to_remove = []

        # duplicated statements within the same group are no-ops under BGP
        # set semantics
        seen, kept = set(), []
        for stmt in statements:
            key = (stmt["group"], " ".join(stmt["text"].split()))
            if key in seen:
                to_remove.append(stmt)
                stats["duplicates_removed"] += 1
            else:
                seen.add(key)
                kept.append(stmt)

        by_group_var = {}
        for stmt in kept:
            hit = self._as_type_statement(stmt["text"], prefixes)
            if hit:
                var, cls = hit
                stmt["var"], stmt["cls"] = var, cls
                by_group_var.setdefault((stmt["group"], var), []).append(stmt)

        for (_, var), stmts in by_group_var.items():
            classes = {s["cls"] for s in stmts}
            if len(classes) < 2:
                continue
            redundant = self._redundant_classes(classes)
            for s in stmts:
                if s["cls"] in redundant and (
                        algebra_pairs is None or (var, s["cls"]) in algebra_pairs):
                    to_remove.append(s)
                    stats["subsumption_removed"] += 1

        if not to_remove:
            return sparql_query, 0, stats

        pieces, pos = [], 0
        for stmt in sorted(to_remove, key=lambda s: s["start"]):
            pieces.append(sparql_query[pos:stmt["start"]])
            pos = stmt["end"]
        pieces.append(sparql_query[pos:])
        optimized = "".join(pieces)

        try:
            sparql_parser.parseQuery(optimized)
        except Exception as e:
            stats["fallback"] = f"rewrite parse failed: {e}"[:150]
            stats["duplicates_removed"] = stats["subsumption_removed"] = 0
            return sparql_query, 0, stats

        removed = stats["duplicates_removed"] + stats["subsumption_removed"]
        return optimized, removed, stats
