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
<meta name="theme-color" content="#0f172a">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Agent VM</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--card2:#24334a;--acc:#38bdf8;--acc2:#0ea5e9;--ok:#4ade80;--err:#f87171;--warn:#fbbf24;--txt:#e2e8f0;--mut:#94a3b8;--side:#0b1220;--line:#334155}
*{margin:0;padding:0;box-sizing:border-box;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--txt);min-height:100vh;padding-bottom:env(safe-area-inset-bottom)}
header{background:rgba(15,23,42,.92);backdrop-filter:blur(8px);padding:14px 16px;position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--line)}
.hamb{background:var(--card);border:1px solid var(--line);color:var(--txt);font-size:17px;cursor:pointer;padding:6px 11px;border-radius:9px}
header .title{flex:1;display:flex;align-items:center;gap:9px}
header h1{font-size:16px;font-weight:600;letter-spacing:.2px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut);box-shadow:0 0 8px transparent;transition:all .3s}
.dot.run{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.dot.wait{background:var(--warn);box-shadow:0 0 8px var(--warn)}
#engineLbl{font-size:11px;color:var(--mut);background:var(--card);border:1px solid var(--line);padding:5px 10px;border-radius:999px;white-space:nowrap}
#sidebar{position:fixed;top:0;left:-280px;width:270px;height:100vh;background:var(--side);z-index:30;transition:left .25s ease;display:flex;flex-direction:column;border-right:1px solid var(--line)}
#sidebar.open{left:0}
#sidebar .head{padding:20px 18px;border-bottom:1px solid var(--line);font-weight:700;font-size:16px;display:flex;align-items:center;gap:10px}
#sidebar .head .dot{margin-left:auto}
#sidebar button{background:none;border:none;color:var(--txt);text-align:left;padding:15px 18px;font-size:15px;cursor:pointer;border-left:3px solid transparent;display:flex;gap:12px;align-items:center;transition:background .15s}
#sidebar button:active{background:#16203a}
#sidebar button.on{background:#12233c;border-left:3px solid var(--acc);color:var(--acc)}
#sidebar .foot{margin-top:auto;padding:14px 18px;font-size:11px;color:var(--mut);border-top:1px solid var(--line)}
#overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:25;display:none;opacity:0;transition:opacity .25s}
main{max-width:780px;margin:0 auto;padding:16px 14px 30px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.card h2{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--mut);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.tag-sys{color:var(--mut)}.tag-agent{color:var(--acc)}.tag-user{color:var(--ok)}.tag-error{color:var(--err)}
#logs{font-size:13px;line-height:1.6;word-wrap:break-word;white-space:pre-wrap;max-height:44vh;overflow-y:auto;padding-right:4px}
.log-line{padding:4px 0;border-bottom:1px solid #26324a}
.log-line:last-child{border-bottom:none}
textarea,.txt{width:100%;background:#0b1220;color:var(--txt);border:1px solid var(--line);border-radius:11px;padding:13px;font-size:15px;outline:none;transition:border-color .15s}
textarea:focus,.txt:focus{border-color:var(--acc)}
textarea{min-height:96px;resize:vertical}
.btn{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#03121f;border:none;border-radius:11px;padding:13px 20px;font-size:15px;font-weight:700;cursor:pointer;width:100%;transition:opacity .15s,transform .1s}
.btn:active{transform:scale(.985)}
.btn:disabled{opacity:.45}
.btn.sec{background:var(--card2);color:var(--txt);border:1px solid var(--line)}
input.txt{margin:7px 0}
label{font-size:12px;color:var(--mut);display:block;margin:10px 0 2px;font-weight:600}
.pill{display:inline-flex;align-items:center;gap:8px;background:var(--card2);border:1px solid var(--line);border-radius:999px;padding:7px 14px;margin:4px;font-size:13px}
.pill button{background:none;border:none;color:var(--err);cursor:pointer;font-size:14px;padding:0}
.mut{color:var(--mut);font-size:12px}
#login{max-width:400px;margin:16vh auto 0}
#login .logo{text-align:center;font-size:42px;margin-bottom:10px}
#login h2{text-align:center;justify-content:center}
.row{display:flex;gap:10px}
.row .btn{flex:1}
.ok-err{margin-top:8px;font-size:13px}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--card2);border:1px solid var(--line);color:var(--txt);padding:12px 22px;border-radius:12px;font-size:14px;z-index:50;opacity:0;transition:opacity .25s;pointer-events:none;max-width:88vw;text-align:center}
#toast.show{opacity:1}
.fadein{animation:fade .35s ease}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
</style>
</head>
<body>
<div id="login" class="card fadein">
  <div class="logo">🤖</div>
  <h2>🔐 Accès Agent VM</h2>
  <input id="pin" class="txt" type="password" inputmode="numeric" placeholder="PIN à 6 chiffres" autofocus>
  <button class="btn" style="margin-top:12px" onclick="login()">Se connecter</button>
  <p class="mut" id="loginErr" style="color:var(--err);margin-top:10px;text-align:center"></p>
</div>
<div id="app" style="display:none">
<div id="overlay" onclick="toggleMenu()"></div>
<div id="sidebar">
  <div class="head">🤖 Agent VM <span class="dot" id="dot2"></span></div>
  <button id="s-chat" class="on" onclick="tab('chat')">💬 <span>Chat</span></button>
  <button id="s-problems" onclick="tab('problems')">🚨 <span>Problèmes</span></button>
  <button id="s-settings" onclick="tab('settings')">⚙️ <span>Réglages</span></button>
  <div class="foot">Agent VM — interface locale/VPN<br>Port 8765</div>
</div>
<header>
  <button class="hamb" onclick="toggleMenu()">☰</button>
  <div class="title"><span class="dot" id="dot"></span><h1>Agent VM</h1></div>
  <span id="engineLbl"></span>
</header>
<main>
  <section id="tab-chat" class="fadein">
    <div class="card"><h2>📋 Activité en direct</h2><div id="logs"><div class="mut" style="padding:8px 0">En attente d'une mission…</div></div></div>
    <div class="card">
      <h2>🚀 Nouvelle mission</h2>
      <textarea id="mission" placeholder="Décris ta mission pour l'agent… (ex : trouve 5 entreprises de Poitiers et fais-moi une liste)"></textarea>
      <div class="row" style="margin-top:12px">
        <button class="btn" id="btnSend" onclick="sendMission()">Lancer</button>
        <button class="btn sec" id="btnStop" onclick="stopMission()">⏹ Stop</button>
      </div>
      <div id="answerBox" style="display:none;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)">
        <h2 style="color:var(--warn)">⏳ L'agent attend ta réponse</h2>
        <textarea id="answer" placeholder="Ta réponse à l'agent…"></textarea>
        <button class="btn" style="margin-top:10px" onclick="sendAnswer()">▶️ Répondre</button>
      </div>
    </div>
  </section>
  <section id="tab-problems" style="display:none" class="fadein">
    <div class="card"><h2>🧭 Missions récentes</h2><div id="missions" class="mut">Chargement…</div></div>
    <div class="card"><h2>🛠️ Infrastructure</h2>
      <p class="mut">Surveillance Proxmox/RMM : disponible dans une prochaine étape (V.3.3). Les incidents apparaîtront ici avec bouton d'approbation.</p></div>
  </section>
  <section id="tab-settings" style="display:none" class="fadein">
    <div class="card"><h2>🧠 Moteur d'exécution</h2>
      <label>Mode</label>
      <select id="set-engine" class="txt">
        <option value="terra">GPT-6.1 Terra</option>
        <option value="lite">Claude Haiku 5.5 (Lite)</option>
        <option value="local">Ollama local</option>
      </select>
      <div id="ollamaBox" style="display:none">
        <label>URL Ollama</label><input id="set-ollama-url" class="txt" placeholder="http://localhost:11434">
        <label>Modèle</label><input id="set-ollama-model" class="txt" placeholder="qwen2.5-vl:7b">
      </div>
      <p class="mut ok-err" id="engineState"></p>
    </div>
    <div class="card"><h2>🔑 Clés API</h2>
      <label>OpenRouter</label><input id="set-orkey" class="txt" type="password" placeholder="Laisser vide = inchangé">
      <label>Mistral (cerveau)</label><input id="set-mistralkey" class="txt" type="password" placeholder="Laisser vide = inchangé">
      <p class="mut ok-err" id="keysState"></p>
    </div>
    <div class="card"><h2>📧 Mail de l'agent (Terra)</h2>
      <label>Adresse</label><input id="set-terra-mail" class="txt" placeholder="agent@gmail.com">
      <label>Mot de passe d'application</label><input id="set-terra-pwd" class="txt" type="password" placeholder="Laisser vide = inchangé">
      <label>Expéditeur autorisé (missions mail)</label><input id="set-auth-sender" class="txt" placeholder="ton@email.com">
      <p class="mut ok-err" id="mailState"></p>
    </div>
    <div class="card"><h2>👤 Compte principal (connexions web)</h2>
      <label>Email</label><input id="set-main-mail" class="txt">
      <label>Mot de passe</label><input id="set-main-pwd" class="txt" type="password" placeholder="Laisser vide = inchangé">
    </div>
    <div class="card"><h2>🤝 Expéditeurs de confiance</h2>
      <input id="newSender" class="txt" placeholder="email@a-ajouter.com">
      <button class="btn sec" style="margin-top:8px" onclick="addSender()">➕ Ajouter</button>
      <div id="senders" style="margin-top:12px"></div></div>
    <div class="card"><h2>🔐 PIN d'accès</h2>
      <input id="newPin" class="txt" placeholder="Nouveau PIN (6 chiffres)">
      <button class="btn sec" style="margin-top:8px" onclick="changePin()">Changer le PIN</button></div>
    <button class="btn" onclick="saveAll()">💾 Sauvegarder tout</button>
    <p class="mut ok-err" id="saveMsg" style="text-align:center"></p>
  </section>
</main>
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
let TOKEN=loadTok();
let lastId=0;
const $=id=>document.getElementById(id);
function toast(msg,ok=true){const t=$('toast');t.textContent=msg;t.style.borderColor=ok?'var(--ok)':'var(--err)';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600);}
function toggleMenu(){const s=$('sidebar');const o=$('overlay');
  const open=s.classList.toggle('open');o.style.display=open?'block':'none';
  requestAnimationFrame(()=>o.style.opacity=open?'1':'0');
  setTimeout(()=>{if(!open)o.style.display='none';},250);}
function tab(t){for(const x of['chat','problems','settings']){
    $('tab-'+x).style.display=x===t?'block':'none';
    $('s-'+x).className=x===t?'on':'';}
  $('sidebar').classList.remove('open');$('overlay').style.opacity='0';
  setTimeout(()=>{$('overlay').style.display='none';},250);
  if(t==='settings')loadSettings();if(t==='problems')loadMissions();}
const api=(path,data)=>fetch('/api/'+path,{method:data?'POST':'GET',
  headers:{'Content-Type':'application/json','X-Auth':TOKEN},
  body:data?JSON.stringify(data):undefined}).then(r=>r.json());
async function login(){
  const btn=document.querySelector('#login .btn');
  btn.disabled=true;btn.textContent='Connexion…';
  try{
    const resp=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pin:$('pin').value})});
    const r=await resp.json();
    if(r.ok){TOKEN=r.token;saveTok(TOKEN);$('loginErr').textContent='';btn.textContent='Connecté !';location.reload();}
    else{$('loginErr').textContent='PIN incorrect';}
  }catch(e){
    $('loginErr').textContent='Erreur réseau : '+(e.message||e)+' — vérifie la connexion à la VM';
  }
  btn.disabled=false;btn.textContent='Se connecter';}
