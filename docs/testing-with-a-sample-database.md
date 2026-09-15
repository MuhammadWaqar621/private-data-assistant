# Testing end-to-end with a throwaway sample database

`docker-compose.yml` deliberately ships **no** target databases. The whole
point of this product is that you point it at a database *you* already
have - so bundling one would be a fiction, and would quietly hide whether
the adapters really work against a real server.

This page gives you a real one to point it at, in about two minutes per
engine. Everything below is disposable: `docker rm -f` the container when
you're done and nothing is left behind.

Prerequisites: the stack is already up (`docker-compose up --build`) and
migrated (`docker-compose exec backend alembic upgrade head`), and you have
an access token:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","full_name":"Your Name","password":"A-long-Password-123!"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

## A note on hostnames

The backend runs inside the docker-compose network, so `localhost` from
*your shell* is not `localhost` from *the backend's point of view*. Two
options:

1. **Put the sample database on the same docker network** (used below) -
   then the backend reaches it by container name, e.g.
   `host: "sample-postgres"`.
2. Or publish it on a host port and use `host.docker.internal` (Docker
   Desktop on Windows/macOS) as the host from the backend's perspective.

The compose project is named `private-data-assistant`, so its default
network is `private-data-assistant_default`. Check with
`docker network ls`.

---

## PostgreSQL

```bash
docker run -d --name sample-postgres \
  --network private-data-assistant_default \
  -e POSTGRES_PASSWORD=samplepass \
  -e POSTGRES_USER=sampleuser \
  -e POSTGRES_DB=shop \
  -p 55432:5432 postgres:16

# seed a couple of tables worth asking questions about
docker exec -i sample-postgres psql -U sampleuser -d shop <<'SQL'
CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  country TEXT NOT NULL,
  created_at DATE NOT NULL DEFAULT CURRENT_DATE
);
CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INT NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL,
  total NUMERIC(10,2) NOT NULL,
  placed_at DATE NOT NULL
);
INSERT INTO customers (name, country) VALUES
  ('Acme Ltd','UK'), ('Globex','US'), ('Initech','US'), ('Umbrella','DE');
INSERT INTO orders (customer_id, status, total, placed_at) VALUES
  (1,'shipped',120.00,'2026-07-03'), (1,'shipped',85.50,'2026-07-19'),
  (2,'pending',430.00,'2026-08-01'), (2,'cancelled',12.00,'2026-08-02'),
  (3,'shipped',999.99,'2026-08-14'), (4,'shipped',54.25,'2026-09-01'),
  (4,'pending',76.00,'2026-09-04');
SQL
```

Register it:

```bash
curl -X POST http://localhost:8000/api/connections \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "Sample shop (Postgres)",
    "engine": "postgres",
    "host": "sample-postgres",
    "port": 5432,
    "database_name": "shop",
    "username": "sampleuser",
    "password": "samplepass"
  }'
# -> {"id": 1, ..., "status": "ready", "schema_indexed_at": "..."}
```

Good questions to try once you've bound it to a chat (see the root
README's "Ask a question"):

- "how many orders do we have?" - a plain `COUNT(*)`
- "what's our total revenue by country?" - forces it to find the foreign
  key between `orders` and `customers`
- "chart orders by status" - should produce a `chart` SSE event
- "hi" - should get a greeting with **no** query at all
- "delete all the orders" - should be refused, in plain language

## MySQL

```bash
docker run -d --name sample-mysql \
  --network private-data-assistant_default \
  -e MYSQL_ROOT_PASSWORD=rootpass \
  -e MYSQL_DATABASE=shop \
  -e MYSQL_USER=sampleuser \
  -e MYSQL_PASSWORD=samplepass \
  -p 53306:3306 mysql:8

# wait ~20s for first-run initialization, then:
docker exec -i sample-mysql mysql -usampleuser -psamplepass shop <<'SQL'
CREATE TABLE products (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  category VARCHAR(50) NOT NULL,
  price DECIMAL(10,2) NOT NULL,
  in_stock INT NOT NULL
);
INSERT INTO products (name, category, price, in_stock) VALUES
  ('Widget','hardware',9.99,120), ('Gadget','hardware',24.50,8),
  ('Sprocket','hardware',3.75,0), ('Support plan','services',199.00,999);
SQL
```

```bash
curl -X POST http://localhost:8000/api/connections \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Sample shop (MySQL)","engine":"mysql","host":"sample-mysql",
       "port":3306,"database_name":"shop","username":"sampleuser",
       "password":"samplepass"}'
```

## MongoDB

```bash
docker run -d --name sample-mongo \
  --network private-data-assistant_default \
  -e MONGO_INITDB_ROOT_USERNAME=sampleuser \
  -e MONGO_INITDB_ROOT_PASSWORD=samplepass \
  -e MONGO_INITDB_DATABASE=shop \
  -p 57017:27017 mongo:7

docker exec -i sample-mongo mongosh -u sampleuser -p samplepass \
  --authenticationDatabase admin shop <<'JS'
db.events.insertMany([
  {type: "signup",  plan: "free", country: "UK", amount: 0},
  {type: "upgrade", plan: "pro",  country: "UK", amount: 49},
  {type: "upgrade", plan: "pro",  country: "US", amount: 49},
  {type: "churn",   plan: "pro",  country: "DE", amount: -49},
  {type: "upgrade", plan: "team", country: "US", amount: 199}
]);
JS
```

