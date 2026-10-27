# Manual Operation & Testing Instructions

This guide provides step-by-step commands to manually start, configure, bootstrap, test, and tear down the **E-Commerce Microservices Platform**.

Both **cURL (Bash / Linux / macOS)** and **PowerShell (Windows)** command examples are provided for every single step.

---

## 📋 Table of Contents
1. [Prerequisites & Initial Setup](#1-prerequisites--initial-setup)
2. [Starting the Application](#2-starting-the-application)
3. [Step-by-Step Manual Workflow](#3-step-by-step-manual-workflow)
   - [Step 1: Verify System Health](#step-1-verify-system-health)
   - [Step 2: Register a Customer User](#step-2-register-a-customer-user)
   - [Step 3: Verify Customer Email](#step-3-verify-customer-email)
   - [Step 4: Login to Obtain JWT Token](#step-4-login-to-obtain-jwt-token)
   - [Step 5: Bootstrap an Administrator User](#step-5-bootstrap-an-administrator-user)
   - [Step 6: Create & Publish Products (Admin)](#step-6-create--publish-products-admin)
   - [Step 7: Browse Products (Customer)](#step-7-browse-products-customer)
   - [Step 8: Manage Cart (Customer)](#step-8-manage-cart-customer)
   - [Step 9: Checkout Cart (Order Creation & Inventory Reservation)](#step-9-checkout-cart-order-creation--inventory-reservation)
   - [Step 10: Create Payment Order (Razorpay)](#step-10-create-payment-order-razorpay)
   - [Step 11: Verify Payment & Confirm Order](#step-11-verify-payment--confirm-order)
   - [Step 12: Check Order History & Profile](#step-12-check-order-history--profile)
4. [Running the Automated Test Suite](#4-running-the-automated-test-suite)
5. [Direct Database Inspection (psql)](#5-direct-database-inspection-psql)
6. [Viewing Logs & Troubleshooting](#6-viewing-logs--troubleshooting)
7. [Tearing Down the Application](#7-tearing-down-the-application)

---

## 1. Prerequisites & Initial Setup

1. **Docker Desktop** installed and running.
2. Open your terminal and navigate to the project directory:
   ```bash
   cd "Application"
   ```
3. Prepare the environment file:
   ```bash
   # Copy sample environment file if .env does not exist
   cp .env.example .env
   ```
   > [!IMPORTANT]
   > Ensure `.env` contains a valid `JWT_SECRET` (at least 32 characters) and your Razorpay test credentials (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`).

---

## 2. Starting the Application

Build and start all 11 Docker containers in detached mode:

```bash
docker compose up -d --build
```

Verify that all 11 containers are healthy:
```bash
docker compose ps
```

You should see 6 microservices and 5 PostgreSQL database containers reporting `(healthy)`:
- `ecommerce-gateway` (Port `8000`)
- `ecommerce-user-auth-service`
- `ecommerce-product-service`
- `ecommerce-payment-service`
- `ecommerce-order-service`
- `ecommerce-cart-service`
- `ecommerce-auth-db`, `ecommerce-product-db`, `ecommerce-payment-db`, `ecommerce-order-db`, `ecommerce-cart-db`

---

## 3. Step-by-Step Manual Workflow

### Step 1: Verify System Health

#### cURL
```bash
curl -s http://localhost:8000/health
```
#### PowerShell
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/health" -Method Get
```

**Expected Response**:
```json
{
  "status": "healthy",
  "service": "api-gateway",
  "version": "1.0.0"
}
```

---

### Step 2: Register a Customer User

#### cURL
```bash
curl -s -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "customer@example.com",
    "password": "Password123!",
    "full_name": "Test Customer"
  }'
```

#### PowerShell
```powershell
$registerResponse = Invoke-RestMethod -Uri "http://localhost:8000/auth/register" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"email":"customer@example.com","password":"Password123!","full_name":"Test Customer"}'
$registerResponse
```

**Expected Response**:
```json
{
  "user_id": 1,
  "email": "customer@example.com",
  "role": "CUSTOMER",
  "is_verified": false,
  "verification_code": "123456"
}
```
*(Save the 6-digit `verification_code` from the response).*

---

### Step 3: Verify Customer Email

#### cURL
```bash
curl -s -X POST http://localhost:8000/auth/verify-email \
  -H "Content-Type: application/json" \
  -d '{
    "email": "customer@example.com",
    "code": "123456"
  }'
```

#### PowerShell
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/auth/verify-email" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"email":"customer@example.com","code":"123456"}'
```

**Expected Response**:
```json
{
  "status": "success",
  "message": "Email verified successfully"
}
```

---

### Step 4: Login to Obtain JWT Token

#### cURL
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "customer@example.com",
    "password": "Password123!"
  }' | jq -r '.access_token')

echo "Customer Token: $TOKEN"
```

#### PowerShell
```powershell
$login = Invoke-RestMethod -Uri "http://localhost:8000/auth/login" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"email":"customer@example.com","password":"Password123!"}'
$CUSTOMER_TOKEN = $login.access_token
Write-Host "Customer Token: $CUSTOMER_TOKEN"
```

---

### Step 5: Bootstrap an Administrator User

> [!NOTE]
> Newly registered users default to `CUSTOMER` role. Creating or updating catalog products requires `ADMIN` or `PRODUCT_MANAGER` privileges. 

1. **Register the Admin User**:
   - Register `admin@example.com` via Step 2 and verify its email via Step 3.
2. **Promote the User to `ADMIN` directly in the database**:
   ```bash
   docker exec -i ecommerce-auth-db psql -U auth_user -d auth_db -c "UPDATE users SET role = 'ADMIN' WHERE email = 'admin@example.com';"
   ```
3. **Login as Admin to get an Admin Token**:
   #### cURL
   ```bash
   ADMIN_TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
     -H "Content-Type: application/json" \
     -d '{
       "email": "admin@example.com",
       "password": "AdminPassword123!"
     }' | jq -r '.access_token')
   echo "Admin Token: $ADMIN_TOKEN"
   ```
   #### PowerShell
   ```powershell
   $adminLogin = Invoke-RestMethod -Uri "http://localhost:8000/auth/login" `
     -Method Post `
     -ContentType "application/json" `
     -Body '{"email":"admin@example.com","password":"AdminPassword123!"}'
   $ADMIN_TOKEN = $adminLogin.access_token
   Write-Host "Admin Token: $ADMIN_TOKEN"
   ```

---

### Step 6: Create & Publish Products (Admin)

#### cURL
```bash
curl -s -X POST http://localhost:8000/products \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sku": "LAPTOP-001",
    "name": "Pro Developer Laptop 16-inch",
    "description": "High performance laptop with 32GB RAM",
    "price": 1499.99,
    "initial_stock": 10,
    "status": "PUBLISHED"
  }'
```

#### PowerShell
```powershell
$newProduct = Invoke-RestMethod -Uri "http://localhost:8000/products" `
  -Method Post `
  -Headers @{ "Authorization" = "Bearer $ADMIN_TOKEN" } `
  -ContentType "application/json" `
  -Body '{
    "sku": "LAPTOP-001",
    "name": "Pro Developer Laptop 16-inch",
    "description": "High performance laptop with 32GB RAM",
    "price": 1499.99,
    "initial_stock": 10,
    "status": "PUBLISHED"
  }'
$PRODUCT_ID = $newProduct.id
$PRODUCT_ID
```

---

### Step 7: Browse Products (Customer)

#### cURL
```bash
# List all published products
curl -s http://localhost:8000/products

# Get single product details by ID
curl -s http://localhost:8000/products/1
```

#### PowerShell
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/products" -Method Get
Invoke-RestMethod -Uri "http://localhost:8000/products/1" -Method Get
```

---

### Step 8: Manage Cart (Customer)

#### Add item to cart:
#### cURL
```bash
curl -s -X POST http://localhost:8000/cart/items \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "product_id": 1,
    "quantity": 2
  }'
```

#### PowerShell
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/cart/items" `
  -Method Post `
  -Headers @{ "Authorization" = "Bearer $CUSTOMER_TOKEN" } `
  -ContentType "application/json" `
  -Body '{"product_id": 1, "quantity": 2}'
```

#### View cart contents:
#### cURL
```bash
curl -s -X GET http://localhost:8000/cart \
  -H "Authorization: Bearer $CUSTOMER_TOKEN"
```
#### PowerShell
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/cart" `
  -Method Get `
  -Headers @{ "Authorization" = "Bearer $CUSTOMER_TOKEN" }
```

---

### Step 9: Checkout Cart (Order Creation & Inventory Reservation)

Checking out the cart validates stock, locks product inventory pessimistically, creates an order in `PAYMENT_PENDING` status, reserves stock, and clears the user's cart.

#### cURL
```bash
ORDER=$(curl -s -X POST http://localhost:8000/cart/checkout \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "shipping_address": "123 Tech Blvd, Silicon Valley, CA 94025"
  }')
ORDER_ID=$(echo $ORDER | jq -r '.id')
TOTAL_AMOUNT=$(echo $ORDER | jq -r '.total_amount')
echo "Created Order ID: $ORDER_ID, Total Amount: $TOTAL_AMOUNT"
```

#### PowerShell
```powershell
$order = Invoke-RestMethod -Uri "http://localhost:8000/cart/checkout" `
  -Method Post `
  -Headers @{ "Authorization" = "Bearer $CUSTOMER_TOKEN" } `
  -ContentType "application/json" `
  -Body '{"shipping_address": "123 Tech Blvd, Silicon Valley, CA 94025"}'
$ORDER_ID = $order.id
$TOTAL_AMOUNT = $order.total_amount
Write-Host "Created Order ID: $ORDER_ID, Total Amount: $TOTAL_AMOUNT"
```

---

### Step 10: Create Payment Order (Razorpay)

#### cURL
```bash
PAYMENT=$(curl -s -X POST http://localhost:8000/payments/create-order \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"order_id\": $ORDER_ID,
    \"amount\": $TOTAL_AMOUNT,
    \"currency\": \"INR\"
  }")
RAZORPAY_ORDER_ID=$(echo $PAYMENT | jq -r '.razorpay_order_id')
echo "Razorpay Order ID: $RAZORPAY_ORDER_ID"
```

#### PowerShell
```powershell
$payment = Invoke-RestMethod -Uri "http://localhost:8000/payments/create-order" `
  -Method Post `
  -Headers @{ "Authorization" = "Bearer $CUSTOMER_TOKEN" } `
  -ContentType "application/json" `
  -Body (@{
    order_id = $ORDER_ID
    amount = $TOTAL_AMOUNT
    currency = "INR"
  } | ConvertTo-Json)
$RAZORPAY_ORDER_ID = $payment.razorpay_order_id
Write-Host "Razorpay Order ID: $RAZORPAY_ORDER_ID"
```

---

### Step 11: Verify Payment & Confirm Order

To complete checkout in testing/sandbox, simulate the Razorpay payment callback:

#### cURL
```bash
# Calculate HMAC SHA256 signature using Python
PAYMENT_ID="pay_test_$(date +%s)"
SIGNATURE=$(python -c "import hmac, hashlib; print(hmac.new(b'YOUR_RAZORPAY_SECRET', b'${RAZORPAY_ORDER_ID}|${PAYMENT_ID}', hashlib.sha256).hexdigest())")

# Verify payment with Order Service
curl -s -X POST "http://localhost:8000/orders/$ORDER_ID/payment/verify" \
  -H "Authorization: Bearer $CUSTOMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"razorpay_order_id\": \"$RAZORPAY_ORDER_ID\",
    \"razorpay_payment_id\": \"$PAYMENT_ID\",
    \"razorpay_signature\": \"$SIGNATURE\"
  }"
```

#### PowerShell
```powershell
$PAYMENT_ID = "pay_test_" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

# Compute HMAC signature using .NET
$secretBytes = [System.Text.Encoding]::UTF8.GetBytes("YOUR_RAZORPAY_SECRET")
$msgBytes = [System.Text.Encoding]::UTF8.GetBytes("${RAZORPAY_ORDER_ID}|${PAYMENT_ID}")
$hmac = New-Object System.Security.Cryptography.HMACSHA256 @(,$secretBytes)
$SIGNATURE = ($hmac.ComputeHash($msgBytes) | ForEach-Object { "{0:x2}" -f $_ }) -join ""

Invoke-RestMethod -Uri "http://localhost:8000/orders/$ORDER_ID/payment/verify" `
  -Method Post `
  -Headers @{ "Authorization" = "Bearer $CUSTOMER_TOKEN" } `
  -ContentType "application/json" `
  -Body (@{
    razorpay_order_id = $RAZORPAY_ORDER_ID
    razorpay_payment_id = $PAYMENT_ID
    razorpay_signature = $SIGNATURE
  } | ConvertTo-Json)
```

**Expected Response**:
```json
{
  "status": "success",
  "order_id": 1,
  "order_status": "PAID"
}
```

---

### Step 12: Check Order History & Profile

#### View customer order history:
```bash
curl -s -X GET http://localhost:8000/orders/my-orders \
  -H "Authorization: Bearer $CUSTOMER_TOKEN"
```

#### View authenticated user profile:
```bash
curl -s -X GET http://localhost:8000/users/me \
  -H "Authorization: Bearer $CUSTOMER_TOKEN"
```

---

## 4. Running the Automated Test Suite

If you want to run the comprehensive 32-step end-to-end integration test suite at once:

- **Windows PowerShell**:
  ```powershell
  .\scripts\test_all.ps1
  ```
- **Bash (Linux / WSL / Git Bash)**:
  ```bash
  bash scripts/test_all.sh
  ```
- **Direct Python**:
  ```bash
  python scripts/test_microservices_e2e.py
  ```

---

## 5. Direct Database Inspection (psql)

To inspect records directly inside the database containers without installing local database clients:

- **Auth Database**:
  ```bash
  docker exec -it ecommerce-auth-db psql -U auth_user -d auth_db -c "\dt"
  docker exec -it ecommerce-auth-db psql -U auth_user -d auth_db -c "SELECT id, email, role, is_verified FROM users;"
  ```
- **Product Database**:
  ```bash
  docker exec -it ecommerce-product-db psql -U product_user -d product_db -c "SELECT id, sku, name, price, stock, status FROM products;"
  ```
- **Cart Database**:
  ```bash
  docker exec -it ecommerce-cart-db psql -U cart_user -d cart_db -c "SELECT * FROM cart_items;"
  ```
- **Order Database**:
  ```bash
  docker exec -it ecommerce-order-db psql -U order_user -d order_db -c "SELECT id, user_id, total_amount, status FROM orders;"
  ```
- **Payment Database**:
  ```bash
  docker exec -it ecommerce-payment-db psql -U payment_user -d payment_db -c "SELECT id, order_id, amount, status FROM payments;"
  ```

---

## 6. Viewing Logs & Troubleshooting

- View logs for a specific service:
  ```bash
  docker compose logs -f gateway
  docker compose logs -f product-service
  docker compose logs -f order-service
  ```
- View logs for all services (tail 50 lines):
  ```bash
  docker compose logs --tail=50
  ```
- Rebuild a single service after making code changes:
  ```bash
  docker compose up -d --build product-service
  ```

---

## 7. Tearing Down the Application

- **Stop containers (database volumes preserved)**:
  ```bash
  docker compose down
  ```
- **Stop containers and erase all database volumes (fresh reset)**:
  ```bash
  docker compose down -v
  ```
