import csv
import hmac
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DB_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / "data" / "disparos.db"))
UAZAPI_URL = os.environ.get("UAZAPI_URL", "").rstrip("/")
UAZAPI_TOKEN = os.environ.get("UAZAPI_TOKEN", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
APP_API_KEY = os.environ.get("APP_API_KEY", "")
MAX_BODY_SIZE = 18 * 1024 * 1024
SEND_INTERVAL_SECONDS = max(1.0, float(os.environ.get("SEND_INTERVAL_SECONDS", "2")))
CAMPAIGN_LOCKS = {}
LOCKS_GUARD = threading.Lock()


class UazapiRequestError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"Uazapi HTTP {status}: {message[:700]}")
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def contact_group_ids(db, contact_ids):
    result = {contact_id: [] for contact_id in contact_ids}
    if not result:
        return result
    placeholders = ",".join("?" for _ in result)
    rows = db.execute(
        f"""SELECT contact_id, group_id FROM contact_group_memberships
            WHERE contact_id IN ({placeholders}) ORDER BY group_id""",
        tuple(result),
    ).fetchall()
    for row in rows:
        result[row["contact_id"]].append(row["group_id"])
    return result


def validated_contact_group_ids(db, group_ids):
    if not isinstance(group_ids, list) or any(
        not isinstance(group_id, str) or not group_id for group_id in group_ids
    ):
        raise ValueError("Selecione uma lista válida de grupos.")
    group_ids = list(dict.fromkeys(group_ids))
    if group_ids:
        placeholders = ",".join("?" for _ in group_ids)
        found = db.execute(
            f"SELECT id FROM contact_groups WHERE id IN ({placeholders})", group_ids
        ).fetchall()
        if len(found) != len(group_ids):
            raise ValueError("Um ou mais grupos selecionados não existem.")
    return group_ids


def set_contact_groups(db, contact_id, group_ids):
    db.execute("DELETE FROM contact_group_memberships WHERE contact_id=?", (contact_id,))
    db.executemany(
        """INSERT INTO contact_group_memberships(group_id, contact_id, created_at)
           VALUES (?, ?, ?)""",
        [(group_id, contact_id, now()) for group_id in group_ids],
    )


@contextmanager
def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_db():
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                opted_in INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'manual',
                group_jid TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT NOT NULL DEFAULT '',
                whatsapp_status TEXT NOT NULL DEFAULT 'unknown'
            );
            CREATE TABLE IF NOT EXISTS contact_groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contact_group_memberships (
                group_id TEXT NOT NULL REFERENCES contact_groups(id) ON DELETE CASCADE,
                contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY(group_id, contact_id)
            );
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                media TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS campaign_recipients (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                contact_id TEXT NOT NULL REFERENCES contacts(id),
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT '',
                contact_name_snapshot TEXT NOT NULL DEFAULT '',
                contact_phone_snapshot TEXT NOT NULL DEFAULT '',
                UNIQUE(campaign_id, contact_id)
            );
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                keyword TEXT NOT NULL,
                reply_text TEXT NOT NULL DEFAULT '',
                media TEXT NOT NULL DEFAULT '',
                webhook_url TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                graph_json TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS activity (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            """
        )
        workflow_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(workflows)").fetchall()
        }
        if "graph_json" not in workflow_columns:
            db.execute("ALTER TABLE workflows ADD COLUMN graph_json TEXT NOT NULL DEFAULT ''")
        contact_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(contacts)").fetchall()
        }
        if "deleted_at" not in contact_columns:
            db.execute("ALTER TABLE contacts ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
        if "whatsapp_status" not in contact_columns:
            db.execute("ALTER TABLE contacts ADD COLUMN whatsapp_status TEXT NOT NULL DEFAULT 'unknown'")
        recipient_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(campaign_recipients)").fetchall()
        }
        if "contact_name_snapshot" not in recipient_columns:
            db.execute("ALTER TABLE campaign_recipients ADD COLUMN contact_name_snapshot TEXT NOT NULL DEFAULT ''")
        if "contact_phone_snapshot" not in recipient_columns:
            db.execute("ALTER TABLE campaign_recipients ADD COLUMN contact_phone_snapshot TEXT NOT NULL DEFAULT ''")
        db.execute(
            """UPDATE campaign_recipients
               SET contact_name_snapshot=COALESCE(
                   (SELECT name FROM contacts WHERE contacts.id=campaign_recipients.contact_id), ''
               ) WHERE contact_name_snapshot=''"""
        )
        db.execute(
            """UPDATE campaign_recipients
               SET contact_phone_snapshot=COALESCE(
                       (SELECT phone FROM contacts WHERE contacts.id=campaign_recipients.contact_id), ''
                   ) WHERE contact_phone_snapshot=''"""
        )
        db.execute(
            """UPDATE contacts SET whatsapp_status='unavailable'
               WHERE id IN (
                   SELECT DISTINCT contact_id FROM campaign_recipients
                   WHERE lower(error) LIKE '%not on whatsapp%'
               )"""
        )
        count = db.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
        if not count:
            db.executemany(
                """INSERT INTO workflows
                   (id, name, keyword, reply_text, active, created_at)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                [
                    (str(uuid.uuid4()), "Boas-vindas", "oi,olá,bom dia,boa tarde,boa noite",
                     "Olá! Como podemos ajudar?", now()),
                    (str(uuid.uuid4()), "Horário de atendimento", "horário,horario,atendimento",
                     "Recebemos sua mensagem. Nosso horário de atendimento é de segunda a sexta, das 9h às 18h.", now()),
                ],
            )
        db.execute(
            "UPDATE campaigns SET status='interrompida', finished_at=? WHERE status='enviando'",
            (now(),),
        )


def get_uazapi_config():
    with connect_db() as db:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key IN ('uazapi_url', 'uazapi_token')"
        ).fetchall()
    saved = {row["key"]: row["value"] for row in rows}
    return (
        saved.get("uazapi_url", UAZAPI_URL).rstrip("/"),
        saved.get("uazapi_token", UAZAPI_TOKEN),
    )


def get_public_base_url():
    with connect_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key='public_base_url'"
        ).fetchone()
    return (row["value"] if row else PUBLIC_BASE_URL).rstrip("/")


def get_app_api_key():
    if APP_API_KEY:
        return APP_API_KEY
    with connect_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key='app_api_key'"
        ).fetchone()
    return row["value"] if row else ""


def get_send_interval_seconds():
    with connect_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key='send_interval_seconds'"
        ).fetchone()
    return max(1.0, float(row["value"])) if row else SEND_INTERVAL_SECONDS


def uazapi_request(path, payload=None, method=None, server_url=None, token=None):
    configured_url, configured_token = get_uazapi_config()
    base_url = (server_url if server_url is not None else configured_url).rstrip("/")
    instance_token = token if token is not None else configured_token
    if not base_url or not instance_token:
        raise RuntimeError("Configure o Server URL e o token Uazapi na tela Integração.")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers={"token": instance_token, "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise UazapiRequestError(error.code, message) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Não foi possível conectar ao Uazapi: {error.reason}") from error


