# SAP integration plan (NOT IMPLEMENTED — nothing connects to SAP)
**Principles:** read-only; approved views/OData/RFC only (no direct table writes, no unrestricted table access); every load logged to
`refresh_log` with attempted/success timestamps, row counts, errors and reconciliation; duplicates prevented by business key; the file
path (CSV) must keep working when SAP is unavailable. Adapter interface (planned): `fetch(source, since) -> rows`, `map(rows) -> model records`,
`validate(records) -> issues`, `reconcile(records, control_totals) -> report`; the importer in `mpi_aps/importer.py` is the first implementation.

**Candidate source mapping — to be CONFIRMED with the SAP team (standard ECC/S4 objects; Midas custom fields/views unknown):**
| Planning data | Typical SAP source |
|---|---|
| Material master, base UoM, alt-UoM conversions | MARA, MARM |
| Sales orders & schedule lines, open/confirmed qty, dates | VBAK/VBAP/VBEP (+ delivery LIKP/LIPS for delivered qty) |
| BOM | STKO/STPO via MAST |
| Production versions / recipes / routings | MKAL, PLKO/PLPO/PLAS |
| Work centres, capacity, calendars | CRHD, KAKO, TC37A/TFACS/THOC |
| Production orders & confirmations (actuals) | AUFK/AFKO/AFPO/AFRU |
| Stock, reservations, receipts | MARD/MSKA, RESB, EKET/EKPO |
| Quality status | QALS/QAVE or batch/stock-type fields |
**Field-level mapping, unit semantics (is the SAP rate gross or net?), and the demonstrated-rate source (likely not SAP) are open decisions.**
