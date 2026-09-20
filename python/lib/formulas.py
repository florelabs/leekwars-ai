# Formules de jeu (dégâts, soins, poisons, boucliers, crits) : un seul endroit à corriger si le site change.
# Sources : doc Leek Wars (aide > mécaniques). Les points marqués « à valider » sont des hypothèses.

CRITICAL_FACTOR = 1.3


def crit_chance(agility: int) -> float:
    """Chance de coup critique. Hypothèse LW : agilité / 10 % (à valider)."""
    return min(1.0, max(0.0, agility / 1000.0))


def expected_mult(agility: int) -> float:
    """Multiplicateur moyen dû aux critiques."""
    return 1.0 + crit_chance(agility) * (CRITICAL_FACTOR - 1.0)


def damage(base: float, strength: int, power: int, rel_shield: int, abs_shield: int) -> float:
    """Dégâts d'une attaque de base `base` : force et puissance du lanceur, boucliers de la cible.
    `base × (1 + force/100) × (1 + puissance/100)`, puis `× (1 − relatif/100) − absolu`, plancher 0.
    Le facteur puissance est à valider selon ton niveau."""
    d = base * (1.0 + strength / 100.0) * (1.0 + power / 100.0)
    d = d * (1.0 - rel_shield / 100.0) - abs_shield
    return d if d > 0 else 0.0


def poison(base: float, magic: int, power: int) -> float:
    """Poison par tour : magie du lanceur, pas de bouclier."""
    return base * (1.0 + magic / 100.0) * (1.0 + power / 100.0)


def nova(base: float, science: int, power: int) -> float:
    """Dégâts nova (retirent de la vie max) : science du lanceur, pas de bouclier."""
    return base * (1.0 + science / 100.0) * (1.0 + power / 100.0)


def heal(base: float, wisdom: int, power: int) -> float:
    return base * (1.0 + wisdom / 100.0) * (1.0 + power / 100.0)


def shield(base: float, resistance: int, power: int) -> float:
    """Valeur d'un bouclier (absolu en PV, relatif en %) : résistance du lanceur."""
    return base * (1.0 + resistance / 100.0) * (1.0 + power / 100.0)


def buff(base: float, science: int, power: int) -> float:
    """Boost de caractéristique (BUFF_* non RAW) : science du lanceur."""
    return base * (1.0 + science / 100.0) * (1.0 + power / 100.0)


def shackle(base: float, magic: int, power: int) -> float:
    """Entrave (PM/PT/force retirés) : magie du lanceur. Formule à valider (les puces ont des bases
    fractionnaires : boulet 0.4–0.5)."""
    return base * (1.0 + magic / 100.0) * (1.0 + power / 100.0)
