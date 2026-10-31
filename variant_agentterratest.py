# -*- coding: utf-8 -*-
"""
agentterratest.py — Moteur d'agent autonome optimisé exclusivement pour OpenAI GPT-6.1 Terra (Flex / Floor)
Compatible à 100% avec agent_server.py et app_client.py.

Optimisations intégrées :
1. Routage OpenRouter Flex (:floor + service_tier="flex") -> 0.50$/M input, 2.50$/M output, 0.025$/M cache read,
   avec bascule automatique sur la route Standard si le délai dépasse 3 secondes.
2. Bridage du raisonnement (reasoning effort="low", exclude=True, temperature=0.0) -> Zéro token gaspillé en bavardage.
3. Alignement du Cache Prompt OpenAI -> Préfixe système 100% statique pour maximiser le Cache Read à 0.025$/M.
4. Saisie Bureautique (Word, Excel, Brave) par bloc Presse-Papiers Unicode -> Orthographe, accents et mise en page intacts.
5. Saisie Cisco Packet Tracer CLI par injection Unicode bas niveau (Win32 SendInput KEYEVENTF_UNICODE) ->
   Frappe manuelle fluide dans l'IOS Cisco sans jamais dépendre du clavier AZERTY/QWERTY ni provoquer de ^V.
6. Édition PDF native via PyMuPDF (fitz) -> Inspection, remplissage de formulaires AcroForm, remplacement de texte
   et insertion d'images/signatures au pixel près sans détruire la mise en page dans Docs/Word.
"""

import os
import sys
import time
import json
import base64
import hashlib
import ctypes
import subprocess
from io import BytesIO

import mss
from PIL import Image, ImageDraw
import pyautogui
import pygetwindow as gw
import pyperclip
from openai import OpenAI

# Importation de PyMuPDF (fitz) avec installation automatique silencieuse si absent sur la VM
try:
    import fitz  # PyMuPDF
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pymupdf", "--quiet"])
        import fitz
    except Exception:
        fitz = None

# Configuration PyAutoGUI sécurisée et réactive
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

# Fichier de stockage local de la clé API (ignoré par Git)
API_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_key.txt")

# --- CONFIGURATION EXCLUSIVE GPT-6.1 TERRA (FLEX / FLOOR) ---
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Le suffixe :floor active le tri par prix le plus bas ET débloque le tier OpenAI Flex (0.50$/M in, 2.50$/M out)
TERRA_MODEL_PRIMARY = "openai/gpt-6.1-terra:floor"
TERRA_MODEL_FALLBACK = "openai/gpt-6.1-terra"

# Résolution envoyée au modèle Vision (optimisée pour le ratio précision/tokens de GPT-6.1 Terra)
VISION_TARGET_WIDTH = 1280
VISION_TARGET_HEIGHT = 720
JPEG_QUALITY = 78


# ==============================================================================
# 1. GESTION DE LA CLÉ API
# ==============================================================================
def load_api_key() -> str:
    """Charge la clé API OpenRouter depuis api_key.txt."""
    if os.path.exists(API_KEY_FILE):
        try:
            with open(API_KEY_FILE, "r", encoding="utf-8") as f:
                key = f.read().strip()
                if key:
                    return key
        except Exception:
            pass
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def save_api_key(api_key: str) -> bool:
    """Sauvegarde la clé API OpenRouter dans api_key.txt."""
    try:
        clean_key = api_key.strip()
        with open(API_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(clean_key)
        os.environ["OPENROUTER_API_KEY"] = clean_key
        return True
    except Exception as e:
        print(f"[Erreur] Impossible d'enregistrer la clé API : {e}")
        return False


def get_openrouter_client() -> OpenAI:
    """Initialise le client OpenAI pointant sur OpenRouter."""
    api_key = load_api_key()
    if not api_key:
        raise ValueError("Clé API OpenRouter manquante (api_key.txt vide ou absent).")
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        timeout=35.0,
        max_retries=1,
        default_headers={
            "HTTP-Referer": "https://github.com/Alto0331/agent",
            "X-Title": "VM Autonomous Agent Terra",
        },
    )


# ==============================================================================
# 2. FRAPPE BAS NIVEAU WIN32 UNICODE (ANTI-BUG AZERTY / QWERTY POUR CISCO CLI)
# ==============================================================================
PUL = ctypes.POINTER(ctypes.c_ulong)


class KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class Input_I(ctypes.Union):
    _fields_ = [
        ("ki", KeyBdInput),
        ("mi", MouseInput),
        ("hi", HardwareInput),
    ]


class Input(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("ii", Input_I),
    ]


KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


def send_unicode_char(char: str):
    """
    Injecte un caractère directement en UTF-16 via l'API Win32 SendInput.
    Avantage majeur : ignore totalement le layout AZERTY/QWERTY de Windows
    et fonctionne comme une vraie frappe manuelle dans Cisco Packet Tracer CLI.
    """
    code = ord(char)
    extra = ctypes.c_ulong(0)

    ii_down = Input_I()
    ii_down.ki = KeyBdInput(0, code, KEYEVENTF_UNICODE, 0, ctypes.pointer(extra))
    x_down = Input(ctypes.c_ulong(INPUT_KEYBOARD), ii_down)

    ii_up = Input_I()
    ii_up.ki = KeyBdInput(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))
    x_up = Input(ctypes.c_ulong(INPUT_KEYBOARD), ii_up)

    ctypes.windll.user32.SendInput(1, ctypes.pointer(x_down), ctypes.sizeof(x_down))
    ctypes.windll.user32.SendInput(1, ctypes.pointer(x_up), ctypes.sizeof(x_up))


def type_unicode_string(text: str, interval: float = 0.005, app_instance=None):
    """Frappe une chaîne caractère par caractère en Unicode pur (idéal pour Cisco IOS CLI)."""
    for ch in text:
        if app_instance and getattr(app_instance, "stop_requested", False):
            break
        if ch == "\r":
            continue
        elif ch == "\n":
            pyautogui.press("enter")
            time.sleep(0.05)
        else:
            send_unicode_char(ch)
            if interval > 0:
                time.sleep(interval)


# ==============================================================================
# 3. CAPTURE D'ÉCRAN & CONVERSION DE COORDONNÉES
# ==============================================================================
def get_screen_size():
    """Retourne la taille réelle de l'écran principal."""
    return pyautogui.size()


