# Especificación de Arquitectura: Plataforma Analítica de Inventarios Retail y Control de Mermas (3NF)

## 1. Resumen Ejecutivo y Propósito
Este proyecto es una solución integral de analítica e ingeniería de datos end-to-end diseñada para resolver desafíos operativos reales en cadenas de tiendas de conveniencia y retail de alta frecuencia (inspirado en entornos reales como Tiendas Tambo y operaciones de comida rápida como Bembos / Intercorp).

Conecta los flujos de trabajo operativos físicos (conteos cíclicos de inventario, conciliación de guías de remisión, rotación FEFO/FIFO, registro y auditoría de mermas) con una arquitectura relacional rigurosa (Esquema PostgreSQL 3NF) y herramientas para la toma de decisiones estratégicas (consultas SQL analíticas y métricas DAX en Power BI bajo esquema en estrella).

---

## 2. Stack Tecnológico
- **Motor de Base de Datos:** PostgreSQL 15+ (con sintaxis estándar compatible con SQL Server / BigQuery)
- **Modelado de Datos:** Tercera Forma Normal (3NF) para rigor transaccional + Esquema en Estrella para analítica de BI
- **Analítica SQL Core:** SQL Avanzado (Funciones de Ventana, Expresiones de Tabla Comunes - CTEs, Consultas Recursivas, Aritmética de Fechas)
- **Inteligencia de Negocios:** Modelado dimensional, esquema en estrella, métricas DAX (Inteligencia de Tiempo, Ratio de Merma, Días de Venta de Inventario - DSI)
- **Automatización y Control de Versiones:** Git, documentación profesional en GitHub con diagramas Mermaid integrados, Docker Compose, GNU Make y scripts PowerShell.

---

## 3. Arquitectura Entidad-Relación (3NF)

### Tablas Maestras:
1. `stores`: Sedes y tiendas físicas (código de tienda, tipo de tienda, distrito, ciudad, metros cuadrados).
2. `suppliers`: Catálogo de proveedores, datos de contacto, condiciones de pago, tiempo de entrega en días (lead time).
3. `product_categories`: Mapeo jerárquico recursivo de categorías (Perecibles, Bebidas, Listos para Comer, Abarrotes).
4. `products`: Catálogo maestro de SKUs (código SKU, código de barras, nombre, category_id, unidad de medida, costo unitario, precio de venta, vida útil en días, indicador de perecibilidad, stock mínimo de seguridad).

### Tablas Operativas y Transaccionales:
5. `goods_receipt_headers`: Recepciones formales de mercadería (GRN / Guías de Remisión) vinculando tienda, proveedor, número de factura/guía, fecha de despacho, fecha/hora de recepción y operador receptor.
6. `goods_receipt_items`: Detalle de líneas recibidas, cantidad despachada, cantidad física aceptada, costo unitario, condición de calidad.
7. `inventory_batches`: Tabla central para el seguimiento FIFO/FEFO. Almacena número de lote, product_id, store_id, grn_item_id, cantidad inicial, cantidad disponible actual, fecha de producción, fecha de vencimiento y estado ('ACTIVE', 'DEPLETED', 'EXPIRED', 'QUARANTINE').
8. `waste_reasons`: Catálogo estándar de motivos de baja operativa (ej. 'EXPIRED', 'DAMAGED_IN_TRANSIT', 'BROKEN_PACKAGING', 'COLD_CHAIN_BREACH').
9. `waste_log`: Bitácora formal de auditoría de cada unidad desechada. Batch_id, store_id, product_id, marca de tiempo, cantidad desechada, costo unitario al momento de la baja, waste_reason_id, usuario autorizador y observaciones.
10. `daily_sales`: Transacciones diarias consolidadas del POS por tienda y SKU para calcular velocidad de rotación, ritmo de ventas y probabilidad de quiebre de stock.

---

