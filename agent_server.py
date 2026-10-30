# -*- coding: utf-8 -*-
import sys
import subprocess
import socket
import json
import threading
import time
import os
import re
import shutil
import imaplib
import smtplib
import email
from email.header import decode_header
from email.message import EmailMessage

# --- 0. SYSTÈME AUTO-INSTALLATEUR COMPLET (C++ REDIST, PIP, BRAVE, PARE-FEU) ---
DEPENDENCIES = {
    'mss': 'mss',
    'PIL': 'Pillow',
    'openai': 'openai',
    'pyautogui': 'PyAutoGUI',
    'pygetwindow': 'PyGetWindow',
    'pyperclip': 'pyperclip',
    'fitz': 'pymupdf'
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")


def verifier_et_installer_vcredist():
    """Vérifie la présence de vcruntime140.dll (requis par PyMuPDF/fitz) et l'installe via winget si absent."""
    sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    dll_presente = (
        os.path.exists(os.path.join(sys32, "vcruntime140.dll"))
        and os.path.exists(os.path.join(sys32, "vcruntime140_1.dll"))
    )

    if dll_presente:
        return

    print("Composants Microsoft Visual C++ Redistributable manquants. Installation silencieuse...")
    try:
        cmd = [
            "winget", "install", "--id", "Microsoft.VCRedist.2015+.x64",
            "-e", "--source", "winget",
            "--silent", "--accept-source-agreements", "--accept-package-agreements"
        ]
        subprocess.check_call(cmd)
        print("Visual C++ Redistributable installé avec succès !\n")
    except Exception as e:
        print(f"Avertissement : installation automatique de VCRedist échouée ({e}).\n")


def installer_dependances():
    print("Vérification des composants nécessaires...")
    verifier_et_installer_vcredist()

    paquets_manquants = []
    for module, package in DEPENDENCIES.items():
        try:
            __import__(module)
        except ImportError as err:
            if module == 'fitz' and "DLL load failed" in str(err):
                print("Réparation de la DLL C++ pour PyMuPDF...")
                try:
                    subprocess.call([
                        "winget", "install", "--id", "Microsoft.VCRedist.2015+.x64",
                        "-e", "--silent", "--accept-source-agreements", "--accept-package-agreements"
                    ])
                except Exception:
                    pass
            paquets_manquants.append(package)

    if paquets_manquants:
        print(f"Installation automatique des paquets manquants : {', '.join(paquets_manquants)}...")
        try:
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--upgrade', *paquets_manquants])
            print("Installation terminée avec succès !\n")
        except Exception as e:
            print(f"Erreur critique lors de l'installation : {e}")
            sys.exit(1)
    else:
        print("Tous les composants Python sont prêts.\n")


def verifier_et_installer_brave():
    brave_paths = [
        os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe")
    ]

    if any(os.path.exists(p) for p in brave_paths) or shutil.which("brave"):
        print("Navigateur Brave déjà présent.\n")
        return

    print("Brave n'est pas installé. Début de l'installation silencieuse...")
    try:
        cmd = [
            "winget", "install", "--id", "Brave.Brave",
            "-e", "--source", "winget",
            "--silent", "--accept-source-agreements", "--accept-package-agreements"
        ]
        subprocess.check_call(cmd)
        print("Brave installé avec succès !\n")
    except Exception as e:
        print(f"Avertissement : installation automatique de Brave échouée ({e}).\n")


def ouvrir_pare_feu_port(port=65432):
    """Autorise automatiquement le port TCP 65432 dans le pare-feu Windows de la VM."""
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=AgentServerVM"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )
    except Exception:
        try:
            subprocess.run(
                [
                    "netsh", "advfirewall", "firewall", "add", "rule",
                    "name=AgentServerVM", "dir=in", "action=allow",
                    "protocol=TCP", f"localport={port}"
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print(f"Pare-feu Windows : Port TCP {port} ouvert automatiquement.\n")
        except Exception:
            pass


# Vérifications et installations AVANT d'importer le reste
installer_dependances()
verifier_et_installer_brave()
ouvrir_pare_feu_port(65432)

from openai import OpenAI

# --- 1. CHARGEMENT DES CERVEAUX D'EXÉCUTION (TERRA / LITE / OLLAMA) ---
import variant_agentterratest as engine_terra

try:
    import agent_core as engine_lite
    HAS_LITE_ENGINE = True
    print("Moteurs d'action chargés : [1] GPT-6.1 Terra | [2] Claude Haiku 5.5 | [3] Ollama Local\n")
except Exception as e:
    engine_lite = None
    HAS_LITE_ENGINE = False
    print(f"Info : Moteur 'agent_core.py' absent ({e}). Terra & Ollama actifs.\n")

_ORIG_TERRA_GET_CLIENT = engine_terra.get_openrouter_client
_ORIG_TERRA_PRIMARY = engine_terra.TERRA_MODEL_PRIMARY
_ORIG_TERRA_FALLBACK = engine_terra.TERRA_MODEL_FALLBACK

if HAS_LITE_ENGINE:
    _ORIG_LITE_GET_CLIENT = engine_lite.get_openrouter_client
    _ORIG_LITE_MODEL = engine_lite.MODEL_LITE

load_api_key = engine_terra.load_api_key
save_api_key = engine_terra.save_api_key

ACTIVE_CLIENT_INSTANCE = None


# ==============================================================================
# 2. MODULE MISTRAL : CERVEAU AMONT (RÉDACTION & ÉCRITURE DIRECTE SUR DISQUE)
# ==============================================================================
def get_mistral_api_key() -> str:
    """Récupère la clé API Mistral depuis credentials.json ou les variables d'environnement."""
    creds = load_credentials()
    key = creds.get("mistral_api_key", "").strip()
    if not key and creds.get("extra_api_name", "").lower().startswith("mistral"):
        key = creds.get("extra_api_key", "").strip()
    return key or os.environ.get("MISTRAL_API_KEY", "").strip()


def extraire_et_ecrire_code_mistral(texte_mistral: str, app_instance=None) -> list[str]:
    """
    Extrait les fichiers générés par Mistral et les écrit directement sur le disque de la VM.
    Format attendu : ```html:C:\\chemin\\fichier.ext ou chemin en commentaire sur la 1ère ligne.
    """
    fichiers_ecrits = []
    pattern = r"```(?:\w+)?(?::([^\n]+))?\n(.*?)```"
    matches = re.findall(pattern, texte_mistral, re.DOTALL)

    for path_header, code_body in matches:
        target_path = path_header.strip() if path_header else ""

        # Si le chemin est en commentaire sur la 1ère ligne du code (ex: // C:\... ou # C:\...)
        if not target_path:
            lines = code_body.strip().splitlines()
            first_line = lines[0] if lines else ""
            if any(first_line.startswith(p) for p in ["//", "#", "/*", "<!--"]):
                possible_path = re.sub(r"^[/#*<!-]+", "", first_line).replace("-->", "").replace("*/", "").strip()
                if any(sep in possible_path for sep in ["\\", "/"]) and "." in possible_path:
                    target_path = possible_path
                    code_body = "\n".join(lines[1:])

        # Si un chemin valide a été détecté, écriture immédiate sur le disque de la VM
        if target_path and any(sep in target_path for sep in ["\\", "/"]) and "." in target_path:
            full_path = os.path.expanduser(os.path.expandvars(target_path))
            try:
                os.makedirs(os.path.dirname(os.path.abspath(full_path)), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(code_body.strip())
                fichiers_ecrits.append(full_path)
                if app_instance:
                    app_instance.log(f"⚡ [Code écrit sur disque par Mistral] : {full_path}\n", "system")
            except Exception as err:
                if app_instance:
                    app_instance.log(f"⚠️ Erreur écriture fichier {full_path} : {err}\n", "error")

    return fichiers_ecrits


def run_mistral_analysis(raw_task: str, app_instance=None) -> tuple[str, list[str]]:
    """
    Interroge l'API Mistral pour analyser la tâche et écrire directement tout le code sur disque.
    Retourne (consigne_pour_terra, liste_des_fichiers_ecrits).
    En cas de dépassement de limite de tokens (429) ou d'absence de clé, bascule proprement sur GPT/Claude.
    """
    mistral_key = get_mistral_api_key()
    if not mistral_key:
        return raw_task, []

    if app_instance:
        app_instance.log("🧠 [Mistral AI] Analyse, génération du code et préparation du plan...\n", "system")

    try:
        client = OpenAI(
            base_url="https://api.mistral.ai/v1",
            api_key=mistral_key,
            timeout=45.0
        )
        desktop_dir = os.path.join(os.path.expanduser("~"), "Desktop")
        sys_prompt = (
            "Tu es l'architecte logiciel principal et le codeur exclusif d'un agent Windows (Terra).\n"
            "1. RÈGLE D'ÉCRITURE DIRECTE DU CODE : Tu dois rédiger 100% du code demandé toi-même. "
            "Pour chaque fichier de code à créer ou modifier, mets OBLIGATOIREMENT son chemin absolu Windows "
            f"(ex: sur le Bureau '{desktop_dir}\\nom_fichier.ext') juste après les deux-points de la balise Markdown.\n"
            f"Exemple strict :\n```html:{desktop_dir}\\bonjour.html\n<!DOCTYPE html>...\n```\n"
            "Le serveur interceptera ton bloc et écrira le fichier directement sur le disque.\n"
            "2. RÈGLE STRICTE POUR L'AGENT TERRA : Terra a l'INTERDICTION ABSOLUE de taper ou modifier du code. "
            "Ne lui demande JAMAIS de faire Ctrl+N, de taper du code ou d'utiliser write_clipboard pour coder. "
            "Donne-lui uniquement des ordres de navigation (ex: ouvrir VS Code sur le fichier déjà créé, "
            "ouvrir le fichier dans Brave pour vérifier l'affichage visuel, tester les boutons).\n"
            "3. INTERDICTION DE FERMER LES FENÊTRES : Ne demande JAMAIS à Terra de fermer les applications ou d'utiliser Alt+F4. "
            "Le résultat final doit toujours rester ouvert à l'écran."
            "4. RAPPORT FINAL : termine ta consigne pour Terra par : 'Termine en écrivant dans le chat RÉSULTAT: <résumé avec liens et preuves>'. "
            "Mistral relira ensuite ce rapport pour vérifier que tout s'est bien passé et pourra redéléguer une étape à Terra si besoin."
        )
        response = client.chat.completions.create(
            model="mistral-large-latest",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": raw_task}
            ],
            temperature=0.2,
            max_tokens=2000
        )
        plan = (response.choices[0].message.content or "").strip()
        if not plan:
            return raw_task, []

        # Écriture directe sur le disque de tous les blocs de code produits par Mistral
        fichiers = extraire_et_ecrire_code_mistral(plan, app_instance=app_instance)

        # On retire les gros blocs de code du texte envoyé à Terra pour économiser les tokens OpenRouter
        plan_sans_code = re.sub(r"```.*?```", "[Code déjà écrit sur le disque par Mistral]", plan, flags=re.DOTALL)

        if app_instance:
            app_instance.log(f"📋 [Plan Mistral prêt] ({len(fichiers)} fichier(s) écrit(s) sur disque).\n", "agent")

        consigne_terra = (
            f"DEMANDE INITIALE : {raw_task}\n\n"
            f"FICHIERS DÉJÀ CRÉÉS SUR DISQUE PAR MISTRAL : {', '.join(fichiers) if fichiers else 'Aucun fichier direct'}\n\n"
            "CONSIGNE STRICTE POUR TERRA :\n"
            "- Le code est DÉJÀ écrit sur le disque par Mistral. Tu as l'INTERDICTION ABSOLUE de coder, d'éditer le texte dans VS Code ou de recréer le fichier.\n"
            "- Si l'utilisateur a demandé d'ouvrir VS Code ou le navigateur pour voir/valider le fichier, ouvre simplement le fichier existant, vérifie visuellement le rendu, et ne touche plus au code.\n"
            "- INTERDICTION ABSOLUE de fermer les fenêtres (pas de Alt+F4). Laisse tout ouvert et appelle finish_task dès que c'est vérifié.\n\n"
            f"FEUILLE DE ROUTE DE NAVIGATION :\n{plan_sans_code}"
        )
        return consigne_terra, fichiers

    except Exception as e:
        if app_instance:
            app_instance.log(f"⚠️ Limite de tokens ou erreur Mistral ({e}). Bascule automatique sur le moteur GPT/Claude.\n", "error")
        return raw_task, []


# ==============================================================================
# 3. GESTION DES IDENTIFIANTS & CONFIGURATION RUNTIME
# ==============================================================================
def load_credentials() -> dict:
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_credentials(data: dict) -> dict:
    current = load_credentials()
    current.update(data)
    if "terra_mail_app_password" in current and isinstance(current["terra_mail_app_password"], str):
        current["terra_mail_app_password"] = current["terra_mail_app_password"].replace(" ", "").strip()
    try:
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Erreur] Sauvegarde credentials.json : {e}")
    return current


def configure_runtime_for_mode(mode: str, creds: dict | None = None):
    if creds is None:
        creds = load_credentials()

    if mode == "local":
        raw_url = creds.get("ollama_url", "http://localhost:11434").strip().rstrip("/")
        base_url = raw_url if raw_url.endswith("/v1") else raw_url + "/v1"
        model_name = creds.get("ollama_model", "qwen2.5-vl:7b").strip() or "qwen2.5-vl:7b"

        def _get_ollama_client():
            return OpenAI(base_url=base_url, api_key="ollama", timeout=90.0, max_retries=1)

        engine_terra.get_openrouter_client = _get_ollama_client
        engine_terra.TERRA_MODEL_PRIMARY = model_name
        engine_terra.TERRA_MODEL_FALLBACK = model_name

        if HAS_LITE_ENGINE:
            engine_lite.get_openrouter_client = _get_ollama_client
            engine_lite.MODEL_LITE = model_name
    else:
        engine_terra.get_openrouter_client = _ORIG_TERRA_GET_CLIENT
        engine_terra.TERRA_MODEL_PRIMARY = _ORIG_TERRA_PRIMARY
        engine_terra.TERRA_MODEL_FALLBACK = _ORIG_TERRA_FALLBACK

        if HAS_LITE_ENGINE:
            engine_lite.get_openrouter_client = _ORIG_LITE_GET_CLIENT
            engine_lite.MODEL_LITE = _ORIG_LITE_MODEL


# ==============================================================================
# 4. INSTANCE D'EXÉCUTION RÉSEAU ET ORCHESTRATION DU TANDEM
# ==============================================================================
class RemoteAppInstance:
    def __init__(self, conn=None, mail_reply_to: str | None = None, mail_subject: str | None = None):
        self.conn = conn
        self.mail_reply_to = mail_reply_to
        self.mail_subject = mail_subject
        self.collected_logs: list[str] = []
        self.final_summary: str = ""

        self.stop_requested = False
        self.agent_running = False
        self.awaiting_api_key = False
        self.pending_task = None
        self.shared_state = {"data": None, "done": False}
        self.human_response = ""
        self.wait_event = threading.Event()

        creds = load_credentials()
        self.engine_mode = "local" if creds.get("use_ollama", False) else creds.get("engine_mode", "terra")

    def log(self, message: str, tag: str = "system"):
        clean_msg = str(message)
        self.collected_logs.append(clean_msg.strip())

        if "✅ Mission accomplie :" in clean_msg:
            self.final_summary = clean_msg.split("✅ Mission accomplie :", 1)[-1].strip()

        if self.mail_reply_to and ("L'agent a besoin d'aide :" in clean_msg or "🤖 Question :" in clean_msg):
            threading.Thread(
                target=MAIL_WATCHER.send_email,
                args=(
                    self.mail_reply_to,
                    f"Re: {self.mail_subject or 'MISSION TERRA'} [QUESTION AGENT]",
                    (
                        f"Bonjour,\n\nL'agent sur la VM a besoin d'une précision pour continuer sa mission :\n\n"
                        f"👉 {clean_msg.strip()}\n\n"
                        f"Réponds directement à cet email pour relancer l'exécution."
                    ),
                ),
                daemon=True,
            ).start()

        target_conn = self.conn
        if target_conn is None and ACTIVE_CLIENT_INSTANCE is not None:
            target_conn = ACTIVE_CLIENT_INSTANCE.conn

        if target_conn is not None:
            try:
                data = json.dumps({"text": clean_msg, "tag": tag}) + "\n"
                target_conn.sendall(data.encode("utf-8"))
            except Exception:
                pass
        else:
            print(clean_msg, end="")

    def wait_for_human(self):
        self.wait_event.clear()
        self.wait_event.wait()


def get_active_engine(app_instance: RemoteAppInstance):
    mode = getattr(app_instance, "engine_mode", "terra")
    configure_runtime_for_mode(mode)
    if mode == "lite" and HAS_LITE_ENGINE:
        return engine_lite
    return engine_terra


def start_agent_task(task: str, app_instance: RemoteAppInstance, on_complete=None, use_mistral=True):
    creds = load_credentials()
    mode = getattr(app_instance, "engine_mode", "terra")
    active_engine = get_active_engine(app_instance)

    mistral_ready = bool(get_mistral_api_key())
    if mode == "local":
        exec_label = f"Ollama Local ({creds.get('ollama_model', 'qwen2.5-vl:7b')})"
    elif active_engine is engine_lite:
        exec_label = "Claude Haiku 5.5 (Mode Lite)"
    else:
        exec_label = "GPT-6.1 Terra"

    tandem_label = f"Tandem Mistral -> {exec_label}" if mistral_ready else exec_label

    app_instance.log(f"🚀 [Orchestration : {tandem_label}] Début de la mission.\n", "system")
    app_instance.stop_requested = False
    app_instance.agent_running = True
    app_instance.collected_logs.clear()
    app_instance.final_summary = ""
    app_instance.shared_state = {"data": None, "done": False}

    def full_workflow():
        try:
            # Étape 1 : Mistral rédige et écrit les fichiers de code directement sur le disque
            # (désactivable avec use_mistral=False, ex: mission directe /terra depuis le chat)
            if use_mistral:
                enriched_task, fichiers_crees = run_mistral_analysis(task, app_instance=app_instance)
            else:
                enriched_task, fichiers_crees = task, []

            if app_instance.stop_requested:
                return

            # Étape 2 : Si Mistral a écrit le code et qu'aucune action visuelle n'est requise,
            # on s'arrête immédiatement (0 token OpenRouter dépensé !)
            t_low = task.lower()
            mots_cles_ui = [
                "ouvre", "lance", "valide", "vérifie", "verifie", "affiche", "regarde",
                "navigateur", "brave", "vscode", "vs code", "clique", "teste", "écran", "ecran"
            ]
            if fichiers_crees and not any(w in t_low for w in mots_cles_ui):
                fichiers_str = ", ".join(fichiers_crees)
                app_instance.log(
                    f"✅ Mission accomplie : Code généré et enregistré par Mistral ({fichiers_str}) sans dépenser de tokens GPT.\n",
                    "system"
                )
                return

            # Étape 3 : Injection sécurisée du compte principal si présent
            main_email = creds.get("main_account_email", "").strip()
            main_pwd = creds.get("main_account_password", "").strip()
            if main_email and main_email.lower() not in enriched_task.lower():
                enriched_task += (
                    f"\n\n[Note Système : Si une connexion est demandée et non ouverte dans Brave, "
                    f"utilise '{main_email}'" + (f" avec le mot de passe '{main_pwd}'." if main_pwd else ".]")
                )

            # Étape 4 : Détection de profil pour l'exécuteur
            try:
                app_instance.shared_state["data"] = active_engine.prepare_task_data(task)
            except Exception:
                app_instance.shared_state["data"] = None
            app_instance.shared_state["done"] = True

            # Étape 5 : Exécution visuelle / navigation uniquement (Terra / Claude / Ollama)
            active_engine.run_agent(enriched_task, app_instance)

            # Étape 6 : Boucle tandem — Mistral vérifie le rapport de Terra et peut redéléguer une étape
            if use_mistral and not app_instance.stop_requested:
                historique = [
                    f"Mission initiale : {task}",
                    f"Fichiers/dossiers créés par Mistral : {', '.join(fichiers_crees) or 'aucun'}",
                ]
                for step in range(TANDEM_MAX_STEPS):
                    if app_instance.stop_requested:
                        return
                    logs_terra = "\n".join(app_instance.collected_logs[-20:])
                    historique.append(f"--- Étape Terra {step + 1} (logs) ---\n{logs_terra}")
                    decision = run_mistral_next_step(task, historique, app_instance)
                    action = str(decision.get("action", "finish")).lower()
                    consigne = str(decision.get("consigne", "")).strip()
                    if action == "terra" and consigne:
                        app_instance.log(f"🔁 [Tandem étape {step + 1}] Mistral redélègue à Terra : {consigne[:150]}\n", "system")
                        app_instance.collected_logs.clear()
                        active_engine.run_agent(consigne, app_instance)
                        continue
                    resume = str(decision.get("resume", "")).strip() or "Mission terminée et vérifiée."
                    app_instance.log(f"✅ Mission accomplie : {resume}\n", "system")
                    return
        finally:
            app_instance.agent_running = False
            if callable(on_complete):
                try:
                    on_complete(app_instance)
                except Exception as cb_err:
                    print(f"[Callback] Erreur : {cb_err}")

    threading.Thread(target=full_workflow, daemon=True).start()


# ==============================================================================
# 5. MODULE MAILWATCHER (IMAP / SMTP)
# ==============================================================================
class MailWatcher:
    def __init__(self):
        self.running = True
        self.current_mail_instance: RemoteAppInstance | None = None
        self.last_error_logged = ""

    def _get_host_ports(self, email_addr: str):
        domain = email_addr.split("@")[-1].lower() if "@" in email_addr else ""
        if any(d in domain for d in ["outlook", "hotmail", "live", "office365"]):
            return "outlook.office365.com", 993, "smtp.office365.com", 587, False
        return "imap.gmail.com", 993, "smtp.gmail.com", 465, True

    def _decode_str(self, raw_val) -> str:
        if not raw_val:
            return ""
        parts = decode_header(raw_val)
        decoded = []
        for content, enc in parts:
            if isinstance(content, bytes):
                decoded.append(content.decode(enc or "utf-8", errors="replace"))
            else:
                decoded.append(str(content))
        return "".join(decoded).strip()

    def _extract_text_body(self, msg) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                cdisp = str(part.get("Content-Disposition") or "")
                if ctype == "text/plain" and "attachment" not in cdisp:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace").strip()
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace").strip()
        return ""

    def send_email(self, to_addr: str, subject: str, body: str) -> bool:
        creds = load_credentials()
        terra_email = creds.get("terra_mail_address", "").strip()
        terra_pwd = creds.get("terra_mail_app_password", "").replace(" ", "").strip()

        if not terra_email or not terra_pwd or not to_addr:
            return False

        _, _, smtp_host, smtp_port, use_ssl = self._get_host_ports(terra_email)
        try:
            em = EmailMessage()
            em["From"] = terra_email
            em["To"] = to_addr
            em["Subject"] = subject
            em.set_content(body)

            if use_ssl:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
                    server.login(terra_email, terra_pwd)
                    server.send_message(em)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                    server.starttls()
                    server.login(terra_email, terra_pwd)
                    server.send_message(em)

            print(f"[MailWatcher] 📤 Rapport envoyé à {to_addr}")
            return True
        except Exception as e:
            print(f"[MailWatcher] ❌ Erreur SMTP : {e}")
            return False

    def _on_mail_task_complete(self, instance: RemoteAppInstance):
        summary = instance.final_summary or "Exécution terminée."
        recent_logs = "\n".join(instance.collected_logs[-20:])
        report_body = (
            f"Bonjour,\n\n"
            f"Voici le rapport d'exécution sur la VM :\n\n"
            f"✅ RÉSUMÉ : {summary}\n\n"
            f"--- ACTIONS EXÉCUTÉES ---\n"
            f"{recent_logs}\n"
            f"-------------------------\n"
        )
        subj = f"Re: {instance.mail_subject or 'MISSION TERRA'} [TERMINÉ]"
        self.send_email(instance.mail_reply_to, subj, report_body)

    def watch_loop(self):
        print("[MailWatcher] 📩 Service d'écoute IMAP actif en arrière-plan.")
        while self.running:
            try:
                creds = load_credentials()
                enabled = creds.get("terra_mail_enabled", True)
                terra_email = creds.get("terra_mail_address", "").strip()
                terra_pwd = creds.get("terra_mail_app_password", "").replace(" ", "").strip()
                auth_sender = creds.get("terra_authorized_sender", "").strip().lower()

                if not enabled or not terra_email or not terra_pwd:
                    time.sleep(8)
                    continue

                imap_host, imap_port, _, _, _ = self._get_host_ports(terra_email)
                mail = imaplib.IMAP4_SSL(imap_host, imap_port)
                mail.login(terra_email, terra_pwd)
                mail.select("INBOX")

                if self.last_error_logged:
                    self.last_error_logged = ""
                    if ACTIVE_CLIENT_INSTANCE:
                        ACTIVE_CLIENT_INSTANCE.log("✅ Connexion IMAP à la boîte Mail de Terra rétablie !\n", "system")

                status, messages = mail.search(None, "UNSEEN")
                if status == "OK" and messages and messages[0]:
                    for num in messages[0].split():
                        res, data = mail.fetch(num, "(RFC822)")
                        if res != "OK" or not data or not data[0]:
                            continue

                        msg = email.message_from_bytes(data[0][1])
                        sender_raw = self._decode_str(msg.get("From", ""))
                        sender_addr = email.utils.parseaddr(sender_raw)[1].lower()
                        subject = self._decode_str(msg.get("Subject", ""))
                        body = self._extract_text_body(msg)

                        if sender_addr == terra_email.lower():
                            continue

                        subject_up = subject.upper()
                        has_keyword = any(k in subject_up for k in ["TERRA", "MISSION", "AGENT", "TASK", "VM"])
                        if auth_sender and sender_addr != auth_sender and not has_keyword:
                            continue

                        instruction_text = body.strip() if body.strip() else subject.strip()
                        if not instruction_text:
                            continue

                        if (
                            self.current_mail_instance
                            and self.current_mail_instance.agent_running
                            and not self.current_mail_instance.wait_event.is_set()
                        ):
                            self.current_mail_instance.log(f"📩 Réponse mail reçue de {sender_addr} !\n", "system")
                            self.current_mail_instance.human_response = instruction_text
                            self.current_mail_instance.wait_event.set()
                            continue

                        if subject_up.strip() == "STOP" or instruction_text.upper() == "STOP":
                            if self.current_mail_instance and self.current_mail_instance.agent_running:
                                self.current_mail_instance.stop_requested = True
                                self.current_mail_instance.wait_event.set()
                            if ACTIVE_CLIENT_INSTANCE and ACTIVE_CLIENT_INSTANCE.agent_running:
                                ACTIVE_CLIENT_INSTANCE.stop_requested = True
                                ACTIVE_CLIENT_INSTANCE.wait_event.set()
                            self.send_email(sender_addr, f"Re: {subject} [STOPPÉ]", "Mission arrêtée.")
                            continue

                        busy = (
                            (self.current_mail_instance and self.current_mail_instance.agent_running)
                            or (ACTIVE_CLIENT_INSTANCE and ACTIVE_CLIENT_INSTANCE.agent_running)
                        )
                        if busy:
                            self.send_email(sender_addr, f"Re: {subject} [OCCUPÉ]", "L'agent traite déjà une tâche.")
                            continue

                        print(f"[MailWatcher] 📩 Mission mail reçue de {sender_addr} : {subject}")
                        mail_inst = RemoteAppInstance(conn=None, mail_reply_to=sender_addr, mail_subject=subject)
                        self.current_mail_instance = mail_inst
                        mail_inst.log(f"\n📩 [MAIL REÇU DE {sender_addr}] Sujet : {subject}\n", "user")

                        full_task = f"{subject}\n{instruction_text}".strip() if subject and subject not in instruction_text else instruction_text
                        start_agent_task(full_task, mail_inst, on_complete=self._on_mail_task_complete)

                mail.logout()
            except Exception as err:
                err_str = str(err)
                if err_str != self.last_error_logged:
                    self.last_error_logged = err_str
                    print(f"[MailWatcher] ⚠️ Alerte Mail Terra : {err_str}")
                    if ACTIVE_CLIENT_INSTANCE:
                        ACTIVE_CLIENT_INSTANCE.log(f"⚠️ Erreur Mail Terra : {err_str}\n", "error")

            time.sleep(12)


MAIL_WATCHER = MailWatcher()


# ==============================================================================
# 6. GESTION DU CLIENT TCP (APP CLIENT <-> VM)
# ==============================================================================
def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", BASE_DIR, *args], capture_output=True, text=True)


def send_update_status(app_instance):
    """Envoie au client l'état git de la VM (commits en avance sur beta/main)."""
    try:
        _git("fetch", "origin")
        info = {
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "head": _git("rev-parse", "--short", "HEAD").stdout.strip(),
        }
        for b in ("beta", "main"):
            out = _git("rev-list", "--count", f"HEAD..origin/{b}").stdout.strip()
            try:
                info[b] = int(out)
            except ValueError:
                info[b] = 0
        payload = {"type": "update_status", "vm": info}
    except Exception as e:
        payload = {"type": "update_status", "vm": {"error": str(e)}}
    target = app_instance.conn or (ACTIVE_CLIENT_INSTANCE.conn if ACTIVE_CLIENT_INSTANCE else None)
    if target is not None:
        try:
            target.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except Exception:
            pass


def handle_update_vm(app_instance, branch: str):
    """Met à jour les fichiers de la VM sur la branche choisie puis redémarre le serveur."""
    try:
        app_instance.log(f"⬇️ Mise à jour de la VM vers '{branch}' en cours...\n", "system")
        if _git("status", "--porcelain").stdout.strip():
            _git("stash", "push", "-m", "auto-update VM")
            app_instance.log("💾 Modifications locales de la VM mises de côté (git stash).\n", "system")
        r = _git("checkout", branch)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        r = _git("pull", "--ff-only", "origin", branch)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        app_instance.log(f"✅ VM mise à jour sur '{branch}'. Redémarrage du serveur...\n", "system")
        time.sleep(1.5)  # laisse le temps au log d'arriver chez le client
        os.execv(sys.executable, [sys.executable, os.path.join(BASE_DIR, "agent_server.py")] + sys.argv[1:])
    except Exception as e:
        app_instance.log(f"❌ Échec de la mise à jour de la VM : {e}\n", "error")


TANDEM_MAX_STEPS = 4


def run_mistral_next_step(raw_task: str, historique: list[str], app_instance=None) -> dict:
    """
    Boucle tandem : Mistral (chef d'équipe) reçoit le rapport de Terra et décide de la
    prochaine action. Retourne {"action": "terra"|"finish", "consigne": ..., "resume": ...}.
    """
    mistral_key = get_mistral_api_key()
    if not mistral_key:
        return {"action": "finish", "resume": "Pas de clé Mistral : mission terminée après exécution de Terra.", "consigne": ""}
    if app_instance:
        app_instance.log("🧠 [Tandem] Mistral relit le rapport de Terra et décide de la suite...\n", "system")
    try:
        client = OpenAI(base_url="https://api.mistral.ai/v1", api_key=mistral_key, timeout=45.0)
        sys_prompt = (
            "Tu es le chef d'équipe d'un tandem d'agents sur une VM Windows.\n"
            "Toi (Mistral) tu crées/lis des fichiers et dossiers sur le disque de la VM (tu peux créer un dossier en y écrivant un fichier) et tu prends les décisions.\n"
            "Terra (GPT) possède le navigateur Brave : il fait les actions web (GitHub, connexions, formulaires) et les validations visuelles (rendu de page, superposition, PDF).\n\n"
            "MISSION DONNÉE PAR L'UTILISATEUR :\n" + raw_task + "\n\n"
            "HISTORIQUE DES ÉTAPES DÉJÀ RÉALISÉES :\n" + "\n".join(historique) + "\n\n"
            "Décide de la PROCHAINE action. Réponds UNIQUEMENT avec un JSON strict, sans aucun texte autour :\n"
            '- {"action": "terra", "consigne": "..."} pour déléguer une étape à Terra (ex: aller vérifier un lien/repo GitHub, valider un rendu visuel, se connecter, corriger un détail). La consigne doit être autonome et se terminer par : Termine en écrivant dans le chat RÉSULTAT: <résumé avec liens et preuves>.\n'
            '- {"action": "finish", "resume": "..."} uniquement si la mission est accomplie ET vérifiée grâce à l\'historique (ne termine pas sur un doute).'
        )
        resp = client.chat.completions.create(
            model="mistral-large-latest",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "Prochaine action ? (JSON uniquement)"},
            ],
            temperature=0.2,
            max_tokens=1000,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
        data = json.loads(raw)
        if not isinstance(data, dict) or "action" not in data:
            raise ValueError("JSON non conforme")
        return data
    except Exception as e:
        if app_instance:
            app_instance.log(f"⚠️ [Tandem] Réponse Mistral illisible ({e}) : on considère la mission terminée.\n", "system")
        return {"action": "finish", "resume": "Mission terminée après exécution de Terra (vérification auto indisponible).", "consigne": ""}


def ask_mistral_direct(question: str, app_instance=None):
    """Chat direct avec Mistral depuis l'app (sans lancer Terra ni passer par le mail)."""
    key = get_mistral_api_key()
    if not key:
        if app_instance:
            app_instance.log("⚠️ Clé API Mistral absente : ajoute-la dans ⚙️ Paramètres > ✨ Mistral (Relai).\n", "error")
        return
    if app_instance:
        app_instance.log(f"💬 [Mistral] {question}\n", "user")
    try:
        client = OpenAI(base_url="https://api.mistral.ai/v1", api_key=key, timeout=45.0)
        resp = client.chat.completions.create(
            model="mistral-large-latest",
            messages=[{"role": "user", "content": question}],
            temperature=0.4,
            max_tokens=1200,
        )
        answer = (resp.choices[0].message.content or "").strip()
        if app_instance:
            app_instance.log(f"✨ [Mistral] {answer}\n", "agent")
    except Exception as e:
        if app_instance:
            app_instance.log(f"❌ Erreur appel Mistral : {e}\n", "error")


def handle_client(conn, addr):
    global ACTIVE_CLIENT_INSTANCE
    print(f"[Réseau] PC Principal connecté depuis {addr}")
    app_instance = RemoteAppInstance(conn=conn)
    ACTIVE_CLIENT_INSTANCE = app_instance

    creds = load_credentials()
    mail_ok = bool(creds.get("terra_mail_address") and creds.get("terra_mail_app_password"))
    mistral_ok = bool(get_mistral_api_key())

    app_instance.log(
        "✅ Connecté au serveur de la VM !\n"
        f"⚙️ Moteurs : Terra | Lite Claude ({'OUI' if HAS_LITE_ENGINE else 'NON'}) | Ollama\n"
        f"🧠 Réflexion & Code Mistral : {'Connecté (Écriture disque directe)' if mistral_ok else 'Non configuré'} | 📩 Mail : {'Actif' if mail_ok else 'Inactif'}\n",
        "system"
    )

    if not load_api_key() and app_instance.engine_mode != "local":
        app_instance.awaiting_api_key = True
        app_instance.log("🔑 Aucune clé OpenRouter détectée. Renseigne-la dans ⚙️ Paramètres > 🔑 API & OAuth.\n", "agent")

    buffer = ""
    while True:
        try:
            data = conn.recv(4096)
            if not data:
                break

            buffer += data.decode('utf-8', errors='replace')
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue

                try:
                    msg = json.loads(line)
                except Exception:
                    msg = {"type": "instruction", "content": line}

                msg_type = msg.get("type", "instruction")

                if msg_type == "stop":
                    app_instance.stop_requested = True
                    app_instance.human_response = "Action annulée par l'utilisateur (STOP NET)."
                    app_instance.wait_event.set()
                    if MAIL_WATCHER.current_mail_instance and MAIL_WATCHER.current_mail_instance.agent_running:
                        MAIL_WATCHER.current_mail_instance.stop_requested = True
                        MAIL_WATCHER.current_mail_instance.wait_event.set()
                    app_instance.log("🛑 Commande STOP reçue.\n", "system")

                elif msg_type == "reset_key":
                    app_instance.awaiting_api_key = True
                    app_instance.log("🔑 Entre ta nouvelle clé OpenRouter :\n", "agent")

                elif msg_type == "set_mode":
                    requested_mode = str(msg.get("mode", "terra")).lower().strip()
                    if "ollama_url" in msg or "ollama_model" in msg:
                        save_credentials({
                            "ollama_url": msg.get("ollama_url", "http://localhost:11434"),
                            "ollama_model": msg.get("ollama_model", "qwen2.5-vl:7b"),
                            "use_ollama": (requested_mode == "local"),
                            "engine_mode": requested_mode,
                        })

                    if requested_mode == "lite":
                        if HAS_LITE_ENGINE:
                            app_instance.engine_mode = "lite"
                            configure_runtime_for_mode("lite")
                            app_instance.log("⚡ Mode LITE (Claude Haiku 5.5) activé.\n", "system")
                        else:
                            app_instance.log("⚠️ Fichier 'agent_core.py' absent sur la VM.\n", "error")
                    elif requested_mode == "local":
                        app_instance.engine_mode = "local"
                        c = load_credentials()
                        configure_runtime_for_mode("local", c)
                        app_instance.log(f"🦙 Mode LOCAL Ollama ({c.get('ollama_model', 'qwen2.5-vl:7b')}) activé.\n", "system")
                    else:
                        app_instance.engine_mode = "terra"
                        configure_runtime_for_mode("terra")
                        app_instance.log("🚀 Mode TERRA (OpenAI GPT-6.1 Terra) activé.\n", "system")

                elif msg_type == "sync_credentials":
                    or_key = msg.get("openrouter_key", "").strip()
                    if or_key:
                        save_api_key(or_key)
                        app_instance.awaiting_api_key = False

                    if not str(msg.get("mistral_api_key", "")).strip() and msg.get("extra_api_name", "").lower().startswith("mistral") and msg.get("extra_api_key"):
                        msg["mistral_api_key"] = msg["extra_api_key"].strip()

                    saved = save_credentials(msg)
                    new_mode = saved.get("engine_mode", app_instance.engine_mode)
                    if saved.get("use_ollama", False):
                        new_mode = "local"
                    app_instance.engine_mode = new_mode
                    configure_runtime_for_mode(new_mode, saved)

                    t_mail = bool(saved.get("terra_mail_address") and saved.get("terra_mail_app_password"))
                    m_ready = bool(get_mistral_api_key())
                    MAIL_WATCHER.last_error_logged = ""
                    app_instance.log(
                        f"✅ Paramètres synchronisés (Mode: {new_mode.upper()} | Codeur Mistral: {'ACTIF' if m_ready else 'OFF'} | Mail Terra: {'OK' if t_mail else 'OFF'}).\n",
                        "system"
                    )

                elif msg_type == "check_update":
                    threading.Thread(target=send_update_status, args=(app_instance,), daemon=True).start()

                elif msg_type == "update_vm":
                    branch = str(msg.get("branch", "beta")).strip().lower()
                    if branch not in ("beta", "main"):
                        branch = "beta"
                    app_instance.log(f"📡 Ordre de mise à jour reçu pour la branche '{branch}'.\n", "system")
                    threading.Thread(target=handle_update_vm, args=(app_instance, branch), daemon=True).start()

                elif msg_type in ["instruction", "task"]:
                    content = msg.get("content", "").strip()

                    if content.lower() in ["/lite", "!lite"]:
                        if HAS_LITE_ENGINE:
                            app_instance.engine_mode = "lite"
                            configure_runtime_for_mode("lite")
                            app_instance.log("⚡ Mode LITE (Claude Haiku 5.5) activé.\n", "system")
                        continue
                    elif content.lower() in ["/terra", "!terra"]:
                        app_instance.engine_mode = "terra"
                        configure_runtime_for_mode("terra")
                        app_instance.log("🚀 Mode TERRA activé.\n", "system")
                        continue
                    elif content.lower() in ["/local", "!local", "/ollama"]:
                        app_instance.engine_mode = "local"
                        configure_runtime_for_mode("local")
                        app_instance.log("🦙 Mode LOCAL Ollama activé.\n", "system")
                        continue

                    # --- Adressage direct à un agent depuis le chat de l'app (sans passer par le mail) ---
                    low = content.lower()
                    if low.startswith("/mistral ") or low.startswith("/m "):
                        question = content.split(" ", 1)[1].strip()
                        threading.Thread(target=ask_mistral_direct, args=(question, app_instance), daemon=True).start()
                        continue
                    if low.startswith("/terra ") or low.startswith("/gpt "):
                        target_task = content.split(" ", 1)[1].strip()
                        app_instance.log(f"🎯 Mission directe pour l'exécuteur : {target_task[:80]}\n", "system")
                        start_agent_task(target_task, app_instance, use_mistral=False)
                        continue

                    if app_instance.awaiting_api_key and app_instance.engine_mode != "local":
                        save_api_key(content)
                        app_instance.awaiting_api_key = False
                        app_instance.log("✅ Clé API enregistrée !\n", "system")
                        if app_instance.pending_task:
                            t = app_instance.pending_task
                            app_instance.pending_task = None
                            start_agent_task(t, app_instance)
                        continue

                    if not load_api_key() and app_instance.engine_mode != "local":
                        app_instance.awaiting_api_key = True
                        app_instance.pending_task = content
                        app_instance.log("🔑 Clé API introuvable. Colle ta clé OpenRouter :\n", "agent")
                        continue

                    if app_instance.agent_running and not app_instance.wait_event.is_set():
                        app_instance.human_response = content
                        app_instance.wait_event.set()
                    elif (
                        MAIL_WATCHER.current_mail_instance
                        and MAIL_WATCHER.current_mail_instance.agent_running
                        and not MAIL_WATCHER.current_mail_instance.wait_event.is_set()
                    ):
                        MAIL_WATCHER.current_mail_instance.human_response = content
                        MAIL_WATCHER.current_mail_instance.wait_event.set()
                    else:
                        start_agent_task(content, app_instance)

                elif msg_type == "answer":
                    ans = msg.get("content", "")
                    app_instance.human_response = ans
                    app_instance.wait_event.set()
                    if MAIL_WATCHER.current_mail_instance:
                        MAIL_WATCHER.current_mail_instance.human_response = ans
                        MAIL_WATCHER.current_mail_instance.wait_event.set()

        except Exception as e:
            print(f"[Réseau] Erreur : {e}")
            break

    print("[Réseau] PC Principal déconnecté.")
    if ACTIVE_CLIENT_INSTANCE is app_instance:
        ACTIVE_CLIENT_INSTANCE = None
    conn.close()


def get_real_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


if __name__ == "__main__":
    local_ip = get_real_local_ip()
    port = 65432

    threading.Thread(target=MAIL_WATCHER.watch_loop, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(1)

    init_creds = load_credentials()
    mail_cfg_str = init_creds.get("terra_mail_address") or "Non configuré"
    mistral_state = "Actif (Écriture disque directe)" if get_mistral_api_key() else "Inactif"

    print("=" * 65)
    print(" 🤖 AGENT SERVEUR PRÊT (Dans la VM)")
    print("=" * 65)
    print(f"👉 ADRESSE IP VM          : {local_ip}")
    print(f"👉 PORT                   : {port}")
    print(f"🧠 CODEUR MISTRAL         : {mistral_state}")
    print(f"👁️ MOTEURS D'ACTION       : Terra (Défaut) | Lite Claude ({'OUI' if HAS_LITE_ENGINE else 'NON'}) | Ollama")
    print(f"📩 SERVICE MAIL TERRA     : {mail_cfg_str}")
    print("=" * 65)
    print("En attente de connexion de la bulle ou d'un email de mission...")

    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