def capture_screen(draw_cursor: bool = True):
    """
    Capture l'écran de la VM, dessine un repère visuel sur la position actuelle
    de la souris pour aider GPT-6.1 Terra, et redimensionne en 1280x720.
    Retourne (base64_jpeg, md5_hash, (real_w, real_h)).
    """
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

    real_w, real_h = img.size

    if draw_cursor:
        try:
            mx, my = pyautogui.position()
            draw = ImageDraw.Draw(img)
            r = 10
            draw.ellipse((mx - r, my - r, mx + r, my + r), outline="#ef4444", width=3)
            draw.line((mx - r - 4, my, mx + r + 4, my), fill="#ef4444", width=2)
            draw.line((mx, my - r - 4, mx, my + r + 4), fill="#ef4444", width=2)
        except Exception:
            pass

    # Empreinte rapide pour détecter si l'écran est figé (boucle d'actions)
    small_hash_img = img.resize((64, 36), Image.Resampling.BILINEAR)
    screen_hash = hashlib.md5(small_hash_img.tobytes()).hexdigest()

    # Redimensionnement optimal pour GPT-6.1 Terra Vision
    if (real_w, real_h) != (VISION_TARGET_WIDTH, VISION_TARGET_HEIGHT):
        img_resized = img.resize((VISION_TARGET_WIDTH, VISION_TARGET_HEIGHT), Image.Resampling.LANCZOS)
    else:
        img_resized = img

    buf = BytesIO()
    img_resized.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
    return b64_str, screen_hash, (real_w, real_h)


def resolve_xy(x: float, y: float) -> tuple[int, int]:
    """
    Convertit les coordonnées envoyées par GPT-6.1 Terra depuis la grille 1280x720
    vers la résolution réelle de la VM.
    """
    real_w, real_h = get_screen_size()
    x_val, y_val = float(x), float(y)

    scale_x = real_w / float(VISION_TARGET_WIDTH)
    scale_y = real_h / float(VISION_TARGET_HEIGHT)

    final_x = int(round(x_val * scale_x))
    final_y = int(round(y_val * scale_y))

    final_x = max(2, min(real_w - 3, final_x))
    final_y = max(2, min(real_h - 3, final_y))
    return final_x, final_y


