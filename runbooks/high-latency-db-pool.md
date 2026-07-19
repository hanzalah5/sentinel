# High p99 latency / database connection pool saturation

## Symptoms
- p99/p95 latency rises sharply (hundreds of ms to seconds) while error rate stays flat.
- Logs show "connection pool exhausted", "timed out waiting for connection", or waits
  acquiring a DB connection.
- Throughput plateaus even as request queue grows.

## Likely causes
- DB connection pool too small for current concurrency.
- Slow queries holding connections longer (missing index, lock contention, N+1).
- A downstream dependency slow, so connections are held while waiting.
- Connection leak in a recent deploy (connections not returned to the pool).

## Investigation
- Compare active vs max connections on the pool and on the database.
- Check slow-query logs and `pg_stat_activity` for long-running / idle-in-transaction.
- Correlate the onset with the most recent deployment.

## Mitigation
- Short term: raise the pool size / DB `max_connections` if the DB has headroom;
  restart leaking instances.
- Longer term: fix slow queries, add indexes, set statement timeouts, and cap pool
  size to what the DB can serve.
