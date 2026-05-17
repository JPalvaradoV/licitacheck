"""
LicitaCheck — Agente IA de supresión contextual

Segundo filtro: lee data/graph_data.json, evalúa con Claude cada licitación
de nivel MEDIO o superior, y decide si alguna alerta debe ser SUPRIMIDA
por contexto legítimo (emergencia sanitaria, proveedor único, continuidad
de servicio, corrección formal).

El score numérico y el nivel siguen calculándose con todas las alertas,
pero el frontend distingue visualmente las alertas activas vs suprimidas
y muestra la justificación.

Uso:
    export ANTHROPIC_API_KEY="sk-ant-..."
    python3 agentes/ia_filter.py
    python3 agentes/ia_filter.py --force      # re-revisa incluso ya analizadas
    python3 agentes/ia_filter.py --limit 5    # smoke test

Política:
- Solo revisa licitaciones MEDIO+ (≥31 score). Ignora BAJO.
- Salta licitaciones que ya tienen agent_review (a menos que --force).
- Cachea el system prompt — significa que las llamadas 2..N son ~90%
  más baratas. Ver https://docs.anthropic.com/.../prompt-caching
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from anthropic import Anthropic
except ImportError:
    print('Falta el paquete `anthropic`. Corré: pip install anthropic', file=sys.stderr)
    sys.exit(1)

JSON_PATH = Path(__file__).resolve().parent.parent / 'data' / 'graph_data.json'

MODEL = 'claude-haiku-4-5-20251001'  # rápido y barato; suficiente para clasificación

SYSTEM_PROMPT = """Sos un clasificador de CONTEXTO TEXTUAL en licitaciones públicas chilenas.

Tu trabajo es identificar, leyendo SOLO la descripción y el organismo, si hay
mención explícita de uno de estos contextos que el reglamento o la práctica
administrativa contemplan como motivos legítimos para procesos especiales:

1. EMERGENCIA / URGENCIA / DESASTRE
   El texto menciona explícitamente: emergencia, urgencia sanitaria, catástrofe,
   desastre, pandemia, incendio, terremoto, alerta declarada, brote epidémico.

2. PROVEEDOR ÚNICO
   El texto menciona explícitamente compatibilidad técnica con marca/modelo,
   licencia propietaria, repuesto específico, medicamento de molécula única,
   o equipamiento con un solo distribuidor identificado en Chile.

3. CONTINUIDAD DE SERVICIO
   El texto identifica explícitamente que es renovación, prórroga o tramo
   de un servicio continuo previo (vigilancia, agua, electricidad,
   alimentación de pacientes, transporte escolar).

4. CORRECCIÓN FORMAL
   El texto dice explícitamente que es errata, modificación de bases,
   ajuste de cantidades, o reapertura por aclaración técnica.

CRITERIO DE SUPRESIÓN
- Suprimís UNA alerta solo si el texto contiene la mención explícita
  correspondiente al contexto.
- Si NO hay mención explícita, NO suprimís y no inventás contexto.

REGLAS DE LENGUAJE (la explicación se publica y los organismos tienen
derecho a réplica — no podemos emitir juicios):

PROHIBIDO usar palabras o frases con carga evaluativa o acusatoria:
- 'se justifica' / 'no se justifica'
- 'es válido' / 'son válidas indicadores'
- 'patrón de riesgo' / 'patrón de irregularidad' / 'sospecha'
- 'mantienen las alertas' / 'persisten las alertas' / 'alertas válidas'
- 'no se justifica el plazo'
- 'extremadamente corta' / 'sin detalles' / 'genérica' / 'inadecuada'
- 'incumple' / 'irregular' / 'falta de diligencia' / 'falta de preparación'

PERMITIDO y RECOMENDADO:
- 'podría' en lugar de 'tiene' al hablar del perfil del organismo
- 'la descripción no menciona X' (factual)
- 'el plazo entre publicación y cierre es de N días' (factual)
- 'la descripción tiene N caracteres' (factual)
- 'el organismo es un servicio de salud' (factual, descriptivo)

ESTRUCTURA DE LA EXPLICACIÓN (máx 2 oraciones, ~280 caracteres):
  Oración 1: qué tipo de organismo es (factual) + qué PODRÍA implicar (condicional).
  Oración 2: qué menciona o NO menciona la descripción (factual).

EJEMPLOS DE EXPLICACIÓN CORRECTA:
  OK: "El organismo es un Servicio de Salud, que podría requerir compras urgentes por su rol. La descripción no menciona emergencia, urgencia ni desastre."
  OK: "El organismo es un hospital público. La descripción se refiere a reposición de techumbre en la Central de Alimentación, que podría afectar la continuidad de servicios sanitarios."
  OK: "La descripción tiene 35 caracteres y no incluye referencia explícita a bases adjuntas, proveedor único ni emergencia."

EJEMPLOS DE EXPLICACIÓN INCORRECTA (NO USAR):
  NO: "Las alertas son válidas indicadores de patrón de riesgo." (juicio)
  NO: "No se justifica el plazo corto." (juicio)
  NO: "La descripción es extremadamente corta sin detalles." (adjetivo evaluativo)
  NO: "Mantiene patrón de irregularidad." (acusación)

FORMATO DE RESPUESTA: SOLO JSON válido, sin markdown ni texto adicional.

{
  "flags_suprimidos": ["<flag1>"],
  "contexto_detectado": "emergencia_sanitaria" | "proveedor_unico" | "continuidad_servicio" | "correccion_formal" | "ninguno",
  "explicacion": "1 a 2 oraciones factuales y condicionales, máx 280 caracteres.",
  "confianza": 0.0
}"""


def build_user_prompt(l):
    flags_label = ', '.join(l.get('flags', []))
    desc = (l.get('descripcion') or '')[:600]
    return f"""Licitación a evaluar:

