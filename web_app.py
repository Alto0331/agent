# -*- coding: utf-8 -*-
"""
web_app.py — Interface web mobile-first pour l'agent (V.3 étape 1).

Serveur HTTP autonome en stdlib pure (aucune dépendance), lancé en thread par
agent_server.py. Accessible en local ou via VPN sur le port 8765.
- Page unique mobile-first (PWA-light) : Chat, Problèmes, Réglages
- API JSON : login PIN, statut, mission, logs, réponse humaine, stop, réglages
- Ne remplace PAS app_client.py (qui continue de fonctionner sur 65432)

Lancement : agent_server.py appelle start_server(...) avec les hooks nécessaires.
"""

import json
import os
import hashlib
import secrets
import threading
import time
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WEB_PORT = 8765
LOG_BUFFER_MAX = 3000


class WebHub:
    """Cœur d'état partagé : logs, instance courante, sessions.
    Les tokens sont persistés sur disque : ils survivent aux redémarrages du serveur."""

    TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_tokens.json")

    def __init__(self):
        self.logs = []            # [{"id": int, "ts": float, "text": str, "tag": str}]
        self.log_id = 0
        self.lock = threading.Lock()
        self.tokens = set()       # tokens de session valides
        self._load_tokens()
        self.current_instance = None
        self.hooks = {}           # rempli par start_server
        self.sessions = {}        # {id: {"title": str, "logs": [{"id","ts","text","tag"}], "log_id": int}}
        self.next_session = 1
        self.active_session = 0   # 0 = nouvelle session
        self._load_sessions()

    SESSIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_sessions.json")

    def _load_sessions(self):
        try:
            if os.path.exists(self.SESSIONS_FILE):
                with open(self.SESSIONS_FILE, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                self.sessions = data.get("sessions", {})
                self.next_session = data.get("next", 1)
        except Exception:
            self.sessions = {}

    def save_sessions(self):
        try:
            with open(self.SESSIONS_FILE, "w", encoding="utf-8") as f:
                json.dump({"sessions": self.sessions, "next": self.next_session}, f, ensure_ascii=False)
        except Exception:
            pass

    def session_log(self, sid: int, text: str, tag: str):
        if sid not in self.sessions:
            self.sessions[sid] = {"title": text.strip()[:60] or "Session", "logs": [], "log_id": 0}
        s = self.sessions[sid]
        s["log_id"] = s.get("log_id", 0) + 1
        s["logs"].append({"id": s["log_id"], "ts": time.time(), "text": str(text), "tag": tag})
        if len(s["logs"]) > 800:
            s["logs"] = s["logs"][-800:]
        self.save_sessions()

    def _load_tokens(self):
        try:
            if os.path.exists(self.TOKENS_FILE):
                with open(self.TOKENS_FILE, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                self.tokens = set(t for t in data.get("tokens", []) if isinstance(t, str))
        except Exception:
            self.tokens = set()

    def _save_tokens(self):
        try:
            with open(self.TOKENS_FILE, "w", encoding="utf-8") as f:
                json.dump({"tokens": list(self.tokens)}, f)
        except Exception:
            pass

    def append_log(self, text: str, tag: str = "system"):
        with self.lock:
            self.log_id += 1
            self.logs.append({"id": self.log_id, "ts": time.time(), "text": str(text), "tag": tag})
            if len(self.logs) > LOG_BUFFER_MAX:
                self.logs = self.logs[-LOG_BUFFER_MAX:]
            if self.active_session:
                self.session_log(self.active_session, text, tag)

    def logs_since(self, since_id: int):
        with self.lock:
            return [e for e in self.logs if e["id"] > since_id]

    def new_token(self) -> str:
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens.add(tok)
        self._save_tokens()
        return tok

    def valid_token(self, tok: str) -> bool:
        with self.lock:
            return tok in self.tokens

    def revoke_all(self):
        """Révoque toutes les sessions (appelé quand le PIN change)."""
        with self.lock:
            self.tokens = set()
        self._save_tokens()


HUB = WebHub()


class WebConn:
    """Faux socket : capte les logs de RemoteAppInstance vers le hub web."""

    def __init__(self):
        pass

    def sendall(self, data: bytes):
        try:
            line = data.decode("utf-8").strip()
            if not line:
                return
            payload = json.loads(line)
            HUB.append_log(payload.get("text", ""), payload.get("tag", "system"))
        except Exception:
            pass

    def recv(self, *a, **k):
        return None

    def close(self):
        pass


def get_pin_hash(creds: dict) -> str:
    return hashlib.sha256((creds.get("web_pin") or "").strip().encode()).hexdigest()


def ensure_pin(creds_fn, save_fn) -> str:
    """Génère un PIN 6 chiffres au premier lancement et le sauvegarde.
    Vérifie la persistance : si la sauvegarde échoue, retente puis alerte clairement."""
    creds = creds_fn()
    if not (creds.get("web_pin") or "").strip():
        pin = str(secrets.randbelow(900000) + 100000)
        save_fn({"web_pin": pin})
        # Vérifie que le PIN est réellement persisté avant de l'annoncer
        reloaded = creds_fn()
        if (reloaded.get("web_pin") or "").strip() != pin:
            save_fn({"web_pin": pin})
            reloaded = creds_fn()
        if (reloaded.get("web_pin") or "").strip() == pin:
            print(f"[WebApp] 🔑 PIN d'accès initial généré : {pin} (modifiable dans les Réglages)")
            return pin
        print("[WebApp] ⚠️ ERREUR : le PIN n'a pas pu être sauvegardé dans credentials.json !")
        print(f"[WebApp] ⚠️ PIN temporaire pour cette session uniquement : {pin}")
        return pin
    return creds.get("web_pin", "").strip()


def open_firewall_port(port=WEB_PORT):
    """Crée la règle pare-feu si elle n'existe pas VRAIMENT.
    Note : 'netsh show rule' renvoie souvent code 0 même sans correspondance,
    on vérifie donc la sortie réelle, et on tente l'ajout avec profile=any
    (couvre Domaine/Privé/Public, nécessaire pour l'interface Tailscale)."""
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=AgentWebApp"],
            capture_output=True, text=True
        )
        already = "AgentWebApp" in (r.stdout or "")
    except Exception:
        already = False
    if already:
        return
    for profile in ("any",):
        try:
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 "name=AgentWebApp", "dir=in", "action=allow",
                 "protocol=TCP", f"localport={port}", f"profile={profile}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass
    print(f"[WebApp] 🛡️ Règle pare-feu 'AgentWebApp' créée (TCP {port}, tous profils) — nécessaire pour l'accès Tailscale/VPN.")


# L'interface web est un fichier HTML independant (web/index.html) :
# plus aucune chaine Python ni double echappement — le HTML/JS est servi octet pour octet.
PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
PAGE = ""
try:
    with open(PAGE_FILE, "r", encoding="utf-8-sig") as _f:
        PAGE = _f.read()
except Exception as _e:
    PAGE = "<html><body style='background:#0b0e14;color:#e6edf3;font-family:sans-serif;text-align:center;padding-top:15vh'><h2>Interface web indisponible</h2><p>Le fichier web/index.html est introuvable ou illisible : " + str(_e) + "</p></body></html>"
    print(f"[WebApp] \u26a0\ufe0f web/index.html introuvable : {_e}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        tok = self.headers.get("X-Auth", "")
        if not tok:
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                if part.strip().startswith("wa_tok="):
                    tok = part.strip().split("=", 1)[1]
                    break
        return HUB.valid_token(tok)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._auth_ok():
            return self._json({"ok": False, "error": "non authentifié"}, 401)

        if u.path == "/api/status":
            inst = HUB.current_instance
            running = bool(inst and getattr(inst, "agent_running", False))
            awaiting = bool(inst and not getattr(inst, "wait_event", threading.Event()).is_set() and getattr(inst, "_awaiting", False))
            creds = HUB.hooks["load_credentials"]()
            mode = "local" if creds.get("use_ollama") else creds.get("engine_mode", "terra")
            label = {"terra": "GPT-6.1 Terra", "lite": "Claude Haiku 5.5", "local": "Ollama local"}.get(mode, mode)
            if HUB.hooks.get("has_mistral")():
                label = "Cerveau Mistral → " + label
            return self._json({"ok": True, "running": running, "awaiting": awaiting,
                               "engine_label": label, "queue": HUB.hooks.get("queue_len", lambda: 0)()})

        if u.path == "/api/logs":
            q = parse_qs(u.query)
            since = int(q.get("since", ["0"])[0])
            sid = int(q.get("session", ["0"])[0])
            if sid:
                s = HUB.sessions.get(sid, {})
                entries = [e for e in s.get("logs", []) if e["id"] > since]
                return self._json({"ok": True, "entries": entries})
            return self._json({"ok": True, "entries": HUB.logs_since(since)})

        if u.path == "/api/settings":
            creds = HUB.hooks["load_credentials"]()
            trusted = HUB.hooks["load_trusted_senders"]()
            return self._json({
                "ok": True,
                "engine_mode": creds.get("engine_mode", "terra"),
                "use_ollama": bool(creds.get("use_ollama")),
                "ollama_url": creds.get("ollama_url", "http://localhost:11434"),
                "ollama_model": creds.get("ollama_model", "qwen2.5-vl:7b"),
                "has_mistral": bool(HUB.hooks.get("has_mistral")()),
                "has_openrouter": bool(HUB.hooks.get("has_openrouter")()),
                "terra_email": creds.get("terra_mail_address", ""),
                "auth_sender": creds.get("terra_authorized_sender", ""),
                "main_email": creds.get("main_account_email", ""),
                "trusted_senders": trusted,
                "proxmox_enabled": bool(creds.get("proxmox_enabled")),
                "proxmox_url": creds.get("proxmox_url", ""),
                "proxmox_token": creds.get("proxmox_token", ""),
                "has_proxmox_secret": bool(creds.get("proxmox_secret")),
                "ntfy_enabled": bool(creds.get("ntfy_enabled")),
                "ntfy_url": creds.get("ntfy_url", "https://ntfy.sh"),
                "ntfy_topic": creds.get("ntfy_topic", ""),
                "announce_ip": creds.get("announce_ip", ""),
                "watched_services": json.dumps(creds.get("watched_services", []), ensure_ascii=False, indent=2),
                "watchdog_interval_min": creds.get("watchdog_interval_min", 5),
                "alert_email": creds.get("alert_email", ""),
                "auto_update_enabled": bool(creds.get("auto_update_enabled")),
                "auto_update_time": creds.get("auto_update_time", "00:00"),
                "update_blacklist": creds.get("update_blacklist", []),
            })

        if u.path == "/api/sessions":
            sessions = []
            for sid in sorted(HUB.sessions.keys(), reverse=True):
                s = HUB.sessions[sid]
                sessions.append({"id": sid, "title": s.get("title", "Session")[:60]})
            return self._json({"ok": True, "sessions": sessions,
                               "current": HUB.active_session,
                               "title": HUB.sessions.get(HUB.active_session, {}).get("title", "")})

        if u.path == "/api/network":
            creds = HUB.hooks["load_credentials"]()
            ips = HUB.hooks.get("network_ips_fn", lambda: [])()
            return self._json({"ok": True,
                               "host": self.headers.get("Host", ""),
                               "announce_ip": creds.get("announce_ip", ""),
                               "ips": ips})

        if u.path == "/api/proxmox_data":
            try:
                import proxmox_monitor
                creds = HUB.hooks["load_credentials"]()
                data = proxmox_monitor.get_dashboard_data(creds)
                data["services"] = HUB.hooks.get("watchdog_services_fn", lambda: [])()
                return self._json(data)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if u.path == "/api/missions":
            html = HUB.hooks.get("missions_html", lambda: "")()
            proxmox_html = HUB.hooks.get("proxmox_report_html", lambda: "")()
            return self._json({"ok": True, "html": html, "proxmox": proxmox_html})

        self._json({"ok": False, "error": "route inconnue"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            data = {}

        if u.path == "/api/login":
            creds = HUB.hooks["load_credentials"]()
            supplied = hashlib.sha256(str(data.get("pin", "")).strip().encode()).hexdigest()
            if supplied == get_pin_hash(creds):
                return self._json({"ok": True, "token": HUB.new_token()})
            print(f"[WebApp] ⚠️ Tentative de login échouée (PIN incorrect). PIN en base : {'défini' if (creds.get('web_pin') or '').strip() else 'ABSENT'}")
            return self._json({"ok": False}, 403)

        if not self._auth_ok():
            return self._json({"ok": False, "error": "non authentifié"}, 401)

        if u.path == "/api/mission":
            text = str(data.get("text", "")).strip()
            if not text:
                return self._json({"ok": False, "error": "mission vide"})
            inst = HUB.current_instance
            busy = bool(inst and getattr(inst, "agent_running", False))
            if busy:
                return self._json({"ok": False, "error": "une mission est déjà en cours"})
            # session : réutilise celle envoyée ou en crée une nouvelle
            sid = int(data.get("session") or 0)
            if sid not in HUB.sessions:
                sid = HUB.next_session
                HUB.next_session += 1
                HUB.sessions[sid] = {"title": text.strip()[:60] or "Session", "logs": [], "log_id": 0}
            HUB.active_session = sid
            HUB.save_sessions()
            web_inst = HUB.hooks["make_instance"](conn=WebConn())
            web_inst._awaiting = False
            HUB.current_instance = web_inst
            web_inst.log(f"💬 [WEB] Mission reçue : {text[:150]}\n", "user")
            HUB.hooks["start_task"](text, web_inst, on_complete=HUB.hooks.get("on_web_complete"))
            return self._json({"ok": True, "session": sid})

        if u.path == "/api/session_logs":
            sid = int(data.get("id") or 0)
            s = HUB.sessions.get(sid)
            if not s:
                return self._json({"ok": False, "error": "session introuvable"})
            return self._json({"ok": True, "entries": s.get("logs", [])})

        if u.path == "/api/session_del":
            sid = int(data.get("id") or 0)
            if sid in HUB.sessions:
                del HUB.sessions[sid]
                if HUB.active_session == sid:
                    HUB.active_session = 0
                HUB.save_sessions()
            return self._json({"ok": True})

        if u.path == "/api/answer":
            inst = HUB.current_instance
            if not inst:
                return self._json({"ok": False, "error": "aucune mission en cours"})
            inst.human_response = str(data.get("text", "")).strip()
            inst.wait_event.set()
            inst._awaiting = False
            return self._json({"ok": True})

        if u.path == "/api/stop":
            inst = HUB.current_instance
            if inst:
                inst.stop_requested = True
                inst.wait_event.set()
                inst._awaiting = False
            return self._json({"ok": True})

        if u.path == "/api/sync":
            allowed = [
                "engine_mode", "use_ollama", "ollama_url", "ollama_model",
                "openrouter_key", "mistral_api_key",
                "terra_mail_address", "terra_mail_app_password", "terra_authorized_sender",
                "main_account_email", "main_account_password",
                "proxmox_enabled", "proxmox_url", "proxmox_token", "proxmox_secret",
                "ntfy_enabled", "ntfy_url", "ntfy_topic", "announce_ip",
                "watched_services", "watchdog_interval_min", "alert_email",
                "auto_update_enabled", "auto_update_time", "update_blacklist",
            ]
            payload = {k: v for k, v in data.items() if k in allowed and v != ""}
            try:
                HUB.hooks["apply_credentials_sync"](payload, HUB.current_instance)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if u.path == "/api/proxmox_check":
            try:
                import proxmox_monitor
                creds = HUB.hooks["load_credentials"]()
                report, issues = proxmox_monitor.check_and_report(creds, notify_ntfy=True)
                for s in HUB.hooks.get("watchdog_services_fn", lambda: [])():
                    icon = "✅" if s.get("status") == "up" else "❌"
                    report += f"\n{icon} Service {s.get('name', '?')} : {s.get('status', '?')}"
                HUB.append_log("🖥️ [Proxmox — vérification manuelle]\n" + report + "\n", "system")
                return self._json({"ok": True, "report": report, "issues_count": len(issues)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if u.path == "/api/vault_save":
            try:
                import vault
                creds = HUB.hooks["load_credentials"]()
                confirm = vault.send_vault_via_gmail(creds, data.get("master_password", ""),
                                                     trusted_senders=HUB.hooks["load_trusted_senders"]())
                HUB.append_log("🔐 [Coffre] " + confirm + "\n", "system")
                return self._json({"ok": True, "message": confirm})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if u.path == "/api/vault_restore":
            try:
                import vault
                creds = HUB.hooks["load_credentials"]()
                confirm, fields = vault.restore_vault_via_gmail(creds, data.get("master_password", ""))
                if fields:
                    HUB.hooks["save_credentials"](fields)
                    if "trusted_senders" in fields:
                        HUB.hooks["save_trusted_senders"](fields["trusted_senders"])
                HUB.append_log("🔐 [Coffre] " + confirm + "\n", "system")
                return self._json({"ok": True, "message": confirm})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if u.path == "/api/trust":
            email = str(data.get("email", "")).strip().lower()
            add = bool(data.get("add", True))
            trusted = HUB.hooks["load_trusted_senders"]()
            if add and "@" in email and email not in trusted:
                trusted.append(email)
            elif not add and email in trusted:
                trusted.remove(email)
            HUB.hooks["save_trusted_senders"](trusted)
            return self._json({"ok": True, "trusted_senders": trusted})

        if u.path == "/api/pin":
            new_pin = str(data.get("pin", "")).strip()
            if len(new_pin) == 6 and new_pin.isdigit():
                HUB.hooks["save_credentials"]({"web_pin": new_pin})
                HUB.revoke_all()
                return self._json({"ok": True, "reconnected_token": HUB.new_token()})
            return self._json({"ok": False, "error": "PIN invalide (6 chiffres)"})

        self._json({"ok": False, "error": "route inconnue"}, 404)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server(start_task, make_instance, load_credentials, save_credentials,
                 load_trusted_senders, save_trusted_senders,
                 has_mistral_fn=None, has_openrouter_fn=None, queue_len_fn=None,
                 missions_html_fn=None, on_web_complete=None, apply_credentials_sync=None,
                 proxmox_report_html_fn=None, network_ips_fn=None, watchdog_services_fn=None):
    """Lancé par agent_server.py dans un thread daemon."""
    HUB.hooks = {
        "start_task": start_task,
        "make_instance": make_instance,
        "apply_credentials_sync": apply_credentials_sync or save_credentials,
        "load_credentials": load_credentials,
        "save_credentials": save_credentials,
        "load_trusted_senders": load_trusted_senders,
        "save_trusted_senders": save_trusted_senders,
        "has_mistral": has_mistral_fn or (lambda: False),
        "has_openrouter": has_openrouter_fn or (lambda: False),
        "queue_len": queue_len_fn or (lambda: 0),
        "missions_html": missions_html_fn or (lambda: ""),
        "proxmox_report_html": proxmox_report_html_fn or (lambda: ""),
        "network_ips_fn": network_ips_fn or (lambda: []),
        "watchdog_services_fn": watchdog_services_fn or (lambda: []),
        "on_web_complete": on_web_complete,
    }
    try:
        pin = ensure_pin(load_credentials, save_credentials)
        open_firewall_port(WEB_PORT)
        server = WebServer(("0.0.0.0", WEB_PORT), Handler)
        print(f"[WebApp] 🌐 Interface web mobile active sur le port {WEB_PORT} (accès local/VPN).")
        print(f"[WebApp] 🔑 PIN d'accès actuel : {pin} (modifiable dans l'app web, Réglages, ou --reset-pin pour le régénérer)")
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except Exception as e:
        print(f"[WebApp] ⚠️ Interface web non démarrée : {e}")