$('pin').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
async function sendMission(){
  const m=$('mission').value.trim();if(!m)return;
  if(!confirm('Lancer cette mission ?'))return;
  $('mission').value='';
  const r=await api('mission',{text:m});
  if(!r.ok)toast(r.error||'Erreur',false);else toast('Mission lancée 🚀');}
async function stopMission(){if(confirm('Arrêter la mission en cours ?')){await api('stop',{});toast('Arrêt demandé');}}
async function sendAnswer(){const a=$('answer').value.trim();if(!a)return;
  $('answer').value='';await api('answer',{text:a});toast('Réponse envoyée ▶️');}
async function poll(){
  try{
    if(!TOKEN)return;
    const s=await api('status');
    const d=$('dot'),d2=$('dot2');
    const cls=s.running?'dot run':(s.awaiting?'dot wait':'dot');
    d.className=cls;d2.className=cls;
    $('engineLbl').textContent=s.engine_label||'';
    $('btnSend').disabled=s.running;
    $('answerBox').style.display=s.awaiting?'block':'none';
    const r=await api('logs?since='+lastId);
    const box=$('logs');
    if(r.entries.length&&box.querySelector('.mut'))box.innerHTML='';
    for(const e of r.entries){
      lastId=e.id;
      const div=document.createElement('div');div.className='log-line tag-'+e.tag;
      div.textContent=e.text;box.appendChild(div);}
    if(r.entries.length)box.scrollTop=box.scrollHeight;
    while(box.children.length>300)box.removeChild(box.firstChild);
  }catch(e){}}
