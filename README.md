# open-llm-proxy

Multi-provider LLM proxy deployed on NRP Nautilus at `https://open-llm-proxy.nrp-nautilus.io`.

Routes requests to NRP, OpenRouter, or Nimbus based on model name. Logs all requests with origin header for per-app attribution.

## Deploy

```bash
# 1. Create the proxy secret (one-time)
kubectl create secret generic open-llm-proxy-secrets \
  --from-literal=nrp-api-key='...' \
  --from-literal=proxy-key='...' \
  -n biodiversity

# 2. Apply manifests
kubectl apply -f service.yaml -n biodiversity
kubectl apply -f ingress.yaml -n biodiversity
kubectl apply -f deployment.yaml -n biodiversity

# 3. Watch rollout
kubectl rollout status deployment/open-llm-proxy -n biodiversity
```

## Update

Push changes to `main`, then:

```bash
kubectl rollout restart deployment/open-llm-proxy -n biodiversity
```

## Health check

```
GET https://open-llm-proxy.nrp-nautilus.io/health
```
