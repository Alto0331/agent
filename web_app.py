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


PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes, viewport-fit=cover">
<meta name="theme-color" content="#0b0e14">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Agent VM</title>
<style>
:root{--bg:#0b0e14;--side:#0d1117;--card:#161b22;--card2:#1c2330;--line:#262d3a;--acc:#3b82f6;--acc2:#2563eb;--ok:#10b981;--err:#ef4444;--warn:#f59e0b;--txt:#e6edf3;--mut:#8b949e}
*{margin:0;padding:0;box-sizing:border-box;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{background:var(--bg);color:var(--txt);display:flex;overflow:hidden}
/* ===== SIDEBAR ===== */
#sidebar{width:270px;background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;flex-shrink:0;z-index:40;transition:transform .25s ease}
#sb-top{padding:14px 14px 10px}
#brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:15px;margin-bottom:12px}
#brand .dot{margin-left:auto}
select#engineSel{width:100%;background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:9px;padding:10px 12px;font-size:13px;outline:none;margin-bottom:10px}
.sb-btn{width:100%;display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--line);color:var(--txt);padding:11px 13px;border-radius:9px;font-size:13.5px;cursor:pointer;margin-bottom:7px;text-align:left}
.sb-btn:active{background:var(--card2)}
.sb-btn.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
#sb-sessions{flex:1;overflow-y:auto;padding:6px 10px}
#sb-sessions h3{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--mut);padding:8px 6px 6px}
.session{display:flex;align-items:center;gap:9px;padding:10px 10px;border-radius:9px;cursor:pointer;color:var(--txt);font-size:13px;border:1px solid transparent;margin-bottom:2px}
.session:active,.session.on{background:var(--card);border-color:var(--line)}
.session .ttl{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.session .del{color:var(--mut);background:none;border:none;font-size:13px;cursor:pointer;padding:2px 4px}
#sb-user{border-top:1px solid var(--line);padding:12px 14px;display:flex;align-items:center;gap:10px;cursor:pointer}
#sb-user .avatar{width:32px;height:32px;border-radius:50%;background:var(--card2);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;color:var(--acc)}
#sb-user .info{flex:1;min-width:0}
#sb-user .nm{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sb-user .ws{font-size:11px;color:var(--mut)}
#sb-user .gear{color:var(--mut);font-size:16px}
/* ===== MAIN ===== */
#main{flex:1;display:flex;flex-direction:column;min-width:0;height:100vh}
header{display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:1px solid var(--line);background:var(--bg);z-index:10}
#hamb{display:none;background:var(--card);border:1px solid var(--line);color:var(--txt);font-size:16px;padding:6px 10px;border-radius:9px;cursor:pointer}
header .ttl{font-weight:600;font-size:14.5px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#stLbl{font-size:11px;color:var(--mut);background:var(--card);border:1px solid var(--line);padding:5px 11px;border-radius:999px;white-space:nowrap}
#stLbl.run{color:var(--ok);border-color:var(--ok)}
#stLbl.wait{color:var(--warn);border-color:var(--warn)}
#chat{flex:1;overflow-y:auto;padding:18px 16px 8px;scroll-behavior:smooth}
#logs{max-width:820px;margin:0 auto;font-size:13.5px;line-height:1.65}
.log-line{padding:7px 2px;border-bottom:1px solid rgba(255,255,255,.04);white-space:pre-wrap;word-wrap:break-word}
.tag-sys{color:var(--mut)}.tag-agent{color:#7dd3fc}.tag-user{color:#86efac}.tag-error{color:var(--err)}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--mut);gap:8px}
.empty .big{font-size:40px}
/* ===== INPUT BAR ===== */
#inputBar{padding:10px 14px calc(12px + env(safe-area-inset-bottom));background:var(--bg)}
#answerBox{max-width:820px;margin:0 auto 10px;background:var(--card);border:1px solid var(--warn);border-radius:13px;padding:12px}
#answerBox .lbl{font-size:12px;color:var(--warn);font-weight:600;margin-bottom:8px}
#inputRow{max-width:820px;margin:0 auto;display:flex;gap:9px;align-items:flex-end}
#mission{flex:1;background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:14px;padding:13px 15px;font-size:14.5px;outline:none;resize:none;max-height:130px;min-height:47px;line-height:1.45}
#mission:focus{border-color:var(--acc)}
.icbtn{width:47px;height:47px;border-radius:50%;flex-shrink:0;border:1px solid var(--line);background:var(--card);color:var(--txt);font-size:17px;cursor:pointer;display:flex;align-items:center;justify-content:center}
.icbtn:active{background:var(--card2)}
#btnSend{background:var(--acc);border-color:var(--acc);color:#fff}
#btnSend:disabled{opacity:.4}
#btnStop{border-color:var(--err);color:var(--err);display:none}
/* ===== MODALES / CARTES SETTINGS ===== */
#overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:35;opacity:0;transition:opacity .2s;display:none}
#drawer{position:fixed;top:0;right:-360px;width:350px;max-width:92vw;height:100vh;background:var(--side);border-left:1px solid var(--line);z-index:50;transition:right .25s;overflow-y:auto;padding:18px 16px calc(30px + env(safe-area-inset-bottom))}
#drawer.open{right:0}
#drawer h2{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--mut);margin:18px 0 10px}
#drawer .close{position:absolute;top:12px;right:12px;background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:5px 11px;cursor:pointer}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
textarea,.txt{width:100%;background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:11px 12px;font-size:14px;outline:none}
textarea:focus,.txt:focus{border-color:var(--acc)}
input.txt{margin:5px 0}
label{font-size:11.5px;color:var(--mut);display:block;margin:9px 0 2px;font-weight:600}
.btn{background:var(--acc);color:#fff;border:none;border-radius:10px;padding:12px;font-size:14px;font-weight:600;cursor:pointer;width:100%}
.btn:active{background:var(--acc2)}
.btn.sec{background:var(--card2);color:var(--txt);border:1px solid var(--line)}
.pill{display:inline-flex;align-items:center;gap:7px;background:var(--card2);border:1px solid var(--line);border-radius:999px;padding:6px 12px;margin:3px;font-size:12.5px}
.pill button{background:none;border:none;color:var(--err);cursor:pointer;padding:0}
.mut{color:var(--mut);font-size:12px}
.ok-err{margin-top:7px;font-size:12.5px}
#login{position:fixed;inset:0;background:var(--bg);z-index:100;display:flex;align-items:center;justify-content:center;padding:20px}
#login .box{width:100%;max-width:380px;text-align:center}
#login .logo{font-size:46px;margin-bottom:6px}
#login h1{font-size:17px;margin-bottom:16px}
#login input{margin-bottom:10px}
#toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--card2);border:1px solid var(--line);color:var(--txt);padding:11px 20px;border-radius:11px;font-size:13.5px;z-index:90;opacity:0;transition:opacity .2s;pointer-events:none;max-width:88vw}
#toast.show{opacity:1}
#ctxBar{max-width:820px;margin:0 auto 10px;display:none;background:var(--card);border:1px solid var(--line);border-radius:13px;padding:12px}
#ctxBar .lbl{font-size:12px;color:var(--mut);font-weight:600;margin-bottom:6px}
#ctxBar textarea{min-height:70px;font-size:13px}
@media(max-width:860px){
#sidebar{position:fixed;left:0;top:0;height:100vh;transform:translateX(-100%)}
#sidebar.open{transform:translateX(0)}
#hamb{display:block}
}
</style>
</head>
<body>
<div id="login"><div class="box">
  <div class="logo">🤖</div>
  <h1>Agent VM</h1>
  <input id="pin" class="txt" type="password" inputmode="numeric" placeholder="PIN à 6 chiffres" autofocus>
  <button class="btn" onclick="login()">Se connecter</button>
  <p class="mut" id="loginErr" style="color:var(--err);margin-top:10px"></p>
</div></div>

<div id="app" style="display:none">
<div id="overlay" onclick="closeAll()"></div>

<div id="sidebar">
  <div id="sb-top">
    <div id="brand">🤖 Agent VM <span class="dot" id="dot2"></span></div>
    <select id="engineSel" onchange="engineChange()">
      <option value="terra">GPT-6.1 Terra</option>
      <option value="lite">Claude Haiku 5.5</option>
      <option value="local">Ollama local</option>
    </select>
    <button class="sb-btn primary" onclick="newSession()">＋ Nouvelle session</button>
    <button class="sb-btn" onclick="toggleCtx()">📄 Contexte</button>
    <button class="sb-btn" onclick="openProblems()">🚨 Problèmes</button>
  </div>
  <div id="sb-sessions"><h3>Sessions</h3><div id="sessions"></div></div>
  <div id="sb-user" onclick="openSettings()">
    <div class="avatar" id="avatar">A</div>
    <div class="info"><div class="nm" id="userName">Agent VM</div><div class="ws">VM Locale</div></div>
    <span class="gear">⚙️</span>
  </div>
</div>

<div id="main">
  <header>
    <button id="hamb" onclick="toggleSB()">☰</button>
    <div class="ttl" id="sessTitle">Nouvelle session</div>
    <span id="stLbl">●</span>
  </header>
  <div id="chat"><div id="logs"></div></div>
  <div id="inputBar">
    <div id="ctxBar">
      <div class="lbl">📄 Contexte de la mission (ajouté automatiquement à la prochaine mission)</div>
      <textarea id="ctxTxt" placeholder="Ex : Je suis en Poitou-Charentes, tu me tutoies, format des rapports en tableau…"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="btn sec" onclick="saveCtx()">Enregistrer</button>
        <button class="btn sec" onclick="toggleCtx()">Fermer</button>
      </div>
    </div>
    <div id="answerBox" style="display:none">
      <div class="lbl">⏳ L'agent attend ta réponse</div>
      <textarea id="answer" placeholder="Ta réponse…"></textarea>
      <button class="btn" style="margin-top:8px" onclick="sendAnswer()">▶️ Répondre</button>
    </div>
    <div id="inputRow">
      <button class="icbtn" id="btnStop" onclick="stopMission()" title="Stop">⏹</button>
      <textarea id="mission" rows="1" placeholder="Message…"></textarea>
      <button class="icbtn" id="btnSend" onclick="sendMission()" title="Envoyer">➤</button>
    </div>
  </div>
</div>

<div id="drawer">
  <button class="close" onclick="closeAll()">✕</button>
  <div id="drawer-content"></div>
</div>
</div>
<div id="toast"></div>
<script>
function saveTok(t){try{localStorage.setItem('wa_tok',t);}catch(e){}
  try{sessionStorage.setItem('wa_tok',t);}catch(e){}
  try{document.cookie='wa_tok='+t+';path=/;max-age=31536000;SameSite=Strict';}catch(e){}}
function loadTok(){try{let t=localStorage.getItem('wa_tok');if(t)return t;}catch(e){}
  try{let t=sessionStorage.getItem('wa_tok');if(t)return t;}catch(e){}
  try{let m=document.cookie.match(/(?:^|; )wa_tok=([^;]+)/);if(m)return m[1];}catch(e){}
  return '';}
let TOKEN=loadTok();let lastId=0;let sessViewed=0;
const $=id=>document.getElementById(id);
function toast(msg,ok=true){const t=$('toast');t.textContent=msg;t.style.borderColor=ok?'var(--ok)':'var(--err)';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500);}
const api=(path,data)=>fetch('/api/'+path,{method:data?'POST':'GET',
  headers:{'Content-Type':'application/json','X-Auth':TOKEN},
  body:data?JSON.stringify(data):undefined}).then(r=>r.json());
/* ===== SESSIONS ===== */
async function loadSessions(){
  const r=await api('sessions');
  const box=$('sessions');box.innerHTML='';
  if(!r.sessions.length){box.innerHTML='<div class="mut" style="padding:8px">Aucune session</div>';return;}
  for(const s of r.sessions){
    const el=document.createElement('div');el.className='session'+(s.id===sessViewed?' on':'');
    const t=document.createElement('span');t.className='ttl';t.textContent=s.title;el.appendChild(t);
    const d=document.createElement('button');d.className='del';d.textContent='✕';
    d.onclick=async(e)=>{e.stopPropagation();if(confirm('Supprimer cette session ?')){await api('session_del',{id:s.id});loadSessions();}};el.appendChild(d);
    el.onclick=()=>viewSession(s.id);box.appendChild(el);}
  if(r.current){sessViewed=r.current;$('sessTitle').textContent=r.title||'Session';}}
function newSession(){sessViewed=0;lastId=0;$('logs').innerHTML='<div class="empty"><div class="big">🤖</div><div>Nouvelle session — envoie ta mission</div></div>';$('sessTitle').textContent='Nouvelle session';loadSessions();if(innerWidth<860)toggleSB();}
function viewSession(id){sessViewed=id;lastId=0;$('logs').innerHTML='';loadSessions();api('session_logs',{id}).then(r=>{const box=$('logs');for(const e of r.entries){const d=document.createElement('div');d.className='log-line tag-'+e.tag;d.textContent=e.text;box.appendChild(d);}box.scrollTop=box.scrollHeight;});$('sessTitle').textContent='Session #'+id;if(innerWidth<860)toggleSB();}
/* ===== UI ===== */
function toggleSB(){$('sidebar').classList.toggle('open');$('overlay').style.display=$('sidebar').classList.contains('open')?'block':'none';}
function closeAll(){$('sidebar').classList.remove('open');$('drawer').classList.remove('open');$('overlay').style.display='none';}
function toggleCtx(){const c=$('ctxBar');c.style.display=c.style.display==='block'?'none':'block';if(c.style.display==='block')$('ctxTxt').value=localStorage.getItem('wa_ctx')||'';if(innerWidth<860)$('sidebar').classList.remove('open');}
function saveCtx(){localStorage.setItem('wa_ctx',$('ctxTxt').value);toggleCtx();toast('Contexte enregistré 📄');}
function openDrawer(html){$('drawer-content').innerHTML=html;$('drawer').classList.add('open');$('overlay').style.display='block';$('sidebar').classList.remove('open');}
async function openSettings(){
  const s=await api('settings');
  openDrawer(`
  <h2>🧠 Moteur</h2>
  <select id="set-engine" class="txt">
    <option value="terra"${s.engine_mode==='terra'?' selected':''}>GPT-6.1 Terra</option>
    <option value="lite"${s.engine_mode==='lite'?' selected':''}>Claude Haiku 5.5</option>
    <option value="local"${s.engine_mode==='local'?' selected':''}>Ollama local</option>
  </select>
  <div id="ollamaBox" style="display:${s.engine_mode==='local'?'block':'none'}">
    <label>URL Ollama</label><input id="set-ollama-url" class="txt" value="${s.ollama_url||'http://localhost:11434'}">
    <label>Modèle</label><input id="set-ollama-model" class="txt" value="${s.ollama_model||'qwen2.5-vl:7b'}">
  </div>
  <h2>🔑 Clés API</h2>
  <label>OpenRouter ${s.has_openrouter?'✅':'❌'}</label><input id="set-orkey" class="txt" type="password" placeholder="Vide = inchangé">
  <label>Mistral ${s.has_mistral?'✅':'❌'}</label><input id="set-mistralkey" class="txt" type="password" placeholder="Vide = inchangé">
  <h2>📧 Mail agent</h2>
  <label>Adresse</label><input id="set-terra-mail" class="txt" value="${s.terra_email||''}">
  <label>Mot de passe app ${s.terra_email?'✅':'❌'}</label><input id="set-terra-pwd" class="txt" type="password" placeholder="Vide = inchangé">
  <label>Expéditeur autorisé</label><input id="set-auth-sender" class="txt" value="${s.auth_sender||''}">
  <h2>👤 Compte principal</h2>
  <label>Email</label><input id="set-main-mail" class="txt" value="${s.main_email||''}">
  <label>Mot de passe</label><input id="set-main-pwd" class="txt" type="password" placeholder="Vide = inchangé">
  <h2>🤝 Expéditeurs de confiance</h2>
  <input id="newSender" class="txt" placeholder="email@ajouter.com">
  <button class="btn sec" style="margin-top:8px" onclick="addSender()">➕ Ajouter</button>
  <div id="senders" style="margin-top:8px">${s.trusted_senders.map(e=>`<span class="pill">${e}<button onclick="rmSender('${e}')">✕</button></span>`).join('')}</div>
  <h2>🔐 PIN</h2>
  <input id="newPin" class="txt" placeholder="Nouveau PIN (6 chiffres)">
  <button class="btn sec" style="margin-top:8px" onclick="changePin()">Changer le PIN</button>
  <div style="height:14px"></div>
  <button class="btn" onclick="saveAll()">💾 Sauvegarder tout</button>
  <p class="mut ok-err" id="saveMsg"></p>
  <div style="height:20px"></div>`);
  $('set-engine').onchange=()=>{$('ollamaBox').style.display=$('set-engine').value==='local'?'block':'none';};}
async function openProblems(){
  const r=await api('missions');
  openDrawer(`<h2>🧭 Missions récentes</h2>
  <div class="card" style="font-size:12px;line-height:1.8">${r.html||'Aucune mission enregistrée.'}</div>
  <h2>🛠️ Infrastructure</h2>
  <div class="card"><p class="mut">Surveillance Proxmox/RMM : prochaine étape (V.3.3).</p></div>`);}
/* ===== ACTIONS ===== */
async function engineChange(){
  const r=await api('sync',{engine_mode:$('engineSel').value,use_ollama:$('engineSel').value==='local'});
  toast(r.ok?'Moteur changé ✅':'Erreur',r.ok);}
async function sendMission(){
  const m=$('mission').value.trim();if(!m)return;
  const ctx=localStorage.getItem('wa_ctx')||'';
  const full=ctx?m+'\n\n[CONTEXTE PERMANENT]\n'+ctx:m;
  $('mission').value='';$('mission').style.height='auto';
  const r=await api('mission',{text:full,session:sessViewed});
  if(!r.ok)toast(r.error||'Erreur',false);else{lastId=0;$('logs').innerHTML='';toast('Mission lancée 🚀');loadSessions();}}
async function stopMission(){if(confirm('Arrêter la mission ?')){await api('stop',{});toast('Arrêt demandé');}}
async function sendAnswer(){const a=$('answer').value.trim();if(!a)return;$('answer').value='';await api('answer',{text:a});toast('Réponse envoyée ▶️');}
/* ===== POLL ===== */
async function poll(){
  try{if(!TOKEN)return;
    const s=await api('status');
    const lbl=$('stLbl');
    lbl.className=s.running?'run':(s.awaiting?'wait':'');
    lbl.textContent=s.running?'● En cours':(s.awaiting?'● Réponse requise':'●');
    $('answerBox').style.display=s.awaiting?'block':'none';
    $('btnStop').style.display=s.running?'flex':'none';
    $('btnSend').disabled=s.running;
    const r=await api('logs?since='+lastId+(sessViewed?'&session='+sessViewed:''));
    const box=$('logs');
    if(r.entries.length){
      if(box.querySelector('.empty'))box.innerHTML='';
      for(const e of r.entries){lastId=e.id;
        const d=document.createElement('div');d.className='log-line tag-'+e.tag;
        d.textContent=e.text;box.appendChild(d);}
      $('chat').scrollTop=$('chat').scrollHeight;}}catch(e){}}
setInterval(poll,2000);
/* ===== SETTINGS HELPERS ===== */
async function addSender(){const v=$('newSender').value.trim();if(v){await api('trust',{email:v,add:true});openSettings();}}
async function rmSender(em){if(confirm('Retirer '+em+' ?')){await api('trust',{email:em,add:false});openSettings();}}
async function changePin(){const v=$('newPin').value.trim();
  if(v.length!==6){toast('PIN : 6 chiffres',false);return;}
  const r=await api('pin',{pin:v});
  if(r.ok&&r.reconnected_token){TOKEN=r.reconnected_token;saveTok(TOKEN);toast('PIN modifié ✅');}else toast(r.error||'Erreur',false);}
async function saveAll(){
  const payload={engine_mode:$('set-engine').value,ollama_url:$('set-ollama-url').value,ollama_model:$('set-ollama-model').value,
    terra_mail_address:$('set-terra-mail').value,terra_authorized_sender:$('set-auth-sender').value,main_account_email:$('set-main-mail').value,
    use_ollama:$('set-engine').value==='local'};
  const g=id=>$(id).value.trim();
  if(g('set-orkey'))payload.openrouter_key=g('set-orkey');
  if(g('set-mistralkey'))payload.mistral_api_key=g('set-mistralkey');
  if(g('set-terra-pwd'))payload.terra_mail_app_password=g('set-terra-pwd');
  if(g('set-main-pwd'))payload.main_account_password=g('set-main-pwd');
  const r=await api('sync',payload);
  $('saveMsg').textContent=r.ok?'✅ Sauvegardé':'❌ '+(r.error||'Erreur');
  $('saveMsg').style.color=r.ok?'var(--ok)':'var(--err)';
  if(r.ok)toast('Sauvegardé ✅');}
/* ===== LOGIN/BOOT ===== */
async function login(){
  const btn=document.querySelector('#login .btn');
  btn.disabled=true;btn.textContent='Connexion…';
  try{const resp=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pin:$('pin').value})});
    const r=await resp.json();
    if(r.ok){TOKEN=r.token;saveTok(TOKEN);location.reload();}
    else $('loginErr').textContent='PIN incorrect';
  }catch(e){$('loginErr').textContent='Erreur réseau : vérifie la connexion à la VM';}
  btn.disabled=false;btn.textContent='Se connecter';}
$('pin').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
$('mission').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMission();}});
$('mission').addEventListener('input',function(){this.style.height='auto';this.style.height=Math.min(this.scrollHeight,130)+'px';});
(async()=>{if(TOKEN){const s=await api('status').catch(()=>null);
  if(s&&s.ok!==false){$('login').style.display='none';$('app').style.display='flex';
  const saved=localStorage.getItem('wa_engine');if(saved)$('engineSel').value=saved;
  loadSessions();new boot();}}})();
function boot(){poll();}
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
            })

        if u.path == "/api/sessions":
            sessions = []
            for sid in sorted(HUB.sessions.keys(), reverse=True):
                s = HUB.sessions[sid]
                sessions.append({"id": sid, "title": s.get("title", "Session")[:60]})
            return self._json({"ok": True, "sessions": sessions,
                               "current": HUB.active_session,
                               "title": HUB.sessions.get(HUB.active_session, {}).get("title", "")})

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
            ]
            payload = {k: v for k, v in data.items() if k in allowed and v != ""}
            try:
                HUB.hooks["apply_credentials_sync"](payload, HUB.current_instance)
                return self._json({"ok": True})
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
                 missions_html_fn=None, on_web_complete=None, apply_credentials_sync=None):
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
