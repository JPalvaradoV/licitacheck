"""
LicitaCheck — Crawler OCDS (Mercado Público Chile)

Consume la API pública OCDS (sin ticket) y produce un Excel compatible con
process_data.py. Soporta modo incremental: mantiene un cache local con los
IDs ya descargados y solo pide al servidor los nuevos.

Uso:
    # Smoke test (10 licitaciones de un mes específico)
    python3 crawler.py --año 2025 --mes 12 --limit 10

    # Mes actual completo (bulk fetch)
    python3 crawler.py

    # Diario incremental + regenerar JSON del sitio
    python3 crawler.py --incremental --auto-process

    # Rango de meses (útil al inicializar el cache)
    python3 crawler.py --meses-atras 3 --incremental --auto-process

Archivos auxiliares (en este mismo directorio):
    .crawler_cache.csv   — datos acumulados (no commitear si pesa mucho)
    crawler.log          — log si se ejecuta con --log-file
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

API_BASE   = 'https://api.mercadopublico.cl/APISOCDS/OCDS'
API_LISTA  = API_BASE + '/listaOCDSAgnoMes/{año}/{mes}/{offset}/{limit}'
API_TENDER = API_BASE + '/tender/{tender_id}'

EXCEL_COLUMNS = [
    'Numero Adquisición', 'Tipo Adquisición', 'Nombre Adquisición',
    'Descripción', 'Organismo', 'Región Compradora',
    'Fecha Publicación', 'Fecha Cierre',
    'Descripción del producto/servicio', 'Código ONU',
    'Unidad de Medida', 'Cantidad',
    'Genérico', 'Nivel 1', 'Nivel 2', 'Nivel 3',
    # Nuevos campos que OCDS expone y el Excel oficial no traía
    'Monto Estimado', 'Oferentes',
]

CACHE_FILE = Path(__file__).resolve().parent / '.crawler_cache.csv'

# Columnas de fecha que deben quedar en un formato canónico único en el cache.
DATE_COLUMNS = ['Fecha Publicación', 'Fecha Cierre']


def log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    prefix = {'info': '·', 'step': '▶', 'ok': '✓', 'warn': '⚠', 'err': '✗'}.get(level, '·')
    print(f'[{ts}] {prefix} {msg}', flush=True)


def http_get_json(url, max_retries=3, backoff=2.0, timeout=30):
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers={'Accept': 'application/json'}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(backoff ** (attempt + 1))
    raise RuntimeError(f'GET falló tras {max_retries} intentos: {url} :: {last_err}')


def fetch_lista(año, mes, offset=0, limit=1000):
    return http_get_json(API_LISTA.format(año=año, mes=mes, offset=offset, limit=limit))


def fetch_tender(tender_id):
    return http_get_json(API_TENDER.format(tender_id=tender_id))


def parse_jerarquia(desc):
    if not desc:
        return '', '', '', ''
    parts = [p.strip() for p in desc.split('/')]
    while len(parts) < 4:
        parts.append('')
    return parts[0], parts[1], parts[2], parts[3]


def buyer_from_parties(parties):
    if not parties:
        return None
    for role_prefer in ['buyer', 'procuringEntity']:
        for p in parties:
            if role_prefer in p.get('roles', []):
                return p
    return None


def tenderers_from_parties(parties):
    """Extrae nombres únicos de oferentes/proveedores ('tenderer'/'supplier')."""
    names = set()
    for p in parties or []:
        roles = p.get('roles', [])
        if 'tenderer' in roles or 'supplier' in roles:
            name = (p.get('name') or '').split(' | ')[0].strip()
            if name:
                names.add(name)
    return sorted(names)


def tender_to_rows(tender_response):
    if not tender_response or 'releases' not in tender_response or not tender_response['releases']:
        return []

    release = tender_response['releases'][0]
    tender = release.get('tender', {})
    parties = release.get('parties', [])
    buyer = buyer_from_parties(parties)

    organismo = ''
    region = ''
    if buyer:
        organismo = (buyer.get('name') or '').split(' | ')[0].strip()
        region = (buyer.get('address') or {}).get('region', '')

    period = tender.get('tenderPeriod') or {}
    value = tender.get('value') or {}
    monto = value.get('amount')
    oferentes_list = tenderers_from_parties(parties)
    oferentes_str = ' | '.join(oferentes_list)

    base = {
        'Numero Adquisición': tender.get('id', ''),
        'Tipo Adquisición':   tender.get('procurementMethodDetails', ''),
        'Nombre Adquisición': tender.get('title', ''),
        'Descripción':        tender.get('description', ''),
        'Organismo':          organismo,
        'Región Compradora':  region,
        'Fecha Publicación':  period.get('startDate', ''),
        'Fecha Cierre':       period.get('endDate', ''),
        'Monto Estimado':     monto,
        'Oferentes':          oferentes_str,
    }

    rows = []
    for item in tender.get('items') or []:
        n1, n2, n3, gen = parse_jerarquia(item.get('description', ''))
        rows.append({
            **base,
            'Descripción del producto/servicio': item.get('description', ''),
            'Código ONU':       (item.get('classification') or {}).get('id', ''),
            'Unidad de Medida': (item.get('unit') or {}).get('name', ''),
            'Cantidad':         item.get('quantity'),
            'Genérico':         gen,
            'Nivel 1':          n1,
            'Nivel 2':          n2,
            'Nivel 3':          n3,
        })

    if not rows:
        rows.append({**base,
                     'Descripción del producto/servicio': '', 'Código ONU': '',
                     'Unidad de Medida': '', 'Cantidad': None,
                     'Genérico': '', 'Nivel 1': '', 'Nivel 2': '', 'Nivel 3': ''})
    return rows


def write_excel_original_format(df, output_path):
    """Layout: filas 0-6 vacías, fila 7 headers, fila 8+ datos (= header=7 en read_excel)."""
    # Reordena y agrega columnas faltantes si vienen de un Excel antiguo
    for col in EXCEL_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[EXCEL_COLUMNS]
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Hoja1', startrow=7, index=False, header=True)


def load_cache(bootstrap_from=None):
    """
    Carga el cache CSV de licitaciones ya descargadas. Si no existe pero hay
    un Excel previo en bootstrap_from, lo importa para evitar re-fetch.
    """
    if CACHE_FILE.exists():
        df = pd.read_csv(CACHE_FILE, dtype={'Numero Adquisición': str})
        log(f'Cache: {df["Numero Adquisición"].nunique():,} licitaciones / {len(df):,} filas')
        return df

    if bootstrap_from and Path(bootstrap_from).exists():
        log(f'Bootstrap: importando {bootstrap_from} al cache...', 'step')
        df = pd.read_excel(bootstrap_from, header=7)
        df['Numero Adquisición'] = df['Numero Adquisición'].astype(str).str.strip()
        for col in EXCEL_COLUMNS:
            if col not in df.columns:
                df[col] = None
        df = df[EXCEL_COLUMNS]
        df.to_csv(CACHE_FILE, index=False)
        log(f'  → cargadas {df["Numero Adquisición"].nunique():,} licitaciones existentes', 'ok')
        return df

    return pd.DataFrame(columns=EXCEL_COLUMNS)


def normalize_dates(df):
    """Unifica las columnas de fecha a ISO 8601 UTC ('YYYY-MM-DDTHH:MM:SSZ').

    El cache acumula dos orígenes con formatos distintos en la MISMA columna:
    el crawler OCDS trae ISO con 'T' y zona ('2025-12-31T11:13:21Z'); el Excel
    bootstrap trae 'YYYY-MM-DD HH:MM:SS' (con espacio, sin zona). Esa mezcla
    rompía el parseo aguas abajo en process_data.py, que infería un único
    formato y convertía el resto a NaT en silencio — borrando dias_plazo y con
    él todos los flags de plazo de miles de licitaciones. Normalizamos al
    escribir para que la columna nunca quede mezclada. Las fechas inválidas o
    ausentes quedan como cadena vacía.
    """
    for col in DATE_COLUMNS:
        if col not in df.columns:
            continue
        parsed = pd.to_datetime(df[col], errors='coerce', utc=True, format='mixed')
        df[col] = parsed.dt.strftime('%Y-%m-%dT%H:%M:%SZ').where(parsed.notna(), '')
    return df


def save_cache(df):
    df.to_csv(CACHE_FILE, index=False)


def crawl_month(año, mes, sleep_s=0.1, limit=None, seen_ids=None, page_size=1000):
    """Descarga detalles del mes filtrando por seen_ids (incremental)."""
    seen_ids = seen_ids or set()
    log(f'Mes {año}-{mes:02d}', 'step')

    first = fetch_lista(año, mes, 0, 1)
    total = first.get('pagination', {}).get('total', 0)
    if total == 0:
        log('  (mes vacío)', 'warn')
        return []
    log(f'  Total reportado por API: {total:,}')

    tender_ids = []
    offset = 0
    while offset < total:
        chunk = fetch_lista(año, mes, offset, page_size)
        data = chunk.get('data', [])
        if not data:
            break
        for d in data:
            ocid = d.get('ocid', '')
            tid = ocid.replace('ocds-70d2nz-', '', 1)
            if tid:
                tender_ids.append(tid)
        offset += len(data)

    new_ids = [t for t in tender_ids if t not in seen_ids]
    skipped = len(tender_ids) - len(new_ids)
    log(f'  En cache: {skipped:,}  ·  Nuevas a descargar: {len(new_ids):,}')

    if limit and len(new_ids) > limit:
        log(f'  Limitado a {limit} por --limit', 'warn')
        new_ids = new_ids[:limit]

    new_rows = []
    errores = 0
    for i, tid in enumerate(new_ids, 1):
        try:
            t = fetch_tender(tid)
            new_rows.extend(tender_to_rows(t))
        except Exception as e:
            errores += 1
            log(f'  Error en {tid}: {e}', 'warn')
        if i % 50 == 0 or i == len(new_ids):
            log(f'    {i:,}/{len(new_ids):,} licitaciones nuevas · errores: {errores}')
        time.sleep(sleep_s)

    return new_rows


def main():
    today = date.today()
    parser = argparse.ArgumentParser(
        description='Crawler OCDS Mercado Público',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--año', type=int, default=today.year)
    parser.add_argument('--mes', type=int, default=today.month)
    parser.add_argument('--meses-atras', type=int, default=0,
                        help='Crawlea también los N meses previos al --mes')
    parser.add_argument('--output', type=Path,
                        default=Path.home() / 'Desktop' / 'Licitacion_Publicada.xlsx')
    parser.add_argument('--incremental', action='store_true',
                        help='Usa cache local; solo descarga IDs nuevos')
    parser.add_argument('--limit', type=int, default=None,
                        help='Limita el número de licitaciones NUEVAS por mes')
    parser.add_argument('--sleep', type=float, default=0.1)
    parser.add_argument('--auto-process', action='store_true',
                        help='Ejecuta process_data.py al terminar')
    parser.add_argument('--bootstrap-from', type=Path,
                        help='Excel existente para precargar el cache (1ra ejecución)')

    args = parser.parse_args()
    if not (1 <= args.mes <= 12):
        log('Mes inválido (1-12)', 'err'); sys.exit(2)

    # ── Cache ────────────────────────────────────────────────
    if args.incremental:
        bootstrap = args.bootstrap_from or args.output
        cached = load_cache(bootstrap_from=bootstrap)
    else:
        cached = pd.DataFrame(columns=EXCEL_COLUMNS)

    seen_ids = set(cached['Numero Adquisición'].dropna().astype(str).str.strip())

    # ── Lista de (año, mes) a crawlear ───────────────────────
    meses_a_correr = []
    cur_y, cur_m = args.año, args.mes
    for _ in range(args.meses_atras + 1):
        meses_a_correr.append((cur_y, cur_m))
        cur_m -= 1
        if cur_m == 0:
            cur_m = 12
            cur_y -= 1
    meses_a_correr.reverse()
    log(f'Meses a procesar: {meses_a_correr}', 'step')

    all_new_rows = []
    for año, mes in meses_a_correr:
        all_new_rows.extend(crawl_month(año, mes, args.sleep, args.limit, seen_ids))
        # Actualiza seen_ids in-memory para evitar repetir entre meses
        for r in all_new_rows:
            seen_ids.add(str(r.get('Numero Adquisición', '')).strip())

    # ── Merge & persist ──────────────────────────────────────
    if all_new_rows:
        merged = pd.concat([cached, pd.DataFrame(all_new_rows)], ignore_index=True)
    else:
        merged = cached

    merged['Numero Adquisición'] = merged['Numero Adquisición'].astype(str).str.strip()
    # Dedup defensivo (id + descripción del item)
    merged = merged.drop_duplicates(
        subset=['Numero Adquisición', 'Descripción del producto/servicio'],
        keep='first'
    )

    # Unifica el formato de fechas antes de persistir (ver normalize_dates).
    merged = normalize_dates(merged)

    if args.incremental:
        save_cache(merged)
        log(f'Cache actualizado: {merged["Numero Adquisición"].nunique():,} licitaciones únicas', 'ok')

    write_excel_original_format(merged, args.output)
    log(f'Excel: {args.output} ({len(merged):,} filas)', 'ok')

    nuevas = len(all_new_rows)
    log(f'Δ Esta corrida agregó {nuevas:,} filas nuevas', 'ok' if nuevas else 'info')

    if args.auto_process:
        log('Encadenando process_data.py...', 'step')
        script = Path(__file__).resolve().parent.parent / 'process_data.py'
        env = {'EXCEL_PATH': str(args.output)}
        import os as _os
        new_env = {**_os.environ, **env}
        subprocess.run([sys.executable, str(script)], check=True, env=new_env)
        log('Pipeline completa.', 'ok')


if __name__ == '__main__':
    main()
