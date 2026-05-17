# LicitaCheck

Plataforma de detección de alertas en licitaciones públicas chilenas. Procesa el catálogo de Mercado Público y marca con señales objetivas los contratos que presentan patrones susceptibles de revisión. Los organismos compradores tienen derecho a réplica.

## Requisitos

- Python 3.9+
- Un navegador moderno

## Instalación

```bash
pip install -r requirements.txt
```

## Uso local

```bash
# Levantar el frontend
python3 -m http.server 8080
# Abrir http://localhost:8080/

# Regenerar datos a partir del Excel existente (~/Desktop/Licitacion_Publicada.xlsx)
python3 process_data.py

# Descargar licitaciones nuevas y actualizar el sitio
python3 agentes/crawler.py --incremental --auto-process
```

## Estructura

```
.
├── index.html                    # Frontend (vanilla JS + vis-network)
├── process_data.py               # Pipeline Excel → JSON con flags y score
├── data/graph_data.json          # Datos procesados (committeado)
├── agentes/
│   ├── crawler.py                # Crawler OCDS (API pública de Mercado Público)
│   ├── .crawler_cache.csv        # Cache acumulado (committeado)
│   └── cl.licitacheck.crawler.plist  # launchd para macOS
├── .github/workflows/
│   └── daily-update.yml          # GitHub Actions: corre el crawler diario
├── vercel.json                   # Config de deploy en Vercel
└── requirements.txt
```

## Automatización

### En tu Mac (launchd, corre todos los días a las 7:00 am)

```bash
# 1. Copiar el plist a LaunchAgents
cp agentes/cl.licitacheck.crawler.plist ~/Library/LaunchAgents/

# 2. Cargar el agente
launchctl load ~/Library/LaunchAgents/cl.licitacheck.crawler.plist

# 3. Verificar que está activo
launchctl list | grep licitacheck

# Ver logs:
tail -f agentes/crawler.log

# Para detenerlo:
launchctl unload ~/Library/LaunchAgents/cl.licitacheck.crawler.plist
```

### En producción con GitHub + Vercel

Ver sección "Deploy" más abajo. El workflow `daily-update.yml` corre todos los días a las 11:00 UTC (≈ 7:00 am Chile), actualiza el JSON y Vercel redeploya automáticamente.

## Agente IA de supresión contextual (segundo filtro)

Después del motor de reglas duras, un agente Claude revisa cada licitación MEDIO+ y decide si alguna alerta debe ser **suprimida** porque el contexto textual la explica (emergencia sanitaria, proveedor único técnicamente justificado, continuidad de servicio esencial, corrección formal documentada).

El agente NO elimina alertas — las marca como **contextualizadas** y conserva la explicación visible en el panel. La transparencia del método se mantiene; el usuario ve qué se detectó Y por qué no aplica.

```bash
# Requiere ANTHROPIC_API_KEY en el entorno
export ANTHROPIC_API_KEY="sk-ant-..."

# Revisa todas las MEDIO+ que aún no tienen review
python3 agentes/ia_filter.py

# Smoke test (3 licitaciones)
python3 agentes/ia_filter.py --limit 3 --min-level ALTO

# Re-revisa incluso las que ya tenían review
python3 agentes/ia_filter.py --force
```

**Costo estimado:** ~USD 0.002 por licitación con Claude Haiku 4.5 + prompt caching. Una corrida completa de ~230 MEDIO+ son ~USD 0.50.

## Red flags actuales

Las descripciones son hechos verificables sobre datos públicos. Cada alerta tiene un peso que refleja su sospecha intrínseca aislada. Nada en este sitio constituye una acusación.

| Flag | Peso | Condición que dispara la alerta |
|---|---:|---|
| `clonacion_intensa` | 35 | Mismo organismo + mismo genérico + misma fecha + tipo "<100 UTM", ≥2 licitaciones |
| `plazo_critico` | 30 | <5 días corridos entre publicación y cierre |
| `descripcion_corta` | 25 | Campo Descripción <50 caracteres |
| `privada` | 22 | Tipo de licitación modalidad privada |
| `fragmentacion` | 20 | Mismo organismo+genérico ≥3 veces en el mes, <100 UTM |
| `plazo_apretado` | 18 | 5-7 días corridos entre publicación y cierre |
| `cierre_post_weekend` | 18 | Publicada viernes + cierre lunes/martes + plazo ≤12 días |
| `proveedor_recurrente` | 25 | Mismo oferente en ≥3 licitaciones del mismo organismo+genérico, habiendo otros oferentes |
| `re_publicacion` | 14 | Mismo organismo publicó otra licitación del mismo genérico en ≤14 días |
| `organismo_alto_riesgo` | 12 | El organismo tiene ≥20% de licitaciones con alerta MEDIO+ (mín. 5 licitaciones) |
| `plazo_corto` | 10 | 8-10 días corridos entre publicación y cierre |
| `plazo_limite_legal` | 10 | Plazo ≤ mínimo del Reglamento de Compras Públicas para el tipo |

| Score | Nivel |
|---|---|
| 0-30 | BAJO |
| 31-60 | MEDIO |
| 61-80 | ALTO |
| 81-100 | CRÍTICO |

## Deploy en Vercel

El proyecto es 100% estático (HTML + JSON). La actualización diaria se hace fuera de Vercel via GitHub Actions.

### Pasos

1. **Sube el repo a GitHub.** Desde la carpeta del proyecto:
   ```bash
   git init
   git add .
   git commit -m "initial"
   gh repo create licitacheck --public --source=. --push
   ```

2. **Conecta el repo a Vercel.** Desde [vercel.com/new](https://vercel.com/new) seleccioná el repo. Vercel detecta el `vercel.json` y deploya automáticamente. No requiere build command.

3. **Activá la GitHub Action.** Ya está en `.github/workflows/daily-update.yml`. La primera vez gatillala manualmente desde la pestaña *Actions* del repo → "Actualización diaria" → *Run workflow*. Después corre sola todos los días a las 11:00 UTC.

### Cómo se actualiza el sitio

```
GitHub Action (diaria)
  ├─ corre agentes/crawler.py --incremental --auto-process
  ├─ baja licitaciones nuevas del API OCDS
  ├─ regenera data/graph_data.json
  └─ commitea & pushea al repo
       ↓
Vercel detecta el push
  └─ redeploy automático (segundos)
       ↓
licitacheck.vercel.app sirve el JSON nuevo
```

### ¿Por qué no automatizar dentro de Vercel?

Vercel Functions tienen timeout de 10-60s. Un crawl completo del mes (~8000 licitaciones, ~13 min) no entra. Las Vercel Cron requieren plan pago para múltiples por día. GitHub Actions es gratis hasta 2000 min/mes (más que suficiente: ~15 min/día = 450 min/mes).

## Datos

- **Fuente:** API pública OCDS de Mercado Público (`api.mercadopublico.cl/APISOCDS/OCDS/...`)
- **Sin ticket ni autenticación** — endpoint abierto, estándar internacional Open Contracting Data Standard (OCDS)
- **Disclaimer:** las alertas son señales objetivas calculadas sobre datos públicos. No constituyen acusación de irregularidad. Los organismos pueden ejercer derecho a réplica vía `contacto@licitacheck.cl`.