## 4. Requerimientos Analíticos (Entregables SQL)
1. **Matriz de Riesgo de Vencimiento FEFO/PEPS:** Identifica lotes activos que vencen en 3, 7 y 14 días, calculando la exposición financiera en riesgo (S/) y sugiriendo rebajas o remates preventivos.
2. **Auditoría de Descuadres entre Guía de Remisión y Conteo Físico:** Compara las cantidades despachadas por el proveedor frente a las aceptadas en tienda para identificar mermas sistemáticas del proveedor y sobrefacturaciones.
3. **Ratio de Intensidad de Mermas por Tienda:** Mide el costo de merma como porcentaje de las ventas brutas ($Merma / Ventas$) particionado por tienda y categoría mediante Funciones de Ventana.
4. **Días de Venta de Inventario (DSI) y Riesgo de Quiebre:** Analiza la velocidad del inventario comparando el stock activo actual contra el promedio móvil de ventas de los últimos 30 días.

---

## 5. Entregables en la Estructura de Directorios
- `sql/01_schema_ddl_3nf.sql`: Script DDL con definición de tablas, Claves Primarias, Claves Foráneas, restricciones CHECK e índices B-Tree optimizados.
- `sql/02_seed_data.sql`: Dataset maestro de datos iniciales con tiendas, proveedores, SKUs perecibles, recepciones de mercadería, lotes, ventas y mermas.
- `sql/03_analytical_queries.sql`: Las 4 consultas analíticas avanzadas de negocio, completamente documentadas con explicación de CTEs y funciones de ventana.
- `sql/04_triggers_and_procedures.sql`: Lógica transaccional con procedimientos FEFO, triggers antifraude y vista materializada.
- `sql/05_tests_business_logic.sql`: Suite de 19 aserciones SQL en una única transacción que termina en `ROLLBACK` (no destructiva); cada prueba reconstruye su fixture.
- `sql/06_ops_schema.sql`: Esquema `ops` de observabilidad: corridas del pipeline, tareas, modelos y resultados de calidad.
- `sql/07_ml_schema.sql`: Esquema `ml`: pronósticos, backtest, recomendaciones de reposición y lotes proyectados a vencer.
- `sql/models/`: Modelos del almacén dimensional (`mart.*`) y sus pruebas, ejecutados por `src/model_runner.py`.
- `src/`: `data_generator.py`, `etl_pipeline.py`, `dq.py`, `dq_report.py`, `model_runner.py`, `forecasting.py`, `policy_experiment.py`, `pipeline.py`, `dbutil.py`.
- `tests/`: 219 pruebas Python (187 unitarias, 32 de integración contra PostgreSQL real).
- `.github/workflows/ci.yml`: Integración continua en tres trabajos: unitarias, integración y validación de Compose.
- `bi/dax_measures.md`: Fórmulas analíticas DAX documentadas para integración con Power BI.
- `README.md`: Documentación principal del repositorio con diagramas de arquitectura, contexto operativo e impacto de negocio.

---

## 6. Capa de Lógica Transaccional (PL/pgSQL)

Las reglas de negocio críticas residen dentro del motor de base de datos, garantizando que no puedan ser vulneradas por aplicaciones cliente. Implementadas en `sql/04_triggers_and_procedures.sql`:

| Objeto | Tipo | Responsabilidad |
|---|---|---|
| `sp_process_fefo_sale` | Procedimiento | Consume `inventory_batches` en estricto orden cronológico `expiration_date ASC`, actualiza lotes agotados a `DEPLETED`, registra ventas en `daily_sales` con el COGS real por lote, y emite `SQLSTATE P0002` ante quiebre de stock. Bloquea filas candidatas con `FOR UPDATE` para evitar sobreventas concurrentes en POS. |
| `sp_expire_overdue_batches` | Procedimiento | Barrido nocturno: identifica lotes `ACTIVE` vencidos, los actualiza a `EXPIRED` y traslada el saldo a `waste_log` bajo el motivo `EXPIRATION`. |
| `trg_audit_high_waste_alert` | Trigger de fila `BEFORE` | Control antifraude en `waste_log`: bloquea bajas superiores a S/ 50.00 que carezcan de autorizador o que sean auto-autorizadas por el operador, y rechaza bajas superiores al `initial_qty` del lote. |
| `mv_store_category_waste_summary` | Vista Materializada | Datamart preagregado a nivel de tienda × categoría × mes para consumo directo desde Power BI. |
| `fn_refresh_waste_datamart` | Función | Refresca el datamart (concurrente por defecto mediante `CONCURRENTLY`), garantizando lecturas ininterrumpidas en tableros de BI. |

