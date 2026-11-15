# Caso de Negocio: Reducción de Mermas de Perecibles en Tiendas de Conveniencia en Perú

**Autor:** Odar Javier Alcocer Vásquez — Ingeniería de Sistemas de Información, UPC  
**Alcance:** Red de 4 tiendas de conveniencia, Lima · 60 días de operación · 15 SKUs  
**Plataforma:** `retail-inventory-analytics-3nf`  

---

## Nota Metodológica sobre las Cifras de este Documento

Este estudio distingue rigurosamente tres categorías de números, identificando cada una con una etiqueta explícita:

| Etiqueta | Significado |
|---|---|
| **MEDIDO** | Calculado directamente a partir del dataset simulado de 60 días en `data/raw/`. Reproducible con `python src/data_generator.py --end-date 2026-10-11` (semilla por defecto `20261012`). La ventana debe fijarse: sin `--end-date` el generador simula los 60 días hasta ayer y las cifras cambian cada día. |
| **BENCHMARK** | Rangos publicados de la industria de supermercados y tiendas de conveniencia, utilizados para contextualizar el problema. |
| **EXPERIMENTO** | Medido sobre 100 realizaciones independientes del simulador, con intervalos de confianza. Reproducible con `python src/policy_experiment.py`; informe completo en [`docs/experiments/policy_comparison.md`](experiments/policy_comparison.md). |
| **SUPUESTO** | Variable o parámetro de modelado para el cálculo del retorno de inversión (ROI). Todo supuesto se declara explícitamente y se evalúa en la tabla de sensibilidad. |

El dataset es una **simulación de una red ya monitoreada y controlada**, por lo que su tasa de merma se sitúa deliberadamente en el extremo eficiente del rango de la industria. El caso de negocio se sustenta en la *composición* de la pérdida —la cual los datos miden con precisión— y no en asumir un escenario inicial artificialmente desastroso.

**Advertencia sobre la línea base:** las cifras MEDIDO de las secciones 3 y 6.1 provienen de **una sola realización** de la simulación (semilla `20261012`). Esa realización resulta **23% más alta que el promedio** en merma por vencimiento: S/ 2,186 frente a S/ 1,778 promedio de 100 semillas (EXPERIMENTO). La *composición* de la pérdida es estable entre semillas; el *monto* absoluto no, y debe leerse como un valor posible, no como el valor esperado.

---

## 1. Resumen Ejecutivo

El comercio minorista de conveniencia en Perú —Tambo+, Oxxo, Listo!, Repshop y las operaciones de comida rápida como Bembos— opera bajo una estructura de márgenes que las mermas de perecibles impactan de forma directa. Una tienda vende comida preparada lista para consumir con una **vida útil de 2 días** junto a lácteos de 21 días y bebidas a temperatura ambiente de 180 días, en una sala de ventas de solo 85 m², atendida por dos o tres colaboradores por turno y sin un seguimiento automatizado por lotes.

La consecuencia es estructural y predecible:

1. **La merma por vencimiento es dominante:** En la red evaluada, el **75.4% de las pérdidas monetarias corresponde a vencimientos** (MEDIDO), no a mermas por robo ni roturas. El vencimiento es la única categoría de pérdida que una regla determinista de rotación FEFO puede prevenir estructuralmente.
2. **Los faltantes de proveedores no se reclaman:** La red pagó facturas por **483 unidades que físicamente nunca ingresaron a tienda** a lo largo de 60 días (MEDIDO), debido a la falta de conciliación sistemática entre la guía de remisión del proveedor y el conteo físico en recepción.
3. **Coexistencia paradójica de quiebres de stock y sobrestock:** Durante los mismos 60 días se registraron **S/ 4,171.60 en ventas perdidas por quiebre de stock** (MEDIDO) *mientras* otros productos perecibles se vencían en anaquel — síntoma inequívoco de un reabastecimiento guiado por intuición y no por velocidad real de rotación.