# ==============================================================================
# 4. PRÉPARATION DE TÂCHE & PROMPTS SYSTÈME STATIQUES (CACHE-FRIENDLY)
# ==============================================================================
SYSTEM_PROMPT_TERRA = f"""Tu es un agent d'exécution autonome sur Windows (VM), taillé pour la vitesse et la précision.
La capture d'écran que tu reçois fait exactement {VISION_TARGET_WIDTH}x{VISION_TARGET_HEIGHT} pixels. Toutes tes coordonnées (x, y) doivent être données dans ce repère ({VISION_TARGET_WIDTH}x{VISION_TARGET_HEIGHT}).

🔴 RÈGLE D'OR DE SILENCE ET DE RENTABILITÉ :
- Tu es un automate d'exécution directe, PAS un chatbot bavard.
- N'écris AUCUNE phrase d'explication avant ou après tes appels d'outils.
- Appelle directement l'outil requis. Si plusieurs actions simples sont évidentes, exécute l'action prioritaire immédiatement.
- Si une page web ou une application charge, n'attends jamais plus de 3 secondes (`wait` avec `seconds: 3`).

🟢 QUALITÉ DU TEXTE, ORTHOGRAPHE ET MISE EN PAGE (WORD, EXCEL, BRAVE, BLOC-NOTES) :
1. ORTHOGRAPHE FRANÇAISE IRRÉPROCHABLE : Rédige toujours avec tous les accents (é, è, ê, à, ù, ç), les apostrophes et une ponctuation soignée.
2. INTERDICTION DU MOT-À-MOT : Ne découpe jamais un texte en plusieurs petits appels. Prépare le paragraphe, le document ou le tableau entier et envoie-le en UN SEUL appel `type_text` ou `write_clipboard`.
3. MISE EN PAGE PROPRE :
   - Dans Word / Bloc-notes / Mails : utilise des sauts de ligne clairs (`\n\n` entre les paragraphes, titres nets, listes structurées sans symboles Markdown bruts comme `**` ou `###` sauf si demandé).
   - Dans Excel : pour remplir plusieurs cellules d'un coup, prépare les lignes avec des tabulations `\t` entre les colonnes et `\n` entre les lignes, puis utilise `write_clipboard(text=..., paste=True)`.
4. NAVIGATION WEB : Privilégie toujours le navigateur Brave (`open_app_or_url` avec l'URL ou `brave`) pour éviter les pop-ups.

🟣 RÈGLES D'ÉDITION DE FICHIERS PDF (PYMUPDF / FITZ) :
- N'ouvre JAMAIS un fichier PDF dans Google Docs ou Word pour le modifier (cela détruit la mise en page).
- Utilise TOUJOURS l'outil `edit_pdf` :
  1. Appelle d'abord `edit_pdf(action="inspect", pdf_path="...")` pour lister les champs de formulaire interactifs, le texte existant et leurs coordonnées exactes `(x0, y0, x1, y1)`.
  2. Appelle ensuite `edit_pdf(action="modify", ...)` pour remplir les champs (`form_fields`), remplacer du texte (`replacements`), insérer du texte à des coordonnées précises (`insert_texts`) ou apposer une image/signature (`insert_images`).

🔵 RÈGLES SPÉCIFIQUES CISCO PACKET TRACER :
1. Pour configurer un routeur ou un switch en ligne de commande (onglet CLI), clique d'abord dans la zone blanche/noire du terminal CLI pour lui donner le focus, puis utilise EXCLUSIVEMENT `send_cisco_cli_block`.
2. `send_cisco_cli_block` tape les commandes manuellement en Unicode pur sans aucun bug AZERTY/QWERTY et gère automatiquement `enable` et `no ip domain-lookup`.
3. N'utilise pas `Ctrl+V` dans la CLI Cisco IOS (cela écrit `^V`). Passe toujours par `send_cisco_cli_block`.

🟪 RÈGLES PARSEC (CONNEXION DISTANTE SUR DEMANDE) :
- L'utilisateur peut te demander de te connecter à l'un de ses PC via Parsec (l'application est installée sur la VM).
- Utilise `parsec_connect(target="<nom du PC>")` : Parsec s'ouvre, le PC est recherché, puis double-clique sur le bon ordinateur pour lancer la connexion.
- JUSTE APRÈS le double-clic de connexion, appelle OBLIGATOIREMENT `parsec_confirm_connection` UNE SEULE FOIS : elle clique dans une zone vide pour capter le focus du PC distant et vérifie que le contrôle a basculé. Sans cette confirmation, tes actions s'exécuteraient sur l'hôte et non dans le PC connecté. N'appelle jamais cet outil en boucle.
- Les TOUCHES IMMERSIVES sont activées : une fois connecté, toutes tes actions (clics, saisie clavier) pilotent directement le PC distant. Continue la mission normalement dedans.
- Ne tente JAMAIS de te déconnecter toi-même à la fin d'une action sur le PC distant : l'utilisateur te déconnectera lui-même avec Ctrl+Alt+². Sauf si il te le demande explicitement (`parsec_disconnect`).

🟡 GESTION DES BLOCAGES ET FIN DE MISSION IMMÉDIATE :
- Si une fenêtre de connexion, un CAPTCHA, un mot de passe inconnu ou une ambiguïté critique bloque la mission, appelle `ask_human`.
- INTERDICTION DE VÉRIFIER EN BOUCLE : Dès que tu as effectué les actions demandées par l'utilisateur (ex: épingler une application, valider un formulaire, saisir un texte, modifier un PDF), appelle IMMÉDIATEMENT `finish_task`.
- Ne fais JAMAIS de clics supplémentaires (ni clic gauche, ni clic droit, ni double-clic) pour "vérifier" ou "contrôler" ton propre travail après coup. Fais confiance à tes actions et termine la mission dès la dernière action exécutée.
"""

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "click_xy",
            "description": f"Clique aux coordonnées (x, y) sur la grille {VISION_TARGET_WIDTH}x{VISION_TARGET_HEIGHT}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": f"Coordonnée X (0 à {VISION_TARGET_WIDTH})"},
                    "y": {"type": "integer", "description": f"Coordonnée Y (0 à {VISION_TARGET_HEIGHT})"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                    "clicks": {"type": "integer", "default": 1, "description": "1 pour clic simple, 2 pour double-clic"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "double_click_xy",
            "description": f"Double-clique aux coordonnées (x, y) sur la grille {VISION_TARGET_WIDTH}x{VISION_TARGET_HEIGHT}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "right_click_xy",
            "description": f"Clic droit aux coordonnées (x, y) sur la grille {VISION_TARGET_WIDTH}x{VISION_TARGET_HEIGHT}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag_and_drop",
            "description": "Maintient le clic gauche de (start_x, start_y) jusqu'à (end_x, end_y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer"},
                    "start_y": {"type": "integer"},
                    "end_x": {"type": "integer"},
                    "end_y": {"type": "integer"},
                    "duration": {"type": "number", "default": 0.4},
                },
                "required": ["start_x", "start_y", "end_x", "end_y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Écrit un texte complet (mot, phrase, paragraphe entier avec accents et sauts de ligne) de manière instantanée et sans faute de frappe. Prépare tout le bloc de texte en une seule fois.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Texte complet à saisir (français soigné, accents et retours à la ligne inclus)."},
                    "press_enter": {"type": "boolean", "default": False, "description": "Appuyer sur Entrée à la fin."},
                    "clear_first": {"type": "boolean", "default": False, "description": "Sélectionner tout (Ctrl+A) et effacer avant d'écrire."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_clipboard",
            "description": "Copie un long bloc de texte ou un tableau formaté (colonnes séparées par \\t, lignes par \\n pour Excel/Word) dans le presse-papiers et le colle proprement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Contenu complet à copier/coller."},
                    "paste": {"type": "boolean", "default": True, "description": "Effectuer Ctrl+V immédiatement."},
                    "press_enter": {"type": "boolean", "default": False},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_pdf",
            "description": "Inspecte ou modifie directement un fichier PDF local via PyMuPDF (fitz) en conservant 100% de la mise en page d'origine (formulaires AcroForm, remplacement de texte exact, ajout de texte ou de signature/image).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["inspect", "modify"],
                        "description": "'inspect' pour lire la structure/champs/coordonnées du PDF, 'modify' pour appliquer les modifications.",
                    },
                    "pdf_path": {
                        "type": "string",
                        "description": "Chemin du fichier PDF source (ex: 'C:\\Users\\Utilisateur\\Downloads\\contrat.pdf' ou '~/Downloads/doc.pdf').",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Chemin de sauvegarde du PDF modifié (si omis ou identique à pdf_path, écrase/met à jour le fichier d'origine).",
                    },
                    "form_fields": {
                        "type": "object",
                        "description": "Dictionnaire {nom_du_champ: valeur} pour remplir les champs de formulaire PDF interactifs.",
                    },
                    "replacements": {
                        "type": "array",
                        "description": "Liste de remplacements de texte [{'search': 'ancien texte', 'replace': 'nouveau texte', 'fontsize': 11}].",
                        "items": {
                            "type": "object",
                            "properties": {
                                "search": {"type": "string"},
                                "replace": {"type": "string"},
                                "fontsize": {"type": "number"},
                                "page": {"type": "integer", "description": "Numéro de page (0-indexé, optionnel)."},
                            },
                            "required": ["search", "replace"],
                        },
                    },
                    "insert_texts": {
                        "type": "array",
                        "description": "Liste de textes à insérer [{'page': 0, 'x': 100, 'y': 200, 'text': 'Contenu', 'fontsize': 11}].",
                        "items": {
                            "type": "object",
                            "properties": {
                                "page": {"type": "integer", "default": 0},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "text": {"type": "string"},
                                "fontsize": {"type": "number", "default": 11},
                            },
                            "required": ["x", "y", "text"],
                        },
                    },
                    "insert_images": {
                        "type": "array",
                        "description": "Liste d'images/signatures à apposer [{'page': 0, 'rect': [x0, y0, x1, y1], 'image_path': 'C:\\...\\sig.png'}].",
                        "items": {
                            "type": "object",
                            "properties": {
                                "page": {"type": "integer", "default": 0},
                                "rect": {"type": "array", "items": {"type": "number"}, "description": "[x0, y0, x1, y1] en points PDF"},
                                "image_path": {"type": "string"},
                            },
                            "required": ["rect", "image_path"],
                        },
                    },
                },
                "required": ["action", "pdf_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_cisco_cli_block",
            "description": "Tape manuellement un bloc de commandes IOS dans le terminal CLI de Cisco Packet Tracer (immunisé contre les bugs AZERTY/QWERTY). Sépare les commandes par des sauts de ligne \\n.",
            "parameters": {
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "string",
                        "description": "Commandes Cisco IOS séparées par \\n (ex: 'enable\\nconf t\\nhostname SW1\\nint fa0/1\\nswitchport mode access\\nno shut').",
                    },
                },
                "required": ["commands"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Appuie sur une touche spéciale (enter, tab, esc, backspace, delete, space, up, down, left, right, win, f1..f12).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "presses": {"type": "integer", "default": 1},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hotkey",
            "description": "Exécute un raccourci clavier (ex: ['ctrl', 's'], ['ctrl', 'a'], ['ctrl', 'c'], ['alt', 'f4'], ['win', 'd']).",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Liste des touches du raccourci dans l'ordre.",
                    },
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Fait défiler la molette de la souris (positif = haut, négatif = bas).",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "integer", "description": "Ex: -500 pour descendre, 500 pour monter."},
                    "x": {"type": "integer", "description": "Optionnel: coordonnée X où placer la souris avant de scroller."},
                    "y": {"type": "integer", "description": "Optionnel: coordonnée Y où placer la souris avant de scroller."},
                },
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app_or_url",
            "description": "Lance directement une application Windows (brave, notepad, calc, explorer, winword, excel, cmd) ou ouvre une URL dans Brave sans chercher l'icône sur le bureau.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Nom de l'exécutable, chemin ou URL complète (https://...)."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_window",
            "description": "Met au premier plan et agrandit une fenêtre ouverte contenant ce titre (ex: 'Packet Tracer', 'Word', 'Excel', 'Brave').",
            "parameters": {
                "type": "object",
                "properties": {
                    "title_substring": {"type": "string"},
                },
                "required": ["title_substring"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parsec_connect",
            "description": "Ouvre Parsec, cherche le PC demandé et lance la connexion distante. Les touches immersives sont actives : une fois connecté, toutes les actions (clics, frappe) pilotent le PC distant. L'utilisateur te déconnectera avec Ctrl+Alt+² quand il voudra.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Nom exact ou approximatif du PC auquel se connecter (ex: 'PC-Bureau', 'Laptop-Alto').",
                    },
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parsec_confirm_connection",
            "description": "OBLIGATOIRE après le double-clic de connexion dans Parsec : clique dans une zone vide (centre de l'écran) pour capter le focus du PC distant et vérifie que l'écran distant réagit (comparaison avant/après). Appelle cet outil UNE seule fois après la connexion, jamais en boucle (3 vérifications internes max).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parsec_disconnect",
            "description": "Se déconnecte du PC distant Parsec via le raccourci immersif Ctrl+Alt+² et rend le contrôle à la VM locale.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Attend pendant un chargement de page ou d'application (3 secondes maximum).",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "default": 3.0, "description": "Durée en secondes (max 3s)."},
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_human",
            "description": "Demande de l'aide ou une information manquante à l'utilisateur via la bulle de chat et met l'agent en pause jusqu'à la réponse.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Question claire et précise pour l'utilisateur."},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "Termine immédiatement la mission dès que les actions demandées ont été exécutées, sans faire de clics supplémentaires de vérification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Résumé court de ce qui a été réalisé."},
                },
                "required": ["summary"],
            },
        },
    },
]


