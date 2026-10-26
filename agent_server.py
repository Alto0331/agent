import sys
import subprocess
import socket
import json
import threading
import os
import shutil

# --- 0. SYSTÈME AUTO-INSTALLATEUR DE DÉPENDANCES ---
DEPENDENCIES = {
    'mss': 'mss',
    'PIL': 'Pillow',
    'openai': 'openai',
    'pyautogui': 'PyAutoGUI',
    'pygetwindow': 'PyGetWindow',
    'pyperclip': 'pyperclip'
}

def installer_dependances():
    print("Vérification des composants nécessaires...")
    paquets_manquants = []
    for module, package in DEPENDENCIES.items():
        try:
            __import__(module)
        except ImportError:
            paquets_manquants.append(package)
    
    if paquets_manquants:
        print(f"Installation automatique des paquets manquants : {', '.join(paquets_manquants)}...")
        try:
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', *paquets_manquants])
            print("Installation terminée avec succès !\n")
        except Exception as e:
            print(f"Erreur critique lors de l'installation : {e}")
            sys.exit(1)
    else:
        print("Tous les composants sont prêts.\n")

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
        print(f"Avertissement : installation automatique de Brave échouée ({e}).")

# Vérifications et installations AVANT d'importer le reste
installer_dependances()
verifier_et_installer_brave()

# Importation du moteur IA et du gestionnaire de clé API
from agent_core import prepare_task_data, run_agent, load_api_key, save_api_key

class RemoteAppInstance:
    """Simule l'interface GUI pour agent_core et communique via TCP."""
    def __init__(self, conn):
        self.conn = conn
        self.stop_requested = False
        self.agent_running = False
        self.awaiting_api_key = False
        self.pending_task = None
        self.shared_state = {"data": None, "done": False}
        self.human_response = ""
        self.wait_event = threading.Event()

    def log(self, message, tag="system"):
        try:
            data = json.dumps({"text": message, "tag": tag}) + "\n"
            self.conn.sendall(data.encode('utf-8'))
        except Exception:
            pass

    def wait_for_human(self):
        self.wait_event.clear()
        self.wait_event.wait()

def start_agent_task(task, app_instance):
    app_instance.log(f"Reçu instruction de la base : {task[:50]}...\n", "system")
    app_instance.stop_requested = False
    app_instance.agent_running = True
    app_instance.shared_state = {"data": None, "done": False}

    def prep(t=task):
        app_instance.shared_state["data"] = prepare_task_data(t)
        app_instance.shared_state["done"] = True
    threading.Thread(target=prep, daemon=True).start()

    def launch(t=task):
        try:
            run_agent(t, app_instance)
        finally:
            app_instance.agent_running = False
    threading.Thread(target=launch, daemon=True).start()

def handle_client(conn, addr):
    print(f"[Réseau] PC Principal connecté depuis {addr}")
    app_instance = RemoteAppInstance(conn)
    app_instance.log("✅ Connecté avec succès au cerveau de l'agent dans la VM !\n", "system")
    
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

                # 3. INSTRUCTION, CLÉ API OU RÉPONSE
                elif msg_type in ["instruction", "task"]:
                    content = msg.get("content", "").strip()
                    
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

if __name__ == "__main__":
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    port = 65432
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("0.0.0.0", port))
    server.listen(1)
    
    print("=" * 50)
    print(" 🤖 AGENT SERVEUR PRÊT (Dans la VM)")
    print("=" * 50)
    print(f"👉 ADRESSE IP VM : {local_ip}")
    print(f"👉 PORT         : {port}")
    if load_api_key():
        print("🔑 Clé API       : Chargée depuis api_key.txt")
    else:
        print("🔑 Clé API       : Aucune (sera demandée au client)")
    print("=" * 50)
    print("En attente de connexion depuis le PC principal...")
    
    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()