ID:           {l.get('id')}
Organismo:    {l.get('organismo')}
Región:       {l.get('region')}
Tipo:         {l.get('tipo')}
Categoría:    {l.get('nivel1')} › {l.get('nivel2')}
Genérico:     {l.get('generico')}
Plazo:        {l.get('dias_plazo')} días
Fecha pub.:   {l.get('fecha_pub')}
Fecha cierre: {l.get('fecha_cierre')}
Nombre:       {l.get('nombre')}
Descripción:  {desc}

Flags disparados: {flags_label}

Devolvé el JSON según las reglas."""


def review_one(client, l):
    """Llama a Claude para una licitación. Retorna dict con la review o None si error."""
    user = build_user_prompt(l)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            temperature=0.2,  # alta consistencia, baja variabilidad estilística
            system=[{
                'type': 'text',
                'text': SYSTEM_PROMPT,
                'cache_control': {'type': 'ephemeral'},
            }],
            messages=[{'role': 'user', 'content': user}],
        )
        text = ''.join(b.text for b in resp.content if hasattr(b, 'text')).strip()
        # A veces el modelo envuelve en markdown — limpiamos defensivamente
        if text.startswith('```'):
            text = text.split('```', 2)[1]
            if text.startswith('json'):
                text = text[4:]
            text = text.strip().rstrip('`').strip()
        review = json.loads(text)
        # Sanity checks
        if not isinstance(review.get('flags_suprimidos'), list):
            review['flags_suprimidos'] = []
        # Solo suprimimos flags que efectivamente estaban en la licitación
        active = set(l.get('flags', []))
        review['flags_suprimidos'] = [f for f in review['flags_suprimidos'] if f in active]
        review['_modelo'] = MODEL
        # Tokens reportados para costo (si están)
        u = getattr(resp, 'usage', None)
        if u:
            review['_tokens_in']  = u.input_tokens
            review['_tokens_out'] = u.output_tokens
            review['_cache_read'] = getattr(u, 'cache_read_input_tokens', 0) or 0
        return review
    except json.JSONDecodeError as e:
        return {'flags_suprimidos': [], 'contexto_detectado': 'ninguno',
                'explicacion': f'Respuesta no parseable del modelo: {e}',
                'confianza': 0.0, '_error': True}
    except Exception as e:
        return {'flags_suprimidos': [], 'contexto_detectado': 'ninguno',
                'explicacion': f'Error de API: {e}',
                'confianza': 0.0, '_error': True}


def main():
    parser = argparse.ArgumentParser(description='Filtro IA contextual sobre data/graph_data.json')
    parser.add_argument('--input',  type=Path, default=JSON_PATH)
    parser.add_argument('--output', type=Path, default=JSON_PATH)
    parser.add_argument('--force',  action='store_true', help='Re-revisa licitaciones que ya tienen agent_review')
    parser.add_argument('--limit',  type=int, default=None, help='Solo revisa N licitaciones (smoke test)')
    parser.add_argument('--sleep',  type=float, default=0.2, help='Pausa entre llamadas (seg)')
    parser.add_argument('--min-level', choices=['MEDIO','ALTO','CRÍTICO'], default='MEDIO',
                        help='Nivel mínimo a revisar (default: MEDIO)')
    args = parser.parse_args()

    if not os.environ.get('ANTHROPIC_API_KEY'):
        print('Falta ANTHROPIC_API_KEY en el entorno.', file=sys.stderr); sys.exit(2)

    print(f'· Cargando {args.input}')
    with open(args.input, 'r', encoding='utf-8') as f:
        data = json.load(f)

    levels_ok = {'CRÍTICO': {'CRÍTICO'},
                 'ALTO':    {'CRÍTICO','ALTO'},
                 'MEDIO':   {'CRÍTICO','ALTO','MEDIO'}}[args.min_level]

    candidates = [l for l in data['licitaciones']
                  if l.get('riesgo') in levels_ok and
                  (args.force or 'agent_review' not in l)]
    print(f'· Candidatas (nivel {args.min_level}+): {len(candidates):,}')
    if args.limit:
        candidates = candidates[:args.limit]
        print(f'· Limitadas a {len(candidates):,} por --limit')

    if not candidates:
        print('· Nada por hacer.'); return

    client = Anthropic()
    suprimidas = 0
    errores = 0
    tot_in = tot_out = tot_cache = 0

    for i, l in enumerate(candidates, 1):
        rev = review_one(client, l)
        l['agent_review'] = rev
        if rev.get('_error'): errores += 1
        if rev.get('flags_suprimidos'): suprimidas += 1
        tot_in    += rev.get('_tokens_in', 0)
        tot_out   += rev.get('_tokens_out', 0)
        tot_cache += rev.get('_cache_read', 0)
        if i % 10 == 0 or i == len(candidates):
            print(f'  {i:,}/{len(candidates):,} · suprimidas {suprimidas} · errores {errores}')
        time.sleep(args.sleep)

    # Estimación de costo aproximada (Haiku 4.5: $1/Mtok input, $5/Mtok output, $0.10/M cache hits)
    cost = (tot_in - tot_cache) * 1.0/1e6 + tot_cache * 0.1/1e6 + tot_out * 5.0/1e6
    print(f'\n· Tokens — in: {tot_in:,}  out: {tot_out:,}  cached: {tot_cache:,}')
    print(f'· Costo estimado: USD {cost:.4f}')

    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    print(f'✓ Guardado {args.output}')


if __name__ == '__main__':
    main()