Esta plataforma ataca los tres problemas con un único núcleo relacional: rotación FEFO forzada en la base de datos (`sp_process_fefo_sale`), conciliación de recepciones (`discrepancy_qty` + Consulta 2) y cobertura de existencias basada en velocidad de ventas (Consulta 4).

**Impacto proyectado para un despliegue en cadena de 400 tiendas:**
* **Beneficio anual estimado:** **S/ 1.68 Millones**
* **Periodo de recuperación:** **12.8 meses**
* **Retorno sobre la Inversión (ROI a 3 años):** **109% en el escenario base** (e incluso un **38%** en el escenario más conservador). Detalle en §6.

> **Matiz medido (§6.4):** al simular la reposición guiada por pronóstico en 100 realizaciones independientes, la merma por vencimiento baja **53%**, pero la utilidad neta mejora solo **0.4%** (≈ S/ 396 mil al año para 400 tiendas, IC 95%: S/ 218 mil – S/ 579 mil), porque pedir menos genera más quiebres de stock. Solo dos tercios del ahorro bruto en merma sobreviven al neto.

---

## 2. El Problema Operativo

### 2.1 Por qué el retail de conveniencia perecible es estructuralmente complejo

Una tienda de conveniencia enfrenta las restricciones más exigentes de la gestión de inventarios:

| Restricción Operativa | Consecuencia en el Negocio |
|---|---|
| Vidas útiles desde 2 hasta 365 días en un mismo pasillo | Ninguna frecuencia única de reposición funciona; cada SKU requiere su propio ciclo |
| Sala de ventas reducida (60–110 m²) | Sin almacén de respaldo: errores de inventario provocan desabastecimiento o vencimiento en horas |
| Demanda concentrada en 3 de los 7 días | Quiebres de stock en fin de semana y sobrestock a mitad de semana en la misma semana |
| Dotación de 2 a 3 colaboradores por turno | La revisión manual de fechas pierde prioridad frente a la atención al cliente en caja |
| Equipos de refrigeración y conservación en caliente | Una sola falla técnica o corte eléctrico compromete y da de baja lotes enteros |

### 2.2 La demanda es altamente estacional — y los datos lo demuestran

La simulación reproduce el comportamiento de consumo real. Ventas promedio por tienda-día en fin de semana (Vie–Dom) versus días de semana (MEDIDO):

| SKU | Días de Semana | Fin de Semana | Incremento (Uplift) |
|---|---:|---:|---:|
| Cerveza Pilsen Callao 650ml | 14.6 unid. | 36.1 unid. | **2.47×** |
| Empanada de Carne (vida útil 2 días) | 27.4 unid. | 57.4 unid. | **2.09×** |
| Agua San Mateo 625ml | 23.2 unid. | 30.6 unid. | 1.32× |
| Leche Fresca UHT 1L | 14.8 unid. | 19.5 unid. | 1.31× |
| Galleta Soda 6pack | 10.4 unid. | 13.1 unid. | 1.26× |

Esto debe entenderse como un problema de inventario antes que de ventas. La empanada **duplica su demanda** el viernes y tiene una vida útil de solo **dos días**. Si se solicita pedido para cubrir el pico de fin de semana, el lunes se genera merma masiva; si se pide según el promedio semanal, el sábado habrá quiebre de stock. La única solución viable es rotar con precisión quirúrgica el producto en exhibición (FEFO) y abastecer según la velocidad medida — exactamente lo que ejecuta la plataforma.

El generador también modela la temperatura ambiental (curva térmica de Lima de 15–27 °C), los días de pago peruanos (quincenas y fin de mes) y el incremento del riesgo de falla en cadena de frío ante olas de calor. Ninguna merma está predefinida arbitrariamente: es el resultado de lo que la rotación FEFO no logró vender a tiempo.

---

## 3. Línea Base Medida (60 días, 4 tiendas)

### 3.1 Desempeño Comercial — MEDIDO

