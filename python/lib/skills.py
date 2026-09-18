# Skills : vue uniforme d'une arme ou d'une puce, dérivée de `item.features` (une fois par combat).
#
# Deux familles (cf docs/planner.md) :
#   - additives   : dégâts, poison, soin, boucliers → valeur en PV, optimisées par le sac à dos de PT ;
#   - structurelles : +PM/+PT/+force sur moi, −PM/−PT sur l'ennemi, téléportation → elles changent l'espace
#     de recherche (portée, budget, map de danger) et sont énumérées comme variantes.

from dataclasses import dataclass, replace
from typing import Any

# Ids numériques des effets (constantes EFFECT_* du site) : on ne dépend pas de `Effect.*` pour rester testable.
E_DAMAGE = 1
E_HEAL = 2
E_BUFF_STRENGTH = 3
E_BUFF_AGILITY = 4
E_RELATIVE_SHIELD = 5
E_ABSOLUTE_SHIELD = 6
E_BUFF_MP = 7
E_BUFF_TP = 8
E_TELEPORT = 10
E_POISON = 13
E_SUMMON = 14
E_SHACKLE_MP = 17
E_SHACKLE_TP = 18
E_SHACKLE_STRENGTH = 19
E_BUFF_RESISTANCE = 21
E_BUFF_WISDOM = 22
E_LIFE_DAMAGE = 28
E_NOVA_DAMAGE = 30
E_RAW_BUFF_MP = 31  # bottes de cuir : valeur brute, non amplifiée par la science
E_RAW_BUFF_TP = 32  # adrénaline
E_RAW_ABSOLUTE_SHIELD = 37
E_RAW_BUFF_STRENGTH = 38  # protéine
E_RAW_BUFF_MAGIC = 39  # wizardry
E_RAW_BUFF_SCIENCE = 40
E_RAW_BUFF_AGILITY = 41  # stretching, warm_up
E_RAW_BUFF_RESISTANCE = 42  # solidification
E_RAW_BUFF_WISDOM = 44  # knowledge
E_RAW_BUFF_POWER = 52
E_RAW_RELATIVE_SHIELD = 54
E_RAW_HEAL = 57
E_STEAL_LIFE = 61

# Genres de skill (chaînes pour lisibilité dans les logs).
DAMAGE = "damage"
POISON = "poison"
HEAL = "heal"
ABS_SHIELD = "abs_shield"
REL_SHIELD = "rel_shield"
BUFF_STRENGTH = "buff_strength"
BUFF_MAGIC = "buff_magic"
BUFF_AGILITY = "buff_agility"
BUFF_WISDOM = "buff_wisdom"
BUFF_RESISTANCE = "buff_resistance"
BUFF_SCIENCE = "buff_science"
BUFF_POWER = "buff_power"
BUFF_MP = "buff_mp"
BUFF_TP = "buff_tp"
SHACKLE_MP = "shackle_mp"
SHACKLE_TP = "shackle_tp"
TELEPORT = "teleport"
OTHER = "other"

KIND_OF_EFFECT = {
    E_DAMAGE: DAMAGE,
    E_LIFE_DAMAGE: DAMAGE,
    E_STEAL_LIFE: DAMAGE,
    E_POISON: POISON,
    E_HEAL: HEAL,
    E_ABSOLUTE_SHIELD: ABS_SHIELD,
    E_RELATIVE_SHIELD: REL_SHIELD,
    E_BUFF_STRENGTH: BUFF_STRENGTH,
    E_BUFF_AGILITY: BUFF_AGILITY,
    E_BUFF_WISDOM: BUFF_WISDOM,
    E_BUFF_RESISTANCE: BUFF_RESISTANCE,
    E_RAW_BUFF_MAGIC: BUFF_MAGIC,
    E_RAW_BUFF_SCIENCE: BUFF_SCIENCE,
    E_RAW_BUFF_AGILITY: BUFF_AGILITY,
    E_RAW_BUFF_RESISTANCE: BUFF_RESISTANCE,
    E_RAW_BUFF_WISDOM: BUFF_WISDOM,
    E_RAW_BUFF_POWER: BUFF_POWER,
    E_BUFF_MP: BUFF_MP,
    E_BUFF_TP: BUFF_TP,
    E_SHACKLE_MP: SHACKLE_MP,
    E_SHACKLE_TP: SHACKLE_TP,
    E_TELEPORT: TELEPORT,
    E_RAW_BUFF_MP: BUFF_MP,
    E_RAW_BUFF_TP: BUFF_TP,
    E_RAW_ABSOLUTE_SHIELD: ABS_SHIELD,
    E_RAW_BUFF_STRENGTH: BUFF_STRENGTH,
    E_RAW_RELATIVE_SHIELD: REL_SHIELD,
    E_RAW_HEAL: HEAL,
}
RAW_EFFECTS = frozenset({E_RAW_BUFF_MP, E_RAW_BUFF_TP, E_RAW_ABSOLUTE_SHIELD, E_RAW_BUFF_STRENGTH,
                         E_RAW_RELATIVE_SHIELD, E_RAW_HEAL, E_RAW_BUFF_MAGIC, E_RAW_BUFF_SCIENCE,
                         E_RAW_BUFF_AGILITY, E_RAW_BUFF_RESISTANCE, E_RAW_BUFF_WISDOM, E_RAW_BUFF_POWER})
