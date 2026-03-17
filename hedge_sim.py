"""
hedge_sim.py v2 — Simulación Hedge Dinámico BTC Up/Down 5m
Para Railway: lee config desde variables de entorno, persiste estado en /data.

ESTRATEGIA:
  1. OBI (Order Book Imbalance) como señal de entrada
  2. Fragmentación de órdenes en 5 micro-compras
  3. Hedge dinámico cuando el lado1 sube lo suficiente
  4. Early exit si no llega el hedge a tiempo

VARIABLES DE ENTORNO (Railway):
  CAPITAL_INICIAL     float  Capital de simulación (default: 100.0)
  STATE_FILE          str    Ruta del archivo de estado (default: /data/state.json)
  LOG_FILE            str    Ruta del log JSON (default: /data/hedge_log.json)
"""

import asyncio
import os
import sys
import time
import json
import logging
from datetime import datetime, timezone
from collections import deque

from strategy_core import (
    find_active_market,
    get_order_book_metrics,
    compute_signal,
    seconds_remaining,
)

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("hedge_sim")
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ─── CONFIG DESDE ENV VARS ────────────────────────────────────────────────────
CAPITAL_INICIAL      = float(os.environ.get("CAPITAL_INICIAL", "100.0"))
STATE_FILE           = os.environ.get("STATE_FILE",  "/data/state.json")
LOG_FILE             = os.environ.get("LOG_FILE",    "/data/hedge_log.json")

# ─── PARÁMETROS ───────────────────────────────────────────────────────────────
MAX_PCT_POR_LADO     = 0.015
POLL_INTERVAL        = 1.0

OBI_THRESHOLD        = 0.10
OBI_WINDOW_SIZE      = 8
OBI_STRONG_THRESHOLD = 0.20

ORDEN_FRAGMENTOS     = 5
ORDEN_DELAY_SECS     = 0.4

SPREAD_MAX           = 0.12
PRECIO_MIN_LADO1     = 0.25
PRECIO_MAX_LADO1     = 0.75

ENTRY_WINDOW_MAX     = 240          # no entra en los primeros 60s del ciclo
ENTRY_WINDOW_MIN     = 60           # no entra en los ultimos 60s

HEDGE_MOVE_MIN       = 0.05
HEDGE_OBI_MIN        = -0.05
TIMEOUT_SIN_HEDGE    = 45          # sale si quedan <45s y aun no hay hedge
HEDGE_PRECIO_MAX     = 0.35

RESOLVED_UP_THRESH   = 0.97
RESOLVED_DN_THRESH   = 0.03

# ─── ESTADO GLOBAL ────────────────────────────────────────────────────────────
estado = {
    "capital":      CAPITAL_INICIAL,
    "pnl_total":    0.0,
    "peak_capital": CAPITAL_INICIAL,
    "max_drawdown": 0.0,
    "wins":         0,
    "losses":       0,
    "ciclos":       0,
    "trades":       [],
}

obi_history_up = deque(maxlen=OBI_WINDOW_SIZE)
obi_history_dn = deque(maxlen=OBI_WINDOW_SIZE)

pos = {
    "activa":           False,
    "lado1_side":       None,
    "lado1_precio_avg": 0.0,
    "lado1_shares":     0.0,
    "lado1_usd":        0.0,
    "lado2_side":       None,
    "lado2_precio_avg": 0.0,
    "lado2_shares":     0.0,
    "lado2_usd":        0.0,
    "hedgeado":         False,
    "capital_usado":    0.0,
    "ts_entrada":       None,
    "secs_entrada":     0.0,
}

eventos = deque(maxlen=100)


# ─── PERSISTENCIA ─────────────────────────────────────────────────────────────

