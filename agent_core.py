import base64
import ctypes
import io
import json
import os
import random
import time
import mss
from PIL import Image
from openai import OpenAI
import pyautogui
import pygetwindow as gw
import pyperclip
import re

# 1. Correction du bug multi-écrans : Coordonnées physiques réelles (DPI Aware)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# Arrêt d'urgence : curseur tout en haut à gauche
pyautogui.FAILSAFE = True

# Gestionnaire multi-écrans
sct = mss.mss()
CURRENT_MONITOR = 1  # 1 = Écran principal, 2 = Deuxième écran

# --- GESTION DYNAMIQUE DE LA CLÉ API (SANS CLÉ PRÉ-ENREGISTRÉE) ---
API_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_key.txt")
client = None

def load_api_key():
    """Lit la clé API depuis le fichier texte si elle existe."""
    if os.path.exists(API_KEY_FILE):
        try:
            with open(API_KEY_FILE, "r", encoding="utf-8") as f:
                key = f.read().strip()
                if key:
                    return key
        except Exception as e:
            print(f"[Erreur] Lecture de api_key.txt impossible : {e}")
    return None

def save_api_key(key):
    """Enregistre la clé API dans api_key.txt et initialise le client."""
    clean_key = key.strip()
    with open(API_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(clean_key)
    init_openai_client(clean_key)
    return clean_key

def init_openai_client(api_key=None):
    """Initialise le client OpenRouter avec la clé fournie ou sauvegardée."""
    global client
    if not api_key:
        api_key = load_api_key()
    if not api_key:
        client = None
        return None
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "https://localhost",
            "X-Title": "Agent_Local_Windows"
        }
    )
    return client

# Initialisation au chargement si le fichier existe déjà
init_openai_client()
# ------------------------------------------------------------------

def prepare_task_data(task):
    if client is None and init_openai_client() is None:
        return None

    network_keywords = ["cisco", "routeur", "switch", "vlan", "ip", "interface", "packet tracer", "réseau", "ping"]
    is_network_task = any(kw in task.lower() for kw in network_keywords)
    
    if is_network_task:
        print("[Arrière-plan] Préparation (Mode Expert Cisco) en cours...")
        sys_content = (
            "Tu es un assistant de préparation de données et un expert certifié Cisco CCNA / BTS SIO SISR. "
            "Analyse la tâche de l'utilisateur et renvoie UNIQUEMENT un objet JSON valide "
            "contenant les textes exacts qui devront être tapés, collés ou cherchés. "
            "SPÉCIALITÉ CISCO : Si la tâche implique de configurer un routeur ou un switch Cisco (VLAN, trunk, "
            "router-on-a-stick dot1Q, DHCP, port-security, routage, etc.), "
            "génère les blocs complets de commandes Cisco IOS exactes, séparées par '\\n', en commençant par "
            "'enable\\nconfigure terminal' et en terminant par 'end\\nwrite memory'. "
            "Si la tâche ne nécessite aucun texte ou script à rédiger, renvoie {}. "
            "N'ajoute aucun texte avant ou après le JSON. "
            "Exemple : {\"config_switch0\": \"enable\\nconfigure terminal\\nvlan 10\\nname Commercial\\nexit\\nend\\nwrite memory\"}"
        )
    else:
        print("[Arrière-plan] Préparation (Mode Basique) en cours...")
        sys_content = (
            "Tu es un assistant de préparation de données. "
            "Analyse la tâche de l'utilisateur et renvoie UNIQUEMENT un objet JSON valide "
            "contenant les textes exacts qui devront être tapés ou cherchés. "
            "RÈGLE ABSOLUE : Tu dois conserver l'orthographe EXACTE demandée par l'utilisateur (argot, blagues, fautes volontaires, mots inventés). "
            "Ne corrige et ne modifie JAMAIS les mots de l'utilisateur. "
            "N'ajoute aucun texte avant ou après le JSON."
        )

    try:
        response = client.chat.completions.create(
            model="cohere/north-mini-code:free",
            messages=[
                {"role": "system", "content": sys_content},
                {"role": "user", "content": f"Tâche : {task}"}
            ]
        )
        content = response.choices[0].message.content.strip()
        
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()
            
        return json.loads(content)
    except Exception as e:
        print(f"[Arrière-plan] Avertissement ou pas de script complexe : {e}")
        return None

def get_active_monitor_info():
    global CURRENT_MONITOR
    if CURRENT_MONITOR >= len(sct.monitors):
        CURRENT_MONITOR = 1
    return sct.monitors[CURRENT_MONITOR]

def capture_screen():
    monitor = get_active_monitor_info()
    sct_img = sct.grab(monitor)
    screenshot = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
    screenshot = screenshot.resize((1280, 720))
    buffered = io.BytesIO()
    screenshot.save(buffered, format="JPEG", quality=95, optimize=True)
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

