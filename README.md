# MeetPulse Wiki API

API FastAPI que guarda transcripciones Markdown inmutables y mantiene la estructura de conocimiento en Cloudflare R2.

## Instalación y ejecución

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\uvicorn app.main:app --reload
```

Completa `.env` con el endpoint S3 de R2, bucket y credenciales de lectura/escritura. `R2_REGION` debe permanecer como `auto`.

## Despliegue en Vercel

Vercel detecta `api/index.py` como el punto de entrada de FastAPI. Importa el repositorio y configura todas las variables de `.env.example` en **Settings → Environment Variables**. Añade además `CORS_ALLOWED_ORIGINS` con los dominios del frontend separados por comas, por ejemplo `https://app.meetpulse.com,https://meetpulse-web.vercel.app`.

Antes del primer despliegue, aplica las migraciones de `migrations/` en Cloudflare D1. Las credenciales de R2 y el token de D1 se mantienen exclusivamente como variables secretas de Vercel.

## Endpoints

- `POST /api/v1/ingest`: `multipart/form-data` con `file` (`.md` UTF-8), `tenant_id`, `client_id`, `project_id`, `title`, `date_time` y uno o más campos `participants`.
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

Una ingesta se guarda en `sources/{tenant}/{client}/{project}/{fecha-utc}-{titulo-slug}.md`. Una colisión de ruta devuelve `409` y nunca reemplaza la fuente. El YAML del archivo se conserva, salvo los seis metadatos canónicos que son reemplazados con los campos del formulario.

No se exponen aún `/query` ni `/lint`; ambos requieren la fase posterior de IA/mantenimiento.

## Estructura y contrato OKF

R2 separa las fuentes crudas e inmutables de la Wiki derivada:

```text
sources/{tenant_id}/{client_id}/{project_id}/{YYYY-MM-DD}-{titulo-slug}.md
wiki/{tenant_id}/index.md
wiki/{tenant_id}/log.md
wiki/{tenant_id}/{client_id}/index.md
wiki/{tenant_id}/{client_id}/{project_id}/index.md
wiki/{tenant_id}/{client_id}/{project_id}/context.md
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

Los campos son obligatorios. `sources` debe contener claves existentes, sin duplicados, dentro del mismo tenant, cliente y proyecto; los IDs de la ruta no se repiten en el YAML. En esta fase solo se expone la edición de `context`; `meeting`, `decision` y `risk` quedan reservados para el pipeline de IA posterior.

## Prueba real contra R2

Configura `.env` con valores reales basados en `.env.example`. El token de R2 debe tener permiso **Object Read & Write** sobre el bucket configurado. Luego ejecuta:

```powershell
$env:RUN_R2_INTEGRATION = "1"
.\.venv\Scripts\python -m pytest -q tests/test_r2_integration.py
```

Si el servidor ya está activo, añade `API_BASE_URL=http://127.0.0.1:8000` para probar el transporte HTTP real en vez del cliente interno.

La prueba crea un tenant aleatorio con prefijo `r2test`, verifica la ingesta, el árbol, la bitácora y la protección ante colisiones; en el teardown elimina exclusivamente los objetos creados bajo `sources/{tenant}/` y `wiki/{tenant}/`.