**Nota de diseño — columnas calculadas y triggers `BEFORE`:** `waste_log.total_loss_amount` es una columna `GENERATED`, y PostgreSQL procesa las columnas generadas *después* de disparar los triggers `BEFORE` de fila. Por ello, `NEW.total_loss_amount` es `NULL` dentro del trigger `trg_audit_high_waste_alert`, el cual recalcula el valor directamente como `qty_discarded × unit_cost_at_loss`. Esto previene fallas silenciosas documentadas en la evaluación de triggers.

---

## 7. Capa de Generación y Simulación de Datos

`src/data_generator.py` — Desarrollado exclusivamente con la librería estándar de Python 3.9+, sin dependencias externas, determinista mediante `--seed`.

Es un **motor de simulación de inventarios real**, no un simple generador de datos aleatorios. Las tiendas almacenan lotes físicos; el reabastecimiento respeta tiempos de entrega y vida útil; las ventas descuentan stock bajo la misma regla FEFO que aplica la base de datos; la demanda insatisfecha se registra como quiebre de stock; y las mermas son el resultado orgánico de lo que no logró rotar a tiempo.

Modelo de demanda multiplicativo:

```
demanda = tasa_base
        × indice_trafico_tienda
        × curva_dia_semana           (pico viernes y sábado)
        × factor_quincena            (15 y fin de mes, ciclo salarial peruano)
        × incremento_fin_semana      (por SKU: 2.15× cerveza, 1.70× comida lista)
        × elasticidad_temperatura    (curva anual de 15–27 °C de Lima)
        × ruido_lognormal            (positivo, asimétrico hacia la derecha como datos reales de POS)
```

Garantía de reproducibilidad: los desfases del calendario de despacho emplean CRC32 en lugar de la función `hash()` de Python, la cual se aleatoriza por proceso del intérprete.

Salida hacia `data/raw/`: `stores.csv`, `product_categories.csv`, `suppliers.csv`, `products.csv`, `waste_reasons.csv`, `goods_receipts.csv`, `batches.csv`, `waste_log.csv`, `daily_sales.csv`.

---

## 8. Capa de Pipeline ETL y Calidad de Datos

`src/etl_pipeline.py` — pandas, SQLAlchemy 2.x, psycopg2.

```
EXTRACCIÓN → TRANSFORMACIÓN → COMPUERTA DE CALIDAD (23 reglas) → CARGA → AUDITORÍA
```

**Modelo de Severidad:**

| Severidad | Reacción del Pipeline |
|---|---|
| `FATAL` | Aborta la ejecución (defecto estructural grave: dataset o columna faltante) |
| `ERROR` | Envía las filas anómalas a `data/quarantine/`, etiquetadas con la regla incumplida; continúa la ejecución |
| `WARNING` | Registra hallazgo en la bitácora de auditoría; la fila se carga normalmente |
| `INFO` | Registra una observación informativa |

**Propiedades de Carga:**
- Idempotencia: `INSERT ... ON CONFLICT DO UPDATE` sobre la clave natural de cada tabla.
- Claves naturales en los CSVs (`store_code`, `sku_code`, `grn_number`); resolución de claves subrogadas contra la base de datos tras cargar cada nivel jerárquico.
- Inserciones en bloques `executemany` (1,000 filas) dentro de una transacción por tabla, respetando dependencias de claves foráneas.
- Coerción segura de tipos con `errors="coerce"`.