| Métrica | Valor Registrado |
|---|---:|
| Ventas brutas totales | S/ 416,525.40 |
| Costo de ventas (COGS) | S/ 237,429.70 |
| **Margen bruto comercial** | **S/ 179,095.70 (43.00%)** |
| Unidades vendidas | 69,465 |
| Ventas promedio por tienda por día | S/ 1,735.52 |
| Participación de productos perecibles en ventas | 63.7% |

### 3.2 Composición de Pérdidas y Mermas — MEDIDO

| Clasificación de Pérdida | Monto Total | Participación | Unidades | ¿Mitigable vía FEFO? |
|---|---:|---:|---:|:---:|
| **VENCIMIENTO (EXPIRATION)** | **S/ 2,186.14** | **75.4%** | 684 | **Sí — directamente** |
| MERMA DESCONOCIDA / ROBO | S/ 342.24 | 11.8% | 74 | No |
| FALLA DE CADENA DE FRÍO | S/ 195.83 | 6.7% | 33 | Parcialmente (tiempo de exposición) |
| DAÑO FÍSICO / ROTURA | S/ 177.05 | 6.1% | 38 | No |
| **Total** | **S/ 2,901.27** | 100% | 829 | |

**El hallazgo más contundente de este estudio:** el vencimiento representa el 75.4% de las mermas monetarias. El 80.2% del valor total de mermas se concentra en SKUs perecibles. En consecuencia, forzar la rotación FEFO a nivel de base de datos impacta de lleno sobre las tres cuartas partes del problema financiero.

Contexto comparativo: la merma total equivale al **0.70% de las ventas** y al **1.62% del margen bruto** (MEDIDO). Los estándares de la industria para tiendas de conveniencia con alta concentración de perecibles oscilan entre el **1% y el 4% de las ventas** (BENCHMARK). Por tanto, la red simulada parte de un estándar operativo eficiente y realista, garantizando la solidez de las proyecciones.

### 3.3 Intensidad de Merma por Tienda — MEDIDO

| Tienda | Distrito | Formato | Merma Total | Intensidad (% sobre ventas) |
|---|---|---|---:|---:|
| TB-101 | Miraflores | FLAGSHIP | S/ 428.74 | **0.326%** |
| TB-103 | Santiago de Surco | CONVENIENCE | S/ 736.12 | 0.780% |
| TB-104 | San Miguel | CONVENIENCE | S/ 690.29 | 0.821% |
| TB-102 | San Isidro | EXPRESS | S/ 1,046.12 | **0.981%** |

La dispersión revela la oportunidad de gestión: la tienda TB-102 pierde **3.0 veces más dinero por sol vendido** que la tienda TB-101. El formato Flagship cuenta con el flujo de clientes necesario para rotar el perecible antes de que caduque; el formato Express de 65 m² no lo tiene, por lo que requiere pedidos con lotes más pequeños y frecuentes antes que una simple exigencia de disciplina al personal.

### 3.4 Nivel de Cumplimiento de Proveedores (Fill Rate) — MEDIDO

| Proveedor | Nivel de Cumplimiento (Fill Rate) |
|---|---:|
| SUP-001 Gloria S.A. | **98.69%** |
| SUP-006 Backus & Johnston | 99.04% |
| SUP-002 Alicorp S.A.A. | 99.06% |
| SUP-004 Bimbo del Perú | 99.30% |
| SUP-003 San Fernando | 99.48% |
| SUP-005 Corporación Lindley | 99.69% |
| **Promedio de la Red** | **99.30%** |

Se detectaron 483 unidades despachadas pero jamás entregadas físicamente, equivalentes a **S/ 1,834.07 al costo de adquisición** en 60 días. Un cumplimiento del 99% puede parecer óptimo a simple vista; sin embargo, como saldo no reclamado representa una pérdida anualizada de **S/ 2,789 por tienda**. A escala de una cadena de cientos de tiendas, es un capital significativo que actualmente nadie está reclamando.

---

## 4. Definición Unificada de KPIs