def record_activity(kind, detail):
    with connect_db() as db:
        db.execute(
            "INSERT INTO activity(id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), kind, detail[:1000], now()),
        )


def clean_phone(value):
    value = str(value or "").strip()
    if "@" in value:
        value = value.split("@", 1)[0]
    return re.sub(r"\D", "", value)


def phone_from_whatsapp_jid(value):
    jid = str(value or "").strip()
    if "@" not in jid:
        return None
    local, domain = jid.rsplit("@", 1)
    if domain.lower() != "s.whatsapp.net":
        return None
    local = local.split(":", 1)[0]
    phone = clean_phone(local)
    return phone if 8 <= len(phone) <= 15 else None


def phone_from_group_participant(participant):
    if not isinstance(participant, dict):
        return None
    for key in ("PhoneNumber", "phoneNumber", "phone_number", "JID", "jid"):
        phone = phone_from_whatsapp_jid(participant.get(key))
        if phone:
            return phone
    return None


def upsert_contact(phone, name="", opted_in=False, source="manual", group_jid="", group_ids=None):
    phone = clean_phone(phone)
    if not 8 <= len(phone) <= 15:
        return False
    stamp = now()
    with connect_db() as db:
        existing = db.execute("SELECT id FROM contacts WHERE phone=?", (phone,)).fetchone()
        if existing:
            contact_id = existing["id"]
            db.execute(
                """UPDATE contacts SET name=CASE WHEN ?='' THEN name ELSE ? END,
                   opted_in=CASE WHEN ?='manual' THEN ? ELSE opted_in END,
                   source=CASE WHEN source='manual' THEN ? ELSE source END,
                   group_jid=CASE WHEN ?='' THEN group_jid ELSE ? END,
                   deleted_at='', updated_at=? WHERE phone=?""",
                (name, name, source, int(bool(opted_in)), source, group_jid, group_jid, stamp, phone),
            )
        else:
            contact_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO contacts(id, phone, name, opted_in, source, group_jid, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (contact_id, phone, str(name or "")[:160], int(bool(opted_in)), source,
                 group_jid, stamp, stamp),
            )
        if group_ids is not None:
            set_contact_groups(
                db, contact_id, validated_contact_group_ids(db, group_ids)
            )
    return True


def media_payload(media, destination, text):
    if not media:
        return {"number": destination, "text": text}
    return {
        "number": destination,
        "type": media.get("type", "image"),
        "file": media.get("data", ""),
        "mimetype": media.get("mimetype", ""),
        "text": text,
    }


def send_message(destination, text, media=None):
    if media:
        return uazapi_request("/send/media", media_payload(media, destination, text))
    return uazapi_request("/send/text", {"number": destination, "text": text})


def personalize(text, contact):
    return (text or "").replace("{{nome}}", contact["name"] or "tudo bem").replace(
        "{{name}}", contact["name"] or "tudo bem"
    )