**Trazabilidad y Auditoría:** `docs/etl_execution.log` (registro secuencial) y archivos `data/quarantine/<dataset>_rejected_<timestamp>.csv` por dataset afectado. Cada corrida deja además un registro estructurado de calidad (ver §14).

**Idempotencia verificada, no declarada.** La carga de `goods_receipt_items` empezó como `DELETE` + `INSERT`, lo que viola la clave foránea `inventory_batches.grn_item_id` en la *segunda* ejecución: la transacción se revertía, los conteos de filas coincidían y parecía idempotente. Ahora es `UPDATE` + `INSERT` si no existe, lo que mantiene estables los `grn_item_id`. La prueba que lo protege compara sumas de verificación de contenido (no conteos) tras ejecutar el pipeline completo dos veces.

---

## 9. Capa de Infraestructura

`docker-compose.yml` — PostgreSQL 15 Alpine con volumen nombrado persistente, verificación de salud (healthcheck) y scripts de inicialización ordenados.

El orden de inicio se asegura mediante los nombres de montaje en el contenedor:

| Nombre de Montaje | Archivo Origen | Propósito |
|---|---|---|
| `01_schema_ddl_3nf.sql` | `sql/01_schema_ddl_3nf.sql` | DDL, restricciones e índices |
| `02_seed_data.sql` | `sql/02_seed_data.sql` | Datos maestros de referencia |
| `03_triggers_and_procedures.sql` | `sql/04_triggers_and_procedures.sql` | Procedimientos, triggers y datamart |
| `04_ops_schema.sql` | `sql/06_ops_schema.sql` | Observabilidad del pipeline |
| `05_ml_schema.sql` | `sql/07_ml_schema.sql` | Salidas de pronóstico y reposición |

Los procedimientos y triggers se ejecutan deliberadamente **después** de los datos iniciales para asegurar que la vista materializada se cree con datos reales y no se evalúen triggers antes de tiempo.

Servicio opcional de pgAdmin disponible bajo el perfil Compose `tools`.

El esquema estrella (`mart.*`) **no** se crea al iniciar el contenedor: lo construye `src/model_runner.py` cuando ya hay datos que modelar. El orquestador reaplica los esquemas `ops` y `ml` (idempotentes) en cada arranque, por lo que funciona también sobre un volumen creado antes de que esos archivos existieran.

**Automatizadores de Tareas:** `Makefile` (GNU Make) y `scripts/run_project.ps1` (PowerShell nativo para Windows) ofrecen los mismos comandos: `init-db`, `pipeline`, `generate`, `validate`, `run-etl`, `models`, `forecast`, `dq-report`, `experiment`, `run-queries`, `refresh-datamart`, `test`, `test-unit`, `test-sql`, `status`, `psql`, `logs`, `destroy`. El SQL se envía a `psql` por entrada estándar: Git Bash en Windows reescribe un argumento que empieza con `/` como ruta de Windows y el archivo nunca se encuentra.

---

## 10. Capa de Pruebas y Verificación

### Suite SQL — `sql/05_tests_business_logic.sql`

19 aserciones en cuatro grupos de negocio: orden de rotación FEFO, manejo de quiebres de stock y atomicidad, trigger antifraude de mermas, y barrido de caducidad e integridad del datamart.

Toda la suite corre en **una sola transacción** que termina en `ROLLBACK`, por lo que es idempotente y no altera datos operativos. Cada prueba empieza con `pg_temp.reset_fixture()`, que borra lo que dejó la anterior y reconstruye los tres lotes de prueba. Los fixtures usan el prefijo `TEST-` y se insertan desordenados cronológicamente para certificar que el procedimiento aplique FEFO por ordenamiento real.