tools = [
    {
        "type": "function",
        "function": {
            "name": "ask_human",
            "description": "OUTIL DE SECOURS DE DERNIER RECOURS. Mets ton exécution en pause et pose une question à l'utilisateur humain dans l'interface de chat UNIQUEMENT si une information critique manque totalement (mot de passe, identifiant inconnu, site bloqué) ou après plusieurs échecs de clics. INTERDICTION de l'utiliser pour demander confirmation sur un choix de nom de fichier, de dossier, ou une déduction logique évidente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "La question précise à poser à l'humain."}
                },
                "required": ["question"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_clipboard",
            "description": "Lit directement le texte présent dans le presse-papiers Windows. À utiliser après avoir fait un focus dans une zone de texte, un document Word, Excel, un mail ou une page web, puis exécuté press_shortcut avec ['ctrl', 'a'] et ['ctrl', 'c']. Permet d'ingérer de longs textes instantanément sans scroller ni faire d'erreurs d'OCR.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_clipboard",
            "description": "Copie un bloc de texte (comme un tableau TSV avec des \\t et \\n) directement dans le presse-papiers Windows. UTILISATION OBLIGATOIRE POUR EXCEL : formatte tes données en tableau, utilise cet outil, clique sur la cellule A1, puis fais un press_shortcut ['ctrl', 'v'] pour tout remplir instantanément.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_window_open",
            "description": "Vérifie si une fenêtre contenant un nom donné est ouverte sur Windows et indique sur quel écran elle se trouve.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"}
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "switch_monitor",
            "description": "Change l'écran observé par l'agent (1 ou 2).",
            "parameters": {
                "type": "object",
                "properties": {
                    "monitor_number": {"type": "integer", "enum": [1, 2]}
                },
                "required": ["monitor_number"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_click",
            "description": "Effectue un clic simple sur l'écran actuellement observé (repère 1280x720).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right"], "default": "left"}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_click_sequence",
            "description": "OUTIL SPÉCIAL RAFALE : Effectue une série de clics rapides pour les actions GUI ordinaires (changement d'onglet, sélection ou placement de plusieurs équipements). NE PAS utiliser pour câbler des appareils : utiliser connect_multiple_devices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "clicks": {
                        "type": "array",
                        "description": "Liste ordonnée des coordonnées à cliquer à la suite.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "button": {"type": "string", "enum": ["left", "right"], "default": "left"}
                            },
                            "required": ["x", "y"]
                        }
                    }
                },
                "required": ["clicks"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "connect_multiple_devices",
            "description": "OUTIL OBLIGATOIRE POUR LE CABLAGE PACKET TRACER. Regroupe toutes les liaisons connues dans un seul appel. auto_x et auto_y doivent viser UNIQUEMENT l'icone de connexion automatique situee dans la rangee d'icones juste AU-DESSUS du libelle exact 'Automatically Choose Connection Type'. Ne clique pas sur le texte du libelle, sur une icone sous la barre de recherche ni sur un type de cable specifique. Pour CHAQUE liaison, l'outil reclique cette meme icone, puis clique sur l'appareil A et l'appareil B.",
            "parameters": {
                "type": "object",
                "properties": {
                    "auto_x": {"type": "integer"},
                    "auto_y": {"type": "integer"},
                    "connections": {
                        "type": "array",
                        "description": "Toutes les liaisons a effectuer, dans l'ordre, en une seule action.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "device_a": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "integer"},
                                        "y": {"type": "integer"}
                                    },
                                    "required": ["x", "y"]
                                },
                                "device_b": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "integer"},
                                        "y": {"type": "integer"}
                                    },
                                    "required": ["x", "y"]
                                }
                            },
                            "required": ["device_a", "device_b"]
                        }
                    }
                },
                "required": ["auto_x", "auto_y", "connections"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "double_click",
            "description": "Effectue un double-clic gauche rapide.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "drag_and_drop",
            "description": "Maintient le clic gauche enfoncé depuis (start_x, start_y) et glisse jusqu'à (end_x, end_y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer"},
                    "start_y": {"type": "integer"},
                    "end_x": {"type": "integer"},
                    "end_y": {"type": "integer"}
                },
                "required": ["start_x", "start_y", "end_x", "end_y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "press_shortcut",
            "description": "Exécute un raccourci clavier (ex: ['ctrl', 't'], ['enter'], ['esc'], ['ctrl', 'a'], ['ctrl', 'c'], ['ctrl', 'v']).",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": ["keys"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_scroll",
            "description": "Place le curseur en (x, y) et fait tourner la molette.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "amount": {"type": "integer", "default": 5}
                },
                "required": ["x", "y", "direction"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Saisie clavier manuelle rapide, sans presse-papiers. Accepte plusieurs champs/lignes dans un seul appel : utilise \\t pour passer au champ suivant et \\n pour valider une ligne ou une commande, seulement si l'ordre de navigation est certain. ATTENTION : NE PAS UTILISER POUR LES TABLEAUX EXCEL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "press_enter": {"type": "boolean", "default": False}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_cisco_cli_block",
            "description": "Envoie du texte ou des commandes dans un terminal/CLI ouvert (Cisco IOS ou autre CLI). Avant l’appel, le curseur doit être dans la zone de saisie. Après l’envoi, vérifie visuellement que le texte apparaît. Si rien n’apparaît, clique à la fin de la dernière ligne visible du terminal, puis réessaie une seule fois.",
            "parameters": {
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "string",
                        "description": "Bloc de commandes Cisco IOS séparées par des sauts de ligne \\n (ou simplement 'no' pour sortir du dialogue initial)"
                    }
                },
                "required": ["commands"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Patiente quand un équipement démarre ou charge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "number"}
                },
                "required": ["seconds"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "À appeler dès que la tâche demandée est totally terminée.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"}
                },
                "required": ["message"]
            }
        }
    }
]

