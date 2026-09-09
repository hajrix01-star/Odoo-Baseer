# Live migration and future updates

Status: source preparation only; no live server or automatic deployment is configured.

## First migration

1. Identify host, SSH access, domain, resources and an empty target database. Do not overwrite an existing database.
2. Prepare the accepted Odoo19 image and compatible PostgreSQL16. Use a restricted Odoo database owner, separate from bootstrap credentials. Keep secrets outside Git.
3. Configure HTTPS, proxy_mode, exact dbfilter, disabled database listing, private database ports and websocket forwarding. Choose workers only after measuring host resources.
4. Freeze local original writes briefly; take a fresh coherent database and filestore backup. Transfer privately with hashes. Never send backup archives or environment files to GitHub.
5. Restore into an isolated target; validate employees, companies, payroll configuration, all business-table projections, attachments and application flows. Configure server URL and external integrations intentionally.
6. Enable traffic only after successful checks. The live database then becomes authoritative. The old local original is retained as a backup and must not accept divergent business entries.

## Future code updates

Develop locally -> validate in QA -> freeze accepted release -> update code and release-source.json together -> push to GitHub -> CI -> serialized deployment to the identified server.

Each deployment must use the exact passing commit, take a coherent LIVE backup, upgrade affected modules and verify health and critical flows. Normal code updates must never restore a local database over live data. Database migrations are versioned and reviewed. No blind automatic code downgrade after a schema migration; recover using compatible source and its corresponding backup when appropriate.

Production workflow remains absent until host/domain, trusted SSH host key, deployment identity, secrets and target paths are known. Configure private off-host scheduled backups, retention, restore verification and failure notifications before launch. Do not publish edits on every filesystem save.

Official references:
- https://www.odoo.com/documentation/19.0/administration/on_premise/deploy.html
- https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments
