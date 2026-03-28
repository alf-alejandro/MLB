"""
Polymarket — Partidos MLB del día
- Gamma API: eventos MLB via tag_id=100381
- CLOB API:  precios en paralelo (igual que NBA-POLY)
- Fallback:  outcomePrices del Gamma si CLOB no responde
"""

import requests
import json
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

GAMMA_API  = "https://gamma-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"
MLB_TAG_ID = 100381
HEADERS    = {"User-Agent": "Mozilla/5.0"}
SESSION    = requests.Session()
SESSION.headers.update(HEADERS)


# ── Gamma: partidos del día ───────────────────────────────────────────────────

def obtener_partidos_hoy() -> list[dict]:
    hoy = date.today().strftime("%Y-%m-%d")

    resp = SESSION.get(
        f"{GAMMA_API}/events",
        params={
            "tag_id":    MLB_TAG_ID,
            "active":    "true",
            "closed":    "false",
            "limit":     100,
            "order":     "startDate",
            "ascending": "true",
        }, timeout=15
    )
    resp.raise_for_status()
    todos = resp.json()

    # Filtrar solo juegos individuales moneyline de hoy
    partidos = []
    for evento in todos:
        for m in evento.get("markets", []):
            if m.get("sportsMarketType") != "moneyline":
                continue
            if m.get("closed") or not m.get("active", True):
                continue
            gt = m.get("gameStartTime", "")
            if not gt or hoy not in gt:
                continue

            token_ids = extraer_token_ids(m)
            outcomes  = extraer_outcomes(m)
            prices_gamma = extraer_outcome_prices(m)

            if len(token_ids) < 2 or len(outcomes) < 2:
                continue

            partidos.append({
                "evento_id":     str(evento.get("id", "")),
                "titulo":        m.get("question", f"{outcomes[0]} vs {outcomes[1]}"),
                "game_time":     gt,
                "token_ids":     token_ids,
                "outcomes":      outcomes,
                "prices_gamma":  prices_gamma,   # fallback si CLOB falla
            })

    if not partidos:
        proximas = sorted(set(
            m.get("gameStartTime", "")[:10]
            for e in todos
            for m in e.get("markets", [])
            if m.get("sportsMarketType") == "moneyline" and not m.get("closed")
        ))
        if proximas:
            print(f"⚠️  Sin partidos para {hoy}. Próximas fechas: {proximas}")

    return partidos


# ── Helpers (copiados del modelo NBA) ─────────────────────────────────────────

def extraer_token_ids(m: dict) -> list[str]:
    raw = m.get("clobTokenIds", "[]")
    try:
        return [str(i) for i in (json.loads(raw) if isinstance(raw, str) else raw)]
    except Exception:
        return []


def extraer_outcomes(m: dict) -> list[str]:
    raw = m.get("outcomes", "[]")
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []


def extraer_outcome_prices(m: dict) -> list[float]:
    raw = m.get("outcomePrices", "[]")
    try:
        lst = json.loads(raw) if isinstance(raw, str) else raw
        return [float(p) for p in lst]
    except Exception:
        return []


def hora_et() -> str:
    return datetime.utcnow().strftime("%H:%M UTC")


def centavos(precio: float) -> str:
    return f"{round(precio * 100)}¢"


# ── CLOB: precio individual (igual que NBA-POLY) ──────────────────────────────

def precio_clob(token_id: str) -> tuple[str, float | None]:
    """Devuelve (token_id, midpoint) — se llama en paralelo."""
    try:
        r = SESSION.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=8
        )
        r.raise_for_status()
        mid = r.json().get("mid")
        return token_id, float(mid) if mid is not None else None
    except Exception:
        return token_id, None


def obtener_precios_paralelo(token_ids: list[str]) -> dict[str, float]:
    """Consulta todos los tokens en paralelo (máx 20 workers) — igual que NBA."""
    resultado = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        futuros = {pool.submit(precio_clob, tid): tid for tid in token_ids}
        for futuro in as_completed(futuros):
            tid, precio = futuro.result()
            if precio is not None:
                resultado[tid] = precio
    return resultado


def enriquecer_con_gamma(partidos: list[dict], precios_clob: dict) -> dict[str, float]:
    """
    Combina precios CLOB con outcomePrices del Gamma.
    Si CLOB no tiene precio para un token, usa Gamma como fallback.
    """
    precios = dict(precios_clob)
    for p in partidos:
        for i, tid in enumerate(p["token_ids"]):
            if tid not in precios and i < len(p["prices_gamma"]):
                precios[tid] = p["prices_gamma"][i]
    return precios


# ── Diagnóstico ────────────────────────────────────────────────────────────────

def diagnosticar_api():
    print(f"\n[DIAGNÓSTICO] tag_id={MLB_TAG_ID}, fecha={date.today()}\n")
    try:
        resp = SESSION.get(
            f"{GAMMA_API}/events",
            params={"tag_id": MLB_TAG_ID, "active": "true", "closed": "false", "limit": 20},
            timeout=15
        )
        todos = resp.json()
        juegos = [
            m for e in todos
            for m in e.get("markets", [])
            if m.get("sportsMarketType") == "moneyline" and not m.get("closed")
        ]
        print(f"  Mercados moneyline MLB abiertos: {len(juegos)}")
        fechas = sorted(set(m.get("gameStartTime", "")[:10] for m in juegos))
        print(f"  Fechas disponibles: {fechas}")
    except Exception as e:
        print(f"  Error: {e}")


# ── Ejecución directa ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[{hora_et()}]  Buscando partidos MLB de hoy...\n")
    partidos = obtener_partidos_hoy()

    if not partidos:
        diagnosticar_api()
    else:
        all_tokens = list({tid for p in partidos for tid in p["token_ids"]})
        print(f"💹 Consultando {len(all_tokens)} tokens en CLOB...\n")
        precios_clob = obtener_precios_paralelo(all_tokens)
        precios = enriquecer_con_gamma(partidos, precios_clob)
        print(f"   CLOB: {len(precios_clob)} | Gamma fallback: {len(precios)-len(precios_clob)} | Total: {len(precios)}\n")

        for p in partidos:
            print(f"  ⚾  {p['titulo']}  [{p['game_time'][:16]}]")
            for i, (tid, eq) in enumerate(zip(p["token_ids"], p["outcomes"])):
                precio = precios.get(tid)
                print(f"     {eq:30s}  {centavos(precio) if precio is not None else 'sin precio'}")
            print()