**Por qué no `SAVEPOINT`.** La primera versión aislaba cada prueba con `SAVEPOINT` + `ROLLBACK TO SAVEPOINT`. Pero ese rollback también revierte los `INSERT` que cada aserción hace en su tabla de resultados, así que el resumen final reportaba **3 de 3** cuando se habían ejecutado 19. Las aserciones sí corrían (cada fallo lanza una excepción), pero el resumen mentía. Solo se notó al ejecutar la suite contra una base real por primera vez.

Cualquier aserción fallida detona una excepción que, mediante `ON_ERROR_STOP`, devuelve un código de salida distinto de cero para CI.

### Suite Python — `tests/` (219 pruebas)

| Archivo | Pruebas | Qué demuestra |
|---|---:|---|
| `test_forecasting.py` | 56 | Modelos y métricas contra series de estructura conocida; sin *look-ahead*; calibración de intervalos; proyección FEFO; reposición |
| `test_pipeline.py` | 35 | Orden del grafo, aislamiento de fallas, reintentos solo ante errores transitorios, selección de tareas |
| `test_generator.py` | 25 | Determinismo, línea base intacta, verdad de terreno, políticas, censura de demanda |
| `test_dq.py` | 28 | Catálogo de reglas, puntuación contra la verdad de terreno, historial |
| `test_etl_gate.py` | 24 | Cada regla con una fila que debe fallar y otra que no; ejecución de extremo a extremo |
| `test_model_runner_unit.py` | 19 | Cabeceras, ordenamiento topológico, ciclos, consistencia de dependencias |
| `test_models_integration.py` | 14 | Incrementalidad, SCD2, instantáneas, idempotencia, pruebas que **deben poder fallar** |
| `test_pipeline_integration.py` | 18 | El pipeline real contra PostgreSQL, dos veces, con una falla inyectada |

Las 32 pruebas de integración crean **una base de datos temporal por prueba** a partir de los archivos SQL reales y la destruyen al terminar; si PostgreSQL no está disponible se *omiten* en lugar de fallar.

### Prueba de mutación

Que las pruebas pasen no demuestra que sirvan. Se introdujeron cinco defectos a propósito y se verificó que la prueba responsable fallara (cada archivo se restauró y se verificó por hash):

| Defecto introducido | Detectado por |
|---|---|
| La carga de recepciones vuelve a `DELETE` + `INSERT` | `test_running_the_whole_pipeline_twice_changes_nothing` |
| El backtest filtra un día del futuro | `test_backtest_folds_use_only_data_before_each_window` |
| Los hechos se unen a la versión *actual* del producto, no a la vigente ese día | `test_scd2_keeps_history_and_prices_each_fact_with_the_version_of_its_day` |
| `R06` deja de marcar faltantes sin motivo | `test_the_gate_catches_every_defect_the_generator_planted` |
| Se desactiva la calibración de intervalos | `test_pooled_calibration_brings_interval_coverage_close_to_nominal` |

---

## 11. Capa de Modelado Dimensional (`mart.*`)

`sql/models/` + `src/model_runner.py`. Un ejecutor propio, sin dependencias, con la parte de dbt que importa a este tamaño: **orden de construcción, carga incremental y pruebas tras cada construcción**.

Cada archivo es un modelo con cabecera (`-- model:`, `-- kind:`, `-- target:`, `-- depends_on:`). El ejecutor ordena por dependencias (ordenamiento topológico que rechaza ciclos y nombres desconocidos), ejecuta cada modelo en su propia transacción y registra cada construcción en `ops.model_runs`.

```
dim_date  dim_store  dim_supplier  dim_waste_reason  dim_product (SCD2)
        \      |         |              |              /
         fact_sales_daily   fact_waste   fact_receipt_line   fact_stock_position
                              \              |                 /
                               mart_views (v_store_day_kpi, v_supplier_scorecard,
                                           v_weekday_demand_profile, v_fefo_exposure_latest)
```

