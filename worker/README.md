# MeetPulse Librarian Worker

Cloudflare Worker que consume las notificaciones de creación de análisis en R2, consolida conocimiento con Gemini mediante Vercel AI Gateway y ejecuta el mantenimiento nocturno.

## Preparación

1. Aplica `migrations/0003_librarian.sql` en la misma base D1 utilizada por la API.
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