# Buff de caractéristique → attribut de `Ent` à augmenter dans le contexte.
STAT_OF_KIND = {
    BUFF_STRENGTH: "strength",
    BUFF_MAGIC: "magic",
    BUFF_AGILITY: "agility",
    BUFF_WISDOM: "wisdom",
    BUFF_RESISTANCE: "resistance",
    BUFF_SCIENCE: "science",
    BUFF_POWER: "power",
}

OFFENSIVE = frozenset({DAMAGE, POISON, SHACKLE_MP, SHACKLE_TP})
SELF_ADDITIVE = frozenset({HEAL, ABS_SHIELD, REL_SHIELD})
STAT_BUFFS = frozenset(STAT_OF_KIND)
SELF_CONTEXT = STAT_BUFFS | {BUFF_MP, BUFF_TP}
ENEMY_VARIANT = frozenset({SHACKLE_MP, SHACKLE_TP})


@dataclass(frozen=True)
class Skill:
    key: str  # nom lisible et unique (ex. "w:pistol", "c:toxin")
    kind: str
    is_weapon: bool
    cost: int
    min_range: int
    max_range: int
    launch: int
    los: bool
    area: int
    max_uses: int  # par tour ; 0 = illimité (limité par les PT)
    min_v: float
    max_v: float
    turns: int
    item: Any = None  # objet Weapon / Chip du moteur (None dans les tests)
    available: bool = True  # cooldown à 0 ce tour
    raw: bool = False  # valeur brute (effets RAW_*) : pas d'amplification par la caractéristique

    @property
    def avg(self) -> float:
        return (self.min_v + self.max_v) / 2.0

    def uses_cap(self, tp: int) -> int:
        cap = tp // self.cost if self.cost > 0 else 0
        if self.max_uses > 0:
            cap = min(cap, self.max_uses)
        return cap


def skill_from_item(item: Any, is_weapon: bool, prefix: str = "") -> Skill | None:
    """Construit un Skill depuis un Weapon/Chip du moteur. Coûte ~125 ops (`features`) + quelques lectures :
    à appeler une fois par combat et par item, puis `refresh()` chaque tour pour le cooldown."""
    kind = OTHER
    min_v = max_v = 0.0
    turns = 0
    raw = False
    for f in item.features:
        ftype = f.type
        k = KIND_OF_EFFECT.get(ftype)
        if k is None:
            continue
        # Un item porte parfois plusieurs effets (destroyer : dégâts + entrave force) : on garde le premier
        # effet connu, sauf si un effet de dégâts arrive ensuite (il domine pour le scoring). Deux effets du
        # même genre (rempart, carapace) s'additionnent.
        if kind == OTHER or (k == DAMAGE and kind != DAMAGE):
            kind = k
            min_v, max_v, turns = float(f.minValue), float(f.maxValue), int(f.turns)
            raw = ftype in RAW_EFFECTS
        elif k == kind:
            min_v += float(f.minValue)
            max_v += float(f.maxValue)
    if kind == OTHER:
        return None
    if is_weapon:
        cooldown = 0
        max_uses = item.maxUses
    else:
        cooldown = item.cooldown
        max_uses = item.maxUses if cooldown == 0 else 1
    return Skill(
        key=(prefix or ("w:" if is_weapon else "c:")) + item.name,
        kind=kind,
        is_weapon=is_weapon,
        cost=item.cost,
        min_range=item.minRange,
        max_range=item.maxRange,
        launch=item.launchType,
        los=item.needsLos,
        area=item.area,
        max_uses=max(0, max_uses),
        min_v=min_v,
        max_v=max_v,
        turns=turns,
        item=item,
        raw=raw,
    )


def refresh(skill: Skill) -> Skill:
    """Skill avec `available` mis à jour depuis le cooldown courant (30 ops par puce)."""
    if skill.is_weapon or skill.item is None:
        return skill
    avail = skill.item.currentCooldown == 0
    if avail == skill.available:
        return skill
    return replace(skill, available=avail)


def make(key: str, kind: str, cost: int, min_range: int, max_range: int, min_v: float, max_v: float,
         *, launch: int = 7, los: bool = True, max_uses: int = 1, turns: int = 0, is_weapon: bool = False,
         area: int = 1, available: bool = True, raw: bool = False) -> Skill:
    """Constructeur court pour les tests et les fixtures."""
    return Skill(key, kind, is_weapon, cost, min_range, max_range, launch, los, area, max_uses,
                 float(min_v), float(max_v), turns, None, available, raw)
