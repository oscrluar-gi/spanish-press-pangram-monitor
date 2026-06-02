# Spanish Press Pangram Monitor

Proyecto open source en Python 3.11+ para que cualquier usuario pueda verificar, de forma reproducible, si articulos publicados por medios espanoles muestran senales de escritura con IA segun Pangram AI Detection API.

El pipeline:

1. descubre URLs de articulos por fecha usando sitemaps, RSS, GDELT y, opcionalmente, Wayback/CDX;
2. extrae texto limpio con `trafilatura`;
3. deduplica por URL normalizada y hash de texto;
4. envia solo 2-3 fragmentos aleatorios a Pangram, no el articulo completo;
5. borra de SQLite todo el texto scrapeado de la fecha tras ejecutar Pangram;
6. guarda resultados, errores y trazabilidad en SQLite;
7. exporta reportes CSV/JSON para revisar cobertura y calidad.

El repositorio debe contener solo codigo, configuracion, tests y documentacion. Las claves, bases SQLite, exports y textos completos son artefactos locales y no deben subirse a GitHub.

## Alcance y Limitaciones

Este proyecto no determina de forma absoluta si un articulo "lo escribio una IA". Usa la respuesta de Pangram como una senal de analisis, junto con trazabilidad de fuente, fecha, extraccion y estado del texto. Los detectores de IA pueden tener falsos positivos y falsos negativos; los resultados deben interpretarse con cautela, especialmente en articulos editados, traducidos, muy breves, de agencia, directos, listados o textos recuperados desde Wayback.

Tampoco pretende saltarse paywalls ni restricciones tecnicas. Si un articulo no puede extraerse de forma respetuosa, queda registrado con su estado de error.

## Instalacion

Desde esta carpeta:

```powershell
cd press-monitor
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
```

Copia `.env.example` a `.env` y configura:

```env
PANGRAM_API_KEY=...
PRESS_MONITOR_DB=data/press_monitor.sqlite
```

No subas `.env` a GitHub. El archivo real `.env` esta ignorado por `.gitignore`; `.env.example` es la plantilla publica.

## Configuracion de Medios

Los medios viven en `config/media.yaml`. Cada entrada puede definir:

- `sitemap_urls`: sitemaps reales del medio.
- `rss_feeds`: RSS reales para complementar o sustituir sitemaps.
- `sitemap_page_range`: rango para plantillas con `{page}`.
- `include_url_patterns` / `exclude_url_patterns`: filtros regex por medio.
- `include_liveblogs`, `include_opinion`, `include_sports`: controles de inclusion.
- `gdelt_discovery`, `gdelt_discovery_limit`: discovery complementario mediante GDELT.
- `wayback_discovery`, `wayback_discovery_min_candidates`: discovery historico mediante Wayback/CDX si faltan candidatos.
- `request_delay_seconds`, `max_concurrency_per_domain`, `max_retries`: limites por medio.

Las URLs de sitemap aceptan `{year}`, `{month}`, `{day}` y `{page}`. Ejemplo:

```yaml
sitemap_urls:
  - "https://elpais.com/sitemaps/{year}/{month}/sitemap_{page}.xml"
sitemap_page_range: [0, 5]
```

Por defecto se incluyen articulos normales y opinion, y se excluyen tags, autores, videos puros, galerias, newsletters, podcasts, secciones y directos.

## Uso

```powershell
python -m src.main discover --date 2026-05-31
python -m src.main extract --date 2026-05-31 --wayback fallback
python -m src.main analyze --date 2026-05-31 --dry-run
python -m src.main analyze --date 2026-05-31 --per-media-limit 10
python -m src.main analyze --date 2026-05-31 --limit 50
python -m src.main run --date 2026-05-31 --wayback fallback
python -m src.main export --date 2026-05-31 --format csv
```

Para comprobar que estas usando la base de datos esperada:

```powershell
python -m src.main status --date 2026-05-31
```

Para probar o reintentar un solo medio:

```powershell
python -m src.main discover --date 2026-05-31 --media abc
```

`analyze` no reenvia articulos con resultado Pangram salvo que pases `--force`. Los articulos `no_text`, `too_short` y `paywall_or_incomplete` no se envian salvo que uses `--include-incomplete`.

Para controlar coste, `analyze` envia por defecto como maximo 10 articulos por medio. Puedes cambiarlo con `--per-media-limit X` o desactivar el limite con `--per-media-limit 0`. La seleccion por medio es aleatoria para no sesgar por orden de URL. Si quieres una muestra reproducible, define `PANGRAM_ARTICLE_SAMPLE_SEED` en `.env`.

