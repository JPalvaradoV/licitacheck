import pandas as pd
import json
import os
import sys

# Permite override por env var (útil cuando el crawler corre desde otra ruta)
EXCEL_PATH = os.environ.get('EXCEL_PATH', '')
if not EXCEL_PATH or not os.path.exists(EXCEL_PATH):
    EXCEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'Licitacion_Publicada.xlsx')
if not os.path.exists(EXCEL_PATH):
    EXCEL_PATH = os.path.expanduser('~/Desktop/Licitacion_Publicada.xlsx')

def clean(s):
    if pd.isna(s):
        return ''
    return str(s).strip()

def process():
    print('Cargando Excel...')
    df = pd.read_excel(EXCEL_PATH, header=7)

    df = df.drop(columns=[c for c in df.columns if str(c).startswith('Unnamed')], errors='ignore')

    df = df.rename(columns={
        'Numero Adquisición':             'id',
        'Tipo Adquisición':               'tipo',
        'Nombre Adquisición':             'nombre',
        'Descripción':                    'descripcion',
        'Organismo':                      'organismo',
        'Región Compradora':              'region',
        'Fecha Publicación':              'fecha_pub',
        'Fecha Cierre':                   'fecha_cierre',
        'Descripción del producto/servicio': 'desc_producto',
        'Código ONU':                     'codigo_onu',
        'Unidad de Medida':               'unidad',
        'Cantidad':                       'cantidad',
        'Genérico':                       'generico',
        'Nivel 1':                        'nivel1',
        'Nivel 2':                        'nivel2',
        'Nivel 3':                        'nivel3',
        'Monto Estimado':                 'monto',
        'Oferentes':                      'oferentes',
    })
    # Las columnas Monto Estimado y Oferentes solo existen cuando los datos
    # vienen del crawler OCDS. Garantizamos su presencia para no romper.
    if 'monto' not in df.columns:     df['monto'] = None
    if 'oferentes' not in df.columns: df['oferentes'] = ''
    df['oferentes'] = df['oferentes'].fillna('').astype(str)

    df = df.dropna(subset=['id'])
    df['id'] = df['id'].astype(str).str.strip()
    df['organismo']    = df['organismo'].fillna('Sin organismo').astype(str).str.strip()
    df['region']       = df['region'].fillna('Sin región').astype(str).str.strip()
    df['tipo']         = df['tipo'].fillna('').astype(str).str.strip()
    df['nombre']       = df['nombre'].fillna('').astype(str).str.strip()
    df['descripcion']  = df['descripcion'].fillna('').astype(str).str.strip()
    df['generico']     = df['generico'].fillna('').astype(str).str.strip()
    df['nivel1']       = df['nivel1'].fillna('').astype(str).str.strip()
    df['nivel2']       = df['nivel2'].fillna('').astype(str).str.strip()

    print(f'Filas válidas: {len(df)}')

    # ── Red Flag: Plazo graduado ─────────────────────────────────
    # Tres niveles según días corridos entre publicación y cierre.
    # utc=True normaliza fechas con y sin timezone (Excel oficial vs OCDS).
    # format='mixed' es CRÍTICO: el cache combina dos formatos en la misma
    # columna — ISO con 'T' (filas del crawler OCDS) y "YYYY-MM-DD HH:MM:SS"
    # (filas del Excel bootstrap). Sin 'mixed', pandas infiere un único
    # formato y convierte el resto a NaT en silencio, borrando dias_plazo
    # y con él TODOS los flags de plazo de miles de licitaciones.
    df['fecha_pub']    = pd.to_datetime(df['fecha_pub'],    errors='coerce', utc=True, format='mixed').dt.tz_localize(None)
    df['fecha_cierre'] = pd.to_datetime(df['fecha_cierre'], errors='coerce', utc=True, format='mixed').dt.tz_localize(None)
    df['dias_plazo']   = (df['fecha_cierre'] - df['fecha_pub']).dt.days

    def plazo_nivel(x):
        if pd.isna(x): return ''
        if x < 5:  return 'critico'
        if x < 8:  return 'apretado'
        if x < 11: return 'corto'
        return ''

    df['plazo_nivel'] = df['dias_plazo'].apply(plazo_nivel)
    df['flag_plazo_critico']  = df['plazo_nivel'] == 'critico'
    df['flag_plazo_apretado'] = df['plazo_nivel'] == 'apretado'
    df['flag_plazo_corto']    = df['plazo_nivel'] == 'corto'

    # ── Red Flag: Plazo al límite legal ──────────────────────────
    # Acepta dos formatos de 'tipo': Excel oficial y API OCDS.
    #   Excel: 'Licitación pública inferior a 100 UTM'
    #   OCDS:  'Licitación Pública Menor a 100 UTM (L1)'
    def min_legal_plazo(tipo):
        t = tipo.lower()
        if 'inferior a 100 utm' in t or 'menor a 100 utm' in t or '(l1)' in t:
            return 5
        if ('100 utm' in t and ('1.000 utm' in t or '1000 utm' in t)) or '(le)' in t:
            return 10
        if (('1.000 utm' in t or '1000 utm' in t) and ('2.000 utm' in t or '2000 utm' in t)) or '(lp)' in t:
            return 20
        if 'mayor a 5000 utm' in t or 'superior a 2000 utm' in t or 'mayor a 2000 utm' in t or '(lr)' in t:
            return 30
        return None

    df['min_legal'] = df['tipo'].apply(min_legal_plazo)
    df['flag_plazo_limite_legal'] = (
        df['min_legal'].notna() &
        df['dias_plazo'].notna() &
        (df['dias_plazo'] <= df['min_legal']) &
        ~df['flag_plazo_critico'] &
        ~df['flag_plazo_apretado'] &
        ~df['flag_plazo_corto']
    )

    # ── Red Flag: Descripción ultra-corta ────────────────────────
    # Bases con descripción <50 caracteres dan discrecionalidad pura
    # al evaluador. Clásica táctica para favorecer a un proveedor.
    df['flag_descripcion_corta'] = df['descripcion'].str.len() < 50

    # ── Red Flag: Cierre post-weekend con plazo corto ────────────
    # Publicada viernes + cierre lunes/martes + plazo ≤ 12 días.
    # El fin de semana intermedio "mata" días hábiles disponibles
    # para preparar la oferta, aunque el plazo calendario parezca normal.
    df['flag_cierre_post_weekend'] = (
        (df['fecha_pub'].dt.weekday == 4) &
        (df['fecha_cierre'].dt.weekday.isin([0, 1])) &
        (df['dias_plazo'] <= 12) &
        df['dias_plazo'].notna()
    )

    # ── Red Flag: Licitación Privada ─────────────────────────────
    df['flag_privada'] = df['tipo'].str.contains('Privada', case=False, na=False)

    # ── Red Flags de partición (clonación intra-día y fragmentación mensual) ──
    # Importante: el Excel tiene una fila por ítem, así que para contar
    # licitaciones únicas hay que deduplicar por id antes de agrupar.
    df['mes_pub']          = df['fecha_pub'].dt.to_period('M').astype(str)
    df['dia_pub']          = df['fecha_pub'].dt.date.astype(str)
    # Reconoce ambas variantes del catálogo de tipos:
    #   Excel oficial: "inferior a 100 UTM"
    #   API OCDS:      "Menor a 100 UTM (L1)"
    df['es_menor_100utm']  = (
        df['tipo'].str.contains('inferior a 100 UTM', case=False, na=False) |
        df['tipo'].str.contains('Menor a 100 UTM',    case=False, na=False) |
        df['tipo'].str.contains(r'\(L1\)',            case=False, na=False, regex=True)
    )

    df_lic = df.drop_duplicates(subset=['id'])[
        ['id','organismo','generico','mes_pub','dia_pub','fecha_pub','es_menor_100utm']
    ].copy()
    lic_menor = df_lic[df_lic['es_menor_100utm'] & (df_lic['generico'] != '')]

    # Clonación intra-día: ≥2 licitaciones mismo día + mismo org + mismo genérico + <100 UTM
    clon = (lic_menor
            .groupby(['organismo', 'generico', 'dia_pub'])
            .size()
            .reset_index(name='clon_count'))
    df = df.merge(clon, on=['organismo', 'generico', 'dia_pub'], how='left')
    df['clon_count'] = df['clon_count'].fillna(0)
    df['flag_clonacion_intensa'] = df['es_menor_100utm'] & (df['clon_count'] >= 2)

    # Fragmentación mensual: ≥3 licitaciones únicas mismo mes + org + genérico + <100 UTM
    frag = (lic_menor
            .groupby(['organismo', 'generico', 'mes_pub'])
            .size()
            .reset_index(name='frag_count'))
    df = df.merge(frag, on=['organismo', 'generico', 'mes_pub'], how='left')
    df['frag_count'] = df['frag_count'].fillna(0)
    # Excluyente con clonación intra-día (mismo patrón ya contado más fuerte)
    df['flag_fragmentacion'] = (
        df['es_menor_100utm'] &
        (df['frag_count'] >= 3) &
        ~df['flag_clonacion_intensa']
    )

    # ── Red Flag: Oferente recurrente ────────────────────────────
    # Mismo oferente aparece en ≥3 licitaciones del mismo organismo+genérico,
    # existiendo además otros oferentes en al menos una de ellas.
    # Captura concentración sospechosa de proveedores cuando hay competencia
    # nominal pero el mismo nombre se repite consistentemente.
    df_lic_ofer = df_lic.merge(
        df.drop_duplicates(subset=['id'])[['id','oferentes']],
        on='id', how='left'
    ).copy()
    df_lic_ofer['oferentes'] = df_lic_ofer['oferentes'].fillna('').astype(str)

    # Construir { (org, generico, oferente) -> count_licitaciones }
    from collections import defaultdict
    ofer_count = defaultdict(set)         # (org, gen, ofer) -> set de licitaciones
    org_gen_ids = defaultdict(set)        # (org, gen) -> set de licitaciones
    org_gen_ofer_total = defaultdict(set) # (org, gen) -> set de oferentes únicos

    for _, r in df_lic_ofer.iterrows():
        if not r['oferentes'] or not r['generico']:
            continue
        oferentes = [o.strip() for o in r['oferentes'].split('|') if o.strip()]
        if not oferentes:
            continue
        key_og = (r['organismo'], r['generico'])
        org_gen_ids[key_og].add(r['id'])
        for o in oferentes:
            ofer_count[(r['organismo'], r['generico'], o)].add(r['id'])
            org_gen_ofer_total[key_og].add(o)

    # Identificar (org, generico, oferente) recurrentes: ≥3 licitaciones
    recurrentes = set()
    for (org, gen, ofer), lic_ids in ofer_count.items():
        if (len(lic_ids) >= 3 and
            len(org_gen_ids[(org, gen)]) >= 3 and
            len(org_gen_ofer_total[(org, gen)]) >= 2):
            recurrentes.update(lic_ids)

    df['flag_proveedor_recurrente'] = df['id'].isin(recurrentes)

    # ── Red Flag: Re-publicación sospechosa ──────────────────────
    # Mismo organismo + mismo genérico publicado de nuevo en ≤14 días
    # (distinto día, sino sería clonación intra-día).
    lic_sorted = (df_lic[df_lic['generico'] != '']
                  .sort_values(['organismo', 'generico', 'fecha_pub'])
                  .copy())
    lic_sorted['fecha_prev'] = lic_sorted.groupby(['organismo','generico'])['fecha_pub'].shift(1)
    lic_sorted['dias_desde_prev'] = (lic_sorted['fecha_pub'] - lic_sorted['fecha_prev']).dt.days
    lic_sorted['flag_re_publicacion'] = (
        lic_sorted['dias_desde_prev'].notna() &
        (lic_sorted['dias_desde_prev'] > 0) &
        (lic_sorted['dias_desde_prev'] <= 14)
    )
    df = df.merge(
        lic_sorted[['id', 'flag_re_publicacion']],
        on='id', how='left'
    )
    df['flag_re_publicacion'] = df['flag_re_publicacion'].fillna(False)

    # ── Score 0-100 ──────────────────────────────────────────────
    # Los tres niveles de plazo son mutuamente excluyentes.
    # ── Rúbrica de scoring ───────────────────────────────────────
    # Cada peso refleja la SOSPECHA INTRÍNSECA de la señal aislada,
    # no su contribución a un umbral. Lo justo es lo justo: una bandera
    # roja por sí sola dice algo; combinarlas potencia la evidencia.
    df['score'] = (
        df['flag_clonacion_intensa'].astype(int)     * 35 +  # patrón intencional clarísimo, sola ya MEDIO
        df['flag_plazo_critico'].astype(int)         * 30 +  # tiempo físicamente irreal
        df['flag_descripcion_corta'].astype(int)     * 25 +  # bases ambiguas = discrecionalidad pura
        df['flag_proveedor_recurrente'].astype(int)  * 25 +  # mismo oferente reiterado con competencia nominal
        df['flag_privada'].astype(int)               * 22 +  # sospechoso pero existen casos legítimos
        df['flag_fragmentacion'].astype(int)         * 20 +  # patrón mensual menos concentrado
        df['flag_plazo_apretado'].astype(int)        * 18 +  # estrecho pero técnicamente posible
        df['flag_cierre_post_weekend'].astype(int)   * 18 +  # consume tiempo hábil real
        df['flag_re_publicacion'].astype(int)        * 14 +  # casos legítimos existen (corrección)
        df['flag_plazo_limite_legal'].astype(int)    * 10 +  # borde legal, débil aislada
        df['flag_plazo_corto'].astype(int)           * 10    # señal débil aislada, importa al sumar
    ).clip(upper=100)

    def risk_level(s):
        if s >= 81: return 'CRÍTICO'
        if s >= 61: return 'ALTO'
        if s >= 31: return 'MEDIO'
        return 'BAJO'

    df['riesgo'] = df['score'].apply(risk_level)

    # ── Organismo IDs ─────────────────────────────────────────────
    org_names = sorted(df['organismo'].unique())
    org_id_map = {name: f'ORG_{i:04d}' for i, name in enumerate(org_names)}
    df['organismo_id'] = df['organismo'].map(org_id_map)

    # ── Deduplicar por id de licitación ───────────────────────────
    # El Excel trae una fila por ítem (producto/servicio) dentro de la
    # licitación. Agrupamos: flags se combinan con OR, géneros se
    # acumulan en lista. Score se recalcula sobre los flags agregados.
    print(f'Filas pre-agregación: {len(df):,}')

    def first_non_empty(s):
        for v in s:
            if pd.notna(v) and str(v).strip():
                return v
        return ''

    grouped = df.groupby('id', as_index=False).agg(
        tipo                     = ('tipo', first_non_empty),
        nombre                   = ('nombre', first_non_empty),
        descripcion              = ('descripcion', first_non_empty),
        organismo                = ('organismo', 'first'),
        organismo_id             = ('organismo_id', 'first'),
        region                   = ('region', 'first'),
        fecha_pub                = ('fecha_pub', 'first'),
        fecha_cierre             = ('fecha_cierre', 'first'),
        dias_plazo               = ('dias_plazo', 'first'),
        nivel1                   = ('nivel1', first_non_empty),
        nivel2                   = ('nivel2', first_non_empty),
        genericos                = ('generico', lambda s: sorted({v for v in s if v})),
        monto                    = ('monto', 'max'),
        oferentes                = ('oferentes', first_non_empty),
        flag_plazo_critico       = ('flag_plazo_critico', 'any'),
        flag_plazo_apretado      = ('flag_plazo_apretado', 'any'),
        flag_plazo_corto         = ('flag_plazo_corto', 'any'),
        flag_plazo_limite_legal  = ('flag_plazo_limite_legal', 'any'),
        flag_cierre_post_weekend = ('flag_cierre_post_weekend', 'any'),
        flag_descripcion_corta   = ('flag_descripcion_corta', 'any'),
        flag_privada             = ('flag_privada', 'any'),
        flag_clonacion_intensa   = ('flag_clonacion_intensa', 'any'),
        flag_fragmentacion       = ('flag_fragmentacion', 'any'),
        flag_re_publicacion      = ('flag_re_publicacion', 'any'),
        flag_proveedor_recurrente= ('flag_proveedor_recurrente', 'any'),
    )

    # ── Score base (sin organismo_alto_riesgo) ───────────────────
    def calc_score_base(g):
        return (
            g['flag_clonacion_intensa'].astype(int)     * 35 +
            g['flag_plazo_critico'].astype(int)         * 30 +
            g['flag_descripcion_corta'].astype(int)     * 25 +
            g['flag_proveedor_recurrente'].astype(int)  * 25 +
            g['flag_privada'].astype(int)               * 22 +
            g['flag_fragmentacion'].astype(int)         * 20 +
            g['flag_plazo_apretado'].astype(int)        * 18 +
            g['flag_cierre_post_weekend'].astype(int)   * 18 +
            g['flag_re_publicacion'].astype(int)        * 14 +
            g['flag_plazo_limite_legal'].astype(int)    * 10 +
            g['flag_plazo_corto'].astype(int)           * 10
        ).clip(upper=100)

    grouped['score_base'] = calc_score_base(grouped)

    # ── Red Flag: Organismo de alto riesgo ───────────────────────
    # Si un organismo tiene ≥20% de sus licitaciones marcadas MEDIO+
    # y al menos 5 licitaciones en total, todas sus licitaciones
    # ganan este flag. Captura patrones sistémicos, no aislados.
    grouped['_medio_o_mas'] = grouped['score_base'] >= 31
    org_stats_pre = grouped.groupby('organismo_id').agg(
        count_total=('id', 'count'),
        count_medio=('_medio_o_mas', 'sum'),
    )
    org_stats_pre['pct_marcadas'] = org_stats_pre['count_medio'] / org_stats_pre['count_total']
    orgs_alto_riesgo = set(org_stats_pre[
        (org_stats_pre['count_total'] >= 5) &
        (org_stats_pre['pct_marcadas'] >= 0.20)
    ].index)
    grouped['flag_organismo_alto_riesgo'] = grouped['organismo_id'].isin(orgs_alto_riesgo)

    # ── Score final (con organismo_alto_riesgo, peso 12) ─────────
    grouped['score'] = (
        grouped['score_base'] +
        grouped['flag_organismo_alto_riesgo'].astype(int) * 12
    ).clip(upper=100)
    grouped['riesgo'] = grouped['score'].apply(risk_level)

    print(f'Organismos de alto riesgo identificados: {len(orgs_alto_riesgo)}')

    print(f'Licitaciones únicas: {len(grouped):,}')

    # ── Build licitaciones list ───────────────────────────────────
    licitaciones = []
    for _, r in grouped.iterrows():
        flags = []
        if r['flag_plazo_critico']:       flags.append('plazo_critico')
        if r['flag_plazo_apretado']:      flags.append('plazo_apretado')
        if r['flag_plazo_corto']:         flags.append('plazo_corto')
        if r['flag_plazo_limite_legal']:  flags.append('plazo_limite_legal')
        if r['flag_cierre_post_weekend']: flags.append('cierre_post_weekend')
        if r['flag_descripcion_corta']:   flags.append('descripcion_corta')
        if r['flag_privada']:             flags.append('privada')
        if r['flag_clonacion_intensa']:   flags.append('clonacion_intensa')
        if r['flag_fragmentacion']:       flags.append('fragmentacion')
        if r['flag_re_publicacion']:      flags.append('re_publicacion')
        if r['flag_proveedor_recurrente']: flags.append('proveedor_recurrente')
        if r['flag_organismo_alto_riesgo']: flags.append('organismo_alto_riesgo')

        oferentes_list = [s.strip() for s in str(r.get('oferentes','') or '').split('|') if s.strip()]
        licitaciones.append({
            'id':           r['id'],
            'nombre':       r['nombre'][:120],
            'descripcion':  r['descripcion'][:400],
            'tipo':         r['tipo'],
            'organismo':    r['organismo'],
            'organismo_id': r['organismo_id'],
            'region':       r['region'],
            'fecha_pub':    r['fecha_pub'].strftime('%Y-%m-%d')  if pd.notna(r['fecha_pub'])    else '',
            'fecha_cierre': r['fecha_cierre'].strftime('%Y-%m-%d') if pd.notna(r['fecha_cierre']) else '',
            'dias_plazo':   int(r['dias_plazo']) if pd.notna(r['dias_plazo']) else None,
            'generico':     r['genericos'][0] if r['genericos'] else '',
            'genericos':    r['genericos'],
            'nivel1':       r['nivel1'],
            'nivel2':       r['nivel2'],
            'monto':        float(r['monto']) if pd.notna(r.get('monto')) else None,
            'oferentes':    oferentes_list,
            'score':        int(r['score']),
            'riesgo':       r['riesgo'],
            'flags':        flags,
        })

    # ── Build organismos list (basado en licitaciones únicas) ─────
    org_stats = (grouped.groupby(['organismo', 'organismo_id'])
                   .agg(count=('id','count'),
                        score_promedio=('score','mean'),
                        score_max=('score','max'),
                        monto_total=('monto', lambda s: float(s.dropna().sum())),
                        monto_count=('monto', lambda s: int(s.dropna().shape[0])))
                   .reset_index())

    organismos = []
    for _, r in org_stats.iterrows():
        organismos.append({
            'id':             r['organismo_id'],
            'nombre':         r['organismo'],
            'count':          int(r['count']),
            'score_promedio': round(float(r['score_promedio']), 1),
            'score_max':      int(r['score_max']),
            'monto_total':    float(r['monto_total']) if r['monto_total'] else 0.0,
            'monto_count':    int(r['monto_count']),
        })

    # ── Stats (basado en licitaciones únicas) ─────────────────────
    rc = grouped['riesgo'].value_counts().to_dict()
    stats = {
        'total':   len(grouped),
        'critico': int(rc.get('CRÍTICO', 0)),
        'alto':    int(rc.get('ALTO',    0)),
        'medio':   int(rc.get('MEDIO',   0)),
        'bajo':    int(rc.get('BAJO',    0)),
        'regiones': sorted(
            [{'nombre': k, 'count': int(v)} for k, v in grouped['region'].value_counts().items()],
            key=lambda x: x['nombre']),
        'tipos': sorted(
            [{'nombre': k, 'count': int(v)} for k, v in grouped['tipo'].value_counts().items() if k],
            key=lambda x: -x['count']),
        'nivel1_top': {k: int(v) for k, v in grouped['nivel1'].value_counts().head(15).items()},
    }

    out = {'licitaciones': licitaciones, 'organismos': organismos, 'stats': stats}
    out_path = os.path.join(os.path.dirname(__file__), 'data', 'graph_data.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)

    print(f'\n✅ JSON generado: {out_path}')
    print(f'   Total : {stats["total"]:,}')
    print(f'   CRÍTICO: {stats["critico"]:,}  ALTO: {stats["alto"]:,}  MEDIO: {stats["medio"]:,}  BAJO: {stats["bajo"]:,}')
    print(f'   Organismos: {len(organismos):,}')

if __name__ == '__main__':
    process()