def run_campaign(campaign_id):
    with LOCKS_GUARD:
        lock = CAMPAIGN_LOCKS.setdefault(campaign_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        with connect_db() as db:
            campaign = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not campaign or campaign["status"] != "enviando":
                return
            recipients = db.execute(
                """SELECT cr.id AS recipient_id, c.* FROM campaign_recipients cr
                   JOIN contacts c ON c.id=cr.contact_id
                   WHERE cr.campaign_id=? AND cr.status='pending' ORDER BY c.name, c.phone""",
                (campaign_id,),
            ).fetchall()
        for contact in recipients:
            with connect_db() as db:
                latest = db.execute(
                    "SELECT opted_in, whatsapp_status FROM contacts WHERE id=?", (contact["id"],)
                ).fetchone()
                if not latest or not latest["opted_in"]:
                    db.execute(
                        "UPDATE campaign_recipients SET status='ignorado', error=? WHERE id=?",
                        ("Contato sem consentimento ativo.", contact["recipient_id"]),
                    )
                    continue
                if latest["whatsapp_status"] == "unavailable":
                    db.execute(
                        "UPDATE campaign_recipients SET status='ignorado', error=? WHERE id=?",
                        ("Número já rejeitado pela Uazapi como não registrado no WhatsApp; corrija o telefone antes de reenviar.", contact["recipient_id"]),
                    )
                    continue
            try:
                send_message(
                    contact["phone"],
                    personalize(campaign["text"], contact),
                    json.loads(campaign["media"]) if campaign["media"] else None,
                )
                with connect_db() as db:
                    db.execute(
                        "UPDATE campaign_recipients SET status='enviado', sent_at=? WHERE id=?",
                        (now(), contact["recipient_id"]),
                    )
            except Exception as error:
                error_text = str(error)[:700]
                with connect_db() as db:
                    db.execute(
                        "UPDATE campaign_recipients SET status='falhou', error=? WHERE id=?",
                        (error_text, contact["recipient_id"]),
                    )
                    if "not on whatsapp" in error_text.casefold():
                        db.execute(
                            "UPDATE contacts SET whatsapp_status='unavailable' WHERE id=?",
                            (contact["id"],),
                        )
            time.sleep(get_send_interval_seconds())
        with connect_db() as db:
            db.execute(
                "UPDATE campaigns SET status='concluída', finished_at=? WHERE id=?",
                (now(), campaign_id),
            )
        record_activity("campanha", f"Campanha concluída: {campaign['name']}")
    finally:
        lock.release()


def create_campaign_resend(source_id):
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        source = db.execute(
            "SELECT name, text, media, status FROM campaigns WHERE id=?", (source_id,)
        ).fetchone()
        if not source:
            raise LookupError("Campanha não encontrada.")
        if source["status"] in ("draft", "enviando"):
            raise ValueError("Somente campanhas já disparadas podem ser reenviadas.")
        recipients = db.execute(
            """SELECT c.id, c.name, c.phone FROM campaign_recipients cr
               JOIN contacts c ON c.id=cr.contact_id
               WHERE cr.campaign_id=? AND c.opted_in=1 AND c.deleted_at=''
                     AND c.whatsapp_status<>'unavailable'
               ORDER BY c.name, c.phone""",
            (source_id,),
        ).fetchall()
        if not recipients:
            raise ValueError(
                "Nenhum destinatário original ainda tem consentimento ativo e telefone disponível."
            )
        campaign_id = str(uuid.uuid4())
        campaign_name = (source["name"] + " (reenvio)")[:160]
        stamp = now()
        db.execute(
            """INSERT INTO campaigns(id,name,text,media,status,created_at,started_at)
               VALUES (?,?,?,?, 'enviando', ?, ?)""",
            (campaign_id, campaign_name, source["text"], source["media"], stamp, stamp),
        )
        db.executemany(
            """INSERT INTO campaign_recipients(
                   id,campaign_id,contact_id,contact_name_snapshot,contact_phone_snapshot
               ) VALUES (?,?,?,?,?)""",
            [
                (str(uuid.uuid4()), campaign_id, row["id"], row["name"], row["phone"])
                for row in recipients
            ],
        )
    record_activity("campanha", f"Reenvio iniciado: {campaign_name}.")
    return {"id": campaign_id, "name": campaign_name, "recipients": len(recipients)}


def event_values(value):
    text = ""
    sender = ""
    from_me = False

    def walk(item):
        nonlocal text, sender, from_me
        if isinstance(item, dict):
            for key, child in item.items():
                lowered = str(key).lower()
                if lowered in ("fromme", "wassentbyapi") and child is True:
                    from_me = True
                if not text and lowered in ("text", "conversation", "body", "caption"):
                    if isinstance(child, str):
                        text = child.strip()
                    elif isinstance(child, dict):
                        walk(child)
                if not sender and lowered in ("sender", "from", "chatid", "remotejid", "participant"):
                    if isinstance(child, str) and ("@" in child or clean_phone(child)):
                        sender = child
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return text, clean_phone(sender), from_me


def handle_webhook(event):
    _, instance_token = get_uazapi_config()
    if not isinstance(event, dict) or not hmac.compare_digest(
        str(event.get("token", "")), instance_token
    ):
        record_activity("webhook", "Evento ignorado: token de instância ausente ou inválido.")
        return
    text, phone, from_me = event_values(event)
    if not text or not phone or from_me:
        return
    if text.strip().lower() in ("sair", "parar", "cancelar", "stop"):
        upsert_contact(phone, opted_in=False, source="manual")
        try:
            send_message(phone, "Tudo certo. Removemos seu contato das campanhas.")
        except Exception as error:
            record_activity("webhook", f"Falha ao confirmar opt-out: {error}")
        record_activity("opt-out", f"Contato {phone} solicitou não receber campanhas.")
        return

    with connect_db() as db:
        workflows = db.execute(
            "SELECT * FROM workflows WHERE active=1 ORDER BY created_at"
        ).fetchall()
        contact = db.execute("SELECT * FROM contacts WHERE phone=?", (phone,)).fetchone()
    contact = dict(contact) if contact else {"phone": phone, "name": ""}
    normalized_text = text.casefold()
    for workflow in workflows:
        if workflow["graph_json"]:
            graph = validate_workflow_graph(json.loads(workflow["graph_json"]))
            if not execute_workflow_graph(graph, phone, contact, text, event, workflow["name"]):
                continue
            record_activity("automação", f"Fluxo '{workflow['name']}' acionado por {phone}.")
            break
        keywords = [item.strip().casefold() for item in workflow["keyword"].split(",") if item.strip()]
        if not any(keyword in normalized_text for keyword in keywords):
            continue
        if workflow["reply_text"] or workflow["media"]:
            send_message(
                phone,
                personalize(workflow["reply_text"], contact),
                json.loads(workflow["media"]) if workflow["media"] else None,
            )
        if workflow["webhook_url"]:
            safe_event = redact_sensitive(event)
            body = json.dumps(
                {"workflow": workflow["name"], "contact": contact, "message": text, "event": safe_event}
            ).encode("utf-8")
            request = urllib.request.Request(
                workflow["webhook_url"],
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10):
                pass
        record_activity("automação", f"Fluxo '{workflow['name']}' acionado por {phone}.")
        break


def redact_sensitive(value):
    if isinstance(value, dict):
        return {
            key: ("[redigido]" if str(key).lower() in ("token", "admintoken", "authorization") else redact_sensitive(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def validate_workflow_graph(graph):
    if not isinstance(graph, dict):
        raise ValueError("O canvas do workflow está inválido.")
    nodes, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list) or len(nodes) > 50 or len(edges) > 100:
        raise ValueError("O workflow deve ter até 50 blocos e 100 conexões.")
    by_id = {}
    allowed = {"trigger", "send_text", "send_media", "webhook"}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"] or node.get("type") not in allowed:
            raise ValueError("Há um bloco inválido no canvas.")
        if node["id"] in by_id:
            raise ValueError("O canvas contém IDs de bloco duplicados.")
        config = node.get("config", {})
        if not isinstance(config, dict):
            raise ValueError("A configuração de um bloco está inválida.")
        node["config"] = config
        if node["type"] == "trigger":
            config["keywords"] = str(config.get("keywords") or "")
        elif node["type"] == "send_text":
            config["text"] = str(config.get("text") or "")
        elif node["type"] == "send_media":
            config["caption"] = str(config.get("caption") or "")
        elif node["type"] == "webhook":
            config["url"] = str(config.get("url") or "").strip()
        by_id[node["id"]] = node
    triggers = [node for node in nodes if node["type"] == "trigger"]
    actions = [node for node in nodes if node["type"] != "trigger"]
    if not triggers or not actions:
        raise ValueError("Adicione ao menos um gatilho e uma ação.")
    for node in nodes:
        config = node["config"]
        if node["type"] == "trigger" and not str(config.get("keywords", "")).strip():
            raise ValueError("Configure as palavras-chave de todos os gatilhos.")
        if node["type"] == "send_text" and not str(config.get("text", "")).strip():
            raise ValueError("Configure a mensagem de todos os blocos de texto.")
        if node["type"] == "send_media":
            media = config.get("media")
            if not isinstance(media, dict) or media.get("type") not in ("image", "video") or not media.get("data"):
                raise ValueError("Adicione uma imagem ou vídeo em cada bloco de mídia.")
        if node["type"] == "webhook":
            url = str(config.get("url", "")).strip()
            parsed_url = urlparse(url)
            if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
                raise ValueError("Informe uma URL HTTP ou HTTPS válida no bloco webhook.")
    adjacency = {node_id: [] for node_id in by_id}
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("source") not in by_id or edge.get("target") not in by_id:
            raise ValueError("Há uma conexão ligada a um bloco inexistente.")
        if edge["source"] == edge["target"]:
            raise ValueError("Um bloco não pode ser conectado a si mesmo.")
        if by_id[edge["target"]]["type"] == "trigger":
            raise ValueError("Conecte gatilhos somente a blocos de ação.")
        adjacency[edge["source"]].append(edge["target"])
    visiting, visited = set(), set()

    def visit(node_id):
        if node_id in visiting:
            raise ValueError("O fluxo não pode conter ciclos.")
        if node_id in visited:
            return
        visiting.add(node_id)
        for target in adjacency[node_id]:
            visit(target)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in by_id:
        visit(node_id)
    if not any(adjacency[node["id"]] for node in triggers):
        raise ValueError("Conecte um gatilho a pelo menos uma ação.")
    reachable = set()
    pending = [node["id"] for node in triggers]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(adjacency[node_id])
    if any(node["type"] != "trigger" and node["id"] not in reachable for node in nodes):
        raise ValueError("Conecte todos os blocos de ação a um gatilho.")
    return {"nodes": nodes, "edges": edges}


def graph_legacy_fields(graph):
    nodes = graph["nodes"]
    triggers = [node for node in nodes if node["type"] == "trigger"]
    text = next((node["config"].get("text", "") for node in nodes if node["type"] == "send_text"), "")
    media = next((node["config"].get("media") for node in nodes if node["type"] == "send_media"), None)
    webhook = next((node["config"].get("url", "") for node in nodes if node["type"] == "webhook"), "")
    keywords = ",".join(
        keyword.strip()
        for node in triggers
        for keyword in str(node["config"].get("keywords", "")).split(",")
        if keyword.strip()
    )
    return keywords, text, json.dumps(media) if media else "", webhook


def execute_workflow_graph(graph, phone, contact, text, event, workflow_name):
    nodes = {node["id"]: node for node in graph["nodes"]}
    outgoing = {}
    for edge in graph["edges"]:
        outgoing.setdefault(edge["source"], []).append(edge["target"])
    keywords = text.casefold()
    pending = []
    matched = False
    for node in graph["nodes"]:
        if node["type"] == "trigger":
            terms = [term.strip().casefold() for term in node["config"].get("keywords", "").split(",") if term.strip()]
            if any(term in keywords for term in terms):
                matched = True
                pending.extend(outgoing.get(node["id"], []))
    if not matched:
        return False
    visited = set()
    while pending:
        node_id = pending.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        node = nodes[node_id]
        config = node["config"]
        if node["type"] == "send_text":
            send_message(phone, personalize(config.get("text", ""), contact))
        elif node["type"] == "send_media":
            send_message(
                phone,
                personalize(config.get("caption", ""), contact),
                config.get("media"),
            )
        elif node["type"] == "webhook":
            body = json.dumps({
                "workflow": workflow_name,
                "contact": contact,
                "message": text,
                "event": redact_sensitive(event),
            }).encode("utf-8")
            request = urllib.request.Request(
                config["url"], data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(request, timeout=10):
                pass
        pending.extend(outgoing.get(node_id, []))
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "DisparosLocal/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, status, data):
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY_SIZE:
            raise ValueError("Requisição muito grande.")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def require_auth(self):
        try:
            app_api_key = get_app_api_key()
            if app_api_key and not hmac.compare_digest(
                self.headers.get("X-App-Key", "").encode("utf-8"),
                app_api_key.encode("utf-8"),
            ):
                self.send_json(401, {"error": "Chave de acesso da aplicação inválida."})
                return False
        except Exception as error:
            self.send_json(500, {"error": f"Não foi possível validar a chave da aplicação: {error}"})
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/":
            return self.serve_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
        if path.startswith("/web/"):
            target = (WEB_ROOT / path.removeprefix("/web/")).resolve()
            if WEB_ROOT.resolve() not in target.parents:
                return self.send_json(404, {"error": "Não encontrado."})
            return self.serve_file(target)
        if not self.require_auth():
            return
        try:
            with connect_db() as db:
                if path == "/api/status":
                    server_url, instance_token = get_uazapi_config()
                    public_base_url = get_public_base_url()
                    eligible_contacts = db.execute(
                        """SELECT COUNT(*) FROM contacts
                           WHERE opted_in=1 AND deleted_at='' AND whatsapp_status<>'unavailable'"""
                    ).fetchone()[0]
                    unavailable_contacts = db.execute(
                        """SELECT COUNT(*) FROM contacts
                           WHERE deleted_at='' AND whatsapp_status='unavailable'"""
                    ).fetchone()[0]
                    return self.send_json(200, {
                        "configured": bool(server_url and instance_token),
                        "server_url": server_url,
                        "token_configured": bool(instance_token),
                        "public_base_url": public_base_url,
                        "public_webhook_url": public_base_url + "/webhooks/uazapi" if public_base_url else "",
                        "api_key_required": bool(get_app_api_key()),
                        "api_key_configured": bool(get_app_api_key()),
                        "api_key_managed_by_server": bool(APP_API_KEY),
                        "send_interval_seconds": get_send_interval_seconds(),
                        "eligible_contacts": eligible_contacts,
                        "unavailable_contacts": unavailable_contacts,
                    })
                if path == "/api/contact-groups":
                    rows = db.execute(
                        """SELECT g.id, g.name, COUNT(c.id) AS contact_count,
                                  SUM(CASE WHEN c.opted_in=1 AND c.whatsapp_status<>'unavailable'
                                           THEN 1 ELSE 0 END) AS eligible_count
                           FROM contact_groups g
                           LEFT JOIN contact_group_memberships m ON m.group_id=g.id
                           LEFT JOIN contacts c ON c.id=m.contact_id AND c.deleted_at=''
                           GROUP BY g.id ORDER BY g.name COLLATE NOCASE"""
                    ).fetchall()
                    return self.send_json(200, [dict(row) for row in rows])
                if path == "/api/contacts":
                    search = parse_qs(parsed.query).get("q", [""])[0].strip()
                    rows = db.execute(
                        """SELECT * FROM contacts WHERE deleted_at='' AND (phone LIKE ? OR name LIKE ?)
                           ORDER BY updated_at DESC LIMIT 500""",
                        (f"%{search}%", f"%{search}%"),
                    ).fetchall()
                    memberships = contact_group_ids(db, [row["id"] for row in rows])
                    return self.send_json(200, [
                        {**dict(row), "group_ids": memberships[row["id"]]} for row in rows
                    ])
                if path == "/api/campaigns":
                    rows = db.execute(
                        """SELECT c.*, COUNT(cr.id) AS total,
                           SUM(CASE WHEN cr.status='enviado' THEN 1 ELSE 0 END) AS sent,
                           SUM(CASE WHEN cr.status='falhou' THEN 1 ELSE 0 END) AS failed,
                           SUM(CASE WHEN cr.status='pending' THEN 1 ELSE 0 END) AS pending,
                           SUM(CASE WHEN cr.status='ignorado' THEN 1 ELSE 0 END) AS ignored
                           FROM campaigns c LEFT JOIN campaign_recipients cr ON cr.campaign_id=c.id
                           GROUP BY c.id ORDER BY c.created_at DESC"""
                    ).fetchall()
                    return self.send_json(200, [dict(row) for row in rows])
                if path.startswith("/api/campaigns/") and path.endswith("/contacts"):
                    campaign_id = path.split("/")[3]
                    if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
                        return self.send_json(404, {"error": "Campanha não encontrada."})
                    rows = db.execute(
                        """SELECT c.id, c.name, c.phone,
                                  EXISTS(
                                      SELECT 1 FROM campaign_recipients cr
                                      WHERE cr.campaign_id=? AND cr.contact_id=c.id
                                  ) AS selected
                           FROM contacts c
                           WHERE c.opted_in=1 AND c.deleted_at='' AND c.whatsapp_status<>'unavailable'
                           ORDER BY c.name, c.phone""",
                        (campaign_id,),
                    ).fetchall()
                    memberships = contact_group_ids(db, [row["id"] for row in rows])
                    return self.send_json(200, [
                        {**dict(row), "group_ids": memberships[row["id"]]} for row in rows
                    ])
                if path.startswith("/api/campaigns/") and path.endswith("/recipients"):
                    campaign_id = path.split("/")[3]
                    rows = db.execute(
                        """SELECT cr.*,
                           CASE WHEN c.deleted_at<>'' THEN 'Contato removido'
                                ELSE COALESCE(NULLIF(cr.contact_name_snapshot,''), c.name) END AS name,
                           CASE WHEN c.deleted_at<>'' THEN 'Removido'
                                ELSE COALESCE(NULLIF(cr.contact_phone_snapshot,''), c.phone) END AS phone
                           FROM campaign_recipients cr
                           JOIN contacts c ON c.id=cr.contact_id WHERE cr.campaign_id=?
                           ORDER BY c.name, c.phone""", (campaign_id,)
                    ).fetchall()
                    return self.send_json(200, [dict(row) for row in rows])
                if path == "/api/workflows":
                    rows = db.execute("SELECT * FROM workflows ORDER BY created_at DESC").fetchall()
                    return self.send_json(200, [dict(row) for row in rows])
                if path == "/api/activity":
                    rows = db.execute(
                        "SELECT * FROM activity ORDER BY created_at DESC LIMIT 10"
                    ).fetchall()
                    return self.send_json(200, [dict(row) for row in rows])
        except Exception as error:
            return self.send_json(500, {"error": str(error)})
        self.send_json(404, {"error": "Rota não encontrada."})

    def serve_file(self, path, content_type=None):
        try:
            content = path.read_bytes()
        except OSError:
            self.send_json(404, {"error": "Arquivo não encontrado."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path == "/webhooks/uazapi":
            try:
                event = self.read_json()
                _, instance_token = get_uazapi_config()
                if not instance_token or not isinstance(event, dict) or not hmac.compare_digest(
                    str(event.get("token", "")), instance_token
                ):
                    return self.send_json(401, {"error": "Token de webhook inválido."})
                self.send_json(200, {"received": True})
                threading.Thread(target=self.process_webhook, args=(event,), daemon=True).start()
            except Exception as error:
                self.send_json(400, {"error": str(error)})
            return
        if not self.require_auth():
            return
        try:
            body = self.read_json()
            if path == "/api/contact-groups":
                name = body.get("name")
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
                    return self.send_json(400, {"error": "Informe um nome de grupo com até 80 caracteres."})
                group_id = str(uuid.uuid4())
                try:
                    with connect_db() as db:
                        db.execute(
                            "INSERT INTO contact_groups(id, name, created_at) VALUES (?, ?, ?)",
                            (group_id, name.strip(), now()),
                        )
                except sqlite3.IntegrityError:
                    return self.send_json(409, {"error": "Já existe um grupo com esse nome."})
                record_activity("contatos", f"Grupo de contatos criado: {name.strip()}.")
                return self.send_json(201, {"id": group_id, "name": name.strip()})
            if path == "/api/contacts":
                phone = clean_phone(body.get("phone"))
                if not 8 <= len(phone) <= 15:
                    return self.send_json(400, {"error": "Informe um telefone com código do país."})
                group_ids = body.get("group_ids") if "group_ids" in body else None
                upsert_contact(
                    phone, body.get("name", ""), body.get("opted_in", False),
                    group_ids=group_ids,
                )
                return self.send_json(201, {"ok": True})
            if path == "/api/contacts/import":
                reader = csv.DictReader(io.StringIO(body.get("csv", "")))
                if not reader.fieldnames:
                    return self.send_json(400, {"error": "CSV vazio ou sem cabeçalho."})
                keys = {key.lower().strip(): key for key in reader.fieldnames if key}
                phone_key = next((keys[k] for k in ("phone", "telefone", "celular", "whatsapp") if k in keys), None)
                name_key = next((keys[k] for k in ("name", "nome") if k in keys), None)
                consent_key = next((keys[k] for k in ("opted_in", "consentimento", "consent") if k in keys), None)
                if not phone_key:
                    return self.send_json(400, {"error": "O CSV deve ter uma coluna phone/telefone/celular."})
                imported = 0
                for row in reader:
                    value = str(row.get(consent_key, "")).lower() if consent_key else ""
                    consent = value in ("1", "true", "sim", "yes", "opt-in")
                    imported += bool(upsert_contact(
                        row.get(phone_key), row.get(name_key, "") if name_key else "",
                        consent, "csv"
                    ))
                record_activity("contatos", f"Importados {imported} contatos por CSV.")
                return self.send_json(200, {"imported": imported})
            if path == "/api/media":
                encoded = body.get("data", "")
                if not isinstance(encoded, str) or not encoded.startswith("data:") or len(encoded) > 16 * 1024 * 1024:
                    return self.send_json(400, {"error": "Arquivo inválido ou maior que 12 MiB."})
                mime = encoded[5:].split(";", 1)[0].lower()
                kind = "video" if mime.startswith("video/") else "image" if mime.startswith("image/") else ""
                if kind not in ("image", "video"):
                    return self.send_json(400, {"error": "Apenas imagens e vídeos são aceitos."})
                return self.send_json(201, {
                    "type": kind, "mimetype": mime,
                    "name": str(body.get("name", ""))[:180], "data": encoded,
                })
            if path == "/api/settings":
                raw_interval = body.get("send_interval_seconds")
                if isinstance(raw_interval, bool):
                    return self.send_json(400, {"error": "Informe um intervalo numérico de pelo menos 1 segundo."})
                try:
                    interval = float(raw_interval)
                except (TypeError, ValueError):
                    return self.send_json(400, {"error": "Informe um intervalo numérico de pelo menos 1 segundo."})
                if not math.isfinite(interval) or interval < 1:
                    return self.send_json(400, {"error": "O intervalo entre envios deve ser de pelo menos 1 segundo."})
                with connect_db() as db:
                    db.execute(
                        """INSERT INTO settings(key,value) VALUES('send_interval_seconds',?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                        (str(interval),),
                    )
                return self.send_json(200, {"send_interval_seconds": interval})
            if path == "/api/settings/access-key":
                if APP_API_KEY:
                    return self.send_json(409, {
                        "error": "A chave é gerenciada pela variável APP_API_KEY na inicialização do servidor."
                    })
                if body.get("clear") is True:
                    with connect_db() as db:
                        db.execute("DELETE FROM settings WHERE key='app_api_key'")
                    return self.send_json(200, {"api_key_configured": False})
                new_key = body.get("key")
                if not isinstance(new_key, str) or not 16 <= len(new_key) <= 4096:
                    return self.send_json(400, {"error": "A chave deve ter entre 16 e 4096 caracteres."})
                with connect_db() as db:
                    db.execute(
                        """INSERT INTO settings(key,value) VALUES('app_api_key',?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                        (new_key,),
                    )
                return self.send_json(200, {"api_key_configured": True})
            if path == "/api/campaigns":
                name, text = str(body.get("name", "")).strip(), str(body.get("text", "")).strip()
                if not name or (not text and not body.get("media")):
                    return self.send_json(400, {"error": "Informe nome e texto ou mídia para a campanha."})
                campaign_id = str(uuid.uuid4())
                with connect_db() as db:
                    db.execute(
                        """INSERT INTO campaigns(id,name,text,media,status,created_at)
                           VALUES (?,?,?,?, 'draft', ?)""",
                        (campaign_id, name[:160], text, json.dumps(body["media"]) if body.get("media") else "", now()),
                    )
                    contacts = db.execute(
                        """SELECT id, name, phone FROM contacts
                           WHERE opted_in=1 AND deleted_at='' AND whatsapp_status<>'unavailable'"""
                    ).fetchall()
                    db.executemany(
                        """INSERT INTO campaign_recipients(
                               id,campaign_id,contact_id,contact_name_snapshot,contact_phone_snapshot
                           ) VALUES (?,?,?,?,?)""",
                        [
                            (str(uuid.uuid4()), campaign_id, row["id"], row["name"], row["phone"])
                            for row in contacts
                        ],
                    )
                return self.send_json(201, {"id": campaign_id, "recipients": len(contacts)})
            if path.startswith("/api/campaigns/") and path.endswith("/duplicate"):
                source_id = path.split("/")[3]
                with connect_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    source = db.execute(
                        "SELECT name, text, media FROM campaigns WHERE id=?", (source_id,)
                    ).fetchone()
                    if not source:
                        return self.send_json(404, {"error": "Campanha não encontrada."})
                    campaign_id = str(uuid.uuid4())
                    db.execute(
                        """INSERT INTO campaigns(id,name,text,media,status,created_at)
                           VALUES (?,?,?,?, 'draft', ?)""",
                        (
                            campaign_id, (source["name"] + " (cópia)")[:160],
                            source["text"], source["media"], now(),
                        ),
                    )
                    recipients = db.execute(
                        """SELECT c.id, c.name, c.phone FROM campaign_recipients cr
                           JOIN contacts c ON c.id=cr.contact_id
                           WHERE cr.campaign_id=? AND c.opted_in=1 AND c.deleted_at=''
                                 AND c.whatsapp_status<>'unavailable'
                           ORDER BY c.name, c.phone""",
                        (source_id,),
                    ).fetchall()
                    db.executemany(
                        """INSERT INTO campaign_recipients(
                               id,campaign_id,contact_id,contact_name_snapshot,contact_phone_snapshot
                           ) VALUES (?,?,?,?,?)""",
                        [
                            (
                                str(uuid.uuid4()), campaign_id, row["id"], row["name"], row["phone"]
                            )
                            for row in recipients
                        ],
                    )
                record_activity("campanha", f"Criado rascunho a partir da campanha {source['name']}.")
                return self.send_json(201, {
                    "id": campaign_id, "name": (source["name"] + " (cópia)")[:160],
                    "text": source["text"], "media": source["media"],
                    "recipients": len(recipients),
                })
            if path.startswith("/api/campaigns/") and path.endswith("/resend"):
                campaign_id = path.split("/")[3]
                try:
                    resend = create_campaign_resend(campaign_id)
                except LookupError as error:
                    return self.send_json(404, {"error": str(error)})
                except ValueError as error:
                    return self.send_json(409, {"error": str(error)})
                threading.Thread(target=run_campaign, args=(resend["id"],), daemon=True).start()
                return self.send_json(202, resend)
            if path.startswith("/api/campaigns/") and path.endswith("/start"):
                campaign_id = path.split("/")[3]
                with connect_db() as db:
                    campaign = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
                    count = db.execute(
                        "SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id=? AND status='pending'",
                        (campaign_id,),
                    ).fetchone()[0]
                    if not campaign:
                        return self.send_json(404, {"error": "Campanha não encontrada."})
                    if campaign["status"] == "enviando":
                        return self.send_json(409, {"error": "A campanha já está em execução."})
                    if not count:
                        return self.send_json(400, {"error": "Não há contatos com consentimento para enviar."})
                    db.execute(
                        "UPDATE campaigns SET status='enviando', started_at=?, finished_at='' WHERE id=?",
                        (now(), campaign_id),
                    )
                threading.Thread(target=run_campaign, args=(campaign_id,), daemon=True).start()
                return self.send_json(202, {"ok": True, "recipients": count})
            if path == "/api/workflows":
                name = str(body.get("name", "")).strip()
                if not name:
                    return self.send_json(400, {"error": "Informe o nome do workflow."})
                graph = None
                if "graph" in body:
                    graph = validate_workflow_graph(body["graph"])
                    keyword, reply, media, webhook_url = graph_legacy_fields(graph)
                else:
                    keyword = str(body.get("keyword", "")).strip()
                    reply = str(body.get("reply_text", "")).strip()
                    webhook_url = str(body.get("webhook_url", "")).strip()
                    media = json.dumps(body["media"]) if body.get("media") else ""
                    if not keyword or (not reply and not media and not webhook_url):
                        return self.send_json(400, {"error": "Informe palavras-chave e uma resposta, mídia ou webhook."})
                    if webhook_url and urlparse(webhook_url).scheme not in ("http", "https"):
                        return self.send_json(400, {"error": "O webhook deve usar HTTP ou HTTPS."})
                workflow_id = str(uuid.uuid4())
                with connect_db() as db:
                    db.execute(
                        """INSERT INTO workflows
                           (id,name,keyword,reply_text,media,webhook_url,active,created_at,graph_json)
                           VALUES(?,?,?,?,?,?,1,?,?)""",
                        (workflow_id, name[:160], keyword, reply, media, webhook_url, now(),
                         json.dumps(graph, ensure_ascii=False) if graph else ""),
                    )
                return self.send_json(201, {"id": workflow_id})
            if path == "/api/uazapi/configure":
                server_url = str(body.get("server_url", "")).strip().rstrip("/")
                supplied_token = str(body.get("token", "")).strip()
                _, saved_token = get_uazapi_config()
                instance_token = supplied_token or saved_token
                public_base_url = str(body.get("public_base_url", "")).strip().rstrip("/")
                parsed_url = urlparse(server_url)
                if (
                    parsed_url.scheme not in ("http", "https")
                    or not parsed_url.netloc
                    or parsed_url.username
                    or parsed_url.password
                    or parsed_url.query
                    or parsed_url.fragment
                ):
                    return self.send_json(400, {
                        "error": "Informe um Server URL HTTP ou HTTPS válido, sem usuário, senha, parâmetros ou fragmento."
                    })
                if not instance_token or len(instance_token) > 4096:
                    return self.send_json(400, {"error": "Informe um token de instância válido."})
                if public_base_url:
                    public_url = urlparse(public_base_url)
                    if public_url.scheme != "https" or not public_url.netloc or public_url.username or public_url.password:
                        return self.send_json(400, {
                            "error": "A URL pública do webhook deve usar HTTPS e não pode incluir usuário ou senha."
                        })
                uazapi_request(
                    "/webhook", method="GET", server_url=server_url, token=instance_token
                )
                with connect_db() as db:
                    db.executemany(
                        """INSERT INTO settings(key,value) VALUES(?,?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                        [
                            ("uazapi_url", server_url), ("uazapi_token", instance_token),
                            ("public_base_url", public_base_url),
                        ],
                    )
                record_activity("integração", f"Instância Uazapi conectada em {server_url}.")
                return self.send_json(200, {
                    "configured": True, "server_url": server_url,
                    "message": "Conexão validada e configurações salvas."
                })
            if path == "/api/groups/list":
                return self.send_json(200, uazapi_request("/group/list", {
                    "limit": 100, "offset": 0, "noParticipants": True,
                }))
            if path == "/api/groups/info":
                jid = str(body.get("groupjid", ""))
                if not jid.endswith("@g.us"):
                    return self.send_json(400, {"error": "JID de grupo inválido."})
                return self.send_json(200, uazapi_request("/group/info", {"groupjid": jid}))
            if path == "/api/groups/sync-members":
                jid = str(body.get("groupjid", ""))
                if not jid.endswith("@g.us"):
                    return self.send_json(400, {"error": "JID de grupo inválido."})
                info = uazapi_request("/group/info", {"groupjid": jid})
                participants = info.get("Participants", info.get("participants", []))
                imported = []
                skipped_non_phone = 0
                for item in participants:
                    if isinstance(item, dict):
                        phone = phone_from_group_participant(item)
                        if not phone:
                            skipped_non_phone += 1
                            continue
                        name = str(
                            item.get("Name") or item.get("name") or
                            item.get("DisplayName") or item.get("displayName") or
                            item.get("Username") or item.get("username") or ""
                        )
                        if upsert_contact(phone, name, False, "grupo", jid):
                            imported.append({"name": name, "phone": phone})
                group_name = str(info.get("Name", info.get("name", "")) or jid)
                record_activity("grupos", f"Salvos {len(imported)} participantes do grupo {jid}.")
                return self.send_json(200, {
                    "saved": len(imported), "group_jid": jid, "group_name": group_name,
                    "skipped_non_phone": skipped_non_phone, "participants": imported,
                })
            if path == "/api/groups/add-participants":
                jid = str(body.get("groupjid", ""))
                participants = [clean_phone(p) for p in body.get("participants", [])]
                participants = [p for p in participants if 8 <= len(p) <= 15]
                if not jid.endswith("@g.us") or not participants:
                    return self.send_json(400, {"error": "Informe grupo e ao menos um telefone válido."})
                result = uazapi_request("/group/updateParticipants", {
                    "groupjid": jid, "action": "add", "participants": participants,
                })
                return self.send_json(200, result)
            if path == "/api/community/edit-groups":
                community = str(body.get("community", ""))
                action = body.get("action")
                groupjids = body.get("groupjids", [])
                if not community.endswith("@g.us") or action not in ("add", "remove") or not groupjids:
                    return self.send_json(400, {"error": "Informe comunidade, ação e grupos."})
                return self.send_json(200, uazapi_request("/community/editgroups", {
                    "community": community, "action": action, "groupjids": groupjids,
                }))
            if path == "/api/webhook/configure":
                public_base_url = get_public_base_url()
                if not public_base_url:
                    return self.send_json(400, {"error": "Informe e salve a URL pública HTTPS na configuração do Uazapi."})
                result = uazapi_request("/webhook", {
                    "url": public_base_url + "/webhooks/uazapi",
                    "events": ["messages"],
                    "excludeMessages": ["wasSentByApi", "isGroupYes"],
                    "enabled": True,
                })
                return self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json(400, {"error": f"Dados inválidos: {error}"})
        except UazapiRequestError as error:
            return self.send_json(error.status, {"error": str(error)})
        except Exception as error:
            return self.send_json(502, {"error": str(error)})
        self.send_json(404, {"error": "Rota não encontrada."})

    def process_webhook(self, event):
        try:
            handle_webhook(event)
        except Exception as error:
            record_activity("webhook", f"Falha ao processar evento: {error}")

    def do_PUT(self):
        if not self.require_auth():
            return
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith("/api/campaigns/") and not path.endswith("/recipients"):
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[1] == "campaigns":
                    campaign_id = parts[2]
                    body = self.read_json()
                    name = body.get("name")
                    text = body.get("text")
                    media = body.get("media")
                    if not isinstance(name, str) or not name.strip():
                        return self.send_json(400, {"error": "Informe o nome da campanha."})
                    if not isinstance(text, str):
                        return self.send_json(400, {"error": "Informe uma mensagem válida."})
                    text = text.strip()
                    if media is not None:
                        if (
                            not isinstance(media, dict)
                            or media.get("type") not in ("image", "video")
                            or not isinstance(media.get("data"), str)
                            or not media["data"].startswith("data:")
                            or len(media["data"]) > 16 * 1024 * 1024
                        ):
                            return self.send_json(400, {"error": "A mídia selecionada é inválida."})
                    if not text and not media:
                        return self.send_json(400, {"error": "Informe uma mensagem ou anexe uma imagem/vídeo."})
                    with connect_db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        campaign = db.execute(
                            "SELECT status FROM campaigns WHERE id=?", (campaign_id,)
                        ).fetchone()
                        if not campaign:
                            return self.send_json(404, {"error": "Campanha não encontrada."})
                        if campaign["status"] != "draft":
                            return self.send_json(409, {
                                "error": "Somente campanhas em rascunho podem ser editadas. Use a campanha como modelo para criar uma cópia."
                            })
                        db.execute(
                            "UPDATE campaigns SET name=?, text=?, media=? WHERE id=?",
                            (
                                name.strip()[:160], text,
                                json.dumps(media, ensure_ascii=False) if media else "",
                                campaign_id,
                            ),
                        )
                    return self.send_json(200, {"ok": True})
            if path.startswith("/api/campaigns/") and path.endswith("/recipients"):
                campaign_id = path.split("/")[3]
                body = self.read_json()
                contact_ids = body.get("contact_ids")
                if not isinstance(contact_ids, list) or any(
                    not isinstance(contact_id, str) or not contact_id
                    for contact_id in contact_ids
                ):
                    return self.send_json(400, {"error": "Informe uma lista válida de contatos."})
                contact_ids = list(dict.fromkeys(contact_ids))
                with connect_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    campaign = db.execute(
                        "SELECT status FROM campaigns WHERE id=?", (campaign_id,)
                    ).fetchone()
                    if not campaign:
                        return self.send_json(404, {"error": "Campanha não encontrada."})
                    if campaign["status"] != "draft":
                        return self.send_json(409, {
                            "error": "Os contatos só podem ser alterados enquanto a campanha estiver em rascunho."
                        })
                    eligible = db.execute(
                        """SELECT id, name, phone FROM contacts
                           WHERE opted_in=1 AND deleted_at='' AND whatsapp_status<>'unavailable'"""
                    ).fetchall()
                    eligible_by_id = {row["id"]: row for row in eligible}
                    if any(contact_id not in eligible_by_id for contact_id in contact_ids):
                        return self.send_json(400, {
                            "error": "A seleção inclui contatos sem consentimento ativo ou indisponíveis."
                        })
                    db.execute(
                        "DELETE FROM campaign_recipients WHERE campaign_id=?", (campaign_id,)
                    )
                    db.executemany(
                        """INSERT INTO campaign_recipients(
                               id,campaign_id,contact_id,contact_name_snapshot,contact_phone_snapshot
                           ) VALUES(?,?,?,?,?)""",
                        [
                            (
                                str(uuid.uuid4()), campaign_id, contact_id,
                                eligible_by_id[contact_id]["name"], eligible_by_id[contact_id]["phone"],
                            )
                            for contact_id in contact_ids
                        ],
                    )
                return self.send_json(200, {"recipients": len(contact_ids)})
            if path.startswith("/api/contact-groups/"):
                group_id = path.rsplit("/", 1)[-1]
                body = self.read_json()
                name = body.get("name")
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
                    return self.send_json(400, {"error": "Informe um nome de grupo com até 80 caracteres."})
                try:
                    with connect_db() as db:
                        cursor = db.execute(
                            "UPDATE contact_groups SET name=? WHERE id=?",
                            (name.strip(), group_id),
                        )
                        if not cursor.rowcount:
                            return self.send_json(404, {"error": "Grupo de contatos não encontrado."})
                except sqlite3.IntegrityError:
                    return self.send_json(409, {"error": "Já existe um grupo com esse nome."})
                return self.send_json(200, {"ok": True})
            if path.startswith("/api/contacts/"):
                contact_id = path.rsplit("/", 1)[-1]
                body = self.read_json()
                name = str(body.get("name", "")).strip()[:160]
                phone = clean_phone(body.get("phone"))
                if not 8 <= len(phone) <= 15:
                    return self.send_json(400, {"error": "Informe um telefone com código do país."})
                with connect_db() as db:
                    group_ids = (
                        validated_contact_group_ids(db, body["group_ids"])
                        if "group_ids" in body else None
                    )
                    cursor = db.execute(
                        """UPDATE contacts SET name=?, phone=?, opted_in=?,
                           whatsapp_status=CASE WHEN phone<>? THEN 'unknown' ELSE whatsapp_status END,
                           updated_at=?
                           WHERE id=? AND deleted_at=''""",
                        (name, phone, int(bool(body.get("opted_in"))), phone, now(), contact_id),
                    )
                    if not cursor.rowcount:
                        return self.send_json(404, {"error": "Contato não encontrado."})
                    if group_ids is not None:
                        set_contact_groups(db, contact_id, group_ids)
                return self.send_json(200, {"ok": True})
            if not path.startswith("/api/workflows/"):
                return self.send_json(404, {"error": "Rota não encontrada."})
            workflow_id = path.rsplit("/", 1)[-1]
            body = self.read_json()
            name = str(body.get("name", "")).strip()
            graph = validate_workflow_graph(body.get("graph"))
            if not name:
                return self.send_json(400, {"error": "Informe o nome do workflow."})
            keyword, reply, media, webhook_url = graph_legacy_fields(graph)
            with connect_db() as db:
                cursor = db.execute(
                    """UPDATE workflows SET name=?, keyword=?, reply_text=?, media=?,
                       webhook_url=?, graph_json=? WHERE id=?""",
                    (name[:160], keyword, reply, media, webhook_url,
                     json.dumps(graph, ensure_ascii=False), workflow_id),
                )
                if not cursor.rowcount:
                    return self.send_json(404, {"error": "Workflow não encontrado."})
            return self.send_json(200, {"ok": True})
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json(400, {"error": f"Dados inválidos: {error}"})
        except sqlite3.IntegrityError:
            return self.send_json(409, {"error": "Já existe um contato com esse telefone."})
        except Exception as error:
            return self.send_json(500, {"error": str(error)})

    def do_PATCH(self):
        if not self.require_auth():
            return
        path = unquote(urlparse(self.path).path)
        try:
            body = self.read_json()
            if path.startswith("/api/workflows/"):
                workflow_id = path.rsplit("/", 1)[-1]
                with connect_db() as db:
                    row = db.execute("SELECT active FROM workflows WHERE id=?", (workflow_id,)).fetchone()
                    if not row:
                        return self.send_json(404, {"error": "Fluxo não encontrado."})
                    db.execute("UPDATE workflows SET active=? WHERE id=?", (0 if row["active"] else 1, workflow_id))
                return self.send_json(200, {"ok": True})
            if path.startswith("/api/contacts/"):
                contact_id = path.rsplit("/", 1)[-1]
                with connect_db() as db:
                    cursor = db.execute(
                        "UPDATE contacts SET opted_in=?, updated_at=? WHERE id=? AND deleted_at=''",
                        (int(bool(body.get("opted_in"))), now(), contact_id),
                    )
                    if not cursor.rowcount:
                        return self.send_json(404, {"error": "Contato não encontrado."})
                return self.send_json(200, {"ok": True})
        except Exception as error:
            return self.send_json(400, {"error": str(error)})
        self.send_json(404, {"error": "Rota não encontrada."})

    def do_DELETE(self):
        if not self.require_auth():
            return
        path = unquote(urlparse(self.path).path)
        if path.startswith("/api/contact-groups/"):
            group_id = path.rsplit("/", 1)[-1]
            try:
                with connect_db() as db:
                    cursor = db.execute("DELETE FROM contact_groups WHERE id=?", (group_id,))
                    if not cursor.rowcount:
                        return self.send_json(404, {"error": "Grupo de contatos não encontrado."})
                record_activity("contatos", "Grupo de contatos excluído.")
                return self.send_json(200, {"ok": True})
            except Exception as error:
                return self.send_json(500, {"error": str(error)})
        if not path.startswith("/api/contacts/"):
            return self.send_json(404, {"error": "Rota não encontrada."})
        contact_id = path.rsplit("/", 1)[-1]
        stamp = now()
        try:
            with connect_db() as db:
                contact = db.execute(
                    "SELECT id FROM contacts WHERE id=? AND deleted_at=''", (contact_id,)
                ).fetchone()
                if not contact:
                    return self.send_json(404, {"error": "Contato não encontrado."})
                db.execute(
                    """UPDATE campaign_recipients
                       SET contact_name_snapshot='', contact_phone_snapshot=''
                       WHERE contact_id=?""",
                    (contact_id,),
                )
                cursor = db.execute(
                    """UPDATE contacts SET phone=?, name='', opted_in=0, source='excluido',
                       group_jid='', deleted_at=?, updated_at=? WHERE id=? AND deleted_at=''""",
                    (f"removido-{contact_id}", stamp, stamp, contact_id),
                )
                if not cursor.rowcount:
                    return self.send_json(404, {"error": "Contato não encontrado."})
                db.execute(
                    "DELETE FROM contact_group_memberships WHERE contact_id=?", (contact_id,)
                )
            record_activity("contatos", "Contato excluído e dados pessoais removidos.")
            return self.send_json(200, {"ok": True})
        except Exception as error:
            return self.send_json(500, {"error": str(error)})


if __name__ == "__main__":
    initialize_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if host not in ("127.0.0.1", "localhost", "::1") and not get_app_api_key():
        raise SystemExit("Configure APP_API_KEY antes de expor o servidor em uma interface de rede.")
    print(f"Disparos em http://{host}:{port} (Ctrl+C para encerrar)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
