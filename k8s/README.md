# Deploy to Minikube

This bundle deploys six application components (Gateway, Auth, Product, Order, Payment, Cart) and five separate PostgreSQL StatefulSets with PVCs. It is for local development/demo on Minikube, not production hardening.

## Prerequisites
- Docker Desktop running and Minikube started.
- Run commands from the project root in WSL2 Ubuntu.
- `kubectl config current-context` should report `minikube`.

## 1. Configure secrets
Create `.env` from `.env.example`, then replace placeholders with a random JWT secret (at least 32 characters), unique database passwords, and Razorpay **test-mode** credentials. Never commit `.env`.

```bash
cp .env.example .env
nano .env
kubectl create namespace ecommerce --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic ecommerce-secrets --namespace ecommerce --from-env-file=.env --dry-run=client -o yaml | kubectl apply -f -
```

The Secret contains the keys from `.env`; each pod references only the keys it needs. `AUTH_EXPOSE_VERIFICATION_CODE=true` is for local demos only.

## 2. Build application images into Minikube
The manifests use local images with `imagePullPolicy: IfNotPresent`. From the project root:

```bash
minikube image build -t ecommerce/api-gateway:1.0 ./services/api-gateway
minikube image build -t ecommerce/auth-service:1.0 ./services/auth-service
minikube image build -t ecommerce/product-service:1.0 ./services/product-service
minikube image build -t ecommerce/order-service:1.0 ./services/order-service
minikube image build -t ecommerce/payment-service:1.0 ./services/payment-service
minikube image build -t ecommerce/cart-service:1.0 ./services/cart-service
```

If your Minikube version does not support `minikube image build`, run `docker build -t ecommerce/<service>:1.0 ./services/<service>` and then `minikube image load ecommerce/<service>:1.0` for each image.

## 3. Deploy
```bash
kubectl apply -f k8s/ecommerce-all.yaml
kubectl get pods -n ecommerce -w
```
Wait for all five DB pods and six app pods to become Ready. For troubleshooting:
```bash
kubectl get all,pvc -n ecommerce
kubectl get events -n ecommerce --sort-by=.lastTimestamp
kubectl logs -n ecommerce deployment/user-auth-service
kubectl logs -n ecommerce deployment/product-service
kubectl logs -n ecommerce deployment/order-service
kubectl logs -n ecommerce deployment/payment-service
kubectl logs -n ecommerce deployment/cart-service
kubectl logs -n ecommerce deployment/api-gateway
```

## 4. Access Gateway
```bash
kubectl port-forward -n ecommerce service/api-gateway 8000:8000
```
Open `http://127.0.0.1:8000/docs` and `http://127.0.0.1:8000/health`. Keep the terminal open while testing.

## 5. Updating and cleanup
After code changes, rebuild the relevant image, then run `kubectl rollout restart deployment/<deployment-name> -n ecommerce`.

To stop application deployments without deleting persistent data: `kubectl scale deployment --all --replicas=0 -n ecommerce` (the DB StatefulSets remain running). To delete all resources and DB data: `kubectl delete namespace ecommerce`; only do this if you intend to erase the local databases.

## Notes
- PostgreSQL data uses PVCs. Minikube disk space is finite.
- One replica per service is configured for local testing.
- Verification code is exposed in registration responses only for local demos; real email delivery is not configured.
- Kubernetes Secret values are base64-encoded, not encrypted by default at rest in a local cluster. Keep `.env` private.
- No Ingress/TLS, production secret manager, backups, or autoscaling is included.
- If a pod fails health checks, inspect its logs before repeatedly restarting it. Live Kubernetes deployment has not been verified in this authoring environment.
