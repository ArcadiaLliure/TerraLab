import sys
import traceback
import os
from PyQt5.QtWidgets import QApplication, QDialog
from TerraLab.widgets.sky_widget import AstronomicalWidget

from PyQt5.QtCore import Qt

class StandaloneAstronomicalWidget(AstronomicalWidget):
    def __init__(self):
        # Initialize as standard window (frameless=False)
        super().__init__(parent=None, frameless=False)
        self.setWindowTitle("TerraLab Standalone")
        self.resize(1024, 768)
        
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
        else:
            super().keyPressEvent(event)

def main():
    app = QApplication(sys.argv)
    
    # Standalone mode
    # Check Terrain Config before launch
    from TerraLab.config import ConfigManager
    config = ConfigManager()
    raster_path = config.get_raster_path()
    
    start_app = True
    if not raster_path or not os.path.exists(raster_path):
        from TerraLab.widgets.terrain_config_dialog import TerrainConfigDialog
        dlg = TerrainConfigDialog()
        if dlg.exec_() == QDialog.Accepted:
             # Reload config just in case
             config._load()
             
    widget = StandaloneAstronomicalWidget()
    widget.show()
    
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