def guardar_estado():
    """Escribe state.json para el dashboard y log_file para historial."""
    total  = estado["wins"] + estado["losses"]
    wr     = estado["wins"] / total * 100 if total > 0 else 0.0
    roi    = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100

    payload = {
        "ts":           datetime.now().isoformat(),
        "capital":      round(estado["capital"], 4),
        "pnl_total":    round(estado["pnl_total"], 4),
        "roi":          round(roi, 2),
        "peak_capital": round(estado["peak_capital"], 4),
        "max_drawdown": round(estado["max_drawdown"], 4),
        "wins":         estado["wins"],
        "losses":       estado["losses"],
        "win_rate":     round(wr, 1),
        "ciclos":       estado["ciclos"],
        "posicion": {
            "activa":        pos["activa"],
            "lado1":         pos["lado1_side"],
            "lado2":         pos["lado2_side"],
            "hedgeado":      pos["hedgeado"],
            "capital_usado": round(pos["capital_usado"], 4),
        },
        "eventos":      list(eventos)[-30:],
        "trades":       estado["trades"][-20:],
    }

    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log.warning(f"guardar_estado error: {e}")

    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "w") as f:
            json.dump({
                "summary": {
                    "capital_inicial": CAPITAL_INICIAL,
                    "capital_actual":  round(estado["capital"], 4),
                    "pnl_total":       round(estado["pnl_total"], 4),
                    "roi_pct":         round(roi, 2),
                    "max_drawdown":    round(estado["max_drawdown"], 4),
                    "wins":            estado["wins"],
                    "losses":          estado["losses"],
                    "win_rate":        round(wr, 1),
                },
                "trades": estado["trades"],
            }, f, indent=2)
    except Exception as e:
        log.warning(f"guardar_log error: {e}")


def restaurar_estado():
    """Al arrancar, recupera capital y trades del LOG_FILE anterior si existe."""
    if not os.path.isfile(LOG_FILE):
        log.info("Sin estado previo — iniciando desde cero.")
        return
    try:
        with open(LOG_FILE) as f:
            data = json.load(f)
        s = data.get("summary", {})
        estado["capital"]      = float(s.get("capital_actual",  CAPITAL_INICIAL))
        estado["pnl_total"]    = float(s.get("pnl_total",       0.0))
        estado["wins"]         = int(s.get("wins",   0))
        estado["losses"]       = int(s.get("losses", 0))
        estado["trades"]       = data.get("trades", [])

        peak = CAPITAL_INICIAL
        for t in estado["trades"]:
            cap = float(t.get("capital", CAPITAL_INICIAL))
            if cap > peak:
                peak = cap
            dd = peak - cap
            if dd > estado["max_drawdown"]:
                estado["max_drawdown"] = dd
        estado["peak_capital"] = peak

        total = estado["wins"] + estado["losses"]
        log.info(
            f"Estado restaurado — {total} trades | "
            f"Capital: ${estado['capital']:.4f} | "
            f"PnL: ${estado['pnl_total']:+.4f} | "
            f"W:{estado['wins']} L:{estado['losses']}"
        )
    except Exception as e:
        log.warning(f"No se pudo restaurar estado: {e}")


# ─── UTILIDADES ───────────────────────────────────────────────────────────────

