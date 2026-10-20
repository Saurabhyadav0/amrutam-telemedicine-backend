# Runbook: backup, restore and disaster recovery

**Objectives:** 99.95% availability (≈ 22 min downtime/month). Data RPO ≤ 5 min within the
region and ≤ 15 min across regions. RTO ≤ 30 min for data restores and ≤ 2 h for a full region
loss.

## What protects what

| Failure | Protection | Expected impact |
|---|---|---|
| Task / instance crash | ECS replaces tasks; ALB health checks on `/health/ready` | None |
| Bad deploy | ECS deployment circuit breaker rolls back automatically | Minutes of elevated errors at most |
| AZ outage | RDS Multi-AZ (synchronous standby), ElastiCache Multi-AZ, tasks in 3 AZs | 1–2 min failover, no data loss |
| Logical corruption (bad migration, buggy job, mistaken delete) | RDS point-in-time recovery: daily snapshots + WAL, 30 days | Restore to the second before the incident |
| Region outage | Automated backups + WAL continuously replicated to ap-south-2; Terraform recreates the stack | ≤ 15 min of data, ≤ 2 h to serve |
| Ransomware / malicious admin | AWS Backup vault in **compliance-mode lock** (no early deletion, even by root): daily ×35 d, monthly ×7 y | Restore from the vault |
| Redis loss | Not backed up for recovery: caches, rate limits and arq queues are reconstructible; the outbox in Postgres is the source of truth for pending work | Cold cache for a few minutes |

## Procedures

### A. Point-in-time restore (logical corruption)
1. Freeze writes: scale the `api` and `worker` services to 0, or enable a maintenance response at
   the ALB.
2. Find the target time from the audit log / deploy history (e.g. 30 s before the bad migration).
3. `aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier amrutam-production-pg --target-db-instance-identifier amrutam-production-pg-restore --restore-time <UTC>` (same subnet group, parameter group and KMS key).
4. Verify on the restored instance: row counts on `consultations`, `payments`, `prescriptions`; run
   the audit chain check (`GET /v1/admin/audit-logs/verify` against a task pointed at the restore).
5. Swap: point the `DATABASE_URL` secret at the restored endpoint (or rename instances), then
   redeploy `api` and `worker`.
6. Reconcile payments for the lost window against the gateway's settlement report. Webhooks are
   idempotent, so asking the gateway to resend events for the window is safe.

### B. Region failover (ap-south-1 → ap-south-2)
1. Declare the incident; stop deploys.
2. Restore the latest replicated automated backup in ap-south-2 (`aws rds restore-db-instance-to-point-in-time --source-db-instance-automated-backups-arn <replicated ARN> --use-latest-restorable-time`).
3. `terraform apply -var region=ap-south-2 -var-file=envs/production.tfvars` in a DR workspace
   (VPC, ECS, Redis, ALB, WAF); copy secrets to the DR region's Secrets Manager.
4. Run migrations (no-op if already current), start `api` + `worker`, smoke test `/health/ready`.
5. Move DNS (Route 53 failover record or a manual weight change); confirm webhooks reach the new
   ALB.
6. On recovery of the primary region, re-establish replication the other way and fail back in a
   maintenance window.

### C. Restore from the locked vault
Use when PITR history is unusable: `aws backup start-restore-job` on the latest good recovery
point in `amrutam-production-vault`, then follow A.4–A.6.

## Drills (quarterly)

* Restore yesterday's snapshot to a scratch instance; run the migration check, the audit chain
  check and row-count checks; record the measured RTO.
* Twice a year, rehearse B in staging end to end.
* Every drill produces a short report: what broke, timings against RPO/RTO, and fixes.