`analyze` tambien acepta la misma ventana temporal que `discover` y `extract`; por ejemplo:

```powershell
python -m src.main analyze --date 2026-05-01 --start-time 05:00 --hours 4 --per-media-limit 10 --dry-run
python -m src.main analyze --date 2026-05-01 --start-time 05:00 --hours 4 --per-media-limit 10
```

Por privacidad y minimizacion de datos, `analyze` no envia el articulo completo a Pangram. Para cada articulo selecciona aleatoriamente 2 o 3 fragmentos de 300 a 500 palabras, envia cada fragmento por separado y guarda una respuesta agregada sin conservar el texto de los fragmentos. Si un articulo tiene menos de 300 palabras, se envia un unico fragmento corto con el texto disponible.

Tras una ejecucion real de `analyze`, el proyecto borra por defecto **todo** `articles.text_clean` de esa fecha, tanto de articulos enviados a Pangram como de articulos extraidos pero no enviados por limite de coste. Se conservan metadatos, `word_count`, `text_hash`, URL, estado de extraccion y respuesta Pangram redactada. Si despues quieres analizar mas articulos de esa misma fecha, vuelve a ejecutar `extract` para reconstruir temporalmente el texto.

Tambien puedes purgar textos manualmente:

```powershell
python -m src.main purge-texts --date 2026-05-31
python -m src.main purge-texts --all
```

Variables opcionales:

```env
PANGRAM_FRAGMENT_MIN_COUNT=2
PANGRAM_FRAGMENT_MAX_COUNT=3
PANGRAM_FRAGMENT_MIN_WORDS=300
PANGRAM_FRAGMENT_MAX_WORDS=500
```

## Flujo Recomendado

Para una fecha nueva, ejecuta en este orden:

```powershell
python -m src.main discover --date 2026-05-31
python -m src.main audit-sources --date 2026-05-31
python -m src.main extract --date 2026-05-31 --wayback fallback
python -m src.main audit-wayback --date 2026-05-31
python -m src.main analyze --date 2026-05-31 --dry-run
python -m src.main analyze --date 2026-05-31 --per-media-limit 10
python -m src.main report --date 2026-05-31
python -m src.main validate --date 2026-05-31 --sample-size 10
python -m src.main purge-texts --date 2026-05-31
```

`audit-sources` y `audit-wayback` son diagnosticos: no sustituyen a `discover` ni a `extract`.

Para trabajar con una franja horaria concreta, usa `--start-time` con `--hours` o `--end-time`. Las horas se interpretan en `Europe/Madrid`:

```powershell
python -m src.main discover --date 2026-05-01 --start-time 05:00 --hours 4
python -m src.main extract --date 2026-05-01 --start-time 05:00 --hours 4 --wayback fallback
```

Algunos medios publican sitemaps mensuales sin dia exacto en la URL. Para esos casos se puede activar `allow_month_fallback: true` por medio. El discovery guardara candidatos del mes y la extraccion validara `article_published_at`; si no coincide con `target_date`, el articulo queda como `out_of_target_date` y no se envia a Pangram.

`discover` usa varias fuentes de URLs en este orden:

1. sitemaps/RSS configurados y fallbacks;
2. GDELT, si `gdelt_discovery` esta activo;
3. Wayback/CDX discovery solo si el medio queda por debajo de `wayback_discovery_min_candidates`.

GDELT esta activo por defecto en `defaults` con un limite moderado. Puedes bajarlo o desactivarlo por medio:

```yaml
gdelt:
  gdelt_enabled: true
  gdelt_max_results_per_media: 100
  gdelt_request_delay_seconds: 12
  gdelt_max_retries: 2
  gdelt_retry_delay_seconds: 15
  gdelt_timeout_seconds: 30

media:
  - name: "ABC"
    domain: "abc.es"
    gdelt_discovery: true
    gdelt_discovery_limit: 100
```

Las URLs encontradas se guardan con `source_type=gdelt` y `discovered_from=gdelt`. GDELT aporta `seendate`, que se guarda como `discovered_lastmod`, pero no sustituye a `article_published_at`: la fecha real se valida despues en `extract`. Si GDELT trae duplicados o articulos fuera de fecha, los constraints de URL normalizada y la validacion de extraccion los contienen.

Para medios cuyos sitemaps son solo actuales o bloquean el acceso, se puede activar discovery historico con Wayback/CDX por medio:

```yaml
media:
  - name: "ABC"
    domain: "abc.es"
    wayback_discovery: true
    wayback_discovery_min_candidates: 2
    wayback_discovery_limit: 250
    wayback_discovery_patterns:
      - "www.{domain}/*/{year}/{month}/{day}/*"
      - "*.{domain}/*{yyyymmdd}*"
```

Esta fuente consulta capturas HTML 200 de Internet Archive con patrones estrechos de fecha en la URL, deduplica por URL normalizada y guarda los candidatos con `source_type=wayback_cdx_discovery`. Es una fuente de descubrimiento, no una garantia de publicacion exacta: la extraccion posterior sigue validando `article_published_at` y descartara articulos fuera de fecha.

Hay dos usos distintos de Wayback:

- **Wayback discovery**: ocurre en `discover`; intenta encontrar URLs historicas cuando sitemaps/RSS/GDELT dejan pocos candidatos. Es sensible a timeouts/503 de CDX y por eso se usa con umbral.
- **Wayback extraction fallback**: ocurre en `extract --wayback fallback`; si una URL ya descubierta falla en vivo, busca un snapshot de esa URL y extrae texto desde `web.archive.org`. Esta parte suele funcionar mejor cuando ya tenemos una URL concreta, aunque sigue dependiendo de que exista snapshot HTML completo.

`validate` no llama a Pangram ni scrapea nuevas paginas. Solo lee SQLite y genera una revision de calidad en `exports/validation/`:

- `validation_summary_YYYY-MM-DD.csv`: metricas por medio.
- `validation_YYYY-MM-DD.json`: metricas completas y warnings.
- `validation_sample_YYYY-MM-DD.csv`: muestra manual sin texto scrapeado; la columna `text_preview` se mantiene vacia por seguridad.

Warnings automaticos:

- medio con 0 URLs descubiertas;
- extraccion/discovery por debajo del 40%;
- mas del 50% de textos por debajo de 300 palabras;
- snapshots Wayback a mas de 72h;
- muchos titulos vacios;
- hashes de texto repetidos;
- URLs sospechosas de secciones, tags, videos o newsletters.

## Wayback Fallback

Wayback fallback es una capa opcional que consulta Internet Archive cuando la URL original falla o produce texto incompleto. No requiere API key.

Modos:

- `--wayback off`: no consulta Wayback.
- `--wayback fallback`: consulta Wayback solo si live falla con HTTP recuperable, `no_text`, `too_short` o `paywall_or_incomplete`.
- `--wayback always`: consulta Wayback aunque live sea correcto, pero conserva live si Wayback no mejora la recuperacion.

Configuracion global en `config/media.yaml`:

```yaml
wayback:
  wayback_enabled: true
  wayback_strategy: "fallback_only"
  wayback_max_days_after: 7
  wayback_max_days_before: 1
  wayback_request_delay_seconds: 1.5
  wayback_max_retries: 2
  wayback_timeout_seconds: 45
  wayback_use_cdx: true
  wayback_use_availability: true
  wayback_extended_search: true
  wayback_extended_max_days_after: 30
  wayback_extended_max_days_before: 7
  wayback_discovery_enabled: true
  wayback_discovery_max_urls_per_media: 250
  wayback_discovery_max_days_after: 3
  wayback_discovery_max_days_before: 0
  wayback_discovery_timeout_seconds: 15
  wayback_discovery_max_retries: 0
  wayback_discovery_min_candidates: 2
```

Tambien puedes usar variables `.env` equivalentes: `WAYBACK_ENABLED`, `WAYBACK_STRATEGY`, `WAYBACK_MAX_DAYS_AFTER`, `WAYBACK_MAX_DAYS_BEFORE`, `WAYBACK_REQUEST_DELAY_SECONDS`, `WAYBACK_MAX_RETRIES`, `WAYBACK_TIMEOUT_SECONDS`, `WAYBACK_USE_CDX`, `WAYBACK_USE_AVAILABILITY`, `WAYBACK_EXTENDED_SEARCH`, `WAYBACK_EXTENDED_MAX_DAYS_AFTER`, `WAYBACK_EXTENDED_MAX_DAYS_BEFORE`, `WAYBACK_DISCOVERY_ENABLED`, `WAYBACK_DISCOVERY_MAX_URLS_PER_MEDIA`, `WAYBACK_DISCOVERY_MAX_DAYS_AFTER`, `WAYBACK_DISCOVERY_MAX_DAYS_BEFORE`, `WAYBACK_DISCOVERY_TIMEOUT_SECONDS`, `WAYBACK_DISCOVERY_MAX_RETRIES`, `WAYBACK_DISCOVERY_MIN_CANDIDATES`.