Todos los KPIs se implementaron tanto en SQL como en DAX, impidiendo discrepancias de cálculo entre la base de datos y los tableros gerenciales.

| KPI | Fórmula / Definición | Ubicación |
|---|---|---|
| **Intensidad de Merma (%)** | `SUM(total_loss_amount) / SUM(gross_revenue)` | Consulta 3, `mv_store_category_waste_summary.waste_intensity_pct` |
| **Merma Abordable por FEFO (%)** | `Pérdida por VENCIMIENTO / Pérdida Total por Merma` | `mv_...fefo_addressable_pct` |
| **Exposición Financiera (S/ en Riesgo)** | `SUM(current_qty × unit_cost)` para lotes ACTIVE que vencen en ≤ 5 días | Consulta 1 |
| **Cumplimiento de Proveedores (Fill Rate %)** | `SUM(qty_accepted) / SUM(qty_dispatched)` | Consulta 2 |
| **Días de Venta de Inventario (DSI)** | `stock_actual / promedio_diario_ultimos_7_dias` | Consulta 4 |
| **Porcentaje de Merma en Unidades** | `unidades_desechadas / (unidades_vendidas + unidades_desechadas)` | `mv_...unit_shrinkage_pct` |
| **Incidentes de Merma no Autorizados** | `COUNT(*) WHERE authorized_by IS NULL` | `mv_...unauthorized_events` |
| **Error de Pronóstico (WAPE)** | `SUM(\|pronóstico − real\|) / SUM(real)` sobre semanas no vistas al elegir el modelo | `ml.forecast_runs.holdout_wape` |
| **Recall de la Compuerta de Calidad** | defectos inyectados detectados / defectos inyectados, fila por fila | `ops.v_dq_trend.gate_recall` |

### Por qué la "Exposición Financiera" es el disparador operativo

La Consulta 1 no reporta pérdidas pasadas (lo cual es labor contable posterior). Reporta el **capital actualmente comprometido en lotes que vencerán en 48 horas y 5 días**, ordenado por tienda y categoría. Esto convierte la gestión de mermas en un plan de acción preventivo diario: aplicar rebajas promocionales, combos o transferencias entre tiendas *hoy*, mientras el producto aún conserva valor comercial. Dar de baja un lote recupera el **0%** de su costo; liquidarlo con un 40% de descuento preserva margen positivo sobre una base del 43%.

---

## 5. Mecanismos de Control Implementados en la Plataforma

| Problema Operativo | Control Técnico en la Plataforma | Componente |
|---|---|---|
| El personal toma el producto frontal, no el más antiguo | `sp_process_fefo_sale` consume lotes por `ORDER BY expiration_date ASC`, con bloqueo de filas para evitar sobreventa concurrente | `sql/04` |
| Lotes agotados permanecen como inventario fantasma | Al llegar a cero, el lote pasa automáticamente a estado `DEPLETED` dentro de la transacción | `sql/04` |
| Producto vencido sigue disponible para venta en sistema | `sp_expire_overdue_batches` reclasifica lotes a `EXPIRED` y traslada el saldo a `waste_log` | `sql/04` |
| Alteración de mermas para encubrir sustracciones | `trg_audit_high_waste_alert` bloquea bajas > S/ 50 sin un autorizador independiente | `sql/04` |
| Facturación indebida de pedidos incompletos | Columna calculada `discrepancy_qty` y consulta de auditoría de recepciones | `sql/01`, `sql/03` |
| Datos operativos inconsistentes distorsionan los KPIs | Pipeline ETL con 23 reglas de calidad clasificadas por dimensión; registros erróneos van a cuarentena | `src/etl_pipeline.py`, `src/dq.py` |
| Lentitud en tableros por consultas complejas | Datamart materializado `mv_store_category_waste_summary` con refresco concurrente | `sql/04` |
| Cada reporte calcula "ventas" o "merma" distinto | Esquema en estrella con carga incremental y 13 pruebas de modelo (conciliación contra el origen 3NF centavo a centavo e invariantes SCD2) | `sql/models/`, `src/model_runner.py` |
| Un cambio de precio reescribe el margen histórico | Dimensión de producto SCD tipo 2: cada hecho se valora con la versión vigente el día de la venta | `sql/models/14_dim_product.sql` |
| Nadie sabe qué lotes se van a vencer antes de que ocurra | Proyección FEFO hacia adelante contra el pronóstico: lista de remates con valor en riesgo | `src/forecasting.py`, `ml.projected_expiry` |
| Una falla a media corrida deja el almacén a medias | Orquestador con dependencias: lo que depende de una tarea fallida se omite, y cada corrida queda registrada | `src/pipeline.py`, `ops.*` |

