# Modèle du monde : snapshot de l'API en début de tour, puis tout le raisonnement se fait ici, en Python pur.
#
# C'est le SEUL module (avec executor.py) qui lit l'API de combat. Les tests construisent un `World`
# directement (grille ASCII + `Ent` à la main), sans moteur.

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from geometry import Grid
from skills import Skill, refresh, skill_from_item

NUM_CELLS = 613


@dataclass
class Ent:
    id: int
    cell: int
    life: int
    max_life: int
    tp: int
    mp: int
    strength: int = 0
    magic: int = 0
    agility: int = 0
    wisdom: int = 0
    resistance: int = 0
    science: int = 0
    power: int = 0
    abs_shield: int = 0
    rel_shield: int = 0
    enemy: bool = True
    summoned: bool = False
    name: str = ""
    skills: list[Skill] = field(default_factory=list)
    weapon_key: str | None = None  # arme équipée (clé de skill), pour le coût de changement d'arme
    ref: Any = None  # objet Entity du moteur (None dans les tests)


@dataclass
class World:
    grid: Grid
    me: Ent
    enemies: list[Ent]  # vivants
    allies: list[Ent] = field(default_factory=list)  # vivants, hors moi
    blocked: list[bool] = field(default_factory=list)  # obstacles + entités vivantes (moi compris)
    los: Callable[[int, int], bool] = lambda a, b: True
    ops: Callable[[], int] = lambda: 0
    max_ops: int = 1_000_000
    turn: int = 1
    teleport_used: bool = False

    def __post_init__(self) -> None:
        if not self.blocked:
            self.blocked = list(self.grid.obstacle)
            for e in [self.me, *self.enemies, *self.allies]:
                self.blocked[e.cell] = True

    def blocked_for(self, mover: Ent) -> list[bool]:
        """Cases infranchissables pour `mover` : sa propre case redevient libre."""
        b = list(self.blocked)
        b[mover.cell] = False
        return b

    def derive(self, me: Ent | None = None, enemies: list[Ent] | None = None, **kw: Any) -> "World":
        """Copie superficielle avec un `me` ou des ennemis modifiés (contextes structurels)."""
        return replace(self, me=me or self.me, enemies=enemies if enemies is not None else self.enemies, **kw)


# ---------------------------------------------------------------------------------------------------
# Lecture de l'API (runtime uniquement)
# ---------------------------------------------------------------------------------------------------

_grid: Grid | None = None
_skills_cache: dict[int, list[Skill]] = {}
_los_cache: dict[tuple[int, int], bool] = {}


def build_grid() -> Grid:
    """Lit les 613 cases une fois (≈ 25 ops par case) : coordonnées + obstacles fixes."""
    xy: list[tuple[int, int]] = []
    obstacle: list[bool] = []
    get = Cell.get
    for i in range(NUM_CELLS):
        c = get(i)
        xy.append((c.x, c.y))
        obstacle.append(c.obstacle)
    grid = Grid(xy, obstacle)
    # Auto-vérification du repère : dist() doit coller à getCellDistance sur deux paires.
    for a, b in ((0, NUM_CELLS - 1), (100, 250)):
        if grid.dist(a, b) != Field.distance(a, b):
            Debug.log(f"[world] repère incohérent : dist({a},{b})={grid.dist(a, b)} vs {Field.distance(a, b)}",
                      Color.RED)
            break
    return grid


def _skills_of(e: Any, is_me: bool) -> list[Skill]:
    cached = _skills_cache.get(e.id)
    if cached is None:
        cached = []
        for w in e.weapons:
            s = skill_from_item(w, True)
            if s is not None:
                cached.append(s)
        for c in e.chips:
            s = skill_from_item(c, False)
            if s is not None:
                cached.append(s)
        _skills_cache[e.id] = cached
    if is_me:
        # Cooldowns : 30 ops par puce, uniquement pour moi (pour les ennemis on suppose tout disponible).
        cached = [refresh(s) for s in cached]
        _skills_cache[e.id] = cached
    return cached


def _ent(e: Any, is_me: bool, enemy: bool) -> Ent:
    w = e.weapon
    return Ent(
        id=e.id, cell=e.cell.id, life=e.life, max_life=e.maxLife, tp=e.tp, mp=e.mp,
        strength=e.strength, magic=e.magic, agility=e.agility, wisdom=e.wisdom, resistance=e.resistance,
        science=e.science, power=e.power, abs_shield=e.absoluteShield, rel_shield=e.relativeShield,
        enemy=enemy, summoned=e.summoned, name=e.name, skills=_skills_of(e, is_me),
        weapon_key=("w:" + w.name) if w is not None else None, ref=e,
    )


def _los(a: int, b: int) -> bool:
    key = (a, b) if a < b else (b, a)
    v = _los_cache.get(key)
    if v is None:
        v = bool(Field.lineOfSight(a, b))  # 31 ops
        _los_cache[key] = v
    return v


def snapshot() -> World:
    """Lit l'état du combat (≈ 15 k ops en régime, + la grille au tour 1) et renvoie un `World`."""
    global _grid
    if _grid is None:
        _grid = build_grid()
    _los_cache.clear()
    me_ref = Fight.me
    me = _ent(me_ref, True, False)
    enemies = [_ent(e, False, True) for e in Fight.getAliveEnemies()]
    allies = [_ent(a, False, False) for a in Fight.getAliveAllies() if a.id != me.id]
    return World(
        grid=_grid, me=me, enemies=enemies, allies=allies, los=_los,
        ops=lambda: System.operations, max_ops=System.maxOperations, turn=Fight.turn,
    )
