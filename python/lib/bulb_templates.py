# GÉNÉRÉ par tools/gen_bulbs.py depuis data/game_data.json — ne pas éditer à la main.
# {id de template: (nom, {stat: (min, max)}, [ids de puces])}. Stat réelle = floor(min + (max − min) × min(300, niveau) / 300).

TEMPLATES: dict[int, tuple[str, dict[str, tuple[int, int]], list[int]]] = {
    1: ('puny_bulb', {'life': (50, 300), 'strength': (0, 100), 'wisdom': (0, 100), 'agility': (0, 100), 'resistance': (0, 100), 'science': (0, 100), 'magic': (0, 0), 'tp': (4, 7), 'mp': (3, 5)}, [21, 19, 3, 8]),
    2: ('fire_bulb', {'life': (300, 500), 'strength': (0, 300), 'wisdom': (0, 200), 'agility': (0, 100), 'resistance': (0, 0), 'science': (0, 0), 'magic': (0, 0), 'tp': (4, 9), 'mp': (3, 5)}, [18, 5, 36, 85]),
    3: ('healer_bulb', {'life': (300, 400), 'strength': (0, 0), 'wisdom': (0, 300), 'agility': (0, 100), 'resistance': (0, 0), 'science': (0, 0), 'magic': (0, 0), 'tp': (4, 8), 'mp': (3, 6)}, [3, 10, 4, 11]),
    4: ('rocky_bulb', {'life': (400, 600), 'strength': (0, 200), 'wisdom': (0, 0), 'agility': (0, 100), 'resistance': (0, 200), 'science': (0, 0), 'magic': (0, 0), 'tp': (4, 8), 'mp': (2, 3)}, [19, 7, 32, 21]),
    5: ('iced_bulb', {'life': (300, 500), 'strength': (0, 300), 'wisdom': (0, 0), 'agility': (0, 100), 'resistance': (0, 0), 'science': (0, 200), 'magic': (0, 0), 'tp': (5, 8), 'mp': (3, 4)}, [2, 30, 31, 28]),
    6: ('lightning_bulb', {'life': (400, 600), 'strength': (0, 400), 'wisdom': (0, 0), 'agility': (0, 100), 'resistance': (0, 0), 'science': (0, 200), 'magic': (0, 0), 'tp': (6, 10), 'mp': (4, 6)}, [1, 6, 33, 25]),
    7: ('metallic_bulb', {'life': (800, 1100), 'strength': (0, 0), 'wisdom': (0, 0), 'agility': (0, 100), 'resistance': (0, 300), 'science': (0, 200), 'magic': (0, 0), 'tp': (5, 9), 'mp': (1, 2)}, [20, 22, 23, 13]),
    8: ('wizard_bulb', {'life': (300, 600), 'strength': (0, 0), 'wisdom': (0, 0), 'agility': (0, 100), 'resistance': (0, 0), 'science': (0, 0), 'magic': (0, 240), 'tp': (5, 8), 'mp': (4, 7)}, [97, 98, 94, 92]),
    9: ('corn', {'life': (100, 900), 'strength': (0, 0), 'wisdom': (0, 400), 'agility': (0, 0), 'resistance': (0, 0), 'science': (0, 0), 'magic': (0, 0), 'tp': (4, 6), 'mp': (0, 0)}, [446, 447]),
    10: ('chilli_pepper', {'life': (100, 900), 'strength': (0, 400), 'wisdom': (0, 0), 'agility': (0, 0), 'resistance': (0, 0), 'science': (0, 0), 'magic': (0, 200), 'tp': (4, 7), 'mp': (0, 0)}, [444, 445]),
    11: ('tactician_bulb', {'life': (500, 700), 'strength': (0, 0), 'wisdom': (0, 0), 'agility': (0, 200), 'resistance': (0, 0), 'science': (0, 0), 'magic': (0, 0), 'tp': (6, 6), 'mp': (6, 6)}, [59, 68, 144, 120, 162, 163]),
    12: ('savant_bulb', {'life': (400, 600), 'strength': (0, 0), 'wisdom': (0, 0), 'agility': (0, 200), 'resistance': (0, 0), 'science': (0, 300), 'magic': (0, 0), 'tp': (6, 8), 'mp': (4, 6)}, [141, 159, 16, 100]),
    13: ('prototaxite', {'life': (300, 1300), 'strength': (0, 0), 'wisdom': (0, 0), 'agility': (0, 0), 'resistance': (0, 0), 'science': (0, 0), 'magic': (0, 0), 'tp': (0, 0), 'mp': (0, 0)}, []),
}