### El Control Antifraude en la Práctica

Las pérdidas por causas desconocidas representan el 11.8% de las mermas medidas y son el rubro más vulnerable a irregularidades internas. El trigger impone **segregación de funciones**: ante mermas superiores a S/ 50, el operador que registra no puede ser quien autoriza. Asimismo, la compuerta ETL (regla `W03`) detecta esta irregularidad aguas arriba, reportándola de forma clara en la bitácora de auditoría.

En el dataset de 60 días, este mecanismo bloqueó **3 declaraciones de baja** por un total de **S/ 560.04** (MEDIDO) que habrían ingresado indebidamente al sistema.

---

## 6. Modelo de Retorno de Inversión (ROI)

### 6.1 Base de Escalabilidad

| Parámetro | Valor | Clasificación |
|---|---:|---|
| Merma anual por vencimiento medida por tienda | S/ 3,325 | MEDIDO (anualizado: ×365/60) |
| Valor anual medido en mercadería no recibida por tienda | S/ 2,789 | MEDIDO (anualizado) |
| Margen bruto anual perdido por quiebres de stock por tienda | S/ 2,728 | MEDIDO (anualizado al 43% de margen) |
| Escala de despliegue | 400 tiendas | SUPUESTO (escala representativa de una cadena peruana) |
| **Pérdida anual por vencimiento en toda la cadena** | **S/ 1,329,902** | Proyectado |
| **Valor anual no recibido en toda la cadena** | **S/ 1,115,725** | Proyectado |
| **Margen bruto anual perdido por quiebres en la cadena** | **S/ 1,091,221** | Proyectado |

### 6.2 Estructura de Costos — SUPUESTO

| Concepto de Inversión | Monto |
|---|---:|
| Construcción de la plataforma, integración POS/ERP e ingeniería de datos | S/ 450,000 |
| Despliegue en tiendas: impresoras de etiquetas, rediseño de procesos, capacitación (S/ 2,500 × 400 tiendas) | S/ 1,000,000 |
| **Inversión Inicial Total (Capex)** | **S/ 1,450,000** (S/ 3,625 por tienda) |
| Costo operativo anual recurrente: soporte, infraestructura cloud, 1 analista de datos/operaciones | S/ 320,000 / año |

### 6.3 Escenarios de Beneficio y Análisis de Sensibilidad

Las tasas de recuperación son SUPUESTOS fundamentados, presentados en tres escenarios transparentes:

| Escenario | Vencimiento Recuperado | Faltantes Proveedor Recuperados | Quiebres Recuperados | Beneficio Anual Neto | Periodo Recuperación | Beneficio Neto (3 años) | ROI (3 años) |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Conservador** | 25% → S/ 332,476 | 50% → S/ 557,862 | 20% → S/ 218,244 | **S/ 1,108,582** | 22.1 meses | S/ 915,747 | **38%** |
| **Base** | 35% → S/ 465,466 | 70% → S/ 781,008 | 40% → S/ 436,488 | **S/ 1,682,962** | 12.8 meses | S/ 2,638,885 | **109%** |
| **Optimista** | 45% → S/ 598,456 | 85% → S/ 948,366 | 55% → S/ 600,172 | **S/ 2,146,994** | 9.5 meses | S/ 4,030,981 | **167%** |