GDELT tambien acepta variables `.env`: `GDELT_ENABLED`, `GDELT_MAX_RESULTS_PER_MEDIA`, `GDELT_REQUEST_DELAY_SECONDS`, `GDELT_MAX_RETRIES`, `GDELT_RETRY_DELAY_SECONDS`, `GDELT_TIMEOUT_SECONDS`.

Diferencia de procedencia:

- `content_source=live`: texto extraido de la URL original.
- `content_source=wayback`: texto extraido de Wayback; primero se intenta `https://web.archive.org/web/<timestamp>id_/<original_url>` y, si falla, se prueban modos replay alternativos.

Los articulos Wayback guardan `source_url`, `original_url`, `wayback_timestamp` y `wayback_distance_seconds`.

Limitaciones: una captura Wayback puede no coincidir exactamente con la version publicada originalmente, puede estar incompleta, puede tener banners del archivo, o puede faltar justo la fecha deseada. El selector prioriza HTML 200 y la captura mas cercana dentro de la ventana configurada; si no encuentra nada en la ventana corta, puede hacer una busqueda extendida configurable.

Discovery historico con CDX tiene otra limitacion: descubre URLs archivadas cerca de la fecha, no articulos publicados de forma certificada en esa fecha. Por defecto usa primero patrones estrechos como `www.{domain}/*/{year}/{month}/{day}/*` y despues variantes con `{yyyymmdd}`; si un medio necesita otra forma de URL, anade `wayback_discovery_patterns`. Las consultas de discovery usan timeout y retries propios para que una respuesta lenta de CDX no bloquee todo el pipeline. Por eso conviene revisar `validate`, `report` y los estados `out_of_target_date` antes de pasar lotes grandes a Pangram.

## Auditoria de Fuentes

Antes de ejecutar el pipeline completo puedes medir cobertura real:

```powershell
python -m src.main audit-sources --date 2026-05-31
```

Genera:

- `exports/audits/audit_YYYY-MM-DD.csv`
- `exports/audits/audit_YYYY-MM-DD.json`

El informe indica por medio y fuente si responde 200, si es XML valido, si es sitemap index, cuantas URLs candidatas aparecen, cuantas pasan el filtro de fecha, cuantas pasan filtros de URL, cuantas vienen de RSS y cuantas de fallback automatico.

Para auditar disponibilidad Wayback sin descargar contenido:

```powershell
python -m src.main audit-wayback --date 2026-05-31
```

Genera `exports/audits/wayback_audit_YYYY-MM-DD.csv` y `.json` con snapshot disponible, timestamp, distancia en horas, API fuente, status y mimetype.

## Pruebas de Fuentes Alternativas

`probe-sources` prueba fuentes experimentales sin escribir en SQLite. Sirve para comparar estrategias antes de convertirlas en discovery real:

```powershell
python -m src.main probe-sources --date 2026-05-01 --media abc,lavanguardia,eleconomista,marca --max-results 10
```

Estrategias disponibles:

- `wayback_robots`: busca `robots.txt` archivado en Wayback y extrae líneas `Sitemap:`.
- `wayback_rss`: busca snapshots Wayback de los RSS configurados y parsea items de la fecha.
- `gdelt`: consulta GDELT DOC 2.0 con `domainis:`/`domain:` y rango diario.
- `web_search`: prueba búsqueda web `site:` vía DuckDuckGo HTML; los resultados quedan como `ok_date_unverified` porque el buscador no certifica fecha de publicación.

Puedes limitar estrategias:

```powershell
python -m src.main probe-sources --date 2026-05-01 --media marca --strategies gdelt,web_search
```

Exporta `exports/source_probes/source_probe_YYYY-MM-DD.csv` y `.json`.

## Reporte

```powershell
python -m src.main report --date 2026-05-31
```

Muestra y exporta en `exports/reports/`:

- articulos descubiertos por medio;
- articulos extraidos correctamente;
- articulos recuperados desde live;
- articulos recuperados desde Wayback;
- hits/misses de Wayback;
- distancia media al snapshot usado;
- articulos enviados o reutilizados en Pangram;
- errores por tipo;
- cobertura extraccion/discovery;
- medios con mas fallos;
- articulos omitidos y razon.

## Estados de Extraccion

