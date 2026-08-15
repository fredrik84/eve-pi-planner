#!/usr/bin/env python3
"""The planet database is GLOBAL, so wiping it is not an ordinary user's button.

On 2026-08-15 production's `pp_planets` was found with **0 rows** — every planet, for every user,
gone. `DELETE /api/planets` was gated on `require_context` (any logged-in player) and the "Clear
all" button sat on the Planet DB page next to Import. Nothing about either said the table was
shared: it reads exactly like the per-account clears elsewhere in the same file, which is the trap.
One confirm dialog stood between any user and the reference data the whole PI planner runs on.

What is pinned:

  * the destructive endpoint requires a SITE ADMIN, not merely a session;
  * IMPORT stays open to everyone — the merge path never deletes, and locking contributions down
    would be a different (and wrong) change;
  * the button is hidden from non-admins, and the CSS that hides it is real;
  * every other global-table writer in planetary.py is checked the same way, so the next one added
    cannot quietly repeat this.

Structural where it must be — a live test would have to actually wipe a database to prove the
negative, which is not a thing to run against anything that matters.

    docker compose cp test_planetdb_guard.py web:/srv/app/ && \
      docker compose exec web python3 test_planetdb_guard.py
"""
import ast
import os
import re
import sys

sys.path.insert(0, ".")

HERE = os.path.dirname(os.path.abspath(__file__))
_fails = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def _route_deps(tree, path_pred):
    """{route path: [dependency names]} for every decorated handler in the module."""
    out = {}
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for dec in fn.decorator_list:
            if not isinstance(dec, ast.Call) or not dec.args:
                continue
            meth = getattr(dec.func, "attr", "")
            route = dec.args[0].value if isinstance(dec.args[0], ast.Constant) else ""
            if not isinstance(route, str) or not path_pred(meth, route):
                continue
            deps = []
            for arg in list(fn.args.args) + list(fn.args.kwonlyargs):
                pass
            for d in fn.args.defaults + [k for k in fn.args.kw_defaults if k is not None]:
                if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "Depends" and d.args:
                    deps.append(getattr(d.args[0], "id", ""))
            out[(meth, route)] = deps
    return out


def main() -> int:
    src = open(os.path.join(HERE, "app", "planetary.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    print("\nwiping the shared planet database takes a site admin:")
    routes = _route_deps(tree, lambda m, r: True)
    clear = routes.get(("delete", "/api/planets"))
    check(clear is not None, "DELETE /api/planets still exists")
    if clear is not None:
        check("require_admin" in clear,
              f"...and depends on require_admin (got {clear})")
        check("require_context" not in clear,
              "...and NOT on require_context, which is merely 'is logged in'")

    print("\ncontributing planets is still open to any logged-in player:")
    imp = routes.get(("post", "/api/planets/import"))
    check(imp is not None, "POST /api/planets/import still exists")
    if imp is not None:
        check("require_admin" not in imp,
              f"import is not admin-gated — the merge path never deletes (got {imp})")

    print("\nthe merge path really is non-destructive:")
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_write_planet_rows"), None)
    check(fn is not None, "_write_planet_rows exists")
    if fn is not None:
        body = "\n".join(src.splitlines()[fn.lineno - 1:(fn.end_lineno or fn.lineno)])
        check("DELETE" not in body.upper(),
              "an import cannot delete a row, so a bad paste cannot empty the table")
        check("ON CONFLICT" in body.upper(), "...it upserts")

    print("\nEVERY destructive route on a global table is admin-gated (source scan):")
    # The specific fix above is worth little on its own — the lesson is the shape, so the shape is
    # what gets checked. A global table is one with no context_id/character_id scoping.
    GLOBAL_TABLES = ("pp_planets", "pp_planet_yield_avg")
    offenders = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        body = "\n".join(src.splitlines()[fn.lineno - 1:(fn.end_lineno or fn.lineno)])
        deletes = [t for t in GLOBAL_TABLES
                   if re.search(r"DELETE\s+FROM\s+" + t + r"\b", body, re.I)]
        if not deletes:
            continue
        # Scoped by the caller's own identity is a different, safe thing.
        if re.search(r"DELETE\s+FROM\s+\w+[^;]*?WHERE[^;]*?(context_id|character_id)", body, re.I):
            continue
        if "require_admin" not in body:
            offenders.append(f"{fn.name} deletes {deletes} unscoped without require_admin")
    check(not offenders, f"no unscoped global delete is reachable without admin: {offenders}")

    print("\nthe button is hidden from the people who must not press it:")
    html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    m = re.search(r'<button[^>]*onclick="clearPlanets\(\)"[^>]*>', html)
    check(bool(m), "the Clear all button is still in the markup")
    if m:
        check("pp-admin-only" in m.group(0),
              f"...and carries the admin-only class (got {m.group(0)[:90]})")
    css = open(os.path.join(HERE, "static", "style-layout-admin.css"), encoding="utf-8").read()
    check(re.search(r"\.pp-admin-only\s*\{[^}]*display:\s*none", css),
          "the class actually hides by default — a class with no rule hides nothing")
    check(re.search(r"html\.nav-adm\s+\.pp-admin-only\s*\{[^}]*display:", css),
          "...and is restored for an admin")

    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
