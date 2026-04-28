# scraprop

Scraper diario de **Zonaprop** y **Argenprop** para departamentos de **3 ambientes** en **Caballito** y **Villa Crespo**, con precio entre **USD 100.000 y USD 170.000**.

Detecta listings nuevos del día y filtra republicaciones (mismo inmueble re-subido con otro ID o por la otra inmobiliaria) usando un fingerprint de dirección + m² + ambientes + barrio + precio (en buckets de USD 5.000).

## Cómo funciona

- `scraper/argenprop.py` — HTTP plano con `requests`. Recorre pages 1..N para `caballito` y `villa-crespo` por separado.
- `scraper/zonaprop.py` — usa `curl_cffi` con impersonación TLS de Chrome para pasar el escudo de Cloudflare. Una sola URL combinada para ambos barrios, ordenada por más reciente primero.
- `scraper/storage.py` — SQLite (`data/listings.db`). Cada listing tiene un `listing_id` único (`<source>:<external_id>`) y un `fingerprint` para detectar republicaciones.
- `run_daily.py` — orquesta todo, escribe `reports/YYYY-MM-DD.md` y `.json`, y un log en `logs/YYYY-MM-DD.log`.

### Fingerprint de republicación

```
sha1( normalize_address(addr) | m2 | ambientes | dorms | barrio | (price // 5000) )
```

- Si llega un `listing_id` nuevo cuyo fingerprint **ya existe** en la DB con otro `listing_id`, se marca `is_republish=1` y `republish_of=<id_original>`. **No aparece en la sección "Nuevos del día"** del reporte; aparece aparte en "Republicaciones (NO enviadas)".
- Si el `listing_id` ya existía, sólo se actualiza `last_seen` y `times_seen`. No se reporta.
- Si el `listing_id` es nuevo y el fingerprint nunca se vio, se reporta como **nuevo**.

## Instalación

```powershell
cd C:\Users\augus\OneDrive\Documentos\scraprop
python -m pip install -r requirements.txt
```

`curl_cffi` ya viene con su propia build de libcurl, no requiere browsers.

## Uso

```powershell
# Corrida completa (ambos sitios)
python run_daily.py

# Sólo un sitio
python run_daily.py --site argenprop
python run_daily.py --site zonaprop

# Smoke test (cap a 2 páginas por sitio)
python run_daily.py --max-pages 2
```

### Outputs

- `reports/YYYY-MM-DD.md` — reporte legible (markdown) con tabla de nuevos del día.
- `reports/YYYY-MM-DD.json` — mismo contenido en JSON, listo para integrarlo a otra herramienta (mail, Telegram, Slack, etc.).
- `logs/YYYY-MM-DD.log` — log completo de la corrida.
- `data/listings.db` — base de datos SQLite. Persiste entre corridas; **no la borres**.
- `viewer/data.json` — snapshot completo de listings activos para el visor web.

## Visor web

El visor es un **HTML estático** (`viewer/index.html`) que lee `viewer/data.json`.

```powershell
# Forma 1: doble click en el archivo
start viewer\index.html

# Forma 2 (recomendada por CORS de fetch en file://): server local
cd viewer
python -m http.server 8000
# y abrí http://localhost:8000
```

Filtros disponibles: texto libre, precio min/max, m² min/max, barrio, fuente, orientación, antigüedad min/max, **sólo nuevos del día**. Ordenamiento por más nuevos / precio / m² / USD por m². Cada card muestra precio, expensas, dirección, barrio, m², ambientes, dormitorios, antigüedad, orientación, descripción, USD/m² calculado, link al aviso original, y una etiqueta `nuevo` para listings con `first_seen == hoy`.

> El visor lee siempre el último snapshot. Cada corrida diaria lo regenera automáticamente.

## Deploy en la nube (recomendado): GitHub Actions + GitHub Pages

Ya viene incluido `.github/workflows/scrape.yml`. Hace todo lo siguiente automáticamente:

- Cron `0 10-23 * * *` UTC = **7:00 a 20:00 ART, una vez por hora** (14 corridas por día).
- Restaura el estado previo (DB, snapshots) desde la rama `data` del repo.
- Corre `python run_daily.py`.
- Pushea el nuevo estado a la rama `data` (la DB no rompe nada estando ahí).
- Publica el visor (`viewer/`) en GitHub Pages.

### ⚠ Limitación con IPs de data center

Los runners de GitHub Actions usan IPs de data center que **ambos sitios bloquean**:
- Argenprop devuelve `HTTP 202` (anti-bot screen)
- Zonaprop devuelve `HTTP 403` (Cloudflare)

