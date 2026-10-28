# -*- coding: utf-8 -*-
import sys
import subprocess
import socket
import json
import threading
import os
import shutil

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

def verifier_et_installer_vcredist():
    """Vérifie la présence de vcruntime140.dll (requis par PyMuPDF/fitz) et l'installe via winget si absent."""
    sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    dll_presente = os.path.exists(os.path.join(sys32, "vcruntime140.dll")) and os.path.exists(os.path.join(sys32, "vcruntime140_1.dll"))

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
            # Si fitz plante à cause d'une DLL C++ malgré le premier contrôle, on force l'installation de VCRedist
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
    # Chemins standards de Brave sur Windows
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

# --- 1. CHARGEMENT DES DEUX CERVEAUX (TERRA + LITE CLAUDE HAIKU 5.5) ---
import variant_agentterratest as engine_terra

try:
    import agent_core as engine_lite
    HAS_LITE_ENGINE = True
    print("Double moteur chargé : [1] GPT-6.1 Terra (Principal) | [2] Claude Haiku 5.5 (Mode Lite)\n")
except Exception as e:
    engine_lite = None
    HAS_LITE_ENGINE = False
    print(f"Info : Moteur secondaire 'agent_core.py' non détecté ({e}). Seul GPT-6.1 Terra est actif.\n")

# Gestionnaire de clé API unifié
load_api_key = engine_terra.load_api_key
save_api_key = engine_terra.save_api_key


class RemoteAppInstance:
    """Simule l'interface GUI pour agent_core / variant_agentterratest et communique via TCP."""
    def __init__(self, conn):
        self.conn = conn
        self.stop_requested = False
        self.agent_running = False
        self.awaiting_api_key = False
        self.pending_task = None
        self.shared_state = {"data": None, "done": False}
        self.human_response = ""
        self.wait_event = threading.Event()
        # Mode par défaut : "terra" (GPT-6.1 Terra) ou "lite" (agent_core / Claude Haiku 5.5)
        self.engine_mode = "terra"

    def log(self, message, tag="system"):
        try:
            data = json.dumps({"text": message, "tag": tag}) + "\n"
            self.conn.sendall(data.encode('utf-8'))
        except Exception:
            pass

    def wait_for_human(self):
        self.wait_event.clear()
        self.wait_event.wait()


def get_active_engine(app_instance):
    """Retourne le module moteur actif selon le mode sélectionné ('terra' ou 'lite')."""
    if getattr(app_instance, "engine_mode", "terra") == "lite" and HAS_LITE_ENGINE:
        return engine_lite
    return engine_terra


def start_agent_task(task, app_instance):
    active_engine = get_active_engine(app_instance)
    mode_label = "Mode LITE (Claude Haiku 5.5 - agent_core)" if active_engine is engine_lite else "Mode TERRA (GPT-6.1 Terra)"

    app_instance.log(f"🧠 [{mode_label}] Reçu instruction : {task[:50]}...\n", "system")
    app_instance.stop_requested = False
    app_instance.agent_running = True
    app_instance.shared_state = {"data": None, "done": False}

    def prep(t=task, eng=active_engine):
        try:
            app_instance.shared_state["data"] = eng.prepare_task_data(t)
        except Exception:
            app_instance.shared_state["data"] = None
        app_instance.shared_state["done"] = True
    threading.Thread(target=prep, daemon=True).start()

    def launch(t=task, eng=active_engine):
        try:
            eng.run_agent(t, app_instance)
        finally:
            app_instance.agent_running = False
    threading.Thread(target=launch, daemon=True).start()


