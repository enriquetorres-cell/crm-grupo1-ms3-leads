# MS3 · Leads — CRM SaaS (CS2032 · Grupo 1)

Captura de leads, puntaje (0–100) y temperatura, asignación automática al vendedor con menos carga, estados e importación masiva por CSV en **S3**.
Base de datos: **DynamoDB**. Arquitectura: [crm-grupo1-infra](https://github.com/enriquetorres-cell/crm-grupo1-infra).

## Endpoints (`/ms3`, todos con token salvo health)

| Método | Ruta | Qué hace |
| --- | --- | --- |
| GET | `/ms3/health` | público |
| GET | `/ms3/leads?limit=&siguiente=&estado=&temperatura=` | listado con cursor `{limit, items, siguiente}` |
| GET | `/ms3/leads/{id}` | detalle |
| POST | `/ms3/leads` | crea, puntúa y asigna (409 si el email o el teléfono ya existen, o si se pasó el límite del plan) |
| PATCH | `/ms3/leads/{id}/estado` | `nuevo → contactado → calificado`, o `descartado` (con motivo) |
| POST | `/ms3/leads/{id}/convertir` | solo un lead calificado con puntaje ≥ 40 |
| POST | `/ms3/importaciones` | URL firmada para subir el CSV directo a S3 |
| GET | `/ms3/importaciones/{id}` | resultado: creados, rechazados y archivo de errores |

## Eventos

- **Publica:** `lead.asignado` (MS5), `lead.convertido` (MS2, MS4, MS5).
- **Consume (cola `crm-ms3`):** `usuario.creado` (el vendedor entra al reparto de leads).

## Estructura DynamoDB

Tabla `crm-ms3-<stage>-leads` (single-table, `tenant_id` + `sk`):

```json
{"tenant_id":"tenant_andina","sk":"LEAD#L1a2b3c4d5e6","lead_id":"L1a2b3c4d5e6","empresa":"Constructora Lima",
 "nombre":"Lucía","apellido":"Ramos","cargo":"Gerente","email":"lucia@clima.pe","telefono":"987654321",
 "fuente":"referido","ruc":null,"empleados":120,"monto_estimado":18000,"puntaje":80,"temperatura":"caliente",
 "estado":"nuevo","propietario":"vendedor@andina.pe","origen":"api","creado_en":"2026-11-08T15:00:00Z"}
{"tenant_id":"tenant_andina","sk":"EMAIL#lucia@clima.pe","lead_id":"L1a2b3c4d5e6"}
{"tenant_id":"tenant_andina","sk":"TEL#987654321","lead_id":"L1a2b3c4d5e6"}
{"tenant_id":"tenant_andina","sk":"MES#2026-11","total":153}
```

Tabla `crm-ms3-<stage>-carga` (`tenant_id` + `vendedor`): `{"tenant_id":"tenant_andina","vendedor":"vendedor@andina.pe","abiertos":4,"orden":"2026-11-08T15:00:00Z","activo":true}`

Bucket `crm-importaciones-<cuenta>-<stage>`: `entrada/<tenant>/<plan>/<id>.csv` (dispara la Lambda `importar`), `resultados/…json`, `errores/…csv`.

## Desplegar

Requiere desplegados `crm-compartido` y `crm-ms1-usuarios`.

```bash
npm install
npx serverless deploy --stage dev
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

`src/comun.py` es una copia de `comun/comun.py` del repo infra: edítalo allá y corre `sincronizar-comun`.
