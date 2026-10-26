import json
import socket
import threading
import tkinter as tk
from tkinter import scrolledtext

class FloatingAssistantBubble:
    def __init__(self, root):
        self.root = root
        self.root.title("Assistant IA")
        self.root.configure(bg="#121212")

        # Configuration réseau
        self.sock = None
        self.is_connected = False
        self.vm_ip = "192.168.15.132"
        self.port = 65432

        # État et dimensions de l'interface
        self.is_expanded = False
        self.chat_width = 380
        self.chat_height = 560
        self.bubble_size = 60
        self.margin = 25

        # Position initiale de la bulle (bas droite)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.bubble_x = screen_w - self.bubble_size - self.margin
        self.bubble_y = screen_h - self.bubble_size - self.margin - 40

        # Variables internes pour le Drag & Drop
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._has_dragged = False

        # Fenêtre sans bordures Windows standard et toujours au premier plan
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)

        self._build_ui()
        self.collapse_chat()
        self.connect_to_vm()

    def _build_ui(self):
        # 1. BULLE FLOTTANTE (Visible quand rétracté)
        self.bubble_frame = tk.Frame(self.root, bg="#121212")
        self.bubble_btn = tk.Label(
            self.bubble_frame,
            text="🤖",
            bg="#2563eb",
            fg="#ffffff",
            font=("Segoe UI Emoji", 20),
            cursor="fleur"
        )
        self.bubble_btn.pack(fill=tk.BOTH, expand=True)

        # Événements de déplacement pour la bulle
        self.bubble_btn.bind("<Button-1>", self._on_bubble_press)
        self.bubble_btn.bind("<B1-Motion>", self._on_bubble_motion)
        self.bubble_btn.bind("<ButtonRelease-1>", self._on_bubble_release)

        # 2. FENÊTRE DE CHAT (Visible quand déplié)
        self.chat_frame = tk.Frame(self.root, bg="#1e1e1e", bd=1, relief=tk.SOLID)

        # En-tête : Titre, Statut, Clé API et Réduction
        self.header = tk.Frame(self.chat_frame, bg="#252526", pady=8, padx=10, cursor="fleur")
        self.header.pack(fill=tk.X)

        # Permet aussi de déplacer la fenêtre de chat via l'en-tête
        self.header.bind("<Button-1>", self._on_header_press)
        self.header.bind("<B1-Motion>", self._on_header_motion)

        self.title_label = tk.Label(self.header, text="Assistant VM", fg="#ffffff", bg="#252526", font=("Segoe UI", 10, "bold"))
        self.title_label.pack(side=tk.LEFT)
        self.title_label.bind("<Button-1>", self._on_header_press)
        self.title_label.bind("<B1-Motion>", self._on_header_motion)

        close_btn = tk.Button(
            self.header,
            text="—",
            fg="#cccccc",
            bg="#252526",
            activebackground="#3e3e42",
            activeforeground="#ffffff",
            bd=0,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            command=self.collapse_chat
        )
        close_btn.pack(side=tk.RIGHT, padx=(5, 0))

        # Bouton pour changer la clé API à tout moment
        key_btn = tk.Button(
            self.header,
            text="🔑",
            fg="#fde047",
            bg="#252526",
            activebackground="#3e3e42",
            activeforeground="#ffffff",
            bd=0,
            font=("Segoe UI Emoji", 9),
            cursor="hand2",
            command=self.request_key_change
        )
        key_btn.pack(side=tk.RIGHT, padx=(5, 5))

        self.status_label = tk.Label(self.header, text="●", fg="#ff5555", bg="#252526", font=("Segoe UI", 11))
        self.status_label.pack(side=tk.RIGHT)

        # Zone centrale : Journal des logs
        self.log_area = scrolledtext.ScrolledText(
            self.chat_frame,
            bg="#18181b",
            fg="#e4e4e7",
            insertbackground="white",
            font=("Consolas", 9),
            wrap=tk.WORD,
            bd=0
        )
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # Coloration
        self.log_area.tag_config("system", foreground="#4ade80")
        self.log_area.tag_config("agent", foreground="#67e8f9")
        self.log_area.tag_config("user", foreground="#fde047")
        self.log_area.tag_config("error", foreground="#f87171")

        # Zone inférieure : Saisie & Boutons
        bottom_box = tk.Frame(self.chat_frame, bg="#1e1e1e", padx=8, pady=8)
        bottom_box.pack(fill=tk.X)

        self.input_entry = tk.Entry(bottom_box, bg="#27272a", fg="#ffffff", insertbackground="white", font=("Segoe UI", 10), bd=1, relief=tk.FLAT)
        self.input_entry.pack(fill=tk.X, pady=(0, 6), ipady=3)
        self.input_entry.bind("<Return>", lambda e: self.send_instruction())

        btn_row = tk.Frame(bottom_box, bg="#1e1e1e")
        btn_row.pack(fill=tk.X)

        # Bouton STOP NET
        self.stop_btn = tk.Button(
            btn_row,
            text="🛑 STOP",
            bg="#dc2626",
            fg="#ffffff",
            activebackground="#991b1b",
            activeforeground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
            command=self.send_emergency_stop
        )
        self.stop_btn.pack(side=tk.RIGHT, padx=(6, 0), ipadx=10, ipady=3)

        # Bouton Envoyer
        self.send_btn = tk.Button(
            btn_row,
            text="Envoyer",
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
            command=self.send_instruction
        )
        self.send_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)

    # --- LOGIQUE DE GLISSER-DÉPOSER (DRAG & DROP) ---
    def _on_bubble_press(self, event):
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self._has_dragged = False

    def _on_bubble_motion(self, event):
        dx = event.x_root - self._drag_start_x
        dy = event.y_root - self._drag_start_y

        if abs(dx) > 3 or abs(dy) > 3:
            self._has_dragged = True

        self.bubble_x += dx
        self.bubble_y += dy
        self.root.geometry(f"{self.bubble_size}x{self.bubble_size}+{self.bubble_x}+{self.bubble_y}")
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root

    def _on_bubble_release(self, event):
        if not self._has_dragged:
            self.expand_chat()

    def _on_header_press(self, event):
        self._chat_drag_x = event.x_root - self.root.winfo_x()
        self._chat_drag_y = event.y_root - self.root.winfo_y()

    def _on_header_motion(self, event):
        new_x = event.x_root - self._chat_drag_x
        new_y = event.y_root - self._chat_drag_y
        self.root.geometry(f"{self.chat_width}x{self.chat_height}+{new_x}+{new_y}")
        self.bubble_x = new_x + self.chat_width - self.bubble_size
        self.bubble_y = new_y + self.chat_height - self.bubble_size

    # --- DÉPLOIEMENT / RÉDUCTION DU CHAT ---
    def expand_chat(self):
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        x = self.bubble_x + self.bubble_size - self.chat_width
        y = self.bubble_y + self.bubble_size - self.chat_height

        x = max(10, min(x, screen_w - self.chat_width - 10))
        y = max(10, min(y, screen_h - self.chat_height - 40))

        self.bubble_frame.pack_forget()
        self.chat_frame.pack(fill=tk.BOTH, expand=True)
        self.root.geometry(f"{self.chat_width}x{self.chat_height}+{x}+{y}")
        self.is_expanded = True

    def collapse_chat(self):
        self.chat_frame.pack_forget()
        self.bubble_frame.pack(fill=tk.BOTH, expand=True)
        self.root.geometry(f"{self.bubble_size}x{self.bubble_size}+{self.bubble_x}+{self.bubble_y}")
        self.is_expanded = False

    def log(self, text, tag="system"):
        self.log_area.insert(tk.END, text, tag)
        self.log_area.see(tk.END)

        triggers = ["🤖 l'agent a besoin d'aide", "mission accomplie", "✅", "⚠️ détection de boucle", "🔑"]
        if any(trig in text.lower() for trig in triggers):
            if not self.is_expanded:
                self.root.after(0, self.expand_chat)

    def connect_to_vm(self):
        def _connect():
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.connect((self.vm_ip, self.port))
                self.is_connected = True
                self.status_label.config(fg="#4ade80")
                self.log("✅ Connecté au serveur VM.\n", "system")
                threading.Thread(target=self._listen_vm, daemon=True).start()
            except Exception as e:
                self.status_label.config(fg="#f87171")
                self.log(f"Erreur de connexion : {e}\n", "error")

        threading.Thread(target=_connect, daemon=True).start()

    def _listen_vm(self):
        buffer = ""
        while self.is_connected:
            try:
                chunk = self.sock.recv(4096).decode("utf-8")
                if not chunk:
                    break
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    text = data.get("text", "")
                    tag = data.get("tag", "system")
                    self.root.after(0, self.log, text, tag)
            except Exception:
                break

        self.is_connected = False
        self.status_label.config(fg="#f87171")
        self.root.after(0, self.log, "Connexion perdue.\n", "error")

    def send_instruction(self):
        text = self.input_entry.get().strip()
        if not text or not self.is_connected:
            return

        payload = {"type": "instruction", "content": text}
        try:
            self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            if text.startswith("sk-or-"):
                self.log("👤 [Clé API envoyée et masquée]\n\n", "user")
            else:
                self.log(f"👤 {text}\n\n", "user")
            self.input_entry.delete(0, tk.END)
        except Exception as e:
            self.log(f"Erreur d'envoi : {e}\n", "error")

    def request_key_change(self):
        if not self.is_connected:
            return
        payload = {"type": "reset_key"}
        try:
            self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except Exception as e:
            self.log(f"Erreur : {e}\n", "error")

    def send_emergency_stop(self):
        if not self.is_connected:
            return
        payload = {"type": "stop"}
        try:
            self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            self.log("🛑 ORDRE DE STOP IMMÉDIAT ENVOYÉ.\n", "error")
        except Exception as e:
            self.log(f"Erreur lors de l'arrêt : {e}\n", "error")

if __name__ == "__main__":
    root = tk.Tk()
    app = FloatingAssistantBubble(root)
    root.mainloop()