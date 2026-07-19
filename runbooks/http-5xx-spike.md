# Elevated HTTP 5xx error rate

## Symptoms
- 5xx rate jumps well above baseline (which should be < 1%).
- Often begins immediately after a deploy or config change.
- May be concentrated on specific endpoints or a specific instance.

## Likely causes
- Bad deploy: regression, failed migration, or misconfiguration.
- A downstream dependency failing or timing out (errors surface as 5xx).
- Resource exhaustion (CPU throttling, memory, connection pool).
- Bad config / secret rotation that broke auth to a dependency.

## Investigation
- Correlate the spike start with the deploy and config timeline.
- Break down 5xx by endpoint, status (500 vs 502/503/504), and instance.
- Check dependency health and recent migrations.

## Mitigation
- If it started with a deploy, roll back first, then diagnose.
- If a dependency is down, fail over or shed load; enable circuit breakers.
- Verify config/secrets restored correctly.
