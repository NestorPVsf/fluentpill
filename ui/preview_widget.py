from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
    QLabel, QTextEdit, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont

class PreviewWidget(QWidget):
    """A floating window to preview and choose between Original and Refined prompt."""
    
    choice_made = pyqtSignal(str) # Emits the final chosen text (if changed), or empty string if original kept.
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(700, 500)
        
        self.original_text = ""
        self.refined_text = ""
        
        self._setup_ui()
        
    def _setup_ui(self):
        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        # Background container
        container = QWidget()
        container.setObjectName("container")
        container.setStyleSheet("""
            QWidget#container {
                background-color: rgba(20, 20, 20, 245);
                border: 1px solid rgba(255, 255, 255, 30);
                border-radius: 12px;
            }
            QLabel {
                color: #FFFFFF;
                font-family: Arial, sans-serif;
                font-weight: bold;
                font-size: 14px;
            }
            QTextEdit {
                background-color: rgba(40, 40, 40, 255);
                color: #E0E0E0;
                border: 1px solid rgba(255, 255, 255, 20);
                border-radius: 6px;
                padding: 8px;
                font-family: Consolas, monospace;
                font-size: 13px;
            }
            QPushButton {
                font-family: Arial, sans-serif;
                font-size: 13px;
                font-weight: bold;
                border-radius: 6px;
                padding: 8px 16px;
            }
            QPushButton#btn_original {
                background-color: transparent;
                color: #A0A0A0;
                border: 1px solid #505050;
                margin-top: 10px;
            }
            QPushButton#btn_original:hover {
                background-color: rgba(255, 255, 255, 10);
                color: #FFFFFF;
                border: 1px solid #707070;
            }
            QPushButton#btn_refined {
                background-color: #FFFFFF;
                color: #000000;
                border: none;
                margin-top: 10px;
            }
            QPushButton#btn_refined:hover {
                background-color: #E0E0E0;
            }
        """)
        
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        title = QLabel("Compara y Elige tu Prompt")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 18px; margin-bottom: 10px;")
        layout.addWidget(title)
        
        # Texts area
        texts_layout = QHBoxLayout()
        texts_layout.setSpacing(20)
        
        # --- Original Column ---
        orig_layout = QVBoxLayout()
        orig_label = QLabel("Original (Ya pegado)")
        orig_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.orig_text_edit = QTextEdit()
        self.orig_text_edit.setReadOnly(True)
        
        btn_original = QPushButton("Descartar (Mantener Original)")
        btn_original.setObjectName("btn_original")
        btn_original.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_original.clicked.connect(self._keep_original)
        
        orig_layout.addWidget(orig_label)
        orig_layout.addWidget(self.orig_text_edit)
        orig_layout.addWidget(btn_original)
        
        # --- Refined Column ---
        ref_layout = QVBoxLayout()
        ref_label = QLabel("✨ Refinado (Editable)")
        ref_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.ref_text_edit = QTextEdit()
        
        btn_refined = QPushButton("✨ Usar este Prompt (Reemplazar)")
        btn_refined.setObjectName("btn_refined")
        btn_refined.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_refined.clicked.connect(self._use_refined)
        
        ref_layout.addWidget(ref_label)
        ref_layout.addWidget(self.ref_text_edit)
        ref_layout.addWidget(btn_refined)
        
        texts_layout.addLayout(orig_layout)
        texts_layout.addLayout(ref_layout)
        
        layout.addLayout(texts_layout)
        main_layout.addWidget(container)
        
    def show_preview(self, original: str, refined: str):
        self.original_text = original
        self.refined_text = refined
        self.orig_text_edit.setPlainText(original)
        self.ref_text_edit.setPlainText(refined)
        
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(
                geo.center().x() - self.width() // 2,
                geo.center().y() - self.height() // 2
            )
            
        self.show()
        
    def _keep_original(self):
        self.choice_made.emit("") # Empty string means no change
        self.hide()
        
    def _use_refined(self):
        # We also allow the user to modify the refined text before clicking accept
        final_text = self.ref_text_edit.toPlainText()
        self.choice_made.emit(final_text)
        self.hide()
