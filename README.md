# Plataforma Analítica de Inventarios Retail y Control de Mermas — Arquitectura de Datos End-to-End

> 🚀 **Dashboard Web Interactivo en Vivo:** [retail-inventory-web.vercel.app](https://retail-inventory-web.vercel.app/)  
> 📁 **Repositorio del Frontend:** [`MrOdar/retail-inventory-web`](https://github.com/MrOdar/retail-inventory-web)

[![Live Demo](https://img.shields.io/badge/Demo_en_Vivo-Vercel_App-000000?style=flat&logo=vercel&logoColor=white)](https://retail-inventory-web.vercel.app/)
[![PostgreSQL](https://img.shields.io/badge/Base_de_Datos-PostgreSQL_15-336791?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Infraestructura-Docker_Compose-2496ED?style=flat&logo=docker&logoColor=white)](docker-compose.yml)
[![Python](https://img.shields.io/badge/ETL-Python_3.9+_·_pandas_·_SQLAlchemy-3776AB?style=flat&logo=python&logoColor=white)](src/etl_pipeline.py)
[![Schema](https://img.shields.io/badge/Arquitectura-3NF_Relacional-orange?style=flat)](#3-arquitectura-relacional-3nf)
[![PL/pgSQL](https://img.shields.io/badge/Lógica_de_Negocio-Stored_Procedures_%26_Triggers-336791?style=flat)](sql/04_triggers_and_procedures.sql)
[![Tests](https://img.shields.io/badge/Pruebas-219_Python_·_19_SQL-success?style=flat)](tests/)
[![Forecast](https://img.shields.io/badge/Pron%C3%B3stico-WAPE_23%25_·_intervalo_80%25_calibrado-blueviolet?style=flat)](#9-pronóstico-de-demanda-y-reposición)
[![Power BI](https://img.shields.io/badge/BI-Star_Schema_%26_DAX-F2C811?style=flat&logo=powerbi&logoColor=black)](bi/dax_measures.md)

Una plataforma integral de ingeniería de datos — infraestructura contenerizada, simulación y generación de datos sintéticos, pipeline ETL con compuerta de calidad estricta (quality gate), lógica transaccional de negocio en PostgreSQL 15, consultas SQL analíticas avanzadas y un datamart optimizado para Business Intelligence — diseñada para resolver el problema crítico del vencimiento de perecibles, descuadres de guías de remisión y fallas en la rotación FEFO/PEPS en tiendas de conveniencia y retail de alta frecuencia.

Además de la base relacional y su lógica transaccional, la plataforma incluye un **almacén dimensional con carga incremental y SCD tipo 2**, un **orquestador de pipeline con reintentos y aislamiento de fallas**, **pronóstico de demanda con evaluación fuera de muestra**, una **proyección FEFO hacia adelante** que lista los lotes que se vencerán antes de que ocurra, un **experimento que mide** si reponer con pronóstico reduce la merma, y una **calidad de datos puntuada contra una verdad de terreno** y versionada en git.

> **Hallazgo principal del dataset simulado de 60 días:** El 75.4% de todas las pérdidas monetarias corresponde a **mermas por vencimiento** — la categoría exacta de pérdida que una regla determinista de rotación FEFO puede mitigar estructuralmente. Análisis completo en [`docs/business_case_study.md`](docs/business_case_study.md).
>
> **Hallazgo del experimento de reposición (100 corridas no vistas):** reponer con pronóstico reduce la merma por vencimiento **53%**, pero la utilidad neta mejora solo **0.4%**, porque pedir menos genera más quiebres. Y una política más magra *sin* estacionalidad **pierde dinero**. Detalle en la [sección 10](#10-experimento-reducir-la-merma-compensa-los-quiebres).

---

## 1. Ejecutar la Plataforma Completa en un Solo Comando

```bash
docker compose up -d
```

Ese comando inicia PostgreSQL 15 y, en su primer arranque, aplica automáticamente el DDL en 3NF, los datos maestros (seed), los procedimientos almacenados, los triggers antifraude y el datamart materializado — en su estricto orden de dependencias.

Luego, ejecuta todo el pipeline con un solo comando:

```bash
make pipeline
```

El orquestador corre seis etapas en orden de dependencia, con reintentos para fallas transitorias y un registro de cada corrida (ver [sección 11](#11-orquestación-y-observabilidad)). Cada etapa puede ejecutarse por separado:

```bash
make init-db      # Inicia PostgreSQL y verifica que cada objeto haya sido creado
make generate     # Simula 60 días de operaciones -> data/raw/*.csv + _manifest.json
make validate     # Compuerta de calidad (23 reglas), sin base de datos
make run-etl      # Extrae -> valida -> cuarentena -> UPSERT en PostgreSQL
make models       # Construye el esquema en estrella (incremental, SCD2) y lo prueba
make forecast     # Pronostica demanda, proyecta vencimientos, recomienda pedidos
make dq-report    # Renderiza docs/data_quality_report.md
make experiment   # Reproduce el experimento de políticas de reposición (~15 s)
make run-queries  # Ejecuta las 4 consultas analíticas avanzadas
make test         # 219 pruebas Python + 19 aserciones SQL
```

Todo desde cero: `make all`.

### En Windows (PowerShell nativo sin `make`)

```powershell
.\scripts\run_project.ps1 all
```

El script de PowerShell expone las mismas acciones (`init-db`, `pipeline`, `generate`, `validate`, `run-etl`, `models`, `forecast`, `dq-report`, `experiment`, `run-queries`, `test`, `test-unit`, `test-sql`, `status`, `psql`, `logs`, `destroy`). Ejecuta `.\scripts\run_project.ps1 help` para ver la lista completa.

### Requisitos Previos

| Requisito | Necesario para |
|---|---|
| Docker Desktop | Servicio de base de datos PostgreSQL 15 |
| Python 3.9+ | Generador, ETL, modelos, pronóstico y orquestador |
| `pip install -r requirements.txt` | pandas · numpy · SQLAlchemy 2.x · psycopg2-binary |
| `pip install -r requirements-dev.txt` | Lo anterior + pytest, para ejecutar las pruebas |

*Nota: No se requiere base de datos activa para probar el motor de calidad de datos — `make validate` (o `.\scripts\run_project.ps1 validate`) ejecuta la extracción y evalúa las 23 reglas contra los CSVs generando el registro de auditoría sin necesidad de conexión. El generador, el experimento de políticas y el módulo de calidad usan solo la librería estándar.*

### Parámetros de Conexión

| Parámetro | Valor |
|---|---|
| Host / Puerto | `localhost:5433` (puerto 5433 para evitar colisiones con PostgreSQL local) |
| Base de Datos | `retail_inventory_db` |
| Usuario / Contraseña | `retail_admin` / `retail_secret` |
| DSN | `postgresql+psycopg2://retail_admin:retail_secret@localhost:5433/retail_inventory_db` |

Puedes sobreescribir cualquiera de estos valores con un archivo `.env` (ver [`.env.example`](.env.example)) o directamente desde la consola: `make run-etl DB_PORT=5555`.

---

## 2. Arquitectura del Repositorio

```
retail-inventory-analytics-3nf/
│
├── docker-compose.yml              # PostgreSQL 15 + pgAdmin opcional; scripts de inicio ordenados
├── Makefile                        # Automatizador: pipeline, models, forecast, test, ...
├── requirements.txt / -dev.txt     # Dependencias de ejecución / de pruebas
├── .github/workflows/ci.yml        # CI: unitarias, integración con PostgreSQL real, Compose
│
├── sql/
│   ├── 01_schema_ddl_3nf.sql       # Esquema 3NF de 10 tablas, restricciones CHECK, índices parciales
│   ├── 02_seed_data.sql            # Datos maestros de referencia
│   ├── 03_analytical_queries.sql   # 4 consultas analíticas: CTEs + funciones de ventana
│   ├── 04_triggers_and_procedures.sql  # Procedimiento FEFO, trigger antifraude, datamart materializado
│   ├── 05_tests_business_logic.sql # 19 aserciones SQL en una transacción que termina en ROLLBACK
│   ├── 06_ops_schema.sql           # Esquema ops: corridas, tareas, modelos, resultados de calidad
│   ├── 07_ml_schema.sql            # Esquema ml: pronósticos, backtest, reposición, lotes por vencer
│   └── models/                     # Esquema en estrella (mart.*): 5 dimensiones, 4 hechos, vistas, pruebas
│
├── src/
│   ├── data_generator.py           # Simulación de inventario; 3 políticas de reposición (stdlib)
│   ├── etl_pipeline.py             # Extracción -> 23 reglas -> UPSERT idempotente -> auditoría
│   ├── dq.py / dq_report.py        # Catálogo de reglas, puntuación vs verdad de terreno, reporte
│   ├── model_runner.py             # Construye y prueba los modelos SQL (dependencias, incremental)
│   ├── forecasting.py              # 5 modelos, backtest de origen móvil, proyección FEFO, pedidos
│   ├── policy_experiment.py        # ¿Reponer con pronóstico reduce la merma? (stdlib)
│   ├── pipeline.py                 # Orquestador: DAG, reintentos, aislamiento de fallas
│   └── dbutil.py                   # Conexión compartida
│
├── tests/                          # 219 pruebas: 187 unitarias, 32 contra PostgreSQL real
│
├── data/
│   ├── raw/                        # CSVs generados + _manifest.json (git-ignorados, reproducibles)
│   └── quarantine/                 # Filas rechazadas por el quality gate, con la regla fallida
│
├── docs/
│   ├── architecture_spec.md        # Especificación técnica de todas las capas
│   ├── business_case_study.md      # Caso de negocio: KPIs, composición de mermas, ROI, evidencia experimental
│   ├── data_quality_report.md      # Reporte de calidad generado desde datos (versionado)
│   ├── data_quality/history.jsonl  # Una línea por corrida de calidad: la tendencia (versionado)
│   └── experiments/                # Informe del experimento de políticas + gráfica de frontera
│
├── bi/
│   └── dax_measures.md             # Esquema en estrella y medidas DAX para Power BI
│
└── scripts/
    └── run_project.ps1             # Automatizador nativo para Windows (equivalente a Makefile)
```

---

## 3. Arquitectura Relacional (3NF)

El modelo elimina anomalías de inserción, actualización y eliminación, garantizando la integridad de dominio mediante restricciones `CHECK`, columnas calculadas almacenadas (`GENERATED ALWAYS AS`) e índices parciales.

```mermaid
erDiagram
    stores ||--o{ goods_receipt_headers : recibe
    stores ||--o{ inventory_batches : almacena
    stores ||--o{ waste_log : registra_merma
    stores ||--o{ daily_sales : registra_venta

    suppliers ||--o{ goods_receipt_headers : despacha

    product_categories ||--o{ products : categoriza
    product_categories ||--o{ product_categories : categoria_padre

    products ||--o{ goods_receipt_items : contiene
    products ||--o{ inventory_batches : genera_lotes
    products ||--o{ waste_log : genera_desperdicio
    products ||--o{ daily_sales : vende

    goods_receipt_headers ||--|{ goods_receipt_items : lineas_de_guia
    goods_receipt_items ||--o{ inventory_batches : crea_lote

    waste_reasons ||--o{ waste_log : clasifica
    inventory_batches ||--o{ waste_log : audita
```

| Tabla | Rol en el Sistema |
|---|---|
| `stores` | Tiendas multiformato (`CONVENIENCE`, `EXPRESS`, `FLAGSHIP`) |
| `suppliers` | Catálogo de proveedores con condiciones de pago y tiempos de entrega (lead times) |
| `product_categories` | Jerarquía recursiva con clasificación de temperatura de almacenamiento |
| `products` | Maestro de SKUs: costos, precios, vida útil, stock de seguridad y punto de reorden |
| `goods_receipt_headers` / `_items` | Auditoría de dos niveles: guía de remisión del proveedor vs. conteo físico en tienda |
| `inventory_batches` | **El núcleo FEFO** — control de vencimiento y existencias disponibles por lote |
| `waste_log` | Libro de bajas operativas: pérdida monetaria, motivo, operador y autorizador |
| `daily_sales` | Agregados de ventas POS por tienda × SKU × fecha |

### Integridad forzada en el motor de base de datos, no en la aplicación

- `discrepancy_qty` — `GENERATED ALWAYS AS (qty_dispatched - qty_accepted) STORED`: las mermas o faltantes del proveedor se convierten en una columna auditable directa, sin necesidad de conciliación manual.
- `total_loss_amount` — `GENERATED ALWAYS AS (qty_discarded × unit_cost_at_loss) STORED`: el valor de la merma jamás puede desalinearse de sus componentes de cálculo.
- `CHECK (current_qty <= initial_qty)`: hace matemáticamente imposible una violación de balance de masa en un lote.
- `CHECK (retail_price >= unit_cost)`: bloquea la inserción de SKUs con margen comercial negativo desde la base de datos.
- `idx_batches_expiry_status ... WHERE batch_status = 'ACTIVE'`: un **índice parcial** que optimiza la búsqueda FEFO para escanear únicamente inventario disponible para la venta.

---

## 4. Lógica Transaccional de Negocio — `sql/04`

### `sp_process_fefo_sale(store, product, qty, [date], [user], [register_sale])`

Aplica la rotación **FEFO (First-Expired, First-Out / Primero en Vencer, Primero en Salir)** en el único lugar donde no puede ser vulnerada: dentro de la transacción de la base de datos.

```sql
CALL sp_process_fefo_sale(1, 1, 5);
```

- Recorre los lotes en estado `ACTIVE` ordenados por `ORDER BY expiration_date ASC, batch_id ASC` — el vencimiento más próximo primero, utilizando FIFO como criterio de desempate.
- Descuenta existencias lote por lote; si un lote llega a cero, pasa automáticamente a estado `DEPLETED`.
- Bloquea las filas candidatas con `FOR UPDATE`, asegurando que dos terminales POS concurrentes no puedan sobrevender el mismo lote causando stock negativo.
- Excluye lotes vencidos: el inventario legalmente no comercializable no es considerado disponible.
- Genera la excepción **`SQLSTATE P0002` — "Stockout / Insufficient Inventory"** detallando tienda, SKU, cantidad solicitada y cantidad disponible real.
- Actualiza `daily_sales` calculando el **costo de ventas real (COGS)** a nivel de los lotes consumidos, en lugar de un promedio de catálogo — garantizando reportes de margen fidedignos.

### `trg_audit_high_waste_alert` — Control antifraude

Trigger `BEFORE INSERT OR UPDATE` sobre `waste_log`, asegurando segregación de funciones en mermas de alto valor:

| Control | Comportamiento |
|---|---|
| Pérdida > S/ 50 sin `authorized_by` | **Bloqueada** — `SQLSTATE P0004` |
| Pérdida > S/ 50 auto-autorizada por el mismo operador | **Bloqueada** — violaría segregación de funciones |
| `qty_discarded` superior al `initial_qty` del lote | **Bloqueada** — baja de stock físicamente imposible |
| Pérdida > S/ 50 debidamente autorizada | Permitida, emitiendo un aviso formal `HIGH-WASTE ALERT` |

> **Detalle técnico de implementación en PostgreSQL:** `total_loss_amount` es una columna `GENERATED`, y PostgreSQL calcula las columnas generadas *después* de ejecutar los triggers `BEFORE` de fila — por lo que `NEW.total_loss_amount` es `NULL` dentro del trigger. La función recalcula el importe directamente a partir de sus columnas base (`qty_discarded * unit_cost_at_loss`). Este diseño intencional previene errores silenciosos comunes en triggers sobre columnas calculadas.

### `sp_expire_overdue_batches([as_of_date], [authorizer])`

Barrido nocturno automatizado: identifica lotes `ACTIVE` cuya fecha de vencimiento ya pasó, los marca como `EXPIRED` y transfiere automáticamente el saldo restante a `waste_log` bajo el motivo `EXPIRATION` — la merma específica que la disciplina FEFO busca reducir al mínimo.

### `mv_store_category_waste_summary` + `fn_refresh_waste_datamart()`

Datamart materializado a grano de **tienda × categoría × mes**, diseñado para alimentar Power BI: ventas brutas, margen, merma desglosada por clasificación, `waste_intensity_pct`, `unit_shrinkage_pct` y `fefo_addressable_pct`.

```sql
SELECT fn_refresh_waste_datamart();  -- Refresco CONCURRENTLY por defecto
```

Respaldado por el índice único `uq_mv_waste_summary_grain`, requerido para ejecutar `REFRESH MATERIALIZED VIEW CONCURRENTLY` — permitiendo que los reportes y dashboards continúen consultando datos sin bloqueos de lectura (`ACCESS EXCLUSIVE`).

---

## 5. Consultas Analíticas Avanzadas — `sql/03`

```bash
make run-queries
```

| # | Consulta Analítica | Técnica SQL Empleada | Decisión de Negocio que Resuelve |
|---|---|---|---|
| 1 | **Matriz de Riesgo de Vencimiento FEFO** | `DENSE_RANK() OVER (PARTITION BY store, category)` | Qué lotes rematar con descuento *hoy*, priorizados por capital monetario en riesgo |
| 2 | **Auditoría de Descuadres GRN vs. Guía** | Agrupación y conciliación multinivel con CTEs | A qué proveedores emitir notas de crédito por faltantes sistemáticos de entrega |
| 3 | **Ratio de Intensidad de Mermas** | CTEs + ranking analítico de ventanas | Qué tienda está perdiendo más dinero por cada sol vendido |
| 4 | **DSI y Velocidad de Rotación vs. Quiebre** | Promedios móviles con funciones de ventana | Qué SKUs reabastecer de urgencia y cuáles pausar por sobrestock |

La Consulta 1 representa el núcleo operativo: reporta el **capital actualmente en riesgo** en lotes que vencerán en las próximas 48 horas y 5 días — en lugar de limitarse a reportar lo que ya se perdió. Esto transforma la gestión de mermas de una autopsia mensual a una lista de trabajo operativa diaria.

---

## 6. Generador de Datos Sintéticos — `src/data_generator.py`

```bash
python src/data_generator.py --days 60 --seed 20261012 --end-date 2026-10-11
```

> **Fije `--end-date` para reproducir las cifras de este documento.** Sin él, el generador simula los 60 días hasta *ayer*, de modo que cambia todos los días. Las cifras citadas aquí corresponden a la ventana que termina el 2026-10-11. Con `make`: `make generate SIM_END=2026-10-11`.

Sin dependencias externas — desarrollado exclusivamente con la librería estándar de Python. Características que hacen que la simulación sea realista:

**1. Estacionalidad de demanda multiplicativa:**
`demanda = base × trafico_tienda × dia_semana × quincena × temperatura × ruido_lognormal`
Incrementos medidos en fin de semana (viernes a domingo) en el dataset generado:

| SKU | Promedio Lun–Jue | Promedio Vie–Dom | Incremento (Uplift) |
|---|---:|---:|---:|
| Cerveza Pilsen Callao 650ml | 14.6 unid. | 36.1 unid. | **2.47×** |
| Empanada de Carne (vida útil 2 días) | 27.4 unid. | 57.4 unid. | **2.09×** |
| Leche Fresca UHT 1L | 14.8 unid. | 19.5 unid. | 1.31× |

La curva térmica anual de Lima (15–27 °C) modula la elasticidad de bebidas y la probabilidad de fallas en la cadena de frío. Los ciclos peruanos de pago (quincenas y fin de mes) elevan el ticket promedio de compra.

**2. Motor de inventario real, no números inventados:** Cada tienda mantiene lotes físicos con fecha de caducidad. El reabastecimiento respeta los tiempos de entrega del proveedor según la vida útil del producto (empanadas de 2 días se reciben a diario; lácteos de 21 días interdiario; abarrotes semanalmente). Las ventas consumen inventario bajo **estricto criterio FEFO** — la misma regla que aplica `sp_process_fefo_sale`. La demanda no satisfecha se registra como quiebre de stock, evitando stocks negativos irreales.

**3. Mermas orgánicas derivadas de la simulación:** La merma por vencimiento surge de lo que la rotación FEFO no logró vender a tiempo — nunca de valores fijos arbitrarios. Daños físicos, fallas de frío y pérdidas desconocidas se inyectan como incidentes operativos estocásticos.

**4. Inyección deliberada de anomalías, con verdad de terreno:** Se plantan defectos de manera controlada para someter a prueba la compuerta ETL, y `_manifest.json` registra la clave natural de **cada uno**. Así el *recall* de la compuerta se mide fila a fila en lugar de afirmarse (ver [sección 12](#12-calidad-de-datos-versionada)). Un faltante "no documentado" lo es de verdad: no lleva nota. Antes, el texto de la nota lo delataba, lo que habría entregado la respuesta a cualquier regla.

**5. Tres políticas de reposición** (`--policy heuristic|level|forecast`): la heurística estática heredada, un nivel estadístico sin estacionalidad y un nivel basado en pronóstico semanal. Las dos últimas aprenden **solo de las ventas ya observadas**, corrigiendo la censura por quiebres. La política `heuristic` reproduce byte a byte la línea base original (verificado).

| Archivo Generado | Registros (60 días) |
|---|---:|
| `stores.csv`, `suppliers.csv`, `product_categories.csv`, `products.csv`, `waste_reasons.csv` | Catálogos maestros de referencia |
| `goods_receipts.csv` | 1,526 recepciones |
| `batches.csv` | 1,584 lotes |
| `waste_log.csv` | 128 registros de merma |
| `daily_sales.csv` | 3,590 registros de venta |

La reproducibilidad está garantizada: el cálculo de calendarios de despacho utiliza CRC32 en lugar del `hash()` nativo de Python (el cual introduce sales aleatorias por proceso y rompería la consistencia de la semilla).

---

## 7. Pipeline ETL y Auditoría de Calidad — `src/etl_pipeline.py`

```bash
python src/etl_pipeline.py --dry-run                 # Modo validación (quality gate sin BD)
python src/etl_pipeline.py --dsn "postgresql+psycopg2://..."   # Carga completa a base de datos
```

```
data/raw/*.csv
     │
     ▼
[ EXTRACCIÓN ]     Lectura tipada, validación de contrato de esquema y dtypes
     │
     ▼
[ TRANSFORMACIÓN ] Resolución de claves naturales, columnas derivadas, deduplicación
     │
     ▼
[ CALIDAD DE DATOS] 23 reglas × 4 severidades — FATAL aborta, ERROR envía a cuarentena
     │
     ▼
[ CARGA ]          UPSERT por bloques, una transacción por tabla en orden de FKs
     │
     ▼
[ AUDITORÍA ]      docs/etl_execution.log + data/quarantine/*.csv + registro de calidad (ops.dq_*)
```

### Batería de 23 Reglas de Calidad

| Regla | Severidad | Validación Realizada |
|---|---|---|
| `G01` | ERROR | Columna obligatoria nula o falla en conversión de tipo de dato |
| `G02` | ERROR | Clave natural duplicada en el archivo fuente |
| `R01`–`R03` | ERROR | Cantidad despachada no positiva, **cantidad recibida negativa**, sobre-recepción |
| `R04` | WARNING | **Descuadre entre guía de remisión y conteo físico** (merma de proveedor) |
| `R05` | WARNING | Temperatura de frío fuera del rango operativo admisible (−25 °C a 85 °C) |
| `R06` | WARNING | **Faltante contra la guía SIN motivo documentado** — separa un rechazo explicado de un faltante que nadie justificó |
| `B01` | ERROR | **`expiration_date` anterior a `manufacturing_date`** (fecha de caducidad ilógica) |
| `B02`–`B04` | ERROR | `initial_qty` no positivo, existencia negativa, balance de masa inconsistente |
| `B05` | WARNING | Vida útil observada difiere del maestro de SKUs (posible error de rotulado) |
| `B06` | WARNING | Lote en estado `ACTIVE` ya vencido que aún conserva existencias (falla FEFO) |
| `W01`–`W02` | ERROR | Cantidad desechada o costo unitario no positivos |
| `W03` | ERROR | **Merma de alto valor (> S/ 50) sin autorizador** — emula `trg_audit_high_waste_alert` |
| `W04` | ERROR | Merma de alto valor auto-autorizada por el mismo operador que registra |
| `S01`, `S03` | ERROR | Importes de venta negativos; discrepancia entre unidades e ingresos |
| `S02` | WARNING | Ingreso por venta inferior al costo (COGS) — margen negativo |
| `S04` | WARNING | Fecha de venta posterior a la fecha actual — descalibración del reloj POS |
| `F01` | ERROR/WARN | Clave foránea huérfana en referencias cruzadas entre tablas |

### Propiedades de Ingeniería del Pipeline

- **Idempotencia verificada:** Toda carga es un upsert sobre la clave natural. Una versión previa cargaba `goods_receipt_items` con `DELETE` + `INSERT`, lo que viola la FK `inventory_batches.grn_item_id` en la *segunda* corrida; la transacción se revertía y los conteos coincidían, así que *parecía* idempotente. La prueba que lo protege compara sumas de verificación de **contenido** tras ejecutar el pipeline completo dos veces.
- **Cada regla registra filas evaluadas y fallidas, incluso las que no encontraron nada:** "0 hallazgos" no distinguía un dataset limpio de una regla que nunca corrió.
- **Claves Naturales en lugar de SERIALs:** Los CSVs transportan `store_code`, `sku_code`, `grn_number`; las claves subrogadas se resuelven contra la base de datos viva tras cargar cada nivel de dependencia — asegurando compatibilidad con datos precargados.
- **Cuarentena en lugar de coerción silenciosa:** Ninguna fila que viole una regla de nivel ERROR es forzada o alterada en la base de datos. Se redirige a `data/quarantine/` etiquetada con la regla infringida para su subsanación en origen.
- **Tipado seguro:** La ingesta inicial se realiza como texto (`str`) y se procesa columna por columna con `errors="coerce"`, convirtiendo errores en nulos detectables en lugar de alterar tipos de datos de forma inadvertida.
- **Alineación con triggers:** La regla `W03` intercepta preventivamente lo que `trg_audit_high_waste_alert` rechazaría en base de datos, generando un hallazgo legible en la bitácora en lugar de abortar la carga con una excepción genérica.

### Salida Ejemplo de Auditoría

Ventana que termina el 2026-10-11 (`--end-date 2026-10-11`):

```
[ERROR  ] R02 | goods_receipts.csv |      2 row(s) | Cantidad recibida negativa: conteo físico imposible
[WARNING] R04 | goods_receipts.csv |    105 row(s) | Descuadre entre guía y conteo físico (483.00 unidades faltantes)
[WARNING] R06 | goods_receipts.csv |      6 row(s) | Faltante contra la guía SIN motivo documentado
[ERROR  ] B01 | batches.csv        |      2 row(s) | expiration_date es anterior a manufacturing_date
[ERROR  ] W03 | waste_log.csv      |      3 row(s) | Merma superior a S/ 50.00 sin usuario autorizador
[WARNING] F01 | waste_log.csv      |      1 row(s) | Referencia huérfana: batch_code no coincide con ningún lote

ETL EXECUTION SUMMARY
  rows extracted    : 6,863 across 9 dataset(s)
  rows quarantined  : 7
  acceptance rate   : 99.898%
```

Nótese la cascada en la última fila: un lote enviado a cuarentena por `B01` dejó una merma que apunta a un lote que ya no existe, y `F01` la detectó. Las reglas se componen.

---

## 8. Modelo Dimensional con Carga Incremental — `sql/models/`

```bash
make models
```

Un ejecutor propio (`src/model_runner.py`, sin dependencias) con la parte de dbt que importa a este tamaño: **orden de construcción, carga incremental y pruebas tras cada construcción**.

| Tipo | Modelos | Estrategia |
|---|---|---|
| `table` | `dim_date`, `dim_store`, `dim_supplier`, `dim_waste_reason` | Upsert completo |
| `scd2` | `dim_product` | Cada cambio de costo, precio o vida útil abre una versión nueva |
| `incremental` | `fact_sales_daily`, `fact_waste`, `fact_receipt_line` | Reprocesa desde (marca de agua − `lookback`) y hace upsert |
| `snapshot` | `fact_stock_position` | Una instantánea por corrida; nunca se trunca |
| `view` | `mart_views` | `v_store_day_kpi`, `v_supplier_scorecard`, `v_weekday_demand_profile`, `v_fefo_exposure_latest` |

- **SCD tipo 2 con consulta punto-en-el-tiempo.** Un hecho se valora con la versión del producto vigente *el día en que ocurrió*. Con tipo 1, reprecificar un SKU reescribiría en silencio el margen de toda venta pasada.
- **Ventana de retroceso.** Un incremental estricto nunca vería datos tardíos o corregidos del POS. Se reprocesan los últimos 3 días. En una re-ejecución real `fact_sales_daily` reprocesó **240 de 3,579 filas**. El costo está documentado y probado: un cambio más antiguo que la ventana no se recoge hasta `--full-refresh`.
- **La instantánea es la única forma de tener historia de stock**: `inventory_batches` solo guarda la cantidad *actual*, así que el stock de ayer no puede reconstruirse.
- **13 pruebas de modelo**, cada una una consulta que devuelve las filas que *violan* una expectativa (cero filas = aprobado). La mayoría son **conciliaciones al centavo** contra el origen 3NF, lo que prueba que ni el incremental, ni el join SCD2, ni el mapeo de claves perdieron o duplicaron filas.

Detalle de diseño en [`docs/architecture_spec.md`](docs/architecture_spec.md) §11.

---

## 9. Pronóstico de Demanda y Reposición

```bash
make forecast
```

Pronostica 14 días por tienda × SKU con **cinco modelos** (`seasonal_naive`, `moving_average`, `weekday_mean`, `holt_winters` amortiguado, `ensemble`), proyecta qué lotes se vencerán sin venderse y recomienda pedidos. Todo se persiste en `ml.*` con fecha de generación, de modo que un pronóstico nunca se pisa con el siguiente.

**Evaluación que no se engaña a sí misma** (60 series, ventanas que ni la elección del modelo ni la calibración vieron):

| Modelo | WAPE |
|---|---:|
| `seasonal_naive` (la vara a superar) | 30.0% |
| `moving_average` | 36.0% |
| `holt_winters` | 23.5% |
| `weekday_mean` | 23.4% |
| **`ensemble` (el que se despliega)** | **23.1%** |
| Mejor modelo elegido *por serie* | 24.1% |

- **Elegir el mejor modelo por serie empeora el resultado** (24.1% vs 23.1%): con dos ventanas por serie, escoger entre cinco se sobreajusta al ruido. Por eso el valor por defecto es el `ensemble` fijo. *Salvedad:* ese valor por defecto se fijó viendo estos números, así que 23.1% es ligeramente optimista.
- **El intervalo "al 80%" cubría solo 66%.** Calibrar con 14 residuos por serie subestima la dispersión; calibrar con residuos relativos agrupados entre series lo lleva a **80.4%**.

Ejemplo real, empanada en TB-101 (patrón semanal capturado; intervalo 80%):

| Dom | Lun | Mar | Mié | Jue | **Vie** | **Sáb** |
|---:|---:|---:|---:|---:|---:|---:|
| 68 (46–95) | 36 (24–50) | 31 (21–43) | 30 (20–42) | 28 (19–39) | **81** (55–114) | **87** (59–123) |

**Proyección FEFO hacia adelante.** Consume los lotes en mano contra el pronóstico en estricto orden de vencimiento y reporta cuáles terminarán en merma: la lista de qué rebajar *hoy*, mientras aún tiene valor. FEFO la hace independiente de entregas futuras, y un lote que vence más allá del horizonte reporta cero pérdida porque el pronóstico no ve tan lejos (ausencia de pérdida proyectada no es evidencia de que no la habrá).

*Limitación conocida:* el historial es de **ventas**, no de demanda. Un día de quiebre subestima lo que los clientes querían y el modelo aprende esa subestimación.

---

## 10. Experimento: ¿Reducir la Merma Compensa los Quiebres?

```bash
make experiment
```

El caso de negocio supone que reponer mejor recupera entre 25% y 45% de la merma por vencimiento. `src/policy_experiment.py` lo **mide**: tres políticas sobre el mismo simulador, con el nivel de servicio elegido en 20 semillas de entrenamiento y evaluado en **100 semillas distintas**.

| Política (media de 100 corridas de 60 días) | Utilidad neta | Merma | de la cual vencimiento | Ventas perdidas |
|---|---:|---:|---:|---:|
| Heurística actual | S/ 178,105 | S/ 2,343 | S/ 1,778 | S/ 3,729 |
| Nivel plano (sin estacionalidad) | S/ 177,294 | S/ 1,508 | S/ 1,001 | S/ 7,774 |
| **Pronóstico estacional** | **S/ 178,756** | **S/ 1,381** | **S/ 836** | S/ 4,874 |

Diferencias pareadas por semilla, IC 95% bootstrap:

| Comparación | Utilidad neta | Lectura |
|---|---:|---|
| Pronóstico vs. heurística | **+S/ 651** [+358, +952] | Mejor, significativo |
| Nivel plano vs. heurística | −S/ 811 [−1,110, −504] | **Peor**, significativo |
| Pronóstico vs. nivel plano | +S/ 1,462 [+1,209, +1,714] | El valor está en el pronóstico |

1. **La merma por vencimiento baja 53%, pero la utilidad neta sube 0.4%.** Solo el 67.7% del ahorro bruto de merma sobrevive como utilidad neta; el resto se pierde en ventas no realizadas.
2. **Bajar el stock sin estacionalidad pierde dinero.** El nivel plano existe precisamente para separar el valor del *pronóstico* del valor de *dimensionar mejor*.
3. **A escala de 400 tiendas** equivale a ≈ **S/ 396 mil al año** (IC 95%: S/ 218 mil – S/ 579 mil): entre el escenario conservador y el base del caso de negocio.

![Frontera merma vs. ventas perdidas](docs/experiments/policy_frontier.svg)

**Limitaciones, sin maquillar:** la demanda simulada es fuertemente semanal *por construcción* (un pronóstico por día de la semana está hecho para encontrar esa señal); la heurística conoce la tasa base real y las políticas aprendidas no; y la utilidad neta omite costo de inventario y pérdida de clientes. Las dos primeras versiones del experimento estaban mal especificadas (cobertura `intervalo + 1` heredada; descartar los días de quiebre sesgaba la demanda a la baja) y se documentan en el [informe completo](docs/experiments/policy_comparison.md).

---

## 11. Orquestación y Observabilidad

```bash
make pipeline
```

```
bootstrap --+
            +--> etl --+--> models --> forecast
generate ---+          |
                       +--> dq_report
```

Corrida real (7.4 s):

```
[1/6] bootstrap   SUCCESS      0.3s
[2/6] generate    SUCCESS      0.1s
[3/6] etl         SUCCESS      5.1s      defects_caught: 12/12
[4/6] dq_report   SUCCESS      0.0s
[5/6] models      SUCCESS      0.3s      tests_passed: 13/13
[6/6] forecast    SUCCESS      1.6s      holdout_wape: 0.2314, interval_coverage: 0.8036
```

- **Dependencias, no una lista fija.** Si `etl` falla, `models` y `forecast` se **omiten** (pronosticar sobre un almacén a medio cargar es peor que no pronosticar); lo que no depende de la falla sigue corriendo. Una tarea omitida nombra la **causa raíz** (`upstream failed: etl`), no su vecino omitido.
- **Reintentos solo donde ayudan.** Una conexión rechazada o un *timeout* se reintentan con retroceso exponencial. Una compuerta de calidad o una prueba fallida **no**: volver a correr falla igual y ocultaría el problema.
- **Ejecución parcial:** `--from models`, `--only forecast`, `--skip generate`.
- **Cada corrida queda registrada** en `ops.pipeline_runs` y `ops.task_runs`:

```sql
SELECT * FROM ops.v_run_history;   -- ¿funcionó la carga de anoche?
SELECT * FROM ops.v_dq_trend;      -- ¿qué tan limpios venían los datos?
```

---

## 12. Calidad de Datos Versionada

```bash
make dq-report      # genera docs/data_quality_report.md
```

El reporte [`docs/data_quality_report.md`](docs/data_quality_report.md) se genera desde datos —nunca a mano— y cita la corrida que lo produjo. Cada ejecución agrega una línea a [`docs/data_quality/history.jsonl`](docs/data_quality/history.jsonl), por lo que la calidad tiene una tendencia bajo control de versiones.

- **Reglas clasificadas por dimensión** (completitud, unicidad, validez, consistencia, integridad, oportunidad) y agregadas en un cuadro de mando.
- **Puntuación contra la verdad de terreno.** El generador registra cada defecto que plantó; compararlo fila a fila mide el *recall* en lugar de asumirlo. Un defecto cuenta como detectado solo si *esa fila* fue marcada por *la regla responsable*:

| Defecto plantado | Regla | Plantados | Detectados |
|---|---|---:|---:|
| `expiry_before_manufacturing` | `B01` | 2 | 2 |
| `negative_received_qty` | `R02` | 2 | 2 |
| `guide_vs_count_discrepancy` | `R06` | 6 | 6 |
| `unauthorized_high_waste` | `W03` | 2 | 2 |

**65 de 65 defectos detectados a lo largo de 6 conjuntos de datos independientes** (semillas distintas).

- **Alcance honesto:** el *recall* se mide contra los defectos que alguien pensó en inyectar. Dice poco sobre los que nadie imaginó; con datos reales no hay lista contra la cual puntuar.
- **El `git_sha` lleva el sufijo `+dirty`** si hay cambios sin commitear: un hash desnudo afirmaría que *ese* commit produjo el resultado.

---

## 13. Pruebas — 219 Python y 19 SQL

```bash
make test          # Python, luego SQL
make test-unit     # solo Python (las de integración se omiten si no hay PostgreSQL)
make test-sql      # solo las 19 aserciones SQL
```

### Suite SQL — `sql/05` (19 aserciones)

Valida el comportamiento crítico de negocio. Toda la suite corre en **una sola transacción** que termina en `ROLLBACK`, haciéndola idempotente y no destructiva — segura de ejecutar en entornos con datos reales. Cada prueba empieza reconstruyendo su fixture con `pg_temp.reset_fixture()`.

| Grupo | Comportamiento Validado |
|---|---|
| **T1 — Rotación FEFO** | El lote más próximo a vencer se consume primero; ventas multi-lote respetan el orden cronológico; lotes en cero pasan a `DEPLETED`; nunca se comercializan lotes vencidos |
| **T2 — Quiebres de Stock** | Existencias insuficientes detonan `P0002`; ventas fallidas dejan el inventario intacto (atomicidad); se rechazan cantidades negativas; `daily_sales` registra ingresos y COGS exactos |
| **T3 — Antifraude** | Se bloquea merma material no autorizada; se acepta y valoriza merma debidamente visada; mermas menores a S/ 50 no exigen firma; se bloquea la auto-autorización |
| **T4 — Barrido y Datamart** | El barrido nocturno reclasifica y da de baja existencias vencidas; la vista materializada existe, tiene clave única y carece de duplicados |

> **Un bug del propio arnés, encontrado al ejecutarlo contra una base real.** La primera versión aislaba cada prueba con `SAVEPOINT` + `ROLLBACK TO SAVEPOINT`, pero ese rollback también revierte los `INSERT` de cada aserción en la tabla de resultados: el resumen decía **3 de 3** cuando se habían ejecutado 19. Las aserciones sí corrían (un fallo lanza una excepción), pero el resumen mentía.

Los lotes de prueba se insertan en **orden cronológico inverso** para forzar a que el procedimiento ordene por FEFO por lógica propia y no por el orden físico de inserción.

### Suite Python — `tests/` (219 pruebas)

187 unitarias y 32 de integración. Cada prueba de integración crea **una base de datos temporal** desde los archivos SQL reales y la destruye al terminar; si PostgreSQL no está, se *omiten* en lugar de fallar.

| Archivo | Pruebas | Qué demuestra |
|---|---:|---|
| `test_forecasting.py` | 56 | Modelos contra series de estructura conocida; **sin look-ahead**; calibración; proyección FEFO |
| `test_pipeline.py` | 35 | Orden del grafo, aislamiento de fallas, reintentos solo transitorios |
| `test_dq.py` | 28 | Catálogo de reglas, puntuación contra verdad de terreno |
| `test_generator.py` | 25 | Determinismo, línea base intacta, la verdad de terreno no miente |
| `test_etl_gate.py` | 24 | Cada regla con una fila que debe fallar y otra que no |
| `test_model_runner_unit.py` | 19 | Cabeceras, dependencias, ciclos |
| `test_pipeline_integration.py` | 18 | **El pipeline real dos veces**, con una falla inyectada |
| `test_models_integration.py` | 14 | Incremental, SCD2, instantáneas, idempotencia |

### Prueba de mutación

Que pasen no demuestra que sirvan. Se introdujeron cinco defectos a propósito y se verificó que la prueba responsable fallara (archivos restaurados y verificados por hash): **5 de 5 detectados.**

| Defecto introducido | Lo detecta |
|---|---|
| La carga de recepciones vuelve a `DELETE` + `INSERT` | Ejecutar el pipeline completo dos veces |
| El backtest filtra un día del futuro | Prueba de no-look-ahead |
| Los hechos se unen a la versión *actual* del producto | Prueba SCD2 punto-en-el-tiempo |
| `R06` deja de marcar faltantes sin motivo | Puntuación contra la verdad de terreno |
| Se desactiva la calibración de intervalos | Prueba de cobertura |

---

## 14. Inteligencia de Negocios (Power BI)

El modelo dimensional proyecta las tablas transaccionales hacia un esquema en estrella para reportes ejecutivos. El esquema **ya no es solo documentación**: se implementa y se prueba en `sql/models/` ([sección 8](#8-modelo-dimensional-con-carga-incremental--sqlmodels)). Especificación completa de medidas DAX en [`bi/dax_measures.md`](bi/dax_measures.md).

| KPI | Fórmula / Definición |
|---|---|
| **Intensidad de Merma (%)** | `SUM(total_loss_amount) / SUM(gross_revenue)` |
| **Merma Abordable por FEFO (%)** | `Pérdida por VENCIMIENTO / Pérdida Total por Merma` |
| **Exposición Financiera (S/ en Riesgo)** | `SUM(current_qty × unit_cost)` para lotes que vencen en ≤ 5 días |
| **Cumplimiento de Entrega (Fill Rate %)** | `SUM(qty_accepted) / SUM(qty_dispatched)` |
| **Días de Venta de Inventario (DSI)** | `stock_actual / promedio_diario_ultimos_7_dias` |

Power BI se conecta directamente a `mv_store_category_waste_summary` en lugar de a las tablas transaccionales — garantizando que los tableros consulten una estructura preagregada e indexada en lugar de realizar escaneos masivos en la base de datos de producción.

---

## 15. Impacto y Resultados de Negocio

Resultados medidos sobre el dataset simulado de 60 días para 4 tiendas (`--end-date 2026-10-11`, **una sola realización**: la merma por vencimiento de esta semilla es 23% mayor que el promedio de 100 semillas):

| Métrica de Negocio | Valor Obtenido |
|---|---:|
| Ventas Brutas Totales | S/ 416,525.40 |
| Margen Bruto Comercial | S/ 179,095.70 (43.00%) |
| Costo Total de Mermas | S/ 2,901.27 (0.70% de las ventas) |
| **Participación de Vencimiento en Mermas** | **75.4% — Mitigable vía disciplina FEFO** |
| Nivel de Servicio de Proveedores (Fill Rate) | 99.30% (483 unidades faltantes no entregadas) |
| Ventas Perdidas por Quiebre de Stock | S/ 4,171.60 |
| Dispersión de Intensidad de Merma entre Tiendas | 0.326% → 0.981% (**Variación de 3.0×**) |

**Proyección para un despliegue en cadena de 400 tiendas (escenario base):**
* **Beneficio Anual Estimado:** **S/ 1.68 Millones**
* **Periodo de Recuperación:** **12.8 meses**
* **Retorno sobre la Inversión (ROI a 3 años):** **109%**

Esa proyección descansa en tasas de recuperación **supuestas**. El experimento de la [sección 10](#10-experimento-reducir-la-merma-compensa-los-quiebres) las contrasta: el beneficio neto medido de la palanca de reposición (≈ S/ 396 mil al año a 400 tiendas) queda entre el escenario conservador y el base, lo que respalda el supuesto sin demostrarlo sobre datos reales.

Cada cifra cuenta con su etiqueta metodológica (MEDIDO, EXPERIMENTO, BENCHMARK o SUPUESTO), acompañada de análisis de sensibilidad y limitaciones en [`docs/business_case_study.md`](docs/business_case_study.md).

---

## 16. Stack Tecnológico

| Capa | Tecnología Empleada |
|---|---|
| **Base de Datos** | PostgreSQL 15 — Modelo 3NF, columnas generadas, índices parciales, vistas materializadas |
| **Lógica Transaccional** | PL/pgSQL — Stored procedures, triggers `BEFORE`, bloqueo concurrente de filas (`FOR UPDATE`) |
| **Generación de Datos** | Python 3.9+ (librería estándar) — Simulación determinista de inventario y demanda retail |
| **Pipeline ETL** | Python · pandas · SQLAlchemy 2.x · psycopg2 |
| **Almacén dimensional** | SQL puro: SCD tipo 2, cargas incrementales con ventana de retroceso, instantáneas periódicas, ejecutor propio con orden topológico |
| **Pronóstico** | numpy · Holt-Winters amortiguado, backtest de origen móvil, intervalos calibrados, proyección FEFO |
| **Orquestación** | Python estándar — DAG, reintentos con retroceso exponencial, aislamiento de fallas, registro en `ops.*` |
| **Infraestructura** | Docker Compose — Scripts de inicio ordenados, healthchecks y volúmenes persistentes |
| **Automatización** | GNU Make · PowerShell nativo para Windows |
| **Pruebas y Validación** | 219 pruebas pytest (32 contra PostgreSQL real con base temporal por prueba) · 19 aserciones SQL · prueba de mutación |
| **Integración Continua** | GitHub Actions: unitarias, servicio PostgreSQL 15, validación de Compose |
| **Inteligencia de Negocios** | Power BI — Esquema en estrella, modelado dimensional, DAX avanzado |

---

## 17. Autor

**Odar Javier Alcocer Vásquez**  
*Estudiante de Ingeniería de Sistemas de Información — Universidad Peruana de Ciencias Aplicadas (UPC)*  
*AWS Academy Cloud Foundations · Rutgers University Supply Chain Analytics · SCRUMstudy SFC™*  

[LinkedIn](https://linkedin.com/in/odar-alcocer) · [GitHub @MrOdar](https://github.com/MrOdar)
