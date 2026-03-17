"""
hedge_sim.py v3 — Bandas Temporales Dinámicas

NUEVA ESTRATEGIA:
  El precio de una opción binaria de 5min es un proxy directo del tiempo restante:
    - precio ~0.50 = mercado 50/50 = inicio del ciclo (mucho tiempo)
    - precio ~0.70 = mercado convencido = mitad/final (poco tiempo)
    - precio >0.75 = casi certeza = últimos segundos (demasiado tarde)

  Las bandas se calculan en función de `secs` (segundos restantes del ciclo):
    - Banda de entrada L1: precio_max baja linealmente con el tiempo
    - Banda de hedge L2:   precio_max también baja (más tarde = hedge más barato o nada)
    - t > 240s restantes: no entrar. Solo gestionar posición abierta.

  Respaldado por datos de 1164 trades reales:
    - zona precio [0.35-0.65]: genera el 85%+ del PnL positivo
    - zona precio >0.65: WR cae a 36-39%, PnL negativo sistemático
    - hedge L2 en [0.25-0.45]: avg +$0.47/trade vs -$0.17 fuera de rango
    - WINs en RESOLUTION: L1 avg 0.51 vs LOSSes en 0.59

VARIABLES DE ENTORNO:
  CAPITAL_INICIAL  float  (default: 100.0)
  STATE_FILE       str    (default: /app/data/state.json)
  LOG_FILE         str    (default: /app/data/hedge_log.json)
  SYMBOL           str    SOL | BTC (default: BTC)
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
CAPITAL_INICIAL = float(os.environ.get("CAPITAL_INICIAL", "100.0"))
STATE_FILE      = os.environ.get("STATE_FILE", "/app/data/state.json")
LOG_FILE        = os.environ.get("LOG_FILE",   "/app/data/hedge_log.json")
SYMBOL          = os.environ.get("SYMBOL", "BTC").upper()

# ─── PARÁMETROS FIJOS ─────────────────────────────────────────────────────────
MAX_PCT_POR_LADO  = 0.015       # 1.5% del capital por lado
MIN_USD_ORDEN     = 1.00        # mínimo real Polymarket
POLL_INTERVAL     = 1.0

OBI_THRESHOLD     = 0.10        # OBI mínimo para considerar señal
OBI_WINDOW_SIZE   = 8           # ventana de historia OBI
OBI_STRONG        = 0.20        # OBI "fuerte" — preferible para entrar

# ─── BANDAS TEMPORALES (el núcleo de v3) ──────────────────────────────────────
# Todos los valores de precio son del token (0.0 → 1.0)
# secs = segundos RESTANTES del ciclo de 5min (300s total)

# ENTRADA L1
ENTRY_SECS_MIN     = 60         # no entra si quedan menos de 60s
PRECIO_L1_MIN      = 0.30       # precio mínimo absoluto para entrar
PRECIO_L1_MAX_LATE = 0.52       # precio máximo cuando quedan ~60s (mercado ya decidido)
PRECIO_L1_MAX_EARLY= 0.65       # precio máximo cuando quedan ~240s+
# La banda baja linealmente: con mucho tiempo acepta hasta 0.65, al final solo 0.52

# HEDGE L2
HEDGE_MOVE_MIN     = 0.05       # L1 debe subir 5c antes de hedgear
HEDGE_OBI_MIN      = -0.05      # OBI mínimo del lado a hedgear
HEDGE_L2_MIN       = 0.25       # precio mínimo L2: mercado muy convencido = no vale hedgear
HEDGE_L2_MAX_EARLY = 0.45       # precio máximo L2 con mucho tiempo restante
HEDGE_L2_MAX_LATE  = 0.35       # precio máximo L2 cuando queda poco tiempo

# EARLY EXIT
EARLY_EXIT_SECS       = 60      # sale si lleva 60s sin conseguir hedge
EARLY_EXIT_OBI_FLIP   = -0.15   # OBI se invierte fuerte → salir
EARLY_EXIT_PRICE_DROP = 0.12    # L1 cae 12c — más alto para no confundir con el spread normal
EARLY_EXIT_GRACE_SECS = 20      # segundos mínimos en posición antes de evaluar cualquier exit
EARLY_EXIT_SECS_MIN   = 45      # salida forzada si quedan <45s sin hedge (diferente a ENTRY_SECS_MIN)

# ─── CONTROL DE FRECUENCIA ────────────────────────────────────────────────────
COOLDOWN_SECS        = 30       # espera mínima entre trades del mismo ciclo
MAX_TRADES_POR_CICLO = 2        # máximo de entradas por ciclo de 5min

# RESOLUCIÓN
SPREAD_MAX           = 0.12
RESOLVED_UP_THRESH   = 0.97
RESOLVED_DN_THRESH   = 0.03


# ─── BANDAS DINÁMICAS ─────────────────────────────────────────────────────────

def banda_entrada_max(secs_restantes: float) -> float:
    """
    Precio máximo permitido para L1 según cuánto tiempo queda.
    - Con >=240s restantes: hasta 0.65 (inicio del ciclo, mercado incierto)
    - Con 60s restantes:    hasta 0.52 (mercado más decidido)
    - Interpolación lineal. Sin límite por tiempo temprano — puede entrar en cualquier momento.
    """
    secs = max(ENTRY_SECS_MIN, min(240, secs_restantes))
    t = (secs - ENTRY_SECS_MIN) / (240 - ENTRY_SECS_MIN)  # 0.0=tarde, 1.0=temprano
    return round(PRECIO_L1_MAX_LATE + t * (PRECIO_L1_MAX_EARLY - PRECIO_L1_MAX_LATE), 4)


def banda_hedge_max(secs_restantes: float) -> float:
    """
    Precio máximo permitido para L2 (el hedge) según tiempo restante.
    - Con mucho tiempo (>180s): hasta 0.45
    - Con poco tiempo (<60s):   hasta 0.35
    """
    t = max(0.0, min(1.0, (secs_restantes - 60) / 120))  # 0=tarde, 1=temprano
    return round(HEDGE_L2_MAX_LATE + t * (HEDGE_L2_MAX_EARLY - HEDGE_L2_MAX_LATE), 4)


def puede_entrar(ask: float, secs_restantes: float) -> tuple[bool, str]:
    """Verifica si el precio de entrada está dentro de la banda temporal."""
    if secs_restantes < ENTRY_SECS_MIN:
        return False, f"muy_tarde {secs_restantes:.0f}s < {ENTRY_SECS_MIN}s"
    if ask < PRECIO_L1_MIN:
        return False, f"precio_bajo {ask:.3f} < {PRECIO_L1_MIN}"
    max_ok = banda_entrada_max(secs_restantes)
    if ask > max_ok:
        return False, f"precio_alto {ask:.3f} > {max_ok:.3f} (banda@{secs_restantes:.0f}s)"
    return True, "ok"


def puede_hedgear(ask_l2: float, secs_restantes: float) -> tuple[bool, str]:
    """Verifica si el precio del hedge está dentro de la banda temporal."""
    if ask_l2 < HEDGE_L2_MIN:
        return False, f"l2_muy_barato {ask_l2:.3f} < {HEDGE_L2_MIN} (mercado muy convencido)"
    max_ok = banda_hedge_max(secs_restantes)
    if ask_l2 > max_ok:
        return False, f"l2_muy_caro {ask_l2:.3f} > {max_ok:.3f} (banda@{secs_restantes:.0f}s)"
    return True, "ok"


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

# Control de frecuencia (se resetean al inicio de cada ciclo de mercado)
estado["ts_ultimo_trade"]   = 0.0   # timestamp del último cierre
estado["trades_este_ciclo"] = 0     # entradas en el ciclo actual

pos = {
    "activa":           False,
    "lado1_side":       None,
    "lado1_precio":     0.0,
    "lado1_shares":     0.0,
    "lado1_usd":        0.0,
    "lado2_side":       None,
    "lado2_precio":     0.0,
    "lado2_shares":     0.0,
    "lado2_usd":        0.0,
    "hedgeado":         False,
    "capital_usado":    0.0,
    "ts_entrada":       None,
    "secs_entrada":     0.0,   # segundos restantes al momento de entrar
}

eventos = deque(maxlen=100)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def mid(m: dict) -> float:
    return (m["best_bid"] + m["best_ask"]) / 2


def log_ev(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    eventos.append(line)
    log.info(msg)


def actualizar_drawdown():
    estado["peak_capital"] = max(estado["peak_capital"], estado["capital"])
    dd = estado["peak_capital"] - estado["capital"]
    estado["max_drawdown"] = max(estado["max_drawdown"], dd)


def resetear_pos():
    for k, v in {
        "activa": False, "lado1_side": None, "lado1_precio": 0.0,
        "lado1_shares": 0.0, "lado1_usd": 0.0, "lado2_side": None,
        "lado2_precio": 0.0, "lado2_shares": 0.0, "lado2_usd": 0.0,
        "hedgeado": False, "capital_usado": 0.0,
        "ts_entrada": None, "secs_entrada": 0.0,
    }.items():
        pos[k] = v


# ─── PERSISTENCIA ─────────────────────────────────────────────────────────────

def guardar_estado():
    total = estado["wins"] + estado["losses"]
    wr    = estado["wins"] / total * 100 if total > 0 else 0.0
    roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100

    payload = {
        "ts":             datetime.now().isoformat(),
        "capital_inicial": CAPITAL_INICIAL,
        "capital":        round(estado["capital"], 4),
        "pnl_total":      round(estado["pnl_total"], 4),
        "roi":            round(roi, 2),
        "peak_capital":   round(estado["peak_capital"], 4),
        "max_drawdown":   round(estado["max_drawdown"], 4),
        "wins":           estado["wins"],
        "losses":         estado["losses"],
        "win_rate":       round(wr, 1),
        "ciclos":         estado["ciclos"],
        "posicion": {
            "activa":        pos["activa"],
            "lado1":         pos["lado1_side"],
            "lado2":         pos["lado2_side"],
            "hedgeado":      pos["hedgeado"],
            "capital_usado": round(pos["capital_usado"], 4),
            "secs_entrada":  round(pos["secs_entrada"], 0),
        },
        "eventos": list(eventos)[-30:],
        "trades":  estado["trades"][-20:],
    }

    try:
        dirpath = os.path.dirname(STATE_FILE)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log.warning(f"No se pudo guardar state: {e}")

    try:
        dirpath = os.path.dirname(LOG_FILE)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        log_data = {"summary": payload, "trades": estado["trades"]}
        with open(LOG_FILE, "w") as f:
            json.dump(log_data, f, indent=2)
    except Exception as e:
        log.warning(f"No se pudo guardar log: {e}")


def restaurar_estado():
    try:
        if not os.path.isfile(LOG_FILE):
            return
        with open(LOG_FILE) as f:
            data = json.load(f)
        s = data.get("summary", {})
        estado["capital"]      = float(s.get("capital",      CAPITAL_INICIAL))
        estado["pnl_total"]    = float(s.get("pnl_total",    0.0))
        estado["wins"]         = int(s.get("wins",   0))
        estado["losses"]       = int(s.get("losses", 0))
        estado["trades"]       = data.get("trades", [])
        log_ev(f"Estado restaurado — capital=${estado['capital']:.2f} W:{estado['wins']} L:{estado['losses']}")
    except Exception as e:
        log_ev(f"restaurar_estado: {e}")


# ─── DISPLAY ──────────────────────────────────────────────────────────────────

def imprimir_estado(up_m, dn_m, secs, sig_up, sig_dn):
    sep = "-" * 70
    print(sep)
    t_str = f"{int(secs)}s restantes" if secs is not None else "tiempo desconocido"

    # Calcular banda actual
    banda_e = banda_entrada_max(secs) if secs else "N/A"
    banda_h = banda_hedge_max(secs) if secs else "N/A"

    print(
        f"  {SYMBOL} 5m | {t_str} | "
        f"UP={up_m['best_ask']:.3f} DN={dn_m['best_ask']:.3f} | "
        f"Banda entrada≤{banda_e:.3f} | Hedge L2≤{banda_h:.3f}"
    )
    total = estado["wins"] + estado["losses"]
    wr    = estado["wins"] / total * 100 if total > 0 else 0
    roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100
    print(
        f"  Capital: ${estado['capital']:.2f} | ROI: {roi:+.1f}% | "
        f"W:{estado['wins']} L:{estado['losses']} WR:{wr:.0f}% | "
        f"DD: ${estado['max_drawdown']:.2f}"
    )
    if pos["activa"]:
        secs_en_pos = time.time() - pos["ts_entrada"] if pos["ts_entrada"] else 0
        print(
            f"  POS: {pos['lado1_side']} @ {pos['lado1_precio']:.4f} | "
            f"${pos['lado1_usd']:.2f} | {int(secs_en_pos)}s en posición | "
            f"{'HEDGEADO' if pos['hedgeado'] else 'sin hedge'}"
        )
        if pos["hedgeado"]:
            print(f"    L2: {pos['lado2_side']} @ {pos['lado2_precio']:.4f} | ${pos['lado2_usd']:.2f}")
    print(
        f"  OBI: UP={up_m['obi']:+.3f} ({sig_up['label']}) | "
        f"DN={dn_m['obi']:+.3f} ({sig_dn['label']})"
    )
    print(sep)


# ─── COMPRA ───────────────────────────────────────────────────────────────────

def comprar(lado: str, ask: float) -> tuple[float, float, float]:
    """Compra directa al best_ask. Retorna (precio, shares, usd) o (0,0,0)."""
    usd = round(estado["capital"] * MAX_PCT_POR_LADO, 4)
    if usd < MIN_USD_ORDEN:
        log_ev(f"  ✗ Orden muy pequeña: ${usd:.2f}")
        return 0.0, 0.0, 0.0
    if usd > estado["capital"]:
        log_ev(f"  ✗ Capital insuficiente: ${estado['capital']:.2f}")
        return 0.0, 0.0, 0.0
    shares = round(usd / ask, 4)
    estado["capital"] -= usd
    log_ev(f"  COMPRA {lado} @ {ask:.4f} | {shares:.4f}sh | ${usd:.2f}")
    return ask, shares, usd


# ─── ENTRADA ──────────────────────────────────────────────────────────────────

async def intentar_entrada(up_m, dn_m, secs):
    if pos["activa"] or secs is None:
        return

    # Control de frecuencia
    secs_desde_ultimo = time.time() - estado["ts_ultimo_trade"]
    if secs_desde_ultimo < COOLDOWN_SECS:
        log_ev(f"  COOLDOWN {int(COOLDOWN_SECS - secs_desde_ultimo)}s restantes")
        return
    if estado["trades_este_ciclo"] >= MAX_TRADES_POR_CICLO:
        log_ev(f"  MAX_TRADES ({MAX_TRADES_POR_CICLO}) alcanzado este ciclo — esperando siguiente")
        return

    obi_up = up_m["obi"]
    obi_dn = dn_m["obi"]

    # Señal OBI: el lado con OBI más positivo es el candidato
    if obi_up > obi_dn and obi_up >= OBI_THRESHOLD:
        lado = "UP"
        ask  = up_m["best_ask"]
        obi  = obi_up
    elif obi_dn > obi_up and obi_dn >= OBI_THRESHOLD:
        lado = "DOWN"
        ask  = dn_m["best_ask"]
        obi  = obi_dn
    else:
        return  # sin señal clara

    ok, razon = puede_entrar(ask, secs)
    if not ok:
        log_ev(f"  SKIP entrada {lado} @ {ask:.3f}: {razon}")
        return

    # Prefiere señal fuerte, pero no obliga
    if obi < OBI_STRONG:
        log_ev(f"  OBI débil ({obi:+.3f}) — entrando igual dentro de banda")

    log_ev(
        f"SEÑAL {lado} — OBI={obi:+.3f} | ask={ask:.3f} | "
        f"{int(secs)}s | banda≤{banda_entrada_max(secs):.3f}"
    )

    precio, shares, usd = comprar(lado, ask)
    if usd == 0.0:
        return

    pos["activa"]       = True
    pos["lado1_side"]   = lado
    pos["lado1_precio"] = precio
    pos["lado1_shares"] = shares
    pos["lado1_usd"]    = usd
    pos["capital_usado"]= usd
    pos["ts_entrada"]   = time.time()
    pos["secs_entrada"] = secs

    estado["trades_este_ciclo"] += 1

    log_ev(
        f"ENTRADA LADO1 {lado} @ {precio:.4f} | {shares:.4f}sh | "
        f"${usd:.2f} | {int(secs)}s restantes | cap=${estado['capital']:.2f}"
    )
    guardar_estado()


# ─── HEDGE ────────────────────────────────────────────────────────────────────

async def intentar_hedge(up_m, dn_m, secs):
    if not pos["activa"] or pos["hedgeado"] or secs is None:
        return

    lado1     = pos["lado1_side"]
    lado2     = "DOWN" if lado1 == "UP" else "UP"
    bid_lado1 = up_m["best_bid"] if lado1 == "UP" else dn_m["best_bid"]
    subida    = bid_lado1 - pos["lado1_precio"]

    if subida < HEDGE_MOVE_MIN:
        return

    obi_lado2 = dn_m["obi"] if lado2 == "DOWN" else up_m["obi"]
    if obi_lado2 < HEDGE_OBI_MIN:
        return

    ask_lado2 = dn_m["best_ask"] if lado2 == "DOWN" else up_m["best_ask"]

    ok, razon = puede_hedgear(ask_lado2, secs)
    if not ok:
        log_ev(f"  SKIP hedge {lado2} @ {ask_lado2:.3f}: {razon}")
        return

    if estado["capital"] * MAX_PCT_POR_LADO < MIN_USD_ORDEN:
        return

    log_ev(
        f"  L1 subio {subida*100:+.1f}c — hedgeando {lado2} @ {ask_lado2:.3f} "
        f"(banda≤{banda_hedge_max(secs):.3f} | {int(secs)}s)"
    )

    precio, shares, usd = comprar(lado2, ask_lado2)
    if usd == 0.0:
        return

    pos["lado2_side"]   = lado2
    pos["lado2_precio"] = precio
    pos["lado2_shares"] = shares
    pos["lado2_usd"]    = usd
    pos["hedgeado"]     = True
    pos["capital_usado"]+= usd

    log_ev(
        f"HEDGE LADO2 {lado2} @ {precio:.4f} | {shares:.4f}sh | "
        f"${usd:.2f} | cap=${estado['capital']:.2f}"
    )
    guardar_estado()


# ─── EARLY EXIT ───────────────────────────────────────────────────────────────

def intentar_early_exit(up_m, dn_m, secs):
    if not pos["activa"] or pos["hedgeado"]:
        return

    lado1       = pos["lado1_side"]
    bid_lado1   = up_m["best_bid"] if lado1 == "UP" else dn_m["best_bid"]
    obi_lado1   = up_m["obi"]      if lado1 == "UP" else dn_m["obi"]
    secs_en_pos = time.time() - pos["ts_entrada"] if pos["ts_entrada"] else 0
    caida       = pos["lado1_precio"] - bid_lado1

    # Grace period: no evaluar nada en los primeros segundos.
    # Al entrar, ask=0.45 y bid=0.37 es spread normal — no es caída real.
    if secs_en_pos < EARLY_EXIT_GRACE_SECS:
        return

    razon = None

    # Timeout: demasiado tiempo en posición sin conseguir hedge
    if secs_en_pos > EARLY_EXIT_SECS:
        razon = f"timeout {int(secs_en_pos)}s sin hedge"

    # Mercado gira fuerte en contra
    elif obi_lado1 < EARLY_EXIT_OBI_FLIP:
        razon = f"OBI invertido {obi_lado1:+.3f}"

    # Precio cayó más allá del spread normal (0.12 > spread típico 0.06-0.08)
    elif caida > EARLY_EXIT_PRICE_DROP:
        razon = f"caida {caida*100:.1f}c desde entrada"

    # Tiempo crítico: quedan muy pocos segundos y no hay hedge — salir ya
    # EARLY_EXIT_SECS_MIN (45s) < ENTRY_SECS_MIN (60s) para no entrar en conflicto
    elif secs is not None and secs < EARLY_EXIT_SECS_MIN:
        razon = f"tiempo critico {int(secs)}s sin hedge"

    if not razon:
        return

    exit_precio = max(bid_lado1, 0.01)
    pnl = round(pos["lado1_shares"] * exit_precio - pos["lado1_usd"], 4)

    estado["capital"]   += pos["lado1_usd"] + pnl
    estado["pnl_total"] += pnl

    if pnl >= 0:
        estado["wins"] += 1
    else:
        estado["losses"] += 1

    actualizar_drawdown()
    log_ev(
        f"EARLY EXIT {lado1} @ {exit_precio:.4f} | {razon} | "
        f"PnL: ${pnl:+.4f} | cap=${estado['capital']:.2f}"
    )
    _registrar_trade("EARLY_EXIT", exit_precio, None, "WIN" if pnl >= 0 else "LOSS", pnl)
    estado["ts_ultimo_trade"] = time.time()
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
        log_ev(f"Tiempo agotado — mid UP={up_mid:.3f} -> {resuelto}")

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
    _registrar_trade(
        "RESOLUTION",
        1.0 if resuelto == pos["lado1_side"] else 0.0,
        resuelto, outcome, pnl_total,
    )
    estado["ts_ultimo_trade"] = time.time()
    resetear_pos()
    guardar_estado()


def _registrar_trade(tipo, exit_precio, resuelto, outcome, pnl):
    estado["trades"].append({
        "ts":           datetime.now().isoformat(),
        "tipo":         tipo,
        "resolucion":   resuelto,
        "lado1_side":   pos["lado1_side"],
        "lado1_usd":    round(pos["lado1_usd"], 4),
        "lado1_precio": round(pos["lado1_precio"], 4),
        "hedgeado":     pos["hedgeado"],
        "lado2_side":   pos["lado2_side"],
        "lado2_usd":    round(pos["lado2_usd"], 4),
        "lado2_precio": round(pos["lado2_precio"], 4),
        "exit_precio":  round(exit_precio, 4),
        "pnl":          round(pnl, 4),
        "capital":      round(estado["capital"], 4),
        "outcome":      outcome,
        "secs_entrada": round(pos["secs_entrada"], 0),  # nuevo: para análisis temporal
    })


# ─── LOOP PRINCIPAL ───────────────────────────────────────────────────────────

async def main_loop():
    log_ev("=" * 70)
    log_ev(f"  HEDGE SIM v3 — Bandas Temporales Dinámicas ({SYMBOL})")
    log_ev(f"  Capital: ${CAPITAL_INICIAL:.0f} | {MAX_PCT_POR_LADO*100:.1f}%/lado = ${CAPITAL_INICIAL*MAX_PCT_POR_LADO:.2f}/orden")
    log_ev(f"  Banda entrada: precio [{PRECIO_L1_MIN:.2f}-{PRECIO_L1_MAX_EARLY:.2f}] encogiendo a [{PRECIO_L1_MIN:.2f}-{PRECIO_L1_MAX_LATE:.2f}]")
    log_ev(f"  Banda hedge:   L2 [{HEDGE_L2_MIN:.2f}-{HEDGE_L2_MAX_EARLY:.2f}] encogiendo a [{HEDGE_L2_MIN:.2f}-{HEDGE_L2_MAX_LATE:.2f}]")
    log_ev(f"  Ventana de entrada: desde cualquier momento hasta {ENTRY_SECS_MIN}s antes del cierre")
    log_ev("=" * 70)

    restaurar_estado()
    guardar_estado()

    mkt  = None
    loop = asyncio.get_event_loop()

    while True:
        try:
            # 1. Descubrir mercado
            if mkt is None:
                log_ev(f"Buscando mercado {SYMBOL} Up/Down 5m...")
                obi_history_up.clear()
                obi_history_dn.clear()
                estado["trades_este_ciclo"] = 0
                mkt = await loop.run_in_executor(None, find_active_market, SYMBOL)
                if mkt:
                    estado["ciclos"] += 1
                    log_ev(f"Mercado: {mkt.get('question', '')} | ciclo #{estado['ciclos']}")
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

            # 3. Actualizar historia OBI
            obi_history_up.append(up_m["obi"])
            obi_history_dn.append(dn_m["obi"])

            # 4. Mercado expirado
            if secs is not None and secs <= 0:
                if pos["activa"]:
                    verificar_resolucion(up_m, dn_m, secs)
                log_ev("Mercado expirado — buscando siguiente ciclo...")
                mkt = None
                await asyncio.sleep(5)
                continue

            # 5. Verificar resolución por precio concluyente
            if pos["activa"]:
                verificar_resolucion(up_m, dn_m, secs)

            # 6. Early exit (tiempo crítico sin hedge)
            if pos["activa"] and not pos["hedgeado"]:
                intentar_early_exit(up_m, dn_m, secs)

            # 7. Intentar hedge con banda temporal
            if pos["activa"] and not pos["hedgeado"]:
                await intentar_hedge(up_m, dn_m, secs)

            # 8. Nueva entrada con banda temporal
            if not pos["activa"]:
                await intentar_entrada(up_m, dn_m, secs)

            # 9. Señales para display
            sig_up = compute_signal(up_m["obi"], list(obi_history_up), OBI_THRESHOLD)
            sig_dn = compute_signal(dn_m["obi"], list(obi_history_dn), OBI_THRESHOLD)

            # 10. Display
            imprimir_estado(up_m, dn_m, secs, sig_up, sig_dn)
            guardar_estado()

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
