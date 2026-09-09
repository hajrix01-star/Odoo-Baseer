# Sales summaries dashboard

Adds **Dashboards → Sales → Sales summaries**, using the native dashboard navigation and company selector. Install `baseer_sales_dashboard` together with its declared dependencies. The dashboard definition is shared across companies; each request reads only the active, authorized SAR company.

The existing `baseer.pos.daily.report._aggregate_days` remains the source of sales, customers and operating-day definitions. No accounting transactions, salary data, spreadsheet formulas or duplicated financial tables are created by viewing the dashboard.

## Metrics

- Sales include VAT and use approved summaries; canceled and draft summaries do not contribute.
- Customers are the counts entered in those summaries, not the number of native POS orders.
- Daily averages use completed operating days, including explicitly approved zero-sales working days. Closed and missing days are excluded.
- Average bill is approved sales divided by recorded customers, as requested by the owner. Positive sales without recorded customers make this ratio unavailable. Missing customer counts affect the daily customer average only when they occur in completed operating days.
- A month or a custom range up to 31 days is plotted daily. Year presets and longer custom ranges are plotted monthly. Monthly display does not change daily KPI denominators.
- Cards compare the corresponding prior calendar period, or the preceding equal-length period for custom/30-day ranges. Missing data do not become a 100% decline; a genuine prior zero can display New instead of an infinite percentage.

The native Odoo date selector appears in the standard top control panel, with relative periods, month, quarter, year, custom ranges and All time. All time uses the active company's approved source date boundaries. All time and open-ended ranges do not invent a previous comparison. Historical aggregation reuses the daily report in chunks of at most 366 days and combines numerators before calculating averages. Requests exceeding 100 calendar years or 100,000 source rows are rejected explicitly, never silently shortened. Period selection, comparisons, aggregation and decimal formatting run in Python. The browser only renders supplied values. Charts distinguish recorded partial periods and leave gaps where no approved data exist.

Below the timeline, shift performance shows morning, evening and full-day summaries separately. Its table and chart use the same company and date selection. Sales, customers and average bill come from each recorded scope; a full-day summary is never split into inferred shifts. Missing rows and approved zero values remain distinct.

Shift and payment performance appear as two adjacent cards on desktop and stack on mobile. Each opens on Chart; Details replaces its chart with the source-value table. Payment details omit the category column; a compact report footer shows category totals from the same validated allocations. Conditional data-quality warnings remain visible when needed.

Payment-method performance reports amount-matched allocation lines from approved summaries, including delivery applications. These are sales allocations, not settlement balances or net bank receipts. Missing or inconsistent allocation lines are reported as uncovered sales; no distribution is invented and percentage shares are unavailable until coverage is complete. No customer count or average bill is assigned to payment methods without a corresponding source.

Compact cards retain their prior-period arrows. Without approved data, cards display dashes and a faded, labeled decorative preview replaces the chart; no sample amounts are presented as business results.

Native spreadsheet sharing is not offered for this dashboard: its content comes from the protected server endpoint rather than a spreadsheet document. Other native dashboards retain their original rendering.

Languages: Arabic and English, with Western digits. Dependencies: existing Owl and Chart.js bundled with Odoo; no external JavaScript service or new library. License: LGPL-3.