def log_ev(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    eventos.append(f"[{ts}] {msg}")
    log.info(msg)

def mid(m) -> float:
    b, a = m["best_bid"], m["best_ask"]
    if b > 0 and a > 0:
        return round((b + a) / 2, 4)
    return round(b or a, 4)

def actualizar_drawdown():
    cap = estado["capital"]
    if cap > estado["peak_capital"]:
        estado["peak_capital"] = cap
    dd = estado["peak_capital"] - cap
    if dd > estado["max_drawdown"]:
        estado["max_drawdown"] = dd

def resetear_pos():
    for k in pos:
        if k in ("activa", "hedgeado"):
            pos[k] = False
        elif isinstance(pos[k], str):
            pos[k] = None
        else:
            pos[k] = 0.0

def imprimir_estado(up_m, dn_m, secs, signal_up, signal_dn):
    sep = "-" * 65
    total = estado["wins"] + estado["losses"]
    wr    = estado["wins"] / total * 100 if total > 0 else 0
    roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100

    print(f"\n{sep}")
    print(f"  Capital: ${estado['capital']:.2f}  PnL: ${estado['pnl_total']:+.2f}  ROI: {roi:+.1f}%  MaxDD: ${estado['max_drawdown']:.2f}")
    print(f"  W:{estado['wins']} L:{estado['losses']} WR:{wr:.0f}%  |  Ciclos: {estado['ciclos']}")

    if up_m and dn_m:
        print(f"  UP  bid={up_m['best_bid']:.3f} ask={up_m['best_ask']:.3f} mid={mid(up_m):.3f}  OBI={up_m['obi']:+.3f} spread={up_m['spread']:.3f}")
        print(f"  DN  bid={dn_m['best_bid']:.3f} ask={dn_m['best_ask']:.3f} mid={mid(dn_m):.3f}  OBI={dn_m['obi']:+.3f} spread={dn_m['spread']:.3f}")
        if signal_up:
            print(f"  Señal UP: {signal_up['label']} conf={signal_up['confidence']}%  combined={signal_up['combined']:+.3f}")
        if signal_dn:
            print(f"  Señal DN: {signal_dn['label']} conf={signal_dn['confidence']}%  combined={signal_dn['combined']:+.3f}")
        print(f"  Tiempo restante: {int(secs) if secs else '?'}s")

    if pos["activa"]:
        secs_en_pos = time.time() - pos["ts_entrada"] if pos["ts_entrada"] else 0
        print(f"\n  POSICION ABIERTA ({int(secs_en_pos)}s):")
        print(f"    Lado1: {pos['lado1_side']} @ avg={pos['lado1_precio_avg']:.3f} | ${pos['lado1_usd']:.2f} | {pos['lado1_shares']:.2f}sh")
        if pos["hedgeado"]:
            print(f"    Lado2: {pos['lado2_side']} @ avg={pos['lado2_precio_avg']:.3f} | ${pos['lado2_usd']:.2f} | {pos['lado2_shares']:.2f}sh")
            print(f"    Capital en juego: ${pos['capital_usado']:.2f} ({pos['capital_usado']/CAPITAL_INICIAL*100:.1f}%)")
        else:
            print(f"    Esperando hedge... (sin cubrir)")
    else:
        print(f"\n  Sin posicion abierta")
    print(sep)


# ─── ENTRADA FRAGMENTADA ──────────────────────────────────────────────────────

async def entrada_fragmentada(lado: str, ask_inicial: float) -> tuple[float, float, float]:
    max_usd  = estado["capital"] * MAX_PCT_POR_LADO
    usd_frag = max_usd / ORDEN_FRAGMENTOS
    precios  = []
    shares_total = 0.0
    usd_total    = 0.0

    log_ev(f"  Fragmentando {lado} en {ORDEN_FRAGMENTOS} partes de ${usd_frag:.2f}...")

    for i in range(ORDEN_FRAGMENTOS):
        precio_frag = ask_inicial
        shares_frag = round(usd_frag / precio_frag, 4)
        precios.append(precio_frag)
        shares_total += shares_frag
        usd_total    += usd_frag
        log_ev(f"    [{i+1}/{ORDEN_FRAGMENTOS}] {lado} @ {precio_frag:.4f} | {shares_frag:.4f}sh | ${usd_frag:.2f}")
        if i < ORDEN_FRAGMENTOS - 1:
            await asyncio.sleep(ORDEN_DELAY_SECS)

    precio_avg = round(sum(precios) / len(precios), 4)
    estado["capital"] -= usd_total
    return precio_avg, round(shares_total, 4), round(usd_total, 4)


# ─── SEÑAL DE ENTRADA ─────────────────────────────────────────────────────────

def evaluar_señal(up_m, dn_m):
    obi_up = up_m["obi"]
    obi_dn = dn_m["obi"]
    obi_history_up.append(obi_up)
    obi_history_dn.append(obi_dn)

    signal_up = compute_signal(obi_up, list(obi_history_up), OBI_THRESHOLD)
    signal_dn = compute_signal(obi_dn, list(obi_history_dn), OBI_THRESHOLD)

    if up_m["spread"] > SPREAD_MAX or dn_m["spread"] > SPREAD_MAX:
        return signal_up, signal_dn, None

    if signal_up["combined"] >= OBI_STRONG_THRESHOLD:
        ask = up_m["best_ask"]
        if PRECIO_MIN_LADO1 <= ask <= PRECIO_MAX_LADO1:
            return signal_up, signal_dn, "UP"

    if signal_dn["combined"] >= OBI_STRONG_THRESHOLD:
        ask = dn_m["best_ask"]
        if PRECIO_MIN_LADO1 <= ask <= PRECIO_MAX_LADO1:
            return signal_up, signal_dn, "DOWN"

    if signal_up["label"] in ("UP", "STRONG UP") and signal_up["combined"] > signal_dn["combined"]:
        ask = up_m["best_ask"]
        if PRECIO_MIN_LADO1 <= ask <= PRECIO_MAX_LADO1:
            return signal_up, signal_dn, "UP"

    if signal_dn["label"] in ("UP", "STRONG UP") and signal_dn["combined"] > signal_up["combined"]:
        ask = dn_m["best_ask"]
        if PRECIO_MIN_LADO1 <= ask <= PRECIO_MAX_LADO1:
            return signal_up, signal_dn, "DOWN"

    return signal_up, signal_dn, None


# ─── ENTRADA LADO 1 ───────────────────────────────────────────────────────────

async def intentar_entrada(up_m, dn_m, secs):
    if pos["activa"]:
        return
    if secs is None or not (ENTRY_WINDOW_MIN < secs <= ENTRY_WINDOW_MAX):
        return
    if estado["capital"] * MAX_PCT_POR_LADO < 0.50:
        return

    signal_up, signal_dn, lado = evaluar_señal(up_m, dn_m)
    if not lado:
        return

    ask = up_m["best_ask"] if lado == "UP" else dn_m["best_ask"]
    obi = up_m["obi"]      if lado == "UP" else dn_m["obi"]

    log_ev(f"SEÑAL {lado} — OBI={obi:+.3f} | ask={ask:.3f} | {int(secs)}s restantes")

    precio_avg, shares, usd = await entrada_fragmentada(lado, ask)

    pos["activa"]           = True
    pos["lado1_side"]       = lado
    pos["lado1_precio_avg"] = precio_avg
    pos["lado1_shares"]     = shares
    pos["lado1_usd"]        = usd
    pos["capital_usado"]    = usd
    pos["ts_entrada"]       = time.time()
    pos["secs_entrada"]     = secs or 0

    log_ev(f"ENTRADA LADO1 {lado} | avg={precio_avg:.4f} | {shares:.4f}sh | ${usd:.2f} | cap=${estado['capital']:.2f}")
    guardar_estado()


# ─── HEDGE LADO 2 ─────────────────────────────────────────────────────────────

async def intentar_hedge(up_m, dn_m):
    if not pos["activa"] or pos["hedgeado"]:
        return

    lado1     = pos["lado1_side"]
    lado2     = "DOWN" if lado1 == "UP" else "UP"
    bid_lado1 = up_m["best_bid"] if lado1 == "UP" else dn_m["best_bid"]
    subida    = bid_lado1 - pos["lado1_precio_avg"]

    if subida < HEDGE_MOVE_MIN:
        return

    obi_lado2 = dn_m["obi"] if lado2 == "DOWN" else up_m["obi"]
    if obi_lado2 < HEDGE_OBI_MIN:
        return

    ask_lado2 = dn_m["best_ask"] if lado2 == "DOWN" else up_m["best_ask"]
    if ask_lado2 <= 0 or ask_lado2 > HEDGE_PRECIO_MAX:
        return
    if estado["capital"] * MAX_PCT_POR_LADO < 0.50:
        return

    log_ev(f"  Lado1 subio {subida*100:+.1f}c — hedgeando en {lado2}")

    precio_avg, shares, usd = await entrada_fragmentada(lado2, ask_lado2)

    pos["lado2_side"]       = lado2
    pos["lado2_precio_avg"] = precio_avg
    pos["lado2_shares"]     = shares
    pos["lado2_usd"]        = usd
    pos["hedgeado"]         = True
    pos["capital_usado"]   += usd

    log_ev(f"HEDGE LADO2 {lado2} | avg={precio_avg:.4f} | {shares:.4f}sh | ${usd:.2f} | cap=${estado['capital']:.2f}")
    guardar_estado()


# ─── TIMEOUT EXIT (sin hedge) ─────────────────────────────────────────────────

def intentar_timeout_exit(up_m, dn_m, secs):
    if not pos["activa"] or pos["hedgeado"]:
        return
    if secs is None or secs > TIMEOUT_SIN_HEDGE:
        return

    lado1       = pos["lado1_side"]
    bid_lado1   = up_m["best_bid"] if lado1 == "UP" else dn_m["best_bid"]
    exit_precio = max(bid_lado1, 0.01)
    pnl         = round(pos["lado1_shares"] * exit_precio - pos["lado1_usd"], 4)

    estado["capital"]   += pos["lado1_usd"] + pnl
    estado["pnl_total"] += pnl

    if pnl >= 0:
        estado["wins"] += 1
    else:
        estado["losses"] += 1

    actualizar_drawdown()
    log_ev(f"TIMEOUT EXIT {lado1} @ bid={exit_precio:.4f} | {int(secs)}s restantes | PnL: ${pnl:+.4f} | cap=${estado['capital']:.2f}")
    _registrar_trade("TIMEOUT_EXIT", exit_precio, None, "WIN" if pnl >= 0 else "LOSS", pnl)
    resetear_pos()
    guardar_estado()


# ─── RESOLUCIÓN ───────────────────────────────────────────────────────────────

def verificar_resolucion(up_m, dn_m, secs):
    if not pos["activa"]:
        return

    up_mid = mid(up_m)
    dn_mid = mid(dn_m)

    resuelto = None
    if up_mid >= RESOLVED_UP_THRESH:
        resuelto = "UP"
    elif up_mid <= RESOLVED_DN_THRESH:
        resuelto = "DOWN"
    elif dn_mid >= RESOLVED_UP_THRESH:
        resuelto = "DOWN"
    elif secs is not None and secs <= 0:
        resuelto = "UP" if up_mid > 0.5 else "DOWN"
        log_ev(f"Tiempo agotado — resolviendo por mid UP={up_mid:.3f} -> {resuelto}")

    if resuelto:
        _aplicar_resolucion(resuelto)


def _aplicar_resolucion(resuelto: str):
    pnl_total = 0.0
    partes    = []

    if resuelto == pos["lado1_side"]:
        pnl_l1 = pos["lado1_shares"] * 1.0 - pos["lado1_usd"]
        partes.append(f"L1 {pos['lado1_side']}=WIN(${pnl_l1:+.2f})")
    else:
        pnl_l1 = -pos["lado1_usd"]
        partes.append(f"L1 {pos['lado1_side']}=LOSS(${pnl_l1:+.2f})")
    pnl_total += pnl_l1

    if pos["hedgeado"]:
        if resuelto == pos["lado2_side"]:
            pnl_l2 = pos["lado2_shares"] * 1.0 - pos["lado2_usd"]
            partes.append(f"L2 {pos['lado2_side']}=WIN(${pnl_l2:+.2f})")
        else:
            pnl_l2 = -pos["lado2_usd"]
            partes.append(f"L2 {pos['lado2_side']}=LOSS(${pnl_l2:+.2f})")
        pnl_total += pnl_l2

    estado["capital"]   += pos["capital_usado"] + pnl_total
    estado["pnl_total"] += pnl_total

    outcome = "WIN" if pnl_total >= 0 else "LOSS"
    if outcome == "WIN":
        estado["wins"] += 1
    else:
        estado["losses"] += 1

    actualizar_drawdown()
    log_ev(
        f"RESOLUCION -> {resuelto} | {' | '.join(partes)} | "
        f"PnL NETO: ${pnl_total:+.2f} | cap=${estado['capital']:.2f}"
    )
    _registrar_trade("RESOLUTION", 1.0 if resuelto == pos["lado1_side"] else 0.0, resuelto, outcome, pnl_total)
    resetear_pos()
    guardar_estado()


def _registrar_trade(tipo, exit_precio, resuelto, outcome, pnl):
    estado["trades"].append({
        "ts":           datetime.now().isoformat(),
        "tipo":         tipo,
        "resolucion":   resuelto,
        "lado1_side":   pos["lado1_side"],
        "lado1_usd":    round(pos["lado1_usd"], 4),
        "lado1_precio": round(pos["lado1_precio_avg"], 4),
        "hedgeado":     pos["hedgeado"],
        "lado2_side":   pos["lado2_side"],
        "lado2_usd":    round(pos["lado2_usd"], 4),
        "lado2_precio": round(pos["lado2_precio_avg"], 4),
        "exit_precio":  round(exit_precio, 4),
        "pnl":          round(pnl, 4),
        "capital":      round(estado["capital"], 4),
        "outcome":      outcome,
    })


# ─── LOOP PRINCIPAL ───────────────────────────────────────────────────────────

async def main_loop():
    log_ev("=" * 65)
    log_ev("  HEDGE SIM v2 — OBI + Fragmentacion + Early Exit")
    log_ev(f"  Capital: ${CAPITAL_INICIAL:.0f} | Max/lado: {MAX_PCT_POR_LADO*100:.1f}% | Max/ciclo: {MAX_PCT_POR_LADO*2*100:.1f}%")
    log_ev(f"  STATE: {STATE_FILE} | LOG: {LOG_FILE}")
    log_ev("  SIMULACION — sin dinero real")
    log_ev("=" * 65)

    restaurar_estado()
    guardar_estado()

    mkt             = None
    loop            = asyncio.get_event_loop()
    signal_up_cache = None
    signal_dn_cache = None

    while True:
        try:
            # 1. Descubrir mercado
            if mkt is None:
                log_ev("Buscando mercado BTC Up/Down 5m...")
                obi_history_up.clear()
                obi_history_dn.clear()
                mkt = await loop.run_in_executor(None, find_active_market, "BTC")
                if mkt:
                    estado["ciclos"] += 1
                    log_ev(f"Mercado: {mkt.get('question','')}")
                else:
                    log_ev("Sin mercado activo — reintentando en 10s...")
                    await asyncio.sleep(10)
                    continue

            # 2. Leer order books
            up_m, err_up = await loop.run_in_executor(
                None, get_order_book_metrics, mkt["up_token_id"]
            )
            dn_m, err_dn = await loop.run_in_executor(
                None, get_order_book_metrics, mkt["down_token_id"]
            )

            if not up_m or not dn_m:
                log_ev(f"Error OB: {err_up or err_dn}")
                await asyncio.sleep(POLL_INTERVAL * 2)
                continue

            secs = seconds_remaining(mkt)

            # 3. Mercado expirado
            if secs is not None and secs <= 0:
                if pos["activa"]:
                    verificar_resolucion(up_m, dn_m, secs)
                log_ev("Mercado expirado — buscando proximo ciclo...")
                mkt = None
                await asyncio.sleep(5)
                continue

            # 4. Resolución por precio concluyente
            if pos["activa"]:
                verificar_resolucion(up_m, dn_m, secs)

            # 5. Timeout exit si no hay hedge y quedan <45s
            if pos["activa"] and not pos["hedgeado"]:
                intentar_timeout_exit(up_m, dn_m, secs)

            # 6. Intentar hedge
            if pos["activa"] and not pos["hedgeado"]:
                await intentar_hedge(up_m, dn_m)

            # 7. Nueva entrada
            if not pos["activa"]:
                await intentar_entrada(up_m, dn_m, secs)

            # 8. Señales para display
            signal_up_cache = compute_signal(up_m["obi"], list(obi_history_up), OBI_THRESHOLD)
            signal_dn_cache = compute_signal(dn_m["obi"], list(obi_history_dn), OBI_THRESHOLD)

            # 9. Mostrar en logs de Railway
            imprimir_estado(up_m, dn_m, secs, signal_up_cache, signal_dn_cache)

        except Exception as e:
            log_ev(f"Error en loop: {e}")
            import traceback
            traceback.print_exc()

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        log.info("Detenido.")
        guardar_estado()
        total = estado["wins"] + estado["losses"]
        wr    = estado["wins"] / total * 100 if total > 0 else 0
        roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100
        print(f"\nCapital: ${estado['capital']:.2f} | ROI: {roi:+.1f}% | W:{estado['wins']} L:{estado['losses']} WR:{wr:.0f}%")
