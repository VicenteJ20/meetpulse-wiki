# MeetPulse Librarian Worker

Cloudflare Worker que consume las notificaciones de creación de análisis en R2, consolida conocimiento con Gemini mediante Vercel AI Gateway y ejecuta el mantenimiento nocturno.

## Preparación

1. Aplica `migrations/0003_librarian.sql` y `migrations/0004_work.sql` en la misma base D1 utilizada por la API. Despliega también la API con sus endpoints de pendientes y notas antes de activar la extracción de compromisos.
2. Reemplaza en `wrangler.jsonc` el nombre del bucket, el ID de D1 y `LIBRARIAN_API_BASE_URL`.
3. Crea las colas:

```powershell
npx wrangler queues create meetpulse-librarian
npx wrangler queues create meetpulse-librarian-dlq
```

4. Configura secretos. `LIBRARIAN_WEBHOOK_SECRET` debe tener exactamente el mismo valor en Vercel:

```powershell
npx wrangler secret put AI_GATEWAY_API_KEY
npx wrangler secret put LIBRARIAN_WEBHOOK_SECRET
```

5. Instala, verifica y despliega:

```powershell
npm install
npm run check
npm run deploy
```

6. Conecta R2 a la cola después de desplegar el consumidor:

```powershell
npx wrangler r2 bucket notification create meetpulse-wiki --event-type object-create --queue meetpulse-librarian --prefix "sources/" --suffix ".md"
```

El cron `0 3 * * *` está declarado en `wrangler.jsonc`. El modelo se controla con `LIBRARIAN_MODEL`; el POC usa `google/gemini-3.1-flash-lite` y no configura fallback costoso.

## Desarrollo local

Usa `.dev.vars` dentro de `worker/` para los secretos locales; el archivo no debe versionarse. R2, D1 y Queues pueden apuntar a recursos de desarrollo o bindings locales de Wrangler.

Para consumir R2 y D1 remotos con una Queue local, conserva `remote: true` en ambos bindings y agrega a `.dev.vars`:

```env
LIBRARIAN_DEV_MODE=true
LIBRARIAN_API_BASE_URL=http://127.0.0.1:8000
AI_GATEWAY_API_KEY=
LIBRARIAN_WEBHOOK_SECRET=
```

`LIBRARIAN_API_BASE_URL` en `.dev.vars` sobrescribe la URL productiva de
`wrangler.jsonc`; así el Worker local aplica los resultados contra la API local.

Con la API activa en `http://127.0.0.1:8000`, inicia el Worker:

```powershell
npm run dev
```

Luego publica un evento sintÃ©tico desde otra terminal. El endpoint solo existe cuando `LIBRARIAN_DEV_MODE=true`:

```bash
curl -X POST http://127.0.0.1:8787 \
  -H "content-type: application/json" \
  -d '{"source_key":"sources/vicente-s-tenant/Youtube/the-white-house/2026-07-17-president-trump-delivers-an-address-to-the-nation-jul-16-2026.md"}'
```

La Queue local invocarÃ¡ el consumidor; el anÃ¡lisis, el contexto, los jobs y las escrituras continuarÃ¡n usando R2/D1 remotos. No habilites `LIBRARIAN_DEV_MODE` en un despliegue.

## Reprocesar análisis completados antes de la extracción de compromisos

Actualizar el Worker no reprocesa automáticamente los jobs que ya están en `succeeded`. Para un lote autorizado, selecciona las fuentes canónicas por su fecha de carga, comprueba que sus RAW existen y respalda los jobs y contextos antes de reabrir únicamente los IDs seleccionados. Procesa el lote en orden cronológico y espera el resultado de cada job antes del siguiente, especialmente cuando comparten proyecto.

Reenvía a `meetpulse-librarian` un evento JSON `PutObject` con la clave y ETag vigentes. Usa como `eventTime` la fecha actual del reproceso: reutilizar la fecha del evento original puede producir `existing derived document differs`, porque los documentos derivados son inmutables. La fecha de la reunión permanece en la fuente original y sigue determinando la semana predeterminada del compromiso.

Verifica `succeeded`, las salidas en R2 y los compromisos con `source_key` en D1. Contrasta cada acción y evidencia con el análisis; finalmente consulta `list_pending` y `list_my_week` por el plugin. No cambies fuentes históricas ni habilites `LIBRARIAN_DEV_MODE` en producción para reprocesarlas.