| Tipo | Modelos | Estrategia |
|---|---|---|
| `table` | `dim_date`, `dim_store`, `dim_supplier`, `dim_waste_reason` | Upsert completo; baratos y sin historia |
| `scd2` | `dim_product` | Cada cambio de costo, precio o vida útil abre una versión nueva |
| `incremental` | `fact_sales_daily`, `fact_waste`, `fact_receipt_line` | Reprocesa desde (marca de agua − `lookback_days`) y hace upsert |
| `snapshot` | `fact_stock_position` | Una instantánea por corrida; **nunca se trunca** |
| `view` | `mart_views` | Definiciones sobre hechos ya incrementales; nada que refrescar |

**SCD tipo 2 y la consulta punto-en-el-tiempo.** El costo y el precio de un SKU cambian, y un hecho debe valorarse con la versión vigente *cuando ocurrió*. Con tipo 1, reprecificar un SKU reescribe en silencio el margen de toda venta pasada en todos los reportes. Los hechos se unen por `product_id` y `fecha ∈ [valid_from, valid_to)`. La detección de cambios compara un hash de los atributos rastreados, de modo que una re-ejecución sin cambios no abre versiones. Un segundo cambio el mismo día sobrescribe en lugar de crear una versión de duración cero. Dos índices únicos parciales hacen cumplir en la base de datos —no por confianza— que haya una sola versión vigente por producto.

**Ventana de retroceso (`lookback`).** Un incremental estricto ("solo lo posterior a la marca de agua") nunca vería datos tardíos ni corregidos del POS. El modelo reprocesa los últimos 3 días y hace upsert. El costo es conocido y está probado: un cambio *más antiguo* que la ventana no se recoge hasta `--full-refresh`. `test_a_change_older_than_the_lookback_is_missed_until_a_full_refresh` fija ese comportamiento; si deja de cumplirse, la documentación miente.

**Por qué `fact_stock_position` es una instantánea.** `inventory_batches` solo guarda la cantidad *actual* de cada lote: el stock de ayer no puede reconstruirse. La historia hay que capturarla mientras ocurre, una instantánea por corrida. Por eso `--full-refresh` no la trunca: destruiría historia irrecuperable.

**Trampa de explosión de filas.** `mart.v_store_day_kpi` agrega ventas y mermas al mismo grano *antes* de unirlas. Unir ambos hechos fila a fila repetiría cada baja una vez por producto vendido e inflaría la pérdida.

**13 pruebas de modelo** (`sql/models/tests.sql`): cada una es una consulta que devuelve las filas que *violan* una expectativa; cero filas es aprobado, y un fallo llega con las claves infractoras. La mayoría son **conciliaciones**: el mismo total calculado desde el origen 3NF y desde el esquema estrella debe coincidir al centavo, lo que prueba que ni la carga incremental, ni el join SCD2, ni el mapeo de claves perdieron o duplicaron filas.

---

## 12. Capa de Pronóstico y Reposición

`src/forecasting.py` — numpy y pandas. El núcleo es puro (arreglos de entrada y salida) y se prueba con series de estructura conocida; la base de datos solo interviene en los bordes.

**Cinco modelos:** `seasonal_naive` (este día, la semana pasada), `moving_average` (media de 7 días), `weekday_mean` (mismo día de las últimas 4 semanas), `holt_winters` (aditivo con tendencia amortiguada y estacionalidad semanal, parámetros por búsqueda en malla) y `ensemble` (promedio de `weekday_mean` y `holt_winters`).

**Evaluación honesta:**

| Práctica | Por qué |
|---|---|
| *Backtest* de origen móvil (4 ventanas de 7 días) | Una sola partición haría depender el ranking de una semana afortunada o desafortunada |
| Elegir y calibrar en las ventanas tempranas, **puntuar en las tardías** | Elegir el mejor de cinco modelos y puntuarlo con los mismos datos infla su precisión: entre cinco candidatos, uno luce bien por azar |
| Calibrar los intervalos con residuos **relativos agrupados entre series** | Con 14 residuos por serie, un intervalo nominal del 80% cubría solo 66% |
| Métrica WAPE, no MAPE | La demanda diaria incluye ceros que hacen indefinido el MAPE |

**Resultados** (60 series tienda × SKU; ventanas no vistas al elegir ni calibrar):

| | WAPE |
|---|---:|
| `seasonal_naive` (la vara a superar) | 30.0% |
| `moving_average` | 36.0% |
| `holt_winters` | 23.5% |
| `weekday_mean` | 23.4% |
| **`ensemble` (el que se despliega)** | **23.1%** |
| Mejor modelo elegido *por serie* | 24.1% |

La cobertura empírica del intervalo nominal 80% es **80.4%**.

**Hallazgo: elegir el mejor modelo por serie empeora el resultado.** Un `ensemble` fijo (23.1%) supera a la selección por serie (24.1%): con solo dos ventanas por serie, escoger entre cinco modelos se sobreajusta al ruido. Es el resultado conocido de la literatura de combinación de pronósticos. Por eso la estrategia por defecto es `ensemble`; `per_series` sigue disponible (`--strategy per_series`) y se mide. *Salvedad honesta:* el valor por defecto se fijó viendo estos números, de modo que 23.1% es ligeramente optimista.

**Proyección FEFO hacia adelante.** Consume los lotes en mano contra el pronóstico en estricto orden de vencimiento, y al cierre del día de vencimiento de un lote da de baja lo que quede. FEFO hace que el resultado sea independiente de entregas futuras (un lote posterior vence después y se consume después). Un lote que vence más allá del horizonte reporta cero pérdida proyectada: la ausencia de pérdida proyectada no es evidencia de que no la habrá. La salida `ml.projected_expiry` es la lista de remates: qué rebajar *hoy*, mientras el producto aún tiene valor.

**Recomendación de pedido.** `cobertura = días hasta la próxima entrega` (derivada de la vida útil); `esperado = pronóstico sobre la cobertura`, topado por la vida útil; `seguridad = z × σ_residual × √cobertura`; `pedido = máximo − stock genuinamente vendible`, donde el stock vendible descuenta lo que la proyección FEFO indica que vencerá.

**Limitación conocida.** El historial es de *ventas*, no de demanda: un día de quiebre subestima lo que los clientes querían y el modelo aprende esa subestimación. Corregirlo exige marcas de quiebre que el almacén no guarda.

---

## 13. Experimento de Políticas de Reposición

`src/policy_experiment.py` convierte la afirmación "mejor reposición reduce la merma" de supuesto a medición. Informe completo generado en `docs/experiments/policy_comparison.md`.

Tres políticas sobre el mismo simulador: **heurística actual**, **nivel plano** (base-stock sin estacionalidad) y **pronóstico estacional**. El nivel plano aísla el valor del *pronóstico* del valor de *dimensionar bien*: sin él, el pronóstico se llevaría el crédito de una ganancia que un promedio plano también entrega.

El nivel de servicio `z` se elige en 20 semillas de entrenamiento y se evalúa en 100 semillas distintas, con diferencias pareadas por semilla e intervalos bootstrap del 95%.

**Resultado:** merma por vencimiento −53.0%, venta perdida +30.7%, utilidad neta **+0.4%** (+S/ 651 por corrida, IC [358, 952]); el nivel plano es **peor** que la heurística (−S/ 811). Solo el pronóstico estacional vuelve rentable el pedido más ajustado. Interpretación completa y limitaciones en `docs/business_case_study.md` §6.4.

**Dos errores de especificación, documentados.** La primera versión copió la cobertura `intervalo + 1` de la heurística (dos días de stock perecible contra una vida de dos días). La corrección ingenua de la demanda censurada —descartar los días de quiebre— sesgaba la demanda a la baja porque elimina justo los días de demanda alta (selección sobre el resultado); se imputan con `max(observado, media de días no censurados)`.

