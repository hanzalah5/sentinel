# Pod CrashLoopBackOff

## Symptoms
- A pod restarts repeatedly; state shows `CrashLoopBackOff` with rising restart count.
- Readiness/liveness probes fail on startup; container exits shortly after start.

## Likely causes
- Bad config or missing/invalid environment variable or secret.
- Failed dependency at startup (DB/cache unreachable, migration not applied).
- Application bug crashing during initialization.
- Misconfigured liveness probe killing a slow-but-healthy start.

## Investigation
- Read logs from the previous (crashed) container instance, not just the current one.
- Check the exit code and any panic/stack trace at startup.
- Verify required env/secrets/config maps are present and valid.
- Confirm probe timings allow for real startup time.

## Mitigation
- Fix the config/secret or restore the missing dependency.
- Roll back a bad deploy.
- Relax liveness `initialDelaySeconds`/timeouts if startup is legitimately slow.
