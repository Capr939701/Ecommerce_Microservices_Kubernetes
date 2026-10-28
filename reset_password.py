"""Emergency / Maintenance Password Reset Script.

Allows an operator to securely hash a new password using scrypt and update
a user account directly in PostgreSQL (via kubectl exec into the auth pod).
"""

import getpass
import hashlib
import os
import secrets
import subprocess
import sys

# Target email account to reset
email = "testcustomer@example.com"

# Prompt operator for password with masked inputs
password = getpass.getpass("Enter your new password: ")
confirm = getpass.getpass("Confirm your new password: ")

if password != confirm:
    sys.exit("Passwords do not match.")

if len(password) < 12:
    sys.exit("Use a password with at least 12 characters.")

# Compute scrypt password hash matching auth-service specifications
salt = secrets.token_bytes(16)
digest = hashlib.scrypt(
    password.encode(),
    salt=salt,
    n=2**14,
    r=8,
    p=1,
)
password_hash = salt.hex() + "$" + digest.hex()

# Remote Python script to execute inside the cluster container
script = r'''
import os
import psycopg2

password_hash, email = os.environ["RESET_DATA"].split("\n", 1)

connection = psycopg2.connect(
    host="auth-db",
    dbname=os.environ["POSTGRES_DB"],
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
)

try:
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET password_hash = %s WHERE email = %s",
                (password_hash, email),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Expected one account to update, got {cursor.rowcount}"
                )
    print("Password updated successfully.")
finally:
    connection.close()
'''

env = os.environ.copy()
env["RESET_DATA"] = password_hash + "\n" + email

# Dispatch execution to the user-auth deployment in Kubernetes
result = subprocess.run(
    [
        "kubectl", "exec", "-i", "-n", "ecommerce",
        "deployment/user-auth-service", "--", "python", "-c", script,
    ],
    input=None,
    env=env,
    text=True,
    capture_output=True,
)

print(result.stdout, end="")
if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    sys.exit(result.returncode)
