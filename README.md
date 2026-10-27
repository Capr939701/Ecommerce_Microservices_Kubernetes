# E-Commerce Microservices Architecture

An enterprise-grade, event-driven e-commerce platform built with FastAPI, PostgreSQL, Docker, and Razorpay integration. All services are decoupled behind a unified API Gateway with JWT-based authentication, network-isolated databases, connection pooling, and idempotent payment processing.

---

## 🏗️ System Architecture & Services Overview

The application consists of **6 FastAPI microservices** and **5 dedicated PostgreSQL databases**, all isolated into private subnets with healthcheck gating and automatic dependency orchestration:

```
                                   [ Client Traffic ]
                                           │
                                           ▼ (Port 8000)
                              ┌─────────────────────────┐
                              │       API Gateway       │
                              └────────────┬────────────┘
                                           │  internal-network (private)
         ┌──────────────────┬──────────────┼──────────────┬──────────────────┐
         ▼                  ▼              ▼              ▼                  ▼
┌─────────────────┐ ┌───────────────┐ ┌──────────┐ ┌──────────────┐ ┌─────────────────┐
│User/Auth Service│ │Product Service│ │Cart Svc  │ │Order Service │ │ Payment Service │
│   (Port 8001)   │ │  (Port 8002)  │ │(Port 8005│ │ (Port 8004)  │ │   (Port 8003)   │
└────────┬────────┘ └───────┬───────┘ └────┬─────┘ └──────┬───────┘ └────────┬────────┘
         │ auth-net         │ prod-net     │ cart-net     │ order-net        │ payment-net
         ▼                  ▼              ▼              ▼                  ▼
 ┌───────────────┐  ┌───────────────┐ ┌──────────┐ ┌──────────────┐  ┌───────────────┐
 │    auth-db    │  │  product-db   │ │ cart-db  │ │   order-db   │  │  payment-db   │
 │ (Postgres 16) │  │ (Postgres 16) │ │(Postgres)│ │(Postgres 16) │  │ (Postgres 16) │
 └───────────────┘  └───────────────┘ └──────────┘ └──────────────┘  └───────────────┘
```

### Services Directory

| Component | Port | Database | Primary Responsibilities |
|---|---:|---|---|
| **API Gateway** | `8000` | None | Unified entry point, route dispatching, Bearer JWT validation, proxying, header sanitation, and injection of trusted `X-Auth-User-Id` and `X-Auth-User-Role`. |
| **User/Auth Service** | `8001` | `auth_db` | User registration, 6-digit email verification, login with constant-time `scrypt` hashing (timing-attack protected), profile synchronization, and login auditing. |
| **Product & Inventory** | `8002` | `product_db` | Catalog management, SKU validation, inventory tracking, pessimistic row locking (`with_for_update`), atomic stock reservation, and stock release workflows. |
| **Payment Service** | `8003` | `payment_db` | Razorpay order generation, HMAC SHA256 signature verification, idempotent payment handling, and state management. |
| **Order Service** | `8004` | `order_db` | Multi-item checkout coordination, subtotal/tax calculations, stock reservation RPCs, payment retry orchestration, and order lifecycle states (`PAYMENT_PENDING`, `PAID`, `CANCELLED`). |
| **Cart Service** | `8005` | `cart_db` | Persistent shopping carts scoped per authenticated customer, quantity updates, product validation, and atomic checkout transitions. |

---

## 🔒 Security & Resilience Features

- **Zero Direct Database Exposure**: Databases are enclosed inside per-service subnets (`auth-network`, `product-network`, `payment-network`, `order-network`, `cart-network`). No database port is exposed to the host machine.
- **Trusted Gateway Identity Propagation**: Downstream services reject unauthenticated requests (`401 Unauthorized`) if `X-Auth-User-Id` is absent. Only the API Gateway is published to port `8000`.
- **Timing-Attack Defense**: The authentication service executes constant-time `scrypt` derivations against a `DUMMY_PASSWORD_HASH` when an email is not found, mitigating account enumeration.
- **Idempotent Operations**:
  - Payment order creation rejects duplicates and returns existing Razorpay orders on retry.
  - Inventory reservations and releases are keyed by `order_id` and idempotent.
- **Fail-Fast Startup Configuration**: All services validate credentials (`JWT_SECRET` length $\ge 32$, Razorpay keys, DB connectivity) inside modern FastAPI `lifespan` handlers.
- **Connection Pooling**: Outbound RPC calls utilize persistent, pooled `httpx.Client` connections to prevent TCP/TLS socket exhaustion.

---

## 🚀 Getting Started

### 1. Prerequisites
- **Docker Desktop** (with Docker Compose v2) running on Windows, macOS, or Linux.
- **Python 3.11+** (if executing the automated test runner locally).

### 2. Environment Configuration
From the project root:
```bash
# Verify or edit .env
cp .env.example .env
```
Ensure the following variables are defined in `.env`:
- `JWT_SECRET`: Random string with $\ge 32$ characters.
- `AUTH_DB_PASSWORD`, `PRODUCT_DB_PASSWORD`, `ORDER_DB_PASSWORD`, `PAYMENT_DB_PASSWORD`, `CART_DB_PASSWORD`.
- `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`: Razorpay test keys (e.g. `rzp_test_...`).
- `GATEWAY_PORT`: Defaults to `8000`.

### 3. Start the Platform
```bash
docker compose up -d --build
```
Inspect container health:
```bash
docker compose ps
```
All 11 containers will report `(healthy)`.

---

## 🧪 Testing & Verification

### Automated All-In-One Test Suite
We provide single-command test scripts that test all 32 end-to-end integration test assertions:

- **Windows PowerShell**:
  ```powershell
  .\scripts\test_all.ps1
  ```
- **Bash (Linux / WSL / Git Bash)**:
  ```bash
  bash scripts/test_all.sh
  ```

### Interactive API Documentation (Swagger)
Access Swagger UI directly through the API Gateway:
- **Swagger Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Check**: [http://localhost:8000/health](http://localhost:8000/health)

---

## 📖 Complete Manual Testing Guide

For individual API curl/PowerShell commands, admin account bootstrapping, and step-by-step feature verification, please refer to:
👉 **[`INSTRUCTIONS.md`](INSTRUCTIONS.md)**

---

## ☸️ Kubernetes & Minikube Deployment

The repository includes complete Kubernetes manifests located in `k8s/`:
- **Manifest File**: `k8s/ecommerce-all.yaml`
- **Kubernetes Documentation**: Refer to `k8s/README.md` for Minikube commands, secret creation, and deployment steps.

---

## 🛑 Stopping the Application

- **Stop containers** (data volumes preserved):
  ```bash
  docker compose down
  ```
- **Stop and delete all data volumes** (clean slate):
  ```bash
  docker compose down -v
  ```