setInterval(poll,2000);
async function loadSettings(){
  const s=await api('settings');
  $('set-engine').value=s.engine_mode||'terra';
  $('set-ollama-url').value=s.ollama_url||'http://localhost:11434';
  $('set-ollama-model').value=s.ollama_model||'qwen2.5-vl:7b';
  $('ollamaBox').style.display=($('set-engine').value==='local')?'block':'none';
  $('set-terra-mail').value=s.terra_email||'';
  $('set-auth-sender').value=s.auth_sender||'';
  $('set-main-mail').value=s.main_email||'';
  $('keysState').textContent='OpenRouter '+(s.has_openrouter?'✅ configuré':'❌ absent')+' · Mistral '+(s.has_mistral?'✅ configuré':'❌ absent');
  $('mailState').textContent=s.terra_email?'✅ '+s.terra_email:'❌ non configuré';
  $('engineState').textContent='Mode actuel : '+(s.engine_mode||'terra')+(s.use_ollama?' (Ollama)':'');
  const box=$('senders');box.innerHTML='';
  for(const em of s.trusted_senders){
    const p=document.createElement('span');p.className='pill';
    const t=document.createElement('span');t.textContent=em;p.appendChild(t);
    const b=document.createElement('button');b.textContent='✕';b.onclick=()=>rmSender(em);p.appendChild(b);
    box.appendChild(p);}}