*Costo total acumulado a 3 años: S/ 1,450,000 (Capex) + 3 × S/ 320,000 (Opex) = S/ 2,410,000.*

### 6.4 Evidencia experimental sobre el supuesto de vencimiento recuperado

Los escenarios de §6.3 suponen que una mejor reposición recupera entre 25% y 45% de la merma por
vencimiento. Ese supuesto puede medirse en lugar de opinarse, y `src/policy_experiment.py` lo mide.

**Diseño.** La red se simula bajo tres políticas de reposición: la **heurística actual** (nivel
estático desde la demanda base del catálogo, ciega al patrón semanal), un **nivel plano** (misma
lógica estadística pero con la media de 14 días, *sin* estacionalidad) y el **pronóstico estacional**
(misma lógica con demanda por día de la semana). El nivel de servicio de cada política se elige en
20 semillas de entrenamiento y se evalúa en **100 semillas distintas**; las diferencias se
comparan por semilla con intervalos bootstrap del 95%. El nivel plano existe para aislar el valor
del *pronóstico* del valor de simplemente *dimensionar mejor* los pedidos.

| Política (EXPERIMENTO, media de 100 corridas de 60 días) | Utilidad neta | Merma total | del cual vencimiento | Ventas perdidas | Nivel de servicio |
|---|---:|---:|---:|---:|---:|
| Heurística actual | S/ 178,105 | S/ 2,343 | S/ 1,778 | S/ 3,729 | 99.2% |
| Nivel plano (z = 2.05) | S/ 177,294 | S/ 1,508 | S/ 1,001 | S/ 7,774 | 98.4% |
| **Pronóstico estacional (z = 2.5)** | **S/ 178,756** | **S/ 1,381** | **S/ 836** | S/ 4,874 | 99.0% |

Utilidad neta = margen bruto − merma. Diferencias por corrida, IC 95% entre corchetes:

| Comparación | Utilidad neta | Merma | Ventas perdidas |
|---|---:|---:|---:|
| Pronóstico vs. heurística | **+S/ 651** [+358, +952] | −S/ 962 [−1,028, −896] | +S/ 1,144 [+973, +1,314] |
| Nivel plano vs. heurística | −S/ 811 [−1,110, −504] | −S/ 835 [−893, −774] | +S/ 4,044 [+3,831, +4,250] |
| **Pronóstico vs. nivel plano** | **+S/ 1,462** [+1,209, +1,714] | −S/ 127 [−195, −58] | −S/ 2,900 [−3,099, −2,701] |

**Lo que dicen los datos:**

1. **La merma por vencimiento baja 53.0%, pero la utilidad neta sube solo 0.4%.** Del ahorro bruto de
   merma (S/ 962 por corrida) sobrevive el **67.7%** como utilidad neta (S/ 651); el resto se pierde
   en margen de ventas no realizadas por quiebre (S/ 311). Pedir menos siempre reduce la merma; lo
   que no siempre ocurre es que valga la pena.
2. **Bajar el stock sin estacionalidad pierde dinero** (−S/ 811 por corrida, significativo). Un
   promedio plano subestima el fin de semana y sobreestima los días de semana a la vez.
3. **El valor está en el pronóstico, no en la política más magra.** Pronóstico vs. nivel plano es
   +S/ 1,462 por corrida, y es lo que vuelve rentable el pedido más ajustado.
4. **Anualizado a 400 tiendas:** +S/ 651 por corrida de 4 tiendas equivale a S/ 990 por tienda por
   año y a **≈ S/ 396 mil al año** (IC 95%: S/ 218 mil – S/ 579 mil). La estimación puntual queda
   **entre el escenario conservador (S/ 332 mil) y el base (S/ 465 mil)** de §6.3, y el intervalo
   incluye ambos. El supuesto de recuperación del 35% del escenario base es plausible, no probado.

**Limitaciones que condicionan estas cifras:**

