# MeetPulse Wiki API

API FastAPI que guarda reuniones analizadas y sus transcripciones RAW inmutables, y mantiene una Wiki OKF en Cloudflare R2.

## Instalación y ejecución

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\uvicorn app.main:app --reload
```

Completa `.env` con el endpoint S3 de R2, bucket y credenciales de lectura/escritura. `R2_REGION` debe permanecer como `auto`.

## Despliegue en Vercel

Vercel detecta `api/index.py` como el punto de entrada de FastAPI. Importa el repositorio y configura todas las variables de `.env.example` en **Settings → Environment Variables**. La aplicación desktop de Tauri ya está permitida por defecto mediante `http://tauri.localhost` y `https://tauri.localhost`, así que no necesita configurar `CORS_ALLOWED_ORIGINS`. Usa esa variable solo si después incorporas un cliente web, con sus orígenes separados por comas.

Antes del primer despliegue, aplica las migraciones de `migrations/` en Cloudflare D1. Las credenciales de R2 y el token de D1 se mantienen exclusivamente como variables secretas de Vercel.

## Endpoints

- `POST /api/v1/ingest`: `multipart/form-data` con el análisis `file` (`.md` UTF-8), la transcripción opcional `raw_file` (`.md`/`.txt` UTF-8), alcance, título, fecha y participantes. Devuelve `job_id` y estado de procesamiento.
- `GET /api/v1/tree/{tenant_id}?client_id=&project_id=`: devuelve el índice Markdown y sus enlaces directos.
- `GET /api/v1/logs/{tenant_id}?limit=100`: devuelve entradas de bitácora, más reciente primero.

### Dashboard y lectura de wiki

- `GET /api/v1/dashboard/{tenant_id}/summary`: KPIs de clientes, proyectos, fuentes, páginas wiki y última actividad.
- `GET /api/v1/dashboard/{tenant_id}/clients?limit=50&offset=0`: clientes con sus contadores y actividad.
- `GET /api/v1/dashboard/{tenant_id}/clients/{client_id}/projects`: proyectos del cliente y sus contadores.
- `GET /api/v1/dashboard/{tenant_id}/activity?limit=20`: eventos de ingesta listos para una vista de actividad.
- `GET /api/v1/wiki/{tenant_id}/documents?client_id=&project_id=`: documentos disponibles dentro del alcance seleccionado.
- `GET /api/v1/wiki/{tenant_id}/documents/{document}?client_id=&project_id=`: contenido Markdown de una fuente de análisis o del contexto de proyecto.
- `PUT /api/v1/wiki/{tenant_id}/documents/context?client_id=&project_id=`: actualiza el contexto con un documento OKF completo en `content_markdown`.
- `GET /api/v1/jobs/{tenant_id}/{job_id}`: estado de un trabajo del Bibliotecario.
- `GET /api/v1/jobs/{tenant_id}?client_id=&project_id=&limit=`: trabajos recientes del alcance seleccionado.

Una ingesta se guarda en `sources/{tenant}/{client}/{project}/{fecha-utc}-{titulo-slug}.md`. Una colisión de ruta devuelve `409` y nunca reemplaza la fuente. El YAML del archivo se conserva, salvo los seis metadatos canónicos que son reemplazados con los campos del formulario.

No se exponen aún `/query` ni `/lint`; ambos requieren la fase posterior de IA/mantenimiento.

## Estructura y contrato OKF

R2 separa transcripciones RAW, reuniones analizadas y Wiki derivada:

```text
raw/{tenant_id}/{client_id}/{project_id}/{YYYY-MM-DD}-{titulo-slug}/{archivo-original}
sources/{tenant_id}/{client_id}/{project_id}/{YYYY-MM-DD}-{titulo-slug}.md
wiki/{tenant_id}/index.md
wiki/{tenant_id}/log.md
wiki/{tenant_id}/{client_id}/index.md
wiki/{tenant_id}/{client_id}/{project_id}/index.md
wiki/{tenant_id}/{client_id}/{project_id}/context.md
wiki/{tenant_id}/{client_id}/{project_id}/decisions/*.md
wiki/{tenant_id}/{client_id}/{project_id}/risks/*.md
```

`index.md` y `log.md` son artefactos internos que la API genera y mantiene; no son documentos OKF. Todo documento de conocimiento bajo `wiki/` debe incluir YAML frontmatter con este contrato:

```yaml
---
type: context # context | meeting | decision | risk
title: Project context
description: Current global state of the project.
sources:
  - sources/acme/client/project/2026-07-12-design-meeting.md
timestamp: 2026-07-12T19:30:00Z
---
```

Los campos son obligatorios. `sources` debe contener claves existentes, sin duplicados, dentro del mismo tenant, cliente y proyecto; los IDs de la ruta no se repiten en el YAML. La reunión canónica permanece en `sources/`; el Bibliotecario crea y versiona documentos `decision` y `risk` cuando existe evidencia explícita.

Los archivos de `sources/` son análisis canónicos de reuniones y enlazan la evidencia primaria mediante `raw_sources` y `raw_sha256`. Los documentos derivados apuntan al análisis, no directamente al RAW. Los análisis históricos sin transcripción se identifican con `provenance_status: analysis_only`.

## Bibliotecario asíncrono

La creación de un objeto bajo `sources/` dispara una notificación R2 hacia Cloudflare Queues. El Worker en `worker/` compara el análisis con el contexto vigente, llama a Gemini mediante Vercel AI Gateway y aplica contextos, decisiones y riesgos por una API interna firmada con HMAC.

El modelo se configura mediante `LIBRARIAN_MODEL` y por defecto es `google/gemini-3.1-flash-lite` con `LIBRARIAN_THINKING_LEVEL=minimal`. No hay fallback automático mientras `LIBRARIAN_FALLBACK_MODEL` esté vacío. Consulta [worker/README.md](worker/README.md) para desplegar Queue, DLQ, evento R2 y cron nocturno.

## Prueba real contra R2

Configura `.env` con valores reales basados en `.env.example`. El token de R2 debe tener permiso **Object Read & Write** sobre el bucket configurado. Luego ejecuta:

```powershell
$env:RUN_R2_INTEGRATION = "1"
.\.venv\Scripts\python -m pytest -q tests/test_r2_integration.py
```

Si el servidor ya está activo, añade `API_BASE_URL=http://127.0.0.1:8000` para probar el transporte HTTP real en vez del cliente interno.

La prueba crea un tenant aleatorio con prefijo `r2test`, verifica la ingesta, el árbol, la bitácora y la protección ante colisiones; en el teardown elimina exclusivamente los objetos creados bajo `sources/{tenant}/` y `wiki/{tenant}/`.