$('set-engine').addEventListener('change',()=>{$('ollamaBox').style.display=$('set-engine').value==='local'?'block':'none';});
async function saveAll(){
  const payload={engine_mode:$('set-engine').value,
    ollama_url:$('set-ollama-url').value,ollama_model:$('set-ollama-model').value,
    terra_mail_address:$('set-terra-mail').value,terra_authorized_sender:$('set-auth-sender').value,
    main_account_email:$('set-main-mail')};
  const ork=$('set-orkey').value.trim();if(ork)payload.openrouter_key=ork;
  const mk=$('set-mistralkey').value.trim();if(mk)payload.mistral_api_key=mk;
  const tp=$('set-terra-pwd').value.trim();if(tp)payload.terra_mail_app_password=tp;
  const mp=$('set-main-pwd').value.trim();if(mp)payload.main_account_password=mp;
  payload.use_ollama=(payload.engine_mode==='local');
  const r=await api('sync',payload);
  if(r.ok){toast('Sauvegardé et synchronisé ✅');
    $('set-orkey').value='';$('set-mistralkey').value='';$('set-terra-pwd').value='';$('set-main-pwd').value='';
    loadSettings();}
  else{toast(r.error||'Erreur',false);}}
async function addSender(){const v=$('newSender').value.trim();
  if(v){await api('trust',{email:v,add:true});$('newSender').value='';toast('Expéditeur ajouté');loadSettings();}}
async function rmSender(em){if(confirm('Retirer '+em+' ?')){await api('trust',{email:em,add:false});loadSettings();}}
async function changePin(){const v=$('newPin').value.trim();
  if(v.length!==6){toast('Le PIN doit faire 6 chiffres',false);return;}
  const r=await api('pin',{pin:v});
  if(r.ok&&r.reconnected_token){TOKEN=r.reconnected_token;saveTok(TOKEN);$('newPin').value='';toast('PIN modifié ✅');}
  else{toast(r.error||'Erreur',false);}}
async function loadMissions(){
  const r=await api('missions');
  $('missions').innerHTML=r.html||'Aucune mission enregistrée.';}
(async()=>{if(TOKEN){const s=await api('status').catch(()=>null);
  if(s&&s.ok!==false){$('login').style.display='none';
  $('app').style.display='block';poll();}}})();
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
