# URL for UI links
from typing import Dict

from PySide2.QtGui import QColor, QFont, QPixmap

from theater.theatergroundobject import CATEGORY_MAP

URLS : Dict[str, str] = {
    "Manual": "https://github.com/shdwp/dcs_liberation/wiki/Manual",
    "Troubleshooting": "https://github.com/shdwp/dcs_liberation/wiki/Troubleshooting",
    "Modding": "https://github.com/shdwp/dcs_liberation/wiki/Modding-tutorial",
    "Repository": "https://github.com/shdwp/dcs_liberation",
    "ForumThread": "https://forums.eagle.ru/showthread.php?t=214834",
    "Issues": "https://github.com/shdwp/dcs_liberation/issues"
}

COLORS: Dict[str, QColor] = {
    "red": QColor(255, 125, 125),
    "bright_red": QColor(200, 64, 64),
    "blue": QColor(164, 164, 255),
    "dark_blue": QColor(45, 62, 80),
    "white": QColor(255, 255, 255),
    "green": QColor(128, 186, 128),
    "bright_green": QColor(64, 200, 64),
    "black": QColor(0, 0, 0),
    "black_transparent": QColor(0, 0, 0, 64)
}


CP_SIZE = 25


FONT = QFont("Arial", 12, weight=5, italic=True)

ICONS: Dict[str, QPixmap] = {}

def load_icons():

    ICONS["New"] = QPixmap("./resources/ui/misc/new.png")
    ICONS["Open"] = QPixmap("./resources/ui/misc/open.png")
    ICONS["Save"] = QPixmap("./resources/ui/misc/save.png")

    ICONS["Terrain_Caucasus"] = QPixmap("./resources/ui/terrain_caucasus.gif")
    ICONS["Terrain_Persian_Gulf"] = QPixmap("./resources/ui/terrain_pg.gif")
    ICONS["Terrain_Nevada"] = QPixmap("./resources/ui/terrain_nevada.gif")

    ICONS["Dawn"] = QPixmap("./resources/ui/daytime/dawn.png")
    ICONS["Day"] = QPixmap("./resources/ui/daytime/day.png")
    ICONS["Dusk"] = QPixmap("./resources/ui/daytime/dusk.png")
    ICONS["Night"] = QPixmap("./resources/ui/daytime/night.png")
    ICONS["Money"] = QPixmap("./resources/ui/misc/money_icon.png")
    ICONS["Ordnance"] = QPixmap("./resources/ui/misc/ordnance_icon.png")

    ICONS["target"] = QPixmap("./resources/ui/ground_assets/target.png")
    ICONS["cleared"] = QPixmap("./resources/ui/ground_assets/cleared.png")
    for category in CATEGORY_MAP.keys():
        ICONS[category] = QPixmap("./resources/ui/ground_assets/" + category + ".png")