def prepare_task_data(task: str) -> dict:
    """
    Fonction appelée en tâche de fond par agent_server.py au lancement d'une instruction.
    Détecte le profil de la tâche (Cisco, PDF, Excel/Word, Web) pour injecter des rappels ciblés
    sans alourdir les appels réseau.
    """
    t_low = task.lower()
    hints = []

    if any(k in t_low for k in ["cisco", "packet tracer", "vlan", "routeur", "router", "switch", "ospf", "dhcp", "ping", "cli", "fa0/", "gig0/"]):
        hints.append(
            "MODE CISCO DÉTECTÉ : Utilise `send_cisco_cli_block` dès que la fenêtre CLI d'un équipement est ouverte et active. "
            "Ne tente jamais de Ctrl+V dans l'IOS Cisco."
        )
    if any(k in t_low for k in ["pdf", "formulaire", "cerfa", "contrat", "facture", "signer", "signature", "remplir"]):
        hints.append(
            "MODE PDF DÉTECTÉ : N'ouvre JAMAIS le PDF dans Google Docs ou Word ! Utilise l'outil `edit_pdf` "
            "(d'abord `action='inspect'` pour voir les champs et coordonnées, puis `action='modify'` pour remplir/remplacer proprement)."
        )
    if any(k in t_low for k in ["excel", "tableau", "cellule", "colonne", "formule", "tableur"]):
        hints.append(
            "MODE EXCEL DÉTECTÉ : Clique sur la cellule de départ (ex: A1) puis utilise `write_clipboard` avec des tabulations `\t` "
            "entre les colonnes et `\n` entre les lignes pour remplir tout le tableau d'un seul coup."
        )
    if any(k in t_low for k in ["parsec", "pc distant", "se connecter au pc", "connecte-toi au pc", "connecte toi au pc", "remote pc"]):
        hints.append(
            "MODE PARSEC DÉTECTÉ : Utilise `parsec_connect` avec le nom exact du PC donné par l'utilisateur. "
            "Les touches immersives sont actives : une fois connecté, pilote le PC distant normalement. "
            "Ne te déconnecte pas tout seul : l'utilisateur le fera avec Ctrl+Alt+²."
        )
    if any(k in t_low for k in ["word", "document", "lettre", "rapport", "rédige", "redige", "texte", "paragraphe", "bloc-notes", "notepad"]):
        hints.append(
            "MODE RÉDACTION DÉTECTÉ : Soigne l'orthographe française, les accents et la ponctuation. "
            "Envoie le texte complet en un seul appel `type_text` (avec `\n\n` pour aérer les paragraphes)."
        )

    return {
        "task": task,
        "hints": "\n".join(hints) if hints else "Exécution standard directe.",
        "timestamp": time.time(),
    }


# ==============================================================================
# 5. EXÉCUTEUR D'ACTIONS (FLUIDE, ROBUSTE, PYMUPDF & IMMUNISÉ AZERTY/QWERTY)
# ==============================================================================
def _find_brave_exe() -> str | None:
    brave_paths = [
        os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for p in brave_paths:
        if os.path.exists(p):
            return p
    return None


def _find_parsec_exe() -> str | None:
    parsec_paths = [
        os.path.expandvars(r"%ProgramFiles%\Parsec\parsec.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Parsec\parsec.exe"),
        os.path.expandvars(r"%LocalAppData%\Parsec\parsec.exe"),
        os.path.expandvars(r"%LocalAppData%\Programs\Parsec\parsec.exe"),
    ]
    for p in parsec_paths:
        if os.path.exists(p):
            return p
    return None


def _parsec_connect(target: str, app_instance=None) -> str:
    """
    Ouvre Parsec, sélectionne le PC demandé et lance la connexion distante.
    Le mode touches immersives est actif : une fois connecté, l'agent pilote le PC distant
    et l'utilisateur le déconnecte avec Ctrl+Alt+².
    """
    parsec_exe = _find_parsec_exe()
    if not parsec_exe:
        return ("Parsec n'est pas installé sur la VM (parsec.exe introuvable). "
                "Demande à l'utilisateur de l'installer via `ask_human`.")

    sub = target.lower().strip()
    wins = [w for w in gw.getAllWindows() if w.title and "parsec" in w.title.lower()]
    if wins:
        w = wins[0]
        try:
            if w.isMinimized:
                w.restore()
            w.activate()
            time.sleep(0.3)
        except Exception:
            pass
    else:
        subprocess.Popen([parsec_exe])
        time.sleep(2.5)
        wins = [w for w in gw.getAllWindows() if w.title and "parsec" in w.title.lower()]
        if wins:
            try:
                wins[0].activate()
            except Exception:
                pass

    computers = [w for w in gw.getAllWindows() if w.title and "parsec" in w.title.lower()]
    if not computers:
        return "Impossible d'ouvrir la fenêtre Parsec. Vérifie l'installation."

    try:
        time.sleep(1.0)
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.5)
        type_unicode_string(target, app_instance=app_instance)
        time.sleep(0.4)
        pyautogui.press("enter")
        time.sleep(1.0)
        pyautogui.press("esc")
        time.sleep(0.5)
    except Exception as e:
        return f"Recherche du PC '{target}' dans Parsec échouée : {e}"

    return (f"Recherche du PC '{target}' lancée dans Parsec. Double-clique sur le bon ordinateur dans la liste "
            "pour lancer la connexion, puis appelle `parsec_confirm_connection` pour valider que le contrôle "
            "a bien basculé sur le PC distant avant de continuer la mission.")