def handle_client(conn, addr):
    print(f"[Réseau] PC Principal connecté depuis {addr}")
    app_instance = RemoteAppInstance(conn)
    app_instance.log(
        "✅ Connecté avec succès au cerveau de l'agent dans la VM !\n"
        f"⚙️ Moteur actuel : GPT-6.1 Terra ({'Mode Lite Claude disponible' if HAS_LITE_ENGINE else 'Mode Lite absent'})\n",
        "system"
    )

    # Vérifie dès la connexion si api_key.txt existe
    if not load_api_key():
        app_instance.awaiting_api_key = True
        app_instance.log(
            "🔑 Aucune clé API détectée (fichier api_key.txt absent).\n"
            "👉 Colle ta clé API OpenRouter ci-dessous et envoie-la pour l'enregistrer définitivement :\n",
            "agent"
        )

    buffer = ""
    while True:
        try:
            data = conn.recv(4096)
            if not data:
                break

            buffer += data.decode('utf-8')
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue

                try:
                    msg = json.loads(line)
                except Exception:
                    msg = {"type": "instruction", "content": line}

                msg_type = msg.get("type", "instruction")

                # 1. ARRÊT D'URGENCE IMMÉDIAT
                if msg_type == "stop":
                    app_instance.stop_requested = True
                    app_instance.human_response = "Action annulée par l'utilisateur (STOP NET)."
                    app_instance.wait_event.set()
                    app_instance.log("🛑 Commande STOP reçue. Interruption forcée.\n", "system")

                # 2. DEMANDE DE CHANGEMENT DE CLÉ API DEPUIS LE BOUTON 🔑
                elif msg_type == "reset_key":
                    app_instance.awaiting_api_key = True
                    app_instance.log("🔑 Entre ta nouvelle clé API OpenRouter pour remplacer l'ancienne :\n", "agent")

                # 3. CHANGEMENT DE MOTEUR IA (TERRA <-> LITE CLAUDE HAIKU 5.5)
                elif msg_type == "set_mode":
                    requested_mode = str(msg.get("mode", "terra")).lower().strip()
                    if requested_mode == "lite":
                        if HAS_LITE_ENGINE:
                            app_instance.engine_mode = "lite"
                            app_instance.log("⚡ Bascule sur le Mode LITE (Claude Haiku 5.5 - agent_core.py) : idéal pour le tri de dossiers et Cisco !\n", "system")
                        else:
                            app_instance.log("⚠️ Impossible d'activer le Mode Lite : le fichier 'agent_core.py' est absent sur la VM.\n", "error")
                    else:
                        app_instance.engine_mode = "terra"
                        app_instance.log("🚀 Bascule sur le Mode TERRA (OpenAI GPT-6.1 Terra) : idéal pour la bureautique, PDF et navigation complète !\n", "system")

                # 4. INSTRUCTION, CLÉ API OU RÉPONSE
                elif msg_type in ["instruction", "task"]:
                    content = msg.get("content", "").strip()

                    # Raccourcis texte pratiques dans le chat : /lite ou /terra
                    if content.lower() in ["/lite", "!lite"]:
                        if HAS_LITE_ENGINE:
                            app_instance.engine_mode = "lite"
                            app_instance.log("⚡ Mode LITE (Claude Haiku 5.5) activé.\n", "system")
                        else:
                            app_instance.log("⚠️ 'agent_core.py' est introuvable sur la VM.\n", "error")
                        continue
                    elif content.lower() in ["/terra", "!terra"]:
                        app_instance.engine_mode = "terra"
                        app_instance.log("🚀 Mode TERRA (GPT-6.1 Terra) activé.\n", "system")
                        continue

                    # Cas A : Saisie de la clé API
                    if app_instance.awaiting_api_key:
                        save_api_key(content)
                        app_instance.awaiting_api_key = False
                        app_instance.log("✅ Clé API enregistrée dans 'api_key.txt' ! Tu peux lancer tes tâches.\n", "system")

                        if app_instance.pending_task:
                            t = app_instance.pending_task
                            app_instance.pending_task = None
                            start_agent_task(t, app_instance)
                        continue

                    # Cas B : api_key.txt manquant
                    if not load_api_key():
                        app_instance.awaiting_api_key = True
                        app_instance.pending_task = content
                        app_instance.log("🔑 Clé API introuvable. Colle ta clé API OpenRouter pour lancer cette tâche :\n", "agent")
                        continue

                    # Cas C : Attente ask_human
                    if app_instance.agent_running and not app_instance.wait_event.is_set():
                        app_instance.human_response = content
                        app_instance.wait_event.set()
                    else:
                        # Cas D : Nouvelle tâche normale
                        start_agent_task(content, app_instance)

                elif msg_type == "answer":
                    app_instance.human_response = msg.get("content", "")
                    app_instance.wait_event.set()

        except Exception as e:
            print(f"[Réseau] Erreur de communication : {e}")
            break

    print("[Réseau] PC Principal déconnecté.")
    conn.close()


def get_real_local_ip():
    """Récupère la véritable adresse IP réseau de la VM (évite 127.0.0.1 ou les interfaces secondaires)."""
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

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(1)

    print("=" * 55)
    print(" 🤖 AGENT SERVEUR PRÊT (Dans la VM)")
    print("=" * 55)
    print(f"👉 ADRESSE IP VM : {local_ip}")
    print(f"👉 PORT          : {port}")
    print(f"🧠 MOTEURS DISPO : Terra (Défaut) | Lite Claude ({'OUI' if HAS_LITE_ENGINE else 'NON'})")
    if load_api_key():
        print("🔑 Clé API       : Chargée depuis api_key.txt")
    else:
        print("🔑 Clé API       : Aucune (sera demandée au client)")
    print("=" * 55)
    print("En attente de connexion depuis le PC principal...")

    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
