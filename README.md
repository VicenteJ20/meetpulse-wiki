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
- `GET /api/v1/wiki/{tenant_id}/documents/{document}?client_id=&project_id=`: contenido Markdown de un documento permitido (`index`, `acuerdos`, `arquitectura`, `conceptos`, `participantes`, `riesgos`).

Una ingesta se guarda en `sources/{tenant}/{client}/{project}/{fecha-utc}-{titulo-slug}.md`. Una colisión de ruta devuelve `409` y nunca reemplaza la fuente. El YAML del archivo se conserva, salvo los seis metadatos canónicos que son reemplazados con los campos del formulario.

No se exponen aún `/query` ni `/lint`; ambos requieren la fase posterior de IA/mantenimiento.

## Prueba real contra R2

Configura `.env` con valores reales basados en `.env.example`. El token de R2 debe tener permiso **Object Read & Write** sobre el bucket configurado. Luego ejecuta:

```powershell
$env:RUN_R2_INTEGRATION = "1"
.\.venv\Scripts\python -m pytest -q tests/test_r2_integration.py
```

Si el servidor ya está activo, añade `API_BASE_URL=http://127.0.0.1:8000` para probar el transporte HTTP real en vez del cliente interno.

La prueba crea un tenant aleatorio con prefijo `r2test`, verifica la ingesta, el árbol, la bitácora y la protección ante colisiones; en el teardown elimina exclusivamente los objetos creados bajo `sources/{tenant}/` y `wiki/{tenant}/`.