def _parsec_confirm_connection(app_instance=None) -> str:
    """
    Confirme que le contrôle a bien basculé sur le PC distant :
    clic au centre (zone vide) pour capter le focus, puis comparaison des captures
    d'écran avant/après mouvement de souris. Borné à 3 tentatives max (pas de clic en boucle).
    """
    try:
        sw, sh = get_screen_size()
        center_x, center_y = sw // 2, sh // 2

        for attempt in range(1, 4):
            if app_instance and getattr(app_instance, "stop_requested", False):
                return "Confirmation Parsec interrompue par l'utilisateur."

            before = capture_screen(draw_cursor=False)
            pyautogui.moveTo(center_x, center_y, duration=0.25)
            pyautogui.click(center_x, center_y)
            time.sleep(0.8)
            pyautogui.moveRel(60, 60, duration=0.25)
            time.sleep(0.5)
            after = capture_screen(draw_cursor=False)

            if hashlib.md5(before).hexdigest() != hashlib.md5(after).hexdigest():
                return ("✅ Connexion Parsec confirmée : le contrôle est bien sur le PC distant. "
                        "Continue la mission normalement dedans.")
            time.sleep(1.0)

        return ("⚠️ Impossible de confirmer la bascule vers le PC distant après 3 tentatives (écran identique). "
                "Vérifie que la connexion Parsec est établie ou utilise `ask_human`.")
    except Exception as e:
        return f"Erreur lors de la confirmation Parsec : {e}"


def _parsec_disconnect() -> str:
    """Se déconnecte du PC distant via le raccourci immersif Ctrl+Alt+²."""
    try:
        pyautogui.hotkey("ctrl", "alt", "²")
        time.sleep(0.6)
        return "Déconnexion Parsec envoyée (Ctrl+Alt+²). Le contrôle revient à la VM."
    except Exception as e:
        return f"Échec de la déconnexion Parsec : {e}"


def _resolve_pdf_path(raw_path: str) -> str:
    """Résout intelligemment le chemin d'un fichier PDF sur la VM (Bureau, Téléchargements, Documents)."""
    expanded = os.path.expanduser(os.path.expandvars(raw_path.strip().strip('"').strip("'")))
    if os.path.exists(expanded):
        return os.path.abspath(expanded)

    # Si seul le nom du fichier est fourni, cherche dans les dossiers courants de l'utilisateur
    filename = os.path.basename(expanded)
    user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    search_dirs = [
        os.path.join(user_profile, "Downloads"),
        os.path.join(user_profile, "Desktop"),
        os.path.join(user_profile, "Documents"),
        os.path.join(user_profile, "OneDrive", "Desktop"),
        os.path.join(user_profile, "OneDrive", "Documents"),
        os.getcwd(),
    ]
    for d in search_dirs:
        candidate = os.path.join(d, filename)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return expanded


