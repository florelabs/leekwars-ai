# Map de danger : dégâts attendus au prochain tour ennemi, pour N'IMPORTE quelle case, en O(1) par case.
#
# Pour chaque ennemi e (et chaque variante d'état : de base, PM entravés, PT entravés) :
#   1. reach_e  = BFS à pied avec ses PM (entités = obstacles)                     ~3 k ops
#   2. dist_e   = champ de distance (sans obstacles) depuis reach_e sur toute la grille   ~13 k ops
#   3. dmg_e[d] = meilleure dépense de PT de e en ne gardant que ses skills de portée ≥ d
#   danger(c) = Σ_e dmg_e[dist_e[c]]
#
# Pessimiste par construction : LOS ennemie ignorée, portée min ignorée (il peut reculer), tout supposé
# disponible (cooldowns ennemis non lus). Les types de lancer LINE/DIAGONAL sont approximés en cercle.

from dataclasses import replace

import formulas
from geometry import INF, Grid, bfs_walk, dist_field
from skills import DAMAGE, POISON, Skill
from world import Ent, World

BASE = "base"
SHACKLED_MP = "mp"
SHACKLED_TP = "tp"


def best_spend(options: list[tuple[int, float, int]], tp: int) -> float:
    """Sac à dos borné : options (coût, valeur unitaire, usages max ; 0 = illimité) → meilleure valeur pour `tp`."""
    best = [0.0] * (tp + 1)
    for cost, value, max_uses in options:
        if cost <= 0 or cost > tp:
            continue
        cap = tp // cost if max_uses <= 0 else min(max_uses, tp // cost)
        # Répétition bornée : on déroule les usages (cap petit : ≤ 4 pour les armes, ≤ 6 pour les puces).
        for _ in range(cap):
            for t in range(tp, cost - 1, -1):
                v = best[t - cost] + value
                if v > best[t]:
                    best[t] = v
    return best[tp]


def unit_damage(sk: Skill, caster: Ent, target: Ent, poison_discount: float = 1.0) -> float:
    """Dégâts moyens d'UN usage de `sk` par `caster` sur `target` (poison : total sur ses tours, décoté)."""
    if sk.kind == DAMAGE:
        return formulas.damage(sk.avg, caster.strength, caster.power, target.rel_shield, target.abs_shield) * \
            formulas.expected_mult(caster.agility)
    if sk.kind == POISON:
        per_turn = formulas.poison(sk.avg, caster.magic, caster.power)
        total = 0.0
        w = 1.0
        for _ in range(max(1, sk.turns)):
            total += per_turn * w
            w *= poison_discount
        return total
    return 0.0


class Danger:
    def __init__(self, world: World, poison_discount: float = 0.7) -> None:
        self.world = world
        self.grid: Grid = world.grid
        self.poison_discount = poison_discount
        self._fields: dict[tuple[int, str], list[float]] = {}
        self._combined: dict[tuple, list[float]] = {}
        self._dist: dict[int, list[int]] = {}  # champ de distance à la zone atteignable de chaque ennemi (base)
        self._depth: list[int] | None = None
        self.enemies: dict[int, Ent] = {e.id: e for e in world.enemies}

    def variant_of(self, e: Ent, variant: str, amount: float = 0.0) -> Ent:
        if variant == SHACKLED_MP:
            return replace(e, mp=max(0, e.mp - round(amount)))
        if variant == SHACKLED_TP:
            return replace(e, tp=max(0, e.tp - round(amount)))
        return e

    def field(self, e: Ent, variant: str = BASE, amount: float = 0.0) -> list[float]:
        key = (e.id, variant)
        f = self._fields.get(key)
        if f is None:
            f = self._compute(self.variant_of(e, variant, amount))
            self._fields[key] = f
        return f

    def _compute(self, e: Ent) -> list[float]:
        world = self.world
        me = world.me
        reach = bfs_walk(self.grid, {e.cell: 0}, e.mp, world.blocked_for(e))
        dist = dist_field(self.grid, list(reach))
        self._dist.setdefault(e.id, dist)
        options = []
        max_range = 0
        for sk in e.skills:
            if sk.kind not in (DAMAGE, POISON) or not sk.available:
                continue
            v = unit_damage(sk, e, me, self.poison_discount)
            if v <= 0:
                continue
            options.append((sk.cost, v, sk.max_uses, sk.max_range))
            if sk.max_range > max_range:
                max_range = sk.max_range
        # dmg_at[d] ne change qu'aux portées max des skills : un sac à dos par seuil distinct, pas par distance.
        dmg_at = [0.0] * (max_range + 2)
        thresholds = sorted({r for _c, _v, _u, r in options}, reverse=True)
        thresholds.append(-1)
        for i in range(len(thresholds) - 1):
            r = thresholds[i]
            v = best_spend([(c, v, u) for c, v, u, rr in options if rr >= r], e.tp)
            for d in range(thresholds[i + 1] + 1, r + 1):
                dmg_at[d] = v
        out = [0.0] * self.grid.n
        last = max_range + 1
        for c in range(self.grid.n):
            d = dist[c]
            out[c] = dmg_at[d] if d < last else 0.0
        return out

    def combined(self, alive: dict[int, tuple[str, float]] | None = None) -> list[float]:
        """Champ de danger total pour un état ennemi donné, caché : une passe sur la grille par état distinct,
        puis O(1) par case. `alive` = {id ennemi: (variante, montant)} ; None = tous, état de base."""
        if alive is None:
            alive = {e.id: (BASE, 0.0) for e in self.enemies.values()}
        key = tuple(sorted((eid, v) for eid, (v, _a) in alive.items()))
        total = self._combined.get(key)
        if total is None:
            fields = []
            for eid, (variant, amount) in alive.items():
                e = self.enemies.get(eid)
                if e is not None:
                    fields.append(self.field(e, variant, amount))
            if not fields:
                total = [0.0] * self.grid.n
            elif len(fields) == 1:
                total = fields[0]
            else:
                total = list(fields[0])
                for f in fields[1:]:
                    for c in range(self.grid.n):
                        total[c] += f[c]
            self._combined[key] = total
        return total

    def at(self, cell: int, alive: dict[int, tuple[str, float]] | None = None) -> float:
        return self.combined(alive)[cell]

    def depth(self) -> list[int]:
        """Distance de chaque case à la zone atteignable ennemie la plus proche (état de base) : sert de
        tie-break au repli (plus c'est grand, plus on est loin de tout)."""
        depth = self._depth
        if depth is None:
            fields: list[list[int]] = []
            for e in self.enemies.values():
                self.field(e)  # garantit self._dist[e.id]
                fields.append(self._dist[e.id])
            if not fields:
                depth = [INF] * self.grid.n
            elif len(fields) == 1:
                depth = fields[0]
            else:
                depth = list(fields[0])
                for f in fields[1:]:
                    for c in range(self.grid.n):
                        if f[c] < depth[c]:
                            depth[c] = f[c]
            self._depth = depth
        return depth
