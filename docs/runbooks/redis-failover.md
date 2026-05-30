# Runbook: Redis Failover

**Symptom:** Cache hit rate dropped to 0, Sentinel reports master down,
or app latency spiked because cache misses cascade to PG.

**Severity:** SEV2 — app should degrade, not die. If it died, that's a
separate bug to fix.

---

## 1. Confirm

```bash
redis-cli -h redis-sentinel-1.kix.internal -p 26379 SENTINEL masters
redis-cli -h redis-sentinel-1.kix.internal -p 26379 SENTINEL replicas mymaster
```

Look for `flags` containing `s_down` or `o_down`. If both Sentinels
agree the master is down, automatic failover should already be in
progress (quorum=2 of 3).

## 2. Let Sentinel do its job (default path)

By default we trust Sentinel. Verify the new master:

```bash
redis-cli -h redis-sentinel-1.kix.internal -p 26379 \
  SENTINEL get-master-addr-by-name mymaster
```

App connects via Sentinel-aware client and should re-discover in <30s.
If it doesn't, force a connection pool reset:

```bash
kubectl rollout restart -n kix deploy/kix-api
```

## 3. Manual failover (Sentinel stuck)

If Sentinel quorum is broken (e.g., two of three Sentinels also down):

```bash
# Identify the most caught-up replica
redis-cli -h redis-replica-1.kix.internal INFO replication

# On chosen replica, take over
redis-cli -h redis-replica-1.kix.internal REPLICAOF NO ONE

# Update DNS / app config
kubectl -n kix create secret generic redis-conn \
  --from-literal=REDIS_URL="redis://redis-replica-1.kix.internal:6379/0" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart -n kix deploy/kix-api
```

## 4. Lost-cache recovery

If we lost the entire dataset (RDB also gone) the cache will rebuild
itself. **Expect a 5–15 min latency spike** as PG absorbs the load.

Mitigations:

1. **Preload hot keys.** We keep a daily-refreshed list of the top 1000
   keys in `s3://kix-ops/redis-hotkeys.txt`:
   ```bash
   aws s3 cp s3://kix-ops/redis-hotkeys.txt /tmp/hotkeys.txt
   python scripts/warm_redis.py --input /tmp/hotkeys.txt
   ```
2. **Rate-limit the stampede.** If PG p99 > 500ms during recovery:
   ```bash
   kubectl set env deploy/kix-api CACHE_STAMPEDE_PROTECT=true
   ```
   This makes the app serve last-known-good values from local in-memory
   LRU for 30s while the global cache warms.
3. **Scale up PG read replicas** temporarily:
   ```bash
   gcloud sql instances patch pg-primary --read-replica-count=4
   ```

## 5. Hot key identification

If a single key is causing a hotspot post-recovery:

```bash
redis-cli --hotkeys -h <master>
# OR sample MONITOR for 10s (CAREFUL — high overhead):
timeout 10 redis-cli -h <master> MONITOR | \
  awk '{print $4}' | sort | uniq -c | sort -rn | head -20
```

For confirmed hot keys (>1k ops/sec on a single key), shard by hashing
into N suffixes (`key:0..key:9`) in the app layer — this requires a
code change, not a runtime fix.

## 6. Cluster slot rebalancing (Redis Cluster only)

If we're in Cluster mode (not Sentinel) and a node was permanently lost:

```bash
redis-cli --cluster check redis-1.kix.internal:6379
redis-cli --cluster fix redis-1.kix.internal:6379
redis-cli --cluster rebalance redis-1.kix.internal:6379 \
  --cluster-use-empty-masters
```

`fix` resolves slots in `migrating`/`importing` limbo. `rebalance`
spreads slots evenly across remaining masters. **Both can briefly
block writes** — schedule for low-traffic window if not urgent.

## 7. Verify after recovery

- Cache hit rate back to baseline (Grafana: `redis.keyspace_hits /
  (hits+misses)` > 0.85 for our workload).
- PG p99 back to baseline (<150ms).
- No keys with TTL = -1 unless intentional.