def _handle_edit_pdf(args: dict) -> str:
    """Gère l'inspection et la modification chirurgicale de fichiers PDF via PyMuPDF (fitz)."""
    if fitz is None:
        return "Erreur : La bibliothèque PyMuPDF (fitz) n'est pas installée. Lance `pip install pymupdf`."

    action = str(args.get("action", "inspect")).lower()
    pdf_path = _resolve_pdf_path(args.get("pdf_path", ""))

    if not os.path.exists(pdf_path):
        return f"Erreur : Fichier PDF introuvable au chemin '{pdf_path}'."

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return f"Erreur d'ouverture du PDF '{pdf_path}' : {e}"

    try:
        if action == "inspect":
            report = [f"📄 PDF : {pdf_path} ({len(doc)} page(s))"]
            for p_idx in range(min(len(doc), 8)):
                page = doc[p_idx]
                rect = page.rect
                report.append(f"--- Page {p_idx} (Dimensions: {int(rect.width)}x{int(rect.height)} pts) ---")

                # 1. Champs de formulaire interactifs (AcroForm)
                widgets = list(page.widgets() or [])
                if widgets:
                    report.append("  [Champs de formulaire interactifs] :")
                    for w in widgets[:40]:
                        r = w.rect
                        report.append(
                            f"    - Nom='{w.field_name}' | Type={w.field_type_string} | "
                            f"Valeur='{w.field_value}' | Rect=[{int(r.x0)}, {int(r.y0)}, {int(r.x1)}, {int(r.y1)}]"
                        )

                # 2. Lignes de texte avec coordonnées pour repérage
                blocks = page.get_text("blocks")
                if blocks:
                    report.append("  [Blocs de texte (x0, y0, x1, y1)] :")
                    for b in blocks[:35]:
                        txt_clean = " ".join(str(b[4]).splitlines()).strip()
                        if txt_clean:
                            report.append(f"    - [{int(b[0])}, {int(b[1])}, {int(b[2])}, {int(b[3])}] : {txt_clean[:90]}")

            doc.close()
            return "\n".join(report)

        elif action == "modify":
            changes_log = []

            # 1. Remplissage des champs de formulaire AcroForm
            form_fields = args.get("form_fields") or {}
            if isinstance(form_fields, dict) and form_fields:
                filled_count = 0
                for page in doc:
                    for widget in page.widgets() or []:
                        if widget.field_name in form_fields:
                            val = form_fields[widget.field_name]
                            widget.field_value = str(val) if not isinstance(val, bool) else val
                            widget.update()
                            filled_count += 1
                changes_log.append(f"{filled_count} champ(s) de formulaire rempli(s)")

            # 2. Remplacement de texte exact (masquage propre + réécriture à la même position)
            replacements = args.get("replacements") or []
            if isinstance(replacements, list) and replacements:
                rep_count = 0
                for item in replacements:
                    search_str = str(item.get("search", ""))
                    replace_str = str(item.get("replace", ""))
                    custom_fs = item.get("fontsize")
                    target_page = item.get("page")
                    if not search_str:
                        continue

                    pages_to_scan = [doc[int(target_page)]] if target_page is not None and 0 <= int(target_page) < len(doc) else doc
                    for page in pages_to_scan:
                        hits = page.search_for(search_str)
                        if not hits:
                            continue
                        insert_queue = []
                        for r in hits:
                            fs = float(custom_fs) if custom_fs else max(8.0, round(r.height * 0.78, 1))
                            page.add_redact_annot(r, fill=(1, 1, 1))
                            insert_queue.append((fitz.Point(r.x0, r.y1 - (r.height * 0.18)), replace_str, fs))
                        page.apply_redactions()
                        for pt, txt, fs in insert_queue:
                            page.insert_text(pt, txt, fontsize=fs, fontname="helv", color=(0, 0, 0))
                            rep_count += 1
                changes_log.append(f"{rep_count} remplacement(s) de texte effectué(s)")

            # 3. Insertion de texte libre à des coordonnées (x, y)
            insert_texts = args.get("insert_texts") or []
            if isinstance(insert_texts, list) and insert_texts:
                ins_count = 0
                for item in insert_texts:
                    p_idx = int(item.get("page", 0))
                    if 0 <= p_idx < len(doc):
                        page = doc[p_idx]
                        x = float(item["x"])
                        y = float(item["y"])
                        txt = str(item["text"])
                        fs = float(item.get("fontsize", 11))
                        page.insert_text(fitz.Point(x, y), txt, fontsize=fs, fontname="helv", color=(0, 0, 0))
                        ins_count += 1
                changes_log.append(f"{ins_count} texte(s) inséré(s)")

            # 4. Insertion d'images ou signatures PNG/JPG
            insert_images = args.get("insert_images") or []
            if isinstance(insert_images, list) and insert_images:
                img_count = 0
                for item in insert_images:
                    p_idx = int(item.get("page", 0))
                    rect_coords = item.get("rect", [])
                    img_path = _resolve_pdf_path(str(item.get("image_path", "")))
                    if 0 <= p_idx < len(doc) and len(rect_coords) == 4 and os.path.exists(img_path):
                        page = doc[p_idx]
                        page.insert_image(fitz.Rect(*[float(c) for c in rect_coords]), filename=img_path)
                        img_count += 1
                changes_log.append(f"{img_count} image(s)/signature(s) insérée(s)")

            # Sauvegarde sécurisée (gère aussi l'écrasement du fichier source)
            out_raw = args.get("output_path")
            out_path = _resolve_pdf_path(out_raw) if out_raw else pdf_path

            if os.path.abspath(out_path) == os.path.abspath(pdf_path):
                temp_out = pdf_path + ".tmp_edit.pdf"
                doc.save(temp_out, garbage=3, deflate=True)
                doc.close()
                os.replace(temp_out, pdf_path)
                final_saved = pdf_path
            else:
                doc.save(out_path, garbage=3, deflate=True)
                doc.close()
                final_saved = out_path

            summary_str = ", ".join(changes_log) if changes_log else "aucune modification spécifiée"
            return f"PDF modifié avec succès ({summary_str}) -> Enregistré sous : {final_saved}"

        else:
            doc.close()
            return f"Action edit_pdf inconnue : {action}"
    except Exception as e:
        try:
            doc.close()
        except Exception:
            pass
        return f"Erreur lors du traitement PDF : {e}"


