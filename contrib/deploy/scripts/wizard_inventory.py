"""Import the real standalone exit endpoints into the local wizard panel inventory."""
import base64
import json
import subprocess
from uuid import NAMESPACE_URL, uuid5


def sync_exits(compose, env, exits):
    if not exits:
        return
    rows = []
    for exit_node in exits:
        key = exit_node["public_key"]
        if len(base64.b64decode(key, validate=True)) != 32:
            raise ValueError("Выход вернул неверный публичный ключ ANet.")
        rows.append(dict(exit_node, id=str(uuid5(NAMESPACE_URL, "anet:wizard:exit:" + key))))
    # The wizard already owns this Compose project. Use its local database so an
    # operator can rotate the panel's admin password without breaking reconciliation.
    # Only standard base64 enters SQL syntax; all operator strings are JSON data.
    encoded = base64.b64encode(json.dumps(rows).encode()).decode()
    sql = f"""
BEGIN;
CREATE TEMP TABLE wizard_exits ON COMMIT DROP AS
 SELECT * FROM jsonb_to_recordset(convert_from(decode('{encoded}','base64'),'UTF8')::jsonb)
 AS e(id uuid, public_key text, address text, quic_port integer, ssh_port integer, ssh_user text);
ALTER TABLE wizard_exits ADD PRIMARY KEY (public_key), ADD UNIQUE (address, quic_port);
LOCK TABLE servers IN SHARE ROW EXCLUSIVE MODE;
DO $$ BEGIN
 IF EXISTS (
   SELECT 1 FROM wizard_exits e JOIN servers s
   ON (s.address = e.address AND s.quic_port = e.quic_port) OR s.id = e.id
   WHERE s.public_key <> e.public_key AND (s.public_key <> '' OR s.control_token_hash IS NOT NULL)
 ) OR EXISTS (
   SELECT e.id FROM wizard_exits e JOIN servers s ON s.public_key = e.public_key
     OR (s.address = e.address AND s.quic_port = e.quic_port)
   GROUP BY e.id HAVING count(*) > 1
 ) THEN
   RAISE EXCEPTION 'Conflicting exit identity: compare existing address, UDP port and public key in the panel';
 END IF;
END $$;
UPDATE servers s SET public_key=e.public_key, address=e.address, quic_port=e.quic_port,
    ssh_port=e.ssh_port, ssh_user=e.ssh_user, updated_at=now()
 FROM wizard_exits e
 WHERE (s.public_key=e.public_key OR (s.public_key='' AND s.address=e.address AND s.quic_port=e.quic_port))
   AND (s.public_key,s.address,s.quic_port,s.ssh_port,s.ssh_user) IS DISTINCT FROM
       (e.public_key,e.address,e.quic_port,e.ssh_port,e.ssh_user);
INSERT INTO servers (id,name,address,quic_port,public_key,ssh_port,ssh_user,created_at,updated_at)
 SELECT id,'Выход · ' || address || ' · UDP ' || quic_port,address,quic_port,public_key,ssh_port,ssh_user,now(),now()
 FROM wizard_exits e WHERE NOT EXISTS (SELECT 1 FROM servers s WHERE s.public_key=e.public_key);
COMMIT;
"""
    subprocess.run([*compose, "exec", "-T", "db", "psql", "-Xq", "-v", "ON_ERROR_STOP=1", "-U", "anet", "-d", "anet"],
                   env=env, input=sql.encode(), check=True, stdout=subprocess.DEVNULL)
