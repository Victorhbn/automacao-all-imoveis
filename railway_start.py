import os
from http.server import ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import app


class RailwayHandler(app.Handler):
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/":
            try:
                html = (app.WEB_ROOT / "index.html").read_text(encoding="utf-8")
                script = (app.WEB_ROOT / "bulk-consent.js").read_text(encoding="utf-8")
                html = html.replace("</body>", f"<script>\n{script}\n</script>\n</body>", 1)
                content = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except OSError:
                self.send_json(500, {"error": "Não foi possível carregar a interface."})
            return
        return super().do_GET()

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path != "/api/contacts/consent-bulk":
            return super().do_POST()
        if not self.require_auth():
            return
        try:
            body = self.read_json()
            contact_ids = body.get("contact_ids")
            opted_in = body.get("opted_in")
            if not isinstance(contact_ids, list) or not contact_ids:
                return self.send_json(400, {"error": "Selecione ao menos um contato."})
            if len(contact_ids) > 500 or any(
                not isinstance(contact_id, str) or not contact_id
                for contact_id in contact_ids
            ):
                return self.send_json(400, {"error": "A seleção deve conter até 500 contatos válidos."})
            if not isinstance(opted_in, bool):
                return self.send_json(400, {"error": "Informe uma ação válida para o consentimento."})
            contact_ids = list(dict.fromkeys(contact_ids))
            placeholders = ",".join("?" for _ in contact_ids)
            with app.connect_db() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute(
                    f"""UPDATE contacts SET opted_in=?, updated_at=?
                        WHERE deleted_at='' AND id IN ({placeholders})""",
                    [int(opted_in), app.now(), *contact_ids],
                )
                updated = cursor.rowcount
            app.record_activity(
                "consentimento",
                f"Consentimento de campanhas {'ativado' if opted_in else 'removido'} em massa para {updated} contatos.",
            )
            return self.send_json(200, {"updated": updated})
        except (ValueError, app.json.JSONDecodeError) as error:
            return self.send_json(400, {"error": f"Dados inválidos: {error}"})
        except Exception as error:
            return self.send_json(500, {"error": str(error)})


def main():
    app.initialize_db()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    if host not in ("127.0.0.1", "localhost", "::1") and not app.get_app_api_key():
        raise SystemExit("Configure APP_API_KEY antes de expor o servidor em uma interface de rede.")
    print(f"Disparos em http://{host}:{port} (Ctrl+C para encerrar)", flush=True)
    ThreadingHTTPServer((host, port), RailwayHandler).serve_forever()


if __name__ == "__main__":
    main()