def execute_action(name: str, args: dict, app_instance=None) -> str:
    """Exécute l'outil demandé par GPT-6.1 Terra avec mouvement de souris fluide et délais optimisés."""
    if app_instance and getattr(app_instance, "stop_requested", False):
        return "Interrompu par l'utilisateur."

    try:
        if name == "click_xy":
            rx, ry = resolve_xy(args["x"], args["y"])
            btn = args.get("button", "left")
            clicks = int(args.get("clicks", 1))
            pyautogui.moveTo(rx, ry, duration=0.22, tween=pyautogui.easeOutQuad)
            pyautogui.click(rx, ry, clicks=clicks, interval=0.06, button=btn)
            time.sleep(0.12)
            return f"Clic ({btn} x{clicks}) effectué en ({rx}, {ry})."

        elif name == "double_click_xy":
            rx, ry = resolve_xy(args["x"], args["y"])
            pyautogui.moveTo(rx, ry, duration=0.22, tween=pyautogui.easeOutQuad)
            pyautogui.doubleClick(rx, ry, interval=0.06)
            time.sleep(0.2)
            return f"Double-clic effectué en ({rx}, {ry})."

        elif name == "right_click_xy":
            rx, ry = resolve_xy(args["x"], args["y"])
            pyautogui.moveTo(rx, ry, duration=0.22, tween=pyautogui.easeOutQuad)
            pyautogui.rightClick(rx, ry)
            time.sleep(0.15)
            return f"Clic droit effectué en ({rx}, {ry})."

        elif name == "drag_and_drop":
            sx, sy = resolve_xy(args["start_x"], args["start_y"])
            ex, ey = resolve_xy(args["end_x"], args["end_y"])
            dur = max(0.25, float(args.get("duration", 0.4)))
            pyautogui.moveTo(sx, sy, duration=0.2, tween=pyautogui.easeOutQuad)
            pyautogui.dragTo(ex, ey, duration=dur, button="left", tween=pyautogui.easeInOutQuad)
            time.sleep(0.15)
            return f"Glisser-déposer de ({sx}, {sy}) vers ({ex}, {ey}) terminé."

        elif name == "type_text":
            text = str(args.get("text", ""))
            if not text:
                return "Aucun texte à saisir."

            if args.get("clear_first", False):
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.03)
                pyautogui.press("backspace")
                time.sleep(0.03)

            # Normalisation des sauts de ligne Windows pour que Word/Bloc-notes respectent la mise en page
            normalized_text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            pyperclip.copy(normalized_text)
            time.sleep(0.03)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.08)

            if args.get("press_enter", False):
                pyautogui.press("enter")
                time.sleep(0.06)

            return f"Texte saisi en bloc avec succès ({len(text)} caractères, orthographe et mise en page préservées)."

        elif name == "write_clipboard":
            text = str(args.get("text", ""))
            normalized_text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            pyperclip.copy(normalized_text)
            time.sleep(0.03)
            if args.get("paste", True):
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.1)
            if args.get("press_enter", False):
                pyautogui.press("enter")
                time.sleep(0.06)
            return f"Contenu copié/collé via presse-papiers ({len(text)} caractères)."

        elif name == "edit_pdf":
            return _handle_edit_pdf(args)

        elif name == "send_cisco_cli_block":
            commands_raw = str(args.get("commands", ""))
            lines = [line.strip() for line in commands_raw.splitlines() if line.strip()]
            if not lines:
                return "Aucune commande Cisco à envoyer."

            old_pause = pyautogui.PAUSE
            pyautogui.PAUSE = 0
            try:
                # Cas du dialogue initial "Would you like to enter the initial configuration dialog? [yes/no]:"
                if len(lines) == 1 and lines[0].lower() in ["no", "n"]:
                    type_unicode_string("no", interval=0.01, app_instance=app_instance)
                    pyautogui.press("enter")
                    time.sleep(0.3)
                    pyautogui.press("enter")
                    return "Réponse 'no' envoyée au prompt initial Cisco."

                # Réveil doux du terminal IOS
                pyautogui.press("enter")
                time.sleep(0.12)

                config_markers = {"configure terminal", "conf t", "conf terminal"}
                has_config_mode = any(l.lower() in config_markers for l in lines)

                if has_config_mode and lines[0].lower() not in {"enable", "en"}:
                    lines.insert(0, "enable")

                if has_config_mode and not any("no ip domain-lookup" in l.lower() for l in lines):
                    for idx, l in enumerate(lines):
                        if l.lower() in config_markers:
                            lines.insert(idx + 1, "no ip domain-lookup")
                            break

                for line in lines:
                    if app_instance and getattr(app_instance, "stop_requested", False):
                        return "Interrompu par l'utilisateur pendant la frappe Cisco."
                    # Frappe manuelle en Unicode Win32 : fonctionne nativement dans Cisco CLI sans bug AZERTY/QWERTY
                    type_unicode_string(line, interval=0.005, app_instance=app_instance)
                    time.sleep(0.02)
                    pyautogui.press("enter")
                    time.sleep(0.15)
            finally:
                pyautogui.PAUSE = old_pause

            return f"{len(lines)} commande(s) Cisco IOS tapée(s) avec succès."

        elif name == "press_key":
            key = str(args.get("key", "enter")).lower()
            presses = int(args.get("presses", 1))
            pyautogui.press(key, presses=presses, interval=0.04)
            time.sleep(0.1)
            return f"Touche '{key}' pressée ({presses}x)."

        elif name == "hotkey":
            keys = [str(k).lower() for k in args.get("keys", [])]
            if keys:
                pyautogui.hotkey(*keys)
                time.sleep(0.12)
            return f"Raccourci {'+'.join(keys)} exécuté."

        elif name == "parsec_connect":
            target = str(args.get("target", "")).strip()
            if not target:
                return "Paramètre 'target' manquant : nom du PC distant requis."
            return _parsec_connect(target, app_instance=app_instance)

        elif name == "parsec_disconnect":
            return _parsec_disconnect()

        elif name == "parsec_confirm_connection":
            return _parsec_confirm_connection(app_instance=app_instance)

        elif name == "scroll":
            amount = int(args.get("amount", -500))
            if "x" in args and "y" in args and args["x"] is not None and args["y"] is not None:
                rx, ry = resolve_xy(args["x"], args["y"])
                pyautogui.moveTo(rx, ry, duration=0.18, tween=pyautogui.easeOutQuad)
            pyautogui.scroll(amount)
            time.sleep(0.15)
            return f"Défilement vertical ({amount}) effectué."

        elif name == "open_app_or_url":
            target = str(args.get("target", "")).strip()
            brave_exe = _find_brave_exe()

            if target.lower().startswith(("http://", "https://", "www.")):
                url = target if target.lower().startswith("http") else f"https://{target}"
                if brave_exe:
                    subprocess.Popen([brave_exe, url])
                else:
                    os.startfile(url)
                time.sleep(1.0)
                return f"URL ouverte dans le navigateur : {url}"

            if target.lower() in ["brave", "brave.exe", "browser", "navigateur"] and brave_exe:
                subprocess.Popen([brave_exe])
                time.sleep(0.9)
                return "Navigateur Brave lancé."

            try:
                subprocess.Popen(target, shell=True)
            except Exception:
                os.startfile(target)
            time.sleep(0.9)
            return f"Application ou cible '{target}' lancée."

        elif name == "focus_window":
            sub = str(args.get("title_substring", "")).lower().strip()
            wins = [w for w in gw.getAllWindows() if w.title and sub in w.title.lower()]
            if not wins:
                return f"Aucune fenêtre trouvée contenant '{sub}'."
            w = wins[0]
            try:
                if w.isMinimized:
                    w.restore()
                w.activate()
                time.sleep(0.2)
            except Exception:
                pass
            return f"Fenêtre '{w.title}' mise au premier plan."

        elif name == "wait":
            # Plafonne l'attente de chargement à 3.0 secondes maximum pour garder un rythme fluide
            secs = min(3.0, max(0.2, float(args.get("seconds", 3.0))))
            time.sleep(secs)
            return f"Attente de {secs:.1f}s terminée."

        else:
            return f"Outil inconnu : {name}"

    except Exception as e:
        return f"Erreur lors de l'exécution de {name} : {e}"


# ==============================================================================
# 6. APPEL API OPENROUTER TAILLE SUR MESURE POUR GPT-6.1 TERRA (FLEX 3s -> STANDARD)
# ==============================================================================
def call_terra_model(client: OpenAI, messages: list):
    """
    Tente toujours `openai/gpt-6.1-terra:floor` (Flex) en premier avec un coupe-circuit strict à 3 secondes.
    Si Flex dépasse 3 secondes ou est saturé, bascule immédiatement sur la route Standard sans attendre.
    """
    extra_flex = {
        "service_tier": "flex",
        "reasoning": {"effort": "low", "exclude": True},
        "provider": {
            "sort": "price",
            "allow_fallbacks": False,
        },
    }

    try:
        # 1er essai : Route Flex avec chrono strict de 3.0s et 0 retry pour couper net en cas d'attente
        return client.with_options(max_retries=0, timeout=3.0).chat.completions.create(
            model=TERRA_MODEL_PRIMARY,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            temperature=0.0,
            max_tokens=700,
            extra_body=extra_flex,
        )
    except Exception as first_err:
        print(f"[Réseau] Flex > 3s ou occupé ({first_err}). Bascule immédiate sur Standard.")
        extra_fallback = {
            "reasoning": {"effort": "low", "exclude": True},
            "provider": {"allow_fallbacks": True},
        }
        # 2e essai : Route Standard instantanée
        return client.with_options(max_retries=2, timeout=35.0).chat.completions.create(
            model=TERRA_MODEL_FALLBACK,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            temperature=0.0,
            max_tokens=700,
            extra_body=extra_fallback,
        )


