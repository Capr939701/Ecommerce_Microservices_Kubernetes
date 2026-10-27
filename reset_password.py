import getpass
import hashlib
import secrets
import os
import sys
import psycopg2

email = "testcustomer@example.com"

password = getpass.getpass("Enter your new password: ")
confirm = getpass.getpass("Confirm your new password: ")

if password != confirm:
    sys.exit("Passwords do not match.")

if len(password) < 12:
    sys.exit("Use a password with at least 12 characters.")

salt = secrets.token_bytes(16)
digest = hashlib.scrypt(
    password.encode(),
    salt=salt,
    n=2**14,
    r=8,
    p=1
)
password_hash = salt.hex() + "$" + digest.hex()

script = r'''
import os
import psycopg2

password_hash, email = os.environ["RESET_DATA"].split("\n", 1)

connection = psycopg2.connect(
    host="auth-db",
    dbname=os.environ["POSTGRES_DB"],
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"]
)

try:
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET password_hash = %s WHERE email = %s",
                (password_hash, email)
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

result = subprocess.run(
    ["kubectl", "exec", "-i", "-n", "ecommerce",
     "deployment/user-auth-service", "--", "python", "-c", script],
    input=None,
    env=env,
    text=True,
    capture_output=True
)
print(result.stdout, end="")
if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    sys.exit(result.returncode)
