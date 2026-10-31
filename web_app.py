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
    """Cœur d'état partagé : logs, instance courante, sessions."""

    def __init__(self):
        self.logs = []            # [{"id": int, "ts": float, "text": str, "tag": str}]
        self.log_id = 0
        self.lock = threading.Lock()
        self.tokens = set()       # tokens de session valides
        self.current_instance = None
        self.hooks = {}           # rempli par start_server

    def append_log(self, text: str, tag: str = "system"):
        with self.lock:
            self.log_id += 1
            self.logs.append({"id": self.log_id, "ts": time.time(), "text": str(text), "tag": tag})
            if len(self.logs) > LOG_BUFFER_MAX:
                self.logs = self.logs[-LOG_BUFFER_MAX:]

    def logs_since(self, since_id: int):
        with self.lock:
            return [e for e in self.logs if e["id"] > since_id]

    def new_token(self) -> str:
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens.add(tok)
        return tok

    def valid_token(self, tok: str) -> bool:
        with self.lock:
            return tok in self.tokens


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
    """Génère un PIN 6 chiffres au premier lancement et le sauvegarde."""
    creds = creds_fn()
    if not (creds.get("web_pin") or "").strip():
        pin = str(secrets.randbelow(900000) + 100000)
        save_fn({"web_pin": pin})
        print(f"[WebApp] 🔑 PIN d'accès initial généré : {pin} (modifiable dans les Réglages)")
        return pin
    return creds.get("web_pin", "").strip()


def open_firewall_port(port=WEB_PORT):
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=AgentWebApp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )
    except Exception:
        try:
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 "name=AgentWebApp", "dir=in", "action=allow",
                 "protocol=TCP", f"localport={port}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass


PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">
<title>Agent VM</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--acc:#38bdf8;--ok:#4ade80;--err:#f87171;--txt:#e2e8f0;--mut:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box;font-family:'Segoe UI',system-ui,sans-serif}
body{background:var(--bg);color:var(--txt);min-height:100vh;padding-bottom:70px}
header{background:var(--card);padding:14px 16px;position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #334155}
header h1{font-size:17px}.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px}
main{max-width:760px;margin:0 auto;padding:14px}
nav{position:fixed;bottom:0;left:0;right:0;background:var(--card);display:flex;border-top:1px solid #334155;z-index:10}
nav button{flex:1;background:none;border:none;color:var(--mut);padding:12px 4px;font-size:12px;cursor:pointer}
nav button.on{color:var(--acc);border-top:2px solid var(--acc)}
.card{background:var(--card);border-radius:12px;padding:14px;margin-bottom:12px}
.tag-sys{color:var(--mut)}.tag-agent{color:var(--acc)}.tag-user{color:var(--ok)}.tag-error{color:var(--err)}
#logs{font-size:13px;line-height:1.55;word-wrap:break-word;white-space:pre-wrap}
.log-line{padding:3px 0;border-bottom:1px solid #293548}
textarea,.txt{width:100%;background:#0b1220;color:var(--txt);border:1px solid #334155;border-radius:10px;padding:12px;font-size:15px}
textarea{min-height:90px;resize:vertical}
.btn{background:var(--acc);color:#04121f;border:none;border-radius:10px;padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer;width:100%}
.btn.sec{background:#334155;color:var(--txt)}
.btn:disabled{opacity:.5}
input.txt{margin:8px 0}
label{font-size:12px;color:var(--mut);display:block;margin-top:10px}
.pill{display:inline-block;background:#334155;border-radius:999px;padding:6px 12px;margin:4px;font-size:13px}
.pill button{background:none;border:none;color:var(--err);cursor:pointer;margin-left:6px}
h2{font-size:15px;margin-bottom:10px}
#login{max-width:380px;margin:14vh auto 0}
.mut{color:var(--mut);font-size:12px}
</style>
</head>
<body>
<div id="login" class="card">
  <h2>🔐 Accès Agent VM</h2>
  <input id="pin" class="txt" type="password" inputmode="numeric" placeholder="PIN à 6 chiffres">
  <button class="btn" onclick="login()">Se connecter</button>
  <p class="mut" id="loginErr" style="color:var(--err);margin-top:8px"></p>
</div>
<div id="app" style="display:none">
<header><h1><span class="dot" id="dot"></span>Agent VM</h1><span class="mut" id="engineLbl"></span></header>
<main>
  <section id="tab-chat">
    <div class="card" id="logsCard"><div id="logs"></div></div>
    <div class="card">
      <textarea id="mission" placeholder="Ta mission pour l'agent... (ex: trouve 5 entreprises de Poitiers...)"></textarea>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn" id="btnSend" onclick="sendMission()">🚀 Lancer</button>
        <button class="btn sec" id="btnStop" onclick="stopMission()">⏹ Stop</button>
      </div>
      <div id="answerBox" style="display:none;margin-top:10px">
        <textarea id="answer" placeholder="Ta réponse à l'agent..."></textarea>
        <button class="btn" style="margin-top:8px" onclick="sendAnswer()">▶️ Répondre</button>
      </div>
    </div>
  </section>
  <section id="tab-problems" style="display:none">
    <div class="card"><h2>🧭 Missions récentes</h2><div id="missions" class="mut">Chargement...</div></div>
    <div class="card"><h2>🛠️ Infrastructure</h2>
      <p class="mut">Surveillance Proxmox/RMM : disponible dans une prochaine étape (V.3.3). Les incidents apparaîtront ici avec bouton d'approbation.</p></div>
  </section>
  <section id="tab-settings" style="display:none">
    <div class="card"><h2>⚙️ Moteur</h2><div id="engineBox">Chargement...</div></div>
    <div class="card"><h2>📧 Expéditeurs de confiance</h2>
      <input id="newSender" class="txt" placeholder="email@a-ajouter.com">
      <button class="btn sec" onclick="addSender()">➕ Ajouter</button>
      <div id="senders" style="margin-top:10px"></div></div>
    <div class="card"><h2>🔑 PIN d'accès</h2>
      <input id="newPin" class="txt" placeholder="Nouveau PIN (6 chiffres)">
      <button class="btn sec" onclick="changePin()">Changer le PIN</button></div>
  </section>
</main>
<nav>
  <button id="n-chat" class="on" onclick="tab('chat')">💬 Chat</button>
  <button id="n-problems" onclick="tab('problems')">🚨 Problèmes</button>
  <button id="n-settings" onclick="tab('settings')">⚙️ Réglages</button>
</nav>
</div>
<script>
let TOKEN=localStorage.getItem('wa_tok')||'';
let lastId=0;
const api=(path,data)=>fetch('/api/'+path,{method:data?'POST':'GET',
  headers:{'Content-Type':'application/json','X-Auth':TOKEN},
  body:data?JSON.stringify(data):undefined}).then(r=>r.json());
async function login(){
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pin:document.getElementById('pin').value})}).then(r=>r.json());
  if(r.ok){TOKEN=r.token;localStorage.setItem('wa_tok',TOKEN);location.reload();}
  else{document.getElementById('loginErr').textContent='PIN incorrect';}
}
document.getElementById('pin').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
function tab(t){for(const x of['chat','problems','settings']){
  document.getElementById('tab-'+x).style.display=x===t?'block':'none';
  document.getElementById('n-'+x).className=x===t?'on':'';}
  if(t==='settings')loadSettings();if(t==='problems')loadMissions();}
async function sendMission(){
  const m=document.getElementById('mission').value.trim();if(!m)return;
  if(!confirm('Lancer cette mission ?'))return;
  document.getElementById('mission').value='';
  await api('mission',{text:m});
}
async function stopMission(){if(confirm('Arrêter la mission en cours ?'))await api('stop',{});}
async function sendAnswer(){
  const a=document.getElementById('answer').value.trim();if(!a)return;
  document.getElementById('answer').value='';
  await api('answer',{text:a});
}
async function poll(){
  try{
    if(!TOKEN)return;
    const s=await api('status');
    document.getElementById('dot').style.background=s.running?'var(--ok)':(s.awaiting?'var(--acc)':'var(--mut)');
    document.getElementById('engineLbl').textContent=s.engine_label||'';
    document.getElementById('btnSend').disabled=s.running;
    document.getElementById('answerBox').style.display=s.awaiting?'block':'none';
    const r=await api('logs?since='+lastId);
    const box=document.getElementById('logs');
    for(const e of r.entries){
      lastId=e.id;
      const d=document.createElement('div');d.className='log-line tag-'+e.tag;
      d.textContent=e.text;box.appendChild(d);
    }
    if(r.entries.length)box.scrollTop=box.scrollHeight;
    if(box.children.length>300)box.removeChild(box.firstChild);
  }catch(e){}
}
setInterval(poll,2000);
async function loadSettings(){
  const s=await api('settings');
  document.getElementById('engineBox').innerHTML=
    'Moteur actif : <b>'+(s.engine_mode||'terra')+'</b>'+(s.use_ollama?' (Ollama)':'')+'<br>'+
    '<span class="mut">Clés configurées : Mistral '+(s.has_mistral?'✅':'❌')+
    ' | OpenRouter '+(s.has_openrouter?'✅':'❌')+'<br>Mail agent : '+(s.terra_email||'non configuré')+'</span>';
  const box=document.getElementById('senders');box.innerHTML='';
  for(const em of s.trusted_senders){
    const p=document.createElement('span');p.className='pill';
    p.innerHTML=em+' <button onclick="rmSender(\\''+em+'\\')">✕</button>';box.appendChild(p);}
}
async function addSender(){const v=document.getElementById('newSender').value.trim();
  if(v){await api('trust',{email:v,add:true});loadSettings();}}
async function rmSender(em){if(confirm('Retirer '+em+' ?')){await api('trust',{email:em,add:false});loadSettings();}}
async function changePin(){const v=document.getElementById('newPin').value.trim();
  if(v.length!==6){alert('Le PIN doit faire 6 chiffres');return;}
  await api('pin',{pin:v});alert('PIN modifié');}
async function loadMissions(){
  const r=await api('missions');
  document.getElementById('missions').innerHTML=r.html||'Aucune mission enregistrée.';
}
(async()=>{if(TOKEN){const s=await api('status').catch(()=>null);
  if(s&&s.ok!==false){document.getElementById('login').style.display='none';
  document.getElementById('app').style.display='block';poll();}}})();
</script>
</body>
</html>"""


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
        return HUB.valid_token(tok)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
            return self._json({"ok": True, "entries": HUB.logs_since(since)})

        if u.path == "/api/settings":
            creds = HUB.hooks["load_credentials"]()
            trusted = HUB.hooks["load_trusted_senders"]()
            return self._json({
                "ok": True,
                "engine_mode": creds.get("engine_mode", "terra"),
                "use_ollama": bool(creds.get("use_ollama")),
                "has_mistral": bool(HUB.hooks.get("has_mistral")()),
                "has_openrouter": bool(creds.get("extra_api_key") or HUB.hooks.get("has_openrouter")()),
                "terra_email": creds.get("terra_mail_address", ""),
                "trusted_senders": trusted,
            })

        if u.path == "/api/missions":
            html = HUB.hooks.get("missions_html", lambda: "")()
            return self._json({"ok": True, "html": html})

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
            if hashlib.sha256(str(data.get("pin", "")).strip().encode()).hexdigest() == get_pin_hash(creds):
                return self._json({"ok": True, "token": HUB.new_token()})
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
            web_inst = HUB.hooks["make_instance"](conn=WebConn())
            web_inst._awaiting = False
            HUB.current_instance = web_inst
            web_inst.log(f"💬 [WEB] Mission reçue : {text[:150]}\n", "user")
            HUB.hooks["start_task"](text, web_inst, on_complete=HUB.hooks.get("on_web_complete"))
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
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "PIN invalide (6 chiffres)"})

        self._json({"ok": False, "error": "route inconnue"}, 404)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server(start_task, make_instance, load_credentials, save_credentials,
                 load_trusted_senders, save_trusted_senders,
                 has_mistral_fn=None, has_openrouter_fn=None, queue_len_fn=None,
                 missions_html_fn=None, on_web_complete=None):
    """Lancé par agent_server.py dans un thread daemon."""
    HUB.hooks = {
        "start_task": start_task,
        "make_instance": make_instance,
        "load_credentials": load_credentials,
        "save_credentials": save_credentials,
        "load_trusted_senders": load_trusted_senders,
        "save_trusted_senders": save_trusted_senders,
        "has_mistral": has_mistral_fn or (lambda: False),
        "has_openrouter": has_openrouter_fn or (lambda: False),
        "queue_len": queue_len_fn or (lambda: 0),
        "missions_html": missions_html_fn or (lambda: ""),
        "on_web_complete": on_web_complete,
    }
    try:
        ensure_pin(load_credentials, save_credentials)
        open_firewall_port(WEB_PORT)
        server = WebServer(("0.0.0.0", WEB_PORT), Handler)
        print(f"[WebApp] 🌐 Interface web mobile active sur le port {WEB_PORT} (accès local/VPN).")
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except Exception as e:
        print(f"[WebApp] ⚠️ Interface web non démarrée : {e}")