# ==============================================================================
# 7. BOUCLE PRINCIPALE DE L'AGENT (run_agent)
# ==============================================================================
def run_agent(task: str, app_instance=None, max_steps: int = 45):
    """
    Boucle principale appelée par agent_server.py.
    Conserve un préfixe de messages 100% statique pour activer le Cache Read d'OpenAI ($0.025/M)
    et ne garde que les 2 dernières captures d'écran en mémoire vive.
    """
    def log_msg(txt: str, tag: str = "system"):
        if app_instance and hasattr(app_instance, "log"):
            app_instance.log(txt, tag)
        else:
            print(txt, end="")

    try:
        client = get_openrouter_client()
    except Exception as e:
        log_msg(f"❌ {e}\n", "error")
        return

    # Récupération des indices préparés par prepare_task_data
    prep_info = "Exécution directe."
    if app_instance and hasattr(app_instance, "shared_state"):
        state = app_instance.shared_state
        if state.get("done") and state.get("data"):
            prep_info = state["data"].get("hints", prep_info)
        else:
            prep_info = prepare_task_data(task).get("hints", prep_info)

    log_msg("🚀 Modèle actif : GPT-6.1 Terra (Flex 3s -> Standard - Raisonnement bridé)\n", "system")

    # PRÉFIXE IMMUABLE (Messages 0 et 1) -> Permet à OpenAI de mettre en cache le prompt et les outils
    static_prefix = [
        {"role": "system", "content": SYSTEM_PROMPT_TERRA},
        {
            "role": "user",
            "content": (
                f"🎯 MISSION UTILISATEUR : {task}\n\n"
                f"📌 CONSIGNES CONTEXTUELLES : {prep_info}\n"
                "Exécute cette mission étape par étape à partir des captures d'écran et appelle `finish_task` dès que la dernière action est réalisée."
            ),
        },
    ]

    # Historique léger des étapes passées (texte uniquement pour économiser les tokens d'entrée)
    history_logs: list[str] = []
    # Conservation des 2 dernières captures maximum [(step_num, b64_img, action_summary)]
    recent_frames: list[dict] = []

    unchanged_screen_count = 0
    last_screen_hash = None

    for step in range(1, max_steps + 1):
        if app_instance and getattr(app_instance, "stop_requested", False):
            log_msg("🛑 Mission interrompue par l'utilisateur.\n", "error")
            return

        # 1. Capture de l'écran actuel
        b64_img, current_hash, _ = capture_screen(draw_cursor=True)

        if last_screen_hash and current_hash == last_screen_hash:
            unchanged_screen_count += 1
        else:
            unchanged_screen_count = 0
        last_screen_hash = current_hash

        # Alerte anti-boucle si l'écran n'a pas bougé depuis 3 actions
        loop_warning = ""
        if unchanged_screen_count >= 3:
            loop_warning = (
                "\n⚠️ ATTENTION : L'écran n'a pas changé après tes 3 dernières actions. "
                "Si la mission demandée est déjà terminée, appelle IMMÉDIATEMENT `finish_task` sans refaire de clic. "
                "Sinon, change d'approche (`open_app_or_url`, `focus_window`, `edit_pdf`, ou `ask_human`)."
            )
            log_msg("⚠️ Détection de boucle : l'écran ne change pas, ajustement de la stratégie...\n", "error")

        # 2. Construction des messages avec préfixe statique (Cache Hit maximal)
        messages = list(static_prefix)

        if history_logs:
            compact_history = "Historique des actions déjà effectuées :\n" + "\n".join(history_logs[-12:])
            messages.append({"role": "assistant", "content": compact_history})

        # Ajout de la capture précédente (si disponible) pour donner le contexte visuel N-1
        if recent_frames:
            prev = recent_frames[-1]
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Capture précédente (Étape {prev['step']}) après : {prev['summary']}"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{prev['b64']}", "detail": "low"}},
                    ],
                }
            )

        # Ajout de la capture courante en haute précision
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Capture actuelle (Étape {step}/{max_steps}). Quelle est ta prochaine action ?{loop_warning}",
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}", "detail": "high"}},
                ],
            }
        )

        # 3. Appel à GPT-6.1 Terra (Flex 3s -> Standard)
        try:
            response = call_terra_model(client, messages)
        except Exception as api_err:
            log_msg(f"❌ Erreur API OpenRouter : {api_err}\n", "error")
            time.sleep(1.5)
            continue

        if app_instance and getattr(app_instance, "stop_requested", False):
            log_msg("🛑 Mission interrompue.\n", "error")
            return

        msg = response.choices[0].message if response and getattr(response, "choices", None) else None
        if msg is None:
            log_msg("⚠️ Réponse API vide (erreur fournisseur). Nouvelle tentative.\n", "error")
            time.sleep(1.5)
            continue
        tool_calls = getattr(msg, "tool_calls", None)

        # Si le modèle répond sans appeler d'outil
        if not tool_calls:
            text_reply = (msg.content or "").strip()
            if text_reply:
                log_msg(f"🤖 {text_reply}\n", "agent")
                history_logs.append(f"Étape {step}: Note -> {text_reply[:120]}")
            continue

        # 4. Exécution des outils appelés par GPT-6.1 Terra
        step_summaries = []
        for tc in tool_calls:
            if app_instance and getattr(app_instance, "stop_requested", False):
                return

            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments or "{}")
            except Exception:
                fn_args = {}

            # Cas 1 : Fin de mission
            if fn_name == "finish_task":
                summary = fn_args.get("summary", "Tâche terminée avec succès.")
                log_msg(f"✅ Mission accomplie : {summary}\n", "system")
                return

            # Cas 2 : Demande d'aide à l'humain
            if fn_name == "ask_human":
                question = fn_args.get("question", "J'ai besoin d'une indication pour continuer.")
                log_msg(f"🤖 L'agent a besoin d'aide : {question}\n", "agent")
                if app_instance and hasattr(app_instance, "wait_for_human"):
                    app_instance.wait_for_human()
                    if getattr(app_instance, "stop_requested", False):
                        return
                    user_reply = getattr(app_instance, "human_response", "").strip()
                    log_msg("▶️ Réponse reçue, reprise de la mission.\n", "system")
                    history_logs.append(f"Étape {step}: Question posée ('{question}') -> Réponse utilisateur : '{user_reply}'")
                else:
                    return
                continue

            # Cas 3 : Action système / GUI / CLI / PDF
            log_msg(f"⚡ [{step}] {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:90]})\n", "agent")
            result_str = execute_action(fn_name, fn_args, app_instance=app_instance)
            step_summaries.append(f"{fn_name} -> {result_str}")
            history_logs.append(f"Étape {step}: {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:80]}) => {result_str}")

        recent_frames.append(
            {
                "step": step,
                "b64": b64_img,
                "summary": " | ".join(step_summaries) if step_summaries else "Observation",
            }
        )
        if len(recent_frames) > 2:
            recent_frames.pop(0)

    log_msg("⚠️ Nombre maximal d'étapes atteint. Arrêt de sécurité.\n", "error")
