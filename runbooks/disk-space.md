# Disk space nearly full

## Symptoms
- Volume usage climbing toward 100%; projected to fill within the hour.
- Writes may start failing ("No space left on device"); services degrade or crash.

## Likely causes
- Log growth (verbose logging, a log loop, missing rotation).
- Unbounded temp files, cache, or upload directory.
- Database/WAL growth or an unrotated backup on the same volume.
- A stuck process writing rapidly.

## Investigation
- Find the largest directories/files and their growth rate.
- Check whether log rotation is configured and running.
- Identify the process responsible for the recent growth.

## Mitigation
- Short term: rotate/compress or safely delete old logs; clear reclaimable temp/cache.
- Longer term: configure rotation and retention; move logs/backups off the data volume;
  alert earlier (e.g., at 80%).