```bash
curl -X POST http://localhost:8000/api/connections \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Sample events (Mongo)","engine":"mongodb","host":"sample-mongo",
       "port":27017,"database_name":"shop","username":"sampleuser",
       "password":"samplepass","extra_params":{"auth_source":"admin"}}'
```

Note `auth_source: "admin"` - the root user created by the `mongo` image
lives in the `admin` database, not in `shop`. Without it, authentication
fails and the connection lands as `status: "failed"` with a message saying
so (which is itself worth seeing once).

## SQLite (a file upload, not a server)

```bash
python - <<'PY'
import sqlite3
connection = sqlite3.connect("sample.sqlite")
connection.executescript("""
CREATE TABLE employees (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  department TEXT NOT NULL,
  salary INTEGER NOT NULL,
  hired_on TEXT NOT NULL
);
INSERT INTO employees (name, department, salary, hired_on) VALUES
  ('Ada','engineering',120000,'2023-01-15'),
  ('Grace','engineering',135000,'2021-06-01'),
  ('Alan','research',110000,'2024-03-20'),
  ('Katherine','research',118000,'2022-11-05'),
  ('Margaret','engineering',142000,'2020-02-17');
""")
connection.commit()
connection.close()
PY

curl -X POST http://localhost:8000/api/connections/sqlite \
  -H "Authorization: Bearer $TOKEN" \
  -F "name=Sample employees (SQLite)" \
  -F "file=@sample.sqlite"
```

The file is uploaded to Vercel Blob at
`{user_id}/{connection_id}/database.sqlite` (see
`backend/app/engine/blob_storage.py`) - locally this needs
`BLOB_READ_WRITE_TOKEN` set in `.env` even under docker-compose, since
there is no local filesystem fallback once a connection is registered.
Every query downloads it to a temp file and opens that `mode=ro` - see the
root README's per-engine read-only section.

## SQL Server

The heaviest of the five, and the one most worth testing if you're going
to rely on it, because T-SQL's `TOP` vs `LIMIT` difference is handled
specially.

```bash
docker run -d --name sample-mssql \
  --network private-data-assistant_default \
  -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='A-Strong-Passw0rd!' \
  -p 51433:1433 mcr.microsoft.com/mssql/server:2022-latest

# wait ~30s for startup, then create a database and a table
docker exec -i sample-mssql /opt/mssql-tools18/bin/sqlcmd -C \
  -S localhost -U sa -P 'A-Strong-Passw0rd!' -Q "
CREATE DATABASE shop;
GO
USE shop;
CREATE TABLE tickets (
  id INT IDENTITY PRIMARY KEY,
  priority VARCHAR(20) NOT NULL,
  status VARCHAR(20) NOT NULL,
  opened_at DATE NOT NULL
);
INSERT INTO tickets (priority, status, opened_at) VALUES
  ('high','open','2026-08-01'), ('low','closed','2026-08-03'),
  ('high','closed','2026-08-11'), ('medium','open','2026-09-02');
"
```

```bash
curl -X POST http://localhost:8000/api/connections \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Sample tickets (MSSQL)","engine":"mssql","host":"sample-mssql",
       "port":1433,"database_name":"shop","username":"sa",
       "password":"A-Strong-Passw0rd!"}'
```

## Give the assistant read-only credentials

Everything above uses an admin/owner account for convenience. Once you've
seen it work, do it properly: create a database user with `SELECT`-only
grants and register *that*. The application's own guard already refuses
writes, but defense in depth costs one statement:

```sql
-- Postgres
CREATE USER assistant WITH PASSWORD 'something-long';
GRANT CONNECT ON DATABASE shop TO assistant;
GRANT USAGE ON SCHEMA public TO assistant;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO assistant;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO assistant;
```

```sql
-- MySQL
CREATE USER 'assistant'@'%' IDENTIFIED BY 'something-long';
GRANT SELECT ON shop.* TO 'assistant'@'%';
```

```javascript
// MongoDB
db.createUser({user: "assistant", pwd: "something-long",
               roles: [{role: "read", db: "shop"}]})
```

This also makes the failure modes realistic: a table the assistant can't
read simply won't appear in its schema index, and it will say so rather
than producing a permission error mid-answer.

## Cleaning up

```bash
docker rm -f sample-postgres sample-mysql sample-mongo sample-mssql
rm -f sample.sqlite
```

Delete the registrations too (this also removes their schema vectors from
Postgres/pgvector and, for SQLite, the stored blob):

```bash
curl -X DELETE http://localhost:8000/api/connections/1 -H "Authorization: Bearer $TOKEN"
```
