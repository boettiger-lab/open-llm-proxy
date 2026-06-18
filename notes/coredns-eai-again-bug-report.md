# Intermittent CoreDNS resolution failures (`EAI_AGAIN`) for external names

## Summary

Pods intermittently fail to resolve **external** hostnames via cluster DNS,
returning `Temporary failure in name resolution` (`getaddrinfo` → `EAI_AGAIN`).
Failures are bursty (seconds to minutes), then resolution recovers on its own.
This surfaces in applications as sporadic outbound-connection failures to
external services (HTTPS calls, APIs, webhooks, etc.).

## Environment

- Cluster DNS: `nameserver 10.96.0.10` (kube-dns/CoreDNS), default `ndots:5`.
- Standard pods, default `dnsPolicy: ClusterFirst`, multiple namespaces/nodes.

## Symptom

- Outbound requests to external FQDNs intermittently fail at the DNS step
  (`EAI_AGAIN`), not at the connection/TLS step.
- In-pod `getent hosts <external-host>` / `dig @10.96.0.10 <external-host>` fail
  during a burst and succeed minutes later, with no pod or app change.

## Scope

- Observed across **multiple pods on different nodes**, roughly concurrently —
  i.e. correlated to the DNS service, not a single bad node or workload.
- Failure rate ~5–10% of external lookups during active periods; intra-cluster
  (`*.svc.cluster.local`) resolution appears unaffected.

## What we ruled out (so it's clearly resolver availability, not client config)

- **Not search-domain amplification:** reducing `ndots` (5 → 2) cut redundant
  queries but did **not** reduce the failure rate.
- **Not insufficient retries:** `options attempts:3 timeout:2` does not rescue
  it — the blip outlasts the resolver retry window.
- **Not application/client-specific:** independent clients/languages on
  different nodes see the same `EAI_AGAIN`.

## Likely area

CoreDNS availability/capacity, or its upstream/forward path being intermittently
unresponsive — e.g. CoreDNS pod restarts/evictions, upstream forwarder timeouts,
rate-limiting, or insufficient replicas/HPA under load. NodeLocal DNSCache, if
not deployed, would both mitigate and help localize.

## Requested

- Check CoreDNS pod health/restarts, query latency, and `SERVFAIL`/timeout rates
  during the bursts; review upstream forwarders.
- Consider NodeLocal DNSCache and/or CoreDNS replica/cache tuning.

## Reproduce

```sh
# From any pod, in a loop; expect intermittent failures during a burst:
for i in $(seq 1 200); do
  getent hosts <external-host> >/dev/null && echo "$i ok" || echo "$i FAIL"
  sleep 1
done
```
