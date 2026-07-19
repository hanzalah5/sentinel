# Container OOMKilled / memory pressure

## Symptoms
- Containers restart with reason `OOMKilled` (exit code 137).
- RSS climbs steadily until the cgroup memory limit, then the process is killed.
- Restarts cluster around traffic peaks or long-running requests.

## Likely causes
- Memory limit set too low for real working set.
- Memory leak (unbounded caches, accumulating buffers, unclosed resources).
- A recent deploy increased per-request memory (bigger payloads, new dependency).
- Load spike increasing concurrency and total heap.

## Investigation
- Plot RSS over time; a sawtooth that always hits the limit before a kill suggests a leak
  or an undersized limit.
- Compare memory limit to observed steady-state usage plus headroom.
- Diff the deploy that preceded the first OOM.

## Mitigation
- Short term: raise the memory limit if the host has capacity; roll back a suspect deploy.
- Longer term: fix the leak, bound caches, and set requests/limits from measured usage.
