"""Snowflake connection, hashing, history, and LMA helpers."""

import datetime
import hashlib
import os
import subprocess

import snowflake.connector

# All backups land here regardless of env (per team decision).
BACKUP_DB_SCHEMA = "MY_PROJECT_DEV_PREP_DB.BACKUPS"


def connect(cfg):
    common = {
        "account": cfg["account"],
        "role": cfg["role"],
        "warehouse": cfg["warehouse"],
    }
    token = os.environ.get("SNOWFLAKE_ID_TOKEN")
    if token:
        return snowflake.connector.connect(
            authenticator="WORKLOAD_IDENTITY",
            workload_identity_provider="OIDC",
            token=token,
            **common,
        )
    password = os.environ.get("SNOWFLAKE_PASSWORD")
    if password:
        return snowflake.connector.connect(
            user=cfg["user"], password=password, **common
        )
    raise SystemExit(
        "No credentials: set SNOWFLAKE_ID_TOKEN (CI) or SNOWFLAKE_PASSWORD (local)."
    )


def execute_multi(conn, sql):
    """Run a file containing multiple statements (e.g. catalog DELETE+INSERT).

    Uses Snowflake's native multi-statement support (num_statements=0 = "any
    number"). This parses statements correctly and, crucially, does NOT break
    on semicolons that appear INSIDE string literals — the catalog's
    QUERY_STATEMENT value contains its own ';', which a naive split would shred.
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, num_statements=0)
    finally:
        cur.close()


def execute(conn, sql):
    cur = conn.cursor()
    try:
        cur.execute(sql)
    finally:
        cur.close()


def fetch_one(conn, sql, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        return cur.fetchone()
    finally:
        cur.close()


def file_hash(sql):
    return hashlib.sha256(sql.encode()).hexdigest()


def git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def history_table(cfg):
    return cfg["history_table"].replace("{env}", cfg["env"])


def get_last_hashes(conn, cfg):
    table = history_table(cfg)
    sql = f"""
        SELECT OBJECT_NAME, FILE_HASH
        FROM {table}
        WHERE ENV = %s AND ACTION = 'DEPLOYED'
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY OBJECT_NAME ORDER BY EXECUTED_AT DESC) = 1
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, (cfg["env"],))
        return {row[0]: row[1] for row in cur.fetchall()}
    except snowflake.connector.errors.ProgrammingError as e:
        raise SystemExit(f"Could not read history {table}: {e}")
    finally:
        cur.close()


def record_history(conn, cfg, obj_name, obj_type, file_path, sql_hash, sha, action):
    table = history_table(cfg)
    cur = conn.cursor()
    try:
        cur.execute(
            f"INSERT INTO {table} "
            "(OBJECT_NAME, OBJECT_TYPE, FILE_PATH, FILE_HASH, GIT_SHA, ENV, ACTION) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (obj_name, obj_type, file_path, sql_hash, sha, cfg["env"], action),
        )
    finally:
        cur.close()


# ----------------------------------------------------------------- LMA ------


def table_exists(conn, fqtn):
    """fqtn = DB.SCHEMA.TABLE. Returns True if it exists."""
    db, schema, name = fqtn.split(".")
    row = fetch_one(
        conn,
        f"SELECT 1 FROM {db}.INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (schema, name),
    )
    return row is not None


def backup_table(conn, target_fqtn, env):
    """Snapshot target_fqtn into DEV_PREP_DB.BACKUPS.<OBJ>_<ENV>_BKP<YYYYMMDD>.

    Only call when the target table exists. Returns the backup name, or None
    if the target didn't exist (first deploy).
    """
    if not table_exists(conn, target_fqtn):
        return None
    obj = target_fqtn.split(".")[-1]
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d")
    backup_name = f"{obj}_{env.upper()}_BKP{stamp}"
    fq_backup = f"{BACKUP_DB_SCHEMA}.{backup_name}"
    execute(conn, f"CREATE OR REPLACE TABLE {fq_backup} AS SELECT * FROM {target_fqtn}")
    return fq_backup


def task_exists(conn, task_fqtn):
    db, schema, name = task_fqtn.split(".")
    row = fetch_one(conn, f"SHOW TASKS LIKE '{name}' IN SCHEMA {db}.{schema}")
    return row is not None