---

## 14. Capa de Orquestación y Observabilidad

`src/pipeline.py`. Un grafo de tareas con dependencias:

```
bootstrap --+
            +--> etl --+--> models --> forecast
generate ---+          |
                       +--> dq_report
```

| Comportamiento | Detalle |
|---|---|
| Dependencias, no una lista fija | Si `etl` falla, `models` y `forecast` se **omiten**. Lo que no depende de la falla (`dq_report` tras un `models` fallido) sigue corriendo |
| Causa raíz | Una tarea omitida nombra la tarea que *realmente* falló (`upstream failed: etl`), no el vecino omitido más cercano |
| Reintentos solo donde ayudan | Un error de conexión o *timeout* se reintenta con retroceso exponencial. Una compuerta de calidad o una prueba fallida **no**: volver a correr falla igual y ocultaría el problema |
| Ejecución parcial | `--only`, `--from` y `--skip` re-ejecutan parte del grafo |
| Registro de cada corrida | `ops.pipeline_runs` y `ops.task_runs`, más `data/processed/pipeline_runs.jsonl` |

El esquema `ops` (`sql/06_ops_schema.sql`) incluye vistas `ops.v_run_history` y `ops.v_dq_trend`, de modo que "¿funcionó la carga de anoche y qué tan limpios venían los datos?" es una consulta.

---

## 15. Calidad de Datos Versionada

`src/dq.py` + `src/dq_report.py`. Se genera `docs/data_quality_report.md` desde datos, nunca a mano, y cada cifra remite a la corrida que la produjo.

- **Catálogo de reglas por dimensión** (completitud, unicidad, validez, consistencia, integridad, oportunidad): los resultados se agregan en un cuadro de mando, no en una lista plana.
- **Cada regla registra filas evaluadas y fallidas, incluso las que no encontraron nada.** El gate original solo registraba las que disparaban, lo que impide calcular una tasa de aprobación: "0 hallazgos" no distingue un dataset limpio de una regla que nunca corrió.
- **Puntuación contra la verdad de terreno.** El generador escribe `_manifest.json` con la clave natural de cada defecto que inyectó. Comparar fila a fila contra esa lista mide el **recall** del gate en lugar de afirmar que "funciona". Un defecto cuenta como detectado solo si *esa fila* fue marcada por *la regla responsable*, no si se marcó la cantidad correcta de filas en otro lugar.
- **Una discrepancia "no documentada" que de verdad no tiene nota.** El generador marcaba antes ese defecto con un texto en la nota, lo que habría entregado la respuesta a cualquier regla. Ahora la nota está vacía, y la regla `R06` (faltante contra la guía *sin motivo registrado*) separa el rechazo documentado del faltante que nadie explicó.

**Resultado:** 65 de 65 defectos inyectados detectados a lo largo de 6 conjuntos de datos independientes (semillas distintas).

**Historial versionado.** Cada corrida agrega una línea a `docs/data_quality/history.jsonl` (pequeña, y se versiona); el registro completo va a `data/quality_runs/` (no se versiona). El `git_sha` lleva el sufijo `+dirty` si hay cambios sin commitear, porque un hash desnudo afirmaría que *ese* commit produjo el resultado.

**Hallazgo de diseño durante las pruebas.** `F01` se evalúa una vez por columna foránea, de modo que `goods_receipts.csv` producía tres resultados con la misma clave `(F01, dataset)`. Las tablas de auditoría tienen esa clave primaria y `ON CONFLICT DO NOTHING` habría descartado dos de los tres **sin error**. Los resultados se fusionan por clave antes de persistir.

**Alcance honesto.** El *recall* se mide contra los defectos que alguien pensó en inyectar. Dice poco sobre los defectos que nadie imaginó; con datos reales no hay lista contra la cual puntuar.