Solución: rutear los requests por **ScraperAPI** (free tier 5.000 reqs/mes), que asigna una IP residencial argentina y pasa los dos sitios.

1. Andá a https://www.scraperapi.com/, hacé sign-up con email (no requiere tarjeta).
2. Copiá tu API key del dashboard.
3. En el repo de GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `SCRAPER_API_KEY`
   - Value: (pegá la key)
4. Listo — el workflow detecta la presencia del secret y rutea automáticamente. Sin secret corre directo (útil sólo en tu PC).

El workflow corre con `--max-pages 3` por sitio para no consumir más de ~84 reqs/día (~2.500/mes), bien dentro de los 5.000 free.

### Pasos para subirlo

```powershell
cd C:\Users\augus\OneDrive\Documentos\scraprop
git init -b main
git add .
git commit -m "scraprop: scraper diario con visor"
gh repo create scraprop --public --source=. --push
```

(Si no tenés `gh`: creá el repo desde la web y `git remote add origin <url> && git push -u origin main`.)

Después, en la web del repo:

1. **Settings → Pages → Build and deployment → Source: GitHub Actions** (es lo que usa `actions/deploy-pages`).
2. **Settings → Actions → General → Workflow permissions → Read and write permissions**.
3. **Actions → scrape-hourly → Run workflow** para hacer la primera corrida sin esperar al cron.

Después de la primera corrida, el visor queda en `https://<tu-usuario>.github.io/scraprop/` y se actualiza solo cada hora entre las 7 y las 20.

### Notas del workflow

- La rama `data` guarda `data/listings.db`, `viewer/data.json`, `viewer/data.js`, `reports/`, `logs/`. Tu rama `main` queda limpia con sólo el código.
- Si querés cambiar la franja horaria, editá la línea `cron:` (recordá que es **UTC**: ART = UTC-3).
- GitHub Actions free tier: 2.000 min/mes en repos privados (gratis ilimitado en públicos). Cada corrida son ~4-5 min → ~70 min/día → cabe holgado incluso en privado.

## Programar la ejecución local (Windows)

### Opción 1 — Programador de tareas (recomendado)

Abrí **Programador de tareas** (Task Scheduler) → **Crear tarea básica**.

- Nombre: `scraprop daily`
- Desencadenador: Diariamente a (por ejemplo) 08:00
- Acción: **Iniciar un programa**
  - Programa/script: `python`
  - Argumentos: `run_daily.py`
  - Iniciar en: `C:\Users\augus\OneDrive\Documentos\scraprop`

O por CLI:

```powershell
schtasks /create /tn "scraprop daily" /tr "python C:\Users\augus\OneDrive\Documentos\scraprop\run_daily.py" /sc daily /st 08:00 /f
```

Para que corra aunque no estés logueado, en el GUI tildá _"Ejecutar tanto si el usuario inició sesión como si no"_ y poné tu password.

### Opción 2 — `.bat` + Programador

`run_daily.bat`:

```bat
@echo off
cd /d "C:\Users\augus\OneDrive\Documentos\scraprop"
python run_daily.py
```

Y programar el `.bat`.

## Estructura

```
scraprop/
├── requirements.txt
├── README.md
├── run_daily.py              # entrypoint
├── run_daily.bat             # wrapper para Programador de tareas
├── scraper/
│   ├── __init__.py
│   ├── common.py             # filtros, normalización, fingerprint, antigüedad/orientación
│   ├── storage.py            # SQLite store + republish detection + JSON helpers
│   ├── argenprop.py          # scraper Argenprop
│   └── zonaprop.py           # scraper Zonaprop (curl_cffi + Cloudflare bypass)
├── viewer/
│   ├── index.html            # SPA estático (sin build, sin deps)
│   └── data.json             # snapshot generado por run_daily.py
├── data/                     # SQLite DB (creada en la primera corrida)
├── reports/                  # reportes diarios (md + json)
└── logs/                     # logs diarios
```

## Notas / limitaciones

- El primer día, **todo es nuevo** porque la DB está vacía. A partir del segundo día sólo aparecen listings con `first_seen == hoy`.
- Si Zonaprop endurece más Cloudflare, podría hacer falta usar un proxy residencial o cambiar a `playwright + playwright_stealth`. Hoy `curl_cffi` con `impersonate="chrome"` pasa.
- El filtro `mas-100000` (precio mínimo) lo aplica el `matches_filters()` de Python; Zonaprop ignora a veces ese parámetro de URL pero los listings están todos en el HTML y los descartamos por código.
- Si en algún momento Argenprop o Zonaprop cambian su HTML, los selectores están en `_parse_card()` de cada scraper.