def move_mouse_smoothly(real_x, real_y):
    current_x, current_y = pyautogui.position()
    distance = ((real_x - current_x) ** 2 + (real_y - current_y) ** 2) ** 0.5
    move_duration = max(0.10, min(0.65, distance / 1800))
    pyautogui.moveTo(
        real_x,
        real_y,
        duration=move_duration,
        tween=pyautogui.easeInOutQuad
    )

def execute_action(name, args, app_instance=None):
    global CURRENT_MONITOR
    monitor = get_active_monitor_info()
    mon_left = monitor["left"]
    mon_top = monitor["top"]
    mon_w = monitor["width"]
    mon_h = monitor["height"]
    
    if name == "ask_human":
        question = args["question"]
        if app_instance:
            app_instance.log(f"🤖 L'agent a besoin d'aide : {question}\n", "agent")
            app_instance.wait_for_human()
            reponse = app_instance.human_response
            app_instance.log(f"👤 Vous avez répondu : {reponse}\n", "user")
            return f"L'utilisateur a répondu : {reponse}"
        else:
             print(f"🤖 L'agent a besoin d'aide : {question}")
             return "Impossible de contacter l'utilisateur."

    elif name == "read_clipboard":
        try:
            content = pyperclip.paste()
            limite = 15000
            if len(content) > limite:
                return f"Contenu du presse-papiers ingéré (tronqué aux {limite} premiers caractères) :\n\n{content[:limite]}"
            return f"Contenu du presse-papiers ingéré avec succès :\n\n{content}"
        except Exception as e:
            return f"Erreur lors de la lecture du presse-papiers : {e}"

    elif name == "write_clipboard":
        try:
            pyperclip.copy(args["text"])
            return "Texte chargé dans le presse-papiers. Prêt à être collé (press_shortcut ['ctrl', 'v'])."
        except Exception as e:
            return f"Erreur du presse-papiers : {e}"

    elif name == "switch_monitor":
        target_mon = int(args["monitor_number"])
        if target_mon < len(sct.monitors):
            CURRENT_MONITOR = target_mon
            return f"Vision basculée avec succès."
        return "Erreur d'écran."

    elif name == "mouse_click":
        real_x = mon_left + int(args["x"] * mon_w / 1280)
        real_y = mon_top + int(args["y"] * mon_h / 720)
        button = args.get("button", "left")
        move_mouse_smoothly(real_x, real_y)
        time.sleep(0.04)
        pyautogui.click(button=button)
        return "Clic effectué."

    elif name == "mouse_click_sequence":
        for click_data in args["clicks"]:
            if app_instance and app_instance.stop_requested:
                return "Interrompu par l'utilisateur."
            real_x = mon_left + int(click_data["x"] * mon_w / 1280)
            real_y = mon_top + int(click_data["y"] * mon_h / 720)
            button = click_data.get("button", "left")
            move_mouse_smoothly(real_x, real_y)
            time.sleep(0.04)
            pyautogui.click(button=button)
            time.sleep(0.055)
        return f"Séquence de {len(args['clicks'])} clics exécutée d'un coup avec succès."

    elif name == "connect_multiple_devices":
        auto_x = mon_left + int(args["auto_x"] * mon_w / 1280)
        auto_y = mon_top + int(args["auto_y"] * mon_h / 720)
        connections = args.get("connections", [])

        if not connections:
            return "Aucune liaison fournie."

        for index, connection in enumerate(connections, start=1):
            if app_instance and app_instance.stop_requested:
                return "Interrompu par l'utilisateur."
            move_mouse_smoothly(auto_x, auto_y)
            time.sleep(0.04)
            pyautogui.click(button="left")
            time.sleep(0.20)

            for device in (connection["device_a"], connection["device_b"]):
                if app_instance and app_instance.stop_requested:
                    return "Interrompu par l'utilisateur."
                real_x = mon_left + int(device["x"] * mon_w / 1280)
                real_y = mon_top + int(device["y"] * mon_h / 720)
                move_mouse_smoothly(real_x, real_y)
                time.sleep(0.04)
                pyautogui.click(button="left")
                time.sleep(0.16)
            time.sleep(0.20)

        return (f"Lot termine : {len(connections)} liaison(s) envoyee(s). "
                "Observe une nouvelle capture pour verifier visuellement les cables.")

    elif name == "double_click":
        real_x = mon_left + int(args["x"] * mon_w / 1280)
        real_y = mon_top + int(args["y"] * mon_h / 720)
        move_mouse_smoothly(real_x, real_y)
        time.sleep(0.04)
        pyautogui.doubleClick(button="left")
        return "Double-clic effectué."

    elif name == "drag_and_drop":
        start_real_x = mon_left + int(args["start_x"] * mon_w / 1280)
        start_real_y = mon_top + int(args["start_y"] * mon_h / 720)
        end_real_x = mon_left + int(args["end_x"] * mon_w / 1280)
        end_real_y = mon_top + int(args["end_y"] * mon_h / 720)
        
        move_mouse_smoothly(start_real_x, start_real_y)
        time.sleep(0.08)
        pyautogui.mouseDown(button="left")
        time.sleep(0.08)
        dist = ((end_real_x - start_real_x) ** 2 + (end_real_y - start_real_y) ** 2) ** 0.5
        drag_duration = max(0.35, min(0.75, dist / 1500))
        pyautogui.moveTo(end_real_x, end_real_y, duration=drag_duration, tween=pyautogui.easeInOutQuart)
        time.sleep(0.08)
        pyautogui.mouseUp(button="left")
        return "Glisser-déposer réussi."

    elif name == "press_shortcut":
        keys = [k.lower().strip() for k in args.get("keys", [])]
        pyautogui.hotkey(*keys)
        time.sleep(0.12)
        return "Raccourci exécuté."

    elif name == "mouse_scroll":
        real_x = mon_left + int(args["x"] * mon_w / 1280)
        real_y = mon_top + int(args["y"] * mon_h / 720)
        direction = args.get("direction", "down")
        amount = int(args.get("amount", 5))
        
        move_mouse_smoothly(real_x, real_y)
        time.sleep(0.06)
        step_scroll = 120 if direction == "up" else -120
        for _ in range(amount):
            if app_instance and app_instance.stop_requested:
                return "Interrompu par l'utilisateur."
            pyautogui.scroll(step_scroll)
            time.sleep(0.02)
        return "Défilement effectué."
        
    elif name == "type_text":
        text = args["text"]
        old_pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0
        try:
            parts = re.split(r'(\n|\t)', text)
            for part in parts:
                if app_instance and app_instance.stop_requested:
                    return "Interrompu par l'utilisateur."
                if not part:
                    continue
                if part == '\n':
                    pyautogui.press('enter')
                    time.sleep(0.12)
                elif part == '\t':
                    pyautogui.press('tab')
                    time.sleep(0.08)
                else:
                    chunks = re.findall(r'\S+|\s+', part)
                    for chunk in chunks:
                        if app_instance and app_instance.stop_requested:
                            return "Interrompu par l'utilisateur."
                        pyperclip.copy(chunk)
                        time.sleep(0.02)
                        pyautogui.hotkey('ctrl', 'v')
                        time.sleep(0.03)
        finally:
            pyautogui.PAUSE = old_pause

        if args.get("press_enter", False):
            pyautogui.press("enter")
            
        return "Texte rédigé avec succès (Frappe organique par blocs)."

    elif name == "send_cisco_cli_block":
        commands_raw = args["commands"]
        lines = [line.strip() for line in commands_raw.splitlines() if line.strip()]
        old_pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0

        if len(lines) == 1 and lines[0].lower() in ["no", "n"]:
            try:
                pyautogui.write("no", interval=0)
                pyautogui.press("enter")
                time.sleep(0.8)
                pyautogui.press("enter")
                time.sleep(0.25)
            finally:
                pyautogui.PAUSE = old_pause
            return "Texte 'no' tapé manuellement."

        pyautogui.press("enter")
        time.sleep(0.35)

        config_markers = {"configure terminal", "conf t", "conf terminal"}
        has_config_mode = any(line.strip().lower() in config_markers for line in lines)
        if lines and lines[0].strip().lower() in config_markers:
            lines.insert(0, "enable")
        elif has_config_mode and lines and lines[0].strip().lower() not in {"enable", "en"}:
            lines.insert(0, "enable")

        if has_config_mode and not any("no ip domain-lookup" in line.lower() for line in lines):
            for i, line in enumerate(lines):
                if line.strip().lower() in config_markers:
                    lines.insert(i + 1, "no ip domain-lookup")
                    break

        try:
            for line in lines:
                if app_instance and app_instance.stop_requested:
                    return "Interrompu par l'utilisateur."
                for char in line:
                    pyautogui.write(char, interval=0)
                    time.sleep(0.004)
                pyautogui.press("enter")
                time.sleep(0.28)
        finally:
            pyautogui.PAUSE = old_pause

        time.sleep(0.25)
        return f"{len(lines)} ligne(s) tapée(s) manuellement."

    elif name == "wait":
        duree = min(max(float(args.get("seconds", 2)), 0.5), 10.0)
        pas = 0.2
        ecoule = 0.0
        while ecoule < duree:
            if app_instance and app_instance.stop_requested:
                return "Attente interrompue par l'utilisateur."
            time.sleep(pas)
            ecoule += pas
        return "Attente terminée."
        
    elif name == "check_window_open":
        target = args["title"].lower()
        windows = [w for w in gw.getAllWindows() if w.title.strip() and target in w.title.lower()]
        if windows:
            return "Fenêtre ouverte."
        return "Fenêtre introuvable."

    elif name == "finish_task":
        if app_instance:
             app_instance.log(f"✅ Mission accomplie : {args['message']}\n", "system")
        return "FIN"
        
    return "Action non reconnue"