- **La demanda simulada es fuertemente semanal por construcción.** Un pronóstico por día de la
  semana está hecho para encontrar justo esa señal. El experimento mide el valor de capturar la
  estacionalidad *cuando existe*, no el valor de pronosticar en una tienda real arbitraria.
- **La heurística conoce la tasa base real** (la lee del catálogo), mientras que las políticas
  aprendidas la infieren de ventas censuradas por quiebres. Eso favorece a la heurística.
- **La utilidad neta omite costo de inventario y pérdida de clientes por quiebre.** Un quiebre cuesta
  más que el margen perdido; considerarlo favorecería políticas con mayor nivel de servicio. La tabla
  de frontera del informe permite reponderar ese equilibrio.
- **Las dos primeras versiones del experimento estaban mal especificadas** y se documentan en el
  informe: copiar la cobertura de `intervalo + 1` días de la heurística retenía dos días de stock
  perecible, y *descartar* los días de quiebre sesgaba la demanda a la baja. Ambas correcciones se
  decidieron tras corridas exploratorias en una sola semilla ajena a las 100 de evaluación y se
  aplicaron antes de la evaluación final; aun así, quien lea esto debe saber que existieron.

---

## 7. Hoja de Ruta de Implementación (Roadmap)

| Fase | Duración | Entregable Clave | Criterio de Éxito |
|---|---|---|---|
| **1. Cimientos** | Semanas 1–4 | Esquema 3NF, procedimiento FEFO, trigger antifraude, compuerta ETL | Suite de pruebas de regresión al 100% (`make test`) |
| **2. Piloto** | Semanas 5–12 | 4 tiendas piloto instrumentadas, reporte diario de exposición activo | Reducción de mermas por vencimiento ≥ 15% vs. histórico |
| **3. Medición** | Semanas 13–16 | Tablero Power BI activo, auditoría de proveedores en mesas comerciales | Primera nota de crédito recuperada ante proveedores |
| **4. Despliegue** | Meses 5–12 | Despliegue en 400 tiendas, barrido nocturno y reabastecimiento por DSI | Reducción global de intensidad de mermas ≥ 25% |
| **5. Optimización** | Mes 12+ | Pronóstico de demanda (`src/forecasting.py`, ya implementado y validado en simulación) recalibrado sobre el historial real de ventas | WAPE ≤ 25% sobre datos reales y reducción de ventas perdidas por quiebre de stock ≥ 30% |

---

## 8. Conclusiones

Tres cuartas partes de las mermas monetarias corresponden al vencimiento de productos (MEDIDO), siendo la única categoría que una regla determinista de rotación FEFO puede mitigar estructuralmente. La plataforma implementa esta regla directamente en las transacciones de base de datos y provee trazabilidad total a través de auditorías analíticas y tableros ejecutivos.

El escenario base proyecta un **beneficio anual de S/ 1.68 Millones frente a una inversión de S/ 1.45 Millones, recuperando la inversión en 12.8 meses con un ROI del 109% a tres años**.

Esa proyección descansa en tasas de recuperación supuestas. La evidencia experimental de §6.4 las contrasta con una medición: la reposición guiada por pronóstico reduce la merma por vencimiento 53%, pero retiene solo dos tercios de ese ahorro como utilidad neta, y únicamente si el pronóstico captura la estacionalidad semanal. El beneficio neto medido de la palanca de reposición (≈ S/ 396 mil al año) queda entre los escenarios conservador y base, lo que respalda la plausibilidad del supuesto sin demostrarlo sobre datos reales.

---

**Autor:** Odar Javier Alcocer Vásquez  
*Estudiante de Ingeniería de Sistemas de Información — Universidad Peruana de Ciencias Aplicadas (UPC)*  
*AWS Academy Cloud Foundations · Rutgers Supply Chain Analytics · SCRUMstudy SFC™*  
[LinkedIn](https://linkedin.com/in/odar-alcocer) · [GitHub @MrOdar](https://github.com/MrOdar)