- `ok_live`: texto suficiente desde la URL original.
- `ok_wayback`: texto suficiente desde Wayback.
- `no_text`: no se pudo extraer texto.
- `too_short`: hubo texto, pero no llega a `PRESS_MONITOR_MIN_WORDS`.
- `paywall_or_incomplete`: probable muro de pago o texto incompleto.
- `out_of_target_date`: el articulo extraido tiene fecha real distinta de `target_date`.
- `http_error`: error HTTP, bloqueo robots o fallo de red.
- `parse_error`: HTML o metadatos no parseables.
- `wayback_not_found`: no se encontro snapshot adecuado.
- `wayback_fetch_error`: fallo descargando snapshot.
- `wayback_parse_error`: snapshot no parseable.
- `wayback_too_short`: snapshot con texto por debajo del minimo.

## Fechas

El pipeline separa:

- `discovered_lastmod`: `lastmod` del sitemap.
- `rss_published_at`: fecha declarada por RSS.
- `article_published_at`: fecha real detectada en el articulo.
- `article_modified_at`: fecha de modificacion detectada.
- `target_date`: fecha de ejecucion solicitada.

Importante: `lastmod` no equivale necesariamente a fecha de publicacion. En discovery se usa solo como senal debil; en extraction se priorizan JSON-LD `NewsArticle`/`Article`, `article:published_time`, `datePublished` y `time[datetime]`.

## Controles de Scraping

- `PRESS_MONITOR_USER_AGENT`: user-agent identificable.
- `PRESS_MONITOR_RESPECT_ROBOTS=true`: respeta `robots.txt` cuando se puede leer.
- `PRESS_MONITOR_DOMAIN_PAUSE_SECONDS=1.5`: pausa minima por dominio.
- `PRESS_MONITOR_EXTRACT_CONCURRENCY=3`: concurrencia global de extraccion.
- `PRESS_MONITOR_MAX_CONCURRENCY_PER_DOMAIN=1`: concurrencia maxima por dominio.
- `PRESS_MONITOR_MAX_RETRIES=2`: retries con backoff exponencial y jitter.
- `PRESS_MONITOR_MIN_WORDS=150`: minimo de palabras para marcar `ok`.
- `PRESS_MONITOR_MAX_SITEMAPS_PER_DOMAIN=30`: limite de sitemaps anidados.

## Persistencia

SQLite crea:

- `media`
- `discovered_urls`
- `articles`
- `pangram_results`
- `wayback_snapshots`
- `run_log`

Hay constraints para URL normalizada, fecha + medio + URL normalizada y hash de texto. El pipeline es idempotente: repetir `run --date YYYY-MM-DD` no debe duplicar filas.

## Advertencia Legal

Este proyecto puede mantener texto completo solo de forma temporal entre `extract` y `analyze`. Tras una ejecucion real de Pangram, o al ejecutar `purge-texts`, el cuerpo extraido se borra de SQLite y se conservan hashes, metadatos y resultados redactados. No redistribuyas, publiques ni reutilices textos completos de terceros sin licencia o permiso adecuado. Esto aplica tambien a textos recuperados desde Wayback Machine.

Revisa robots.txt, terminos de uso, limites de cada medio y condiciones de Internet Archive. Publicar solo metricas agregadas o pequenas citas permitidas por la legislacion aplicable suele ser mucho mas seguro que publicar corpus completos.

## Publicar en GitHub de Forma Segura

Publica esta carpeta como repositorio independiente. Si tienes otros proyectos en la carpeta padre, como una app de contador de macros, no ejecutes `git add .` desde la carpeta padre. Trabaja desde `press-monitor/`.

Antes de subir el repositorio:

1. Comprueba que `.env` existe solo en local y que no se ha anadido al indice de Git.
2. No subas `data/`, `exports/`, `.venv/`, `.pytest_cache/`, `*.sqlite`, logs ni `*.egg-info/`.
3. Cambia el contacto del `PRESS_MONITOR_USER_AGENT` en tu `.env`.
4. Ejecuta `python -m pytest`.
5. Revisa `git status --short` y confirma que solo hay codigo, tests, configuracion y documentacion.
6. Si alguna vez se commiteo una clave, revocala antes de hacer publico el repo.

Archivos pensados para publicarse:

- `README.md`
- `LICENSE`
- `SECURITY.md`
- `CONTRIBUTING.md`
- `.env.example`
- `.gitignore`
- `pyproject.toml`
- `config/media.yaml`
- `src/`
- `tests/`

Archivos que no deben publicarse:

- `.env`
- `data/`
- `exports/`
- bases SQLite;
- CSV/JSON con textos completos;
- logs con URLs, errores o respuestas de APIs.

## Seguridad

Consulta `SECURITY.md` para el manejo de secretos y datos locales. La clave de Pangram se lee exclusivamente desde `PANGRAM_API_KEY` en `.env` o variables de entorno.

## Tests

```powershell
python -m pytest
```
