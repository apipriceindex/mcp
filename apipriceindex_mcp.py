#!/usr/bin/env python3
"""
apipriceindex-mcp — serveur MCP de l'API Price Index.

Expose l'index ouvert (apipriceindex.com, CC BY 4.0) à tout agent compatible
MCP via 3 outils : chercher un modèle, obtenir son prix vérifié (avec source
et date), estimer la facture mensuelle d'un workload sur tout le catalogue.

Règles maison :
- La donnée est lue en direct sur /api/all.json (cache mémoire 15 min).
- Index injoignable → erreur explicite, JAMAIS de dernière valeur servie
  en silence.
- Index périmé (as_of > 14 j) → on répond quand même, avec avertissement.
- Pas de prix caché publié → aucune remise inventée (décision partagée avec
  le calculateur du site et llm-cost-diff).
- Chaque réponse porte l'attribution CC BY et l'as_of de l'index.

Une seule dépendance : le SDK `mcp`. La logique métier est dans des fonctions
pures (_search/_price/_estimate) testables sans le SDK.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

DATA_URL = os.environ.get("APIPRICEINDEX_URL", "https://apipriceindex.com/api/all.json")
CACHE_TTL_S = 15 * 60
STALE_DAYS = 14
SEARCH_LIMIT = 20
RANK_LIMIT = 25
ATTRIBUTION = "API Price Index (https://apipriceindex.com) — CC BY 4.0"

_cache: dict = {"at": 0.0, "index": None}


class IndexUnavailable(RuntimeError):
    pass


def _fetch_index() -> dict:
    """Index avec cache 15 min. Lève IndexUnavailable si la source est morte —
    le cache expiré n'est pas resservi : mieux vaut une erreur qu'un prix faux."""
    now = time.monotonic()
    if _cache["index"] is not None and now - _cache["at"] < CACHE_TTL_S:
        return _cache["index"]
    req = urllib.request.Request(DATA_URL, headers={"User-Agent": "apipriceindex-mcp/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            index = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # réseau, HTTP, JSON : même verdict — indisponible
        raise IndexUnavailable(
            f"Price index unreachable ({DATA_URL}): {e}. "
            "No cached data served — check https://apipriceindex.com/health.json"
        ) from e
    _cache.update(at=now, index=index)
    return index


def _meta(index: dict) -> dict:
    """Bloc commun à toutes les réponses : attribution + fraîcheur + alerte éventuelle."""
    meta = {"attribution": ATTRIBUTION, "index_as_of": index.get("as_of")}
    try:
        dt = datetime.strptime(index.get("as_of") or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_d = (datetime.now(timezone.utc) - dt).days
        if age_d > STALE_DAYS:
            meta["warning"] = (
                f"Index is {age_d} days old — prices may be outdated. "
                "Check https://apipriceindex.com/health.json"
            )
    except ValueError:
        pass
    return meta


def _brief(m: dict) -> dict:
    p = m.get("pricing") or {}
    return {
        "id": m["id"],
        "name": m.get("name"),
        "provider": (m.get("provider") or {}).get("name"),
        "context_window": m.get("context_window"),
        "input_usd_per_mtok": p.get("input_per_mtok"),
        "output_usd_per_mtok": p.get("output_per_mtok"),
        "open_weights": m.get("open_weights"),
    }


def _search(query: str, provider: str | None = None, min_context: int | None = None,
            open_weights: bool | None = None) -> dict:
    index = _fetch_index()
    q = query.strip().lower()
    hits = []
    for m in index.get("models", []):
        hay = " ".join(str(x).lower() for x in (
            m.get("id"), m.get("name"), m.get("base_model"),
            (m.get("provider") or {}).get("name")) if x)
        if q and q not in hay:
            continue
        if provider and provider.strip().lower() != (m.get("provider") or {}).get("id", "").lower():
            continue
        if min_context is not None and (m.get("context_window") or 0) < min_context:
            continue
        if open_weights is not None and bool(m.get("open_weights")) != open_weights:
            continue
        hits.append(m)
    # Modèles avec prix vérifié d'abord : c'est eux qu'on peut vraiment servir.
    hits.sort(key=lambda m: (m.get("pricing") is None, m["id"]))
    return {
        "matches": [_brief(m) for m in hits[:SEARCH_LIMIT]],
        "total_matches": len(hits),
        "note": "Use the `id` field with get_price or estimate_cost.",
        **_meta(index),
    }


def _price(model_id: str) -> dict:
    index = _fetch_index()
    m = next((x for x in index.get("models", []) if x.get("id") == model_id), None)
    if m is None:
        return {
            "error": f"'{model_id}' is not tracked by the index. No price invented.",
            "hint": "Find valid ids with search_models, or browse "
                    "https://apipriceindex.com/use-this-data/",
            **_meta(index),
        }
    p = m.get("pricing")
    if not p:
        return {
            "error": f"'{model_id}' is tracked but has no verified price yet.",
            "page": m.get("page"),
            **_meta(index),
        }
    return {
        "id": m["id"],
        "name": m.get("name"),
        "provider": (m.get("provider") or {}).get("name"),
        "pricing": p,  # input/output/cached_input per Mtok, currency, confidence, verified_at, source_url
        "context_window": m.get("context_window"),
        "max_output": m.get("max_output"),
        "license": m.get("license"),
        "open_weights": m.get("open_weights"),
        "page": m.get("page"),
        **_meta(index),
    }


def _monthly_cost(p: dict, input_mtok: float, output_mtok: float, cache_pct: float) -> tuple[float, bool]:
    """Même arithmétique que le calculateur du site et cost_diff.py.
    Renvoie (coût mensuel USD, remise cache réellement appliquée ?)."""
    cached = p.get("cached_input_per_mtok")
    c = min(100.0, max(0.0, cache_pct)) / 100.0
    if cached is None:
        c = 0.0  # pas de prix caché publié → aucune remise inventée
    cost = (input_mtok * (1 - c) * p["input_per_mtok"]
            + input_mtok * c * (cached if cached is not None else p["input_per_mtok"])
            + output_mtok * p["output_per_mtok"])
    return cost, c > 0


def _estimate(input_mtok: float, output_mtok: float, cache_pct: float = 0.0,
              models: list[str] | None = None) -> dict:
    index = _fetch_index()
    by_id = {m["id"]: m for m in index.get("models", [])}
    if models:
        unknown = [i for i in models if i not in by_id]
        if unknown:
            return {
                "error": f"Not tracked by the index: {', '.join(unknown)}. No price invented.",
                "hint": "Find valid ids with search_models.",
                **_meta(index),
            }
        pool = [by_id[i] for i in models]
    else:
        pool = list(by_id.values())
    lines = []
    for m in pool:
        p = m.get("pricing") or {}
        if p.get("input_per_mtok") is None or p.get("output_per_mtok") is None:
            continue  # catalogue entier : on saute les non-pricés au lieu d'échouer
        cost, cache_applied = _monthly_cost(p, input_mtok, output_mtok, cache_pct)
        lines.append({
            "id": m["id"],
            "name": m.get("name"),
            "provider": (m.get("provider") or {}).get("name"),
            "monthly_usd": round(cost, 2),
            "cache_discount_applied": cache_applied,
            "price_verified_at": p.get("verified_at"),
            "page": m.get("page"),
        })
    lines.sort(key=lambda l: l["monthly_usd"])
    truncated = models is None and len(lines) > RANK_LIMIT
    return {
        "workload": {"input_mtok": input_mtok, "output_mtok": output_mtok, "cache_pct": cache_pct},
        "estimates": lines[:RANK_LIMIT] if truncated else lines,
        "total_priced_models": len(lines),
        "note": "Cost ranking only — this index does not judge model quality. "
                "Cache discount applied only where the provider publishes a cached-input price.",
        **_meta(index),
    }


def build_server():
    """Construit le serveur MCP (import différé : la logique reste testable sans SDK).
    SDK mcp >= 2 (MCPServer, ex-FastMCP)."""
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("apipriceindex")

    @mcp.tool()
    def search_models(query: str, provider: str | None = None, min_context: int | None = None,
                      open_weights: bool | None = None) -> dict:
        """Search the API Price Index for LLM API endpoints by name, id or provider.

        Returns matching models with their `id` (use it with get_price /
        estimate_cost), provider, context window and USD prices per million
        tokens. Optional filters: provider slug (e.g. 'openai'), minimum
        context window, open-weights only.
        """
        return _search(query, provider, min_context, open_weights)

    @mcp.tool()
    def get_price(model_id: str) -> dict:
        """Get the verified price of one model (USD per million tokens).

        Returns input/output/cached-input prices with verification date,
        cross-check confidence and source URL. Prices are re-checked daily
        against official pricing pages. Data: apipriceindex.com, CC BY 4.0.
        """
        return _price(model_id)

    @mcp.tool()
    def estimate_cost(input_mtok: float, output_mtok: float, cache_pct: float = 0,
                      models: list[str] | None = None) -> dict:
        """Estimate the monthly USD bill of a workload and rank models by cost.

        Give monthly volumes in millions of tokens (input and output) and an
        optional share of input served from cache (0-100). Ranks the given
        model ids — or the whole catalog — from cheapest to most expensive.
        Cost only: this tool does not rank model quality.
        """
        return _estimate(input_mtok, output_mtok, cache_pct, models)

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