def run_agent(task, app_instance=None):
    if client is None and init_openai_client() is None:
        if app_instance:
            app_instance.log("🔑 Erreur : Aucune clé API enregistrée.\n", "error")
        return

    nb_ecrans = len(sct.monitors) - 1
    consigne_base = (
        "Tu es un agent autonome contrôlant Windows, capable d'accomplir n'importe quelle mission bureautique, réseau Cisco, web, téléchargement ou recherche de façon totalement autonome. "
        f"L'ordinateur possède {nb_ecrans} écran(s). Tu observes l'écran {CURRENT_MONITOR}.\n\n"
        "🟢 RÈGLE D'OR : NAVIGATION WEB PROFONDE & RECHERCHE ACTIVE :\n"
        "1. INTERDICTION DE S'ARRÊTER AUX RÉSULTATS DE RECHERCHE : Une page Google/Brave Search n'est qu'un tremplin. Il est FORMELLEMENT INTERDIT d'appeler 'finish_task' depuis une page de moteur de recherche si des détails précis (pilotes, prix exacts, caractéristiques, fichiers à télécharger) sont demandés. Tu DOIS cliquer sur le lien le plus pertinent pour entrer sur le site source.\n"
        "2. GESTION DES COOKIES & CHARGEMENTS : En entrant sur un site, ferme immédiatement les bandeaux de cookies (bouton 'Accepter', 'Refuser' ou touche 'esc'), attends avec wait si nécessaire, puis explore.\n"
        "3. EXHAUSTIVITÉ DU CAHIER DES CHARGES : Avant d'appeler 'finish_task', relis mentalement la tâche initiale. Si l'utilisateur a demandé plusieurs éléments, n'arrête pas avant d'avoir navigué pour tous les obtenir.\n"
        "4. INGESTION PAR LE PRESSE-PAPIERS : Dès que tu arrives sur la page cible, fais un clic au centre, presse ['ctrl', 'a'], ['ctrl', 'c'], puis appelle 'read_clipboard'. Cela t'évite de scroller et te donne l'intégralité des données d'un coup.\n\n"
        "🟢 AUTONOMIE, PRISE D'INITIATIVE ET ZÉRO HÉSITATION :\n"
        "Tu as l'autorisation stricte d'agir seul et de déduire les informations logiques sans demander confirmation.\n"
        "1. NAVIGUABILITÉ & GESTION DES FENÊTRES : Ne demande JAMAIS d'aide pour changer d'application. Utilise la barre des tâches de Windows, ouvre des onglets navigateur avec ['ctrl', 't'], ferme avec ['ctrl', 'w'], clique pour basculer d'une fenêtre à une autre.\n"
        "2. INITIATIVE BUREAUTIQUE & FICHIERS : Ne demande JAMAIS comment nommer un fichier, dans quel dossier le ranger ou comment trier un cours. Analyse le contexte et nomme-le de manière logique (ex: 'Cours_Chapitre1_Reseau.docx'). Crée des dossiers pertinents par matière/chapitre sur le bureau ou dans les documents si nécessaire.\n"
        "3. RECHERCHE WEB & PROJETS : Si l'utilisateur te demande de chercher des composants (ex: config PC), des cours ou de traiter des mails, explore les sites, compare les prix/caractéristiques toi-même, sélectionne les options cohérentes avec le budget et synthétise le résultat sans hésitation.\n"
        "4. DÉDUCTION RÉSEAU : Si tu configures un équipement et qu'il te manque un détail mineur, déduis-le logiquement à partir des scripts en arrière-plan ou des éléments déjà configurés à l'écran. N'invente pas, connecte les points.\n"
        "5. OUTIL DE SECOURS (ask_human) : N'utilise cet outil qu'en DERNIER RECOURS ABSOLU, uniquement si une donnée critique manque totalement (ex: mot de passe non fourni) ou en cas de blocage physique persistant. Ne l'utilise jamais pour valider un nom, un titre ou une étape intermédiaire.\n"
        "6. ADAPTABILITÉ ET DÉCOUVERTE D'OUTILS WEB : Si ta méthode actuelle pour traiter une information ou un fichier échoue ou détruit le formatage (ex: éditer un PDF dans Google Docs déforme le texte), abandonne cette méthode. Ouvre le navigateur Brave (n'utilise JAMAIS Edge) et cherche un outil web gratuit spécialisé (ex: 'éditeur PDF en ligne', 'convertisseur web'). LIMITATION CRITIQUE : Cette permission de chercher des alternatives s'applique uniquement à la bureautique et aux tâches web. Ne cherche JAMAIS de logiciel alternatif pour un programme métier installé comme Cisco Packet Tracer ; pour ce dernier, tu dois résoudre le problème en interne.\n"
        "7. GESTION DES TRAITEMENTS DE TEXTE (WORD / DOCS) : N'essaie JAMAIS d'obtenir une mise en page parfaite au millimètre près. Si un logiciel comme Word Online transforme une ligne en liste à puces ou ajoute un retrait, IGNORE-LE et poursuis ta tâche. Il est FORMELLEMENT INTERDIT d'effacer tout le document avec ['ctrl', 'a'] pour le retaper plusieurs fois. Tape le texte une seule fois en bloc, puis appelle directement finish_task.\n"
        "8. EXCEL ET TABLEAUX : Pour remplir un tableur, n'utilise JAMAIS 'type_text' case par case. Formate toutes tes données sous forme de texte TSV (les colonnes séparées par '\\t' et les lignes par '\\n'). Utilise l'outil 'write_clipboard' pour charger ce bloc en mémoire, clique sur la cellule de départ (ex: A1) et utilise 'press_shortcut' avec ['ctrl', 'v']. Excel rangera instantanément chaque mot dans la bonne case.\n\n"
        "⚠️ RÈGLES DE SÉCURITÉ PACKET TRACER (POUR ÉVITER LES BUGS) ⚠️\n"
        "1. INTERDICTION DE LA BARRE DE RECHERCHE : N'utilise JAMAIS la zone de texte 'Search for device' en bas à gauche de Packet Tracer. C'est formellement interdit car cela sélectionne souvent des modèles incompatibles. Tu DOIS cliquer physiquement sur l'icône de la catégorie (ex: 'End Devices' ou 'Network Devices') puis sur le modèle exact souhaité.\n"
        "2. POP-UPS D'ERREUR BLOQUANTS : Si une fenêtre d'erreur apparaît à l'écran (par exemple 'The cable cannot be connected to that port'), cela signifie qu'un modèle incorrect a été placé ou qu'un port est plein. Utilise immédiatement l'outil 'press_shortcut' avec la touche ['esc'] ou ['enter'] pour fermer l'erreur, supprime le mauvais équipement si besoin, et reprends la tâche.\n\n"
        "⚠️ RÈGLE ABSOLUE POUR COMMUNIQUER (ask_human) ⚠️\n"
        "INTERDICTION FORMELLE de poser des questions ou de demander des confirmations dans tes simples réflexions textuelles. L'utilisateur NE PEUT PAS y répondre. "
        "Si tu es bloqué, qu'il te manque une information (comme une adresse e-mail), ou que tu as un doute, tu DOIS OBLIGATOIREMENT appeler la fonction 'ask_human'. C'est le SEUL et UNIQUE moyen d'afficher une boîte de dialogue à l'utilisateur pour qu'il te débloque.\n\n"
        "PLANIFICATION RAPIDE ET REGROUPEMENT DES ACTIONS :\n"
        "Depuis CHAQUE capture d'écran, établis une liste ordonnée des prochaines actions concrètes nécessaires pour atteindre l'objectif, y compris les équipements de catégories différentes, les champs à remplir et les commandes à saisir. "
        "Exécute ensuite toute la séquence dont les cibles sont déjà visibles et dont les coordonnées restent valides, sans demander une capture après chaque action. "
        "Après avoir placé un équipement, ne place pas automatiquement un deuxième du même type : consulte la liste des éléments requis et identifie le prochain élément réellement nécessaire, même s'il est dans une autre catégorie. "
        "Si le prochain élément est dans une autre catégorie, clique sur cette catégorie, sélectionne le modèle correspondant puis place-le à une position distincte, dans la même séquence si les coordonnées sont connues. "
        "Répète ce raisonnement jusqu'à ce qu'une nouvelle capture soit indispensable pour identifier un modèle ou une cible inconnue. N'utilise pas une capture comme excuse pour interrompre une séquence dont les prochaines étapes sont déjà connues. "
        "RÈGLE DE CATÉGORIE PACKET TRACER : pour placer un PC, un Laptop, un Server ou un autre terminal utilisateur, sélectionne la catégorie 'End Devices'. Ne cherche JAMAIS les PC dans 'Miscellaneous'. Pour un switch, va dans 'Network Devices' puis 'Switches'; pour un routeur, 'Network Devices' puis 'Routers'. "
        "Groupe les clics de changement de catégorie, choix du modèle et placement dans mouse_click_sequence lorsque les coordonnées sont connues et stables. "
        "Si la liste des modèles change et que la position du modèle suivant n'est pas certaine, fais une capture à ce seul point, puis reprends la séquence. "
        "Utilise mouse_click_sequence pour plusieurs clics et un seul appel type_text pour saisir une série de valeurs quand l'ordre des champs est prévisible (utilise \\t pour passer au champ suivant, et \\n uniquement pour valider/changer de ligne si c'est le comportement attendu). Pour une CLI, envoie le bloc complet avec send_cisco_cli_block. "
        "Pour placer plusieurs objets, choisis des emplacements clairement séparés; ne clique jamais deux fois au même endroit pour placer deux appareils.\n"
        "L'agent doit rester generaliste : Packet Tracer, le navigateur ou les logiciels bureautiques sont des applications graphiques comme les autres. Utilise les outils GUI disponibles pour toute fonction demandee.\n\n"
        "📚 RÈGLES DE NAVIGATION PACKET TRACER (TOUT SE PASSE EN BAS À GAUCHE) :\n"
        "1. POSER PLUSIEURS ÉQUIPEMENTS D'UN COUP : Avec mouse_click_sequence, enchaîne directement : Clic Catégorie -> Clic Modèle -> Clic Grille (Position 1) -> Clic Catégorie (ou Modèle) -> Clic Grille (Position 2), etc.\n"
        "2. CÂBLAGE PACKET TRACER — RÈGLE OBLIGATOIRE ET DÉTERMINISTE :\n"
        "   - Si nécessaire, clique d'abord sur la catégorie 'Connections' (éclair orange en haut du panneau en bas à gauche), puis prends une nouvelle capture.\n"
        "   - L'outil à sélectionner est UNIQUEMENT l'icône de connexion automatique dans la rangée d'icônes située JUSTE AU-DESSUS du libellé exact 'Automatically Choose Connection Type'. Donne à auto_x/auto_y les coordonnées de cette icône, pas celles du texte. Ne clique JAMAIS sur un éclair sous la barre de recherche, ni sur un câble spécifique.\n"
        "   - Quand plusieurs liaisons sont demandées et que les appareils sont visibles, appelle connect_multiple_devices UNE SEULE FOIS avec toutes les paires d'appareils dans l'ordre. N'utilise JAMAIS mouse_click_sequence, mouse_click ou drag_and_drop pour câbler.\n"
        "   - L'outil connect_multiple_devices clique lui-même sur cette même icône Auto avant CHAQUE liaison, puis sur l'appareil A et l'appareil B.\n"
        "3. TERMINAUX ET CLI (TOUT EQUIPEMENT OU APPLICATION) :\n"
        "   - Quand une fenetre possede un terminal/CLI, clique dans sa zone de saisie avant d'envoyer du texte.\n"
        "   - Si le terminal affiche 'Translating ...', appuie sur Ctrl+C pour interrompre la résolution.\n"
        "Quand toute la tâche demandée est terminée et vérifiée, appelle finish_task."
    )

    messages = [{"role": "system", "content": consigne_base}]
    scripts_injectes = False
    consecutive_no_action = 0  # Compteur pour détecter si l'agent boucle sans agir
    
    # Mécanisme de détection de boucle : stocke les dernières actions identiques
    last_action_signature = None
    repeated_action_count = 0
    
    step = 1
    while True:
        if app_instance and app_instance.stop_requested:
            if app_instance:
                app_instance.log("🛑 Exécution arrêtée net par l'utilisateur.\n", "system")
            break

        if not scripts_injectes and app_instance and app_instance.shared_state.get("done") and app_instance.shared_state.get("data"):
            messages[0]["content"] = consigne_base + (
                f"\n\nIMPORTANT : Voici les scripts Cisco IOS générés en arrière-plan pour toi : "
                f"{json.dumps(app_instance.shared_state['data'], ensure_ascii=False)}. "
                "Utilise 'send_cisco_cli_block' (CLI Cisco) ou 'type_text' (PC/Windows) avec ces textes exacts."
            )
            app_instance.log("[Système] Scripts Cisco prêts et transmis à l'agent.\n", "system")
            scripts_injectes = True

        if app_instance:
             app_instance.log(f"--- Étape {step} ---\n", "system")
        else:
             print(f"--- Étape {step} ---")

        img_b64 = capture_screen()
        
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user" and isinstance(msg.get("content"), list):
                msg["content"] = "[Capture d'écran de l'étape précédente archivée]"
        
        prompt_text = f"Tâche : {task}. Voici la capture actuelle de l'écran {CURRENT_MONITOR}. Analyse tous les éléments visibles et regroupe dans cette réponse toute la séquence fiable."
        if consecutive_no_action > 0:
            prompt_text += " ⚠️ AVERTISSEMENT CRITIQUE : Tu viens de réfléchir à voix haute sans utiliser d'outil. L'utilisateur NE PEUT PAS répondre à ton texte. Si tu as besoin d'une information, TU DOIS OBLIGATOIREMENT exécuter la fonction 'ask_human'. C'est un ordre."
        else:
            prompt_text += " Si tu es bloqué ou qu'il manque une info, appelle impérativement l'outil ask_human."

        user_content = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
        ]
        messages.append({"role": "user", "content": user_content})
        
        try:
            response = client.chat.completions.create(
                model="anthropic/claude-haiku-5.5",
                messages=messages,
                tools=tools,
                tool_choice="auto"
            )
        except Exception as e:
            if app_instance:
                app_instance.log(f"❌ Erreur API OpenRouter (Clé invalide ou crédit épuisé) : {e}\n", "error")
                app_instance.log("🔑 Clique sur l'icône 🔑 en haut de la bulle pour changer de clé API.\n", "system")
            return
        
        msg = response.choices[0].message
        messages.append(msg)
        
        if msg.tool_calls:
            consecutive_no_action = 0
            for tool in msg.tool_calls:
                # Vérification d'arrêt d'urgence avant chaque appel d'outil
                if app_instance and app_instance.stop_requested:
                    app_instance.log("🛑 Action annulée net par le bouton STOP.\n", "system")
                    if hasattr(app_instance, 'agent_running'):
                        app_instance.agent_running = False
                    return

                fn_name = tool.function.name
                fn_args = json.loads(tool.function.arguments)
                
                # --- DÉTECTION DE BOUCLE : Répétition de la même action 3 fois ---
                # On ignore les actions légitimes répétitives comme wait
                if fn_name != "wait":
                    sig = f"{fn_name}:{json.dumps(fn_args, sort_keys=True)}"
                    if sig == last_action_signature:
                        repeated_action_count += 1
                    else:
                        last_action_signature = sig
                        repeated_action_count = 1
                    
                    if repeated_action_count >= 3:
                        msg_loop = (
                            f"Je constate que je tente de faire la même action ({fn_name}) "
                            f"pour la 3ème fois consécutive sans résultat probant. "
                            f"Je m'arrête pour éviter de boucler. Que dois-je faire ?"
                        )
                        if app_instance:
                            app_instance.log(f"⚠️ Détection de boucle infinie sur {fn_name}. Pause forcée.\n", "system")
                        result = execute_action("ask_human", {"question": msg_loop}, app_instance)
                        repeated_action_count = 0
                        last_action_signature = None
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool.id,
                            "content": result
                        })
                        continue
                
                # --- AFFICHAGE EN TEMPS RÉEL DE L'ACTION ---
                if app_instance:
                    app_instance.log(f"🛠️ Action : {fn_name} {fn_args}\n", "agent")
                else:
                    print(f"🛠️ Action : {fn_name} {fn_args}")
                
                result = execute_action(fn_name, fn_args, app_instance)
                
                if result == "FIN" or (app_instance and app_instance.stop_requested):
                    if hasattr(app_instance, 'agent_running'):
                        app_instance.agent_running = False
                    return
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool.id,
                    "content": result
                })
        else:
            consecutive_no_action += 1
            if app_instance:
                 app_instance.log(f"🧠 L'agent réfléchit (sans agir) : {msg.content}\n", "system")
            else:
                 print(f"🧠 L'agent réfléchit (sans agir) : {msg.content}")
            
        time.sleep(0.15)
        step += 1